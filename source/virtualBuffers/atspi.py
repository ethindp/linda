"""Browse mode for web documents read over AT-SPI: Firefox now, Chromium later.

NVDA's own buffer storage holds the document (linuxPlatform.vbufLib), and the engine's renderer fills
it (linuxPlatform.vbufEngine), walking the document over its own connection on a thread. Nothing is
injected into the browser, so there is no binding handle: prepare does not ask for one. The engine
watches each rendered document; its changes reach the buffer through linuxPlatform.vbufUpdates, and
its document events through onEngineEvents.
"""

import ctypes
import threading
import time

import api
import aria
import browseMode
import config
import queueHandler
import textInfos
import tones
import treeInterceptorHandler
import ui
from controlTypes import IsCurrent, OutputReason, Role, State
from linuxPlatform import vbufEngine, vbufLib
from linuxPlatform.atspi import accessibleFor, handler
from linuxPlatform.atspi.client import Node, NodeGone
from logHandler import log

from . import (
	VBufStorage_findMatch_notEmpty,
	VBufStorage_findMatch_word,
	VirtualBuffer,
	VirtualBufferTextInfo,
)

try:
	from linuxPlatform import vbufUpdates
except ImportError:
	# Without the update thread a buffer keeps the page as it was rendered, until NVDA+F5.
	vbufUpdates = None

#: The object path every accessible of the ATK bridge and of Qt has, with a number at its end.
_PATH_PREFIX = "/org/a11y/atspi/accessible/"

#: Firefox focuses a new document as soon as it exists, often holding no more than the page's first
#: lines, and say all would stop after them. So the first reading of a document still loading waits
#: until the load ends, or until the page has stopped changing for LOAD_QUIET_SECONDS, but never longer
#: than LOAD_HOLD_SECONDS; the rest reaches the buffer as updates meanwhile.
LOAD_QUIET_SECONDS = 0.4
LOAD_HOLD_SECONDS = 2.0

_EMBEDDED_OBJECT = "\ufffc"


class Identifiers:
	"""docHandle and ID for AT-SPI identities: a small number per bus name, and the number at the end
	of an object path cast to signed 32 bits. The ATK bridge counts up from 1 and never reuses a
	number, and Qt counts from 2^31; the engine's renderer applies the same rule."""

	def __init__(self) -> None:
		self._docHandles: dict[str, int] = {}
		self._busNames: dict[int, str] = {}
		self._lock = threading.Lock()

	def docHandle(self, busName: str) -> int:
		with self._lock:
			handle = self._docHandles.get(busName)
			if handle is None:
				handle = self._docHandles[busName] = len(self._docHandles) + 1
				self._busNames[handle] = busName
			return handle

	def busName(self, docHandle: int) -> str | None:
		return self._busNames.get(docHandle)

	@staticmethod
	def id(path: str) -> int:
		"""The ID for path; 0, which no accessible has, for an application's root. A path ending in
		something other than a number, as WebKitGTK's do, has the ID the engine gave it."""
		tail = path.rpartition("/")[2]
		if tail.isdecimal() and int(tail) < 2**32:
			return ctypes.c_int32(int(tail)).value
		if tail.isdecimal() or tail in ("", "root") or not vbufEngine.isAvailable():
			return 0
		return vbufEngine.namedId(path)

	@staticmethod
	def path(ID: int) -> str:
		if ID < 0 and vbufEngine.isAvailable() and (named := vbufEngine.namedPath(ID)) is not None:
			return named
		return f"{_PATH_PREFIX}{ID & 0xFFFFFFFF}"

	def of(self, node: Node) -> tuple[int, int]:
		return self.docHandle(node.busName), self.id(node.path)


#: One numbering of bus names for every document, so that an identifier in one buffer means the same in
#: another.
identifiers = Identifiers()

#: Applications whose actions the buffer has asked for once. Firefox fills each part of its
#: accessibility cache only when a client first asks for it, and until then answers that an object has
#: no actions and drops DoAction, so the first Enter on a link would do nothing.
_actionsPrimed: set[str] = set()

