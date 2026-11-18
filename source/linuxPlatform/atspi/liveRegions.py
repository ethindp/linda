"""Live regions and alerts on web pages, spoken as NVDA's in-process hook speaks them for Gecko on
Windows (nvdaHelper/remote/ia2LiveRegions.cpp and textFromIAccessible.cpp).

A browser says nothing on AT-SPI that a region is live beyond its object attributes: an addition is
object:text-changed:insert on the region's container, an embedded-object character standing for each
new child, and a text change is the same event carrying the new text. Each one from a page's live
region is read here while the event is handled: the region's politeness, busy, relevant and atomic
come from the container-* attributes the browser puts on every object inside a region. An element
with the alert role announces itself with a showing state change, which is how Gecko maps its alert
event to ATK.
"""

import collections
import time

from logHandler import log

from . import client as _clientModule
from .client import AppHung, Node, NodeGone

#: The toolkits of browsers whose pages carry live regions in container-* object attributes.
WEB_TOOLKITS = frozenset({"gecko", "chromium"})
#: Politeness values that make a region live.
LIVE_VALUES = frozenset({"polite", "assertive", "rude"})
#: What an embedded object stands as in its parent's text.
EMBEDDED = "￼"
#: Objects visited at most to gather what one change says, so a huge insertion cannot hold NVDA.
TEXT_NODE_LIMIT = 300
#: Characters spoken at most for one change.
TEXT_LIMIT = 4000
#: Parents remembered, by key; a page's objects keep their parent for as long as they live.
PARENT_CACHE_LIMIT = 5000
#: Ancestors walked at most from an event's source to its window.
DEPTH_LIMIT = 64

_parents: collections.OrderedDict[tuple[str, str], Node | None] = collections.OrderedDict()
#: Whether each ancestor met is a document, by key; a page's objects keep their role.
_isDocument: collections.OrderedDict[tuple[str, str], bool] = collections.OrderedDict()
#: Seconds within which an alert announced again is the same showing: the browser can say it twice.
ALERT_REPEAT_SECONDS = 1.0
#: The alert spoken last, by key, and when.
_lastAlert: tuple[tuple[str, str], float] | None = None


def _client():
	return _clientModule.client


def _parent(node: Node) -> Node | None:
	key = node.key
	if key in _parents:
		return _parents[key]
	parent = _client().parent(node)
	_parents[key] = parent
	if len(_parents) > PARENT_CACHE_LIMIT:
		_parents.popitem(last=False)
	return parent


def _isDocumentNode(node: Node) -> bool:
	key = node.key
	if key not in _isDocument:
		_isDocument[key] = _client().role(node) == "document web"
		if len(_isDocument) > PARENT_CACHE_LIMIT:
			_isDocument.popitem(last=False)
	return _isDocument[key]


def _ancestors(node: Node) -> list[Node]:
	"""node's ancestors, nearest first, up to and including its application."""
	result = []
	current = node
	for _ in range(DEPTH_LIMIT):
		parent = _parent(current)
		if parent is None:
			break
		result.append(parent)
		if parent.path == _clientModule.ROOT_PATH:
			break
		current = parent
	return result


def _isInForegroundPage(node: Node) -> bool:
	"""Whether node is on the page shown in the foreground window: a page in a background tab is not
	showing, and neither is a window behind the one in front."""
	from . import handler

	c = _client()
	ancestors = _ancestors(node)
	if len(ancestors) < 2 or ancestors[-1].path != _clientModule.ROOT_PATH:
		return False
	topLevel = ancestors[-2]
	if topLevel != handler._foreground or not handler._foregroundActive:
		return False
	documents = [a for a in ancestors if _isDocumentNode(a)]
	if not documents:
		return False
	return "showing" in c.states(documents[-1], fresh=True)


def _isWeb(node: Node) -> bool:
	return _client().toolkit(node).lower() in WEB_TOOLKITS


def _isOff(node: Node) -> bool:
	return _client().attributes(node).get("live") == "off"


def _linkedObject(node: Node, offset: int) -> Node | None:
	"""The embedded object at offset in node's text."""
	from gi.repository import Atspi

	c = _client()
	index = _clientModule.call(node, Atspi.Hypertext.get_link_index, offset)
	if index is None or index < 0:
		return None
	link = _clientModule.call(node, Atspi.Hypertext.get_link, index)
	if link is None:
		return None
	return c.node(_clientModule.call(node, lambda: link.get_object(0)))


class _Gatherer:
	"""Text for speech from a part of a page, as getTextFromIAccessible gathers it."""

	def __init__(self) -> None:
		self.parts: list[str] = []
		self.visited = 0

	def text(self) -> str:
		return " ".join("".join(self.parts).split())[:TEXT_LIMIT]

	def _nameAndDescription(self, node: Node) -> bool:
		c = _client()
		gotText = False
		for value in (c.name(node), c.description(node)):
			if value and value.replace(EMBEDDED, "").strip():
				self.parts.append(value + " ")
				gotText = True
		return gotText

	def gather(
		self,
		node: Node,
		text: str | None = None,
		start: int = 0,
		recurse: bool = True,
		includeTopLevelText: bool = True,
	) -> bool:
		"""Add node's text, or text, a part of it from start, expanding each embedded object into the
		text of that object; when node has no text, its name and description.

		:return: Whether anything but white space was added.
		"""
		self.visited += 1
		if self.visited > TEXT_NODE_LIMIT:
			return False
		c = _client()
		useNewText = text is not None
		gotText = False
		if text is None and "Text" in c.interfaces(node):
			text = c.text(node)
		if text is not None:
			for index, character in enumerate(text):
				if character == EMBEDDED and recurse:
					if self.visited > TEXT_NODE_LIMIT:
						# gather would return at once, after this loop had asked for the object and its
						# attributes; past the limit, the rest of the embedded objects are left out.
						continue
					child = _linkedObject(node, start + index)
					if child is not None:
						if not _isOff(child):
							gotText |= self.gather(child)
						continue
				if includeTopLevelText:
					self.parts.append(character)
					if character != EMBEDDED and not character.isspace():
						gotText = True
			self.parts.append(" ")
		elif recurse:
			for child in c.children(node, limit=TEXT_NODE_LIMIT):
				if not _isOff(child):
					gotText |= self.gather(child)
		if not gotText and not useNewText:
			gotText = self._nameAndDescription(node)
		return gotText


