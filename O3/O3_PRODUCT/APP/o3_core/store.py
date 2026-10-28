"""Single authoritative SQLite repository and byte-exact, content-addressed archive."""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

KINDS = ('Email', 'Event', 'Person', 'Document', 'Organization', 'LineOfEffort')

def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def encode(v: object) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

class Store:
    """Owns ALL durable state. No other robot opens its own SQLite database."""
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'archive').mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / 'o3.sqlite', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.execute('PRAGMA journal_mode=WAL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            self.db.close()
            raise RuntimeError('Unknown O3 database version: refusing unapproved migration')
        if version == 1:
            self._pre_migration_backup()
        if version not in (2, 3, 4):
            self.db.executescript('''
        CREATE TABLE IF NOT EXISTS records(
          record_id TEXT PRIMARY KEY,
          record_type TEXT NOT NULL CHECK(record_type IN ('Email','Event','Person','Document','Organization','LineOfEffort')),
          scope TEXT NOT NULL,
          identity_basis TEXT NOT NULL,
          identity_key TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          source_sha TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
          properties_json TEXT NOT NULL,
          added_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS record_by_kind ON records(record_type, active);
        CREATE TABLE IF NOT EXISTS observed_files(
          path TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          scope TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS file_members(
          path TEXT NOT NULL REFERENCES observed_files(path) ON DELETE CASCADE,
          record_id TEXT NOT NULL REFERENCES records(record_id),
          fingerprint TEXT NOT NULL,
          PRIMARY KEY(path,record_id)
        );
        CREATE INDEX IF NOT EXISTS member_record ON file_members(record_id);
        CREATE TABLE IF NOT EXISTS record_versions(
          record_id TEXT NOT NULL REFERENCES records(record_id),
          fingerprint TEXT NOT NULL,
          source_sha TEXT NOT NULL,
          archive_relpath TEXT NOT NULL,
          seen_at TEXT NOT NULL,
          PRIMARY KEY(record_id,fingerprint)
        );
        CREATE TABLE IF NOT EXISTS evaluations(
          run_id INTEGER PRIMARY KEY,
          record_id TEXT NOT NULL REFERENCES records(record_id),
          input_sha TEXT NOT NULL,
          outcome_json TEXT NOT NULL,
          evaluated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relationships(
          owner TEXT NOT NULL,
          source_id TEXT NOT NULL REFERENCES records(record_id),
          predicate TEXT NOT NULL,
          target_id TEXT NOT NULL REFERENCES records(record_id),
          evidence_json TEXT NOT NULL,
          PRIMARY KEY(owner,source_id,predicate,target_id)
        );
        CREATE INDEX IF NOT EXISTS rel_source ON relationships(source_id);
        CREATE INDEX IF NOT EXISTS rel_target ON relationships(target_id);
        CREATE TABLE IF NOT EXISTS engine_meta(
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO engine_meta VALUES('reconcile_needed','0');
        CREATE TABLE IF NOT EXISTS derived_evidence(
          producer TEXT NOT NULL,
          source_id TEXT NOT NULL REFERENCES records(record_id),
          fingerprint TEXT NOT NULL,
          result_json TEXT NOT NULL,
          PRIMARY KEY(producer,source_id)
        );
        PRAGMA user_version=1;
        ''')
        if version not in (2, 3, 4):
            self._upgrade_provenance(version)


    def _pre_migration_backup(self) -> None:
        """An on-disk, checked SQLite snapshot before changing an existing v1 DB."""
        backups = self.root / 'backups'
        backups.mkdir(exist_ok=True)
        target = backups / ('o3_pre_v2_' + str(time.time_ns()) + '.sqlite')
        temporary = target.with_suffix('.tmp')
        try:
            out = sqlite3.connect(temporary)
            try:
                self.db.backup(out)
                if out.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Pre-migration backup failed quick_check')
            finally:
                # The SQLite connection context manager does not close the
                # handle; explicitly closing also checkpoints any backup WAL.
                out.close()
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _upgrade_provenance(self, old_version: int) -> None:
        """Add append-only evidence trails, leaving existing tables and IDs unchanged."""
        trigger_sql = (
            """CREATE TRIGGER provenance_record_insert AFTER INSERT ON records BEGIN
                INSERT INTO record_history(record_id,change_type,record_type,fingerprint,source_sha,active,
                    properties_json,added_json,changed_at)
                VALUES (NEW.record_id,'created',NEW.record_type,NEW.fingerprint,NEW.source_sha,
                    NEW.active,NEW.properties_json,NEW.added_json,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            END""",
            """CREATE TRIGGER provenance_record_update AFTER UPDATE ON records
                WHEN OLD.fingerprint!=NEW.fingerprint OR OLD.active!=NEW.active OR
                     OLD.source_sha!=NEW.source_sha OR OLD.properties_json!=NEW.properties_json OR
                     OLD.added_json!=NEW.added_json
                BEGIN
                INSERT INTO record_history(record_id,change_type,record_type,fingerprint,source_sha,active,
                    properties_json,added_json,changed_at)
                VALUES (NEW.record_id,CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'deactivated'
                   WHEN OLD.active=0 AND NEW.active=1 THEN 'reactivated' ELSE 'updated' END,
                   NEW.record_type,NEW.fingerprint,NEW.source_sha,NEW.active,NEW.properties_json,
                   NEW.added_json,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            END""",
            """CREATE TRIGGER provenance_relationship_insert AFTER INSERT ON relationships BEGIN
                INSERT INTO relationship_history(owner,source_id,predicate,target_id,change_type,evidence_json,changed_at)
                VALUES(NEW.owner,NEW.source_id,NEW.predicate,NEW.target_id,'asserted',NEW.evidence_json,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            END""",
            """CREATE TRIGGER provenance_relationship_delete AFTER DELETE ON relationships BEGIN
                INSERT INTO relationship_history(owner,source_id,predicate,target_id,change_type,evidence_json,changed_at)
                VALUES(OLD.owner,OLD.source_id,OLD.predicate,OLD.target_id,'retracted',OLD.evidence_json,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            END""",
            """CREATE TRIGGER provenance_relationship_update AFTER UPDATE OF evidence_json ON relationships
                WHEN OLD.evidence_json!=NEW.evidence_json BEGIN
                INSERT INTO relationship_history(owner,source_id,predicate,target_id,change_type,evidence_json,changed_at)
                VALUES(NEW.owner,NEW.source_id,NEW.predicate,NEW.target_id,'evidence_updated',NEW.evidence_json,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            END""",
            """CREATE TRIGGER provenance_derived_insert AFTER INSERT ON derived_evidence BEGIN
                INSERT INTO derived_history(producer,source_id,change_type,fingerprint,result_json,changed_at)
                VALUES(NEW.producer,NEW.source_id,'established',NEW.fingerprint,NEW.result_json,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            END""",
            """CREATE TRIGGER provenance_derived_update AFTER UPDATE ON derived_evidence
                WHEN OLD.fingerprint!=NEW.fingerprint OR OLD.result_json!=NEW.result_json BEGIN
                INSERT INTO derived_history(producer,source_id,change_type,fingerprint,result_json,changed_at)
                VALUES(NEW.producer,NEW.source_id,'reassessed',NEW.fingerprint,NEW.result_json,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            END""",
            """CREATE TRIGGER provenance_derived_delete AFTER DELETE ON derived_evidence BEGIN
                INSERT INTO derived_history(producer,source_id,change_type,fingerprint,result_json,changed_at)
                VALUES(OLD.producer,OLD.source_id,'withdrawn',OLD.fingerprint,OLD.result_json,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            END""",
        )
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute("""CREATE TABLE source_observations(
                observation_id INTEGER PRIMARY KEY,
                path TEXT NOT NULL, record_id TEXT NOT NULL REFERENCES records(record_id),
                kind TEXT NOT NULL, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
                source_sha TEXT NOT NULL, archive_relpath TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(path, record_id, fingerprint, source_sha)
            )""")
            self.db.execute('CREATE INDEX obs_record ON source_observations(record_id)')
            self.db.execute("""CREATE TABLE record_history(
                event_id INTEGER PRIMARY KEY, record_id TEXT NOT NULL,
                change_type TEXT NOT NULL, record_type TEXT NOT NULL,
                fingerprint TEXT NOT NULL, source_sha TEXT NOT NULL,
                active INTEGER NOT NULL, properties_json TEXT NOT NULL,
                added_json TEXT NOT NULL, changed_at TEXT NOT NULL
            )""")
            self.db.execute('CREATE INDEX history_record ON record_history(record_id,event_id)')
            self.db.execute("""CREATE TABLE relationship_history(
                event_id INTEGER PRIMARY KEY, owner TEXT NOT NULL,
                source_id TEXT NOT NULL, predicate TEXT NOT NULL, target_id TEXT NOT NULL,
                change_type TEXT NOT NULL, evidence_json TEXT NOT NULL, changed_at TEXT NOT NULL
            )""")
            self.db.execute('CREATE INDEX history_link ON relationship_history(owner,source_id,predicate,target_id,event_id)')
            self.db.execute("""CREATE TABLE derived_history(
                event_id INTEGER PRIMARY KEY, producer TEXT NOT NULL, source_id TEXT NOT NULL,
                change_type TEXT NOT NULL, fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL, changed_at TEXT NOT NULL
            )""")
            self.db.execute('CREATE INDEX history_derived ON derived_history(producer,source_id,event_id)')
            if old_version == 1:
                stamp=now()
                # Existing v1 archives and observations are retained; old assertions
                # are marked as baselines, never falsely claimed to be new writes.
                self.db.execute("""INSERT INTO record_history(record_id,change_type,record_type,fingerprint,
                    source_sha,active,properties_json,added_json,changed_at)
                    SELECT record_id,'baseline_v1',record_type,fingerprint,source_sha,active,
                           properties_json,added_json,? FROM records""",(stamp,))
                self.db.execute("""INSERT INTO relationship_history(owner,source_id,predicate,target_id,
                    change_type,evidence_json,changed_at)
                    SELECT owner,source_id,predicate,target_id,'baseline_v1',evidence_json,?
                    FROM relationships""",(stamp,))
                self.db.execute("""INSERT INTO derived_history(producer,source_id,change_type,
                    fingerprint,result_json,changed_at)
                    SELECT producer,source_id,'baseline_v1',fingerprint,result_json,?
                    FROM derived_evidence""",(stamp,))
                for r in self.db.execute("""SELECT f.path,f.kind,f.scope,f.sha256,m.record_id,m.fingerprint
                        FROM observed_files f JOIN file_members m ON m.path=f.path"""):
                    sha=r['sha256']
                    rel=Path('archive')/sha[:2]/(sha+('.ics' if r['kind']=='Event' else '.json'))
                    self.db.execute("""INSERT OR IGNORE INTO source_observations(
                       path,record_id,kind,scope,fingerprint,source_sha,archive_relpath,observed_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                       (r['path'],r['record_id'],r['kind'],r['scope'],r['fingerprint'],sha,rel.as_posix(),stamp))
            for sql in trigger_sql:
                self.db.execute(sql)
            self.db.execute('PRAGMA user_version=2')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self) -> None: self.db.close()
    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

    def archive(self, raw: bytes, extension: str) -> str:
        """Publish a verified immutable blob atomically; never expose partial files."""
        sha = digest(raw)
        loc = Path('archive') / sha[:2] / (sha + '.' + extension)
        target = self.root / loc
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if digest(target.read_bytes()) != sha:
                raise RuntimeError('Archive hash mismatch: existing evidence is corrupt')
            return loc.as_posix()
        # Source writes are external and read-only. We publish a separate
        # content-addressed archive only after fsync, before DB transactions.
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.pending-',
                                             suffix='.part', delete=False) as f:
                temp_path=Path(f.name)
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            # One writer per state root is the explicit v0.x runtime contract.
            # Recheck to avoid overwriting a previously established blob.
            if target.exists():
                if digest(target.read_bytes()) != sha:
                    raise RuntimeError('Archive hash mismatch: existing evidence is corrupt')
            else:
                os.replace(temp_path,target)
                temp_path=None
            return loc.as_posix()
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def row(self, record_id: str):
        return self.db.execute('SELECT * FROM records WHERE record_id=?', (record_id,)).fetchone()

    def records(self, kind: str | None = None, active: bool = True):
        if kind is not None:
            return self.db.execute('SELECT * FROM records WHERE record_type=? AND active=? ORDER BY record_id', (kind, int(active))).fetchall()
        return self.db.execute('SELECT * FROM records WHERE active=? ORDER BY record_id', (int(active),)).fetchall()

    def upsert(self, kind: str, rid: str, scope: str, basis: str, key: str,
               fp: str, source_sha: str, properties: dict, added: dict,
               archive_rel: str | None, outcome: dict | None) -> str:
        if kind not in KINDS: raise ValueError('Unsupported Record Type')
        existing = self.row(rid)
        if existing is not None and (existing['record_type'] != kind or existing['scope'] != scope):
            raise ValueError('ID reused across Record Types or scopes')
        status = 'created' if existing is None else 'updated' if (existing['fingerprint'] != fp or not existing['active']) else 'unchanged'
        if status == 'unchanged': return status
        self.db.execute('''INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(record_id) DO UPDATE SET identity_basis=excluded.identity_basis,
          identity_key=excluded.identity_key, fingerprint=excluded.fingerprint,
          source_sha=excluded.source_sha, active=1, properties_json=excluded.properties_json,
          added_json=excluded.added_json, updated_at=excluded.updated_at''',
            (rid,kind,scope,basis,key,fp,source_sha,1,encode(properties),encode(added),now()))
        if archive_rel is not None:
            self.db.execute('INSERT OR IGNORE INTO record_versions VALUES(?,?,?,?,?)',
                            (rid,fp,source_sha,archive_rel,now()))
        if outcome is not None:
            self.db.execute('INSERT INTO evaluations(record_id,input_sha,outcome_json,evaluated_at) VALUES(?,?,?,?)',
                            (rid,source_sha,encode(outcome),now()))
        self.db.execute("UPDATE engine_meta SET value='1' WHERE key='reconcile_needed'")
        return status

    def replace_claims(self, owner: str, links: dict, *, allowed: dict[str,tuple[set[str],set[str]]]) -> int:
        """Replace one rule-owner's assertions. Claims have explicit, traceable evidence."""
        desired = []
        for key, evidence in sorted(links.items()):
            s,p,t = key
            a,b = self.row(s), self.row(t)
            if not a or not b or not a['active'] or not b['active']:
                continue
            if p not in allowed:
                raise ValueError('Predicate not in ontology: ' + p)
            sources, targets = allowed[p]
            if a['record_type'] not in sources or b['record_type'] not in targets:
                raise ValueError('Relationship violates ontology source/target: ' + p)
            desired.append((owner,s,p,t,encode(evidence)))
        before = {(r['source_id'],r['predicate'],r['target_id']):r['evidence_json']
                  for r in self.db.execute('SELECT * FROM relationships WHERE owner=?',(owner,))}
        after = {(s,p,t):ev for _,s,p,t,ev in desired}
        if before == after: return 0
        # Apply only the delta: do not generate fictitious retract/assert events
        # for unrelated links when one claim changes.
        for (s,p,t) in sorted(before.keys() - after.keys()):
            self.db.execute('DELETE FROM relationships WHERE owner=? AND source_id=? AND predicate=? AND target_id=?',
                            (owner,s,p,t))
        for (s,p,t),evidence in sorted(after.items()):
            if (s,p,t) not in before:
                self.db.execute('INSERT INTO relationships VALUES(?,?,?,?,?)',(owner,s,p,t,evidence))
            elif before[(s,p,t)] != evidence:
                self.db.execute('UPDATE relationships SET evidence_json=? WHERE owner=? AND source_id=? AND predicate=? AND target_id=?',
                                (evidence,owner,s,p,t))
        return 1

    def reconciliation_needed(self) -> bool:
        return self.db.execute("SELECT value FROM engine_meta WHERE key='reconcile_needed'").fetchone()[0]=='1'

    def mark_pending(self):
        self.db.execute("UPDATE engine_meta SET value='1' WHERE key='reconcile_needed'")

    def clear_pending(self):
        self.db.execute("UPDATE engine_meta SET value='0' WHERE key='reconcile_needed'")

    def refresh_observation(self, path: str, kind: str, scope: str,
                            stat: os.stat_result, sha: str, members: dict[str,str]):
        self.db.execute('INSERT INTO observed_files VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET '
                        'kind=excluded.kind, scope=excluded.scope,size_bytes=excluded.size_bytes,'
                        'mtime_ns=excluded.mtime_ns,sha256=excluded.sha256',
                        (path,kind,scope,stat.st_size,stat.st_mtime_ns,sha))
        self.db.execute('DELETE FROM file_members WHERE path=?',(path,))
        self.db.executemany('INSERT INTO file_members VALUES(?,?,?)',
                            [(path,rid,fp) for rid,fp in sorted(members.items())])
        # A new raw capture is evidence even when its normalized record value
        # is identical to an earlier version. This is deliberately separate
        # from record_versions, which tracks normalized record revisions.
        sha_ext = '.ics' if kind == 'Event' else '.json'
        archive_rel = (Path('archive') / sha[:2] / (sha + sha_ext)).as_posix()
        self.db.executemany("""INSERT OR IGNORE INTO source_observations(
            path,record_id,kind,scope,fingerprint,source_sha,archive_relpath,observed_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            [(path,rid,kind,scope,fp,sha,archive_rel,now()) for rid,fp in sorted(members.items())])

    def deactivate_orphans(self) -> int:
        missing = [r['record_id'] for r in self.db.execute('''
          SELECT r.record_id FROM records r WHERE r.record_type IN ('Email','Person','Event','Document')
          AND r.active=1 AND NOT EXISTS(SELECT 1 FROM file_members m WHERE m.record_id=r.record_id)
        ''')]
        for rid in missing:
            self.mark_pending()
            self.db.execute('UPDATE records SET active=0, updated_at=? WHERE record_id=?',(now(),rid))
            self.db.execute('DELETE FROM relationships WHERE owner LIKE ?',(rid + ':%',))
        return len(missing)

    def active_relations(self) -> list[tuple[str,str,str]]:
        return [(r[0],r[1],r[2]) for r in self.db.execute('''
            SELECT DISTINCT rel.source_id, rel.predicate, rel.target_id
            FROM relationships rel JOIN records s ON s.record_id=rel.source_id
              JOIN records t ON t.record_id=rel.target_id
            WHERE s.active=1 AND t.active=1 ORDER BY 1,2,3
        ''')]

    def trace(self, record_id: str) -> dict:
        """Inspect one record's evidence and lifecycle; no writes or extra stores."""
        row = self.row(record_id)
        if row is None:
            raise KeyError('Unknown record ID')
        db = self.db
        sources = [dict(r) for r in db.execute("""SELECT path,kind,scope,fingerprint,
            source_sha,archive_relpath,observed_at FROM source_observations
            WHERE record_id=? ORDER BY observation_id""",(record_id,))]
        history = [dict(r) for r in db.execute("""SELECT * FROM record_history
            WHERE record_id=? ORDER BY event_id""",(record_id,))]
        evaluations = [dict(r) for r in db.execute("""SELECT * FROM evaluations
            WHERE record_id=? ORDER BY run_id""",(record_id,))]
        rels = [dict(r) for r in db.execute("""SELECT * FROM relationships
            WHERE source_id=? OR target_id=? ORDER BY owner,predicate,target_id""",(record_id,record_id))]
        rel_history = [dict(r) for r in db.execute("""SELECT * FROM relationship_history
            WHERE source_id=? OR target_id=? ORDER BY event_id""",(record_id,record_id))]
        supports=[]
        # The authoritative derived source ledger already contains the claim;
        # expose it without duplicating claims in a second support database.
        for r in db.execute('SELECT * FROM derived_evidence ORDER BY producer,source_id'):
            data=json.loads(r['result_json'])
            if any(c.get('record_id')==record_id for c in data.get('claims',[])):
                origin=self.row(r['source_id'])
                supports.append({'producer':r['producer'],'source_record_id':r['source_id'],
                    'source_sha':data.get('source_sha256'),
                    'input_fingerprint':r['fingerprint']})
        return {'record_id':record_id,'record_type':row['record_type'],
                'active':bool(row['active']),'identity_basis':row['identity_basis'],
                'source_observations':sources,'record_history':history,
                'rule_evaluations':evaluations,'current_derived_support':supports,
                'current_relationships':rels,'relationship_history':rel_history}

    def audit_provenance(self) -> dict:
        """Read-only consistency audit of current truth, retained evidence and history."""
        check=getattr(self,'_audit_checkpoint',None) or (lambda:None)
        progress=getattr(self,'_audit_progress',None)
        check()
        db=self.db
        issues=[]
        checked_blobs={}
        def issue(code, item):
            issues.append({'code':code,'item':str(item)})
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            issue('sqlite_quick_check','database')
        for r in db.execute('PRAGMA foreign_key_check'):
            check()
            issue('foreign_key',tuple(r))
        root_archive=(self.root/'archive').resolve()
        def verify_blob(rel,sha):
            check()
            key=(rel,sha)
            if key in checked_blobs:
                return
            checked_blobs[key]=False
            path=(self.root/rel).resolve()
            if not path.is_relative_to(root_archive) or not path.is_file():
                issue('archive_missing_or_outside',rel)
                return
            if digest(path.read_bytes())!=sha:
                issue('archive_checksum_mismatch',rel)
                return
            checked_blobs[key]=True
            check()
            if progress:progress({'files_completed':len(checked_blobs)})
        for r in db.execute('SELECT record_id,source_sha,archive_relpath FROM record_versions'):
            check()
            verify_blob(r['archive_relpath'],r['source_sha'])
        for r in db.execute('SELECT * FROM source_observations'):
            check()
            verify_blob(r['archive_relpath'],r['source_sha'])
            if self.row(r['record_id']) is None:
                issue('observation_unknown_record',r['record_id'])
        for r in db.execute("""SELECT f.path,f.sha256,m.record_id,m.fingerprint
                   FROM observed_files f JOIN file_members m ON m.path=f.path"""):
            check()
            match=db.execute("""SELECT 1 FROM source_observations WHERE path=? AND
                   record_id=? AND fingerprint=? AND source_sha=?""",
                   (r['path'],r['record_id'],r['fingerprint'],r['sha256'])).fetchone()
            if match is None:
                issue('current_observation_missing',r['record_id']+'@'+r['path'])
        records=db.execute('SELECT * FROM records').fetchall()
        for r in records:
            check()
            h=db.execute("""SELECT * FROM record_history WHERE record_id=?
                             ORDER BY event_id DESC LIMIT 1""",(r['record_id'],)).fetchone()
            if not h or any(h[k]!=r[k] for k in
                ('record_type','fingerprint','source_sha','active','properties_json','added_json')):
                issue('record_history_mismatch',r['record_id'])
            if r['record_type'] in ('Email','Person','Event','Document'):
                current=db.execute("""SELECT 1 FROM record_versions WHERE record_id=?
                            AND fingerprint=? AND source_sha=?""",
                            (r['record_id'],r['fingerprint'],r['source_sha'])).fetchone()
                if current is None:
                    issue('source_version_missing',r['record_id'])
            elif r['active']:
                if r['record_type']=='LineOfEffort' and r['identity_basis'] in ('user_created','user_adopted'):
                    required_op='loe.create' if r['identity_basis']=='user_created' else 'loe.adopt'
                    receipt=db.execute('SELECT 1 FROM ws_audit WHERE subject_kind=? AND subject_id=? AND operation=? LIMIT 1',
                        ('LineOfEffort',r['record_id'],required_op)).fetchone() if db.execute('PRAGMA user_version').fetchone()[0]>=3 else None
                    if not receipt:issue('manual_loe_authorship_missing',r['record_id'])
                    properties=json.loads(r['properties_json'])
                    expected_fp=digest(encode({'authored_version':1,'fields':properties}).encode())
                    if r['fingerprint']!=expected_fp:issue('manual_loe_fingerprint_mismatch',r['record_id'])
                    if r['identity_basis']=='user_created':
                        if r['source_sha']!='':issue('manual_loe_false_source_sha',r['record_id'])
                        if db.execute('SELECT 1 FROM record_versions WHERE record_id=? LIMIT 1',(r['record_id'],)).fetchone():
                            issue('manual_loe_false_source_archive',r['record_id'])
                    continue
                producer='Organization' if r['record_type']=='Organization' else 'LOE'
                support=False
                for d in db.execute("SELECT result_json FROM derived_evidence WHERE producer IN ('Organization','DocumentIssuer')" if producer=='Organization' else "SELECT result_json FROM derived_evidence WHERE producer='LOE'"):
                    check()
                    if any(x.get('record_id')==r['record_id'] for x in json.loads(d[0]).get('claims',[])):
                        support=True;break
                if not support:
                    issue('derived_record_without_support',r['record_id'])
        # Latest assertion per owner + edge must exactly match the current edge.
        history={}
        for r in db.execute('SELECT * FROM relationship_history ORDER BY event_id'):
            check()
            history[(r['owner'],r['source_id'],r['predicate'],r['target_id'])]=r
        current={}
        for r in db.execute('SELECT * FROM relationships'):
            check()
            key=(r['owner'],r['source_id'],r['predicate'],r['target_id'])
            current[key]=r
            h=history.get(key)
            if not h or h['change_type'] not in ('asserted','evidence_updated','baseline_v1') or h['evidence_json']!=r['evidence_json']:
                issue('relationship_history_mismatch','|'.join(key))
            if not r['owner'].count(':'):
                issue('relationship_owner_unknown',r['owner'])
            else:
                owner_id=r['owner'].split(':',1)[1]
                owner_record=self.row(owner_id)
                if not owner_record or not owner_record['active']:
                    issue('relationship_evidence_inactive',r['owner'])
        for key,h in history.items():
            check()
            if key not in current and h['change_type']!='retracted':
                issue('relationship_retraction_missing','|'.join(key))
        d_history={}
        for r in db.execute('SELECT * FROM derived_history ORDER BY event_id'):
            check()
            d_history[(r['producer'],r['source_id'])]=r
        derived=db.execute('SELECT * FROM derived_evidence').fetchall()
        current_derived=set()
        for r in derived:
            check()
            key=(r['producer'],r['source_id']);current_derived.add(key)
            h=d_history.get(key)
            if not h or h['change_type']=='withdrawn' or h['result_json']!=r['result_json'] or h['fingerprint']!=r['fingerprint']:
                issue('derived_history_mismatch',key)
        for key,h in d_history.items():
            check()
            if key not in current_derived and h['change_type']!='withdrawn':
                issue('derived_withdrawal_missing',key)
        return {
          'ok':not issues, 'issues':issues,
          'checked':{'records':len(records),'relationship_claims':len(current),
                     'source_observations':db.execute('SELECT COUNT(*) FROM source_observations').fetchone()[0],
                     'record_history':db.execute('SELECT COUNT(*) FROM record_history').fetchone()[0],
                     'relationship_history':db.execute('SELECT COUNT(*) FROM relationship_history').fetchone()[0],
                     'derived_history':db.execute('SELECT COUNT(*) FROM derived_history').fetchone()[0],
                     'archive_blobs':len({rel for rel,_ in checked_blobs})}}

    def summary(self) -> dict:
        return {'records':{t:len(self.records(t)) for t in KINDS},
                'relationships':len(self.active_relations()),
                'source_versions':self.db.execute('SELECT COUNT(*) FROM record_versions').fetchone()[0],
                'evaluations':self.db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0]}