#: Documents the engine could not render, by bus name and path, which are given no buffer again.
_unrenderable: set[tuple[str, str]] = set()

#: Role.LIST is left out: the renderer gives it to HTML lists as well as to list boxes.
_FORM_FIELD_ROLES = (
	Role.BUTTON,
	Role.CHECKBOX,
	Role.COMBOBOX,
	Role.TREEVIEW,
	Role.RADIOBUTTON,
	Role.TAB,
	Role.TOGGLEBUTTON,
	Role.MENUBUTTON,
	Role.SLIDER,
	Role.SPINBUTTON,
)

#: Firefox's AT-SPI roles that linuxPlatform.atspi.roles leaves out, as Gecko's IAccessible2 role
#: presents them on Windows.
_EXTRA_ROLES = {
	"panel": Role.GROUPING,
	"label": Role.LABEL,
	"menu": Role.POPUPMENU,
}

#: The states of a column or row header that the page says is sorted, by its sort object attribute.
_SORT_STATES = {
	"ascending": State.SORTED_ASCENDING,
	"descending": State.SORTED_DESCENDING,
	"other": State.SORTED,
}

#: The roles NVDA treats as an application inside a document, whose content browse mode leaves alone.
_APPLICATION_ROLES = (Role.APPLICATION, Role.DIALOG)


def _role(*roles: Role) -> list[str]:
	return [str(r.value) for r in roles]


def _state(state: State) -> str:
	return f"state_{state.value}"


class AtSpiBufferTextInfo(VirtualBufferTextInfo):
	def _normalizeControlField(self, attrs: textInfos.ControlField):
		role = Role(int(attrs.get("role", Role.UNKNOWN.value)))
		atspiRole = attrs.get("atspi::role", "")
		states = {State(int(key[6:])) for key in attrs if key.startswith("state_")}
		xmlRoles = attrs.get("xml-roles", "").split(" ")
		tag = attrs.get("tag", "").lower()
		if role == Role.UNKNOWN:
			role = _EXTRA_ROLES.get(atspiRole, Role.UNKNOWN)
		if tag == "img" and role == Role.STATICTEXT:
			# Firefox gives an image with alt text the static role, where IAccessible2 says graphic.
			role = Role.GRAPHIC
		elif tag == "blockquote":
			role = Role.BLOCKQUOTE
		elif xmlRoles[0] == "application":
			# Gecko's ATK mapping makes an ARIA application an embedded object.
			role = Role.APPLICATION
		elif role == Role.DOCUMENT and xmlRoles[0] == "article":
			role = Role.ARTICLE
		elif role in (Role.GROUPING, Role.SECTION) and xmlRoles[0] == "figure":
			role = Role.FIGURE
		elif role in (Role.LANDMARK, Role.SECTION) and xmlRoles[0] == "region":
			role = Role.REGION
		elif xmlRoles[0] == "switch":
			role = Role.SWITCH
			states.discard(State.PRESSED)
			states.discard(State.CHECKABLE)
			if State.CHECKED in states:
				states.discard(State.CHECKED)
				states.add(State.ON)
		elif role == Role.EDITABLETEXT and not states & {State.EDITABLE, State.FOCUSABLE, State.UNAVAILABLE}:
			role = Role.STATICTEXT
		if atspiRole == "password text":
			states.add(State.PROTECTED)
		if atspiRole in ("list", "list item"):
			# Gecko marks an HTML list read-only through IAccessible2, which tells it from a list box: one
			# without it is spoken on every line inside it.
			states.add(State.READONLY)
		popupState = aria.ariaHaspopupValuesToNVDAStates.get(attrs.get("haspopup"))
		if popupState:
			states.discard(State.HASPOPUP)
			states.add(popupState)
		if sortState := _SORT_STATES.get(attrs.get("sort")):
			states.add(sortState)
		if "atspi::action_click" in attrs:
			# The renderer marks an object whose first action is click, as IAccessibleAction_click.
			states.add(State.CLICKABLE)
		landmark = next((r for r in xmlRoles if r in aria.landmarkRoles), None)
		if landmark and role != Role.LANDMARK and landmark != xmlRoles[0]:
			landmark = None
		if landmark:
			attrs["landmark"] = landmark
		if roleText := attrs.get("roledescription"):
			attrs["roleText"] = roleText
		placeholder = self._getPlaceholderAttribute(attrs, "placeholder-text")
		if placeholder is not None:
			attrs["placeholder"] = placeholder
		current = attrs.get("current", "false")
		try:
			isCurrent = IsCurrent(current)
		except ValueError:
			isCurrent = IsCurrent.YES
		if isCurrent != IsCurrent.NO:
			attrs["current"] = isCurrent
		if role == Role.TABLE:
			self._addTableFacts(attrs)
		attrs["role"] = role
		attrs["states"] = states
		return super()._normalizeControlField(attrs)

	def _addTableFacts(self, attrs: textInfos.ControlField) -> None:
		"""Row and column counts and the layout guess for a table field that the renderer left them out
		of, asked of the table itself on the main thread."""
		# The renderer gives the counts, and table-layout only to a table Firefox guesses is for layout.
		if "table-rowcount" in attrs:
			return
		try:
			facts = self.obj._tableFacts(
				int(attrs["controlIdentifier_docHandle"]), int(attrs["controlIdentifier_ID"])
			)
		except (KeyError, ValueError):
			return
		if facts is None:
			return
		rows, columns, isLayout = facts
		attrs.setdefault("table-rowcount", str(rows))
		attrs.setdefault("table-columncount", str(columns))
		attrs.setdefault("table-layout", "1" if isLayout else "0")

	def _get_location(self):
		obj = self._getNVDAObjectFromOffset(self._startOffset)
		return obj.location if obj else None


