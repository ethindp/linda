"""One import-surface check, run in a fresh interpreter; test_importSurface.py starts it.

usage: importSurfaceProbe.py RESULT_JSON core|sweep|hazard NAME|refuse|noLouis|refuseWhen CONDITION [ENTRY]

It writes its findings to RESULT_JSON and exits 0 whatever they are; deciding pass or fail is the
test's job. The findings include what harness.Checks recorded in this process, so that the test can
hold the run to the harness's definition of a clean run.
"""

import ast
import builtins
import importlib
import importlib.util
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE = os.path.join(ROOT, "source")
#: Where prepare puts the configuration directory, removed at the end.
workDir: str | None = None
#: The standard library changes behaviour when any of these imports.
neverImportable = frozenset({"winreg", "msvcrt", "_winapi"})

#: Modules the sweep never imports, because importing them does something other than define them.
notImported = {
	"setup": "py2exe build script: importing it starts a build",
}


def _loadHarness() -> types.ModuleType:
	# By its path, as this runs with -I and tests/linux stays off sys.path.
	spec = importlib.util.spec_from_file_location(
		"harness", os.path.join(os.path.dirname(os.path.abspath(__file__)), "harness.py")
	)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


harness = _loadHarness()
#: What harness.Checks records in this process, as it happens.
events: list[dict] = []


def _record(kind: str, **fields: object) -> None:
	events.append({"kind": kind, "pid": os.getpid(), **fields})


def auditedSince(start: int) -> list[str]:
	"""What each NoLinuxImplementation made since events held start entries names, raised or not."""
	return [event["what"] for event in events[start:] if event["kind"] == "noLinuxImplementation"]


def writeViolations() -> list[str]:
	"""Each write under source/ or tests/unit this process tried, which harness.Checks refused."""
	return [f"{event['event']} {event['path']}" for event in events if event["kind"] == "write"]


def prepare() -> None:
	"""bootstrap.install() and the preconditions nvda.pyw and core.main establish before imports."""
	global workDir
	workDir = tempfile.mkdtemp(prefix="nvda-probe-")
	harness.prepareChild("config", Path(workDir))


def coreMainImports() -> list[dict]:
	"""Every import statement in core.main(), in source order, with the exceptions core.main
	itself catches around it."""
	with open(os.path.join(SOURCE, "core.py"), encoding="utf-8") as f:
		tree = ast.parse(f.read())
	main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
	steps = []

	def visit(node: ast.AST, handled: tuple[str, ...]) -> None:
		for child in ast.iter_child_nodes(node):
			if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
				continue
			if isinstance(child, ast.Import):
				for alias in child.names:
					steps.append(
						{"line": child.lineno, "module": alias.name, "name": None, "handled": handled}
					)
			elif isinstance(child, ast.ImportFrom):
				for alias in child.names:
					steps.append(
						{
							"line": child.lineno,
							"module": child.module,
							"name": alias.name,
							"handled": handled,
						},
					)
			if isinstance(child, ast.Try):
				caught = []
				for handler in child.handlers:
					types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
					caught.extend(t.id for t in types if isinstance(t, ast.Name))
				for stmt in child.body:
					visit(ast.Module(body=[stmt], type_ignores=[]), handled + tuple(caught))
				for part in (child.handlers, child.orelse, child.finalbody):
					for stmt in part:
						visit(ast.Module(body=[stmt], type_ignores=[]), handled)
			else:
				visit(child, handled)

	visit(main, ())
	return steps


_finderFile = os.path.join(SOURCE, "linuxPlatform", "finder.py")


def _failure(e: BaseException) -> dict:
	frames = traceback.extract_tb(e.__traceback__)
	# Where the failing import is written, not the finder that refused it.
	inSource = [f for f in frames if f.filename.startswith(SOURCE) and f.filename != _finderFile]
	where = inSource[-1] if inSource else (frames[-1] if frames else None)
	return {
		"type": f"{type(e).__module__}.{type(e).__qualname__}",
		"message": str(e)[:500],
		"where": f"{os.path.relpath(where.filename, ROOT)}:{where.lineno}" if where else None,
		"traceback": "".join(traceback.format_exception(e))[-3000:],
	}


