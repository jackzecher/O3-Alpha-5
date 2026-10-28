#!/usr/bin/env python3
"""Offline synthetic launch of the exact accompanying Alpha 5 application."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import struct
import sys
import tempfile
import traceback
import zipfile

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE.parent / 'O3'

class PreflightError(RuntimeError):
    pass

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def verify_files(root, expected, exclusions=()):
    if not isinstance(expected, dict) or not expected:
        raise PreflightError('INVALID_FILE_MANIFEST')
    found = set()
    for file in root.rglob('*'):
        if file.is_symlink():
            raise PreflightError('LINKED_PAYLOAD_NOT_SUPPORTED')
        if file.is_file():
            name = file.relative_to(root).as_posix()
            if name not in exclusions:
                found.add(name)
    if found != set(expected):
        raise PreflightError('PAYLOAD_FILE_SET_MISMATCH: ' + str(root.name))
    for name, fingerprint in expected.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name
                or ':' in name or not isinstance(fingerprint, str) or len(fingerprint) != 64):
            raise PreflightError('UNSAFE_MANIFEST_ENTRY')
        if digest(root / name) != fingerprint:
            raise PreflightError('PAYLOAD_HASH_MISMATCH: ' + name)

def verify_package():
    data = json.loads((HERE / 'AI_RUNTIME_MANIFEST.json').read_text())
    if data.get('schema') != 2 or data.get('mode') != 'OFFLINE_SYNTHETIC_QT_ONLY':
        raise PreflightError('INVALID_AI_RUNTIME_POLICY')
    verify_files(PAYLOAD, data['o3_files'])
    verify_files(HERE, data['runtime_files'], ('AI_RUNTIME_MANIFEST.json',))
    return data

def check_host():
    if not (sys.implementation.name == 'cpython' and sys.version_info.major == 3
            and 11 <= sys.version_info.minor <= 13):
        raise PreflightError('Requires CPython 3.11-3.13')
    if platform.system() != 'Linux' or platform.machine().lower() not in ('x86_64', 'amd64') or struct.calcsize('P') != 8:
        raise PreflightError('Requires Linux x86_64; use O3/O3.exe on Windows')
    libc, version = platform.libc_ver()
    try:
        supported = libc == 'glibc' and tuple(map(int, version.split('.')[:2])) >= (2, 28)
    except ValueError:
        supported = False
    if not supported:
        raise PreflightError('Requires glibc >= 2.28')

def unpack_archive(archive_path, destination, allowed):
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if (not name.parts or name.is_absolute() or '..' in name.parts
                    or '\\' in member.filename or ':' in member.filename
                    or name.parts[0] not in allowed or member.flag_bits & 1
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise PreflightError('UNSAFE_DEPENDENCY_ARCHIVE_ENTRY')
            target = destination.joinpath(*name.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)

def prepare_runtime(temporary, capture):
    libs = temporary / 'libraries'
    libs.mkdir()
    for wheel in sorted((HERE / 'wheels').glob('*.whl')):
        unpack_archive(wheel, libs, {'PySide6', 'shiboken6',
            'pyside6_essentials-6.9.2.dist-info', 'shiboken6-6.9.2.dist-info'})
    unpack_archive(HERE / 'pure_python_deps.zip', libs, {'rdflib', 'pyparsing',
        'rdflib-7.6.0.dist-info', 'pyparsing-3.3.3.dist-info'})
    sys.path[:0] = [str(libs), str(PAYLOAD / 'O3_PRODUCT/APP'), str(HERE)]
    for variable, directory in [('XDG_CACHE_HOME', 'cache'), ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data')]:
        target = temporary / directory
        target.mkdir()
        os.environ[variable] = str(target)
    os.environ['QT_PLUGIN_PATH'] = str(libs / 'PySide6/Qt/plugins')
    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(libs / 'PySide6/Qt/plugins/platforms')
    if capture:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    elif not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise PreflightError('No graphical display; use --capture-all OUTPUT_DIRECTORY')
    import PySide6, shiboken6, rdflib, pyparsing
    from PySide6.QtCore import qVersion
    actual = {'pyside6': PySide6.__version__, 'shiboken6': shiboken6.__version__,
              'qt': qVersion(), 'rdflib': rdflib.__version__, 'pyparsing': pyparsing.__version__}
    if actual != {'pyside6': '6.9.2', 'shiboken6': '6.9.2', 'qt': '6.9.2', 'rdflib': '7.6.0', 'pyparsing': '3.3.3'}:
        raise PreflightError('DEPENDENCY_VERSION_MISMATCH')
    return actual

def output_path(value, directory=False):
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(PAYLOAD) or path.is_relative_to(HERE):
        raise PreflightError('Capture output must be outside O3/ and AI_RUNTIME/')
    if directory:
        path.mkdir(parents=True, exist_ok=True)
    else:
        if path.suffix.lower() != '.png':
            raise PreflightError('Screenshot output must end in .png')
        path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check-only', action='store_true')
    group.add_argument('--capture-all', metavar='OUTPUT_DIRECTORY')
    group.add_argument('--screenshot', metavar='OUTPUT.png')
    parser.add_argument('--view', choices=('mail', 'workspace', 'records', 'reader', 'connections', 'settings', 'sources'), default='mail')
    parser.add_argument('--width', type=int, default=1360)
    parser.add_argument('--height', type=int, default=860)
    parser.add_argument('--text-scale', type=float, choices=(1.0, 1.25, 1.5), default=1.0)
    args = parser.parse_args()
    if not 860 <= args.width <= 4096 or not 680 <= args.height <= 2160:
        parser.error('Supported size: width 860-4096, height 680-2160')
    if not sys.flags.isolated or not sys.flags.no_site:
        parser.error('Use python3 -I -S AI_RUNTIME/run.py to isolate host packages and startup hooks')
    sys.dont_write_bytecode = True
    try:
        check_host()
        data = verify_package()
        print('AI_INTEGRITY_PASS:', data['application_build_id'], flush=True)
        if args.check_only:
            return 0
        if args.capture_all:
            args.capture_all = output_path(args.capture_all, directory=True)
        if args.screenshot:
            args.screenshot = output_path(args.screenshot)
        with tempfile.TemporaryDirectory(prefix='o3_alpha4_ai_') as folder:
            temporary = Path(folder)
            versions = prepare_runtime(temporary, bool(args.capture_all or args.screenshot))
            print('OFFLINE_RUNTIME_READY:', json.dumps(versions), flush=True)
            import preview_app
            return preview_app.launch(args, temporary, data, versions)
    except Exception as exc:
        print('AI_PREVIEW_FAILED:', type(exc).__name__, str(exc), file=sys.stderr, flush=True)
        traceback.print_exc()
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