class AtSpiBuffer(VirtualBuffer):
	TextInfo = AtSpiBufferTextInfo

	def __init__(self, rootNVDAObject):
		root = rootNVDAObject.node
		self.busName: str = root.busName
		self.docPath: str = root.path
		#: Whether the whole document is in the buffer; a large one arrives in parts.
		self.complete = False
		self._docURL: str | None = None
		#: Set when the engine says the document was reloaded or replaced; nothing brings it back.
		self._gone = False
		self._terminated = False
		#: (rows, columns, layout) of each table, by (docHandle, ID), or None when it could not be asked.
		self._tables: dict[tuple[int, int], tuple[int, int, bool] | None] = {}
		#: Ends the wait for a document still loading when it was rendered; None when not waiting.
		self._loadHold = None
		#: time.monotonic() by which that wait ends whatever the page does.
		self._loadHoldUntil = 0.0
		#: Whether the document said last that it was busy, as it does when it is being left.
		self._busy = False
		super().__init__(rootNVDAObject, backendName="atspi")

	def prepare(self):
		# No helper is injected into the browser, so there is no binding handle to wait for.
		self.shouldPrepare = False
		self.loadBuffer()

	def loadBuffer(self):
		import wx

		self.isLoading = True
		self._loadProgressCallLater = wx.CallLater(1000, self._loadProgress)
		threading.Thread(
			name=f"{self.__class__.__module__}.{self.loadBuffer.__qualname__}",
			target=self._loadWithEngine,
			args=(self.busName, self.docPath, identifiers.docHandle(self.busName)),
			daemon=True,
		).start()

	def _loadWithEngine(self, busName: str, path: str, docHandle: int) -> None:
		start = time.perf_counter()
		handle = None
		try:
			if vbufUpdates is not None:
				vbufUpdates.expect(busName, path)
			rendered = vbufEngine.render(busName, path, docHandle, progressive=True)
			renderedAt = time.perf_counter()
			try:
				handle = vbufLib.create()
				vbufLib.build(handle, rendered.stream)
			finally:
				rendered.stream.free()
		except RuntimeError as e:
			# The engine's own refusal, which names the application and the call that failed.
			log.warning(f"The engine could not render the document, which is read without browse mode: {e}")
			self._loadFailed(handle)
			return
		except Exception:
			log.exception("The engine could not render the document")
			self._loadFailed(handle)
			return
		log.info(
			f"Buffer rendered by the engine in {renderedAt - start:.3f} s, built in "
			f"{time.perf_counter() - renderedAt:.4f} s, {vbufLib.lib().VBuf_getTextLength(handle)} "
			f"code units: {rendered.info}",
		)
		queueHandler.queueFunction(queueHandler.eventQueue, self._engineLoadDone, handle, rendered.info)

	def _loadFailed(self, handle: int | None) -> None:
		if handle:
			vbufLib.destroy(handle)
		queueHandler.queueFunction(queueHandler.eventQueue, self._engineLoadDone, None, {})

	def _engineLoadDone(self, handle: int | None, info: dict) -> None:
		"""On the main thread: take the built buffer, unless this interceptor died while it rendered."""
		if self._terminated:
			self._loadProgressCallLater.Stop()
			if handle:
				vbufLib.destroy(handle)
			self._unwatch()
			return
		if not handle:
			# A buffer left without content held the document in focus mode with nothing behind it, so
			# NVDA+space and the review cursor met an empty buffer. Without one, focus and review read
			# the document's own objects.
			self._loadBufferDone(success=False)
			_unrenderable.add((self.busName, self.docPath))
			treeInterceptorHandler.killTreeInterceptor(self)
			# Translators: Reported when a document cannot be read in browse mode, only through its objects.
			ui.message(_("No browse mode for this document"))
			return
		self.VBufHandle = ctypes.c_void_p(handle)
		self._docURL = info.get("docURL") or self._docURL
		self.complete = bool(info.get("complete"))
		self._tables.clear()
		if vbufUpdates is not None:
			vbufUpdates.register(self)
		self._takeFocus()
		if info.get("busy") and vbufUpdates is not None:
			import wx

			self._loadHoldUntil = time.monotonic() + LOAD_HOLD_SECONDS
			self._loadHold = wx.CallLater(int(LOAD_QUIET_SECONDS * 1000), self._endLoadHold)
			return
		self._loadBufferDone()
		self._primeActions()

	def _extendLoadHold(self) -> None:
		"""The page changed while its first reading waits: wait until it is quiet again."""
		left = self._loadHoldUntil - time.monotonic()
		if left <= 0:
			self._endLoadHold()
		else:
			self._loadHold.Start(int(min(left, LOAD_QUIET_SECONDS) * 1000))

	def _endLoadHold(self) -> None:
		"""On the main thread: report the document, now that it has loaded or waiting for it is over."""
		hold = self._loadHold
		if hold is None:
			return
		self._loadHold = None
		hold.Stop()
		if self._terminated:
			return
		# The page may have focused one of its fields while it loaded.
		self._takeFocus()
		self._loadBufferDone()
		self._primeActions()

	def _takeFocus(self) -> None:
		"""Make this buffer the focus's tree interceptor when the focus is in it but came before it,
		when membership could not yet be answered from the buffer alone, as when Firefox restores the
		focus to a link on going back or a page focuses its search box as it loads."""
		focus = api.getFocusObject()
		if focus.treeInterceptor is None and focus in self:
			focus.treeInterceptor = self
		else:
			self._startDialogBuffer()

	def event_treeInterceptor_loseFocus(self):
		self._startDialogBuffer()

	def _startDialogBuffer(self) -> None:
		"""Give the dialog the focus has moved into a buffer of its own, when the dialog is on this
		buffer's page and has none yet.

		This buffer leaves a dialog's content out, as an application, and the focus inside it then has
		no tree interceptor: arrows and quick navigation say nothing, and NVDA+Space does not help. On
		Windows behaviors.WebDialog gives such a dialog a buffer; the AT-SPI objects ask
		treeInterceptorClassFor only for a document, so the buffer is started here, as
		treeInterceptorHandler.update would start it, once the focus has left this buffer for it.
		"""
		focus = api.getFocusObject()
		if focus.treeInterceptor is not None:
			return
		chain = [*api.getFocusAncestors(), focus]
		try:
			ownIndex = next(i for i, obj in enumerate(chain) if obj == self.rootNVDAObject)
			dialog = next(
				obj for obj in reversed(chain[ownIndex + 1 :]) if getattr(obj, "atspiRole", None) == "dialog"
			)
		except (StopIteration, NodeGone):
			return
		for running in treeInterceptorHandler.runningTable:
			if isinstance(running, AtSpiBuffer) and running.rootNVDAObject == dialog:
				return
		buffer = AtSpiBuffer(dialog)
		if not buffer.isAlive:
			return
		treeInterceptorHandler.runningTable.add(buffer)
		log.debug(f"Adding a buffer for a dialog on the page: {buffer}")
		# Once loaded, it takes the focus, which is in it by then (_takeFocus).
		if buffer.shouldPrepare:
			buffer.prepare()

	def onEngineEvents(self, events: tuple[str, ...], info: dict) -> None:
		"""On the main thread, after the update that came with events, if any, was applied."""
		if self._terminated:
			return
		self._tables.clear()
		if "urlChanged" in events or info.get("docURL"):
			self._docURL = info.get("docURL") or self._docURL
		if "complete" in events:
			self.complete = True
		if "reload" in events or "gone" in events:
			# Firefox makes a new document object for a reload and for every page followed, and
			# that one gets a buffer of its own when it takes the focus.
			self._gone = True
			if self._loadHold is not None:
				self._loadHold.Stop()
				self._loadHold = None
			# What Firefox takes from the page's objects as they go may be told only after this buffer,
			# which says nothing of it, is gone. NVDA speaks such a change of the focus and of what
			# holds it, so those are told that their page went.
			for obj in (*api.getFocusAncestors(), api.getFocusObject()):
				if obj in self:
					obj.wentWithItsPage = True
			treeInterceptorHandler.killTreeInterceptor(self)
			return
		if self._loadHold is not None:
			if "loadComplete" in events:
				self._endLoadHold()
			else:
				self._extendLoadHold()
			return
		if self.VBufHandle:
			self._handleUpdate()
			self._adoptFocus()
		if "loadComplete" in events:
			self.event_documentLoadComplete(self.rootNVDAObject, lambda: None)

	def _adoptFocus(self) -> None:
		"""Take the focus as this document's when its focus event came before the update that brought
		it into the buffer, as a field does that a page builds and focuses after it has loaded: without
		this the field stays outside both browse and focus mode, and Escape does nothing."""
		focus = api.getFocusObject()
		if focus.treeInterceptor is not None or focus not in self:
			return
		focus.treeInterceptor = self
		try:
			self._set_selection(self.makeTextInfo(focus), reason=OutputReason.FOCUS)
		except LookupError:
			pass
		if not self.passThrough and self.shouldPassThrough(focus, reason=OutputReason.FOCUS):
			self.passThrough = True
			browseMode.reportPassThrough(self)

	def applyUpdate(self, stream: "vbufEngine.Stream") -> bool:
		"""On the main thread: apply an update batch to the storage. The storage keeps the caret on its
		text; the text say all reads next is kept on its text here, or reading would go on from the old
		offsets, now in other words, as a page changes above the reader.

		:return: Whether the whole batch was applied.
		"""
		handle = self.VBufHandle.value
		reader = self._sayAllReader()
		anchors = None
		if reader is not None:
			anchors = [vbufLib.anchor(handle, offset) for offset in (reader._startOffset, reader._endOffset)]
		applied = vbufLib.update(handle, stream)
		if anchors is not None and None not in anchors:
			start, end = (vbufLib.place(handle, where) for where in anchors)
			if start is not None and end is not None and start <= end:
				reader._startOffset, reader._endOffset = start, end
		return applied

	def _sayAllReader(self) -> textInfos.offsets.OffsetsTextInfo | None:
		"""The text say all reads next, while it reads this buffer."""
		from speech import sayAll

		handler = sayAll.SayAllHandler
		active = handler._getActiveSayAll() if handler is not None else None
		reader = getattr(active, "reader", None)
		if isinstance(reader, textInfos.offsets.OffsetsTextInfo) and reader.obj is self:
			return reader
		return None

	def _unwatch(self) -> None:
		unwatch = getattr(vbufEngine, "unwatch", None)
		# An engine that does not watch documents has nothing to forget.
		if unwatch is not None:
			try:
				unwatch(self.busName, self.docPath)
			except Exception:
				log.exception("The engine could not stop watching the document")

	def unloadBuffer(self):
		if vbufUpdates is not None:
			vbufUpdates.unregister(self)
		self._unwatch()
		if self.VBufHandle:
			vbufLib.destroy(self.VBufHandle.value)
		self.VBufHandle = None
		self._tables.clear()

	def terminate(self):
		self._terminated = True
		if self._loadHold is not None:
			self._loadHold.Stop()
			self._loadHold = None
		super().terminate()

	def _primeActions(self) -> None:
		if self.busName in _actionsPrimed:
			return
		_actionsPrimed.add(self.busName)
		try:
			link = next(self._iterNodesByType("link"), None)
			obj = link.obj if link is not None else None
			if obj is not None:
				handler.client().actions(obj.node)
		except (NodeGone, LookupError):
			pass

	def _tableFacts(self, docHandle: int, ID: int) -> tuple[int, int, bool] | None:
		key = (docHandle, ID)
		if key not in self._tables:
			self._tables[key] = None
			try:
				obj = self.getNVDAObjectFromIdentifier(docHandle, ID)
				if obj is not None:
					rows, columns = handler.client().tableSize(obj.node)
					isLayout = obj.atspiAttributes.get("layout-guess") == "true"
					self._tables[key] = (rows, columns, isLayout)
			except NodeGone:
				pass
		return self._tables[key]

	def __contains__(self, obj):
		# Local state only: a browser that hangs must not stall every event NVDA handles.
		node = getattr(obj, "node", None)
		if not isinstance(node, Node) or node.busName != self.busName:
			return False
		if node == self.rootNVDAObject.node:
			return True
		if not self.VBufHandle:
			return False
		docHandle, ID = identifiers.of(node)
		found = ctypes.c_uint64()
		lib = vbufLib.lib()
		lib.VBuf_getControlFieldNodeWithIdentifier(self.VBufHandle, docHandle, ID, ctypes.byref(found))
		if not found:
			return False
		start = ctypes.c_int()
		end = ctypes.c_int()
		if not lib.VBuf_getFieldNodeOffsets(self.VBufHandle, found, ctypes.byref(start), ctypes.byref(end)):
			return False
		pos = self.makeTextInfo(textInfos.offsets.Offsets(start.value, start.value))
		application = next(self._iterNodesByAttribs({"role": _role(*_APPLICATION_ROLES)}, "up", pos), None)
		if application is None:
			return True
		# A dialog's own buffer has the dialog as its root, which is not an application inside it.
		return application.vbufFieldIdentifier == identifiers.of(self.rootNVDAObject.node)

	def _get_isAlive(self):
		# Never a call into the browser (#7818). A hung browser keeps its buffer, as on Windows.
		return not self._gone

	def getNVDAObjectFromIdentifier(self, docHandle, ID):
		busName = identifiers.busName(docHandle)
		if not busName or not ID:
			return None
		node = accessibleFor.nodeFor(busName, identifiers.path(ID))
		if node is None:
			return None
		return handler.getObject(node)

	def getIdentifierFromNVDAObject(self, obj):
		return identifiers.of(obj.node)

	def _get_documentConstantIdentifier(self):
		return self._docURL

	def _get_documentURL(self) -> str | None:
		return self._docURL

	def _shouldIgnoreFocus(self, obj):
		if obj.role == Role.DOCUMENT and State.EDITABLE not in obj.states:
			return True
		return super()._shouldIgnoreFocus(obj)

	def event_gainFocus(self, obj, nextHandler):
		# The handler read obj's states to judge the focus before the browser's state changes for the
		# same click had arrived; browse mode caches them now as what the next change is spoken against.
		obj.invalidateCache()
		return super().event_gainFocus(obj, nextHandler)

	event_gainFocus.ignoreIsReady = True

	#: Where the browser put its caret while the focus was outside the page, as Firefox's find bar
	#: selects each match it finds.
	_caretWhileAway = None

	def event_treeInterceptor_gainFocus(self):
		caretWhileAway, self._caretWhileAway = self._caretWhileAway, None
		if caretWhileAway is not None and self._hadFirstGainFocus and not self.passThrough:
			try:
				info = self.makeTextInfo(caretWhileAway)
			except (LookupError, NotImplementedError, RuntimeError):
				info = None
			if info is not None:
				info.collapse()
				# What the page says as it takes the focus back is then read from the match.
				self.selection = info
		super().event_treeInterceptor_gainFocus()

	def event_caret(self, obj, nextHandler):
		if self.passThrough:
			return nextHandler()
		if api.getFocusObject().treeInterceptor is not self:
			# The page is not read while the focus is in the find bar; the caret goes to the match
			# once the find bar closes.
			self._caretWhileAway = obj
			return nextHandler()
		# Firefox moves its caret, and sends no scrolling event, when a same-page link is followed or
		# a script moves the focus; the virtual caret follows it there.
		if obj != self.rootNVDAObject and obj in self:
			self._handleScrollTo(obj)

	def event_typedCharacter(self, obj, nextHandler, ch):
		# Firefox writes an embedded object character into a loading document's text for each object
		# the page adds, which the echo of text inserted into the focus would read as typed. Nothing is
		# typed into a document that is not editable.
		if obj == self.rootNVDAObject or ch == _EMBEDDED_OBJECT:
			return
		nextHandler()

	# The document has the focus while it loads, before the buffer is ready.
	event_typedCharacter.ignoreIsReady = True

	def script_trapNonCommandGesture(self, gesture):
		tones.beep(500, 30)

	def _quickNavScript(self, gesture, itemType, direction, errorMessage, readUnit):
		if not self.complete:
			# Translators: Reported when a browse mode search finds nothing in the part of a page loaded
			# so far.
			errorMessage = _("Loading")
		return super()._quickNavScript(gesture, itemType, direction, errorMessage, readUnit)

	def _iterNodesByType(self, nodeType, direction="next", pos=None):
		items = super()._iterNodesByType(nodeType, direction, pos)
		if nodeType != "table" or config.conf["documentFormatting"]["includeLayoutTables"]:
			return items
		return (item for item in items if not self._isLayoutTable(item))

	def _isLayoutTable(self, item) -> bool:
		docHandle, ID = item.vbufFieldIdentifier
		facts = self._tableFacts(docHandle, ID)
		return bool(facts and facts[2])

	def _searchableAttribsForNodeType(self, nodeType):
		if nodeType.startswith("heading") and nodeType[7:].isdigit():
			return {"role": _role(Role.HEADING), "level": [nodeType[7:]]}
		simple = {
			"heading": _role(Role.HEADING),
			"list": _role(Role.LIST),
			"listItem": _role(Role.LISTITEM),
			"separator": _role(Role.SEPARATOR),
			"radioButton": _role(Role.RADIOBUTTON),
			"comboBox": _role(Role.COMBOBOX),
			"button": _role(Role.BUTTON, Role.TOGGLEBUTTON, Role.MENUBUTTON),
			"frame": _role(Role.INTERNALFRAME),
			"tab": _role(Role.TAB),
			"progressBar": _role(Role.PROGRESSBAR),
			"toggleButton": _role(Role.TOGGLEBUTTON),
			"menuItem": _role(Role.MENUITEM, Role.CHECKMENUITEM, Role.RADIOMENUITEM, Role.MENUBUTTON),
			"math": _role(Role.MATH),
		}
		if nodeType in simple:
			return {"role": simple[nodeType]}
		if nodeType == "link":
			return {"role": _role(Role.LINK), _state(State.LINKED): ["1"]}
		if nodeType == "visitedLink":
			return {"role": _role(Role.LINK), _state(State.VISITED): ["1"]}
		if nodeType == "unvisitedLink":
			return {"role": _role(Role.LINK), _state(State.LINKED): ["1"], _state(State.VISITED): [None]}
		if nodeType == "table":
			attrs = {"role": _role(Role.TABLE)}
			if not config.conf["documentFormatting"]["includeLayoutTables"]:
				attrs["table-layout"] = [None]
			return attrs
		if nodeType == "graphic":
			return [{"role": _role(Role.GRAPHIC)}, {"tag": ["img"]}]
		if nodeType == "blockQuote":
			return [{"role": _role(Role.BLOCKQUOTE)}, {"tag": ["blockquote"]}]
		if nodeType == "checkBox":
			return [
				{"role": _role(Role.CHECKBOX, Role.SWITCH)},
				{"xml-roles": [VBufStorage_findMatch_word("switch")]},
			]
		if nodeType == "slider":
			return [{"role": _role(Role.SLIDER)}, {"xml-roles": [VBufStorage_findMatch_word("slider")]}]
		if nodeType == "formField":
			return [
				{"role": _role(*_FORM_FIELD_ROLES), _state(State.READONLY): [None]},
				{"atspi::role": ["list box"], _state(State.READONLY): [None]},
				{"role": _role(Role.EDITABLETEXT, Role.COMBOBOX), _state(State.EDITABLE): ["1"]},
				{_state(State.EDITABLE): ["1"], f"parent::{_state(State.EDITABLE)}": [None]},
			]
		if nodeType == "edit":
			return [
				{"role": _role(Role.EDITABLETEXT), _state(State.EDITABLE): ["1"]},
				{_state(State.EDITABLE): ["1"], f"parent::{_state(State.EDITABLE)}": [None]},
			]
		if nodeType == "focusable":
			return {_state(State.FOCUSABLE): ["1"]}
		if nodeType == "landmark":
			return [
				{"role": _role(Role.LANDMARK)},
				{"xml-roles": [VBufStorage_findMatch_word(r) for r in aria.landmarkRoles]},
				{
					"xml-roles": [VBufStorage_findMatch_word("region")],
					"name": [VBufStorage_findMatch_notEmpty],
				},
			]
		if nodeType == "article":
			return {"xml-roles": [VBufStorage_findMatch_word("article")]}
		if nodeType == "grouping":
			return [
				{
					"xml-roles": [VBufStorage_findMatch_word(r) for r in ("group", "radiogroup")],
					"name": [VBufStorage_findMatch_notEmpty],
				},
				{"tag": ["fieldset"], "name": [VBufStorage_findMatch_notEmpty]},
			]
		if nodeType == "embeddedObject":
			return [
				{"tag": ["embed", "object", "applet", "audio", "video"]},
				{"role": _role(Role.EMBEDDEDOBJECT, *_APPLICATION_ROLES)},
			]
		if nodeType == "figure":
			return [{"xml-roles": [VBufStorage_findMatch_word("figure")]}, {"tag": ["figure"]}]
		return None

	def event_stateChange(self, obj, nextHandler):
		if not self.isAlive:
			return treeInterceptorHandler.killTreeInterceptor(self)
		states = obj.states
		isRoot = obj == self.rootNVDAObject
		if isRoot:
			self._busy = State.BUSY in states
		# The page and the link followed go away: while the page is left, Firefox takes from what it
		# holds the states that make it available, and an object that is gone has none of them. Firefox
		# on Windows tells nothing of either.
		if State.DEFUNCT in states or (self._busy and not isRoot and State.UNAVAILABLE in states):
			return
		return nextHandler()


def treeInterceptorClassFor(obj) -> type | None:
	"""The buffer class for a document object, or None: Gecko's web documents that are not editable,
	as mozilla.py chooses on Windows, while both the storage and the renderer are there; and a dialog
	on a page read in browse mode, which the page's buffer leaves out as an application, as
	behaviors.WebDialog gives it a buffer of its own on Windows."""
	if not (vbufLib.isAvailable() and vbufEngine.isAvailable()):
		return None
	try:
		if (obj.node.busName, obj.node.path) in _unrenderable:
			return None
		if obj.atspiRole == "document web" and "editable" not in obj.atspiStates:
			return AtSpiBuffer
		if obj.atspiRole == "dialog":
			parentBuffer = getattr(obj.parent, "treeInterceptor", None)
			if isinstance(parentBuffer, AtSpiBuffer):
				return AtSpiBuffer
	except NodeGone:
		pass
	return None
