"""Archive validation still rejects missing, conflicting, changed and linked evidence."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
from o3_km import migration

report = {'checks': {}}
def rejected(fn):
    try:
        fn()
    except RuntimeError as error:
        assert str(error).startswith('INTEGRITY_FAILED'), str(error)
    else:
        raise AssertionError('Damaged evidence was accepted')

with tempfile.TemporaryDirectory(prefix='o3-archive-check-') as td:
    state = Path(td)/'state'
    content = b'BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n'
    sha = hashlib.sha256(content).hexdigest()
    relative = 'archive/'+sha[:2]+'/'+sha+'.ics'
    file = state/relative
    file.parent.mkdir(parents=True)
    file.write_bytes(content)
    with sqlite3.connect(':memory:') as db:
        for table in ('record_versions', 'source_observations'):
            db.execute('CREATE TABLE '+table+'(archive_relpath TEXT,source_sha TEXT)')
            db.executemany('INSERT INTO '+table+' VALUES(?,?)', [(relative,sha)]*1500)
        with patch.object(migration, '_file_hash', wraps=migration._file_hash) as hashed:
            migration._validate_archive(state, db)
            assert hashed.call_count == 1
        report['checks']['shared_calendar_evidence_hashed_once_per_validation'] = True
        # A different expected hash on the SAME path must not be hidden by deduplication.
        db.execute('INSERT INTO source_observations VALUES(?,?)', (relative,'0'*64))
        rejected(lambda:migration._validate_archive(state,db))
        db.execute("DELETE FROM source_observations WHERE source_sha=?", ('0'*64,))
        report['checks']['conflicting_claim_in_either_table_rejected'] = True
        # Each new verification reads the bytes again; there is no persisted cache.
        file.write_bytes(b'changed')
        rejected(lambda:migration._validate_archive(state,db))
        file.unlink()
        rejected(lambda:migration._validate_archive(state,db))
        file.write_bytes(content)
        migration._validate_archive(state,db)
        report['checks']['changed_and_missing_bytes_rejected_on_later_verification'] = True
        for rel in ('../outside.ics', 'workspace.json', 'archive/../outside.ics'):
            db.execute('INSERT INTO source_observations VALUES(?,?)',(rel,sha))
            rejected(lambda:migration._validate_archive(state,db))
            db.execute('DELETE FROM source_observations WHERE archive_relpath=?',(rel,))
        report['checks']['unsafe_observation_only_paths_rejected'] = True
        outside=Path(td)/'outside.ics';outside.write_bytes(content)
        file.unlink();file.symlink_to(outside)
        rejected(lambda:migration._validate_archive(state,db))
        file.unlink();file.write_bytes(content)
        report['checks']['linked_archive_file_rejected'] = True
        # A reference present only in record_versions still receives validation.
        missing='archive/ff/'+'f'*64+'.json'
        db.execute('INSERT INTO record_versions VALUES(?,?)',(missing,'f'*64))
        rejected(lambda:migration._validate_archive(state,db))
        report['checks']['version_only_missing_reference_rejected'] = True
        db.execute('CREATE TABLE observed_files(kind TEXT,path TEXT)')
        source=Path(td)/'O3Data'
        db.executemany('INSERT INTO observed_files VALUES(?,?)',
                       [('Email',str(source/'Mail/Inbox'/f'{n}.json')) for n in range(1000)])
        roots=migration._source_roots(db)
        assert roots=={source.resolve()}
        try:
            migration._no_overlap(source.resolve()/'backup',roots)
        except RuntimeError as error:
            assert str(error).startswith('BACKUP_REQUIRED')
        else:raise AssertionError('Source overlap allowed')
        report['checks']['canonical_source_boundaries_preserved'] = True
report['passed']=True
out=Path(__file__).resolve().parents[1]/'evidence/archive_verification.json'
out.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
