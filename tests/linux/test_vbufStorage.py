"""NVDA's buffer storage on Linux (engine/vbuf), its binding (linuxPlatform.vbufLib) and the thread
that applies the engine's updates to it (linuxPlatform.vbufUpdates), on render streams the tests
write. No document and no engine take part.

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_vbufStorage.py

The library is the one LINDA_VBUF_LIB names when it names a file, and is otherwise built here
with engine/vbuf/build.sh; the sanitizer build is always built here.
"""

import ctypes
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import harness
import pytest

BUILD = harness.ROOT / "engine" / "vbuf" / "build.sh"
BINDING = harness.SOURCE / "linuxPlatform" / "vbufLib.py"
#: Seconds for a build of the library, which the sanitizer slows.
BUILD_BOUND = 600


# The stream writers and the binding loader below are also sent, as source, to the processes the
# concurrency and sanitizer tests start, so they use nothing but the standard library.


def units(text: str) -> int:
	return len(text.encode("utf-16-le", "surrogatepass")) // 2


def string(text: str) -> str:
	return f"{units(text)}:{text}"


def attributes(attrs: dict | None) -> str:
	attrs = attrs or {}
	return f"{len(attrs)};" + "".join(string(name) + string(value) for name, value in attrs.items())


def control(
	ID: int, *children: str, block: bool = False, hidden: bool = False, attrs: dict | None = None
) -> str:
	flags = (1 if block else 0) | (2 if hidden else 0)
	return f"C1,{ID},{flags},{attributes(attrs)}" + "".join(children) + "E"


def text(value: str, attrs: dict | None = None) -> str:
	return f"T{string(value)}{attributes(attrs)}"


def replace(ID: int, subtree: str) -> str:
	return f"S1,{ID};{subtree}"


def reuse(ID: int) -> str:
	return f"R1,{ID};"


class Stream:
	"""A render stream in memory this object keeps, as vbufLib takes the engine's."""

	def __init__(self, value: str) -> None:
		data = value.encode("utf-16-le", "surrogatepass")
		self.length = len(data) // 2
		self._array = (ctypes.c_uint16 * max(self.length, 1)).from_buffer_copy(data or b"\0\0")
		self.pointer = ctypes.addressof(self._array)


def loadBinding(binding: str, library: str):
	"""linuxPlatform.vbufLib bound to library, loaded from its file so that nothing else of NVDA's is."""
	spec = importlib.util.spec_from_file_location("vbufLibUnderTest", binding)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	dll = ctypes.CDLL(library)
	module._bind(dll)
	module._lib = dll
	return module


def textOf(vbuf, handle) -> str:
	return vbuf.VBuf_getTextInRange(handle, 0, vbuf.lib().VBuf_getTextLength(handle), False) or ""


def caretOf(vbuf, handle) -> int:
	start, end = ctypes.c_int(), ctypes.c_int()
	vbuf.lib().VBuf_getSelectionOffsets(handle, ctypes.byref(start), ctypes.byref(end))
	return start.value


def nodeOf(vbuf, handle, ID: int) -> int:
	node = ctypes.c_uint64()
	vbuf.lib().VBuf_getControlFieldNodeWithIdentifier(handle, 1, ID, ctypes.byref(node))
	return node.value


def offsetsOf(vbuf, handle, node: int) -> tuple[int, int] | None:
	start, end = ctypes.c_int(), ctypes.c_int()
	if not vbuf.lib().VBuf_getFieldNodeOffsets(handle, node, ctypes.byref(start), ctypes.byref(end)):
		return None
	return start.value, end.value


def lineAt(vbuf, handle, offset: int, maxLength: int = 100) -> tuple[int, int] | None:
	start, end = ctypes.c_int(), ctypes.c_int()
	# NVDA's default, useScreenLayout, which lets a line run across inline fields.
	if not vbuf.lib().VBuf_getLineOffsets(
		handle, offset, maxLength, 1, ctypes.byref(start), ctypes.byref(end)
	):
		return None
	return start.value, end.value


def findSpec(attribs: dict[str, list[str | None]]) -> tuple[str, str]:
	"""What virtualBuffers._prepareForFindByAttributes makes of one option of exact values, where None
	means the attribute is absent."""
	regexp = []
	for name, values in attribs.items():
		alternatives = "|".join(f"{value};" if value is not None else ";" for value in values)
		regexp.append(f"{name}:(?:{alternatives})")
	return " ".join(attribs), "".join(regexp)


def find(vbuf, handle, offset: int, direction: int, attribs: dict) -> tuple[int, int, int] | None:
	"""The node, start and end findNodeByAttributes gives; direction 0 is forward and 1 back."""
	names, regexp = findSpec(attribs)
	start, end, node = ctypes.c_int(), ctypes.c_int(), ctypes.c_uint64()
	vbuf.VBuf_findNodeByAttributes(
		handle,
		offset,
		direction,
		names,
		regexp,
		ctypes.byref(start),
		ctypes.byref(end),
		ctypes.byref(node),
	)
	if not node.value:
		return None
	return node.value, start.value, end.value


