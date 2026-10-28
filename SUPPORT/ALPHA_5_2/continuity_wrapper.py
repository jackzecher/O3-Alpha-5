"""Linux integration of protected wrapper hooks, not native platform qualification.

Only the wrapper's in-memory expected-platform literal is adapted to offscreen.
Identity/dependency/platform acceptance is NOT tested or claimed by this harness.
The shipped files remain untouched. Real Qt, worker, SQLite, locking, ownership,
navigation and shutdown are exercised against temporary synthetic state.
"""
from pathlib import Path
import base64
import argparse
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
CASES=('fresh','existing_legacy_geometry','existing_hex_geometry','alpha4_upgrade','alpha4_cancel')
parser.add_argument('case', choices=CASES)
parser.add_argument('--package', type=Path, default=ROOT/'package')
parser.add_argument('--output', type=Path, default=ROOT/'evidence/continuity_wrapper')
parser.add_argument('--strict-manifests', action='store_true')
args = parser.parse_args()
PACKAGE = args.package.resolve()
KIT = PACKAGE / 'O3/O3_PRODUCT'
case = args.case
assert case in CASES

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

runner = load('alpha4_ai_runner', PACKAGE / 'AI_RUNTIME/run.py')
if args.strict_manifests: runner.verify_package()
state_api = load('alpha4_state', KIT / '_O3_WORKSPACE_STATE.py')
records = []
with tempfile.TemporaryDirectory(prefix='o3_alpha4_wrapper_test_') as temp:
    base = Path(temp)
    from continuity_support import runtime
    versions = runtime(PACKAGE, base)
    from PySide6 import QtWidgets
    from PySide6.QtCore import QTimer
    import o3_gui.logic as logic
    from o3_gui.km_client import WorkspaceLock
    from seed import seed
    from o3_km import migration
    from o3_km.catalog import CATALOG_FINGERPRINT
    entered=threading.Event()
    original_boundaries=migration._validate_archive_boundaries
    def delayed_boundaries(where,db,**kw):
        if Path(where)!=state:
            entered.set();started=time.monotonic()
            while case=='alpha4_cancel' or time.monotonic()-started<46:
                kw['_check']();time.sleep(.01)
        return original_boundaries(where,db,**kw)
    if case.startswith('alpha4_'):migration._validate_archive_boundaries=delayed_boundaries
    workspace = state_api.prepare_workspace(KIT, base)
    state = workspace / 'state'
    if case != 'fresh':
        if case.startswith('alpha4_'):
            seeded=base/'fixture/state'
            shutil.copytree(ROOT/'.test_work/checkpoint_scale/original/state',seeded)
        else:seeded = seed(base / 'fixture')
        shutil.rmtree(state)
        shutil.move(str(seeded), str(state))
        state_api.mark_database_initialized(state)
        config = logic.read_config(state)
        config['alpha4_continuity_probe'] = {'preserve': 'synthetic sentinel'}
        logic.save_config(state, config)
    with sqlite3.connect(state / 'o3.sqlite') if case != 'fresh' else tempfile.TemporaryFile() as db:
        before_count = db.execute('SELECT count(*) FROM records').fetchone()[0] if case != 'fresh' else 0

    loaded_config = None
    original_read = logic.read_config
    def read_config(path):
        global loaded_config
        loaded_config = original_read(path)
        return loaded_config
    logic.read_config = read_config

    RealApplication = QtWidgets.QApplication
    watchdog_fired = []
    class TestApplication(RealApplication):
        def __init__(self, argv):
            super().__init__(argv)
            self.watchdog = QTimer(self)
            self.watchdog.setSingleShot(True)
            def timeout():
                watchdog_fired.append(True)
                for widget in self.topLevelWidgets():
                    if hasattr(widget, 'initialized'):
                        widget.close()
            self.watchdog.timeout.connect(timeout)
            self.watchdog.start(65000 if case=='alpha4_upgrade' else 30000)
            if case=='alpha4_cancel':
                self.cancel_timer=QTimer(self);self.cancel_timer.setInterval(20)
                def cancel():
                    if entered.is_set():
                        self.cancel_timer.stop()
                        for widget in self.topLevelWidgets():
                            if hasattr(widget,'initialized'):widget.close()
                self.cancel_timer.timeout.connect(cancel);self.cancel_timer.start()
            if case != 'fresh':
                dummy = QtWidgets.QMainWindow()
                dummy.resize(1060, 800)
                geometry = bytes(dummy.saveGeometry())
                dummy.close()
                loaded_config['geometry'] = ({'window': base64.b64encode(geometry).decode(),
                                               'retained_field': 'keep'}
                                             if case == 'existing_legacy_geometry' else geometry.hex())
                logic.save_config(state, loaded_config)
    QtWidgets.QApplication = TestApplication

    source = (KIT / '_O3_WORKSPACE_APP.py').read_text()
    assert source.count('expected_platform = "windows"') == 1
    wrapper = types.ModuleType('alpha4_test_wrapper')
    exec(compile(source.replace('expected_platform = "windows"', 'expected_platform = "offscreen"'),
                 '<test-only offscreen platform adapter>', 'exec'), wrapper.__dict__)

    class Sink:
        # Satisfy the monitor's prerequisite set; these external gates are not
        # exercised here and are explicitly excluded from reported coverage.
        passed = {'identity', 'dependencies'}
        failures = []
        stop_requested = False
        classification = wrapper.ALPHA if case != 'existing_hex_geometry' else wrapper.NONNIPR_ALPHA
        def emit(self, code, **details):
            records.append({'callback': code, 'details': details})
            stage, result, _ = wrapper.SPECS[code]
            if result == 'PASS':
                self.passed.add(stage)
            if result == 'FAIL':
                self.failures.append(code)
            if code == 'WORKSPACE_READY':
                def close():
                    for widget in RealApplication.instance().topLevelWidgets():
                        if hasattr(widget, 'initialized'):
                            records.append({'window_title': widget.windowTitle(), 'initialized': widget.initialized,
                                            'has_mail_workspace': hasattr(widget, 'mail_workspace')})
                            widget.close()
                QTimer.singleShot(300, close)
        def warning(self, operation):
            self.failures.append('operation:' + operation)
        def finish(self, clean):
            ready_expected=case!='alpha4_cancel'
            return 0 if clean and ('ready' in self.passed)==ready_expected and not self.failures else 2

    wrapper.read_stop_commands = lambda commands: None
    sink = Sink()
    result = wrapper.run_workbench(KIT, state, state_api, sink)
    assert result == 0, (result, records, sink.failures)
    assert not watchdog_fired
    codes=[x['callback'] for x in records if 'callback' in x]
    if case.startswith('alpha4_'):
        assert 'WORKSPACE_UPGRADE_REQUIRED' in codes
        maintenance=next(x for x in records if x.get('callback')=='WORKSPACE_UPGRADE_REQUIRED')
        assert maintenance['details']['writer_open'] is False
        if case=='alpha4_upgrade':
            assert codes.index('WORKSPACE_UPGRADE_REQUIRED')<codes.index('ENGINE_READY')<codes.index('WORKSPACE_READY')
        else:assert 'ENGINE_READY' not in codes and 'WORKSPACE_READY' not in codes
    assert all(not x.get('initialized') or 'O3' in x['window_title'] for x in records)
    assert state_api._marker(workspace)['database_initialized']
    final = original_read(state)
    if case != 'fresh':
        assert final['alpha4_continuity_probe'] == {'preserve': 'synthetic sentinel'}
        if case == 'existing_legacy_geometry':
            assert final['geometry']['retained_field'] == 'keep'
    with sqlite3.connect(state / 'o3.sqlite') as db:
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        after_count = db.execute('SELECT count(*) FROM records').fetchone()[0]
        assert after_count == before_count
        if case.startswith('alpha4_'):
            expected=migration.ALPHA4_CATALOG_FINGERPRINT if case=='alpha4_cancel' else CATALOG_FINGERPRINT
            assert db.execute("SELECT value FROM engine_meta WHERE key='km_schema_contract'").fetchone()[0]==expected
    test_lock = WorkspaceLock(state)
    assert test_lock.tryLock(0), 'Lock not released by clean shutdown'
    test_lock.unlock()

result = {'case': case, 'result': 'PASS_LINUX_WRAPPER_INTEGRATION',
          'scope': 'Synthetic Qt offscreen adaptation; not Windows/NIPR acceptance',
          'excluded': ['native executable', 'Windows platform', 'runtime identity acceptance', 'NIPR host policy'],
          'shipped_startup_modified': False, 'runtime': versions,
          'record_count_preserved': before_count, 'sqlite_integrity': 'ok',
          'schema': 4, 'ownership_initialized': True, 'lock_released': True,
          'callbacks': records}
output = args.output
output.mkdir(parents=True, exist_ok=True)
(output / (case + '.json')).write_text(json.dumps(result, indent=2) + '\n')
print(case + ': PASS - '+('cancelled before readiness' if case=='alpha4_cancel' else 'ready')+', clean shutdown, preserved state, released lock')