def _doImport(module: str, name: str | None) -> None:
	imported = __import__(module, fromlist=[name] if name else [])
	if name and not hasattr(imported, name):
		raise ImportError(f"cannot import name {name!r} from {module!r}")


def runCoreMainImports() -> list[dict]:
	results = []
	for step in [{"line": 0, "module": "core", "name": None, "handled": ()}, *coreMainImports()]:
		label = f"from {step['module']} import {step['name']}" if step["name"] else f"import {step['module']}"
		result = {"step": label, "line": step["line"]}
		try:
			_doImport(step["module"], step["name"])
			result["status"] = "ok"
		except Exception as e:  # noqa: BLE001
			# A catch-all handler would hide any failure, so only a named exception counts.
			handledBy = [
				n
				for n in step["handled"]
				if n not in ("Exception", "BaseException") and isinstance(e, getattr(builtins, n, ()))
			]
			if handledBy:
				result["status"] = "handled"
				result["handledBy"] = handledBy
				result["message"] = str(e)
			else:
				result["status"] = "fail"
				result.update(_failure(e))
		results.append(result)
	return results


def allSourceModules() -> list[str]:
	modules = []
	for dirPath, dirNames, fileNames in os.walk(SOURCE):
		dirNames[:] = sorted(d for d in dirNames if d != "__pycache__")
		rel = os.path.relpath(dirPath, SOURCE)
		parts = [] if rel == "." else rel.split(os.sep)
		# Their files are imported under the names they replace or stand in for.
		if parts[:2] in (["linuxPlatform", "replacements"], ["linuxPlatform", "standIns"]):
			dirNames[:] = []
			continue
		for fileName in sorted(fileNames):
			if not fileName.endswith(".py"):
				continue
			stem = fileName[:-3]
			modules.append(".".join(parts if stem == "__init__" else [*parts, stem]))
	return sorted(m for m in modules if m)


def _importInChild(module: str, timeout: int) -> dict:
	readFd, writeFd = os.pipe()
	pid = os.fork()
	if pid == 0:
		os.close(readFd)
		signal.alarm(timeout)
		sys.stdout = sys.stderr = io.StringIO()
		del events[:]
		try:
			imported = importlib.import_module(module)
			# The file actually loaded, which for a replaced module is not upstream's.
			result = {"status": "ok", "file": getattr(imported, "__file__", None)}
		except BaseException as e:  # noqa: BLE001
			result = {"status": "fail", **_failure(e)}
		result["writeViolations"] = writeViolations()
		result["windowsModulesLoaded"] = sorted(neverImportable & set(sys.modules))
		with os.fdopen(writeFd, "w") as pipe:
			json.dump(result, pipe)
		os._exit(0)
	os.close(writeFd)
	with os.fdopen(readFd) as pipe:
		data = pipe.read()
	_, status = os.waitpid(pid, 0)
	if data:
		return json.loads(data)
	if os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGALRM:
		return {"status": "fail", "type": "timeout", "message": f"import took more than {timeout} s"}
	return {"status": "fail", "type": "crash", "message": f"child ended with wait status {status}"}


def runSweep() -> dict:
	import threading

	if threading.active_count() != 1:
		raise RuntimeError(f"cannot fork a clean child: {threading.active_count()} threads are running")
	results = {}
	windowsModulesLoaded = {}
	inChildren: list[str] = []
	for module in allSourceModules():
		if module in notImported:
			results[module] = {"status": "notImported", "reason": notImported[module]}
			continue
		result = results[module] = _importInChild(module, timeout=60)
		inChildren.extend(result.pop("writeViolations", ()))
		if loaded := result.pop("windowsModulesLoaded", None):
			windowsModulesLoaded[module] = loaded
	return {
		"results": results,
		"windowsModulesLoaded": windowsModulesLoaded,
		"writeViolationsInChildren": inChildren,
	}