def document(first: str = "First paragraph.") -> str:
	"""A heading between two paragraphs, all blocks, the caret's usual company in the update tests."""
	return control(
		1,
		control(2, text(first), block=True),
		control(3, text("Heading"), block=True, attrs={"role": "40", "level": "1"}),
		control(4, text("Last."), block=True),
		block=True,
	)


def _workload(binding: str, library: str, iterations: int) -> dict:
	"""Malformed batches, bad handles, then a reader and an updater on one buffer at the same time,
	iterations times each. Returns counts; a fault ends the process instead."""
	import random
	import threading

	vbuf = loadBinding(binding, library)
	lib = vbuf.lib()
	paragraphs = 20

	def page(generation: int) -> str:
		return control(
			1,
			*(
				control(10 + i, text(f"Paragraph {i} of generation {generation}. " * (1 + i % 3)), block=True)
				for i in range(paragraphs)
			),
			block=True,
		)

	handle = vbuf.create()
	vbuf.build(handle, Stream(page(0)))
	refused = 0
	for bad in (
		"X",
		"S1,10;",
		"S1,10;C1,10,1,0;T3:ab",
		"S1,10;T1:x0;",
		replace(10, control(10)) + replace(1, control(1)),
		replace(10, control(10)) + replace(10, control(10)),
		replace(10, control(10, reuse(11))),
		replace(1, control(1, reuse(10), reuse(10))),
		"S1,1;R1,10;",
		replace(10, control(10)) + "S",
	):
		refused += not vbuf.update(handle, Stream(bad))
	other = vbuf.create()
	vbuf.build(other, Stream(page(1)))
	foreign = nodeOf(vbuf, other, 10)
	vbuf.destroy(other)
	bad = 0
	bad += not offsetsOf(vbuf, handle, foreign)
	bad += not lib.VBuf_getTextLength(other)
	bad += not vbuf.update(other, Stream(replace(10, control(10))))
	bad += not lineAt(vbuf, handle, -1)
	vbuf.destroy(other)

	applied = [0]
	reads = [0]
	stop = threading.Event()

	def updater() -> None:
		for i in range(iterations):
			if i % 2:
				kept = [reuse(10 + j) for j in range(paragraphs) if j != i % paragraphs]
				fresh = control(10 + i % paragraphs, text(f"Fresh {i}. " * (1 + i % 4)), block=True)
				batch = replace(
					1, control(1, *kept[: i % paragraphs], fresh, *kept[i % paragraphs :], block=True)
				)
			else:
				j = i % paragraphs
				batch = replace(10 + j, control(10 + j, text(f"Changed {i}. " * (1 + i % 5)), block=True))
			applied[0] += vbuf.update(handle, Stream(batch))
		stop.set()

	def reader() -> None:
		rng = random.Random(1)
		start, end, docHandle, ID = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
		node = ctypes.c_uint64()
		count = 0
		while count < iterations or not stop.is_set():
			length = lib.VBuf_getTextLength(handle)
			offset = rng.randrange(max(length, 1))
			lineAt(vbuf, handle, offset)
			vbuf.VBuf_getTextInRange(handle, offset, min(offset + 40, length), True)
			lib.VBuf_locateControlFieldNodeAtOffset(
				handle,
				offset,
				ctypes.byref(start),
				ctypes.byref(end),
				ctypes.byref(docHandle),
				ctypes.byref(ID),
				ctypes.byref(node),
			)
			# Often a field the updater has freed since.
			lib.VBuf_getFieldNodeOffsets(handle, node, ctypes.byref(start), ctypes.byref(end))
			lib.VBuf_getIdentifierFromControlFieldNode(
				handle, node, ctypes.byref(docHandle), ctypes.byref(ID)
			)
			find(vbuf, handle, offset, count % 2, {"role": ["40"]})
			lib.VBuf_setSelectionOffsets(handle, offset, offset)
			count += 1
		reads[0] = count

	threads = [threading.Thread(target=updater), threading.Thread(target=reader)]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()
	length = lib.VBuf_getTextLength(handle)
	vbuf.destroy(handle)
	return {"refused": refused, "bad": bad, "applied": applied[0], "reads": reads[0], "length": length}


