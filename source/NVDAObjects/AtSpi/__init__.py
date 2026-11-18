"""NVDAObjects for AT-SPI accessibles: the Linux counterpart of NVDAObjects.IAccessible and NVDAObjects.UIA.

Every fact comes from linuxPlatform.atspi's client through a Node; nothing here calls libatspi. The
tree is flattened as Windows' is: each application's top-level windows are children of the desktop,
and the per-process application object is never part of NVDA's hierarchy.

No property here reads more than a bounded number of objects: a GetChildren on a big list froze Caja
for 4 s, and counting a Qt list's 2,003 items by building their objects froze NVDA for 17 s. So
positions come from the TableCell and Table interfaces or the posinset and setsize attributes,
children stop at CHILD_LIMIT and never enter an object that manages its descendants, and siblings are
counted by their roles only, and only in small containers.
"""

import os
import re
from typing import ClassVar

import api
from controlTypes import Role, State
from linuxPlatform.atspi import client as atspiClient
from linuxPlatform.atspi import handler
from linuxPlatform.atspi.client import Node, NodeGone
from linuxPlatform.atspi.roles import ROLES, mapStates
from locationHelper import RectLTWH

from NVDAObjects import NVDAObject
from NVDAObjects.AtSpi.deletion import DeletionEcho
from NVDAObjects.AtSpi.terminal import VteTerminal
from NVDAObjects.AtSpi.textInfo import AtspiTextInfo
from NVDAObjects.behaviors import (
	Dialog,
	EditableTextWithAutoSelectDetection,
	EditableTextWithoutAutoSelectDetection,
	Notification,
	ProgressBar,
	ToolTip,
)

#: The registry's path for the desktop, which every application root shares with it.
ROOT_PATH = "/org/a11y/atspi/accessible/root"
#: Children read from one object at most.
CHILD_LIMIT = 200
#: The most siblings an item is counted among when nothing gives its position directly.
SIBLING_LIMIT = 50
#: The same for a menu item: a GTK 3 menu built from actions keeps every item it may show, and Caja's
#: have more than fifty, most of them hidden.
MENU_SIBLING_LIMIT = 120
#: Columns read at most to speak a row of a table as a list item with columns.
ROW_COLUMN_LIMIT = 12
#: Objects visited at most, and how deep, to name an object from the text inside it.
DESCENDANT_TEXT_LIMIT = 20
DESCENDANT_TEXT_DEPTH = 4
#: Objects visited at most to read a desktop notification's text.
NOTIFICATION_TEXT_LIMIT = 40
#: Objects visited at most to find a status bar in a window that has no Collection interface.
STATUS_BAR_SEARCH_LIMIT = 400

#: Toolkit names as applications give them: GTK 3 (and wx, which is GTK 3 underneath), GTK 4, Qt.
GTK3 = "gtk"
GTK4 = "GTK"
QT = "Qt"
GECKO = "Gecko"

