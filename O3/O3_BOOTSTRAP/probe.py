"""Read-only Qt/dependency preflight in an explicitly selected existing Windows Python.

No package installation, imports from O3_PRODUCT, or source access occurs here.
The launcher ignores all non-protocol subprocess output (including Qt diagnostics).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import struct
import sys

PREFIX = 'O3_TECHNICIAN_PROBE='
PINS = {'rdflib_version': '7.6.0', 'pyparsing_version': '3.3.3',
        'pyside6_version': '6.9.2', 'qt_version': '6.9.2', 'shiboken6_version': '6.9.2'}


def emit(stage, **items):
    print(PREFIX + json.dumps({'stage': stage, **items}, sort_keys=True, separators=(',', ':')), flush=True)


def fail(code):
    emit('FAIL', code=code)
    return 2


def main():
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        return fail('WINDOWS_X64_REQUIRED')
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        return fail('ISOLATED_INVOCATION_REQUIRED')
    prefix = Path(sys.prefix).resolve()
    python = Path(sys.executable).resolve()
    if (not python.is_relative_to(prefix) or python.name.casefold() != 'python.exe'
            or python.parent not in (prefix, prefix / 'Scripts')):
        return fail('PYTHON_IDENTITY_MISMATCH')
    mode = os.environ.get('O3_BOOTSTRAP_MODE')
    if mode not in ('nipr', 'home'):
        return fail('INVALID_BOOTSTRAP_MODE')
    if mode == 'nipr':
        if (prefix.name.casefold() != 'focus-native' or python.parent != prefix
                or not (prefix / 'conda-meta' / 'history').is_file()):
            return fail('CONDA_IDENTITY_MISMATCH')
    elif (prefix != (Path(os.environ.get('LOCALAPPDATA', '')) /
                      'Programs' / 'Python' / 'Python312').resolve()):
        return fail('HOME_PYTHON_IDENTITY_MISMATCH')
    version = '.'.join(map(str, sys.version_info[:3]))
    # CPython patch versions remain in the same 3.12 ABI family. Report the
    # real patch level so the parent and supervised child can agree exactly.
    if (tuple(sys.version_info[:2]) != (3, 12) or
            (mode == 'nipr' and version != '3.12.15')):
        return fail('PYTHON_VERSION_UNQUALIFIED')
    try:
        with python.open('rb') as stream:
            exe_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError:
        return fail('PYTHON_EXECUTABLE_UNREADABLE')
    emit('IDENTITY_OK', python_version=version, python_bits=64,
         python_executable_sha256=exe_hash,
         prefix_sha256=hashlib.sha256(os.fsencode(str(prefix))).hexdigest(),
         runtime_origin_within_prefix=True, mode=mode, isolated=True, bytecode_disabled=True)

    # Match the child process's permitted DLL resolution: process-local only,
    # using this existing environment. Hold directory handles until exit.
    dll_handles = []
    for name in ('Library/bin', 'DLLs'):
        directory = prefix / name
        if directory.is_dir():
            try:
                dll_handles.append(os.add_dll_directory(str(directory)))
            except (OSError, AttributeError):
                return fail('NATIVE_DLL_PATH_INVALID')

    libraries = {}
    for name, module_name in (('rdflib_version', 'rdflib'), ('pyparsing_version', 'pyparsing'),
                              ('pyside6_version', 'PySide6'), ('shiboken6_version', 'shiboken6')):
        try:
            module = importlib.import_module(module_name)
        except (ImportError, OSError):
            return fail('MISSING_' + module_name.upper())
        origin = getattr(module, '__file__', None)
        if (not isinstance(origin, str)
                or not Path(origin).resolve().is_relative_to(prefix)):
            return fail('DEPENDENCY_ORIGIN_MISMATCH')
        actual = getattr(module, '__version__', None)
        if not isinstance(actual, str) or actual != PINS[name]:
            return fail('DEPENDENCY_VERSION_UNQUALIFIED')
        libraries[name] = actual
    try:
        from PySide6.QtCore import qVersion, QLibraryInfo, QTimer
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError):
        return fail('NATIVE_QT_IMPORT_FAILED')
    libraries['qt_version'] = qVersion()
    if libraries != PINS:
        return fail('DEPENDENCY_VERSION_UNQUALIFIED')
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo('America/New_York')
        ZoneInfo('America/Los_Angeles')
    except (ImportError, OSError, KeyError):
        return fail('TIMEZONE_DATA_UNAVAILABLE')
    emit('DEPENDENCIES_OK', dependencies=libraries, timezone_support=True,
         origins_within_runtime=True)
    # Native Qt can abort before Python can catch an exception. In that case the
    # parent uses the last successful stage to report NATIVE_QT_START_FAILED.
    try:
        app = QApplication(['O3 Universal Native Qt Check'])
        platform = app.platformName().lower()
        if platform != 'windows':
            return fail('QT_PLATFORM_NOT_WINDOWS')
        plugins = [Path(x).resolve() for x in app.libraryPaths()]
        if not any(path.is_relative_to(prefix) and (path / 'platforms' / 'qwindows.dll').is_file()
                   for path in plugins):
            return fail('QT_PLATFORM_PLUGIN_ORIGIN_INVALID')
        QTimer.singleShot(0, app.quit)
        if app.exec() != 0:
            return fail('QT_EVENT_LOOP_FAILED')
        emit('NATIVE_OK', qt_platform=platform, qt_event_loop=True,
             plugin_origin_within_runtime=True,
             qt_library_info_within_runtime=Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)).resolve().is_relative_to(prefix))
        return 0
    except (ImportError, OSError, RuntimeError):
        return fail('NATIVE_QT_START_FAILED')


if __name__ == '__main__':
    raise SystemExit(main())
