"""Locate a blocked verifier, report actual progress, preserve behavior/privacy."""
from pathlib import Path
import json
import shutil
import threading
import tempfile
import time
from unittest.mock import patch
from o3_gui.km_session import KMSession
from o3_gui.startup_diagnostics import StartupDiagnostics
from o3_km import migration

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'.test_work/checkpoint_scale/original/state'
report={'scope':'Synthetic Alpha 4 fixture, deliberately blocked verifier; no target-host claim','checks':{}}
assert SOURCE.is_dir(), 'Run the genuine Alpha 4 fixture seed first'
with tempfile.TemporaryDirectory(prefix='o3-verifier-diagnostic-') as temporary:
    state=Path(temporary)/'state';shutil.copytree(SOURCE,state)
    entered=threading.Event();resume=threading.Event();result={};failure=[]
    original=migration._validate_archive_boundaries
    def blocked_archive(where,db,**kwargs):
        if Path(where)!=state:
            entered.set()
            assert resume.wait(15),'Test did not release its verifier block'
        return original(where,db,**kwargs)
    def run():
        try:
            diagnostic=StartupDiagnostics(state);result['diagnostic']=diagnostic
            with patch.object(migration,'_validate_archive_boundaries',blocked_archive):
                session=KMSession(state,actor='synthetic:diagnostic',startup_diagnostics=diagnostic)
                try:
                    session.open()
                    result['checkpoint']=json.loads(session.km._meta('alpha5_starter_conversion_v1'))['checkpoint']['path']
                finally:session.close()
            diagnostic.finish('ready')
        except BaseException as exc:failure.append(repr(exc))
    worker=threading.Thread(target=run);worker.start()
    try:
        assert entered.wait(15),'Verifier did not reach injected block'
        diag=result['diagnostic']
        diag.sample();diag.finish('timeout')
        data=json.loads(diag.path.read_text())
        assert data['stage']=='checkpoint_verify_archive_boundaries',data
        frames=data['samples'][-1]['worker_frames']
        assert any(f['file']=='o3_km/migration.py' for f in frames),frames
        assert any(f['file']=='o3_gui/km_session.py' for f in frames),frames
        assert not data['errors'],data
        assert str(state) not in json.dumps(data)
        assert 'synthetic:diagnostic' not in json.dumps(data)
        report['checks']['blocked_worker_code_location_at_timeout']=True
        report['checks']['report_excludes_source_paths_and_frame_values']=True
    finally:
        resume.set();worker.join(20)
    assert not worker.is_alive() and not failure,failure
    data=json.loads(result['diagnostic'].path.read_text())
    assert data['status']=='timeout' and data['completed_after_timeout']
    report['checks']['timeout_is_not_relabelled_success_after_unblocking']=True
    stages=[];counts=[]
    def progress(stage,values):
        stages.append(stage)
        if values and stage=='checkpoint_verify_extract':counts.append(dict(values))
    verified=migration.verify_alpha5_checkpoint(result['checkpoint'],_progress=progress)
    assert verified['verified']
    assert counts[-1]['files_completed']==counts[-1]['files_total']
    assert counts[-1]['bytes_completed']==counts[-1]['bytes_total']
    assert all(a['bytes_completed']<=b['bytes_completed'] for a,b in zip(counts,counts[1:]))
    assert {'checkpoint_verify_extract','checkpoint_verify_database_open',
            'checkpoint_verify_database_schema','checkpoint_verify_database_identity',
            'checkpoint_verify_archive_boundaries','checkpoint_verify_provenance',
            'checkpoint_verify_ownership','checkpoint_verify_cleanup','checkpoint_verify_complete'}<=set(stages)
    report['checks']['all_verifier_substages_and_monotonic_byte_counts']=True
    def bad_diagnostic(*args):raise OSError('Synthetic logging failure')
    assert migration.verify_alpha5_checkpoint(result['checkpoint'],_progress=bad_diagnostic)==verified
    report['checks']['logging_failure_does_not_change_verification_result']=True
report['passed']=True
out=ROOT/'evidence/verifier_diagnostics.json';out.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
