"""The one place NVDA's Linux object layer reads AT-SPI from.

Everything above this module asks for facts about a Node and never touches libatspi. A Node is an
accessible's identity, (unique bus name, object path), with a reference this client uses to reach it.
This client answers each question with libatspi through PyGObject. libatspi answers roles, names,
descriptions, parents and interfaces it has read before from its cache, which the events it
listens for keep current (see CACHE_MASK); everything else is one D-Bus round trip at a time. The
engine's mirror can stand behind the same calls later and answer more of them from memory.

Every round trip goes through call, which keeps an application that stops answering from stalling NVDA
more than once: libatspi waits for an answer on NVDA's main thread, and a property read that times out
returns 0 instead of failing.
"""

import collections
import dataclasses
import inspect
import threading
import time
import warnings
from collections.abc import Callable

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

#: The registry's path for the desktop, which every application root shares with it.
ROOT_PATH = "/org/a11y/atspi/accessible/root"
#: libatspi's deadline for one call, in milliseconds, also for applications it met in the last 15 s,
#: which otherwise get 15 s each.
TIMEOUT_MS = 300
#: A call that takes this long is logged, for finding out which applications answer slowly and when.
SLOW_ANSWER_SECONDS = 0.05
#: A call that takes this long has met libatspi's deadline. What it returns then is libatspi's, not the
#: application's: zero for most reads, and the states it last saw for a state set. The application is
#: taken for hung.
TIMED_OUT_AFTER_SECONDS = TIMEOUT_MS / 1000 - 0.01
#: Waiting on one application that answers, summed over its slow answers of the last
#: BUDGET_WINDOW_SECONDS, after which it is not asked for a while either. A busy program answers late
#: now and then, which is no hang; one that answers everything late would hold NVDA up as surely as
#: one that never answers.
WAIT_BUDGET_SECONDS = 1.0
BUDGET_WINDOW_SECONDS = 2.0
#: How long a hung application that sends no event is left alone before one call may try it again.
PROBE_AFTER_SECONDS = 2.0
#: Children read from one accessible at most; one GetChildren on Caja's list view froze it 4 s.
CHILDREN_LIMIT = 200
#: Accessibles visited at most when walking for one that has a state, where Collection is missing.
WALK_LIMIT = 500
#: What Atspi.init returns when it cannot connect to the accessibility bus.
_INIT_NO_BUS = 2
#: The desktop's bus name, which libatspi gives the desktop it returns.
REGISTRY_BUS_NAME = "org.a11y.atspi.Registry"


class NodeGone(LookupError):
	"""The accessible no longer exists, or its application does not answer."""


class AppHung(NodeGone):
	"""The accessible's application has stopped answering, so it was not asked."""


@dataclasses.dataclass(frozen=True, eq=False)
class Node:
	busName: str
	path: str
	#: libatspi's proxy; the engine would hold its own handle here.
	ref: object = dataclasses.field(repr=False, compare=False)

	@property
	def key(self) -> tuple[str, str]:
		return (self.busName, self.path)

	def __eq__(self, other: object) -> bool:
		return isinstance(other, Node) and self.key == other.key

	def __hash__(self) -> int:
		return hash(self.key)


@dataclasses.dataclass
class Relation:
	type: str
	targets: list[Node]


#: Round trips made through call, and the seconds spent in them, for measurement.
callCount = 0
callSeconds = 0.0
#: When each application that stopped answering was last given up on, by bus name.
_hungSince: dict[str, float] = {}
#: Applications no longer asked anything, by bus name.
_refused: set[str] = set()
#: When each application last answered slowly, and how long it took, over the budget's window.
_slowAnswers: dict[str, collections.deque[tuple[float, float]]] = {}
_mainThreadId = threading.main_thread().ident
#: Whether libatspi could not connect to the accessibility bus. It then ends the whole process with
#: abort() on the first call that needs the bus, so no call is made.
_hasNoBus = False


def _isPlainCallable(func: Callable) -> bool:
	return inspect.isfunction(func) or inspect.ismethod(func)