def workloadScript(iterations: int) -> str:
	"""A program that runs _workload on the library its first argument names and prints its counts."""
	parts = [
		"import ctypes",
		"import importlib.util",
		"import json",
		"import sys",
		*(
			inspect.getsource(f)
			for f in (
				units,
				string,
				attributes,
				control,
				text,
				replace,
				reuse,
				Stream,
				loadBinding,
				nodeOf,
				offsetsOf,
				lineAt,
				findSpec,
				find,
				_workload,
			)
		),
		f"print(json.dumps(_workload({str(BINDING)!r}, sys.argv[1], {iterations})))",
	]
	return "\n\n".join(parts)


def buildLibrary(out: Path, mode: str = "release") -> Path:
	done = subprocess.run(
		["bash", str(BUILD), str(harness.ROOT / "nvdaHelper"), str(out), mode],
		capture_output=True,
		text=True,
		timeout=BUILD_BOUND,
		check=False,
	)
	assert done.returncode == 0, f"build.sh {mode} failed:\n{done.stderr[-4000:]}"
	return out


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> Path:
	named = os.environ.get("LINDA_VBUF_LIB", "")
	if os.path.isfile(named):
		return Path(named)
	return buildLibrary(tmp_path_factory.mktemp("vbuf") / "libnvdaVbuf.so")


@pytest.fixture(scope="module")
def vbuf(library: Path) -> ModuleType:
	return loadBinding(str(BINDING), str(library))


@pytest.fixture
def buffers(vbuf: ModuleType) -> Iterator:
	"""buffers(stream) is a new buffer built from stream; each is destroyed after the test."""
	made = []

	def make(value: str) -> int:
		handle = vbuf.create()
		made.append(handle)
		vbuf.build(handle, Stream(value))
		return handle

	yield make
	for handle in made:
		vbuf.destroy(handle)


def test_astralCharacterIsTwoUnits(tmp_path: Path, library: Path):
	stream = control(1, control(2, text("a\U0001f600b"), block=True), block=True)
	done = harness.runChild(
		tmp_path,
		f"""
		import ctypes
		from NVDAHelper import localLib
		from linuxPlatform import vbufLib
		import XMLFormatting
		data = {stream!r}.encode("utf-16-le", "surrogatepass")
		array = (ctypes.c_uint16 * (len(data) // 2)).from_buffer_copy(data)

		class Stream:
			pointer = ctypes.addressof(array)
			length = len(data) // 2

		handle = ctypes.c_void_p(vbufLib.create())
		vbufLib.build(handle.value, Stream)
		length = localLib.VBuf_getTextLength(handle)
		markup = localLib.VBuf_getTextInRange(handle, 0, length, True)
		commands = XMLFormatting.XMLTextParser().parse(markup)
		result = {{
			"length": length,
			"plain": localLib.VBuf_getTextInRange(handle, 0, length, False),
			"second": localLib.VBuf_getTextInRange(handle, 1, 3, False),
			"markup": markup,
			"parsed": "".join(c for c in commands if isinstance(c, str)),
		}}
		localLib.VBuf_destroyBuffer(ctypes.byref(handle))
		result["cleared"] = handle.value is None
		""",
		env={"LINDA_VBUF_LIB": str(library)},
	)
	done.assertClean()
	value = done.value
	assert value["length"] == 4
	assert value["plain"] == "a\U0001f600b"
	assert value["second"] == "\U0001f600"
	# xml.h writes each half of the pair on its own; NVDA's parser joins them.
	assert '<unich value="55357" /><unich value="56832" />' in value["markup"]
	assert value["parsed"] == "a\U0001f600b"
	assert value["cleared"]


def test_linesBreakAtBlocksAndWrapAt100(vbuf: ModuleType, buffers):
	words = " ".join(f"word{i:02d}" for i in range(22))
	assert 150 <= len(words) < 160
	handle = buffers(
		control(
			1,
			control(2, text("Short line."), block=True),
			control(3, text("Lead "), control(4, text(words)), block=True),
			block=True,
		),
	)
	full = textOf(vbuf, handle)
	assert lineAt(vbuf, handle, 3) == (0, len("Short line."))
	paragraph = offsetsOf(vbuf, handle, nodeOf(vbuf, handle, 3))
	start, end = lineAt(vbuf, handle, paragraph[0])
	assert start == paragraph[0]
	assert end - start <= 100
	assert full[end - 1] == " "
	assert lineAt(vbuf, handle, end) == (end, paragraph[1])
	# The same text inline in one block with no limit is one line.
	assert lineAt(vbuf, handle, paragraph[0], 0) == paragraph


