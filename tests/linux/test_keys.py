"""Keys read from the X display: NVDA commands reach NVDA alone, other keys reach the application and
NVDA both, and the key map names keys as NVDA on Windows does.

Every test that starts NVDA runs once with Insert as the NVDA key and the desktop layout, and once
with Caps Lock as the NVDA key and the laptop layout. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_keys.py
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession
from Xlib import XK, display
from Xlib.ext import xinput
from Xlib.protocol import rq

#: Expected key names, recorded from NVDA's own KeyboardInputGesture fed by the key map.
KEY_NAMES = harness.HERE / "fixtures" / "keyNames"
#: Seconds NVDA has to start and speak the focus.
STARTUP_TIMEOUT = 60.0
#: What the fixture's text field holds at the start, all of it selected once Tab reaches it.
ENTRY_TEXT = "Alice"


def configText(layout: str, nvdaKey: int) -> str:
	return "\n".join(
		(
			"schemaVersion = 25",
			"[general]",
			"\tlanguage = en",
			"\tplayStartAndExitSounds = False",
			"\tshowWelcomeDialogAtStartup = False",
			"[speech]",
			"\tsynth = transcript",
			"[braille]",
			"\tdisplay = noBraille",
			"[keyboard]",
			f"\tkeyboardLayout = {layout}",
			f"\tNVDAModifierKeys = {nvdaKey}",
			"[featureFlag]",
			"\tplayErrorSound = 2",
			"[addonStore]",
			"\tautomaticUpdates = disabled",
			"\tbaseServerURL = http://127.0.0.1:1",
			"",
		),
	)


#: (keyboard layout, NVDAModifierKeys, the NVDA key as xdotool names it).
LAYOUTS = {
	"desktop": ("desktop", 6, "Insert"),
	"laptop": ("laptop", 1, "Caps_Lock"),
}


class KeySession:
	"""The GTK fixture with its text field focused, and NVDA started beside it."""

	def __init__(self, desktop: DesktopSession, tmpPath: Path, layoutName: str, env: dict | None = None):
		self.desktop = desktop
		self.layout, nvdaKey, self.nvda = LAYOUTS[layoutName]
		config = tmpPath / "nvda.ini"
		config.write_text(configText(self.layout, nvdaKey))
		desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
		desktop.startNVDA(config, env=env)
		desktop.expect("Fixture window", since=0, timeout=STARTUP_TIMEOUT)
		mark = desktop.mark()
		desktop.press("Tab", "Tab", "Tab", "Tab")
		desktop.expect("Name: edit\N{HORIZONTAL ELLIPSIS}", since=mark)
		assert entryState() == (ENTRY_TEXT, len(ENTRY_TEXT))

	def chord(self, key: str) -> str:
		return f"{self.nvda}+{key}"

	def quit(self) -> harness.Launch:
		launch = self.desktop.nvda
		launch.process.terminate()
		launch.waitForExit(20)
		return launch


def entryState() -> tuple[str, int]:
	"""The text of the fixture's text field and where its caret is, read through AT-SPI."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		for index in range(app.get_child_count() if app else 0):
			window = app.get_child_at_index(index)
			if window is not None and window.get_name() == "Fixture window":
				entry = _findEntry(window, Atspi)
				assert entry is not None, "the fixture has no text field"
				return Atspi.Text.get_text(entry, 0, -1), Atspi.Text.get_caret_offset(entry)
	raise AssertionError("no fixture window")


def _findEntry(node, Atspi):
	if node.get_role_name() == "text" and node.get_state_set().contains(Atspi.StateType.EDITABLE):
		return node
	for index in range(node.get_child_count()):
		child = node.get_child_at_index(index)
		if child is not None and (found := _findEntry(child, Atspi)) is not None:
			return found
	return None