def _outcome(call: object) -> dict:
	try:
		value = call()
	except BaseException as e:  # noqa: BLE001
		return {
			"raised": type(e).__name__,
			"isNotImplementedError": isinstance(e, NotImplementedError),
			"message": str(e),
		}
	return {"raised": None, "returned": repr(value)}


def _callWithNones(func: object) -> object:
	"""Call func with None for each parameter it requires, as a caller that got that far would."""
	import inspect

	try:
		parameters = inspect.signature(func).parameters.values()
	except (TypeError, ValueError):
		return func()
	required = [
		p
		for p in parameters
		if p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
	]
	return func(
		*(None for p in required if p.kind is not p.KEYWORD_ONLY),
		**{p.name: None for p in required if p.kind is p.KEYWORD_ONLY},
	)


def _standInSurface() -> dict:
	"""What every public name each stand-in module defines does when used: called for a function,
	constructed for a class, and every public method or property of a class that constructs."""
	import ctypes
	import enum
	import inspect

	from comtypes import IUnknown
	from linuxPlatform import finder

	def category(cls: type) -> str:
		if issubclass(cls, BaseException):
			return "exception"
		if issubclass(cls, enum.Enum):
			return "enum"
		if issubclass(cls, IUnknown):
			return "interface"
		if issubclass(cls, (ctypes._SimpleCData, ctypes.Structure, ctypes.Union, ctypes._Pointer)):
			return "ctypesType"
		return "class"

	surface: dict[str, dict] = {}
	for moduleName in finder.standInModules:
		module = importlib.import_module(moduleName)
		for attr in sorted(vars(module)):
			if attr.startswith("_"):
				continue
			obj = getattr(module, attr)
			label = f"{moduleName}.{attr}"
			if inspect.ismodule(obj):
				continue
			if not (inspect.isclass(obj) or callable(obj)):
				surface[label] = {"kind": "value", "type": type(obj).__name__}
				continue
			if getattr(obj, "__module__", None) != moduleName:
				# Imported, from another stand-in or from ctypes, and covered where it is defined.
				continue
			if not inspect.isclass(obj):
				surface[label] = {"kind": "function", **_outcome(lambda obj=obj: _callWithNones(obj))}
				continue
			made: list[object] = []
			outcome = _outcome(lambda obj=obj, made=made: made.append(_callWithNones(obj)))
			surface[label] = {"kind": "class", "category": category(obj), **outcome}
			for memberName, member in vars(obj).items():
				if memberName.startswith("_"):
					continue
				memberLabel = f"{label}.{memberName}"
				if isinstance(member, property):
					if made:
						surface[memberLabel] = {
							"kind": "property",
							**_outcome(lambda name=memberName, made=made: getattr(made[0], name)),
						}
				elif isinstance(member, (staticmethod, classmethod)):
					bound = getattr(obj, memberName)
					surface[memberLabel] = {"kind": "method", **_outcome(lambda f=bound: _callWithNones(f))}
				elif inspect.isfunction(member) and made:
					bound = getattr(made[0], memberName)
					surface[memberLabel] = {"kind": "method", **_outcome(lambda f=bound: _callWithNones(f))}
	return surface