def test_quickNavigationSearches(vbuf: ModuleType, buffers):
	link = "19"
	handle = buffers(
		control(
			1,
			control(2, text("Title"), block=True, attrs={"role": "40", "level": "1"}),
			control(
				3,
				text("See "),
				control(4, text("old"), attrs={"role": link, "state_99": "1"}),
				text(" and "),
				control(5, text("new"), attrs={"role": link}),
				block=True,
			),
			control(6, text("Section"), block=True, attrs={"role": "40", "level": "2"}),
			control(7, text("Body."), block=True),
			control(8, text("End"), block=True, attrs={"role": "40", "level": "2"}),
			block=True,
		),
	)
	heading = {"role": ["40"]}
	starts = {ID: offsetsOf(vbuf, handle, nodeOf(vbuf, handle, ID))[0] for ID in (2, 4, 5, 6, 8)}
	assert find(vbuf, handle, -1, 0, heading)[1] == starts[2]
	assert find(vbuf, handle, 0, 0, heading)[1] == starts[6]
	assert find(vbuf, handle, starts[6], 0, heading)[1] == starts[8]
	assert find(vbuf, handle, starts[8], 0, heading) is None
	assert find(vbuf, handle, starts[8], 1, heading)[1] == starts[6]
	assert find(vbuf, handle, starts[6], 1, heading)[1] == starts[2]
	assert find(vbuf, handle, starts[2], 1, heading) is None
	assert find(vbuf, handle, 0, 0, {"role": ["40"], "level": ["2"]})[1] == starts[6]
	unvisited = {"role": [link], "state_99": [None]}
	assert find(vbuf, handle, 0, 0, unvisited)[1] == starts[5]
	assert find(vbuf, handle, starts[8], 1, unvisited)[1] == starts[5]
	assert find(vbuf, handle, 0, 0, {"role": [link], "state_99": ["1"]})[1] == starts[4]


def test_replacementBeforeTheCaretKeepsIt(vbuf: ModuleType, buffers):
	handle = buffers(document())
	heading = offsetsOf(vbuf, handle, nodeOf(vbuf, handle, 3))
	vbuf.lib().VBuf_setSelectionOffsets(handle, heading[0] + 2, heading[0] + 2)
	longer = "A much longer first paragraph than before."
	assert vbuf.update(handle, Stream(replace(2, control(2, text(longer), block=True))))
	assert textOf(vbuf, handle) == f"{longer}HeadingLast."
	caret = caretOf(vbuf, handle)
	assert caret == len(longer) + 2
	assert textOf(vbuf, handle)[caret] == "a"


def test_replacingTheRootKeepsTheCaret(vbuf: ModuleType, buffers):
	handle = buffers(document())
	heading = offsetsOf(vbuf, handle, nodeOf(vbuf, handle, 3))
	vbuf.lib().VBuf_setSelectionOffsets(handle, heading[0] + 4, heading[0] + 4)
	assert vbuf.update(handle, Stream(replace(1, document("Changed."))))
	assert textOf(vbuf, handle) == "Changed.HeadingLast."
	assert caretOf(vbuf, handle) == len("Changed.") + 4


def test_reuseKeepsTheField(vbuf: ModuleType, buffers):
	handle = buffers(document())
	heading = nodeOf(vbuf, handle, 3)
	last = nodeOf(vbuf, handle, 4)
	batch = replace(
		1,
		control(
			1,
			control(9, text("Inserted. "), block=True),
			control(2, text("New first."), block=True),
			reuse(3),
			reuse(4),
			block=True,
		),
	)
	assert vbuf.update(handle, Stream(batch))
	assert textOf(vbuf, handle) == "Inserted. New first.HeadingLast."
	assert nodeOf(vbuf, handle, 3) == heading
	assert nodeOf(vbuf, handle, 4) == last
	start = len("Inserted. New first.")
	assert offsetsOf(vbuf, handle, heading) == (start, start + len("Heading"))
	docHandle, ID = ctypes.c_int(), ctypes.c_int()
	assert vbuf.lib().VBuf_getIdentifierFromControlFieldNode(
		handle, heading, ctypes.byref(docHandle), ctypes.byref(ID)
	)
	assert (docHandle.value, ID.value) == (1, 3)
	# A reused field that is gone from the page later is replaced like any other.
	assert vbuf.update(handle, Stream(replace(1, control(1, reuse(3), block=True))))
	assert textOf(vbuf, handle) == "Heading"
	assert offsetsOf(vbuf, handle, last) is None


@pytest.mark.parametrize(
	"batch",
	[
		"X",
		"S1,2;",
		"S1,2;C1,2,1,0;T5:abc",
		"S1,2;T1:x0;",
		"S1,2;C1,2,1,0;T1:x0;ES",
		"S1,2;C1,2,1,0;T1:x0;EC1,5,0,0;E",
		replace(2, control(2)) + replace(1, control(1)),
		replace(2, control(2)) + replace(2, control(2)),
		replace(2, control(2, reuse(3))),
		replace(1, control(1, reuse(3), reuse(3))),
		replace(1, control(1, reuse(1))),
		"S1,1;R1,3;",
		replace(1, control(1, reuse(77))),
		"S1,2;C1,2,1,-1;E",
		"S1,2;C1,2,1,0;T-1:E",
		replace(2, control(2, text("x"), control(1, text("y")))),
		replace(3, control(1, text("y"))),
	],
)
def test_badBatchChangesNothing(vbuf: ModuleType, buffers, batch: str):
	handle = buffers(document())
	vbuf.lib().VBuf_setSelectionOffsets(handle, 20, 20)
	before = textOf(vbuf, handle), caretOf(vbuf, handle), [nodeOf(vbuf, handle, ID) for ID in (1, 2, 3, 4)]
	assert not vbuf.update(handle, Stream(batch))
	assert (
		textOf(vbuf, handle),
		caretOf(vbuf, handle),
		[nodeOf(vbuf, handle, ID) for ID in (1, 2, 3, 4)],
	) == before