def transcript(launch: harness.Launch) -> list[dict]:
	path = launch.directory / "transcript.jsonl"
	return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_nvdaChordReachesNVDAAlone(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KeySession(desktop, tmp_path, layoutName)
	mark = desktop.mark()
	desktop.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT
	# A letter typed after the chord reaches the field as typed: with Caps Lock as the NVDA key, the
	# lock the X server toggled is back as it was.
	desktop.press("a")
	time.sleep(0.3)
	assert entryState()[0] == "a"
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	log = launch.text("nvda.log")
	assert f"Input: kb({keys.layout}):NVDA+t" in log
	assert f"Input: kb({keys.layout}):(typing key)" in log
	assert f"Input: kb({keys.layout}):a\n" not in log


class _DeviceFakeInput(rq.Request):
	"""XTEST's FakeInput for an event of a given input device, which python-xlib's leaves out."""

	_request = rq.Struct(
		rq.Card8("opcode"),
		rq.Opcode(2),
		rq.RequestLength(),
		rq.Card8("eventType"),
		rq.Card8("detail"),
		rq.Pad(2),
		rq.Card32("time"),
		rq.Card32("root"),
		rq.Pad(8),
		rq.Int16("x"),
		rq.Int16("y"),
		rq.Pad(7),
		rq.Card8("deviceid"),
	)


#: xdotool's names for the modifiers, as X names their left keys.
MODIFIER_KEYS = {"control": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "super": "Super_L"}
#: Seconds a person holds a key, and leaves between the keys of a chord.
HOLD = 0.1
GAP = 0.03


class Keyboard:
	"""Keys typed on the rig display's own keyboard, as a person's keyboard types them.

	xdotool types through XTEST's device, which NVDA sends its own keys through too, so NVDA can let go
	of a key xdotool holds. A person's keyboard is a device of its own: the X server then drops a
	press NVDA sends of a key that keyboard holds, as a key is held while NVDA decides on it. This
	types on the X server's own keyboard device instead, through XTEST's request for device events.
	"""

	def __init__(self) -> None:
		self._display = display.Display()
		devices = self._display.xinput_query_device(xinput.AllDevices).devices
		self._device = next(
			device.deviceid
			for device in devices
			if device.use == xinput.SlaveKeyboard and "XTEST" not in str(device.name)
		)
		self._firstEvent = self._display.query_extension("XInputExtension").first_event
		self._xtest = self._display.query_extension("XTEST").major_opcode

	def _send(self, key: str, down: bool) -> None:
		keysym = XK.string_to_keysym(MODIFIER_KEYS.get(key, key))
		keycode = self._display.keysym_to_keycode(keysym)
		assert keycode, f"no key on the rig's keyboard is {key}"
		_DeviceFakeInput(
			display=self._display.display,
			opcode=self._xtest,
			# DeviceKeyPress and DeviceKeyRelease, as XInput numbers them.
			eventType=self._firstEvent + (1 if down else 2),
			detail=keycode,
			time=0,
			root=0,
			x=0,
			y=0,
			deviceid=self._device,
		)
		self._display.sync()

	def down(self, key: str) -> None:
		self._send(key, True)
		time.sleep(GAP)

	def up(self, key: str) -> None:
		self._send(key, False)
		time.sleep(GAP)

	def press(self, *chords: str, hold: float = HOLD) -> None:
		"""Press each chord in turn, as xdotool names them: its keys down in order, held, then up."""
		for chord in chords:
			keys = chord.split("+")
			for key in keys:
				self.down(key)
			time.sleep(hold)
			for key in reversed(keys):
				self.up(key)

	def type(self, text: str) -> None:
		self.press(*text)

	def close(self) -> None:
		self._display.close()


@pytest.fixture
def keyboard() -> Iterator[Keyboard]:
	board = Keyboard()
	yield board
	board.close()


def waitForEntry(text: str, timeout: float = 3) -> None:
	deadline = time.monotonic() + timeout
	while (found := entryState()[0]) != text:
		assert time.monotonic() < deadline, f"the field holds {found!r}, not {text!r}"
		time.sleep(0.05)


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_nvdaChordWithoutACommandReachesTheApplication(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path, layoutName: str
):
	keys = KeySession(desktop, tmp_path, layoutName)
	# NVDA has no command on z: the field has the z, without the NVDA key, as on Windows. Caps Lock
	# held down is the Lock modifier to the X server, which the key comes with, so with Caps Lock as the
	# NVDA key it may come in capitals.
	keyboard.press(keys.chord("z"))
	waitForEntry("z" if layoutName == "desktop" else "Z")
	# NVDA's own commands are still NVDA's alone, the next one with the NVDA key still held too.
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	mark = desktop.mark()
	keyboard.down(keys.nvda)
	keyboard.press("y")
	time.sleep(0.3)
	keyboard.press("t")
	time.sleep(0.3)
	keyboard.up(keys.nvda)
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0].lower() == "zy"
	assert not desktopSession.capsLockIsOn()
	# The keys after a chord are grabbed again as they were: NVDA+t is NVDA's, and a letter typing.
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	keyboard.press("a")
	time.sleep(0.3)
	assert entryState()[0].lower() == "zya"
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	assert f"Input: kb({keys.layout}):NVDA+z" in launch.text("nvda.log")


def passedTwice(keyboard: Keyboard, key: str) -> None:
	"""Press key twice, as quickly as a person does to have NVDA pass it on."""
	keyboard.press(key)
	time.sleep(0.15)
	keyboard.press(key)
	time.sleep(0.3)


def test_insertPassedThrough(desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path):
	keys = KeySession(desktop, tmp_path, "desktop")
	keyboard.press("Home")
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	# Insert reaches the field, which then types over its text.
	keyboard.press("Insert")
	keyboard.type("x")
	time.sleep(0.3)
	assert entryState() == ("xlice", 1)
	# The NVDA key pressed twice quickly reaches the field too, which types between letters again.
	passedTwice(keyboard, "Insert")
	keyboard.type("y")
	time.sleep(0.3)
	assert entryState() == ("xylice", 2)
	# Passed through, a chord with the NVDA key reaches the field whole: Insert, then the letter.
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	keyboard.press(keys.chord("t"))
	time.sleep(0.3)
	assert entryState() == ("xytice", 3)
	assert "Fixture window" not in desktop.spokenSince(mark)
	keys.quit().assertClean(logNames=("nvda.log",))


def test_capsLockPassedThrough(desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path):
	keys = KeySession(desktop, tmp_path, "laptop")
	keyboard.press("Home")
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	keyboard.press("Caps_Lock")
	time.sleep(0.3)
	assert desktopSession.capsLockIsOn()
	keyboard.type("x")
	time.sleep(0.3)
	assert entryState() == ("XAlice", 1)
	mark = desktop.mark()
	passedTwice(keyboard, "Caps_Lock")
	desktop.expect("caps lock off", since=mark)
	assert not desktopSession.capsLockIsOn()
	passedTwice(keyboard, "Caps_Lock")
	desktop.expect("caps lock off", "caps lock on", since=mark)
	assert desktopSession.capsLockIsOn()
	passedTwice(keyboard, "Caps_Lock")
	assert not desktopSession.capsLockIsOn()
	# One press used as the NVDA key leaves the lock as it was.
	keyboard.press(keys.chord("t"))
	keyboard.type("y")
	time.sleep(0.3)
	assert entryState() == ("XyAlice", 2)
	keys.quit().assertClean(logNames=("nvda.log",))