def call[T](node: Node, func: Callable[..., T], *args) -> T:
	"""One round trip to node's application: func(node.ref, *args), or func(*args) when args already
	start with node.ref or func is a Python function or bound method.

	Main thread only: libatspi is not thread safe. A call to an application marked hung raises AppHung
	without asking it. A call that met libatspi's deadline marks it and raises AppHung, since what
	libatspi returns then cannot be told from a real answer, so an application that really hangs holds
	NVDA up for TIMEOUT_MS at a time. An answer before the deadline, however late, is kept: a busy
	application is not a hung one, until its slow answers add up to WAIT_BUDGET_SECONDS.

	:raise NodeGone: The accessible is gone, or this is not the main thread.
	:raise AppHung: Its application does not answer.
	"""
	global callCount, callSeconds
	if threading.get_ident() != _mainThreadId:
		raise NodeGone("AT-SPI is only read on the main thread")
	if _hasNoBus:
		raise NodeGone("there is no accessibility bus")
	busName = node.busName
	if busName in _refused:
		raise NodeGone(busName)
	since = _hungSince.get(busName)
	if since is not None:
		now = time.monotonic()
		if now - since < PROBE_AFTER_SECONDS:
			raise AppHung(busName)
		# This call is the probe; until it answers, the application stays marked.
		_hungSince[busName] = now
	if not (args and args[0] is node.ref) and not _isPlainCallable(func):
		args = (node.ref, *args)
	callCount += 1
	start = time.monotonic()
	try:
		result = func(*args)
	except GLib.Error as e:
		error: NodeGone = NodeGone(e.message)
		result = None
	else:
		error = None
	elapsed = time.monotonic() - start
	callSeconds += elapsed
	if elapsed >= TIMED_OUT_AFTER_SECONDS:
		_markHung(busName, f"did not answer within {TIMEOUT_MS} ms")
		raise AppHung(busName) from None
	if since is not None:
		_hungSince.pop(busName, None)
	if elapsed >= SLOW_ANSWER_SECONDS:
		_noteSlowAnswer(busName, elapsed, getattr(func, "__name__", ""))
	if error is not None:
		raise error
	return result


def _markHung(busName: str, why: str) -> None:
	if busName not in _hungSince:
		from logHandler import log

		log.debugWarning(f"AT-SPI: {busName} {why}; not asking it for now")
	_hungSince[busName] = time.monotonic()
	_slowAnswers.pop(busName, None)


def _noteSlowAnswer(busName: str, elapsed: float, funcName: str) -> None:
	from logHandler import log

	log.debug(f"AT-SPI: slow answer {elapsed * 1000:.0f} ms from {busName} to {funcName}")
	now = time.monotonic()
	answers = _slowAnswers.setdefault(busName, collections.deque())
	answers.append((now, elapsed))
	while answers and now - answers[0][0] > BUDGET_WINDOW_SECONDS:
		answers.popleft()
	waited = sum(seconds for _at, seconds in answers)
	if waited >= WAIT_BUDGET_SECONDS:
		_markHung(busName, f"kept NVDA waiting {waited * 1000:.0f} ms in {BUDGET_WINDOW_SECONDS:g} s")


def heardFrom(busName: str) -> None:
	"""An event came from busName, so it answers again."""
	_hungSince.pop(busName, None)


def refuse(busName: str) -> None:
	"""Ask busName nothing more: every later call to it raises NodeGone."""
	_refused.add(busName)


def forgetApplications() -> None:
	_hungSince.clear()
	_slowAnswers.clear()
	_toolkits.clear()


_toolkits: dict[str, str] = {}

#: What libatspi may answer from memory once it has read it, each kept current by an event libatspi
#: always listens for: StateChanged for the states, PropertyChange for the rest. Applications built
#: on atk-bridge send an event only if some client registered for it, so CACHE_EVENT_TYPES must be
#: among the events listened to.
#: States have to be among them. libatspi fills its cache from each application's GetItems, whose
#: reply it can take inside the dispatch of another call, NVDA's call to its own process among them;
#: for every object in it libatspi asks whether it manages its descendants, and with states left out
#: of the mask that question is a call of its own, which libdbus cannot dispatch inside a dispatch, and
#: NVDA's main thread waits for ever. The states toolkits do not announce every change of, a frame's
#: active and a document's showing, are read with states(node, fresh=True).
#: Children are left out: libatspi learns them only from the application's cache, and keeps them
#: only through children-changed, which NVDA does not ask for, as a page sends thousands while it loads.
CACHE_MASK = (
	Atspi.Cache.NAME
	| Atspi.Cache.DESCRIPTION
	| Atspi.Cache.ROLE
	| Atspi.Cache.PARENT
	| Atspi.Cache.STATES
	| Atspi.Cache.INTERFACES
)
#: The events that keep CACHE_MASK's facts current, besides those NVDA presents.
CACHE_EVENT_TYPES = ("object:property-change:accessible-parent",)