def test_batchForAGoneFieldAppliesTheRest(vbuf: ModuleType, buffers):
	handle = buffers(document())
	batch = replace(99, control(99, text("Gone."), reuse(3))) + replace(
		2, control(2, text("Kept."), block=True)
	)
	assert not vbuf.update(handle, Stream(batch))
	assert textOf(vbuf, handle) == "Kept.HeadingLast."


def test_buildRefusesMalformedAndUpdateOps(vbuf: ModuleType):
	for stream in (document()[:-1], replace(1, document()), control(1, reuse(2)), "", control(1) + "X"):
		handle = vbuf.create()
		try:
			with pytest.raises(ValueError, match="malformed"):
				vbuf.build(handle, Stream(stream))
			assert vbuf.lib().VBuf_getTextLength(handle) == 0
		finally:
			vbuf.destroy(handle)


#: Fields nested deeper than vbufLinux.cpp allows, and deep enough that storage.cpp, which recurses
#: once a level, would overflow the stack reading them.
TOO_DEEP = 200_000
#: As deep as vbufLinux.cpp allows.
MAX_NESTING = 4096


def nested(depth: int, firstID: int = 1) -> str:
	"""Fields nested depth deep, with text at the bottom; built without recursing."""
	return "".join(f"C1,{ID},0,0;" for ID in range(firstID, firstID + depth)) + text("Bottom.") + "E" * depth


def runScript(tmp_path: Path, library: Path, body: str) -> dict:
	"""Run body in a process of its own, where vbuf is the binding, the stream writers are defined and
	result is printed at the end, so that a crash or a hang fails the test rather than ending it.

	:return: result.
	"""
	script = tmp_path / "script.py"
	writers = (units, string, attributes, control, text, replace, reuse, nested, Stream, loadBinding)
	script.write_text(
		"\n\n".join(inspect.getsource(f) for f in writers)
		+ "\n\nimport ctypes, importlib.util, json, sys\n"
		+ f"vbuf = loadBinding({str(BINDING)!r}, sys.argv[1])\n"
		+ "result = {}\n"
		+ textwrap.dedent(body)
		+ "\nprint(json.dumps(result))\n",
		encoding="utf-8",
	)
	done = subprocess.run(
		[sys.executable, "-I", str(script), str(library)],
		capture_output=True,
		text=True,
		timeout=60,
		check=False,
	)
	assert done.returncode == 0, f"{harness.describeExit(done.returncode)}:\n{done.stderr[-4000:]}"
	return json.loads(done.stdout)


def test_nestingTooDeepForTheStackIsRefused(tmp_path: Path, library: Path):
	result = runScript(
		tmp_path,
		library,
		f"""
for name, depth in (("tooDeep", {TOO_DEEP}), ("deepest", {MAX_NESTING}), ("deeper", {MAX_NESTING + 1})):
	handle = vbuf.create()
	try:
		vbuf.build(handle, Stream(nested(depth)))
	except ValueError:
		result[name] = None
	else:
		length = vbuf.lib().VBuf_getTextLength(handle)
		result[name] = len(vbuf.VBuf_getTextInRange(handle, 0, length, True))
	if name == "deepest":
		# A replacement deep in the buffer may not nest past the limit either.
		result["shallowUpdate"] = vbuf.update(handle, Stream(replace({MAX_NESTING}, nested(1, {MAX_NESTING}))))
		result["deepUpdate"] = vbuf.update(handle, Stream(replace({MAX_NESTING}, nested(2, {MAX_NESTING}))))
	vbuf.destroy(handle)
""",
	)
	assert result["tooDeep"] is None
	assert result["deeper"] is None
	assert result["deepest"] > MAX_NESTING
	assert result["shallowUpdate"] is True
	assert result["deepUpdate"] is False