#: A global plugin for these tests only: a command that blocks NVDA's main thread.
BLOCK_PLUGIN = """
import time

import globalPluginHandler
from logHandler import log
from scriptHandler import script


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	@script(gesture="kb:NVDA+shift+f11")
	def script_blockMainThread(self, gesture):
		log.info("Blocking the main thread")
		time.sleep(3)
		log.info("The main thread is free again")
"""


#: A global plugin for these tests only: a reset of the configuration that leaves it missing for a
#: second, rather than the tens of milliseconds reading nvda.ini takes, so that what reads it from
#: another thread meanwhile surely does; and a command that records which key threads are running.
RESET_PLUGIN = """
import threading
import time

import config
import globalPluginHandler
from logHandler import log
from scriptHandler import script


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		build = config.conf._initBaseConf

		def slowly(factoryDefaults=False):
			log.info("The configuration is missing")
			time.sleep(1)
			build(factoryDefaults=factoryDefaults)
			log.info("The configuration is back")

		config.conf._initBaseConf = slowly

	@script(gesture="kb:NVDA+shift+f11")
	def script_keyThreads(self, gesture):
		running = {thread.name for thread in threading.enumerate()}
		log.info(f"Key threads: {sorted(running & {'keyboardWatch', 'X11KeySource decider'})}")
"""


#: A global plugin for these tests only: the keyboard watcher's own turns stretched to half a
#: minute, which would otherwise hide whether it looks again as soon as a command has run.
SLOW_WATCH_PLUGIN = """
import globalPluginHandler
from linuxPlatform import inputHook


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		inputHook.WATCH_INTERVAL = 30
"""


def blockHooks(directory: Path, plugin: str = BLOCK_PLUGIN) -> dict[str, str]:
	hooks = directory / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "blockProbe.py").write_text(plugin)
	return {"LINDA_TEST_HOOKS_DIR": str(hooks)}


def test_blockedMainThreadLeavesTheNVDAKeysWorking(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path
):
	keys = KeySession(desktop, tmp_path, "desktop", env=blockHooks(tmp_path))
	keyboard.press(keys.chord("shift+F11"))
	desktop.nvda.waitForLog("Blocking the main thread", 5)
	blocked = time.monotonic()
	# The NVDA key's grabs are answered off the main thread, so they hold the keyboard no longer while
	# it is blocked: the chord is NVDA's, and the letters after it reach the field at once.
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	keyboard.type("xy")
	waitForEntry("xy", timeout=1)
	assert time.monotonic() - blocked < 2.5, "the keys were not all typed while NVDA was blocked"
	desktop.nvda.waitForLog("The main thread is free again", 5)
	desktop.expect("Fixture window", since=mark)
	keys.quit().assertClean(logNames=("nvda.log",))


