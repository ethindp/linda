"""Text of AT-SPI accessibles, through their Text interface.

Offsets are the toolkit's own. Where it counts code points, as GTK does, encoding is None and offsets
are str indices; where it counts UTF-16 units, as Qt does, encoding is NVDA's UTF-16 one and
OffsetsTextInfo converts. Characters and words come from NVDA's own segmentation over the line, so
that they are the same in every toolkit; lines come from the toolkit, paragraphs from the newlines in
the text, and sentences from ICU over the paragraph.
"""

import textInfos
import textUtils
from colors import RGB
from linuxPlatform.atspi.client import NodeGone
from linuxPlatform.atspi.textSource import TextSource, textGeneration, toolkitOf, unitFor
from locationHelper import RectLTWH
from logHandler import log
from textInfos.offsets import OffsetsTextInfo, findEndOfLine, findStartOfLine

#: What ends a line when the toolkit leaves it out of the line it gives.
LINE_SEPARATORS = ("\n", "\r", " ", " ")
#: What Qt wraps a line at and leaves out of both lines.
WRAP_SEPARATORS = (" ", "\t")
#: The character GTK and Qt show for each character of a password.
MASK_CHARACTER = "●"
#: What NVDA says for a character of a protected field, as speech.PROTECTED_CHAR.
PROTECTED_CHARACTER = "*"
#: Above this many characters a paragraph is the toolkit's line rather than read from the whole text.
PARAGRAPH_FROM_TEXT_LIMIT = 50000
#: Default text attributes kept per accessible, by node key; cleared when it grows past this.
DEFAULT_ATTRIBUTES_KEPT = 200

_defaultLanguages: dict[tuple[str, str], str | None] = {}


def _isBold(weight: str) -> bool:
	return (weight.isdigit() and int(weight) >= 600) or weight.lower() in ("bold", "heavy", "ultrabold")


def _colour(value: str) -> RGB:
	if "," in value and not value.startswith("rgb"):
		channels = [int(c) for c in value.split(",")[:3]]
		# GTK gives 16 bits a channel ("65535,0,0"), Qt 8 ("255,0,0").
		shift = 8 if max(channels) > 255 else 0
		red, green, blue = (c >> shift for c in channels)
		return RGB(red, green, blue)
	return RGB.fromString(value)


def formatField(attributes: dict[str, str], defaultLanguage: str | None = None) -> textInfos.FormatField:
	"""NVDA's format field for AT-SPI text attributes, in GTK's Pango names, Qt's or Gecko's."""
	field = textInfos.FormatField()
	family = attributes.get("family-name") or attributes.get("font-family")
	if family:
		# Qt quotes it. Giving font-family as well made NVDA say the name twice.
		field["font-name"] = family.strip("'\"")
	size = attributes.get("size") or attributes.get("font-size")
	if size:
		if size.isdigit() and int(size) >= 1024:
			# GTK gives a tag's size in Pango units, 1024 a point, and the default size in points.
			size = str(round(int(size) / 1024))
		field["font-size"] = size if size.endswith("pt") else f"{size}pt"
	weight = attributes.get("weight") or attributes.get("font-weight")
	field["bold"] = bool(weight) and _isBold(weight)
	style = attributes.get("style") or attributes.get("font-style")
	field["italic"] = bool(style) and style.lower() in ("italic", "oblique")
	underline = (
		attributes.get("underline")
		or attributes.get("text-underline-type")
		or attributes.get("text-underline-style")
	)
	field["underline"] = bool(underline) and underline not in ("none", "false")
	strike = attributes.get("strikethrough") or attributes.get("text-line-through-type")
	field["strikethrough"] = bool(strike) and strike not in ("none", "false")
	for key, target in (
		("fg-color", "color"),
		("color", "color"),
		("bg-color", "background-color"),
		("background-color", "background-color"),
	):
		value = attributes.get(key)
		if not value or isinstance(field.get(target), RGB):
			continue
		try:
			field[target] = _colour(value)
		except (ValueError, TypeError):
			log.debugWarning(f"Text attribute {key} has a colour NVDA cannot read: {value!r}")
	invalid = attributes.get("invalid")
	if invalid in ("spelling", "true"):
		field["invalid-spelling"] = True
	elif invalid == "grammar":
		field["invalid-grammar"] = True
	language = attributes.get("language")
	# GTK gives the locale's language to every run, "c" under the C locale; only a language that
	# differs from the default was given by the document.
	if language and language != defaultLanguage and language.lower() not in ("c", "posix"):
		field["language"] = language.replace("-", "_")
	return field