def test_aFieldWithTheRootsIdentifierLeavesLaterUpdatesWhole(tmp_path: Path, library: Path):
	# Each batch carrying the root's identifier below the root once broke the storage's identifier
	# map: the next replacement of the root never returned, and replacing the fields that carried it
	# freed memory that was never allocated.
	result = runScript(
		tmp_path,
		library,
		f"""
		def textOf(handle):
			return vbuf.VBuf_getTextInRange(handle, 0, vbuf.lib().VBuf_getTextLength(handle), False)
		page = {control(1, control(2, text("a")), control(3, text("b")))!r}
		handle = vbuf.create()
		vbuf.build(handle, Stream(page))
		result["clash"] = vbuf.update(handle, Stream({replace(2, control(2, text("a"), control(1, text("x"))))!r}))
		result["root"] = vbuf.update(handle, Stream({replace(1, control(1, control(2, text("c"))))!r}))
		result["afterRoot"] = textOf(handle)
		vbuf.destroy(handle)
		handle = vbuf.create()
		vbuf.build(handle, Stream(page))
		for ID, value in ((2, "x"), (3, "y")):
			vbuf.update(handle, Stream(replace(ID, control(ID, control(1, text(value))))))
		result["fields"] = [vbuf.update(handle, Stream(replace(ID, control(ID, text(value))))) for ID, value in ((2, "c"), (3, "d"))]
		result["afterFields"] = textOf(handle)
		vbuf.destroy(handle)
		""",
	)
	assert result == {
		"clash": False,
		"root": True,
		"afterRoot": "c",
		"fields": [True, True],
		"afterFields": "cd",
	}


def test_badHandlesGetZero(vbuf: ModuleType, buffers):
	lib = vbuf.lib()
	handle = buffers(document())
	other = buffers(document("Other."))
	start, end, docHandle, ID = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
	node = ctypes.c_uint64()
	foreign = nodeOf(vbuf, other, 3)
	assert not lib.VBuf_getFieldNodeOffsets(handle, foreign, ctypes.byref(start), ctypes.byref(end))
	assert not lib.VBuf_isFieldNodeAtOffset(handle, foreign, 0)
	assert not lib.VBuf_getIdentifierFromControlFieldNode(
		handle, foreign, ctypes.byref(docHandle), ctypes.byref(ID)
	)
	assert not lib.VBuf_getFieldNodeOffsets(handle, 0x12345678, ctypes.byref(start), ctypes.byref(end))
	# A text field is no control field, though its handle is good.
	assert lib.VBuf_locateTextFieldNodeAtOffset(
		handle, 0, ctypes.byref(start), ctypes.byref(end), ctypes.byref(node)
	)
	assert not lib.VBuf_getIdentifierFromControlFieldNode(
		handle, node, ctypes.byref(docHandle), ctypes.byref(ID)
	)
	assert lineAt(vbuf, handle, -1) is None
	assert not lib.VBuf_locateTextFieldNodeAtOffset(
		handle, -1, ctypes.byref(start), ctypes.byref(end), ctypes.byref(node)
	)
	assert vbuf.VBuf_getTextInRange(handle, -1, 3, False) is None
	assert find(vbuf, handle, -2, 0, {"role": ["40"]}) is None
	gone = vbuf.create()
	vbuf.build(gone, Stream(document()))
	vbuf.destroy(gone)
	assert lib.VBuf_getTextLength(gone) == 0
	assert vbuf.VBuf_getTextInRange(gone, 0, 3, False) is None
	assert lineAt(vbuf, gone, 0) is None
	assert not lib.VBuf_getControlFieldNodeWithIdentifier(gone, 1, 3, ctypes.byref(node))
	assert not node.value
	assert not vbuf.update(gone, Stream(replace(2, control(2))))
	with pytest.raises(ValueError, match="malformed"):
		vbuf.build(gone, Stream(document()))
	vbuf.destroy(gone)
	assert lib.VBuf_getTextLength(None) == 0
	assert textOf(vbuf, handle) == "First paragraph.HeadingLast."


def runWorkload(tmp_path: Path, library: Path, iterations: int, env: dict | None = None) -> dict:
	script = tmp_path / "workload.py"
	script.write_text(workloadScript(iterations), encoding="utf-8")
	done = subprocess.run(
		[sys.executable, "-I", str(script), str(library)],
		capture_output=True,
		text=True,
		timeout=600,
		env={**os.environ, **(env or {})},
		check=False,
	)
	assert done.returncode == 0, (
		f"the workload ended with {harness.describeExit(done.returncode)}:\n{done.stderr[-6000:]}"
	)
	assert "Sanitizer" not in done.stderr and "runtime error" not in done.stderr, done.stderr[-6000:]
	counts = json.loads(done.stdout.strip().splitlines()[-1])
	assert counts["refused"] == 10
	assert counts["bad"] == 4
	assert counts["applied"] == iterations
	assert counts["reads"] >= iterations
	return counts


def test_readerAndUpdaterConcurrently(tmp_path: Path, library: Path):
	runWorkload(tmp_path, library, 10_000)