def test_keysTypedDuringAResetLeaveTheCommandsWorking(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path
):
	keys = KeySession(desktop, tmp_path, "desktop", env=blockHooks(tmp_path, RESET_PLUGIN))
	launch = desktop.nvda
	keyboard.press(keys.chord("control+r"))
	launch.waitForLog("The configuration is missing", 5)
	# NVDA reads its configuration for every key, and logging the failure reads it again.
	keyboard.type("xy")
	launch.waitForLog("The configuration is back", 5)
	time.sleep(1)
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	launch = keys.quit()
	# NVDA failing those keys is logged; the thread that hands NVDA its keys dying of it is not.
	launch.assertClean(ignoring=("logErrors",), logNames=("nvda.log",))
	assert "Exception in thread X11KeySource decider" not in launch.text("nvda.log")


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_inputHelpHoldsTheKeyboardAfterAReset(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path, layoutName: str
):
	# The thread that holds the keyboard for input help reads the configuration as often as it looks.
	# Typed on the keyboard, whose releases NVDA handles without its configuration, unlike xdotool's.
	keys = KeySession(desktop, tmp_path, layoutName, env=blockHooks(tmp_path, RESET_PLUGIN))
	launch = desktop.nvda
	keyboard.press(keys.chord("control+r"))
	launch.waitForLog("The configuration is back", 5)
	time.sleep(0.5)
	keyboard.press(keys.chord("shift+F11"))
	launch.waitForLog("Key threads: ['X11KeySource decider', 'keyboardWatch']", 5)
	mark = desktop.mark()
	keyboard.press(keys.chord("1"))
	on = "input help on\N{HORIZONTAL ELLIPSIS}"
	desktop.expect(on, since=mark)
	keyboard.press("b")
	desktop.expect(on, "b\N{HORIZONTAL ELLIPSIS}", since=mark)
	keyboard.press(keys.chord("1"))
	desktop.expect(on, "b\N{HORIZONTAL ELLIPSIS}", "input help off", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("chord", (False, True), ids=("nvdaKey", "chordAfterAPassedKey"))
def test_stoppedNVDALetsTheKeyboardGo(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path, chord: bool
):
	keys = KeySession(desktop, tmp_path, "desktop")
	launch = desktop.nvda
	if chord:
		# z has no command, so the field has it, and every key is grabbed while Insert is still held.
		keyboard.down("Insert")
		keyboard.press("z")
		waitForEntry("z")
		typed = "zxy"
	else:
		typed = "xy"
	os.kill(launch.process.pid, signal.SIGSTOP)
	try:
		pressed = time.monotonic()
		if chord:
			# Held by the X server for NVDA's answer, with y behind it, until the guard lets them go.
			keyboard.type("xy")
		else:
			# Insert held for NVDA's answer, with x and y behind it; the field gets all three, Insert
			# turning on typing over.
			keyboard.press("Insert")
			keyboard.type("xy")
		waitForEntry(typed, timeout=pressed + 3 - time.monotonic())
	finally:
		if chord:
			keyboard.up("Insert")
		os.kill(launch.process.pid, signal.SIGCONT)
	launch.waitForLog("Keys are read from the X display again", 10)
	time.sleep(0.5)
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == typed
	keys.quit().assertClean(logNames=("nvda.log",))


def test_nvdaKeyReleasedWhileNVDAWasStoppedIsReleased(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path
):
	keys = KeySession(desktop, tmp_path, "desktop")
	launch = desktop.nvda
	keyboard.press("End")
	keyboard.down("Insert")
	time.sleep(0.3)
	os.kill(launch.process.pid, signal.SIGSTOP)
	try:
		# The guard lets the keyboard go, and the release goes with the connection it closes.
		time.sleep(3.5)
		keyboard.up("Insert")
	finally:
		os.kill(launch.process.pid, signal.SIGCONT)
	launch.waitForLog("Keys are read from the X display again", 10)
	time.sleep(0.5)
	# A plain t, which an NVDA key still taken for held would make NVDA+t as well.
	mark = desktop.mark()
	keyboard.press("t")
	waitForEntry(ENTRY_TEXT + "t")
	time.sleep(0.5)
	assert "Fixture window" not in desktop.spokenSince(mark)
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	assert f"Input: kb({keys.layout}):NVDA+t" not in launch.text("nvda.log")


#: A window with a text field that prints what the field holds each time it changes.
ENTRY_APP = """
import sys
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
window = Gtk.Window(title=sys.argv[1])
entry = Gtk.Entry()
window.add(entry)
entry.connect("changed", lambda entry: print(entry.get_text(), flush=True))
window.connect("destroy", Gtk.main_quit)
window.show_all()
print("ready", flush=True)
Gtk.main()
"""


def test_nvdaChordJustAfterTheFocusMovesIsNVDAs(desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path):
	# The NVDA keys are grabbed on the window with the focus, and follow it only once the focus has
	# moved: a chord typed in between reaches the application. X has no way to close that gap, which
	# is kept to a few milliseconds; this is how often a chord 5 ms after the switch gets through it.
	app = tmp_path / "entry.py"
	app.write_text(ENTRY_APP)
	titles = ("Left entry", "Right entry")
	apps = {}
	for title in titles:
		apps[title] = desktop.startApp([sys.executable, str(app), title], title=title)
		assert apps[title].stdout.readline().strip() == "ready"
		os.set_blocking(apps[title].stdout.fileno(), False)
	config = tmp_path / "nvda.ini"
	config.write_text(configText("desktop", 6))
	desktop.startNVDA(config)
	desktop.expect(titles[-1], since=0, timeout=STARTUP_TIMEOUT)
	time.sleep(1)
	leaked = []
	for attempt in range(16):
		title = titles[attempt % 2]
		subprocess.run(["wmctrl", "-F", "-a", title], check=True, capture_output=True)
		time.sleep(0.005)
		keyboard._send("Insert", True)
		keyboard._send("t", True)
		keyboard._send("t", False)
		keyboard._send("Insert", False)
		time.sleep(0.6)
		try:
			if os.read(apps[title].stdout.fileno(), 65536):
				leaked.append(attempt)
		except BlockingIOError:
			pass
	# Some 18 in 24 got through before the grabs were sent as soon as they were made.
	assert len(leaked) <= 4, f"{len(leaked)} of 16 chords reached the application: {leaked}"
	desktop.nvda.process.terminate()
	desktop.nvda.waitForExit(20)


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_numpadReviewKeyLeavesTheCaret(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KeySession(desktop, tmp_path, layoutName)
	desktop.press("End")
	before = entryState()
	mark = desktop.mark()
	# Num Lock is off on the rig's display, so this is the review cursor's current line.
	desktop.press("KP_Up")
	desktop.expect(ENTRY_TEXT, since=mark)
	time.sleep(0.3)
	assert entryState() == before
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_inputHelpHoldsTheKeyboard(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KeySession(desktop, tmp_path, layoutName)
	mark = desktop.mark()
	desktop.press(keys.chord("1"))
	on = "input help on\N{HORIZONTAL ELLIPSIS}"
	desktop.expect(on, since=mark)
	desktop.press("t")
	desktop.expect(on, "t\N{HORIZONTAL ELLIPSIS}", since=mark)
	desktop.press(keys.chord("1"))
	desktop.expect(on, "t\N{HORIZONTAL ELLIPSIS}", "input help off", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT
	desktop.press("b")
	time.sleep(0.3)
	assert entryState()[0] == "b", "the keyboard was not let go after input help"
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_chordAfterInputHelpOffInTheSameHoldIsNVDAs(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path, layoutName: str
):
	keys = KeySession(desktop, tmp_path, layoutName)
	mark = desktop.mark()
	keyboard.press(keys.chord("1"))
	on = "input help on\N{HORIZONTAL ELLIPSIS}"
	desktop.expect(on, since=mark)
	# The NVDA key goes down while input help holds the keyboard, so no grab of its own is active
	# once input help lets the keyboard go.
	keyboard.down(keys.nvda)
	keyboard.press("1")
	desktop.expect(on, "input help off", since=mark)
	time.sleep(0.3)
	# NVDA has no command on z, which the field gets as on Windows; it has one on t.
	keyboard.press("z")
	waitForEntry("z" if layoutName == "desktop" else "Z")
	mark = desktop.mark()
	keyboard.press("t")
	keyboard.up(keys.nvda)
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0].lower() == "z"
	assert not desktopSession.capsLockIsOn()
	keys.quit().assertClean(logNames=("nvda.log",))


def test_capsLockChordInAnOpenMenuLeavesTheLockAlone(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path
):
	keys = KeySession(desktop, tmp_path, "laptop")
	# The menu holds the keyboard, so no grab of NVDA's becomes active and every key reaches the menu
	# too; the X server toggles Caps Lock as it goes down whatever.
	keyboard.press("F10")
	time.sleep(0.5)
	keyboard.press("Down")
	time.sleep(0.5)
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.5)
	locked = desktopSession.capsLockIsOn()
	keyboard.press("Escape")
	keyboard.press("Escape")
	assert not locked, "Caps Lock was left on"
	keys.quit().assertClean(logNames=("nvda.log",))


def test_inputHelpTurnedOnInAnOpenMenuHoldsTheKeyboardOnceItCloses(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path
):
	keys = KeySession(desktop, tmp_path, "desktop")
	keyboard.press("F10")
	time.sleep(0.5)
	mark = desktop.mark()
	keyboard.press(keys.chord("1"))
	on = "input help on\N{HORIZONTAL ELLIPSIS}"
	desktop.expect(on, since=mark)
	# The menu holds the keyboard as input help would, so the keys reach the menu, which closes.
	keyboard.press("Escape")
	keyboard.press("Escape")
	time.sleep(0.5)
	assert entryState()[0] == ENTRY_TEXT
	mark = desktop.mark()
	keyboard.press("q")
	desktop.expect("q", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT, "the keyboard was not held once the menu let it go"
	keyboard.press(keys.chord("1"))
	desktop.expect("q", "input help off", since=mark)
	keys.quit().assertClean(logNames=("nvda.log",))


def test_inputHelpHoldsTheKeyboardFromTheNextKey(desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path):
	keys = KeySession(desktop, tmp_path, "desktop", env=blockHooks(tmp_path, SLOW_WATCH_PLUGIN))
	mark = desktop.mark()
	keyboard.press(keys.chord("1"))
	on = "input help on\N{HORIZONTAL ELLIPSIS}"
	desktop.expect(on, since=mark)
	keyboard.press("b")
	desktop.expect(on, "b", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT, "a key typed after input help was turned on reached the field"
	keyboard.press(keys.chord("1"))
	desktop.expect(on, "b", "input help off", since=mark)
	keyboard.press("c")
	waitForEntry("c")
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_controlCancelsReading(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KeySession(
		desktop,
		tmp_path,
		layoutName,
		env={"LINDA_TRANSCRIPT_MODE": "timed", "LINDA_TRANSCRIPT_MS_PER_CHAR": "10"},
	)
	launch = desktop.nvda
	mark = desktop.mark()
	desktop.press(keys.chord("b"), gap=0)
	deadline = time.monotonic() + 10
	while len(desktop.spokenSince(mark)) == 0:
		assert time.monotonic() < deadline, "reading the window did not start"
		time.sleep(0.01)
	pressed = time.monotonic()
	subprocess.run(["xdotool", "key", "Control_L"], check=True)
	time.sleep(1.5)
	records = [r for r in transcript(launch) if r["t"] >= pressed]
	cancels = [r for r in records if r["kind"] == "cancel"]
	assert cancels, f"no cancel after Control: {records}"
	assert cancels[0]["t"] - pressed < 0.1, f"the cancel came {cancels[0]['t'] - pressed:.3f} s after Control"
	assert not [r for r in records if r["kind"] == "speak" and r["t"] > cancels[0]["t"]]
	keys.quit().assertClean(logNames=("nvda.log",))


def test_polishAltGrLettersAreTyping(desktop: DesktopSession, tmp_path: Path):
	subprocess.run(["setxkbmap", "pl"], check=True)
	try:
		keys = KeySession(desktop, tmp_path, "desktop")
		desktop.typeText("ął")
		time.sleep(0.3)
		assert entryState()[0] == "ął"
		launch = keys.quit()
		launch.assertClean(logNames=("nvda.log",))
		log = launch.text("nvda.log")
		assert log.count("Input: kb(desktop):(typing key)") >= 2
		assert "alt+a" not in log and "alt+l" not in log
	finally:
		subprocess.run(["setxkbmap", "us"], check=True)


#: The key map fed into NVDA's KeyboardInputGesture, as the recorded names were made.
NAMES_CHILD = r"""
import NVDAHelper  # noqa: F401
import xkbcommon.xkb as xkb

import inputCore
import keyboardHandler
import languageHandler
import speech
import types
import winUser
from linuxPlatform import inputHook
from linuxPlatform.keys import keyMap

languageHandler.setLanguage("en")
inputCore.initialize()
# Speech is not running here; the shift keys ask whether it is paused.
speech.getState = lambda: types.SimpleNamespace(isPaused=False)

context = xkb.Context()
EXTRA = {224, 225, 238, 237, 190, 85, 117}
ALL = sorted(set(keyMap.FIXED) | set(keyMap.NUMPAD) | set(keyMap.LAYOUT) | EXTRA)
result = {}
for layout in LAYOUTS:
	keymap = context.keymap_new_from_names(rules="evdev", model="pc105", layout=layout)
	state = keymap.state_new()
	t = keyMap.Translator(keymap, state)
	numLock = {"on": False}
	inputHook.mapVirtualKeyToChar = t.mapVirtualKeyToChar
	inputHook.vkKeyScan = t.vkKeyScan
	inputHook.getKeyState = lambda vk: int(numLock["on"]) if vk == 0x90 else 0
	winUser.getKeyNameText = lambda scan, ext: "keyNameText_%02x%s" % (scan, "e" if ext else "")

	def describe(keycode, modifiers=()):
		vk, scan, ext = t.translate(keycode)
		return keyboardHandler.KeyboardInputGesture(set(modifiers), vk, scan, ext), (vk, scan, ext)

	lines = []
	for lockOn in (False, True):
		if lockOn:
			state.update_key(77, xkb.XKB_KEY_DOWN)
			state.update_key(77, xkb.XKB_KEY_UP)
			numLock["on"] = True
		for evdev in ALL:
			if lockOn and evdev not in keyMap.NUMPAD and evdev not in (55, 74, 78, 98):
				continue
			keycode = evdev + 8
			sym = xkb.keysym_get_name(state.key_get_one_sym(keycode)) if keymap.key_get_name(keycode) else "-"
			g, hook = describe(keycode)
			ident = g.identifiers[1]
			lines.append(
				f"evdev={evdev} xkeysym={sym} numlock={'on' if lockOn else 'off'} hook=(vk={hook[0]:#04x}, scan={hook[1]:#04x}, "
				f"ext={hook[2]}) -> {ident} character={g.isCharacter} command={g.shouldReportAsCommand} "
				f"speech={g.speechEffectWhenExecuted}"
			)
		if lockOn:
			state.update_key(77, xkb.XKB_KEY_DOWN)
			state.update_key(77, xkb.XKB_KEY_UP)
			numLock["on"] = False
	combos = {
		"insert+t (NVDA+t)": ({(0x2D, True)}, 20 + 8),
		"control+alt+n": ({(0xA2, False), (0xA4, False)}, 49 + 8),
		"altgr+a as Windows reports it (fake left control and right alt)": ({(0xA2, False), (0xA5, True)}, 30 + 8),
		"right alt+a without the fake control": ({(0xA5, True)}, 30 + 8),
		"shift+a": ({(0xA0, False)}, 30 + 8),
		"numpad insert+downArrow (NVDA+downArrow)": ({(0x2D, False)}, 108 + 8),
		"capsLock+t": ({(0x14, False)}, 20 + 8),
		"windows+d": ({(0x5B, True)}, 32 + 8),
	}
	for label, (mods, keycode) in combos.items():
		g, hook = describe(keycode, mods)
		lines.append(f"combo {label}: -> {g.identifiers[1]} display={g.displayName!r} character={g.isCharacter} speech={g.speechEffectWhenExecuted}")
	for ch in "aA1!ąĄł;'[":
		try:
			lines.append(f"fromName {ch!r}: {keyboardHandler.KeyboardInputGesture.fromName(ch).identifiers[1]}")
		except (LookupError, ValueError) as e:
			# Since NVDA's #20647, fromName raises ValueError for an unknown key name, from the
			# LookupError that names the key; the recorded names keep the cause.
			cause = e.__cause__ if isinstance(e, ValueError) and e.__cause__ is not None else e
			lines.append(f"fromName {ch!r}: {type(cause).__name__}: {cause}")
	result[layout] = lines
"""


def test_keyNamesMatchTheRecordedOnes(tmp_path: Path):
	layouts = ("us", "pl")
	done = harness.runChild(tmp_path, f"LAYOUTS = {layouts!r}\n" + NAMES_CHILD)
	assert done.error is None, done.error
	done.assertClean()
	for layout in layouts:
		expected = (KEY_NAMES / f"names-{layout}.txt").read_text().splitlines()
		assert done.value[layout] == expected


#: The key source alone, in a child, with a window of the child's own to grab keys on and the rig's
#: keyboard to type on. Its NVDA keeps a captured Space and the NVDA key, as browse mode's activation
#: and NVDA+Space do, and passes every other key.
SOURCE_CHILD = r"""
import sys, time
sys.path.insert(0, str(harness.HERE))
from Xlib import X, Xatom, display
from linuxPlatform.keys import x11
from test_keys import Keyboard

delivered = []

def deliver(event):
	delivered.append((event.vkCode, event.isUp))
	return not event.captured or event.vkCode not in (0x20, 0x2D)

source = x11.X11KeySource()
source.start(deliver, lambda: "browse")
app = display.Display()
root = app.screen().root
pidAtom = app.intern_atom("_NET_WM_PID")
kb = Keyboard()
source.setConsumed(x11.EXTENDED_INSERT, True)

def makeWindow(parent=None):
	window = (parent or root).create_window(
		0, 0, 200, 200, 0, app.screen().root_depth, event_mask=X.KeyPressMask
	)
	window.change_property(pidAtom, Xatom.CARDINAL, 32, [1])
	window.map()
	app.sync()
	time.sleep(0.3)
	window.set_input_focus(X.RevertToParent, X.CurrentTime)
	app.sync()
	time.sleep(0.2)
	return window

def reaches(key, timeout=3.0):
	while app.pending_events():
		app.next_event()
	kb.press(key)
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		while app.pending_events():
			if app.next_event().type == X.KeyPress:
				return True
		time.sleep(0.05)
	return False
"""

#: Shift+Tab in browse mode, the Tab kept and sent back by NVDA a moment later as
#: KeyboardInputGesture.send sends it, while the person still holds both keys; then let go of in
#: each order. Each press of Tab the window gets, with whether Shift was down for it.
SENT_BACK_CHILD = r"""
import threading
from Xlib import XK
VK = x11.keyMap.VK
first = makeWindow()
source.setCapture("browse")
time.sleep(0.5)

def sendBack():
	time.sleep(0.02)
	shift = not source.keyState(VK["SHIFT"]) & 0x8000
	if shift:
		source.inject(VK["SHIFT"], 0, False, False)
	if source.keyState(VK["TAB"]) & 0x8000:
		source.inject(VK["TAB"], 0, False, True)
	source.inject(VK["TAB"], 0, False, False)
	source.inject(VK["TAB"], 0, False, True)
	if shift:
		source.inject(VK["SHIFT"], 0, False, True)

def keepTab(event):
	if event.captured and event.vkCode == VK["TAB"] and not event.isUp:
		threading.Thread(target=sendBack).start()
		return False
	return True

source._deliver = keepTab
tab = app.keysym_to_keycode(XK.string_to_keysym("Tab"))
result = {}
for order in (("Shift_L", "Tab"), ("Tab", "Shift_L")):
	while app.pending_events():
		app.next_event()
	kb.down("Shift_L")
	kb.down("Tab")
	time.sleep(0.15)
	for name in order:
		kb.up(name)
	time.sleep(0.5)
	tabs = []
	while app.pending_events():
		event = app.next_event()
		if event.type == X.KeyPress and event.detail == tab:
			tabs.append(bool(event.state & X.ShiftMask))
	result[order[0] + " first"] = tabs
source.stop()
"""


def test_keySentBackKeepsItsModifiers(tmp_path: Path):
	# The X server drops a key sent while the keyboard holds it, so it goes once the keyboard lets
	# go, by which time a person has often let go of Shift too.
	with DesktopSession(tmp_path / "desktop", windowManager=False):
		done = harness.runChild(tmp_path, SOURCE_CHILD + SENT_BACK_CHILD, timeout=60)
	assert done.error is None, done.error
	assert done.value == {"Shift_L first": [True], "Tab first": [True]}


#: numpad Insert as the NVDA key, pressed after a modifier and with f, each key kept by NVDA: the
#: key codes of the presses the window gets for each modifier, which should be that modifier's alone.
NUMPAD_INSERT_CHILD = r"""
from Xlib import XK
source.setConsumed(x11.NUMPAD_INSERT, False)
first = makeWindow()
time.sleep(0.5)
source._deliver = lambda event: not event.captured
result = {}
for modifier in ("Control_L", "Alt_L", "Super_L"):
	while app.pending_events():
		app.next_event()
	kb.down(modifier)
	kb.down("KP_Insert")
	kb.press("f")
	kb.up("KP_Insert")
	kb.up(modifier)
	time.sleep(0.3)
	got = []
	while app.pending_events():
		event = app.next_event()
		if event.type == X.KeyPress:
			got.append(event.detail)
	result[modifier] = got == [app.keysym_to_keycode(XK.string_to_keysym(modifier))] or got
source.stop()
"""


def test_numpadInsertAfterAModifierIsNVDAs(tmp_path: Path):
	# Num Lock is off on the rig's display, so the key is numpad Insert rather than 0.
	with DesktopSession(tmp_path / "desktop", windowManager=False):
		done = harness.runChild(tmp_path, SOURCE_CHILD + NUMPAD_INSERT_CHILD, timeout=60)
	assert done.error is None, done.error
	assert done.value == {"Control_L": True, "Alt_L": True, "Super_L": True}


#: Space kept by NVDA in browse mode, its window going while the key is still down, then NVDA+Space
#: in the window after it.
WINDOW_GONE_CHILD = r"""
outer = makeWindow() if GONE == "parentUnmapped" else None
first = makeWindow(outer)
source.setCapture("browse")
time.sleep(0.5)
kb.down("space")
time.sleep(0.3)
{"destroyed": first.destroy, "unmapped": first.unmap, "parentUnmapped": lambda: outer.unmap()}[GONE]()
app.sync()
time.sleep(0.2)
kb.up("space")
time.sleep(0.3)
second = makeWindow()
source.setCapture("browse")
time.sleep(0.5)
kb.down("Insert")
kb.press("space")
kb.up("Insert")
time.sleep(0.3)
result = {"reached": reaches("F6")}
source.stop()
"""


#: The child's process group stopped, as Control+Z in the terminal it runs in stops its job, by a
#: helper outside it, which then presses the NVDA key and F5 on the rig's keyboard.
JOB_STOPPED_CHILD = r"""
import json, os, signal, subprocess, textwrap
first = makeWindow()
time.sleep(0.5)
helper = textwrap.dedent(f'''
	import json, os, signal, sys, time
	sys.path.insert(0, {str(harness.HERE)!r})
	from Xlib import X, display
	from test_keys import Keyboard
	kb = Keyboard()
	app = display.Display()
	window = app.create_resource_object("window", {first.id})
	window.change_attributes(event_mask=X.KeyPressMask)
	app.sync()
	os.killpg({os.getpgid(0)}, signal.SIGSTOP)
	try:
		time.sleep(0.5)
		kb.press("Insert")
		time.sleep(0.3)
		while app.pending_events():
			app.next_event()
		kb.press("F5")
		deadline = time.monotonic() + 6
		reached = False
		while time.monotonic() < deadline and not reached:
			while app.pending_events():
				if app.next_event().type == X.KeyPress:
					reached = True
			time.sleep(0.05)
	finally:
		os.killpg({os.getpgid(0)}, signal.SIGCONT)
	print(json.dumps(reached))
''')
stopper = subprocess.run(
	[sys.executable, "-c", helper], capture_output=True, text=True, timeout=60, start_new_session=True
)
result = {"reached": json.loads(stopper.stdout or "null"), "stderr": stopper.stderr[-2000:]}
source.stop()
"""


def test_stoppedJobLetsTheKeyboardGo(tmp_path: Path):
	with DesktopSession(tmp_path / "desktop", windowManager=False):
		# A group of its own, as the shell gives a job, so that stopping it stops neither pytest nor
		# the rig.
		done = harness.runChild(
			tmp_path, "import os\nos.setpgid(0, 0)\n" + SOURCE_CHILD + JOB_STOPPED_CHILD, timeout=60
		)
	assert done.error is None, done.error
	assert done.value["reached"] is True, f"F5 was held while the job was stopped: {done.value}"


#: The NVDA key held on the rig's keyboard by a helper, and released while the whole child stalls,
#: as the guard sees a process that holds the interpreter lock in a call that does not return.
STALLED_CHILD = r"""
import ctypes, subprocess, textwrap
first = makeWindow()
time.sleep(0.5)
helper = textwrap.dedent(f'''
	import sys, time
	sys.path.insert(0, {str(harness.HERE)!r})
	from test_keys import Keyboard
	kb = Keyboard()
	time.sleep(0.3)
	kb.down("Insert")
	time.sleep(1.0)
	kb.up("Insert")
''')
typist = subprocess.Popen([sys.executable, "-c", helper])
time.sleep(0.8)
heldBefore = 118 in source._keycodesDown
ctypes.PyDLL(None).sleep(4)
typist.wait()
time.sleep(3)
result = {
	"heldBefore": heldBefore,
	"keyState": source.keyState(0x2D),
	"keyboardDown": sorted(source._keyboardDown),
	"released": (0x2D, True) in delivered,
}
source.stop()
"""


def test_keyReleasedDuringAStallIsReleased(tmp_path: Path):
	with DesktopSession(tmp_path / "desktop", windowManager=False):
		done = harness.runChild(tmp_path, SOURCE_CHILD + STALLED_CHILD, timeout=60)
	assert done.error is None, done.error
	assert done.value == {"heldBefore": True, "keyState": 0, "keyboardDown": [], "released": True}


#: A captured key and the NVDA key typed while another program holds the keyboard, as a menu or a
#: password prompt does, then again once it lets go: how long each press took to reach NVDA, and
#: whether it was taken from the application.
OTHER_GRAB_CHILD = r"""
first = makeWindow()
source.setCapture("browse")
time.sleep(0.5)
arrived = []

def note(event):
	if not event.isUp:
		arrived.append((event.vkCode, time.monotonic(), event.captured))
	return True

source._deliver = note

def timed(name, vk):
	pressed = time.monotonic()
	kb.press(name)
	time.sleep(0.6)
	return [(t - pressed < 0.1, captured) for code, t, captured in arrived if code == vk][-1]

popup = root.create_window(0, 0, 50, 50, 0, app.screen().root_depth, override_redirect=True)
popup.map()
app.sync()
time.sleep(0.2)
popup.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
app.sync()
time.sleep(0.3)
result = {"held": {"Down": timed("Down", 0x28), "Insert": timed("Insert", 0x2D)}}
app.ungrab_keyboard(X.CurrentTime)
app.sync()
time.sleep(0.3)
result["free"] = {"Down": timed("Down", 0x28), "Insert": timed("Insert", 0x2D)}
source.stop()
"""


def test_keysReachNVDAAtOnceWhileAnotherProgramHoldsTheKeyboard(tmp_path: Path):
	# No grab of NVDA's takes a key from the program that holds the keyboard, and a press waited for
	# as one held for NVDA kept every key after it waiting too, each cutting off what NVDA had begun
	# to say of the menu item it moved to.
	with DesktopSession(tmp_path / "desktop", windowManager=False):
		done = harness.runChild(tmp_path, SOURCE_CHILD + OTHER_GRAB_CHILD, timeout=60)
	assert done.error is None, done.error
	assert done.value == {
		"held": {"Down": [True, False], "Insert": [True, False]},
		"free": {"Down": [True, True], "Insert": [True, True]},
	}


@pytest.mark.parametrize("gone", ("destroyed", "unmapped", "parentUnmapped"))
def test_keyboardGoesFreeAfterTheWindowOfAHeldKeyGoes(tmp_path: Path, gone: str):
	# The X server ends a grab whose window goes, and the release of the key that made it active
	# never comes through it. Taken for still active, that grab would leave the release of Space under
	# the NVDA key's own grab unanswered, and the keyboard held until NVDA let go of it.
	with DesktopSession(tmp_path / "desktop", windowManager=False):
		done = harness.runChild(tmp_path, f"GONE = {gone!r}\n" + SOURCE_CHILD + WINDOW_GONE_CHILD, timeout=60)
	assert done.error is None, done.error
	assert done.value == {"reached": True}, "the keyboard stayed held after NVDA+Space"
