"""Exercise the real Qt readiness timer and shutdown on the populated upgrade."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
import time
from PySide6.QtWidgets import QApplication
from o3_gui.app import O3Window
from o3_gui.km_client import WorkspaceLock
from o3_gui.logic import read_config

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--fixture', type=Path, required=True)
args = parser.parse_args()
out = ROOT/'evidence/startup_checkpoint_qt'
out.mkdir(parents=True, exist_ok=True)
app = QApplication(['O3 Alpha 5.1 synthetic upgrade check'])
app.setStyle('Fusion')
with tempfile.TemporaryDirectory(prefix='o3-scale-qt-') as tmp:
    state=Path(tmp)/'state'
    shutil.copytree(args.fixture/'original/state',state)
    config=read_config(state)
    lock=WorkspaceLock(state)
    assert lock.tryLock(0)
    started=time.monotonic()
    window=O3Window(state,config,lock)
    signals=[]
    window.startup_finished.connect(signals.append)
    assert window.startup_timer.interval()==45000
    window.show()
    try:
        while not window.initialized and not window.last_error:
            app.processEvents()
            if time.monotonic()-started>45:
                raise TimeoutError('Native Qt upgrade exceeded unchanged readiness limit')
            time.sleep(.005)
        assert window.initialized and not window.last_error,window.last_error
        elapsed=time.monotonic()-started
        assert signals==[True] and not window.startup_timer.isActive(),signals
        assert window.summary_data['counts']['Event']==1500
        until=time.monotonic()+.4
        while time.monotonic()<until:app.processEvents();time.sleep(.005)
        assert window.grab().save(str(out/'actual_qt.png'))
        diagnostic=json.loads(window.worker.startup_diagnostics.path.read_text())
        assert diagnostic['status']=='ready' and diagnostic['diagnostic_build']=='alpha5.1'
        assert any(e['stage']=='checkpoint_source_archive_verification' for e in diagnostic['events'])
        assert any(e['stage']=='checkpoint_verify' for e in diagnostic['events'])
        report={'scope':'Actual Linux offscreen Qt, genuine Alpha 4 synthetic calendar state',
                'ready_seconds':round(elapsed,3),'timer_ms':45000,'ready_signals':signals,
                'event_count':1500,'diagnostic_status':diagnostic['status'],
                'checkpoint_substages_recorded':True}
    finally:
        window.close()
        deadline=time.monotonic()+20
        while not window.allow_close and time.monotonic()<deadline:
            app.processEvents();time.sleep(.005)
        assert not window.thread.isRunning() and window.allow_close
    again=WorkspaceLock(state)
    assert again.tryLock(0)
    again.unlock()
    assert read_config(state)['alpha3']['drafts']==config['alpha3']['drafts']
    report.update(clean_shutdown=True,lock_released=True,saved_draft_preserved=True,passed=True)
(out/'checks.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