def test_sanitizerBuild(tmp_path: Path):
	library = buildLibrary(tmp_path / "libnvdaVbuf-asan.so", "asan")
	runtime = subprocess.run(
		["g++", "-print-file-name=libasan.so"], capture_output=True, text=True, check=True
	)
	preload = runtime.stdout.strip()
	assert os.path.isabs(preload), f"g++ does not know where libasan is: {preload!r}"
	runWorkload(
		tmp_path,
		library,
		2_000,
		env={
			"LD_PRELOAD": preload,
			# CPython frees little at exit by design; its leaks are not ours to report.
			"ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:halt_on_error=1",
			"UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1",
		},
	)


def test_updateThreadAppliesThenTellsTheBuffer(tmp_path: Path, library: Path):
	update = replace(2, control(2, text("Updated."), block=True))
	done = harness.runChild(
		tmp_path,
		f"""
		import ctypes
		import queue
		import threading
		import time
		import queueHandler
		from linuxPlatform import vbufEngine, vbufLib, vbufUpdates

		class FakeStream:
			def __init__(self, value):
				data = value.encode("utf-16-le", "surrogatepass")
				self.array = (ctypes.c_uint16 * (len(data) // 2)).from_buffer_copy(data)
				self.pointer = ctypes.addressof(self.array)
				self.length = len(data) // 2
				self.freed = False

			def free(self):
				self.freed = True

		class Batch:
			def __init__(self, stream, busName, docPath, events, info):
				self.stream = stream
				self.busName = busName
				self.docPath = docPath
				self.events = events
				self.info = info

		def textOf(handle):
			length = vbufLib.lib().VBuf_getTextLength(handle)
			return vbufLib.VBuf_getTextInRange(handle, 0, length, False)

		class FakeBuffer:
			busName = ":1.5"
			docPath = "/org/a11y/atspi/accessible/7"

			def __init__(self):
				self.VBufHandle = ctypes.c_void_p(vbufLib.create())
				vbufLib.build(self.VBufHandle.value, FakeStream({document()!r}))
				self.seen = []

			def onEngineEvents(self, events, info):
				self.seen.append({{
					"mainThread": threading.current_thread() is threading.main_thread(),
					"events": list(events),
					"info": info,
					"text": textOf(self.VBufHandle),
				}})

		result = {{}}
		vars(vbufEngine).pop("next", None)
		buffer = FakeBuffer()
		vbufUpdates.register(buffer)
		result["withoutNext"] = vbufUpdates._thread is None
		vbufUpdates.unregister(buffer)

		streams = [FakeStream({update!r}), FakeStream({update!r})]
		batches = [
			Batch(streams[0], ":1.5", "/org/a11y/atspi/accessible/7", ("complete",), {{"complete": True}}),
			Batch(None, ":1.5", "/org/a11y/atspi/accessible/7", ("loadComplete",), {{}}),
			Batch(streams[1], ":1.9", "/org/a11y/atspi/accessible/7", ("gone",), {{}}),
		]
		waits = []
		shutdowns = []

		def fakeNext(timeoutMs):
			waits.append(timeoutMs)
			if batches:
				return batches.pop(0)
			time.sleep(0.01)
			return None

		vbufEngine.next = fakeNext
		vbufEngine.shutdown = lambda: shutdowns.append(True)
		vbufUpdates.register(buffer)
		deadline = time.monotonic() + 10
		while len(buffer.seen) < 2 and time.monotonic() < deadline:
			try:
				function, args, kwargs = queueHandler.eventQueue.get(timeout=0.1)
			except queue.Empty:
				continue
			function(*args, **kwargs)
		while batches and time.monotonic() < deadline:
			time.sleep(0.01)
		time.sleep(0.1)
		vbufUpdates.terminate()
		result.update(
			seen=buffer.seen,
			leftOver=queueHandler.eventQueue.qsize(),
			freed=[stream.freed for stream in streams],
			waits=sorted(set(waits)),
			shutdowns=len(shutdowns),
			stopped=vbufUpdates._thread is None,
		)
		vbufUpdates.unregister(buffer)
		vbufLib.destroy(buffer.VBufHandle.value)
		""",
		env={"LINDA_VBUF_LIB": str(library)},
	)
	done.assertClean()
	value = done.value
	assert value["withoutNext"]
	assert value["seen"] == [
		{
			"mainThread": True,
			"events": ["complete"],
			"info": {"complete": True},
			"text": "Updated.HeadingLast.",
		},
		{"mainThread": True, "events": ["loadComplete"], "info": {}, "text": "Updated.HeadingLast."},
	]
	# The batch for a document no buffer holds is kept for one that may yet register, and its stream
	# freed once the thread stops.
	assert value["leftOver"] == 0
	assert value["freed"] == [True, True]
	assert value["waits"] == [500]
	assert value["shutdowns"] == 1
	assert value["stopped"]