def _enableCache() -> bool:
	"""Have libatspi answer from what it already knows, as it does under atspi_event_main.

	Without it, libatspi keeps what it reads and the events that change it, and still asks the
	application every time: every role and name NVDA reads is a round trip. NVDA runs wx's
	main loop rather than libatspi's, which is what keeps libatspi from trusting its cache, and setting
	a cache mask is libatspi's own way of saying the client keeps its events flowing. The mask is set
	on the desktop, whose mask every application without its own uses.

	It is off unless LINDA_ATSPI_CACHE=1, until NVDA forgets what toolkits change without saying so:
	with it on, a Pluma tab keeps its name from before its file was changed, and Caja's list view, once
	switched to, has its focused file go unannounced.
	"""
	import os

	if os.environ.get("LINDA_ATSPI_CACHE", "0") != "1":
		return False
	desktop = Atspi.get_desktop(0)
	if desktop is None:
		return False
	try:
		Atspi.Accessible.set_cache_mask(desktop, CACHE_MASK)
	except (TypeError, GLib.Error):
		return False
	return True


#: Applications whose own cache mask is set, by bus name.
_masked: set[str] = set()


def _maskApplication(app, busName: str) -> None:
	"""Give app CACHE_MASK as its own. libatspi uses the desktop's only for an application whose root it
	has seen as the desktop's child, and otherwise its default, which keeps children too."""
	try:
		root = app.root
	except AttributeError:
		root = None
	if root is None:
		# Asked again for the next node of this application, by when libatspi may know its root.
		return
	_masked.add(busName)
	if len(_masked) > 4096:
		_masked.clear()
	try:
		Atspi.Accessible.set_cache_mask(root, CACHE_MASK)
	except (TypeError, GLib.Error):
		pass


