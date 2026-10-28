"""Real Qt upgrade readiness, cancellation and recovery, including >45s work."""
from pathlib import Path
import json,shutil,sqlite3,tempfile,threading,time
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from o3_gui.app import O3Window
from o3_gui.logic import read_config
from o3_gui.km_client import WorkspaceLock
from o3_gui.km_session import KMSession
from o3_km import migration
from o3_km.catalog import CATALOG_FINGERPRINT
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'.test_work/checkpoint_scale/original/state'
OUT=ROOT/'evidence/upgrade_lifecycle_qt';OUT.mkdir(parents=True,exist_ok=True)
app=QApplication(['Synthetic protected-upgrade test']);app.setStyle('Fusion')
report={'scope':'Actual Linux offscreen Qt; controlled delays; not Windows/NIPR acceptance','checks':{}}
def spin(until,seconds):
    deadline=time.monotonic()+seconds
    while not until():
        app.processEvents();time.sleep(.005)
        assert time.monotonic()<deadline,'Qt operation exceeded test deadline'
    app.processEvents()
def catalog(state):
    with sqlite3.connect((state/'o3.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        return db.execute("SELECT value FROM engine_meta WHERE key='km_schema_contract'").fetchone()[0]
for case in ('cancel','timeout','long_success'):
    with tempfile.TemporaryDirectory(prefix='o3-upgrade-qt-') as td:
        state=Path(td)/'state';shutil.copytree(SOURCE,state)
        config_before=(state/'workspace.json').read_bytes()
        session=KMSession(state);pre=session.open(defer_starter_conversion=True)
        assert pre['mode']=='starter_conversion_required' and session.engine is None
        session.close()
        entered=threading.Event();maintenance=[];messages=[];ready=[];ticks=[];block=[True]
        original=migration._validate_archive_boundaries
        def delayed(where,db,**kw):
            if Path(where)!=state and block[0]:
                entered.set();start=time.monotonic()
                while case!='long_success' or time.monotonic()-start<46:
                    kw['_check']();time.sleep(.01)
            return original(where,db,**kw)
        class Window(O3Window):
            def on_maintenance(self,data):
                assert self.worker.engine is None
                maintenance.append(data)
                super().on_maintenance(data)
            def on_ready(self,data):
                ready.append(True);super().on_ready(data)
            def show_information(self,*args,**kwargs):messages.append(args)
        lock=WorkspaceLock(state);assert lock.tryLock(0)
        with patch.object(migration,'_validate_archive_boundaries',delayed):
            window=Window(state,read_config(state),lock);window.show()
            if case=='timeout':window.worker.upgrade_control.MAX_SECONDS=2
            pulse=QTimer(window);pulse.setInterval(20);pulse.timeout.connect(lambda:ticks.append(time.monotonic()));pulse.start()
            try:
                spin(entered.is_set,15)
                assert maintenance and window.storage_maintenance and not window.initialized
                assert window.startup_timer.interval()==45000 and not window.startup_timer.isActive()
                assert window.upgrade_dialog is not None and window.upgrade_dialog.isVisible()
                if case=='cancel':
                    start=time.monotonic();window.close();spin(lambda:window.allow_close,5)
                    assert time.monotonic()-start<3 and not window.thread.isRunning()
                    assert catalog(state)==migration.ALPHA4_CATALOG_FINGERPRINT
                    assert (state/'workspace.json').read_bytes()==config_before
                    assert not list((Path(td)/'O3_Recovery').glob('*.partial'))
                    report['checks']['close_cancels_worker_and_preserves_old_workspace']=True
                elif case=='timeout':
                    spin(lambda:bool(messages),10)
                    assert not ready and not window.initialized and not window.storage_maintenance
                    assert window.worker.session.engine is None
                    assert catalog(state)==migration.ALPHA4_CATALOG_FINGERPRINT
                    assert window.last_error[0]=='upgrade_starters' and 'UPGRADE_TIMEOUT' in window.last_error[1]
                    report['checks']['upgrade_limit_returns_recoverable_failure_without_ready']=True
                    block[0]=False;window.worker.upgrade_control.MAX_SECONDS=300
                    window.begin_starter_upgrade();spin(lambda:window.initialized or window.last_error,15)
                    assert window.initialized and not window.last_error and ready==[True]
                    report['checks']['retry_in_same_window_reaches_ready_and_clears_failure']=True
                else:
                    assert window.upgrade_dialog.grab().save(str(OUT/'upgrade_progress.png'))
                    began=time.monotonic();spin(lambda:window.initialized or window.last_error,55)
                    assert window.initialized and not window.last_error and ready==[True]
                    assert time.monotonic()-began>=44
                    assert len(ticks)>1000 and window.upgrade_dialog is None
                    assert catalog(state)==CATALOG_FINGERPRINT
                    assert read_config(state)['alpha3']['drafts']==json.loads(config_before)['alpha3']['drafts']
                    end=time.monotonic()+.5
                    while time.monotonic()<end:app.processEvents();time.sleep(.005)
                    assert window.grab().save(str(OUT/'ready.png'))
                    report['checks']['upgrade_over_45_seconds_keeps_gui_responsive_and_reaches_ready']=True
                    report['checks']['ordinary_startup_timer_remains_45000_ms']=True
            finally:
                window.close();spin(lambda:window.allow_close,10)
                assert not window.thread.isRunning()
                pulse.stop();window.deleteLater();app.processEvents()
        again=WorkspaceLock(state);assert again.tryLock(0);again.unlock()
        if case in ('cancel','timeout'):
            session=KMSession(state);session.open();session.close()
            assert catalog(state)==CATALOG_FINGERPRINT
            report['checks'][case+'_can_retry_successfully']=True
report['passed']=True
(OUT/'checks.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