#: Paragraphs in the large page whose rest replaces the whole document.
LARGE_PARAGRAPHS = 20_000


def largeDocument(paragraphs: int, words: str) -> str:
	return control(
		1,
		*(control(i + 2, text(f"Paragraph {i} {words}. "), block=True) for i in range(paragraphs)),
		block=True,
	)


def test_theRestOfALargePageIsBuiltOffTheMainThread(tmp_path: Path, library: Path):
	top = largeDocument(3, "of the top")
	rest = replace(1, largeDocument(LARGE_PARAGRAPHS, "of the whole page"))
	caretAt = len("Paragraph 0 of the top. Paragraph 1 of ")
	done = harness.runChild(
		tmp_path,
		f"""
		import ctypes
		import queue
		import threading
		import time
		import queueHandler
		from linuxPlatform import vbufEngine, vbufLib, vbufUpdates

		class FakeStream:
			def __init__(self, value):
				data = value.encode("utf-16-le", "surrogatepass")
				self.array = (ctypes.c_uint16 * (len(data) // 2)).from_buffer_copy(data)
				self.pointer = ctypes.addressof(self.array)
				self.length = len(data) // 2

			def free(self):
				pass

		class Batch:
			busName = ":1.5"
			docPath = "/org/a11y/atspi/accessible/7"
			events = ("complete",)
			info = {{"complete": True}}

			def __init__(self, stream):
				self.stream = stream

		calls = []
		lib = vbufLib.lib()
		for name in ("VBufLinux_update", "VBufLinux_buildReplacement", "VBufLinux_adopt", "VBufLinux_destroy"):
			def record(*args, _name=name, _function=getattr(lib, name)):
				calls.append((_name, threading.current_thread() is threading.main_thread()))
				return _function(*args)
			setattr(lib, name, record)

		def state(handle):
			start, end = ctypes.c_int(), ctypes.c_int()
			lib.VBuf_getSelectionOffsets(handle, ctypes.byref(start), ctypes.byref(end))
			length = lib.VBuf_getTextLength(handle)
			return vbufLib.VBuf_getTextInRange(handle, 0, length, False), start.value

		class FakeBuffer:
			busName = Batch.busName
			docPath = Batch.docPath

			def __init__(self):
				self.VBufHandle = ctypes.c_void_p(vbufLib.create())
				vbufLib.build(self.VBufHandle.value, FakeStream({top!r}))
				lib.VBuf_setSelectionOffsets(self.VBufHandle, {caretAt}, {caretAt})
				self.done = False

			def onEngineEvents(self, events, info):
				self.done = True

		# The same batch applied on this thread, as it was before, for what the buffer should hold.
		reference = FakeBuffer()
		vbufLib.update(reference.VBufHandle.value, FakeStream({rest!r}))
		expected = state(reference.VBufHandle)
		vbufLib.destroy(reference.VBufHandle.value)

		buffer = FakeBuffer()
		batches = [Batch(FakeStream({rest!r}))]

		def fakeNext(timeoutMs):
			if batches:
				return batches.pop(0)
			time.sleep(0.01)
			return None

		vbufEngine.next = fakeNext
		vbufEngine.shutdown = lambda: None
		del calls[:]
		vbufUpdates.register(buffer)
		deadline = time.monotonic() + 30
		while not buffer.done and time.monotonic() < deadline:
			try:
				function, args, kwargs = queueHandler.eventQueue.get(timeout=0.1)
			except queue.Empty:
				continue
			function(*args, **kwargs)
		# The old fields are freed on the update thread once it next wakes.
		while not any(name == "VBufLinux_destroy" for name, _main in calls) and time.monotonic() < deadline:
			time.sleep(0.05)
		vbufUpdates.terminate()
		vbufUpdates.unregister(buffer)
		result = {{"calls": calls[:], "state": state(buffer.VBufHandle), "expected": expected}}
		vbufLib.destroy(buffer.VBufHandle.value)
		""",
		env={"LINDA_VBUF_LIB": str(library)},
	)
	done.assertClean()
	value = done.value
	# Built on the update thread, swapped in on the main thread, and the old fields freed on the update
	# thread: the main thread neither builds nor frees a document.
	assert value["calls"] == [
		["VBufLinux_buildReplacement", False],
		["VBufLinux_adopt", True],
		["VBufLinux_destroy", False],
	]
	assert value["state"] == value["expected"]
	text, caret = value["state"]
	assert text.startswith("Paragraph 0 of the whole page. Paragraph 1 of the whole page.")
	assert len(text) > LARGE_PARAGRAPHS * 20
	# The caret stays as far into its paragraph as it was.
	assert caret == len("Paragraph 0 of the whole page. Paragraph 1 of ")
