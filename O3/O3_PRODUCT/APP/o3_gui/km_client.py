"""Qt transport and stable lifecycle lock, not a second KM data/semantic layer."""
from __future__ import annotations
import hashlib
import os
import weakref
from pathlib import Path
from PySide6.QtCore import QObject, Signal, QLockFile


class WorkspaceLock:
    """Keep ownership outside the state directory that offline restore replaces.

    Also hold the former GUI lock during normal operation so a running v0.4
    process cannot be silently joined. Neither lock is removed based on age.
    """
    def __init__(self, state: Path):
        self.state = Path(state)
        key = hashlib.sha256(os.path.normcase(str(self.state.resolve())).encode()).hexdigest()[:16]
        self.stable = QLockFile(str(self.state.parent / ('.o3-workspace-' + key + '.lock')))
        self.legacy = QLockFile(str(self.state / '.o3-gui.lock'))
        for lock in (self.stable, self.legacy): lock.setStaleLockTime(0)

    def setStaleLockTime(self, value):
        # Retain compatibility with the existing supervised launch protocol.
        self.stable.setStaleLockTime(0); self.legacy.setStaleLockTime(0)

    def tryLock(self, timeout=0):
        if not self.stable.tryLock(timeout): return False
        if not self.legacy.tryLock(0):
            self.stable.unlock(); return False
        return True

    def release_legacy(self):
        self.legacy.unlock()

    def reacquire_legacy(self):
        if not self.stable.isLocked(): return False
        self.legacy = QLockFile(str(self.state / '.o3-gui.lock'))
        self.legacy.setStaleLockTime(0)
        return self.legacy.tryLock(0)

    def isLocked(self):
        return self.stable.isLocked()

    def unlock(self):
        self.legacy.unlock(); self.stable.unlock()


class KMClient(QObject):
    """Correlates asynchronous worker replies; owns no SQL, facts or evaluator."""
    changed = Signal(object)
    status = Signal(str)

    def __init__(self, submit, parent=None):
        super().__init__(parent)
        self.submit = submit
        self.contract = None
        self.workspace_id = None
        self.revision = None
        self.epoch = None
        self.pending = {}
        self.serial = 0
        self.maintenance = False
        self.operations = set()

    def configure(self, capabilities):
        self.contract = capabilities['contract']
        self.workspace_id = capabilities['workspace_id']
        self.revision = capabilities['workspace_revision']
        self.epoch = capabilities['core_epoch']
        self.operations = set(capabilities['operations'])

    def request(self, operation, params=None, callback=None, *, expected=None, owner=None, as_of=None):
        if self.maintenance or not self.contract:
            raise RuntimeError('Workspace is in protected maintenance; no KM requests are accepted')
        if operation not in self.operations:
            raise ValueError('Operation is not advertised by this KM version: ' + operation)
        if len(self.pending) >= 128:
            raise RuntimeError('Too many queued requests; finish the current operation first')
        self.serial += 1
        token = self.serial
        request = {'contract': self.contract, 'workspace_id': self.workspace_id,
                   'operation': operation, 'params': params or {}}
        if expected is not None: request['expected_workspace_revision'] = expected
        if as_of is not None: request['as_of'] = as_of
        self.pending[token] = (callback, weakref.ref(owner) if owner is not None else None)
        self.submit('km', {'token': token, 'request': request})
        return token

    def invalidate(self):
        # Drop pre-maintenance presentation callbacks, never queued KM writes.
        # Worker FIFO still completes accepted writes before restore/close.
        self.pending.clear()

    def receive(self, data):
        if data.get('token') not in self.pending:
            return
        response = data['response']
        if response.get('workspace_id') == self.workspace_id:
            self.revision = response.get('workspace_revision', self.revision)
            self.epoch = response.get('core_epoch', self.epoch)
        callback, owner = self.pending.pop(data['token'], (None, None))
        if callback is not None and (owner is None or owner() is not None):
            callback(response)
        if response.get('ok') and response.get('result', {}).get('changed'):
            self.changed.emit(response)

    def cancel_owner(self, owner):
        for token, (_, ref) in list(self.pending.items()):
            if ref is not None and ref() is owner:
                # Discard only the closed view's callback, not accepted work.
                # The completion still advances revision/epoch and refreshes
                # live views after a mutation. Outstanding work remains bounded.
                self.pending[token] = (None, None)
