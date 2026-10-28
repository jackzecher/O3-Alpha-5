"""Open the KM-integrated O3 workbench in one explicit persistent workspace.

The parent selects the interpreter and supervises startup. This entry point
never supplies fixtures, discovers a source, or imposes a session deadline.
Its protocol parser is stdlib-only so the parent can import it safely.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import struct
import sys
import threading

BUILD_ID = "0.9.1-km12-p3-prism2"
PREFIX = "O3_WORKSPACE_JSON="
WINDOWS = "WINDOWS_WORKSPACE"
TECHNICIAN = "WINDOWS_TECHNICIAN_TEST"
ALPHA = "WINDOWS_NIPR_ALPHA"
NONNIPR_ALPHA = "WINDOWS_NONNIPR_ALPHA"
_TECHNICIAN_DLL_HANDLES = []
REQUIRED_STARTUP = {"validation", "identity", "dependencies", "native", "engine", "ready"}
STAGES = (*sorted(REQUIRED_STARTUP), "operation", "maintenance", "shutdown", "final")
DEPENDENCY_FIELDS = ('rdflib_version', 'pyparsing_version', 'pyside6_version',
                     'qt_version', 'shiboken6_version')
STATE_CODES = (
    "WORKSPACE_PATH_INVALID", "WORKSPACE_PATH_LINKED", "WORKSPACE_LOCATION_INVALID",
    "WORKSPACE_UNOWNED", "WORKSPACE_MARKER_INVALID", "WORKSPACE_LAYOUT_INVALID",
    "WORKSPACE_CONFIG_INVALID", "WORKSPACE_DATABASE_MISSING", "WORKSPACE_DATABASE_INVALID",
    "WORKSPACE_SCHEMA_UNSUPPORTED", "WORKSPACE_IDENTITY_MISMATCH", "WORKSPACE_IO_FAILED",
)
OPERATIONS = ("scan", "list", "detail", "content", "overview", "preview", "audit", "rules",
              "backup", "verify_backup", "export_rdf", "km", "restore", "migrate", "reopen", "OTHER")
ERROR_TYPES = ("IMPORT_ERROR", "MODULE_NOT_FOUND", "OS_ERROR", "VALUE_ERROR", "RUNTIME_ERROR", "OTHER")


def enum(*values):
    return lambda value: type(value) is str and value in values


def integer(low, high):
    return lambda value: type(value) is int and low <= value <= high


BOOL = lambda value: type(value) is bool
VERSION = lambda value: type(value) is str and re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}", value) is not None
FIELDS = {
    "validation": {"workspace_owned": BOOL, "state_schema_expected": integer(2, 4), "config_loaded": BOOL,
                   "source_configured": BOOL, "paused": BOOL, "automatic_discovery": BOOL},
    "maintenance": {"upgrade_required": BOOL, "writer_open": BOOL},
    "identity": {"python_version": VERSION, "python_bits": integer(64, 64), "platform": enum("WINDOWS"),
                 "isolated": BOOL, "bytecode_disabled": BOOL, "prefix_matches": BOOL},
    "dependencies": {**{key: VERSION for key in DEPENDENCY_FIELDS}, "origins_within_runtime": BOOL},
    "native": {"platform_name": enum("windows"), "window_exposed": BOOL,
               "paint_observed": BOOL, "event_loop_observed": BOOL},
    "engine": {"initialized": BOOL},
    "ready": {"ready": BOOL},
    "operation": {"operation": enum(*OPERATIONS), "warning_observed": BOOL},
    "shutdown": {"worker_closed": BOOL, "thread_stopped": BOOL, "window_closed": BOOL,
                 "lock_released": BOOL, "app_exit_code": integer(-2147483648, 4294967295)},
    "final": {"ready": BOOL, "shutdown_clean": BOOL, "failure_count": integer(0, 256),
              "stop_requested": BOOL, "operation_warning_count": integer(0, 65535)},
    "error": {"error_type": enum(*ERROR_TYPES)},
    "stop": {"stop_requested": BOOL},
}
SPECS = {
    "WORKSPACE_VALIDATED": ("validation", "PASS", "validation"),
    "IDENTITY_VERIFIED": ("identity", "PASS", "identity"),
    "DEPENDENCIES_VERIFIED": ("dependencies", "PASS", "dependencies"),
    "NATIVE_WINDOW_OBSERVED": ("native", "PASS", "native"),
    "WORKSPACE_UPGRADE_REQUIRED": ("maintenance", "NOT_TESTED", "maintenance"),
    "ENGINE_READY": ("engine", "PASS", "engine"),
    "WORKSPACE_READY": ("ready", "PASS", "ready"),
    "WORKSPACE_OPERATION_WARNING": ("operation", "NOT_TESTED", "operation"),
    "ORDERLY_SHUTDOWN": ("shutdown", "PASS", "shutdown"),
    "SHUTDOWN_UNCONFIRMED": ("shutdown", "FAIL", "shutdown"),
    "STOP_REQUESTED": ("shutdown", "NOT_TESTED", "stop"),
    "WORKSPACE_SESSION_CLOSED": ("final", "PASS", "final"),
    "WORKSPACE_SESSION_FAILED": ("final", "FAIL", "final"),
    "WORKSPACE_SESSION_INCOMPLETE": ("final", "NOT_TESTED", "final"),
    **{code: (stage, "FAIL", "error") for code, stage in {
        **{code: "validation" for code in STATE_CODES},
        "WORKSPACE_ALREADY_OPEN": "validation", "IDENTITY_CHECK_FAILED": "identity",
        "DEPENDENCIES_CHECK_FAILED": "dependencies", "TIMEZONE_DATA_UNAVAILABLE": "dependencies",
        "NATIVE_WINDOW_CHECK_FAILED": "native",
        "ENGINE_INITIALIZE_FAILED": "engine", "GUI_EXCEPTION": "ready", "STARTUP_EXCEPTION": "ready",
    }.items()},
}
CODES = tuple(SPECS)


def validate_protocol_record(record, runtime_contract=None) -> bool:
    if type(record) is not dict or set(record) != {"stage", "result", "code", "details"}:
        return False
    code = record["code"]
    if type(code) is not str or code not in SPECS:
        return False
    stage, result, schema = SPECS[code]
    details = record["details"]
    if record["stage"] != stage or record["result"] != result or type(details) is not dict:
        return False
    if set(details) != set(FIELDS[schema]) | {"classification"}:
        return False
    classification = details["classification"]
    declared = runtime_contract.get("classification") if isinstance(runtime_contract, dict) else None
    expected = declared if declared in (TECHNICIAN, ALPHA, NONNIPR_ALPHA) else WINDOWS
    if (classification != expected
            or not all(check(details[key]) for key, check in FIELDS[schema].items())):
        return False
    if result != "PASS":
        return True
    if schema == "validation":
        return details["workspace_owned"] and details["config_loaded"] and not details["automatic_discovery"]
    if schema == "identity":
        return (isinstance(runtime_contract, dict)
                and details['python_version'] == runtime_contract.get('python_version')
                and details["platform"] == "WINDOWS"
                and all(details[key] for key in ("isolated", "bytecode_disabled", "prefix_matches")))
    if schema == "dependencies":
        approved = runtime_contract.get('dependencies') if isinstance(runtime_contract, dict) else None
        return (type(approved) is dict and set(approved) == set(DEPENDENCY_FIELDS)
                and all(details[key] == approved[key] for key in DEPENDENCY_FIELDS)
                and details['origins_within_runtime'])
    if schema == "native":
        return (code == "NATIVE_WINDOW_OBSERVED"
                and details["platform_name"] == "windows"
                and all(details[key] for key in ("window_exposed", "paint_observed", "event_loop_observed")))
    if schema in ("engine", "ready"):
        return all(details[key] for key in FIELDS[schema])
    if schema == "shutdown":
        return details["app_exit_code"] == 0 and all(details[key] for key in ("worker_closed", "thread_stopped", "window_closed", "lock_released"))
    if schema == "final":
        return (code == "WORKSPACE_SESSION_CLOSED"
                and details["ready"] and details["shutdown_clean"] and details["failure_count"] == 0 and not details["stop_requested"])
    return False


def error_type(exc):
    for cls, label in ((ModuleNotFoundError, "MODULE_NOT_FOUND"), (ImportError, "IMPORT_ERROR"),
                       (OSError, "OS_ERROR"), (ValueError, "VALUE_ERROR"), (RuntimeError, "RUNTIME_ERROR")):
        if isinstance(exc, cls):
            return label
    return "OTHER"


class Evidence:
    def __init__(self, classification, output=print, runtime_contract=None):
        self.classification, self.output = classification, output
        self.runtime_contract = runtime_contract
        self.passed = set()
        self.failures = 0
        self.stop_requested = False
        self.operation_warning_count = 0
        self.warned_operations = set()

    def emit(self, code, **details):
        stage, result, _ = SPECS[code]
        record = {"stage": stage, "result": result, "code": code,
                  "details": {"classification": self.classification, **details}}
        if not validate_protocol_record(record, self.runtime_contract):
            raise ValueError("INVALID_AUTHORED_PROTOCOL")
        if result == "FAIL":
            self.failures = min(256, self.failures + 1)
        if result == "PASS":
            self.passed.add(stage)
        self.output(PREFIX + json.dumps(record, separators=(",", ":")), flush=True)

    def warning(self, operation):
        operation = operation if operation in OPERATIONS else "OTHER"
        self.operation_warning_count = min(65535, self.operation_warning_count + 1)
        # Unlimited sessions must not grow the protocol log once per poll/scan.
        if operation not in self.warned_operations:
            self.warned_operations.add(operation)
            self.emit("WORKSPACE_OPERATION_WARNING", operation=operation, warning_observed=True)

    def finish(self, clean):
        ready = REQUIRED_STARTUP <= self.passed
        code = "WORKSPACE_SESSION_FAILED" if self.failures else "WORKSPACE_SESSION_INCOMPLETE"
        if ready and clean and not self.failures and not self.stop_requested:
            code = "WORKSPACE_SESSION_CLOSED"
        self.emit(code, ready=ready, shutdown_clean=clean, failure_count=self.failures,
                  stop_requested=self.stop_requested, operation_warning_count=self.operation_warning_count)
        return 0 if SPECS[code][1] == "PASS" else (2 if self.failures else 4)


def load_state_api(kit):
    spec = importlib.util.spec_from_file_location("_O3_WORKSPACE_STATE", kit / "_O3_WORKSPACE_STATE.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def disable_source_discovery(ui):
    # app.py imports this name directly; its local alias is the one used by both
    # on_ready and the native source chooser. The chooser itself is unchanged.
    ui.discover_sources = lambda: []


def read_stop_commands(commands):
    """One bounded command buffer; raw fd read cannot retain a TextIO lock."""
    try:
        pending, discard = bytearray(), False
        while True:
            chunk = os.read(sys.stdin.fileno(), 64)
            if not chunk:
                return
            for byte in chunk:
                if byte == 10:
                    if not discard and bytes(pending).rstrip(b"\r") == b"STOP":
                        try:
                            commands.put_nowait("STOP")
                        except queue.Full:
                            pass
                        return
                    pending.clear()
                    discard = False
                elif len(pending) < 5:
                    pending.append(byte)
                else:
                    discard = True
    except (OSError, ValueError):
        return


def run_workbench(kit, state, state_api, evidence):
    from PySide6.QtCore import QEvent, QObject, QTimer, QLockFile, Slot
    from PySide6.QtWidgets import QApplication
    import o3_gui.app as ui
    from o3_gui.logic import read_config
    from o3_gui.presentation import CSS

    from o3_gui.km_client import WorkspaceLock
    lock = WorkspaceLock(state)
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        evidence.emit("WORKSPACE_ALREADY_OPEN", error_type="RUNTIME_ERROR")
        return evidence.finish(False)
    try:
        state_api.validate_database_for_open(state)
        config = read_config(state)
        if evidence.classification == TECHNICIAN:
            # First-run state must stay empty until the accepted Engine creates
            # its database. Bind only this newly generated synthetic source in
            # memory; persist after the existing readiness/ownership checkpoint.
            session = state.parents[1]
            synthetic = session / 'SYNTHETIC_INPUT' / 'O3Data'
            backups = session / 'Backups'
            if (not synthetic.is_dir() or not backups.is_dir()
                    or config.get('source_root') not in (None, str(synthetic))
                    or config.get('backup_root') not in (None, str(backups))):
                raise ValueError('TEST_WORKSPACE_INVALID')
            config.update(source_root=str(synthetic), backup_root=str(backups))
    except Exception:
        lock.unlock()
        raise
    disable_source_discovery(ui)
    evidence.emit("WORKSPACE_VALIDATED", workspace_owned=True, state_schema_expected=4, config_loaded=True,
                  source_configured=bool(config.get("source_root")), paused=bool(config.get("paused")), automatic_discovery=False)
    app = QApplication(["O3"])
    app.setApplicationName("O3 Operational Intelligence")
    app.setOrganizationName("O3")
    app.setStyle("Fusion")
    app.setStyleSheet(CSS)
    expected_platform = "windows"
    if app.platformName().lower() != expected_platform:
        lock.unlock()
        evidence.emit("NATIVE_WINDOW_CHECK_FAILED", error_type="RUNTIME_ERROR")
        return evidence.finish(False)

    class WorkspaceWindow(ui.O3Window):
        def choose_source(self):
            if evidence.classification == TECHNICIAN:
                self.show_information('Synthetic source locked',
                    'This technician workspace is bound to its isolated synthetic O3Data. '
                    'External sources cannot be selected in this test.', warning=True)
                return
            return super().choose_source()

        def on_maintenance(self, data):
            evidence.emit("WORKSPACE_UPGRADE_REQUIRED", upgrade_required=True, writer_open=False)
            super().on_maintenance(data)

        def on_ready(self, summary):
            if self.closing:
                return
            # Commit the ownership fact at the actual worker-ready callback,
            # before scans are queued or an immediate close can skip a poll.
            try:
                state_api.mark_database_initialized(state)
            except Exception as exc:
                safe_state_code = getattr(exc, "code", None)
                code = safe_state_code if type(safe_state_code) is str and safe_state_code in STATE_CODES else "WORKSPACE_IO_FAILED"
                evidence.emit(code, error_type=error_type(exc))
                self.startup_timer.stop()
                QTimer.singleShot(0, self.close)
                return
            if evidence.classification == TECHNICIAN:
                try:
                    from o3_gui.logic import save_config
                    save_config(state, config)
                except (OSError, ValueError) as exc:
                    evidence.emit('WORKSPACE_IO_FAILED', error_type=error_type(exc))
                    self.startup_timer.stop()
                    QTimer.singleShot(0, self.close)
                    return
            super().on_ready(summary)
            evidence.emit("ENGINE_READY", initialized=True)

        def closeEvent(self, event):
            # A failed/aborted initialization must not save display preferences
            # over an existing configuration. Successful workspaces use the
            # unchanged close handler and its normal preference persistence.
            if self.initialized or self.allow_close:
                return super().closeEvent(event)
            event.ignore()
            if self.closing:
                return
            self.closing = True
            self.tick.stop()
            self.preference_timer.stop()
            if self.thread.isRunning():
                self.shutdown.emit()
            else:
                self.done_closing()

    window = WorkspaceWindow(state, config, lock)
    if evidence.classification == TECHNICIAN:
        window.setWindowTitle('O3 | SYNTHETIC TECHNICIAN TEST — NOT PRODUCTION')
        window.mode_chip.setText('SYNTHETIC TEST')
    elif evidence.classification == ALPHA:
        window.setWindowTitle('O3 NIPR Alpha 1 | Nonproduction')
        window.mode_chip.setText('NIPR ALPHA')
    elif evidence.classification == NONNIPR_ALPHA:
        window.setWindowTitle('O3 non-NIPR Alpha 1 | Nonproduction')
        window.mode_chip.setText('NON-NIPR ALPHA')
    else:
        window.setWindowTitle("O3 | Operational Intelligence Workspace")
        window.mode_chip.setText("LOCAL WORKSPACE")
    commands = queue.Queue(maxsize=1)

    class Monitor(QObject):
        def __init__(self):
            super().__init__(window)
            self.painted = False
            self.worker_closed = False
            self.error_seen = None
            self.closing_requested = False
            self.timer = QTimer(self)
            self.timer.setInterval(100)
            self.timer.timeout.connect(self.poll)

        def eventFilter(self, obj, event):
            if obj is window and event.type() == QEvent.Type.Paint:
                self.painted = True
            return False

        @Slot()
        def closed(self):
            self.worker_closed = True

        def request_close(self):
            if self.closing_requested:
                return
            self.closing_requested = True
            modal = app.activeModalWidget()
            if modal is not None and hasattr(modal, "reject"):
                modal.reject()
            window.close()

        def record_error(self):
            if window.last_error is None or window.last_error is self.error_seen:
                return
            self.error_seen = window.last_error
            if window.last_error[0] == "initialize":
                evidence.emit("ENGINE_INITIALIZE_FAILED", error_type="OTHER")
                self.request_close()
            else:
                evidence.warning(window.last_error[0])

        @Slot()
        def poll(self):
            try:
                if not commands.empty() and not evidence.stop_requested:
                    commands.get_nowait()
                    evidence.stop_requested = True
                    evidence.emit("STOP_REQUESTED", stop_requested=True)
                    self.request_close()
                handle = window.windowHandle()
                if "native" not in evidence.passed and self.painted and handle is not None and handle.isExposed():
                    code = "NATIVE_WINDOW_OBSERVED"
                    evidence.emit(code, platform_name=expected_platform, window_exposed=True,
                                  paint_observed=True, event_loop_observed=True)
                self.record_error()
                if (REQUIRED_STARTUP - {"ready"}) <= evidence.passed and "ready" not in evidence.passed and not evidence.failures:
                    evidence.emit("WORKSPACE_READY", ready=True)
            except Exception as exc:
                self.timer.stop()
                safe_state_code = getattr(exc, "code", None)
                code = safe_state_code if type(safe_state_code) is str and safe_state_code in STATE_CODES else "GUI_EXCEPTION"
                evidence.emit(code, error_type=error_type(exc))
                self.request_close()

    monitor = Monitor()
    window.installEventFilter(monitor)
    window.worker.closed.connect(monitor.closed)

    def slot_exception(_type, value, _traceback):
        evidence.emit("GUI_EXCEPTION", error_type=error_type(value))
        QTimer.singleShot(0, monitor.request_close)

    sys.excepthook = slot_exception
    threading.Thread(target=read_stop_commands, args=(commands,), daemon=True, name="O3StopReader").start()
    window.show()
    window.raise_()
    window.activateWindow()
    monitor.timer.start()
    app_exit = app.exec()
    monitor.timer.stop()
    monitor.record_error()
    details = {"worker_closed": monitor.worker_closed, "thread_stopped": not window.thread.isRunning(),
               "window_closed": window.allow_close and not window.isVisible(), "lock_released": not lock.isLocked(),
               "app_exit_code": app_exit}
    clean = app_exit == 0 and all(details[key] for key in ("worker_closed", "thread_stopped", "window_closed", "lock_released"))
    evidence.emit("ORDERLY_SHUTDOWN" if clean else "SHUTDOWN_UNCONFIRMED", **details)
    return evidence.finish(clean)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--technician-test", action="store_true")
    parser.add_argument("--nipr-alpha", action="store_true")
    parser.add_argument("--non-nipr-alpha", action="store_true")
    args = parser.parse_args(argv)
    if sum((args.technician_test, args.nipr_alpha, args.non_nipr_alpha)) > 1:
        return 2
    if args.technician_test or args.nipr_alpha or args.non_nipr_alpha:
        # Both are nonproduction paths. Neither creates production approval.
        # The production runtime manifest remains mandatory in the other branch.
        selected_class = (TECHNICIAN if args.technician_test else
                          ALPHA if args.nipr_alpha else NONNIPR_ALPHA)
        key = ("O3_TECHNICIAN_CONTRACT" if args.technician_test else
               "O3_NIPR_ALPHA_CONTRACT" if args.nipr_alpha else "O3_NONNIPR_ALPHA_CONTRACT")
        raw = os.environ.get(key, "")
        if not 0 < len(raw) < 4096:
            return 2
        try:
            contract = json.loads(raw)
            keys = {'classification', 'python_version', 'python_bits', 'dependencies',
                    'python_executable_sha256', 'prefix_sha256'}
            if (type(contract) is not dict or set(contract) != keys
                    or contract['classification'] != selected_class
                    or contract['python_bits'] != 64
                    or type(contract['python_executable_sha256']) is not str
                    or re.fullmatch('[0-9a-f]{64}', contract['python_executable_sha256']) is None
                    or type(contract['prefix_sha256']) is not str
                    or re.fullmatch('[0-9a-f]{64}', contract['prefix_sha256']) is None
                    or type(contract['dependencies']) is not dict
                    or set(contract['dependencies']) != set(DEPENDENCY_FIELDS)
                    or not VERSION(contract['python_version'])
                    or any(not VERSION(value) for value in contract['dependencies'].values())):
                return 2
            with Path(sys.executable).open('rb') as stream:
                executable_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            prefix_sha = hashlib.sha256(os.fsencode(str(Path(sys.prefix).resolve()))).hexdigest()
            if (executable_sha != contract['python_executable_sha256']
                    or prefix_sha != contract['prefix_sha256']):
                return 2
            if sys.platform == 'win32':
                for name in ('Library/bin', 'DLLs'):
                    directory = Path(sys.prefix) / name
                    if directory.is_dir():
                        _TECHNICIAN_DLL_HANDLES.append(os.add_dll_directory(str(directory)))
        except (OSError, ValueError, TypeError, KeyError):
            return 2
        runtime_contract = contract
        classification = selected_class
    else:
        # Original production path: approved distribution manifest is mandatory.
        runtime_manifest = json.loads((Path(args.kit).resolve().parent / 'RUNTIME_MANIFEST.json').read_text(encoding='utf-8'))
        runtime_contract = {'python_version':runtime_manifest['python_version'],
                            'dependencies':runtime_manifest['dependencies']}
        classification = WINDOWS
    evidence = Evidence(classification, runtime_contract=runtime_contract)
    stage = "IDENTITY_CHECK_FAILED"
    try:
        if sys.platform != "win32" or struct.calcsize("P") != 8:
            raise RuntimeError("WINDOWS_X64_REQUIRED")
        platform = "WINDOWS"
        executable = Path(sys.executable).resolve()
        evidence.emit("IDENTITY_VERIFIED", python_version=".".join(map(str, sys.version_info[:3])),
                      python_bits=struct.calcsize("P") * 8, platform=platform, isolated=bool(sys.flags.isolated),
                      bytecode_disabled=bool(sys.dont_write_bytecode), prefix_matches=executable.is_relative_to(Path(sys.prefix).resolve()))
        stage = "DEPENDENCIES_CHECK_FAILED"
        import rdflib
        import pyparsing
        import PySide6
        import shiboken6
        from PySide6.QtCore import qVersion
        origins = all(Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
                      for module in (rdflib, pyparsing, PySide6, shiboken6))
        stage = "TIMEZONE_DATA_UNAVAILABLE"
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        stage = "DEPENDENCIES_CHECK_FAILED"
        evidence.emit("DEPENDENCIES_VERIFIED", rdflib_version=rdflib.__version__, pyparsing_version=pyparsing.__version__,
                      pyside6_version=PySide6.__version__, qt_version=qVersion(), shiboken6_version=shiboken6.__version__,
                      origins_within_runtime=origins)
        stage = "WORKSPACE_PATH_INVALID"
        kit = Path(args.kit)
        if not kit.is_absolute() or kit.resolve() != Path(__file__).resolve().parent:
            raise ValueError("EXACT_KIT_REQUIRED")
        state_api = load_state_api(kit)
        state, _logs = state_api.validate_workspace(kit, Path(args.workspace))
        sys.path.insert(0, str(kit / "APP"))
        stage = "STARTUP_EXCEPTION"
        return run_workbench(kit, state, state_api, evidence)
    except Exception as exc:
        safe_state_code = getattr(exc, "code", None)
        code = safe_state_code if type(safe_state_code) is str and safe_state_code in STATE_CODES else stage
        evidence.emit(code, error_type=error_type(exc))
        return evidence.finish(False)


if __name__ == "__main__":
    raise SystemExit(main())