def runHazard(name: str) -> dict:
	import ctypes

	if name == "windowsModulesAbsent":
		found = {}
		for module in sorted(neverImportable):
			try:
				findable = importlib.util.find_spec(module) is not None
			except ModuleNotFoundError:
				findable = False
			found[module] = {"inSysModules": module in sys.modules, "findable": findable}
		return {"found": found}
	if name == "shadowsRefused":
		# A file named after a module Linux never provides, anywhere on sys.path, must not import,
		# and nor may a file added to a replaced package's search path.
		shadowDir = tempfile.mkdtemp()
		for fileName in ("winreg.py", "msvcrt.py", "injected.py"):
			with open(os.path.join(shadowDir, fileName), "w", encoding="utf-8") as f:
				f.write("")
		sys.path.insert(0, shadowDir)
		import comtypes.gen

		import NVDAHelper

		paths = {"NVDAHelper": list(NVDAHelper.__path__), "comtypes.gen": list(comtypes.gen.__path__)}
		comtypes.gen.__path__.append(shadowDir)
		outcomes = {
			module: _outcome(lambda module=module: importlib.import_module(module))
			for module in ("winreg", "msvcrt", "comtypes.gen.injected", "NVDAHelper.localWin10")
		}
		return {"paths": paths, "outcomes": outcomes}
	if name == "mimetypes":
		import mimetypes

		return {"guess": mimetypes.guess_type("x.html")[0]}
	if name == "subprocess":
		import subprocess

		done = subprocess.run(
			[sys.executable, "-I", "-c", "print('subprocess ran')"],
			capture_output=True,
			text=True,
			check=False,
		)
		return {"returncode": done.returncode, "stdout": done.stdout.strip()}
	if name == "louisLoader":
		import louis

		return {
			"loaderIsCdll": louis._loader is ctypes.cdll,
			"functypeIsCfunctype": louis._functype is ctypes.CFUNCTYPE,
			"libraryType": type(louis.liblouis).__name__,
			"version": louis.version(),
		}
	if name == "standInCalls":
		import winsound

		import comtypes
		import comtypes.client
		from pycaw.utils import AudioUtilities

		start = len(events)
		calls = {
			"ctypes.windll.kernel32.GetTickCount()": lambda: ctypes.windll.kernel32.GetTickCount(),
			"ctypes.oledll.ole32.CoInitialize(None)": lambda: ctypes.oledll.ole32.CoInitialize(None),
			"ctypes.WinDLL('user32').MessageBeep(0)": lambda: ctypes.WinDLL("user32").MessageBeep(0),
			"WINFUNCTYPE prototype bound to user32.GetForegroundWindow": lambda: ctypes.WINFUNCTYPE(
				ctypes.c_void_p
			)(
				("GetForegroundWindow", ctypes.windll.user32),
			)(),
			"ctypes.GetLastError()": lambda: ctypes.GetLastError(),
			"ctypes.FormatError(5)": lambda: ctypes.FormatError(5),
			"ctypes.WinError()": lambda: ctypes.WinError(),
			"winsound.MessageBeep()": lambda: winsound.MessageBeep(),
			"comtypes.CoCreateInstance(GUID())": lambda: comtypes.CoCreateInstance(comtypes.GUID()),
			"comtypes.client.CreateObject('SAPI.SpVoice')": lambda: comtypes.client.CreateObject(
				"SAPI.SpVoice"
			),
			"comtypes.COMObject()": lambda: comtypes.COMObject(),
			"pycaw AudioUtilities.GetAllSessions()": lambda: AudioUtilities.GetAllSessions(),
			"WINFUNCTYPE COM method prototype": lambda: ctypes.WINFUNCTYPE(ctypes.HRESULT)(3, "Release")(),
		}
		outcomes = {label: _outcome(call) for label, call in calls.items()}
		callback = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)(lambda x: x * 2)
		found = {
			"outcomes": outcomes,
			"callbackIsCFuncPtr": isinstance(callback, ctypes._CFuncPtr),
			"callbackResult": callback(21),
			"truthiness": {
				"ctypes.windll.user32": bool(ctypes.windll.user32),
				"ctypes.WinDLL('user32')": bool(ctypes.WinDLL("user32")),
				"ctypes.oledll.ole32": bool(ctypes.oledll.ole32),
			},
			"privateAttribute": _outcome(lambda: ctypes.windll.user32._funcptr),
		}
		return {**found, "audited": auditedSince(start)}
	if name == "standInSurface":
		return _standInSurface()
	if name == "replacedNames":
		import IAccessibleHandler
		import UIAHandler

		start = len(events)

		def fromImport() -> None:
			from UIAHandler import getUIAUnitFromNVDAUnit  # noqa: F401

		outcomes = {
			"IAccessibleHandler.NVDARoleFromAttr": _outcome(lambda: IAccessibleHandler.NVDARoleFromAttr),
			"UIAHandler.UIA_NativeWindowHandlePropertyId": _outcome(
				lambda: UIAHandler.UIA_NativeWindowHandlePropertyId,
			),
			"from UIAHandler import getUIAUnitFromNVDAUnit": _outcome(fromImport),
			"UIAHandler.__wrapped__": _outcome(lambda: UIAHandler.__wrapped__),
			"from IAccessibleHandler import *": _outcome(
				lambda: __import__("IAccessibleHandler", fromlist=["*"]),
			),
			"IAccessibleHandler.RelationType": _outcome(lambda: IAccessibleHandler.RelationType.__name__),
			"UIAHandler.handler": _outcome(lambda: UIAHandler.handler),
			"hasattr(UIAHandler, 'UIA')": _outcome(lambda: hasattr(UIAHandler, "UIA")),
			"getattr(IAccessibleHandler, 'IA2', None)": _outcome(
				lambda: getattr(IAccessibleHandler, "IA2", None),
			),
		}
		try:
			IAccessibleHandler.accFocus  # noqa: B018
		except BaseException as e:  # noqa: BLE001
			caughtAs = [
				base.__name__ for base in (AttributeError, NotImplementedError) if isinstance(e, base)
			]
		return {"outcomes": outcomes, "audited": auditedSince(start), "caughtAs": caughtAs}
	if name == "addonAppModules":
		# App modules outside NVDA's own, as add-ons install them, that use a name the replacements lack
		# when they are imported.
		import appModuleHandler
		import appModules
		from linuxPlatform import processes

		addonDir = tempfile.mkdtemp(prefix="nvda-addon-")
		sources = {
			"addonfromimport": "from UIAHandler import UIA\n",
			"addonattribute": "import IAccessibleHandler\nIA2 = IAccessibleHandler.IA2\n",
		}
		for moduleName, text in sources.items():
			with open(os.path.join(addonDir, f"{moduleName}.py"), "w", encoding="utf-8") as f:
				f.write(
					f"import appModuleHandler\n{text}\nclass AppModule(appModuleHandler.AppModule):\n\tpass\n"
				)
		appModules.__path__.append(addonDir)
		outcomes = {}
		try:
			for moduleName in sources:
				processes.getAppName = lambda processID, moduleName=moduleName: moduleName
				outcomes[moduleName] = _outcome(
					lambda: appModuleHandler.getAppNameFromProcessID(os.getpid() + 1)
				)
		finally:
			shutil.rmtree(addonDir, ignore_errors=True)
		return outcomes
	if name == "uiaGuards":
		# Code that on Windows asks whether a TextInfo or tree interceptor is a UI Automation or
		# Word one, which Linux never makes.
		import types

		import controlTypes
		import textInfos
		from browseMode import BrowseModeDocumentTreeInterceptor
		from documentNavigation import paragraphHelper
		from textInfos.offsets import OffsetsTextInfo

		class Document:
			text = "first\nsecond\n\nthird\n"
			caret = 0

		class DocumentText(OffsetsTextInfo):
			encoding = None

			def _getStoryText(self) -> str:
				return self.obj.text

			def _getStoryLength(self) -> int:
				return len(self.obj.text)

			def _getCaretOffset(self) -> int:
				return self.obj.caret

			def _setCaretOffset(self, offset: int) -> None:
				self.obj.caret = offset

		paragraphs = {}
		for move in ("moveToSingleLineBreakParagraph", "moveToMultiLineBreakParagraph"):
			document = Document()
			paragraphs[move] = {
				**_outcome(
					lambda move=move, document=document: getattr(paragraphHelper, move)(
						True,
						False,
						DocumentText(document, textInfos.POSITION_CARET),
					),
				),
				"caret": document.caret,
			}
		fields = [
			textInfos.FieldCommand("controlStart", textInfos.ControlField(role=controlTypes.Role.LINK)),
			textInfos.FieldCommand("formatChange", textInfos.FormatField(color="blue")),
			"a link",
			textInfos.FieldCommand("controlEnd", None),
		]
		info = types.SimpleNamespace(getTextWithFields=lambda formatConfig: fields)
		document = types.SimpleNamespace(STYLE_ATTRIBUTES=BrowseModeDocumentTreeInterceptor.STYLE_ATTRIBUTES)
		styles = BrowseModeDocumentTreeInterceptor._extractStyles(document, info)
		return {
			"paragraphs": paragraphs,
			"linkStyle": [dict(f.field) for f in styles if isinstance(f, textInfos.FieldCommand)],
		}
	if name == "guid":
		from comtypes import GUID

		text = "{618736E0-3C3D-11CF-810C-00AA00389B71}"
		guid = GUID(text)
		return {
			"str": str(guid),
			"repr": repr(guid),
			"size": ctypes.sizeof(guid),
			"nullIsFalse": not GUID(),
			"setIsTrue": bool(guid),
			"equalByValue": guid == GUID(text.lower()),
			"unequal": guid != GUID(),
			"notEqualToText": guid != text,
			"hashByValue": hash(guid) == hash(GUID(text)),
		}
	if name == "finderTables":
		from linuxPlatform import finder

		def build(**tables: tuple[str, ...]) -> dict:
			saved = {key: getattr(finder, key) for key in tables}
			try:
				for key, value in tables.items():
					setattr(finder, key, value)
				return _outcome(finder._buildTable)
			finally:
				for key, value in saved.items():
					setattr(finder, key, value)

		return {
			"asListed": _outcome(lambda: len(finder._buildTable())),
			"neverProvidedListed": build(standInModules=(*finder.standInModules, "winreg")),
			"listedTwice": build(standInModules=(*finder.standInModules, "winsound")),
			"keptWithoutReplacedPackage": build(keptSubmodules=("config.configFlags",)),
			"noFile": build(replacedModules=(*finder.replacedModules, "noSuchReplacement")),
		}
	if name == "windowsError":
		error = WindowsError(2, "x", None, 5)  # noqa: UP024
		return {
			"osErrorIsWindowsError": isinstance(OSError(), WindowsError),
			"isOSErrorSubclass": issubclass(WindowsError, OSError),
			"isOSError": WindowsError is OSError,
			"winerror": WindowsError(2, "x").winerror,  # noqa: UP024
			"givenWinerror": error.winerror,
			"givenErrno": error.errno,
		}
	if name == "lockScreen":
		# Both run on hot paths: the first for every focus and navigator change, the second on every
		# core pump cycle. A raise from either becomes an ERROR record in the log each time, which the
		# harness's checks record.
		import IAccessibleHandler
		from utils.security import objectBelowLockScreenAndWindowsIsLocked

		return {
			"objectBelowLockScreen": _outcome(lambda: objectBelowLockScreenAndWindowsIsLocked(object())),
			"IAccessibleHandler.pumpAll": _outcome(IAccessibleHandler.pumpAll),
		}
	if name == "installTwice":
		from linuxPlatform import bootstrap, finder, windowsCtypes

		before = {
			"windll": id(ctypes.windll),
			"WindowsError": id(builtins.WindowsError),
			"finders": sum(isinstance(f, finder.PlatformFinder) for f in sys.meta_path),
		}
		bootstrap.install()
		bootstrap._installed = False
		bootstrap.install()
		windowsCtypes.install()
		finder.install()
		after = {
			"windll": id(ctypes.windll),
			"WindowsError": id(builtins.WindowsError),
			"finders": sum(isinstance(f, finder.PlatformFinder) for f in sys.meta_path),
		}
		return {"before": before, "after": after}
	raise ValueError(f"unknown hazard {name}")


