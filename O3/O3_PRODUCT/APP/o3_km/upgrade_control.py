"""Cooperative control for one owned upgrade, independent of the Qt event queue."""
from contextlib import contextmanager
import sqlite3
import threading
import time


class UpgradeStopped(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code + ': saved workspace upgrade stopped safely')


class UpgradeControl:
    # Recovery limit, not a promised launch time. Ordinary startup keeps its
    # original watchdog. Only an explicit protected upgrade gets this budget.
    MAX_SECONDS = 300

    def __init__(self):
        self.stopped = threading.Event()
        self.deadline = None

    def begin(self):
        self.check()
        self.deadline = time.monotonic() + self.MAX_SECONDS

    def stop(self):
        self.stopped.set()

    def check(self):
        if self.stopped.is_set():
            raise UpgradeStopped('UPGRADE_CANCELLED')
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise UpgradeStopped('UPGRADE_TIMEOUT')

    def end(self):
        self.deadline = None


@contextmanager
def interruptible_sqlite(db, check=None):
    """Cancel long SQLite checks on their owning thread; restore the handler."""
    if check is None:
        yield db
        return
    stopped = []
    def poll():
        try:
            check()
            return 0
        except Exception as exc:
            stopped.append(exc)
            return 1
    check()
    db.set_progress_handler(poll, 10000)
    try:
        yield db
    except sqlite3.Error:
        if stopped:
            raise stopped[0] from None
        raise
    finally:
        db.set_progress_handler(None, 0)
