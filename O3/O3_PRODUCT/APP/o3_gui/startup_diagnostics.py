"""Bounded startup evidence without exception text, local paths or source data."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import sys

FILENAME = 'O3_ENGINE_STARTUP_DIAGNOSTIC.json'
APP = Path(__file__).resolve().parents[1]
CODES = frozenset(('ACTOR_UNAVAILABLE STATE_PATH_UNSAFE STATE_SCHEMA_UNSUPPORTED '
    'STATE_IDENTITY_MISSING STATE_INTEGRITY_FAILED STATE_ALREADY_OPEN STATE_LOCKED '
    'STATE_NOT_READY SCHEMA_INCOMPATIBLE LEGACY_CONFIG_CHANGED INTEGRITY_FAILED '
    'BACKUP_REQUIRED BACKUP_VERIFICATION_FAILED MIGRATION_BLOCKED INVALID_VALUE '
    'INVALID_TYPE KM_FAILURE REVISION_CONFLICT UPGRADE_CANCELLED UPGRADE_TIMEOUT').split())


def safe_exception(exc):
    # Never retain a traceback object, frame locals, raw messages or filenames
    # from OSError. They can contain addresses, operational records or usernames.
    typ = type(exc)
    known = typ.__module__ in ('builtins', 'sqlite3', 'json.decoder') or typ.__module__.startswith(('o3_gui.', 'o3_km.', 'o3_core.'))
    result = {'exception_type': typ.__name__ if known else 'OtherException', 'frames': []}
    code = getattr(exc, 'code', None)
    if not isinstance(code, str) or code not in CODES:
        candidate = str(exc).split(':', 1)[0]
        code = candidate if candidate in CODES else None
    if code: result['error_code'] = code
    for key in ('errno', 'winerror', 'sqlite_errorcode'):
        value = getattr(exc, key, None)
        if type(value) is int: result[key] = value
    tb = exc.__traceback__
    while tb is not None:
        file = Path(tb.tb_frame.f_code.co_filename)
        try:
            relative = file.resolve().relative_to(APP).as_posix()
        except (ValueError, OSError):
            relative = None
        if relative:
            result['frames'].append({'file': relative, 'line': tb.tb_lineno})
        tb = tb.tb_next
    result['frames'] = result['frames'][-16:]
    return result


class StartupDiagnostics:
    def __init__(self, state):
        state = Path(state)
        self.path = (state.parent/'logs' if state.name == 'state' and (state.parent/'workspace-owner.json').is_file() else state.parent/(state.name+'-startup-logs')) / FILENAME
        self.lock = threading.RLock()
        self.worker_thread = threading.get_ident()
        self.last_progress_save = 0.0
        self.started = time.monotonic()
        self.data = {'format': 'o3-engine-startup/1', 'diagnostic_build': 'alpha5.3',
            'started_utc': datetime.now(timezone.utc).isoformat(), 'status': 'running',
            'stage': 'session_construction', 'events': [], 'errors': [],
            'privacy': 'No exception messages, absolute paths, account names, credentials, source records or frame locals.'}
        self.phase('session_construction')

    def phase(self, stage):
        with self.lock:
            self.data['stage'] = stage
            self.data['events'].append({'stage': stage, 'elapsed_seconds': round(time.monotonic()-self.started, 3)})
            self.data['events'] = self.data['events'][-32:]
            self.data.pop('progress', None)
            self._save()

    def progress(self, stage, counts=None):
        """Record numeric work completed, never filenames or source values."""
        with self.lock:
            changed = self.data['stage'] != stage
            if changed:
                self.data['stage'] = stage
                self.data['events'].append({'stage':stage,'elapsed_seconds':round(time.monotonic()-self.started,3)})
                self.data['events'] = self.data['events'][-32:]
            self.data['progress'] = {key:value for key,value in (counts or {}).items()
                if key in ('files_completed','files_total','bytes_completed','bytes_total')
                and type(value) is int and value >= 0}
            now = time.monotonic()
            if changed or now-self.last_progress_save >= 1:
                self.last_progress_save = now
                self._save()

    def sample(self):
        """Sample only code locations on the engine worker; never frame locals."""
        if not self.lock.acquire(blocking=False):return
        try:
            frames=[]
            frame=None
            try:
                frame=sys._current_frames().get(self.worker_thread)
                while frame is not None:
                    file=Path(frame.f_code.co_filename)
                    try: label=file.resolve().relative_to(APP).as_posix()
                    except (ValueError,OSError):
                        label='stdlib/'+file.name if file.name in (
                            'tempfile.py','shutil.py','pathlib.py','contextlib.py','threading.py') else None
                        if file.name=='__init__.py' and file.parent.name=='zipfile':label='stdlib/zipfile/__init__.py'
                    if label:frames.append({'file':label,'line':frame.f_lineno})
                    frame=frame.f_back
            except Exception:
                pass
            finally:
                del frame
            sample={'elapsed_seconds':round(time.monotonic()-self.started,3),
                    'stage':self.data['stage'],'worker_frames':frames[:16]}
            if self.data.get('progress'):sample['progress']=dict(self.data['progress'])
            self.data.setdefault('samples',[]).append(sample)
            self.data['samples']=self.data['samples'][-10:]
            self._save()
        finally:
            self.lock.release()

    def failure(self, exc):
        with self.lock:
            try: detail = safe_exception(exc)
            except Exception: detail = {'exception_type': 'DiagnosticEncodingFailed', 'frames': []}
            entry = {'stage': self.data['stage'], **detail}
            self.data['errors'].append(entry)
            self.data['errors'] = self.data['errors'][-4:]
            self._save()

    def finish(self, status):
        with self.lock:
            if status == 'timeout': self.sample()
            if self.data.get('status') != 'timeout': self.data['status'] = status
            if status == 'ready' and self.data['status'] == 'timeout': self.data['completed_after_timeout'] = True
            self._save()

    def _save(self):
        pending = None
        try:
            self.data['elapsed_seconds'] = round(time.monotonic()-self.started, 3)
            if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents): return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                    prefix='.o3-startup-', suffix='.tmp', delete=False) as stream:
                pending = Path(stream.name)
                json.dump(self.data, stream, indent=2)
            os.replace(pending, self.path)
        except Exception:
            # Diagnostics must never change startup success/failure or its gates.
            pass
        finally:
            if pending is not None:
                try: pending.unlink(missing_ok=True)
                except OSError: pass
