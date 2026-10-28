"""Own one project workspace without adopting, resetting, or migrating other state.

The launcher checks paths and ownership before starting its child. The child
must acquire the application's QLockFile before validate_database_for_open().
Only immediate state entries and bounded configuration/SQLite metadata are
inspected; operational source and backup paths are never opened here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import uuid

BUILD_ID = '0.9.1-km12-p3-prism2'
COMPATIBLE_OWNER_BUILDS = frozenset({BUILD_ID,'0.9.0-km12-workspace-types','0.8.0-km114-views1','0.7.0-km114-builder1', '0.6.1-km114-efficiency1', '0.6.0-km114-modernization1', '0.5.0-km114-integration1', '0.4.0-pass2-workspace1'})
WORKSPACE_NAME = 'O3_Workspace'
MARKER_FILENAME = 'workspace-owner.json'
BLOCK_CODES = (
    'WORKSPACE_PATH_INVALID', 'WORKSPACE_PATH_LINKED', 'WORKSPACE_LOCATION_INVALID',
    'WORKSPACE_UNOWNED', 'WORKSPACE_MARKER_INVALID', 'WORKSPACE_LAYOUT_INVALID',
    'WORKSPACE_CONFIG_INVALID', 'WORKSPACE_DATABASE_MISSING',
    'WORKSPACE_DATABASE_INVALID', 'WORKSPACE_SCHEMA_UNSUPPORTED',
    'WORKSPACE_IDENTITY_MISMATCH', 'WORKSPACE_IO_FAILED',
)
IDENTITY_SCOPES = {'directory_scope': 'o3data', 'calendar_scope': 'primary',
                   'document_scope': 'o3data', 'loe_namespace': 'o3data'}


class WorkspaceBlocked(RuntimeError):
    def __init__(self, code):
        if code not in BLOCK_CODES:
            raise ValueError('UNKNOWN_WORKSPACE_ERROR_CODE')
        self.code = code
        super().__init__(code)


def _absolute(value):
    try:
        path = Path(value).absolute()
    except (TypeError, ValueError, OSError):
        raise WorkspaceBlocked('WORKSPACE_PATH_INVALID') from None
    if '..' in path.parts or (os.name == 'nt' and
            (str(path).startswith('\\\\') or len(path.drive) != 2)):
        raise WorkspaceBlocked('WORKSPACE_PATH_INVALID')
    return path


def _plain_existing(value):
    path = _absolute(value)
    try:
        for item in reversed((path, *path.parents)):
            info = item.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
                raise WorkspaceBlocked('WORKSPACE_PATH_LINKED')
        return path.resolve(strict=True)
    except OSError:
        raise WorkspaceBlocked('WORKSPACE_PATH_INVALID') from None


def _overlap(first, second):
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _location(kit, workspace):
    kit = _plain_existing(kit)
    workspace = _absolute(workspace)
    parent = _plain_existing(workspace.parent)
    workspace = parent / workspace.name
    if (not kit.is_dir() or not parent.is_dir() or workspace.name != WORKSPACE_NAME
            or parent == Path(parent.anchor) or _overlap(kit, workspace)
            or any(part.casefold() == 'o3data' for part in workspace.parts)):
        raise WorkspaceBlocked('WORKSPACE_LOCATION_INVALID')
    # The active interpreter and its base installation are never state storage.
    for value in (sys.prefix, sys.base_prefix, Path(sys.executable).parent):
        runtime = Path(value).resolve()
        if _overlap(workspace, runtime):
            raise WorkspaceBlocked('WORKSPACE_LOCATION_INVALID')
    return kit, workspace


def _marker(workspace):
    path = workspace / MARKER_FILENAME
    try:
        path.lstat()
    except FileNotFoundError:
        raise WorkspaceBlocked('WORKSPACE_UNOWNED') from None
    except OSError:
        raise WorkspaceBlocked('WORKSPACE_IO_FAILED') from None
    path = _plain_existing(path)
    try:
        if not path.is_file() or path.stat().st_size > 4096:
            raise WorkspaceBlocked('WORKSPACE_MARKER_INVALID')
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        raise WorkspaceBlocked('WORKSPACE_MARKER_INVALID') from None
    if (type(value) is not dict
            or set(value) != {'format_version', 'build_id', 'purpose', 'workspace_id',
                              'database_initialized'}
            or type(value['format_version']) is not int or value['format_version'] != 1
            or value['build_id'] not in COMPATIBLE_OWNER_BUILDS or value['purpose'] != 'PERSISTENT_WORKSPACE'
            or type(value['database_initialized']) is not bool
            or type(value['workspace_id']) is not str
            or re.fullmatch('[0-9a-f]{32}', value['workspace_id']) is None):
        raise WorkspaceBlocked('WORKSPACE_MARKER_INVALID')
    return value


def _state_entries(state):
    """Check only immediate entries; never walk the content-addressed archive."""
    state = _plain_existing(state)
    if not state.is_dir():
        raise WorkspaceBlocked('WORKSPACE_LAYOUT_INVALID')
    try:
        entries = {}
        for entry in state.iterdir():
            path = _plain_existing(entry)
            entries[entry.name] = path
        for name in ('o3.sqlite', 'o3.sqlite-wal', 'o3.sqlite-shm', 'workspace.json',
                     'workspace-rules.json', '.o3-gui.lock'):
            if name in entries and not entries[name].is_file():
                raise WorkspaceBlocked('WORKSPACE_LAYOUT_INVALID')
        if 'archive' in entries and not entries['archive'].is_dir():
            raise WorkspaceBlocked('WORKSPACE_LAYOUT_INVALID')
        return state, entries
    except OSError:
        raise WorkspaceBlocked('WORKSPACE_IO_FAILED') from None


def validate_workspace(kit, workspace):
    """Validate ownership/layout without opening the database or source paths."""
    _, workspace = _location(kit, workspace)
    workspace = _plain_existing(workspace)
    if not workspace.is_dir():
        raise WorkspaceBlocked('WORKSPACE_LAYOUT_INVALID')
    _marker(workspace)
    state, _ = _state_entries(workspace / 'state')
    logs = _plain_existing(workspace / 'logs')
    if not logs.is_dir():
        raise WorkspaceBlocked('WORKSPACE_LAYOUT_INVALID')
    return state, logs


def prepare_workspace(kit, project=None):
    """Create one new owned workspace, or preserve and validate the existing one.

    project is the caller-selected parent directory; omitting it uses kit.parent.
    An existing unmarked directory is never adopted, even when empty.
    """
    kit = _plain_existing(kit)
    parent = kit.parent if project is None else _plain_existing(project)
    _, workspace = _location(kit, parent / WORKSPACE_NAME)
    try:
        workspace.mkdir()
    except FileExistsError:
        validate_workspace(kit, workspace)
        return workspace
    except OSError:
        raise WorkspaceBlocked('WORKSPACE_IO_FAILED') from None
    try:
        (workspace / 'state').mkdir()
        (workspace / 'logs').mkdir()
        marker = {'format_version': 1, 'build_id': BUILD_ID,
                  'purpose': 'PERSISTENT_WORKSPACE', 'workspace_id': uuid.uuid4().hex,
                  'database_initialized': False}
        # Publish ownership last. An interrupted unmarked creation is not adopted.
        with (workspace / MARKER_FILENAME).open('x', encoding='utf-8') as stream:
            json.dump(marker, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise WorkspaceBlocked('WORKSPACE_IO_FAILED') from None
    validate_workspace(kit, workspace)
    return workspace


def _configuration(entries):
    path = entries.get('workspace.json')
    if path is None:
        return {}
    try:
        if path.stat().st_size > 256 * 1024:
            raise WorkspaceBlocked('WORKSPACE_CONFIG_INVALID')
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        raise WorkspaceBlocked('WORKSPACE_CONFIG_INVALID') from None
    if type(value) is not dict:
        raise WorkspaceBlocked('WORKSPACE_CONFIG_INVALID')
    for name in ('source_root', 'backup_root'):
        if name in value and (type(value[name]) is not str or len(value[name]) > 32767
                              or '\x00' in value[name]):
            raise WorkspaceBlocked('WORKSPACE_CONFIG_INVALID')
    if 'paused' in value and type(value['paused']) is not bool:
        raise WorkspaceBlocked('WORKSPACE_CONFIG_INVALID')
    return value


def validate_database_for_open(state):
    """Call only while holding the GUI lock; no Engine or migration is invoked.

    A missing database is allowed only before the first startup, when the state
    directory is empty apart from the lock. Existing data is never recreated.
    """
    state, entries = _state_entries(state)
    marker = _marker(state.parent)
    database = entries.get('o3.sqlite')
    if database is None:
        if marker['database_initialized'] or set(entries) - {'.o3-gui.lock'}:
            raise WorkspaceBlocked('WORKSPACE_DATABASE_MISSING')
        return
    configuration = _configuration(entries)
    connection = None
    try:
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (2, 3, 4):
            raise WorkspaceBlocked('WORKSPACE_SCHEMA_UNSUPPORTED')
        row = connection.execute("SELECT value FROM engine_meta WHERE key='identity_scopes'").fetchone()
        scopes = json.loads(row[0]) if row else None
        if (not isinstance(scopes, dict) or set(scopes) != set(IDENTITY_SCOPES)
                or any(not isinstance(v, str) or not v.strip() or len(v) > 240 for v in scopes.values())):
            raise WorkspaceBlocked('WORKSPACE_IDENTITY_MISMATCH')
        populated = (connection.execute('SELECT 1 FROM records LIMIT 1').fetchone()
                     or connection.execute('SELECT 1 FROM observed_files LIMIT 1').fetchone())
        if populated and not configuration.get('source_root'):
            raise WorkspaceBlocked('WORKSPACE_CONFIG_INVALID')
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise WorkspaceBlocked('WORKSPACE_DATABASE_INVALID') from None
    finally:
        if connection is not None:
            connection.close()


def mark_database_initialized(state):
    """Publish successful first initialization while the child holds its GUI lock.

    Call after Engine readiness and before reporting application readiness. The
    durable bit prevents loss of all state contents from resembling first use.
    """
    state, entries = _state_entries(state)
    if 'o3.sqlite' not in entries:
        raise WorkspaceBlocked('WORKSPACE_DATABASE_MISSING')
    validate_database_for_open(state)
    marker = _marker(state.parent)
    if marker['database_initialized'] and marker['build_id'] == BUILD_ID:
        return
    marker['database_initialized'] = True
    marker['build_id'] = BUILD_ID
    target = state.parent / MARKER_FILENAME
    temporary = target.with_name(MARKER_FILENAME + '.writing-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(marker, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        raise WorkspaceBlocked('WORKSPACE_IO_FAILED') from None
    finally:
        if temporary.exists():
            temporary.unlink()