#: Roles whose number comes from the Value interface.
VALUE_ROLES = frozenset({Role.SLIDER, Role.SPINBUTTON, Role.SCROLLBAR, Role.PROGRESSBAR, Role.DIAL})
#: Roles whose items NVDA counts as "n of m" among their siblings.
COUNTED_ROLES = frozenset(
	{
		Role.LISTITEM,
		Role.TREEVIEWITEM,
		Role.MENUITEM,
		Role.CHECKMENUITEM,
		Role.RADIOMENUITEM,
		Role.TAB,
	},
)
MENU_ROLES = frozenset({Role.MENUITEM, Role.CHECKMENUITEM, Role.RADIOMENUITEM, Role.MENUBAR, Role.POPUPMENU})
#: Raw roles counted together as one kind of sibling.
_SIBLING_KINDS = (
	frozenset({"menu item", "check menu item", "radio menu item", "menu"}),
	frozenset({"page tab"}),
	frozenset({"radio button"}),
	frozenset({"list item"}),
	frozenset({"tree item"}),
)
#: Roles that GTK 4, and Caja's list view, leave unnamed with their text in a child label.
DESCENDANT_NAMED_ROLES = frozenset(
	{
		Role.LISTITEM,
		Role.MENUITEM,
		Role.CHECKMENUITEM,
		Role.RADIOMENUITEM,
		Role.TABLECELL,
		Role.TABLEROW,
		Role.TREEVIEWITEM,
		Role.BUTTON,
		Role.TOGGLEBUTTON,
		Role.TAB,
	},
)
#: Roles whose descendants are not read when an object is named from the text inside it.
_OPAQUE_RAW_ROLES = frozenset({"list", "list box", "table", "tree", "tree table", "menu", "popup menu"})
#: How the names GTK's own dialogs give their layout boxes end.
_LAYOUT_NAME_SUFFIXES = (" Revealer", " Stack", " Layer")
_keyName = re.compile(r"<(\w+)>")
_MODIFIERS = {"control": "Ctrl", "primary": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Windows"}
_QT_MODIFIERS = {"Ctrl": "Ctrl", "Alt": "Alt", "Shift": "Shift", "Meta": "Windows"}


def _client() -> atspiClient.Client:
	"""The client every fact comes through."""
	return atspiClient.client


def desktopNode() -> Node:
	return handler.desktopNode()


def isApplication(node: Node) -> bool:
	return node.path == ROOT_PATH and node != desktopNode()


def applications() -> list[Node]:
	"""Every application on the desktop that answers."""
	c = _client()
	try:
		apps = c.applications()
	except NodeGone:
		return []
	return [app for app in apps if not c.isHung(app.busName)]


def toolkitOf(node: Node) -> str:
	try:
		return _client().toolkit(node)
	except NodeGone:
		return ""


def accessibleIdOf(node: Node) -> str:
	try:
		return _client().accessibleId(node)
	except NodeGone:
		return ""


def childNodes(node: Node, states: frozenset[str] | None = None, limit: int = CHILD_LIMIT) -> list[Node]:
	"""node's children, the first limit at most, and none when node manages its descendants: such a
	container lists only what it shows, or, in Qt, every row, and one request for all of them froze
	Caja for 4 s."""
	c = _client()
	if states is None:
		states = c.states(node)
	if "manages-descendants" in states:
		return []
	count = c.childCount(node)
	if count <= limit:
		return c.children(node)
	return [child for child in (c.childAt(node, i) for i in range(limit)) if child]


def _isSingleColumnTable(node: Node) -> bool:
	c = _client()
	if "Table" not in c.interfaces(node):
		return False
	_rows, columns = c.tableSize(node)
	return columns == 1


def _descendantText(node: Node) -> str:
	"""The names of the first named objects inside node, in tree order, as GTK 4 keeps a list item's
	or a button's text in a label inside it."""
	c = _client()
	texts: list[str] = []
	visited = 0

	def visit(parent: Node, depth: int) -> None:
		nonlocal visited
		for child in childNodes(parent, limit=DESCENDANT_TEXT_LIMIT):
			if visited >= DESCENDANT_TEXT_LIMIT:
				return
			visited += 1
			name = c.name(child)
			if name:
				# Caja's side pane repeats a place's name in two cells of its row.
				if not texts or texts[-1] != name:
					texts.append(name)
			elif depth < DESCENDANT_TEXT_DEPTH and c.role(child) not in _OPAQUE_RAW_ROLES:
				visit(child, depth + 1)

	try:
		visit(node, 1)
	except NodeGone:
		pass
	return " ".join(texts)


def _formatAccelerator(gtkAccel: str) -> str:
	"""GTK's "<Control><Shift>o" as NVDA spells a shortcut, "Ctrl+Shift+O"."""
	mods = [_MODIFIERS.get(m.lower(), m) for m in _keyName.findall(gtkAccel)]
	key = _keyName.sub("", gtkAccel)
	if len(key) == 1:
		key = key.upper()
	return "+".join([*mods, key]) if key else ""


def _formatKeySequence(sequence: str) -> str:
	"""A key sequence as Qt and the keyshortcuts attribute spell it, "Alt+n", as NVDA does, "Alt+N"."""
	*mods, key = sequence.split("+") if sequence != "+" else ["+"]
	mods = [_QT_MODIFIERS.get(m.capitalize(), m.capitalize()) for m in mods]
	if len(key) == 1:
		key = key.upper()
	return "+".join([*mods, key])


class AtSpi(NVDAObject):
	"""An accessible reached through AT-SPI."""

	node: Node

	@classmethod
	def kwargsFromSuper(cls, kwargs: dict, relation=None) -> bool:
		node = handler.nodeForRelation(relation)
		if node is None:
			return False
		kwargs["node"] = node
		return True

	def __init__(self, node: Node) -> None:
		self.node = node
		super().__init__()
		handler.registerObject(self)

	#: Whether the page this was on has gone, and its buffer with it.
	wentWithItsPage = False

	def _isEqual(self, other) -> bool:
		return self.node.key == other.node.key

	def event_stateChange(self):
		# As the page's buffer does while it is there: Firefox on Windows tells nothing of the states
		# an object loses as its page is left.
		if self.wentWithItsPage:
			return
		super().event_stateChange()

	def findOverlayClasses(self, clsList) -> None:
		role = self.role
		states = self.atspiStates
		ifaces = self.atspiInterfaces
		if role == Role.TOGGLEBUTTON and self._comboBoxAbove is not None:
			clsList.append(ComboBoxButton)
		if role == Role.SPINBUTTON and "Text" in ifaces and "Value" in ifaces:
			clsList.append(SpinButton)
		elif "Text" in ifaces and ("editable" in states or role == Role.EDITABLETEXT):
			if self._isRichTextEditor:
				clsList.append(RichTextEditor)
			clsList.append(DeletionEcho)
			clsList.append(EditableTextWithAutoSelectDetection)
		elif role == Role.TERMINAL and "Text" in ifaces:
			clsList.append(VteTerminal)
		if role in (Role.LISTITEM, Role.TREEVIEWITEM) and self.atspiRole == "table cell":
			clsList.append(TableRowItem)
		elif role == Role.LISTITEM and self.atspiRole == "icon":
			clsList.append(IconItem)
		elif role == Role.TABLECELL and "TableCell" in ifaces:
			clsList.append(TableCell)
		if role == Role.DIALOG and self.atspiRole != "file chooser":
			# A file chooser's labels are its controls' names, not a message to read as it opens.
			clsList.append(Dialog)
		elif role == Role.PROGRESSBAR:
			clsList.append(ProgressBar)
		elif role == Role.TOOLTIP:
			clsList.append(ToolTip)
		elif role == Role.ALERT and self.atspiParentRole == "application":
			clsList.append(DesktopNotification)
		elif role == Role.ALERT:
			clsList.append(Notification)
		if self.atspiParentRole == "application":
			clsList.append(TopLevelWindow)
		clsList.append(AtSpi)
		super().findOverlayClasses(clsList)

	def _get__isRichTextEditor(self) -> bool:
		"""Whether this is an editable element of a page in Firefox, which keeps its text in the
		paragraphs inside it, as NVDA's Editor is on Windows; a field of a form keeps its own."""
		states = self.atspiStates
		return (
			self.toolkit == GECKO
			and self.role == Role.EDITABLETEXT
			and "focusable" in states
			and "multi-line" in states
			and "Hypertext" in self.atspiInterfaces
			and self.atspiAttributes.get("tag") != "textarea"
		)

	def event_caret(self):
		# Firefox moves the caret of the paragraph inside an editor, which alone reports it; the
		# editor, which has the focus, takes it as its own, as NVDA's IAccessible does on Windows.
		focus = api.getFocusObject()
		if self is not focus and isinstance(focus, RichTextEditor) and self.isDescendantOf(focus):
			focus.event_caret()
			return
		super().event_caret()

	# Raw AT-SPI facts, each cached for one core cycle like every NVDAObject property.

	atspiRole: str

	def _get_atspiRole(self) -> str:
		try:
			return _client().role(self.node)
		except NodeGone:
			return handler.roleOfVanished(self.node) or "invalid"

	atspiStates: frozenset[str]

	def _get_atspiStates(self) -> frozenset[str]:
		try:
			return _client().states(self.node)
		except NodeGone:
			return frozenset({"defunct"})

	def _get_atspiAttributes(self) -> dict[str, str]:
		try:
			return _client().attributes(self.node)
		except NodeGone:
			return {}

	atspiInterfaces: frozenset[str]

	def _get_atspiInterfaces(self) -> frozenset[str]:
		try:
			return _client().interfaces(self.node)
		except NodeGone:
			return frozenset()

	def _get_atspiName(self) -> str:
		try:
			name = _client().name(self.node)
		except NodeGone:
			return handler.knownName(self.node) or ""
		if name.endswith(_LAYOUT_NAME_SUFFIXES) and self.atspiRole in ("panel", "filler"):
			# GTK's file chooser names its boxes after their place in its own layout, "Browse Header
			# Stack" and the like, which a user has no use for.
			return ""
		return name

	def _get_atspiParentNode(self) -> Node | None:
		try:
			return _client().parent(self.node)
		except NodeGone:
			return None

	def _get_atspiParentRole(self) -> str | None:
		parent = self.atspiParentNode
		if parent is None:
			return None
		try:
			return _client().role(parent)
		except NodeGone:
			return None

	def _get_atspiRelations(self) -> dict[str, list[Node]]:
		relations: dict[str, list[Node]] = {}
		try:
			# Qt gives one relation per target, so a type can come more than once.
			for relation in _client().relations(self.node):
				relations.setdefault(relation.type, []).extend(relation.targets)
		except NodeGone:
			pass
		return relations

	toolkit: str

	def _get_toolkit(self) -> str:
		"""The toolkit's name as the application gives it: "gtk" for GTK 3 and wx, "GTK" for GTK 4,
		"Qt"."""
		return toolkitOf(self.node)

	def _get_accessibleId(self) -> str:
		return accessibleIdOf(self.node)

	def _get_processID(self) -> int:
		return handler.processIDOf(self.node)

	# What speech, braille and navigation read.

	def _get_role(self) -> Role:
		raw = self.atspiRole
		if raw == "text":
			# GTK 3 uses one role for entries, text views and labels in some places. A text the keyboard
			# reaches but cannot change is a read-only field, as Windows shows a read-only edit control:
			# a document reader's text, a chat history, a log.
			states = self.atspiStates
			if "editable" in states:
				return Role.EDITABLETEXT
			# NVDA's own app module presents its single-line read-only fields, which wx makes multi-line
			# text views, itself.
			if "focusable" in states and "Text" in self.atspiInterfaces and self.processID != os.getpid():
				return Role.EDITABLETEXT
			return Role.STATICTEXT
		if raw == "password text":
			# Windows presents a password field as an edit field with the protected state.
			return Role.EDITABLETEXT
		if raw == "label":
			# A page's label element is IAccessible2's label, which NVDA leaves out of the focus
			# ancestry: a radio button inside one would be announced after its own name.
			if self.toolkit == GECKO:
				return Role.LABEL
			return Role.STATICTEXT
		if raw == "filler" or (raw == "viewport" and not self.atspiName):
			# GTK's boxes and the viewport that scrolls a box: layout, and a container
			# Dialog.getDialogText descends into.
			return Role.PANEL
		if raw == "panel":
			toolkit = self.toolkit
			if toolkit == GTK4 and self.accessibleId == "GtkStatusbar":
				return Role.STATUSBAR
			# A GTK 3 frame and a Qt group box are panels named by their label: Windows' group box.
			if self.atspiName and toolkit in (GTK3, QT):
				return Role.GROUPING
			# Gecko gives a fieldset and an ARIA group this role, which IAccessible2 calls a grouping.
			if toolkit == GECKO:
				if self.atspiAttributes.get("xml-roles", "").split(" ")[0] == "figure":
					return Role.FIGURE
				return Role.GROUPING
			return Role.PANEL
		if raw == "menu":
			# Java's model, inherited by GTK: a menu is the item that opens a submenu.
			parentRole = self.atspiParentRole
			if parentRole in ("menu bar", "menu") or "selectable" in self.atspiStates:
				return Role.MENUITEM
			return Role.POPUPMENU
		if (
			raw in ("table", "tree table")
			and self.toolkit == GTK3
			and "manages-descendants" in self.atspiStates
		):
			# A GTK 3 tree view is what GTK programs use for an ordinary list, and with columns for what
			# Windows shows as a list view's details, Caja's list view among them; only a one-column
			# tree is a tree view.
			if raw == "tree table" and self._isSingleColumnTable:
				return Role.TREEVIEW
			return Role.LIST
		if raw == "table cell":
			rowTable = self._rowTable
			if rowTable is None:
				return Role.TABLECELL
			_node, tableRole, columns = rowTable
			if tableRole == "tree" or (tableRole == "tree table" and columns == 1):
				return Role.TREEVIEWITEM
			return Role.LISTITEM
		if raw == "layered pane" and "Selection" in self.atspiInterfaces:
			# Caja's icon view, of a folder and of the desktop: Windows' list view in icon mode.
			return Role.LIST
		if raw == "icon" and self.atspiParentRole == "layered pane":
			return Role.LISTITEM
		if (
			raw == "check box"
			and self.toolkit == QT
			and self.accessibleId.endswith(("QPushButton", "QToolButton"))
		):
			# Qt reports a checkable push or tool button as a check box.
			return Role.TOGGLEBUTTON
		if raw == "frame" and self.atspiAttributes.get("window-type") == "dialog":
			# wx's dialogs are GTK windows marked only by this attribute.
			return Role.DIALOG
		if raw == "file chooser" and self.atspiParentRole == "application":
			# GTK's open and save dialogs, which Windows users know as the Open and Save As dialogs.
			return Role.DIALOG
		if raw == "alert" and self.atspiParentRole == "application":
			# GTK's message dialog is a top-level alert; Windows presents a message box as a dialog. A
			# desktop notification is one whose window GTK knows as a notification's.
			if self.atspiAttributes.get("window-type") == "notification":
				return Role.ALERT
			return Role.DIALOG
		if raw == "invalid":
			return Role.UNKNOWN
		if raw == "button" and "Hyperlink" in self.atspiInterfaces and self.toolkit in (GTK3, GTK4):
			# GTK's link button; a browser gives every object in a page's text the interface.
			return Role.LINK
		return ROLES.get(raw, Role.UNKNOWN)

	def _get__comboBoxAbove(self) -> Node | None:
		"""The combo box within two levels above: GTK 3 puts its button inside a filler inside it."""
		node = self.atspiParentNode
		for _ in range(2):
			if node is None:
				return None
			try:
				if _client().role(node) == "combo box":
					return node
				node = _client().parent(node)
			except NodeGone:
				return None
		return None

	def _get__isPopupShowing(self) -> bool:
		try:
			popup = _client().childAt(self.node, 0)
			return popup is not None and "showing" in _client().states(popup)
		except NodeGone:
			return False

	def _get__isSingleColumnTable(self) -> bool:
		try:
			return _isSingleColumnTable(self.node)
		except NodeGone:
			return False

	def _get__rowTable(self) -> tuple[Node, str, int] | None:
		"""For a cell of a GTK 3 tree view or a Qt tree, which take the focus for a whole row: the view, its
		raw role, and how many columns it has."""
		toolkit = self.toolkit
		if toolkit not in (GTK3, QT):
			return None
		parent = self.atspiParentNode
		if parent is None:
			return None
		c = _client()
		try:
			tableRole = c.role(parent)
			# A Qt table is a grid whose cells are spoken one at a time, as NVDA speaks a table's.
			rowRoles = ("table", "tree table") if toolkit == GTK3 else ("tree",)
			if tableRole not in rowRoles or "manages-descendants" not in c.states(parent):
				return None
			if "Table" not in c.interfaces(parent):
				return None
			_rows, columns = c.tableSize(parent)
		except NodeGone:
			return None
		return (parent, tableRole, columns)

	def _get_roleText(self) -> str | None:
		if self.role == Role.UNKNOWN and self.atspiRole not in ("unknown", "invalid"):
			return self.atspiRole
		return super().roleText

	def _get__labelText(self) -> str:
		"""The names of the objects this one is labelled by."""
		c = _client()
		labels = []
		groups = []
		for node in self.atspiRelations.get("labelled-by", []):
			try:
				name = c.name(node)
				if name:
					# Qt labels each control in a group box by the box too, which NVDA already says as
					# the focus's ancestor.
					(groups if c.role(node) in ("panel", "filler", "grouping") else labels).append(name)
			except NodeGone:
				continue
		return " ".join(labels or groups)

	def _get__comboBoxNamedByChoice(self) -> bool:
		"""GTK 3 and Qt name a combo box by its current choice, which NVDA calls its value; GTK 4 names
		it by its label and gives the choice in valuetext. One the user types into, such as Firefox's
		address bar, holds its choice in its text, which NVDA reads, and its name is its own."""
		if "editable" in self.atspiStates and "Text" in self.atspiInterfaces:
			return False
		return "valuetext" not in self.atspiAttributes

	def _get_name(self) -> str:
		role = self.role
		name = self.atspiName
		if role == Role.COMBOBOX and self._comboBoxNamedByChoice:
			return self._labelText
		if name:
			if role == Role.TABCONTROL and self.toolkit == QT and name == self._selectedTabName:
				# Qt names a tab bar after its current tab, which follows it as the focus.
				return ""
			if role == Role.STATICTEXT and "focusable" in self.atspiStates and self.toolkit == GTK3:
				# A value the keyboard reaches is read with its title, as Windows reads a read-only field.
				title = self._labelText or self._gridTitle
				if title and title != name:
					return f"{title} {name}"
			return name
		labelled = self._labelText
		if labelled:
			return labelled
		if role == Role.TAB and self.toolkit == GTK3:
			# A GTK 3 tab is named by its label; one without a name is a page of a notebook whose tabs
			# are hidden, and what is inside it is the page, not its name.
			return name
		if role in DESCENDANT_NAMED_ROLES or (role == Role.STATUSBAR and self.toolkit == GTK4):
			return _descendantText(self.node)
		return name

	def _get_isPresentableFocusAncestor(self) -> bool:
		if self.atspiRole == "file chooser" and self.role != Role.DIALOG:
			# The chooser inside GTK's open and save dialogs, named "File Chooser Widget".
			return False
		if self.toolkit == GTK3 and self.atspiRole in ("page tab", "page tab list"):
			# A GTK notebook holds its pages inside its tabs, where a Windows tab control sits beside the
			# page, so every move into a page would announce the notebook: Caja and Pluma keep one around
			# their views even with the tabs hidden. Only a tab of several, which says which document
			# or page the focus is in, is announced, and the tab list only when its tab has the focus.
			if self.atspiRole == "page tab list":
				focus = api.getFocusObject()
				return isinstance(focus, AtSpi) and focus.atspiParentNode == self.node
			if not self.atspiName or self._tabCount <= 1:
				return False
		return super().isPresentableFocusAncestor

	def _get__tabCount(self) -> int:
		parent = self.atspiParentNode
		try:
			return _client().childCount(parent) if parent is not None else 0
		except NodeGone:
			return 0

	def _get__gridTitle(self) -> str:
		"""For a value a GTK 3 dialog shows as a label the keyboard can reach and relates to no title, the
		title on its left that ends with a colon."""
		c = _client()
		parent = self.atspiParentNode
		index = self.indexInParent
		if parent is None or index is None:
			return ""
		try:
			# GtkGrid lists its children last added first, so a title added before its value comes after
			# it.
			sibling = c.childAt(parent, index + 1)
			if sibling is not None and c.role(sibling) == "label":
				text = c.name(sibling)
				if text.endswith(":"):
					return text
		except NodeGone:
			pass
		return ""

	def _get__selectedTabName(self) -> str | None:
		c = _client()
		try:
			for child in childNodes(self.node, self.atspiStates, SIBLING_LIMIT):
				if c.role(child) == "page tab" and "selected" in c.states(child):
					return c.name(child)
		except NodeGone:
			pass
		return None

	def _get_value(self) -> str | None:
		role = self.role
		if role == Role.COMBOBOX:
			if self._comboBoxNamedByChoice:
				return self.atspiName or None
			return self.atspiAttributes.get("valuetext") or None
		if role in VALUE_ROLES and "Value" in self.atspiInterfaces:
			try:
				current, minimum, maximum, text = _client().value(self.node)
			except NodeGone:
				return None
			if text:
				return text
			if role == Role.PROGRESSBAR and maximum > minimum:
				return f"{round((current - minimum) * 100 / (maximum - minimum))}%"
			return str(int(current)) if current == int(current) else f"{current:g}"
		return None

	def _get_placeholder(self) -> str | None:
		"""The hint an empty field shows, which NVDA says on focus in place of its text: Firefox gives a
		page's placeholder as an object attribute, as IAccessible2 gives it on Windows."""
		# Speech asks every focus for it; a busy GTK application is not asked once more.
		if self.toolkit != GECKO:
			return None
		return self.atspiAttributes.get("placeholder-text") or None

	def _get_errorMessage(self) -> str | None:
		"""The text of what the page names as the error message of a field, as IA2's error relation gives
		it on Windows."""
		c = _client()
		for node in self.atspiRelations.get("error-message", []):
			try:
				text = c.text(node) if "Text" in c.interfaces(node) else ""
				text = " ".join(text.replace("\ufffc", " ").split()) or c.name(node)
			except NodeGone:
				continue
			if text:
				return text
		return super().errorMessage

	def _get_description(self) -> str | None:
		try:
			description = _client().description(self.node)
		except NodeGone:
			return None
		if not description or description == self.atspiName:
			return None
		if self.role in VALUE_ROLES and "Value" in self.atspiInterfaces:
			# GTK 3 describes a scale by the value it draws.
			try:
				current = _client().value(self.node)[0]
			except NodeGone:
				current = None
			if current is not None and description.strip() in (
				str(current),
				f"{current:g}",
				str(int(current)),
			):
				return None
		return description

	def _get_states(self) -> set[State]:
		raw = self.atspiStates
		role = self.role
		states = mapStates(raw, role, self.atspiAttributes)
		if self.atspiRole == "password text":
			states.add(State.PROTECTED)
		if role == Role.EDITABLETEXT and self.atspiRole == "text" and "editable" not in raw:
			# GTK says nothing of a text view that cannot be changed beyond leaving editable out.
			states.add(State.READONLY)
		if self.atspiRole == "menu" and role == Role.MENUITEM:
			states.add(State.HASPOPUP)
		if role in MENU_ROLES:
			# GTK marks the highlighted menu item selected; Windows menus have no such state.
			states.discard(State.SELECTED)
		if role == Role.STATICTEXT:
			# Qt marks every label read-only, which Windows never says of static text.
			states.discard(State.READONLY)
		if State.MULTILINE in states and not (
			State.EDITABLE in states or role in (Role.EDITABLETEXT, Role.DOCUMENT, Role.TERMINAL)
		):
			# GTK puts multi-line on every label and notebook tab; only fields have it on Windows.
			states.discard(State.MULTILINE)
		if role == Role.COMBOBOX and not {State.EXPANDED, State.COLLAPSED} & states:
			# A GTK 3 combo box has no expanded state; its popup's showing state stands in for it.
			states.add(State.EXPANDED if self._isPopupShowing else State.COLLAPSED)
		if role == Role.LINK:
			states.add(State.LINKED)
		return states

	def _get_keyboardShortcut(self) -> str | None:
		shortcuts = self.atspiAttributes.get("keyshortcuts")
		if shortcuts:
			# GTK 4 and Gecko give the shortcut as ARIA does, "Alt+n", and several apart by spaces.
			return _formatKeySequence(shortcuts.split()[0])
		if "Action" not in self.atspiInterfaces:
			return None
		try:
			actions = _client().actions(self.node)
		except NodeGone:
			return None
		if not actions:
			return None
		if self.toolkit == QT:
			return self._qtShortcut([binding for _name, binding in actions])
		return self._gtkShortcut(actions[0][1] or "")

	def _gtkShortcut(self, binding: str) -> str | None:
		"""GTK's binding, "mnemonic;path;accelerator", as Windows reports shortcuts."""
		parts = binding.split(";")
		mnemonic = parts[0] if parts else ""
		accelerator = parts[2] if len(parts) > 2 else ""
		mnemonicKey = _formatAccelerator(mnemonic) if mnemonic else ""
		if self.role in MENU_ROLES:
			# On Windows a menu item's mnemonic is the bare letter; its accelerator comes first.
			letter = mnemonicKey.split("+")[-1] if mnemonicKey else ""
			if letter and self.atspiParentRole == "menu bar":
				# A menu bar item opens with Alt and its letter, a menu item with its letter alone.
				letter = f"Alt+{letter}"
			return "  ".join(k for k in (_formatAccelerator(accelerator), letter) if k) or None
		if mnemonicKey and "+" not in mnemonicKey:
			mnemonicKey = f"Alt+{mnemonicKey}"
		return mnemonicKey or _formatAccelerator(accelerator) or None

	def _qtShortcut(self, bindings: list[str]) -> str | None:
		"""Qt's binding, a key sequence such as "Alt+P", "Ctrl+O" or "Down", as Windows reports
		shortcuts."""
		binding = next((b for b in bindings if b), "")
		if "+" not in binding.strip("+"):
			# A key the control handles once focused, such as Down on a combo box, which opens it: not
			# a way to reach it.
			return None
		shortcut = _formatKeySequence(binding)
		mods, _, key = shortcut.rpartition("+")
		if self.role in MENU_ROLES and mods == "Alt" and len(key) == 1:
			# Qt gives a menu item's mnemonic as Alt and its letter; inside a menu it is the bare letter.
			return shortcut if self.atspiParentRole == "menu bar" else key
		return shortcut

	def _get_location(self) -> RectLTWH | None:
		if "Component" not in self.atspiInterfaces:
			return None
		try:
			extents = _client().extents(self.node)
		except NodeGone:
			return None
		return RectLTWH(*extents) if extents else None

	def _get_parent(self):
		parent = self.atspiParentNode
		if parent is None or parent == desktopNode() or isApplication(parent):
			return api.getDesktopObject()
		return handler.getObject(parent)

	def _get_childCount(self) -> int:
		try:
			return _client().childCount(self.node)
		except NodeGone:
			return 0

	def getChild(self, index: int):
		try:
			child = _client().childAt(self.node, index)
		except NodeGone:
			return None
		return handler.getObject(child) if child else None

	def _get_children(self) -> list:
		try:
			nodes = childNodes(self.node, self.atspiStates)
		except NodeGone:
			return []
		return [handler.getObject(n) for n in nodes]

	def _get_firstChild(self):
		return self.getChild(0) if self.childCount > 0 else None

	def _get_lastChild(self):
		count = self.childCount
		return self.getChild(count - 1) if count > 0 else None

	def _get_simpleFirstChild(self):
		if "manages-descendants" in self.atspiStates and _isReadingObjects():
			# Say all from NVDA+b would read every row of a long list, one object at a time.
			return None
		return super().simpleFirstChild

	def _get_indexInParent(self) -> int | None:
		try:
			index = _client().indexInParent(self.node)
		except NodeGone:
			return None
		return index if index >= 0 else None

	def _sibling(self, step: int):
		index = self.indexInParent
		if index is None:
			return None
		parentNode = self.atspiParentNode
		if parentNode is None:
			return None
		try:
			sibling = _client().childAt(parentNode, index + step) if index + step >= 0 else None
		except NodeGone:
			return None
		return handler.getObject(sibling) if sibling else None

	def _get_next(self):
		return self._sibling(1)

	def _get_previous(self):
		return self._sibling(-1)

	def _get_positionInfo(self) -> dict[str, int]:
		info = {}
		attrs = self.atspiAttributes
		if "level" in attrs and attrs["level"].isdigit():
			info["level"] = int(attrs["level"])
		if attrs.get("posinset", "").isdigit() and attrs.get("setsize", "").isdigit():
			info["indexInGroup"] = int(attrs["posinset"])
			info["similarItemsInGroup"] = int(attrs["setsize"])
			return info
		role = self.role
		try:
			if role == Role.LISTITEM and "TableCell" in self.atspiInterfaces:
				# Qt's list items are rows of a table.
				position = self._tableCellPosition()
				if position:
					info.update(position)
					return info
			if role in (Role.RADIOBUTTON, Role.RADIOMENUITEM):
				members = self.atspiRelations.get("member-of", [])
				if 1 < len(members) <= SIBLING_LIMIT:
					ordered = _inTreeOrder(members)
					if self.node in ordered:
						info["indexInGroup"] = ordered.index(self.node) + 1
						info["similarItemsInGroup"] = len(ordered)
						return info
			if role in COUNTED_ROLES or role == Role.RADIOBUTTON:
				info.update(self._positionAmongSiblings())
		except NodeGone:
			pass
		return info

	def _tableCellPosition(self) -> dict[str, int]:
		c = _client()
		row, _column = c.cellPosition(self.node)
		table = c.cellTable(self.node)
		rows, _columns = c.tableSize(table) if table else (0, 0)
		if row < 0 or rows <= 0:
			return {}
		return {"indexInGroup": row + 1, "similarItemsInGroup": rows}

	def _positionAmongSiblings(self) -> dict[str, int]:
		"""The item's place among the visible siblings of its kind, counted by their roles alone, and
		only in a container small enough to read: Qt gives radio buttons no group, and GTK 3 gives
		menu items and tabs no position."""
		kind = next((k for k in _SIBLING_KINDS if self.atspiRole in k), None)
		parent = self.atspiParentNode
		if kind is None or parent is None:
			return {}
		limit = MENU_SIBLING_LIMIT if kind is _SIBLING_KINDS[0] else SIBLING_LIMIT
		c = _client()
		parentStates = c.states(parent)
		if "manages-descendants" in parentStates or c.childCount(parent) > limit:
			return {}
		index = 0
		position = 0
		for sibling in childNodes(parent, parentStates, limit):
			if c.role(sibling) not in kind or "visible" not in c.states(sibling):
				continue
			index += 1
			if sibling == self.node:
				position = index
		if not position:
			return {}
		return {"indexInGroup": position, "similarItemsInGroup": index}

	def _get_labeledBy(self):
		targets = self.atspiRelations.get("labelled-by", [])
		return handler.getObject(targets[0]) if targets else None

	def _get_controllerFor(self) -> list:
		return [handler.getObject(n) for n in self.atspiRelations.get("controller-for", [])]

	def _get_flowsTo(self):
		targets = self.atspiRelations.get("flows-to", [])
		return handler.getObject(targets[0]) if targets else None

	def _get_flowsFrom(self):
		targets = self.atspiRelations.get("flows-from", [])
		return handler.getObject(targets[0]) if targets else None

	def isDescendantOf(self, obj) -> bool:
		node = self.atspiParentNode
		for _ in range(100):
			if node is None:
				return False
			if isinstance(obj, AtSpi) and node.key == obj.node.key:
				return True
			try:
				node = _client().parent(node)
			except NodeGone:
				return False
		return False

	def _get_isInForeground(self) -> bool:
		foreground = api.getForegroundObject()
		return isinstance(foreground, AtSpi) and foreground.node.busName == self.node.busName

	def setFocus(self) -> None:
		try:
			_client().grabFocus(self.node)
		except NodeGone:
			pass

	def scrollIntoView(self) -> None:
		# Browse mode scrolls on every caret move; an object without Component simply stays put.
		if "Component" not in self.atspiInterfaces:
			return
		from linuxPlatform.atspi import accessibleFor

		try:
			accessibleFor.scrollTo(self.node)
		except NodeGone:
			pass

	def _get_actionCount(self) -> int:
		if "Action" not in self.atspiInterfaces:
			return 0
		try:
			return len(_client().actions(self.node))
		except NodeGone:
			return 0

	def getActionName(self, index=None) -> str:
		index = index or self.defaultActionIndex
		try:
			return _client().actions(self.node)[index][0]
		except (NodeGone, IndexError):
			raise NotImplementedError from None

	def doAction(self, index=None) -> None:
		index = index or self.defaultActionIndex
		try:
			if not _client().doAction(self.node, index):
				raise NotImplementedError
		except NodeGone:
			raise NotImplementedError from None

	def _get_selectionContainer(self):
		parent = self.atspiParentNode
		try:
			if parent is not None and "Selection" in _client().interfaces(parent):
				return handler.getObject(parent)
		except NodeGone:
			pass
		return None

	def getSelectedItemsCount(self, maxCount=2) -> int:
		if "Selection" not in self.atspiInterfaces:
			return 0
		try:
			return _client().selectedCount(self.node)
		except NodeGone:
			return 0

	def _get_TextInfo(self):
		# Whatever its role, an object that is edited has its caret in its text: a page's text area
		# made a combobox, or an editable element without a role.
		if "Text" in self.atspiInterfaces and (
			"editable" in self.atspiStates
			or self.role
			in (
				Role.EDITABLETEXT,
				Role.STATICTEXT,
				Role.TERMINAL,
				Role.DOCUMENT,
				Role.SPINBUTTON,
				Role.PASSWORDEDIT,
			)
		):
			return AtspiTextInfo
		return super().TextInfo

	def _get_treeInterceptorClass(self):
		if self.role == Role.DOCUMENT:
			from virtualBuffers.atspi import treeInterceptorClassFor

			cls = treeInterceptorClassFor(self)
			if cls is not None:
				return cls
		return super().treeInterceptorClass

	def findStatusBar(self) -> "AtSpi | None":
		"""The first status bar inside this object, usually a top-level window, found by its role."""
		c = _client()
		try:
			if "Collection" in self.atspiInterfaces:
				found = c.firstMatchWithRole(self.node, "status bar")
				return handler.getObject(found) if found else None
			found = self._walkForStatusBar()
		except NodeGone:
			return None
		return handler.getObject(found) if found else None

	def _walkForStatusBar(self) -> Node | None:
		"""A breadth-first search, for GTK 4, which has no Collection interface and makes its status bar
		a panel known only by its accessible ID."""
		c = _client()
		gtk4 = self.toolkit == GTK4
		queue = [self.node]
		visited = 0
		while queue and visited < STATUS_BAR_SEARCH_LIMIT:
			node = queue.pop(0)
			for child in childNodes(node):
				visited += 1
				role = c.role(child)
				if role == "status bar" or (
					gtk4 and role == "panel" and accessibleIdOf(child) == "GtkStatusbar"
				):
					return child
				if role not in _OPAQUE_RAW_ROLES:
					queue.append(child)
		return None

	def _get_devInfo(self) -> list[str]:
		info = super().devInfo
		info.append(f"AT-SPI node: {self.node.busName} {self.node.path}")
		info.append(f"AT-SPI role: {self.atspiRole}")
		info.append(f"AT-SPI states: {', '.join(sorted(self.atspiStates))}")
		info.append(f"AT-SPI interfaces: {', '.join(sorted(self.atspiInterfaces))}")
		info.append(f"AT-SPI attributes: {self.atspiAttributes}")
		info.append(f"toolkit: {self.toolkit}")
		return info


def _inTreeOrder(nodes: list[Node]) -> list[Node]:
	"""nodes in the order a user meets them; GTK lists a radio group's members last first."""
	c = _client()
	parents = {c.parent(n) for n in nodes}
	if len(parents) == 1:
		return sorted(nodes, key=c.indexInParent)
	return sorted(nodes, key=lambda n: (c.extents(n) or (0, 0, 0, 0))[1::-1])


def _isReadingObjects() -> bool:
	from speech import sayAll

	return isinstance(sayAll.SayAllHandler._getActiveSayAll(), sayAll._ObjectsReader)


def _topLevelNodes() -> list[Node]:
	"""Every application's top-level windows, in the order of the applications on the desktop."""
	result = []
	for app in applications():
		try:
			result.extend(childNodes(app))
		except NodeGone:
			continue
	return result


class RichTextEditor(AtSpi):
	"""An editable element of a page, its paragraphs' text read as one."""

	def _get_TextInfo(self):
		from NVDAObjects.AtSpi.compoundText import AtspiCompoundTextInfo

		return AtspiCompoundTextInfo


class TopLevelWindow(AtSpi):
	"""A window whose AT-SPI parent is its application, presented as a child of the desktop."""

	def _sibling(self, step: int):
		siblings = _topLevelNodes()
		if self.node not in siblings:
			return None
		target = siblings.index(self.node) + step
		return handler.getObject(siblings[target]) if 0 <= target < len(siblings) else None

	def _get_indexInParent(self) -> int | None:
		siblings = _topLevelNodes()
		return siblings.index(self.node) if self.node in siblings else None


class DesktopNotification(Notification, AtSpi):
	"""A notification from the desktop's notification daemon, a window of its own that never takes the
	focus: spoken as NVDA speaks a Windows toast, its text alone, once."""

	#: The notification spoken last, and its text, which a daemon announces both as it creates the
	#: window and as it shows it.
	_lastSpoken: tuple[tuple[str, str], str] | None = None

	def _get_notificationText(self) -> str:
		c = _client()
		texts: list[str] = []
		stack = [self.node]
		visited = 0
		try:
			while stack and visited < NOTIFICATION_TEXT_LIMIT:
				node = stack.pop()
				visited += 1
				stack.extend(reversed(childNodes(node, limit=NOTIFICATION_TEXT_LIMIT)))
				if (
					node != self.node
					and c.role(node) in ("label", "text", "static")
					and (name := c.name(node))
				):
					texts.append(name)
		except NodeGone:
			pass
		return " ".join(texts)

	def event_alert(self) -> None:
		import config
		import ui

		if not config.conf["presentation"]["reportHelpBalloons"]:
			return
		text = self.notificationText
		spoken = (self.node.key, text)
		if not text or DesktopNotification._lastSpoken == spoken:
			return
		DesktopNotification._lastSpoken = spoken
		ui.message(text)

	event_show = event_alert


class ComboBoxButton(AtSpi):
	"""The button inside a GTK 3 combo box, which takes the keyboard focus for it."""

	def _get_focusRedirect(self):
		combo = self._comboBoxAbove
		return handler.getObject(combo) if combo is not None else None


class SpinButton(EditableTextWithoutAutoSelectDetection, AtSpi):
	"""A GTK spin button: an edit field whose number is its value, spoken as a value on Windows."""

	def _get_value(self) -> str | None:
		try:
			current, _minimum, _maximum, text = _client().value(self.node)
		except NodeGone:
			return None
		return text or (str(int(current)) if current == int(current) else f"{current:g}")

	def _get__hasNavigableText(self) -> bool:
		# Said as its value on focus, as Windows' up-down controls are, not as the text of a field.
		return False

	def script_caret_moveByLine(self, gesture) -> None:
		# Up, Down and the page keys change the number, which its value change then speaks once; read
		# as a line too, it would be spoken twice.
		gesture.send()

	def _get_states(self):
		states = super().states
		states.discard(State.EDITABLE)
		return states


class TableRowItem(AtSpi):
	"""A cell of a GTK 3 tree view, which takes the focus for its row: spoken as the item of a Windows
	list view is, with the text of each column after the first behind its header, or as a tree view's
	item where the tree view has a single column."""

	def _get_name(self) -> str:
		rowTable = self._rowTable
		try:
			if rowTable is None or rowTable[2] <= 1:
				return self._cellText(self.node)
			table, _tableRole, columns = rowTable
			row, column = _client().cellPosition(self.node)
			return self._rowText(table, row, column, min(columns, ROW_COLUMN_LIMIT))
		except NodeGone:
			return ""

	def _cellText(self, cell: Node) -> str:
		c = _client()
		name = c.name(cell)
		if name:
			return name
		# Caja puts its file name and icon in two cells inside the first column's.
		return _descendantText(cell) if c.childCount(cell) else ""

	def _rowText(self, table: Node, row: int, focusedColumn: int, columns: int) -> str:
		"""The text of each of the row's columns, those after the first behind their headers.

		Every column is read from the table, the focused cell's own included: GTK 3 focuses the cell of
		whichever column the cursor is in, and Caja the inner cell of its name column. The focused cell's
		text stands in for its column's only when the table gives nothing there, and is added where no
		column already holds it.
		"""
		import config
		from config.configFlags import ReportTableHeaders

		c = _client()
		withHeaders = config.conf["documentFormatting"]["reportTableHeaders"] in (
			ReportTableHeaders.ROWS_AND_COLUMNS,
			ReportTableHeaders.COLUMNS,
		)
		ownText = self._cellText(self.node)
		cellTexts: list[str] = []
		for column in range(columns):
			cell = c.cellAt(table, row, column)
			text = self._cellText(cell) if cell is not None else ""
			if not text and column == focusedColumn:
				text = ownText
			cellTexts.append(text)
		if ownText and not any(ownText in text for text in cellTexts):
			cellTexts[min(max(focusedColumn, 0), len(cellTexts) - 1)] = ownText
		texts = []
		for column, text in enumerate(cellTexts):
			if not text:
				continue
			# The first column with text names the row, as a Windows list view's first column does; a
			# column of check boxes before it has none.
			header = c.columnHeader(table, column) if withHeaders and texts else ""
			texts.append(f"{header}: {text}" if header else text)
		return "; ".join(texts)

	def _get_positionInfo(self) -> dict[str, int]:
		rowTable = self._rowTable
		if rowTable is not None and rowTable[1] == "tree":
			# A Qt tree numbers its rows through every open branch, which is no item's place among its
			# siblings, and gives no level.
			return {}
		try:
			return self._tableCellPosition()
		except NodeGone:
			return {}

	def _get_states(self) -> set[State]:
		states = super().states
		# GTK 3 says nothing of a row being selectable; it is, and NVDA leaves the selection of a
		# selectable item unsaid.
		states.add(State.SELECTABLE)
		return states

	#: Milliseconds after an arrow key that a Qt tree's item is read again for a branch it opened or
	#: closed.
	EXPANSION_CHECK_MS = 150

	def script_expandOrCollapse(self, gesture) -> None:
		gesture.send()
		if self.toolkit != QT:
			return
		# Qt opens and closes a tree's branches without saying so, where GTK and Windows send a state
		# change, and replaces the item's accessible as it does; the item is read again and the change
		# spoken.
		import core

		core.callLater(self.EXPANSION_CHECK_MS, self._reportExpansion)

	__gestures: ClassVar[dict[str, str]] = {
		"kb:rightArrow": "expandOrCollapse",
		"kb:leftArrow": "expandOrCollapse",
	}

	#: Whether the item was open or closed when it was last spoken; the key reaches Qt as soon as NVDA,
	#: so the state as the script runs may already be the new one.
	spokenExpansion: set[State] | None = None

	def _get__expansion(self) -> set[State]:
		return self.states & {State.EXPANDED, State.COLLAPSED}

	def event_gainFocus(self) -> None:
		self.spokenExpansion = self._expansion
		super().event_gainFocus()

	def _reportExpansion(self) -> None:
		if api.getFocusObject() is not self:
			return
		import eventHandler

		node = handler.nodeForRelation("focus")
		if node is not None and node != self.node:
			# The item Qt made in place of this one has the focus without an event.
			eventHandler.queueEvent("gainFocus", handler.eventObject(node))
			return
		expansion = self._expansion
		if self.spokenExpansion is not None and expansion != self.spokenExpansion:
			import speech
			from controlTypes import OutputReason

			speech.speakObjectProperties(self, states=True, reason=OutputReason.CHANGE)
		self.spokenExpansion = expansion


class IconItem(AtSpi):
	"""A file in Caja's icon view."""

	def _get_positionInfo(self) -> dict[str, int]:
		parent = self.atspiParentNode
		index = self.indexInParent
		if parent is None or index is None:
			return {}
		c = _client()
		try:
			count = c.childCount(parent)
			# The field a file was renamed in stays behind as the view's last child.
			last = c.childAt(parent, count - 1) if count else None
			if last is not None and c.role(last) != "icon":
				count -= 1
		except NodeGone:
			return {}
		return {"indexInGroup": index + 1, "similarItemsInGroup": count}

	def _get_states(self) -> set[State]:
		states = super().states
		# Caja's icons carry neither sensitive nor enabled, and are usable; nor do they say when they are
		# selected, so whether they are is left unsaid.
		states.discard(State.UNAVAILABLE)
		states.discard(State.SELECTABLE)
		states.discard(State.SELECTED)
		return states


class TableCell(AtSpi):
	def _get_table(self):
		try:
			table = _client().cellTable(self.node)
		except NodeGone:
			return None
		return handler.getObject(table) if table else None

	def _get_tableID(self):
		table = self.table
		return table.node.key if table else None

	# Speech and braille take NotImplementedError from a table property as "not known" and present
	# the rest; a cell or table that is gone, or a busy application, must say the same rather than
	# leave the focus unannounced.

	def _cellPosition(self) -> tuple[int, int]:
		try:
			return _client().cellPosition(self.node)
		except NodeGone:
			raise NotImplementedError from None

	def _tableSize(self) -> tuple[int, int]:
		table = self.table
		if table is None:
			raise NotImplementedError
		try:
			return _client().tableSize(table.node)
		except NodeGone:
			raise NotImplementedError from None

	def _get_rowNumber(self) -> int:
		return self._cellPosition()[0] + 1

	def _get_columnNumber(self) -> int:
		return self._cellPosition()[1] + 1

	def _get_rowCount(self) -> int:
		return self._tableSize()[0]

	def _get_columnCount(self) -> int:
		return self._tableSize()[1]

	def _get_columnHeaderText(self) -> str:
		try:
			return " ".join(h for h in _client().columnHeaderNames(self.node) if h)
		except NodeGone:
			raise NotImplementedError from None


class Desktop(AtSpi):
	"""The registry's desktop, whose children are every application's top-level windows."""

	def __init__(self, node: Node | None = None):
		super().__init__(node=node or desktopNode())

	def _isEqual(self, other) -> bool:
		return True

	processID = None
	role = Role.DESKTOPPANE
	isPresentableFocusAncestor = False

	def _get_name(self) -> str:
		# Translators: the desktop as NVDA names it on Windows.
		return _("Desktop")

	def _get_parent(self):
		return None

	def _get_next(self):
		return None

	def _get_previous(self):
		return None

	def _get_indexInParent(self):
		return None

	def _get_children(self) -> list:
		return [handler.getObject(w) for w in _topLevelNodes()]

	def _get_childCount(self) -> int:
		return len(_topLevelNodes())

	def getChild(self, index: int):
		nodes = _topLevelNodes()
		return handler.getObject(nodes[index]) if 0 <= index < len(nodes) else None

	def _get_states(self) -> set[State]:
		return set()

	def _get_location(self):
		try:
			extents = _client().extents(self.node)
		except NodeGone:
			extents = None
		if extents:
			return RectLTWH(*extents)
		import wx

		width, height = wx.GetDisplaySize()
		return RectLTWH(0, 0, width, height)