def _installState() -> dict:
	import ctypes

	return {
		"windllAdded": hasattr(ctypes, "windll"),
		"windowsErrorAdded": hasattr(builtins, "WindowsError"),
		"findersAdded": sum(type(f).__name__ == "PlatformFinder" for f in sys.meta_path),
	}


def runRefuse() -> dict:
	sys.path.insert(0, SOURCE)
	from linuxPlatform import bootstrap

	sys.platform = "win32"
	try:
		bootstrap.install()
	except RuntimeError as e:
		return {"raised": "RuntimeError", "message": str(e), **_installState()}
	return {"raised": None}


def runRefuseWhen(condition: str, entry: str = "bootstrap") -> dict:
	"""install() refused by a condition found only after its first check, and called twice, since a
	caller that catches the refusal may well try again. entry "windowsCtypes" calls
	windowsCtypes.install() itself, which must refuse as wholly as bootstrap does."""
	import ctypes
	import types

	sys.path.insert(0, SOURCE)
	from linuxPlatform import bootstrap, finder, windowsCtypes

	if condition == "winregImported":
		sys.modules["winreg"] = types.ModuleType("winreg")
	elif condition == "ctypesNameTaken":
		# The last name install() adds, so that adding them one by one would leave the rest behind.
		ctypes.HRESULT = ctypes.c_long
	elif condition == "windowsErrorTaken":
		builtins.WindowsError = OSError
	elif condition == "missingReplacement":
		finder.replacedModules = (*finder.replacedModules, "noSuchReplacement")
	else:
		raise ValueError(f"unknown condition {condition}")
	raised = []
	for _ in range(2):
		try:
			(windowsCtypes.install if entry == "windowsCtypes" else bootstrap.install)()
		except Exception as e:  # noqa: BLE001
			raised.append(type(e).__name__)
		else:
			raised.append(None)
	return {
		"raised": raised,
		"installed": bootstrap._installed,
		"ctypesNames": sorted(name for name in windowsCtypes.names if hasattr(ctypes, name)),
		"windowsErrorIsLinux": getattr(builtins, "WindowsError", None) is bootstrap.WindowsError,
		"findersAdded": sum(isinstance(f, finder.PlatformFinder) for f in sys.meta_path),
	}