class Client:
	"""Questions about nodes, each one round trip to the application in this implementation."""

	def __init__(self) -> None:
		global _hasNoBus
		self._listener = None
		#: Whether libatspi answers from its cache; never without a bus.
		self.caching = False
		if Atspi.init() == _INIT_NO_BUS:
			from logHandler import log

			log.warning("AT-SPI: cannot connect to the accessibility bus; no program can be read")
			_hasNoBus = True
			return
		Atspi.set_timeout(TIMEOUT_MS, TIMEOUT_MS)
		self.caching = _enableCache()

	@property
	def hasBus(self) -> bool:
		"""Whether libatspi is connected to the accessibility bus. Without it every call on a node raises
		NodeGone, and the desktop has no children."""
		return not _hasNoBus

	def node(self, acc) -> Node | None:
		if acc is None:
			return None
		try:
			app = acc.app
			busName = app.bus_name if app else ""
			node = Node(busName, acc.path, acc)
		except AttributeError:
			return None
		if self.caching and busName not in _masked:
			_maskApplication(app, busName)
		return node

	def desktop(self) -> Node:
		if _hasNoBus:
			return Node(REGISTRY_BUS_NAME, ROOT_PATH, None)
		return self.node(Atspi.get_desktop(0))

	def applications(self) -> list[Node]:
		desktop = self.desktop()
		apps = []
		for i in range(call(desktop, Atspi.Accessible.get_child_count)):
			app = self.node(call(desktop, Atspi.Accessible.get_child_at_index, i))
			if app:
				apps.append(app)
		return apps

	def isHung(self, busName: str) -> bool:
		"""Whether busName is not to be asked now: once PROBE_AFTER_SECONDS have passed, the next call is
		the one that tries it again, so an application that went quiet as it recovered is not waited on
		for ever."""
		since = _hungSince.get(busName)
		return since is not None and time.monotonic() - since < PROBE_AFTER_SECONDS

	def toolkit(self, node: Node) -> str:
		"""The application's ToolkitName: "gtk" for GTK 3, "GTK" for GTK 4, "Qt", and so on."""
		name = _toolkits.get(node.busName)
		if name is None:
			# Qt answers ToolkitName only on its application object.
			name = call(node, lambda: (node.ref.get_application() or node.ref).get_toolkit_name()) or ""
			_toolkits[node.busName] = name
		return name

	def accessibleId(self, node: Node) -> str:
		"""The AccessibleId the toolkit gives the accessible, which names its widget class in GTK 4 and Qt."""
		return call(node, Atspi.Accessible.get_accessible_id) or ""

	def role(self, node: Node) -> str:
		# GTK 4 and Qt give their own names from GetRoleName; the number is AT-SPI's.
		role = call(node, Atspi.Accessible.get_role)
		if role == Atspi.Role.INVALID:
			# What libatspi answers, without asking, for an accessible it has seen go.
			raise NodeGone(node.path)
		return Atspi.role_get_name(role) or ""

	def name(self, node: Node) -> str:
		return call(node, Atspi.Accessible.get_name) or ""

	def description(self, node: Node) -> str:
		return call(node, Atspi.Accessible.get_description) or ""

	def forget(self, node: Node) -> None:
		"""Have libatspi ask the application again for everything it keeps of node."""
		if self.caching:
			Atspi.Accessible.clear_cache_single(node.ref)

	def states(self, node: Node, fresh: bool = False) -> frozenset[str]:
		"""node's states, from libatspi's cache where it keeps them.

		:param fresh: Ask the application, for a state toolkits change without saying so: a frame's
			active, a document's showing. libatspi can forget only all it knows of one object at once, so
			the object's name and role are asked again too, the next time they are read.
		"""
		if fresh:
			self.forget(node)
		stateSet = call(node, Atspi.Accessible.get_state_set)
		return frozenset(s.value_nick for s in stateSet.get_states())

	def attributes(self, node: Node) -> dict[str, str]:
		return dict(call(node, Atspi.Accessible.get_attributes) or {})

	def interfaces(self, node: Node) -> frozenset[str]:
		return frozenset(call(node, Atspi.Accessible.get_interfaces))

	def processID(self, node: Node) -> int:
		return call(node, Atspi.Accessible.get_process_id)

	def parent(self, node: Node) -> Node | None:
		return self.node(call(node, Atspi.Accessible.get_parent))

	def childCount(self, node: Node) -> int:
		return call(node, Atspi.Accessible.get_child_count)

	def childAt(self, node: Node, index: int) -> Node | None:
		return self.node(call(node, Atspi.Accessible.get_child_at_index, index))

	def children(self, node: Node, limit: int = CHILDREN_LIMIT) -> list[Node]:
		"""node's first limit children, one at a time; none for a container that manages its
		descendants, which may have thousands made on demand."""
		if "manages-descendants" in self.states(node):
			return []
		count = min(self.childCount(node), limit)
		return [c for c in (self.childAt(node, i) for i in range(count)) if c]

	def indexInParent(self, node: Node) -> int:
		return call(node, Atspi.Accessible.get_index_in_parent)

	def relations(self, node: Node) -> list[Relation]:
		result = []
		for rel in call(node, Atspi.Accessible.get_relation_set) or []:
			targets = [self.node(rel.get_target(i)) for i in range(rel.get_n_targets())]
			result.append(Relation(rel.get_relation_type().value_nick, [t for t in targets if t]))
		return result

	def extents(self, node: Node) -> tuple[int, int, int, int] | None:
		rect = call(node, Atspi.Component.get_extents, Atspi.CoordType.SCREEN)
		if rect.width <= 0 and rect.height <= 0:
			return None
		return (rect.x, rect.y, rect.width, rect.height)

	def accessibleAtPoint(self, node: Node, x: int, y: int) -> Node | None:
		return self.node(
			call(node, Atspi.Component.get_accessible_at_point, x, y, Atspi.CoordType.SCREEN),
		)

	def grabFocus(self, node: Node) -> bool:
		return call(node, Atspi.Component.grab_focus)

	def value(self, node: Node) -> tuple[float, float, float, str]:
		return (
			call(node, Atspi.Value.get_current_value),
			call(node, Atspi.Value.get_minimum_value),
			call(node, Atspi.Value.get_maximum_value),
			call(node, Atspi.Value.get_text) or "",
		)

	def actions(self, node: Node) -> list[tuple[str, str]]:
		return [
			(
				call(node, lambda i=i: _actionName(node.ref, i)),
				call(node, Atspi.Action.get_key_binding, i),
			)
			for i in range(call(node, Atspi.Action.get_n_actions))
		]

	def doAction(self, node: Node, index: int) -> bool:
		return call(node, Atspi.Action.do_action, index)

	def selectedChild(self, node: Node, index: int = 0) -> Node | None:
		return self.node(call(node, Atspi.Selection.get_selected_child, index))

	def selectedCount(self, node: Node) -> int:
		return call(node, Atspi.Selection.get_n_selected_children)

	def tableSize(self, node: Node) -> tuple[int, int]:
		return (call(node, Atspi.Table.get_n_rows), call(node, Atspi.Table.get_n_columns))

	def cellPosition(self, node: Node) -> tuple[int, int]:
		_ok, row, column = call(node, Atspi.TableCell.get_position)
		return (row, column)

	def cellTable(self, node: Node) -> Node | None:
		return self.node(call(node, Atspi.TableCell.get_table))

	def cellAt(self, table: Node, row: int, column: int) -> Node | None:
		return self.node(call(table, Atspi.Table.get_accessible_at, row, column))

	def columnHeader(self, table: Node, column: int) -> str:
		"""The text of a table's column header: its header object's name, or the column's description."""
		header = self.node(call(table, Atspi.Table.get_column_header, column))
		name = self.name(header) if header else ""
		return name or call(table, Atspi.Table.get_column_description, column) or ""

	def columnHeaderNames(self, node: Node) -> list[str]:
		headers = call(node, Atspi.TableCell.get_column_header_cells) or []
		return call(node, lambda: [h.get_name() or "" for h in headers])

	def text(self, node: Node, start: int = 0, end: int = -1) -> str:
		return call(node, Atspi.Text.get_text, start, end) or ""

	def characterCount(self, node: Node) -> int:
		return call(node, Atspi.Text.get_character_count)

	def caretOffset(self, node: Node) -> int:
		return call(node, Atspi.Text.get_caret_offset)

	def setCaretOffset(self, node: Node, offset: int) -> bool:
		return call(node, Atspi.Text.set_caret_offset, offset)

	def selections(self, node: Node) -> list[tuple[int, int]]:
		count = call(node, Atspi.Text.get_n_selections)
		result = []
		for i in range(count):
			r = call(node, Atspi.Text.get_selection, i)
			result.append((r.start_offset, r.end_offset))
		return result

	def stringAtOffset(self, node: Node, offset: int, unit: str) -> tuple[int, int, str]:
		granularity = {
			"char": Atspi.TextGranularity.CHAR,
			"word": Atspi.TextGranularity.WORD,
			"sentence": Atspi.TextGranularity.SENTENCE,
			"line": Atspi.TextGranularity.LINE,
			"paragraph": Atspi.TextGranularity.PARAGRAPH,
		}[unit]
		r = call(node, Atspi.Text.get_string_at_offset, offset, granularity)
		return (r.start_offset, r.end_offset, r.content or "")

	def _firstMatch(self, node: Node, rule) -> Node | None:
		matches = call(
			node,
			Atspi.Collection.get_matches,
			rule,
			Atspi.CollectionSortOrder.CANONICAL,
			1,
			True,
		)
		return self.node(matches[0]) if matches else None

	def firstMatchWithRole(self, node: Node, roleName: str) -> Node | None:
		"""The first descendant of node, in tree order, with the role libatspi names roleName."""
		role = next(
			r for r in (Atspi.Role(i) for i in range(Atspi.ROLE_COUNT)) if Atspi.role_get_name(r) == roleName
		)
		rule = Atspi.MatchRule.new(
			None,
			Atspi.CollectionMatchType.NONE,
			None,
			Atspi.CollectionMatchType.NONE,
			[role],
			Atspi.CollectionMatchType.ANY,
			None,
			Atspi.CollectionMatchType.NONE,
			False,
		)
		return self._firstMatch(node, rule)

	def firstMatchWithState(self, node: Node, state: str, limit: int = WALK_LIMIT) -> Node | None:
		"""The first descendant of node, in tree order, that has state and is showing: through
		Collection, or for GTK 4, which has none, by a walk of at most limit accessibles."""
		matches = self.matchesWithState(node, state, 1, limit)
		return matches[0] if matches else None

	def matchesWithState(self, node: Node, state: str, count: int, limit: int = WALK_LIMIT) -> list[Node]:
		"""The first count descendants of node, in tree order, that have state and are showing; for GTK 4,
		which has no Collection, only the first, found by a walk of at most limit accessibles."""
		# Showing too: Qt leaves a closed combo box's current item focused.
		if "Collection" not in self.interfaces(node):
			found = self._walkForState(node, state, limit)
			return [found] if found is not None else []
		stateSet = Atspi.StateSet.new(
			[getattr(Atspi.StateType, state.upper().replace("-", "_")), Atspi.StateType.SHOWING],
		)
		rule = Atspi.MatchRule.new(
			stateSet,
			Atspi.CollectionMatchType.ALL,
			None,
			Atspi.CollectionMatchType.NONE,
			None,
			Atspi.CollectionMatchType.NONE,
			None,
			Atspi.CollectionMatchType.NONE,
			False,
		)
		matches = call(
			node,
			Atspi.Collection.get_matches,
			rule,
			Atspi.CollectionSortOrder.CANONICAL,
			count,
			True,
		)
		return [found for found in (self.node(match) for match in matches or []) if found is not None]

	def _walkForState(self, node: Node, state: str, limit: int) -> Node | None:
		# Entries are a node, or a (parent, index) pair for a child not fetched yet.
		stack: list[Node | tuple[Node, int]] = list(reversed(self.children(node)))
		visited = 0
		while stack and visited < limit:
			current = stack.pop()
			visited += 1
			try:
				if isinstance(current, tuple):
					current = self.childAt(*current)
					if current is None:
						continue
				states = self.states(current)
			except AppHung:
				raise
			except NodeGone:
				continue
			if "showing" not in states:
				continue
			if state in states:
				return current
			if "manages-descendants" in states:
				continue
			try:
				count = min(self.childCount(current), CHILDREN_LIMIT)
				# Each child is fetched only when the walk reaches it: the match is usually found long
				# before the last of a parent's children, each of which costs a round trip.
				stack.extend((current, i) for i in reversed(range(count)))
			except AppHung:
				raise
			except NodeGone:
				continue
		return None

	def listen(self, eventTypes: list[str], callback: Callable[..., None]) -> None:
		"""Call callback(type, node, detail1, detail2, anyData) for each event, on the main thread,
		whenever GLib's default context runs, which is inside wx's main loop and inside wx.Yield."""

		def onEvent(event) -> None:
			anyData = event.any_data
			if isinstance(anyData, Atspi.Accessible):
				anyData = self.node(anyData)
			callback(event.type, self.node(event.source), event.detail1, event.detail2, anyData)

		if _hasNoBus:
			return
		self._listener = Atspi.EventListener.new(onEvent)
		for eventType in eventTypes:
			self._listener.register(eventType)

	def stopListening(self, eventTypes: list[str]) -> None:
		if self._listener:
			for eventType in eventTypes:
				try:
					self._listener.deregister(eventType)
				except GLib.Error:
					# A registry too busy to answer in time; the registration ends with NVDA's connection.
					break
			self._listener = None


def _actionName(ref, index: int) -> str:
	try:
		return Atspi.Action.get_localized_name(ref, index)
	except GLib.Error:
		# Qt answers only GetName, which libatspi reaches through a deprecated function whose warning
		# would be logged.
		with warnings.catch_warnings():
			warnings.simplefilter("ignore", DeprecationWarning)
			return Atspi.Action.get_action_name(ref, index)


_client: Client | None = None


def _instance() -> Client:
	global _client
	if _client is None:
		_client = Client()
	return _client


def __getattr__(name: str) -> Client:
	# The client connects to the accessibility bus, which importing this module must not do.
	if name == "client":
		return _instance()
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
