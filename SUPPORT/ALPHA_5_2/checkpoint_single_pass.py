"""One authoritative content audit, with path and tamper protection retained."""
from pathlib import Path
from collections import Counter
from unittest.mock import patch
import json,sqlite3,tempfile,shutil,time
from o3_gui.km_session import KMSession
from o3_km import migration
from o3_core.store import Store
from o3_km.upgrade_control import UpgradeControl,UpgradeStopped,interruptible_sqlite
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'.test_work/checkpoint_scale/original/state'
report={'scope':'Real released Alpha 4 synthetic calendar; no managed-Windows timing claim','checks':{}}
def reject(fn):
    try:fn()
    except RuntimeError:return
    raise AssertionError('Damaged evidence accepted')
with tempfile.TemporaryDirectory() as td:
    state=Path(td)/'state';shutil.copytree(SOURCE,state)
    original_hash=migration._file_hash
    hashed=[]
    def counted(path):hashed.append(Path(path));return original_hash(path)
    session=KMSession(state,actor='test:single-pass')
    with patch.object(migration,'_file_hash',counted):session.open()
    assert not any('archive' in p.parts for p in hashed),hashed
    receipt=json.loads(session.km._meta('alpha5_starter_conversion_v1'))['checkpoint']
    session.close()
    report['checks']['no_duplicate_migration_content_hash_during_conversion']=True
    reads=Counter();old_read=Path.read_bytes
    def read(path):
        if 'archive' in path.parts:reads[str(path)]+=1
        return old_read(path)
    with patch.object(Path,'read_bytes',read),patch.object(migration,'_file_hash',side_effect=AssertionError('Duplicate hashing')):
        assert migration.verify_alpha5_checkpoint(receipt['path'])['verified']
    assert reads and set(reads.values())=={1},reads
    report['checks']['every_referenced_blob_read_by_core_once']=True
    original=migration._verify_core_provenance_readonly
    def tampered(state,db,**kw):
        rel=db.execute('SELECT archive_relpath FROM record_versions LIMIT 1').fetchone()[0]
        (state/rel).write_bytes(b'Changed after ZIP extraction and boundary validation')
        return original(state,db,**kw)
    with patch.object(migration,'_verify_core_provenance_readonly',tampered):
        reject(lambda:migration.verify_alpha5_checkpoint(receipt['path']))
    report['checks']['changed_extracted_bytes_still_rejected']=True
    # The actual Core audit must reject an inconsistent hash in either table.
    for table in ('record_versions','source_observations'):
        with tempfile.TemporaryDirectory() as copy:
            restored=Path(copy);migration.verify_alpha5_checkpoint(receipt['path'],_extract_to=restored)
            with sqlite3.connect(restored/'o3.sqlite') as db:
                db.execute('UPDATE '+table+" SET source_sha=? WHERE rowid=(SELECT rowid FROM "+table+' LIMIT 1)',('0'*64,));db.commit()
                migration._validate_archive_boundaries(restored,db)
                reject(lambda:migration._verify_core_provenance_readonly(restored,db))
        report['checks'][table+'_conflicting_expected_hash_rejected']=True
    with patch.object(Store,'audit_provenance',return_value={'ok':False}):
        reject(lambda:migration.verify_alpha5_checkpoint(receipt['path']))
    report['checks']['failed_core_audit_blocks_checkpoint']=True
    # Cooperative cancellation interrupts SQLite and restores its callback.
    control=UpgradeControl();control.begin();calls=[0]
    def check():
        calls[0]+=1
        if calls[0]>2:control.stop()
        control.check()
    with sqlite3.connect(':memory:') as db:
        try:
            with interruptible_sqlite(db,check):
                db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n').fetchone()
        except UpgradeStopped as exc:assert exc.code=='UPGRADE_CANCELLED'
        else:raise AssertionError('SQL cancellation not observed')
        assert db.execute('SELECT 42').fetchone()[0]==42
    report['checks']['long_sql_checks_cancel_and_handler_is_removed']=True
    # A callback error must never waive or break verification.
    assert migration.verify_alpha5_checkpoint(receipt['path'],_progress=lambda *a:1/0)['verified']
    report['checks']['diagnostic_callback_failure_does_not_change_result']=True
report['passed']=True
(ROOT/'evidence/checkpoint_single_pass.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