def runNoLouis() -> dict:
	sys.path.insert(0, SOURCE)
	sys.modules["louis"] = None
	from linuxPlatform import bootstrap

	try:
		bootstrap.install()
	except RuntimeError as e:
		return {"raised": "RuntimeError", "message": str(e), **_installState()}
	return {"raised": None}


def main() -> None:
	resultPath, mode = sys.argv[1], sys.argv[2]
	result: dict = {"mode": mode}
	checks = harness.Checks(_record)
	try:
		if mode == "refuse":
			result.update(runRefuse())
		elif mode == "noLouis":
			result.update(runNoLouis())
		elif mode == "refuseWhen":
			result.update(runRefuseWhen(*sys.argv[3:5]))
		else:
			prepare()
			result["core"] = runCoreMainImports()
			if mode == "sweep":
				result.update(runSweep())
			elif mode == "hazard":
				result["hazard"] = runHazard(sys.argv[3])
	except BaseException as e:  # noqa: BLE001
		result["error"] = _failure(e)
	checks.finish()
	result["writeViolations"] = writeViolations() + result.pop("writeViolationsInChildren", [])
	result["events"] = events
	with open(resultPath, "w", encoding="utf-8") as f:
		json.dump(result, f, indent=1, default=repr)
	if workDir:
		shutil.rmtree(workDir, ignore_errors=True)
	sys.stdout.flush()
	sys.stderr.flush()
	# Skips interpreter teardown, where NVDA's atexit handlers would run against a half-built core.
	os._exit(0)


if __name__ == "__main__":
	main()
