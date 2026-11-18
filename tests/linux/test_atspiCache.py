"""libatspi's cache under NVDA's AT-SPI client: what it has read once is answered without asking the
application again, and an application's events keep it current.

An application stopped with SIGSTOP answers nothing, so a read of it either comes from the cache at
once or waits out libatspi's deadline; that tells the two apart without counting D-Bus messages.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_atspiCache.py
"""

import json
import signal
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
from desktopSession import DesktopSession

GTK_TITLE = "Fixture window"

#: Reads the fixture's window and its first objects, stops the application, reads them again, and
#: reports what each read gave and how long the second took.
READ_STOPPED = """
import os
import signal
import time

from linuxPlatform.atspi import client as atspiClient

c = atspiClient.Client()
window = next(
	child
	for app in c.applications()
	for child in c.children(app)
	if c.name(child) == {title}
)
nodes = [window, *c.children(window)][:6]


def facts():
	return [(c.role(n), c.name(n), c.description(n), sorted(c.states(n))) for n in nodes]


before = facts()
os.kill({pid}, signal.SIGSTOP)
try:
	started = time.monotonic()
	try:
		after = facts()
	except atspiClient.NodeGone as e:
		after = repr(e)
	seconds = time.monotonic() - started
	# A fresh read asks the application, which does not answer while it is stopped.
	try:
		c.states(nodes[0], fresh=True)
		statesAnswered = True
	except atspiClient.NodeGone:
		statesAnswered = False
finally:
	os.kill({pid}, signal.SIGCONT)
result = {{"caching": c.caching, "same": after == before, "seconds": seconds, "statesAnswered": statesAnswered}}
"""


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def readStopped(tmp_path: Path, desktop: DesktopSession, env: dict[str, str] | None = None) -> dict:
	app = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert app.stdout.readline() == "ready\n"
	done = harness.runChild(
		tmp_path,
		READ_STOPPED.format(title=json.dumps(GTK_TITLE), pid=app.pid),
		prepare="bootstrap",
		env=env,
	)
	app.send_signal(signal.SIGCONT)
	done.assertClean()
	return done.value


def test_factsReadOnceAreAnsweredWithoutTheApplication(tmp_path, desktop):
	value = readStopped(tmp_path, desktop)
	assert value["caching"] is True
	assert value["same"] is True, value
	# Six objects, four facts each: asked of the stopped application, the first would already wait
	# out libatspi's 300 ms deadline.
	assert value["seconds"] < 0.1, value
	assert value["statesAnswered"] is False, value


def test_cacheCanBeTurnedOff(tmp_path, desktop):
	value = readStopped(tmp_path, desktop, env={"LINDA_ATSPI_CACHE": "0"})
	assert value["caching"] is False
	assert value["same"] is False, value
	# The first read waits out libatspi's 300 ms deadline, less what timing jitter takes.
	assert value["seconds"] >= 0.25, value