def _atomicContainer(node: Node, attributes: dict[str, str]) -> Node | None:
	"""The region or part of one marked atomic that node is in, as findAriaAtomic finds it."""
	current, currentAttributes = node, attributes
	for _ in range(DEPTH_LIMIT):
		if currentAttributes.get("atomic") == "true":
			return current
		if currentAttributes.get("container-atomic") != "true":
			return None
		parent = _parent(current)
		if parent is None:
			return None
		current, currentAttributes = parent, _client().attributes(parent)
	return None


def _report(text: str, politeness: str) -> None:
	"""Speak and braille a live region's change, as nvdaControllerInternal_reportLiveRegion does."""
	import api
	import braille
	import config
	import speech
	from speech.priorities import Spri

	if not config.conf["presentation"]["reportDynamicContentChanges"]:
		return
	focus = api.getFocusObject()
	if focus.sleepMode == focus.SLEEP_FULL:
		return
	priority = Spri.NEXT if politeness in ("assertive", "rude") else Spri.NORMAL
	speech.speakText(text, priority=priority)
	braille.handler.message(text)


def _textInserted(event) -> None:
	_eventType, source, offset, _length, inserted = event
	if not isinstance(inserted, str) or not inserted:
		return
	attributes = _client().attributes(source)
	politeness = attributes.get("container-live")
	if politeness not in LIVE_VALUES:
		return
	if attributes.get("container-busy") == "true":
		return
	if "editable" in _client().states(source):
		# Typing into a field an author marked live would otherwise be read back as it is typed.
		return
	relevant = attributes.get("container-relevant")
	if relevant is None or relevant == "all":
		allowAdditions = allowText = True
	else:
		allowAdditions = "additions" in relevant
		allowText = "text" in relevant
	if not (allowAdditions or allowText):
		return
	if not _isInForegroundPage(source):
		return
	gatherer = _Gatherer()
	atomic = _atomicContainer(source, attributes)
	if atomic is not None:
		gotText = gatherer.gather(atomic)
	else:
		gotText = gatherer.gather(
			source,
			text=inserted,
			start=offset,
			recurse=allowAdditions,
			includeTopLevelText=allowText,
		)
	text = gatherer.text()
	if gotText and text:
		log.debug(f"Live region, {politeness}: {text!r}")
		_report(text, politeness)


def _isRepeatedAlert(source: Node) -> bool:
	return (
		_lastAlert is not None
		and _lastAlert[0] == source.key
		and time.monotonic() - _lastAlert[1] < ALERT_REPEAT_SECONDS
	)


def _alertShown(source: Node) -> None:
	"""Speak an alert on a page as NVDA's IAccessible.event_alert speaks a Gecko alert, which is a
	dialog whose text is its description: its role and name, then its text, at once."""
	global _lastAlert
	import api
	import controlTypes
	import speech
	from speech.priorities import Spri

	if _isRepeatedAlert(source) or not _isInForegroundPage(source):
		return
	focus = api.getFocusObject()
	if focus.sleepMode == focus.SLEEP_FULL:
		return
	for obj in (focus, *api.getFocusAncestors()):
		if getattr(obj, "node", None) == source:
			return
	c = _client()
	name = c.name(source)
	gatherer = _Gatherer()
	gatherer.gather(source)
	text = gatherer.text()
	if not text and not name:
		return
	_lastAlert = (source.key, time.monotonic())
	sequence = speech.getPropertiesSpeech(
		reason=controlTypes.OutputReason.FOCUS,
		name=name,
		role=controlTypes.Role.ALERT,
	)
	if text and text != name:
		sequence.append(text)
	log.debug(f"Alert on a page: {sequence!r}")
	speech.speak(sequence, priority=Spri.NOW)


def handle(event) -> bool:
	"""Speak what a raw AT-SPI event from a web page says of a live region or an alert.

	:return: True when the event was an alert on a page and has been dealt with here, so that it is
		not presented again as an object's alert.
	"""
	eventType, source, detail1, _detail2, _anyData = event
	isInsert = eventType.startswith("object:text-changed:insert")
	isShown = eventType == "object:state-changed:showing" and detail1
	if not (isInsert or isShown):
		return False
	try:
		if not _isWeb(source):
			return False
		if isInsert:
			_textInserted(event)
			return False
		if _client().role(source) != "alert":
			return False
		_alertShown(source)
		return True
	except AppHung:
		return False
	except NodeGone:
		return bool(isShown) and _isRepeatedAlert(source)


def forget() -> None:
	global _lastAlert
	_parents.clear()
	_isDocument.clear()
	_lastAlert = None
