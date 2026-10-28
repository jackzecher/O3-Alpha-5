"""Actual Qt timer samples a blocked engine worker without changing readiness."""
from pathlib import Path
import json,shutil,tempfile,threading,time
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from o3_gui.app import O3Window
from o3_gui.logic import read_config
from o3_gui.km_client import WorkspaceLock
from o3_km import migration

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT.parent/'alpha5_1_fix/.test_work/checkpoint_scale/original/state'
app=QApplication(['Synthetic verifier diagnostic inspection'])
app.setStyle('Fusion')
with tempfile.TemporaryDirectory(prefix='o3-verifier-qt-') as temporary:
    state=Path(temporary)/'state';shutil.copytree(SOURCE,state)
    block=threading.Event();resume=threading.Event();original=migration._validate_archive
    def blocked(where,db):
        if Path(where)!=state:
            block.set();assert resume.wait(20)
        return original(where,db)
    lock=WorkspaceLock(state);assert lock.tryLock(0)
    with patch.object(migration,'_validate_archive',blocked):
        window=O3Window(state,read_config(state),lock);window.show()
        try:
            deadline=time.monotonic()+15
            while True:
                app.processEvents();time.sleep(.01)
                diag=window.worker.startup_diagnostics
                if diag and diag.data.get('samples'):break
                assert time.monotonic()<deadline
            assert block.is_set() and not window.initialized and not window.last_error
            assert window.startup_timer.interval()==45000 and window.startup_timer.isActive()
            sample=diag.data['samples'][-1]
            assert sample['stage']=='checkpoint_verify_archive',sample
            assert any(f['file']=='o3_km/migration.py' for f in sample['worker_frames'])
        finally:resume.set()
        deadline=time.monotonic()+20
        while not window.initialized:
            app.processEvents();time.sleep(.01)
            assert time.monotonic()<deadline and not window.last_error,window.last_error
        assert not window.startup_sample_timer.isActive()
        window.close();deadline=time.monotonic()+20
        while not window.allow_close:
            app.processEvents();time.sleep(.01);assert time.monotonic()<deadline
        assert not window.thread.isRunning()
    again=WorkspaceLock(state);assert again.tryLock(0);again.unlock()
report={'scope':'Real Linux Qt with deliberately blocked checkpoint verifier',
        'five_second_timer_captures_worker_code_location':True,
        'normal_45_second_startup_limit_unchanged':True,
        'not_ready_during_block':True,'ready_after_release':True,
        'sampler_stops_on_readiness':True,'clean_shutdown_and_lock_release':True,'passed':True}
(ROOT/'evidence/verifier_diagnostics_qt.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