#: The key of an object's whole text among its cached properties.
_STORY_TEXT_KEY = object()


class AtspiTextInfo(OffsetsTextInfo):
	"""Text through the AT-SPI Text interface of obj, which has node, toolkit and isProtected.

	Caches have names without a leading underscore, as OffsetsTextInfo copies every attribute whose
	name has one into each copy, and a copy kept as the review position would carry a line or a
	length from before the text changed.
	"""

	detectFormattingAfterCursorMaybeSlow = False

	def _get_textSource(self) -> TextSource:
		source = self.__dict__.get("textSourceCache")
		if source is None:
			obj = self.obj
			toolkit = getattr(obj, "toolkit", None) or toolkitOf(obj.node)
			source = self.textSourceCache = TextSource(obj.node, unitFor(toolkit))
		return source

	def _get_encoding(self) -> str | None:
		return textUtils.WCHAR_ENCODING if self.textSource.unit == "utf16" else None

	def _shown(self, text: str) -> str:
		"""text as NVDA presents it: a password's masking characters become NVDA's own."""
		if MASK_CHARACTER in text and self.obj.isProtected:
			return text.replace(MASK_CHARACTER, PROTECTED_CHARACTER)
		return text

	def _getStoryLength(self) -> int:
		try:
			return self.textSource.characterCount()
		except NodeGone:
			return 0

	def _storyLengthCached(self) -> int:
		length = self.__dict__.get("storyLengthCache")
		if length is None:
			length = self.storyLengthCache = self._getStoryLength()
		return length

	def _getStoryText(self) -> str:
		# Moving or expanding by sentence or paragraph reads the whole text each time, and say all does
		# so for every sentence. The text is kept with the object for the core cycle, as NVDA keeps its
		# properties, and dropped as soon as the application says it changed. The key is a fresh
		# object, so that nothing else's cached property can meet it.
		text = self.__dict__.get("storyTextCache")
		if text is not None:
			return text
		source = self.textSource
		cache = getattr(self.obj, "_propertyCache", None)
		key = (_STORY_TEXT_KEY, source.unit, textGeneration(source.node))
		if cache is not None:
			text = cache.get(key)
		if text is None:
			try:
				text = self._shown(source.text(0, -1))
			except NodeGone:
				return ""
			if cache is not None:
				for stale in [k for k in cache if isinstance(k, tuple) and k and k[0] is _STORY_TEXT_KEY]:
					del cache[stale]
				cache[key] = text
		self.storyTextCache = text
		return text

	def _getTextRange(self, start: int, end: int) -> str:
		if start >= end:
			return ""
		story = self.__dict__.get("storyTextCache")
		if story is not None:
			if self.encoding is None:
				return story[start:end]
			strStart, strEnd = textUtils.WideStringOffsetConverter(story).encodedToStrOffsets(start, end)
			return story[strStart:strEnd]
		line = self.__dict__.get("lineCache")
		if line and line[0] <= start and end <= line[1]:
			lineStart, _lineEnd, lineText = line
			if self.encoding is None:
				return lineText[start - lineStart : end - lineStart]
			converter = textUtils.WideStringOffsetConverter(lineText)
			strStart, strEnd = converter.encodedToStrOffsets(start - lineStart, end - lineStart)
			return lineText[strStart:strEnd]
		try:
			return self._shown(self.textSource.text(start, end))
		except NodeGone:
			return ""

	def _getCaretOffset(self) -> int:
		try:
			offset = self.textSource.caretOffset()
		except NodeGone as e:
			raise RuntimeError(f"no caret: {e}") from None
		if offset < 0:
			raise RuntimeError("no caret in this object")
		return offset

	def _setCaretOffset(self, offset: int) -> None:
		try:
			self.textSource.setCaretOffset(offset)
		except NodeGone:
			log.debugWarning("Could not move the caret", exc_info=True)

	def _getSelectionOffsets(self) -> tuple[int, int]:
		try:
			selections = self.textSource.selections()
		except NodeGone:
			selections = []
		if selections:
			start, end = selections[0]
			return (min(start, end), max(start, end))
		caret = self._getCaretOffset()
		return (caret, caret)

	def _setSelectionOffsets(self, start: int, end: int) -> None:
		try:
			self.textSource.setSelection(start, end)
		except NodeGone:
			log.debugWarning("Could not select text", exc_info=True)

	def _getLineOffsets(self, offset: int) -> tuple[int, int]:
		try:
			start, end, text = self.textSource.stringAtOffset(offset, "line")
		except NodeGone:
			return (offset, offset + 1)
		if (start == end == 0 and offset > 0) or start > offset or end < offset:
			# Past the end some toolkits answer an empty range at 0, and Qt -1.
			return (offset, offset + 1)
		storyLength = self._storyLengthCached()
		if end < storyLength and not text.endswith("\n"):
			# GTK 4 and Qt leave the newline out of the line, and give an empty line as an empty range,
			# so that the newline belongs to no line and the review cursor and say all stick on it. It
			# goes to the line before it, as GTK 3 and NVDA's own findEndOfLine give it. Qt also leaves
			# out the space or tab a line wraps at.
			separators = LINE_SEPARATORS + (WRAP_SEPARATORS if self.textSource.unit == "utf16" else ())
			try:
				following = self.textSource.text(end, end + 1)
			except NodeGone:
				following = ""
			if following in separators:
				end += 1
				text += following
		if not (start <= offset < end or offset == end == storyLength):
			return (offset, offset + 1)
		self.lineCache = (start, end, self._shown(text))
		return (start, end)

	def _getParagraphOffsets(self, offset: int) -> tuple[int, int]:
		# GTK 3 and Qt 5 have no paragraphs and GTK 4 and Qt 6 leave out the newline, while a visual
		# line cuts ICU's sentences, and so say all, at every wrap.
		storyLength = self._storyLengthCached()
		if not 0 < storyLength <= PARAGRAPH_FROM_TEXT_LIMIT:
			return self._getLineOffsets(offset)
		text = self._getStoryText()
		if not text:
			# Qt 5 gives a password field's length and no text.
			return self._getLineOffsets(offset)
		if self.encoding is None:
			return (findStartOfLine(text, offset), findEndOfLine(text, offset))
		converter = textUtils.WideStringOffsetConverter(text)
		strOffset = converter.encodedToStrOffsets(offset, offset)[0]
		return converter.strToEncodedOffsets(findStartOfLine(text, strOffset), findEndOfLine(text, strOffset))

	def _getBoundingRectFromOffset(self, offset: int) -> RectLTWH:
		try:
			x, y, width, height = self.textSource.characterExtents(offset)
		except NodeGone as e:
			raise LookupError(str(e)) from None
		if width <= 0 and height <= 0:
			raise LookupError(f"no extents at offset {offset}")
		return RectLTWH(x, y, width, height)

	def _getOffsetFromPoint(self, x: int, y: int) -> int:
		try:
			offset = self.textSource.offsetAtPoint(x, y)
		except NodeGone as e:
			raise LookupError(str(e)) from None
		if offset < 0:
			raise LookupError(f"no character at {x}, {y}")
		return offset

	def _defaultLanguage(self) -> str | None:
		key = self.obj.node.key
		if key not in _defaultLanguages:
			if len(_defaultLanguages) >= DEFAULT_ATTRIBUTES_KEPT:
				_defaultLanguages.clear()
			try:
				_defaultLanguages[key] = self.textSource.defaultAttributes().get("language")
			except NodeGone:
				return None
		return _defaultLanguages[key]

	def _getFormatFieldAndOffsets(self, offset, formatConfig, calculateOffsets=True):
		try:
			attributes, start, end = self.textSource.attributeRun(offset)
		except NodeGone:
			return (textInfos.FormatField(), (self._startOffset, self._endOffset))
		language = attributes.get("language")
		defaultLanguage = (
			self._defaultLanguage() if language and language.lower() not in ("c", "posix") else None
		)
		field = formatField(attributes, defaultLanguage)
		if not start <= offset < end:
			# Qt's line edit answers an empty run at every offset, and GTK 4 one for text with no tag.
			# Walking them an offset at a time costs a call a character and, over UTF-16, cuts surrogate
			# pairs in half, which loses the character. The rest of the line is taken as the run, which
			# misses formatting that starts within it on GTK 4.
			_lineStart, lineEnd = self._getLineOffsets(offset)
			start, end = offset, lineEnd if lineEnd > offset else self._storyLengthCached()
		return (field, (start, end))
