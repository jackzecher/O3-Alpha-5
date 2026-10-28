"""Explicit, guarded Core v2 -> KM v3 migration and WAL-consistent archive backup.

Never migrates by importing a module or opening the KM interface. Operational
upgrades require an independently approved backup destination and shutdown of
all other writers. Synthetic test runs use the same safeguards.
"""
from __future__ import annotations
from pathlib import Path, PurePosixPath, PureWindowsPath
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from .upgrade_control import interruptible_sqlite
from .catalog import CATALOG_FINGERPRINT, DEFAULT_SETTINGS, PROPERTIES, PRESETS, TEMPLATES,template_expression,addresses,patterns

LEGACY_KM10_CATALOG_FINGERPRINT='3cf0d4643846f76bea24030187641fbdd5d7307de1b4c03976e00f1a9b009900'
# The exact released Alpha 4 catalog is a recognized migration input, never a
# wildcard permission to open an unknown or damaged Workspace.
ALPHA4_CATALOG_FINGERPRINT='832699e397f61a44cd0f6d287fb54541813cf4a7a2ed8fd7db8c4b0324fa3b75'

TABLES_SQL='''
CREATE TABLE ws_definitions(
 definition_id TEXT PRIMARY KEY,
 definition_kind TEXT NOT NULL CHECK(definition_kind IN ('property','rule','view','preset')),
 owner_type TEXT NOT NULL CHECK(owner_type IN ('Email','Event','Person','Document','Organization','LineOfEffort','WorkspaceOrganization')),
 origin TEXT NOT NULL CHECK(origin IN ('custom','builtin_override')),
 specification_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('enabled','disabled','retired')),
 revision INTEGER NOT NULL CHECK(revision>=1)
);
CREATE TABLE ws_values(
 target_kind TEXT NOT NULL CHECK(target_kind IN ('Email','Event','Person','Document','Organization','LineOfEffort','WorkspaceOrganization')),
 target_id TEXT NOT NULL,
 property_id TEXT NOT NULL,
 value_json TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>=1),
 PRIMARY KEY(target_kind,target_id,property_id)
);
CREATE TABLE ws_organizations(
 ws_org_id TEXT PRIMARY KEY,
 display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 160),
 archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
 created_at_utc TEXT NOT NULL,
 updated_at_utc TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>=1)
);
CREATE TABLE ws_org_members(
 core_org_id TEXT PRIMARY KEY REFERENCES records(record_id),
 ws_org_id TEXT NOT NULL REFERENCES ws_organizations(ws_org_id),
 assigned_revision INTEGER NOT NULL CHECK(assigned_revision>=1)
);
CREATE INDEX ws_org_members_by_overlay ON ws_org_members(ws_org_id);
CREATE TABLE ws_audit(
 event_id INTEGER PRIMARY KEY,
 workspace_revision INTEGER NOT NULL UNIQUE,
 actor TEXT NOT NULL,
 operation TEXT NOT NULL,
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 before_json TEXT,
 after_json TEXT,
 dependencies_json TEXT NOT NULL,
 at_utc TEXT NOT NULL
);
'''
NAMES=('ws_definitions','ws_values','ws_organizations','ws_org_members','ws_audit')
WORKSPACE_COLUMNS={
    'ws_definitions':{'definition_id','definition_kind','owner_type','origin','specification_json','state','revision'},
    'ws_values':{'target_kind','target_id','property_id','value_json','revision'},
    'ws_organizations':{'ws_org_id','display_name','archived','created_at_utc','updated_at_utc','revision'},
    'ws_org_members':{'core_org_id','ws_org_id','assigned_revision'},
    'ws_audit':{'event_id','workspace_revision','actor','operation','subject_kind','subject_id','before_json','after_json','dependencies_json','at_utc'},
}
CORE_V2_TABLES={'records','observed_files','file_members','record_versions','evaluations',
 'relationships','engine_meta','derived_evidence','source_observations','record_history',
 'relationship_history','derived_history'}
def _utc():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def _json(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def _hash(b):return hashlib.sha256(b).hexdigest()
def _file_hash(path):
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def _zip_name(name):
    """One portable, strictly relative backup namespace (also safe on Windows)."""
    if not isinstance(name,str) or not name or '\\' in name or ':' in name:
        raise RuntimeError('BACKUP_VERIFICATION_FAILED: unsafe archive member')
    posix=PurePosixPath(name);windows=PureWindowsPath(name)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive or
        posix.as_posix()!=name or any(part in ('','.', '..') or
            part.endswith((' ','.')) or any(ord(c)<32 or c in '<>"|?*' for c in part) or
            PureWindowsPath(part).is_reserved() for part in posix.parts) or
        not (name in ('o3.sqlite','workspace-rules.json','workspace.json') or
             (len(posix.parts)>=3 and posix.parts[0]=='archive'))):
        raise RuntimeError('BACKUP_VERIFICATION_FAILED: unsafe archive member')
    return name

def _archive_name_matches_hash(name,sha):
    """The existing Core archives are content-addressed as archive/xx/<sha>.<ext>."""
    parts=PurePosixPath(name).parts
    if len(parts)!=3 or parts[0]!='archive':return False
    name_sha,sep,ext=parts[2].partition('.')
    return (sep=='.' and ext in ('json','ics') and len(sha)==64 and
            name_sha==sha and parts[1]==sha[:2])

def _source_roots(db):
    """Observed input paths protect O3Data even when a caller omits its root."""
    roots=set()
    for kind,path in db.execute('SELECT kind,path FROM observed_files'):
        original=Path(path)
        for parent in original.parents:
            if (kind=='Email' and parent.name in ('Inbox','Sent') and parent.parent.name=='Mail' or
                kind=='Person' and parent.name=='Profiles' and parent.parent.name=='Contacts' or
                kind=='Event' and parent.name=='Calendars' or
                kind=='Document' and parent.name=='Documents'):
                roots.add(parent.parent.parent if kind in ('Email','Person')
                          else parent.parent)
                break
    # Captures commonly share one source root. Resolve it once, particularly on
    # managed/network drives, while retaining the same canonical overlap checks.
    return {root.resolve() for root in roots}

def _no_overlap(destination,roots):
    for root in roots:
        root=Path(root).resolve()
        if destination.is_relative_to(root) or root.is_relative_to(destination):
            raise RuntimeError('BACKUP_REQUIRED: destination overlaps state/source')
def _table_set(db):return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

# The frozen Core v0.4 schema is an input contract, not something a future
# Workspace migration may silently repair or approximate.
CORE_COLUMNS={
    'records':'record_id record_type scope identity_basis identity_key fingerprint source_sha active properties_json added_json updated_at',
    'observed_files':'path kind scope size_bytes mtime_ns sha256',
    'file_members':'path record_id fingerprint',
    'record_versions':'record_id fingerprint source_sha archive_relpath seen_at',
    'evaluations':'run_id record_id input_sha outcome_json evaluated_at',
    'relationships':'owner source_id predicate target_id evidence_json',
    'engine_meta':'key value',
    'derived_evidence':'producer source_id fingerprint result_json',
    'source_observations':'observation_id path record_id kind scope fingerprint source_sha archive_relpath observed_at',
    'record_history':'event_id record_id change_type record_type fingerprint source_sha active properties_json added_json changed_at',
    'relationship_history':'event_id owner source_id predicate target_id change_type evidence_json changed_at',
    'derived_history':'event_id producer source_id change_type fingerprint result_json changed_at',
}
CORE_TRIGGERS={
    'provenance_record_insert','provenance_record_update',
    'provenance_relationship_insert','provenance_relationship_delete',
    'provenance_relationship_update','provenance_derived_insert',
    'provenance_derived_update','provenance_derived_delete',
}
def validate_core_schema(db):
    if not CORE_V2_TABLES.issubset(_table_set(db)):
        raise RuntimeError('SCHEMA_INCOMPATIBLE: missing Core table')
    for table,names in CORE_COLUMNS.items():
        existing=' '.join(row[1] for row in db.execute('PRAGMA table_info('+table+')'))
        if existing!=names:
            raise RuntimeError('SCHEMA_INCOMPATIBLE: Core '+table+' layout mismatch')
    triggers={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not CORE_TRIGGERS.issubset(triggers):
        raise RuntimeError('SCHEMA_INCOMPATIBLE: Core provenance triggers missing')
def _meta(db,key,default=None):
 row=db.execute('SELECT value FROM engine_meta WHERE key=?',(key,)).fetchone();return row[0] if row else default

def validate_workspace_schema(db, *, version, allow_alpha4=False):
    """Validate the inherited KM seam at its actual schema version.

    Alpha 4 is accepted only by an explicit conversion/recovery caller. Normal
    service opens and post-conversion validation require the current catalog.
    The v4 extension must not bypass checks on its five inherited tables.
    """
    if version not in (3,4) or db.execute('PRAGMA user_version').fetchone()[0]!=version:
        raise RuntimeError('SCHEMA_INCOMPATIBLE: Workspace schema v'+str(version)+' required')
    validate_core_schema(db)
    if not CORE_V2_TABLES.union(NAMES).issubset(_table_set(db)):
        raise RuntimeError('SCHEMA_INCOMPATIBLE: incomplete schema')
    accepted={CATALOG_FINGERPRINT}
    if allow_alpha4:accepted.add(ALPHA4_CATALOG_FINGERPRINT)
    if _meta(db,'km_schema_contract') not in accepted:
        raise RuntimeError('SCHEMA_INCOMPATIBLE: catalog fingerprint mismatch')
    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('INTEGRITY_FAILED')
    # Detect incorrect table variants rather than masking them with IF NOT EXISTS.
    for table,needed in WORKSPACE_COLUMNS.items():
        if {r[1] for r in db.execute('PRAGMA table_info('+table+')')}!=needed:
            raise RuntimeError('SCHEMA_INCOMPATIBLE: unexpected '+table+' columns')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise RuntimeError('INTEGRITY_FAILED: invalid Workspace foreign key')

def validate_schema3(db, *, allow_alpha4=False):
    validate_workspace_schema(db,version=3,allow_alpha4=allow_alpha4)

def _checked_archive_references(state,db,*,_check=None):
    """Check every distinct reference's path before any evidence is consumed."""
    if (state/'archive').is_symlink():raise RuntimeError('INTEGRITY_FAILED: archive directory link')
    archive_root=(state/'archive').resolve()
    # One ICS capture may support thousands of event records. Verify each
    # distinct (path, expected hash) once per audit, not the entire capture once
    # per event in both tables. Conflicting hashes remain separate and fail.
    references=db.execute('''SELECT archive_relpath,source_sha FROM record_versions
                            UNION SELECT archive_relpath,source_sha FROM source_observations''')
    for rel,sha in references:
        if _check:_check()
        try:_zip_name(rel)
        except RuntimeError:raise RuntimeError('INTEGRITY_FAILED: unsafe archive path') from None
        if not rel.startswith('archive/'):
            raise RuntimeError('INTEGRITY_FAILED: unsafe archive path')
        candidate=state/rel
        if any((state.joinpath(*PurePosixPath(rel).parts[:i])).is_symlink()
               for i in range(1,len(PurePosixPath(rel).parts)+1)):
            raise RuntimeError('INTEGRITY_FAILED: archive symlink')
        path=candidate.resolve()
        if not path.is_relative_to(archive_root) or not path.is_file():
            raise RuntimeError('INTEGRITY_FAILED: source archive verification')
        yield path,sha

def _validate_archive(state,db):
    # Standalone callers still require a complete content check here.
    for path,sha in _checked_archive_references(state,db):
        if _file_hash(path)!=sha:
            raise RuntimeError('INTEGRITY_FAILED: source archive verification')

def _validate_archive_boundaries(state,db,*,_check=None):
    # Only used immediately before the full Core provenance audit. That audit
    # reads each distinct (path, expected SHA) and checks its actual bytes, as
    # well as the history/reference relationships. Do not duplicate that read.
    for _ in _checked_archive_references(state,db,_check=_check):
        pass

def _verify_core_provenance_readonly(state: Path, db: sqlite3.Connection, *, _check=None, _progress=None):
    """Reuse the original Core audit against an existing read-only snapshot.

    Construct no Store or Engine (both manage application state), and perform
    no SQL writes. This is deliberately the SAME audit as normal Core use.
    """
    from o3_core.store import Store
    view = object.__new__(Store)
    view.root = Path(state).resolve()
    view.db = db
    view._audit_checkpoint = _check
    def progress(counts):
        if _progress:
            try:_progress(counts)
            except Exception:pass
    view._audit_progress = progress
    db.row_factory = sqlite3.Row
    if not Store.audit_provenance(view)['ok']:
        raise RuntimeError('INTEGRITY_FAILED: Core provenance/history audit failed')


def verified_backup(state:Path,destination:Path,*,independent_location_approved:bool=False,source_root:Path|None=None,
                    _allow_legacy_catalog:bool=False):
    """Create complete verified ZIP; caller must independently approve destination policy."""
    if Path(state).is_symlink():raise RuntimeError('BACKUP_REQUIRED: linked state root')
    state=Path(state).resolve()
    target=Path(destination).expanduser()
    if target.is_symlink():raise RuntimeError('BACKUP_REQUIRED: symlink destination')
    destination=target.resolve()
    if not independent_location_approved:raise RuntimeError('BACKUP_REQUIRED: independent location approval needed')
    if source_root is None:raise RuntimeError('BACKUP_REQUIRED: validated O3Data source root required')
    if Path(source_root).is_symlink():raise RuntimeError('BACKUP_REQUIRED: linked source root')
    dbfile=state/'o3.sqlite'
    if not dbfile.is_file():raise RuntimeError('BACKUP_REQUIRED: missing source database')
    with tempfile.TemporaryDirectory(prefix='o3-km-backup-') as tmp:
        snap=Path(tmp)/'o3.sqlite';src=sqlite3.connect(f'file:{dbfile.as_posix()}?mode=ro',uri=True)
        dst=sqlite3.connect(snap)
        try:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('BACKUP_VERIFICATION_FAILED')
            try:_verify_core_provenance_readonly(state,dst)
            except RuntimeError:
                raise RuntimeError('BACKUP_VERIFICATION_FAILED: Core provenance/history audit failed') from None
            known_roots=_source_roots(dst)
            if known_roots and Path(source_root).resolve() not in known_roots:
                raise RuntimeError('BACKUP_REQUIRED: source root disagrees with observed O3Data')
            _no_overlap(destination,{state,*known_roots,Path(source_root).resolve()})
        finally:dst.close();src.close()
        # No destination is created until its observed source/state boundaries have passed.
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():raise RuntimeError('BACKUP_REQUIRED: backup already exists')
        entries={'o3.sqlite':snap}
        archive=state/'archive'
        if archive.is_symlink():raise RuntimeError('BACKUP_VERIFICATION_FAILED: archive directory link')
        for f in archive.rglob('*') if archive.exists() else []:
            if f.is_symlink():raise RuntimeError('BACKUP_VERIFICATION_FAILED: archive link')
            if f.is_file():entries[_zip_name(f.relative_to(state).as_posix())]=f
        for f in (state/'workspace-rules.json',state/'workspace.json'):
            if f.is_symlink():raise RuntimeError('BACKUP_VERIFICATION_FAILED: configuration link')
            if f.is_file():entries[f.name]=f
        manifest={'format':'o3-km-backup/1','created_utc':_utc(),'sha256':{},
                  'independent_location_approved_by_caller':True,'verified':False}
        pending=destination.with_name(destination.name+'.partial-'+uuid.uuid4().hex)
        try:
            with zipfile.ZipFile(pending,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
                for name,file in sorted(entries.items()):
                    manifest['sha256'][name]=_file_hash(file)
                    z.write(file,arcname=name)
                z.writestr('backup-manifest.json',_json(manifest))
            verify_backup(pending,_allow_legacy_catalog=_allow_legacy_catalog)
            os.replace(pending,destination)
        finally:pending.unlink(missing_ok=True)
    return {'path':str(destination),'sha256':_file_hash(destination),'files':len(entries),
            'verified':True,'independent_location_approved_by_caller':True}

def verify_backup(path:Path,*,_allow_legacy_catalog:bool=False):
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        if (len(set(names))!=len(names) or len(set(n.casefold() for n in names))!=len(names) or
            names.count('backup-manifest.json')!=1):
            raise RuntimeError('BACKUP_VERIFICATION_FAILED: manifest mismatch')
        manifest_info=z.getinfo('backup-manifest.json')
        if manifest_info.is_dir() or (manifest_info.external_attr>>16)&0o170000==0o120000:
            raise RuntimeError('BACKUP_VERIFICATION_FAILED: unsafe manifest member')
        if manifest_info.file_size>1_000_000:
            raise RuntimeError('BACKUP_VERIFICATION_FAILED: manifest oversized')
        manifest=json.loads(z.read('backup-manifest.json'))
        if (not isinstance(manifest,dict) or manifest.get('format')!='o3-km-backup/1' or
            not isinstance(manifest.get('sha256'),dict) or
            set(names)!=(set(manifest['sha256'])|{'backup-manifest.json'}) or 'o3.sqlite' not in names):
            raise RuntimeError('BACKUP_VERIFICATION_FAILED: manifest mismatch')
        for name,sha in manifest['sha256'].items():
            _zip_name(name)
            info=z.getinfo(name)
            if info.is_dir() or (info.external_attr>>16)&0o170000==0o120000 or not isinstance(sha,str) or len(sha)!=64:
                raise RuntimeError('BACKUP_VERIFICATION_FAILED: unsafe member type')
            digest=hashlib.sha256()
            with z.open(name) as file:
                for chunk in iter(lambda:file.read(1024*1024),b''):digest.update(chunk)
            if digest.hexdigest()!=sha:
                raise RuntimeError('BACKUP_VERIFICATION_FAILED: file integrity')
        with tempfile.TemporaryDirectory() as tmp:
            dbpath=Path(tmp)/'s.sqlite'
            with z.open('o3.sqlite') as source,open(dbpath,'wb') as target:
                shutil.copyfileobj(source,target,length=1024*1024)
            db=sqlite3.connect(dbpath)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or list(db.execute('PRAGMA foreign_key_check')):
                    raise RuntimeError('BACKUP_VERIFICATION_FAILED: database')
                version=db.execute('PRAGMA user_version').fetchone()[0]
                workspace_id=_meta(db,'km_workspace_id') if version>=3 else None
                if version not in (2,3,4):
                    raise RuntimeError('BACKUP_VERIFICATION_FAILED: unsupported Core schema')
                validate_core_schema(db)
                if version==4:
                    from .workspace_types import validate_v4
                    validate_v4(db,allow_alpha4=True)
                if version==3:
                    if _allow_legacy_catalog and _meta(db,'km_schema_contract')==LEGACY_KM10_CATALOG_FINGERPRINT:
                        if not CORE_V2_TABLES.union(NAMES).issubset(_table_set(db)):
                            raise RuntimeError('BACKUP_VERIFICATION_FAILED: legacy Workspace tables missing')
                    else:validate_schema3(db,allow_alpha4=True)
                for name,digest in manifest['sha256'].items():
                    if name.startswith('archive/') and not _archive_name_matches_hash(name,digest):
                        raise RuntimeError('BACKUP_VERIFICATION_FAILED: archive is not content-addressed')
                for table in ('record_versions','source_observations'):
                    for rel,sha in db.execute('SELECT archive_relpath,source_sha FROM '+table):
                        _zip_name(rel)
                        if not rel.startswith('archive/') or manifest['sha256'].get(rel)!=sha:
                            raise RuntimeError('BACKUP_VERIFICATION_FAILED: source provenance mismatch')
                for rid,kind,fp,sha in db.execute('SELECT record_id,record_type,fingerprint,source_sha FROM records'):
                    if kind in ('Email','Event','Person','Document'):
                        if not db.execute('SELECT 1 FROM record_versions WHERE record_id=? AND fingerprint=? AND source_sha=?',
                                          (rid,fp,sha)).fetchone():
                            raise RuntimeError('BACKUP_VERIFICATION_FAILED: current source record lacks matching evidence')

            finally:db.close()
    return {'verified':True,'file_count':len(manifest['sha256']), 'created_utc':manifest['created_utc'],
            'schema_version':version,'workspace_id':workspace_id}

def _legacy(state):
    path=state/'workspace-rules.json'
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_size>40_000:raise RuntimeError('MIGRATION_BLOCKED: legacy config invalid')
    from o3_km.catalog import patterns as pattern_fn
    obj=json.loads(path.read_text(encoding='utf-8'))
    if set(obj)!={'version','Email'} or obj['version']!=1 or set(obj['Email'])!={'direct','noise'}:
        raise RuntimeError('MIGRATION_BLOCKED: legacy structure')
    a=obj['Email']['direct'];n=obj['Email']['noise']
    if set(a)!={'configured','mailbox_aliases'} or set(n)!={'configured','sender_patterns'}:
        raise RuntimeError('MIGRATION_BLOCKED: legacy fields')
    if not isinstance(a['configured'],bool) or not isinstance(n['configured'],bool):raise RuntimeError('MIGRATION_BLOCKED: legacy boolean')
    aliases=addresses(a['mailbox_aliases']);p=pattern_fn(n['sender_patterns'])
    # The v0.3.1 GUI deduplicated and lowercased values but preserved input order.
    # KM 1.0 canonicalizes the set by sorting. Do not reject valid UI history.
    if (len(aliases)!=len(a['mailbox_aliases']) or sorted(a['mailbox_aliases'])!=aliases
        or len(p)!=len(n['sender_patterns']) or sorted(n['sender_patterns'])!=p):
        raise RuntimeError('MIGRATION_BLOCKED: legacy normalization')
    if a['configured'] and not aliases or n['configured'] and not p:
        raise RuntimeError('MIGRATION_BLOCKED: legacy incomplete')
    return obj,_hash(path.read_bytes())

def migrate_v2_to_v3(state:Path, backup:Path,*,independent_location_approved:bool=False,
                     source_root:Path|None=None,actor:str='migration:authorized',legacy_file:Path|None=None,
                     exclusive_writer_stopped:bool=False):
    if not exclusive_writer_stopped:
        raise RuntimeError('STATE_LOCKED: explicit stopped-writer confirmation required before migration')
    state=Path(state).resolve(); dbfile=state/'o3.sqlite'
    if not dbfile.is_file():raise RuntimeError('MIGRATION_BLOCKED: source DB missing')
    db=sqlite3.connect(f'file:{dbfile.as_posix()}?mode=ro',uri=True)
    try:
        if db.execute('PRAGMA user_version').fetchone()[0]!=2:raise RuntimeError('MIGRATION_BLOCKED: requires v2')
        validate_core_schema(db)
        if any(k in _table_set(db) for k in NAMES):
            raise RuntimeError('MIGRATION_BLOCKED: partial/foreign schema')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('INTEGRITY_FAILED')
        if list(db.execute('PRAGMA foreign_key_check')):raise RuntimeError('INTEGRITY_FAILED: FK')
        _validate_archive(state,db)
        _verify_core_provenance_readonly(state,db)
        before={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]
                for t in ('records','record_history','relationships','relationship_history','source_observations')}
        events=[dict(zip(('path','scope'),row)) for row in db.execute("SELECT path,scope FROM observed_files WHERE kind='Event'")]
    finally:db.close()
    legacy=_legacy(state) if legacy_file is None else _legacy(Path(legacy_file).parent)
    bk=verified_backup(state,backup,independent_location_approved=independent_location_approved,source_root=source_root)
    db=sqlite3.connect(dbfile,timeout=10);db.execute('PRAGMA foreign_keys=ON')
    try:
        db.execute('BEGIN IMMEDIATE')
        # executescript commits implicitly; use individual complete statements.
        for statement in TABLES_SQL.split(';'):
            if statement.strip():db.execute(statement)
        settings=dict(DEFAULT_SETTINGS)
        r=1;ws_id='ws_'+uuid.uuid4().hex
        if legacy:
            raw,source_hash=legacy
            settings['self_addresses']=sorted(raw['Email']['direct']['mailbox_aliases'])
            # Previously saved GUI settings are explicitly user-selected
            # configurations. Instantiate ordinary editable definitions, not
            # a privileged preset-evaluator override.
            for template_id,enabled,configuration in (
                ('WS-EMAIL-DIRECT-001',raw['Email']['direct']['configured'],{}),
                ('WS-EMAIL-NOISE-001',raw['Email']['noise']['configured'],
                 {'sender_patterns':sorted(raw['Email']['noise']['sender_patterns'])})):
                template=TEMPLATES[template_id];pid=template['property_id']
                prop={**template['property_schema'],'property_id':pid,'owner_type':'Email',
                      'source_preset_id':template_id,'origin':'custom',
                      'label':template['property_schema']['display_label'],'version':1}
                rid='ws:user:legacy_'+template_id.lower().replace('-','_')
                rule={'rule_id':rid,'target_record_type':'Email','output_property_id':pid,
                      'expression':template_expression(template_id,configuration),
                      'dependencies':[],'rule_version':1,
                      'description':'Migrated editable legacy Workspace configuration'}
                state_name='enabled' if enabled else 'disabled'
                db.execute('INSERT INTO ws_definitions VALUES(?,?,?,?,?,?,?)',
                           (pid,'property','Email','custom',_json(prop),state_name,1))
                db.execute('INSERT INTO ws_definitions VALUES(?,?,?,?,?,?,?)',
                           (rid,'rule','Email','custom',_json(rule),state_name,1))
            db.execute('INSERT INTO ws_audit(workspace_revision,actor,operation,subject_kind,subject_id,before_json,after_json,dependencies_json,at_utc) VALUES(?,?,?,?,?,?,?,?,?)',
                (r,actor,'migration.legacy_import','configuration','workspace',None,
                 _json({'source_sha':source_hash}),_json([]),_utc()))
        else:r=0
        # existing Primary identities must remain scoped as 'primary'
        cals={}
        for e in events:
            path=Path(e['path'])
            if path.parent.name.casefold()!='calendars':
                raise RuntimeError('CALENDAR_SCOPE_CONFLICT: nested legacy ICS requires explicit review')
            rel=path.name
            if rel.casefold()=='primary.ics':
                if e['scope']!='primary':raise RuntimeError('CALENDAR_SCOPE_CONFLICT: primary already rekeyed')
                if 'primary' in cals and cals['primary']['path']!=rel:
                    raise RuntimeError('CALENDAR_SCOPE_CONFLICT: multiple Primary files')
                cals['primary']={'scope':'primary','path':'Primary.ics','display_name':'Primary','state':'active','revision':1}
            elif e['scope']=='primary':
                raise RuntimeError('CALENDAR_SCOPE_CONFLICT: non-Primary file shares primary scope')
            else:
                if e['scope'] in cals and cals[e['scope']]['path']!=rel:
                    raise RuntimeError('CALENDAR_SCOPE_CONFLICT: scope reused')
                cals[e['scope']]={'scope':e['scope'],'path':rel,'display_name':Path(rel).stem,'state':'active','revision':1}
        if 'primary' not in cals:
            cals['primary']={'scope':'primary','path':'Primary.ics','display_name':'Primary','state':'unavailable','revision':1}
        meta={'km_workspace_id':ws_id,'km_workspace_revision':str(r),'km_core_epoch':'0',
              'workspace_settings_v1':_json(settings),'calendar_sources_v1':_json(cals),
              'km_schema_contract':CATALOG_FINGERPRINT,
              'km_bound_source_root_v1':str(Path(source_root).resolve())}
        if legacy:meta['km_legacy_config_receipt_v1']=legacy[1]
        for key,value in meta.items():db.execute('INSERT INTO engine_meta(key,value) VALUES(?,?)',(key,value))
        db.execute('PRAGMA user_version=3')
        # Validate inside the migration transaction: a failed postcondition must
        # never leave a partially approved v3 committed on disk.
        validate_schema3(db)
        after={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in before}
        if before!=after:raise RuntimeError('MIGRATION_BLOCKED: Core row count changed')
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {'migrated':True,'schema':3,'workspace_id':ws_id,'legacy_imported':bool(legacy),
            'backup':bk,'core_rows_unchanged':before==after}

def migrate_v3_to_configurable_workspace(state:Path,backup:Path,*,
        independent_location_approved:bool=False,source_root:Path|None=None,
        exclusive_writer_stopped:bool=False,actor:str='migration:authorized'):
    """Explicit, non-destructive KM catalog change on the existing v3 schema.

    Handles old preset overrides/manual built-ins without rekeying values. Never
    opens a live writer or edits O3Data. Five existing Workspace tables suffice.
    """
    if not exclusive_writer_stopped:
        raise RuntimeError('STATE_LOCKED: stopped writer confirmation required')
    state=Path(state).resolve();dbfile=state/'o3.sqlite'
    if not dbfile.is_file() or source_root is None:
        raise RuntimeError('MIGRATION_BLOCKED: state and source root required')
    with closing(sqlite3.connect(f'file:{dbfile.as_posix()}?mode=ro',uri=True)) as check:
        if check.execute('PRAGMA user_version').fetchone()[0]!=3:
            raise RuntimeError('SCHEMA_INCOMPATIBLE: old KM schema v3 required')
        validate_core_schema(check)
        if not CORE_V2_TABLES.union(NAMES).issubset(_table_set(check)):
            raise RuntimeError('SCHEMA_INCOMPATIBLE: old Workspace tables missing')
        old=_meta(check,'km_schema_contract')
        if old==CATALOG_FINGERPRINT:
            return {'migrated':False,'reason':'already_current'}
        if old!=LEGACY_KM10_CATALOG_FINGERPRINT:
            raise RuntimeError('SCHEMA_INCOMPATIBLE: unsupported catalog fingerprint')
        if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or list(check.execute('PRAGMA foreign_key_check')):
            raise RuntimeError('INTEGRITY_FAILED: SQLite failed preflight')
        _validate_archive(state,check)
        _verify_core_provenance_readonly(state,check)
        rows_before={k:check.execute('SELECT COUNT(*) FROM '+k).fetchone()[0]
                     for k in ('records','record_history','relationships','source_observations')}
    protected=verified_backup(state,Path(backup),
          independent_location_approved=independent_location_approved,source_root=source_root,
          _allow_legacy_catalog=True)
    with closing(sqlite3.connect(dbfile,timeout=10)) as db:
        db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            overrides=list(db.execute("SELECT * FROM ws_definitions WHERE definition_kind='preset' AND origin='builtin_override'"))
            # Manual values remain authoritative; add matching opt-in property
            # definitions only when earlier Workspace state really used them.
            installed=set()
            for override in overrides:
                if override['definition_id'] not in PRESETS:
                    raise RuntimeError('MIGRATION_BLOCKED: unrecognized legacy template')
                installed.add(PRESETS[override['definition_id']]['property_id'])
            for row in db.execute('SELECT DISTINCT property_id FROM ws_values'):
                if row['property_id'] in PROPERTIES:installed.add(row['property_id'])
            mapping=[]
            for pid in sorted(installed):
                if db.execute('SELECT 1 FROM ws_definitions WHERE definition_id=?',(pid,)).fetchone():
                    raise RuntimeError('MIGRATION_BLOCKED: installed property collision')
                schema=PROPERTIES[pid]
                source=next((r for r in overrides if PRESETS[r['definition_id']]['property_id']==pid),None)
                template_id=source['definition_id'] if source else next(
                    (key for key,item in TEMPLATES.items() if item['property_id']==pid),None)
                if template_id is None:raise RuntimeError('MIGRATION_BLOCKED: unknown Workspace property')
                content={**schema,'property_id':pid,'source_preset_id':template_id,
                    'origin':'custom','label':schema['display_label'],'version':1}
                enabled=not source or source['state']=='enabled'
                state_name='enabled' if enabled else 'disabled'
                db.execute('INSERT INTO ws_definitions VALUES(?,?,?,?,?,?,?)',
                           (pid,'property',schema['owner_type'],'custom',_json(content),state_name,1))
                if source:
                    cfg=json.loads(source['specification_json']).get('settings',{})
                    rid='ws:user:legacy_'+template_id.lower().replace('-','_')
                    if db.execute('SELECT 1 FROM ws_definitions WHERE definition_id=?',(rid,)).fetchone():
                        raise RuntimeError('MIGRATION_BLOCKED: producer ID collision')
                    expression=template_expression(template_id,cfg)
                    dependencies=sorted(_expression_dependencies(expression))
                    rule={'rule_id':rid,'target_record_type':schema['owner_type'],
                          'output_property_id':pid,'expression':expression,
                          'dependencies':dependencies,'rule_version':1,
                          'description':'Migrated editable Workspace preset'}
                    db.execute('INSERT INTO ws_definitions VALUES(?,?,?,?,?,?,?)',
                               (rid,'rule',schema['owner_type'],'custom',_json(rule),state_name,1))
                    mapping.append({'template':template_id,'property':pid,'rule':rid})
            db.execute("DELETE FROM ws_definitions WHERE definition_kind='preset' AND origin='builtin_override'")
            revision=int(_meta(db,'km_workspace_revision','0'))+1
            db.execute('INSERT INTO ws_audit(workspace_revision,actor,operation,subject_kind,subject_id,before_json,after_json,dependencies_json,at_utc) VALUES(?,?,?,?,?,?,?,?,?)',
                       (revision,actor,'migration.configurable_workspace','configuration','workspace',
                        _json({'catalog':old,'legacy_override_count':len(overrides)}),
                        _json({'catalog':CATALOG_FINGERPRINT,'migrated':mapping}),_json([]),_utc()))
            db.execute("UPDATE engine_meta SET value=? WHERE key='km_workspace_revision'",(str(revision),))
            db.execute("UPDATE engine_meta SET value=? WHERE key='km_schema_contract'",(CATALOG_FINGERPRINT,))
            validate_schema3(db)
            rows_after={k:db.execute('SELECT COUNT(*) FROM '+k).fetchone()[0] for k in rows_before}
            if rows_before!=rows_after:raise RuntimeError('MIGRATION_BLOCKED: Core changed during upgrade')
            db.commit()
        except Exception:
            db.rollback();raise
    return {'migrated':True,'catalog':'configurable-workspace-v1.1',
            'backup':protected,'migrated_properties':sorted(installed),'original_core_rows_preserved':True}

def _expression_dependencies(expression):
    if 'all' in expression or 'any' in expression:
        return set().union(*(_expression_dependencies(x) for x in expression.get('all',expression.get('any',[]))))
    value=expression.get('field')
    return {value} if isinstance(value,str) and value.startswith('ws:') else set()


def restore_backup(backup:Path,state:Path,*,source_root:Path|None=None,exclusive_writer_stopped:bool=False,confirmed:bool=False):
    """Restore independently verified backup, offline only; retain old state for rollback."""
    if not exclusive_writer_stopped or not confirmed:
        raise RuntimeError('STATE_LOCKED: restore requires confirmed stopped writer')
    if source_root is None:raise RuntimeError('MIGRATION_BLOCKED: validated O3Data source root required')
    if Path(source_root).is_symlink() or Path(state).is_symlink():
        raise RuntimeError('MIGRATION_BLOCKED: linked state/source root')
    verify_backup(backup)
    state=Path(state).resolve()
    try:_no_overlap(state,{Path(source_root).resolve()})
    except RuntimeError:raise RuntimeError('MIGRATION_BLOCKED: restore destination overlaps O3Data') from None
    if not state.is_dir() or state.is_symlink():raise RuntimeError('MIGRATION_BLOCKED: invalid state root')
    try:_no_overlap(Path(backup).resolve(),{state})
    except RuntimeError:raise RuntimeError('MIGRATION_BLOCKED: backup must survive state replacement') from None
    staging=state.with_name(state.name+'.restore-'+uuid.uuid4().hex)
    rollback=state.with_name(state.name+'.pre-restore-'+uuid.uuid4().hex)
    staging.mkdir()
    try:
        with zipfile.ZipFile(backup) as z:
            for name in z.namelist():
                if name=='backup-manifest.json':continue
                _zip_name(name)
                path=staging/name;path.parent.mkdir(parents=True,exist_ok=True)
                with z.open(name) as source,open(path,'xb') as target:
                    shutil.copyfileobj(source,target,length=1024*1024)
        # Verify the staged *operational* state, not just ZIP checksums.
        # Use the existing Core audits and KM integrity validator; no new engine,
        # database, or restoration service is introduced.
        with closing(sqlite3.connect(staging/'o3.sqlite')) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or list(db.execute('PRAGMA foreign_key_check')):
                raise RuntimeError('BACKUP_VERIFICATION_FAILED: SQLite integrity')
            _no_overlap(state,_source_roots(db))
            _validate_archive(staging,db)
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version==4:
                from .workspace_types import validate_v4
                validate_v4(db,allow_alpha4=True)
            elif version==3:validate_schema3(db,allow_alpha4=True)
            namespaces=json.loads(_meta(db,'identity_scopes'))
            incoming_workspace=_meta(db,'km_workspace_id') if version>=3 else None
            incoming_roots=_source_roots(db)
            # A valid backup is not sufficient evidence that it belongs to this
            # existing state. Refuse cross-workspace replacement even when the
            # caller supplied a different but readable O3Data directory.
            current_file=state/'o3.sqlite'
            if current_file.is_file():
                try:
                    with closing(sqlite3.connect(f'file:{current_file.as_posix()}?mode=ro',uri=True)) as existing:
                        current_scopes=json.loads(_meta(existing,'identity_scopes'))
                        current_workspace=_meta(existing,'km_workspace_id')
                        current_roots=_source_roots(existing)
                    if current_scopes!=namespaces:
                        raise RuntimeError('MIGRATION_BLOCKED: restored identity scopes differ from current workspace')
                    if current_workspace and incoming_workspace and current_workspace!=incoming_workspace:
                        raise RuntimeError('MIGRATION_BLOCKED: backup belongs to a different workspace')
                    if current_roots and incoming_roots and current_roots!=incoming_roots:
                        raise RuntimeError('MIGRATION_BLOCKED: backup source root differs from current workspace')
                except sqlite3.DatabaseError:
                    # A damaged current state may require recovery; it cannot
                    # provide trustworthy identity evidence to compare here.
                    # The staged backup is still fully validated before switch.
                    pass
        from o3_core.engine import Engine,Settings
        with Engine(staging,Settings(**namespaces)) as engine:
            if not engine.audit_provenance()['ok'] or not engine.audit_rdf()['ok']:
                raise RuntimeError('BACKUP_VERIFICATION_FAILED: Core provenance/RDF audit')
            if version in (3,4):
                from .service import KMService
                if not KMService(engine,actor='restore:verification',allow_alpha4_catalog=True).audit_workspace()['ok']:
                    raise RuntimeError('BACKUP_VERIFICATION_FAILED: Workspace integrity audit')
        os.replace(state,rollback)
        try:os.replace(staging,state)
        except Exception:os.replace(rollback,state);raise
        return {'restored':True,'rollback_state':str(rollback)}
    finally:
        if staging.exists():shutil.rmtree(staging)


def initialize_workspace(state: Path, *, exclusive_writer_stopped: bool = False, schema_version: int = 3):
    """Publish a NEW empty schema-3 workspace; never migrate or replace saved data.

    The native application holds its stable workspace lock before calling this.
    Reuses this module's schema, defaults and validation, without installing any
    Workspace properties or binding a fabricated source. Existing databases,
    archive contents and legacy settings must use the guarded migration path.
    """
    if not exclusive_writer_stopped:
        raise RuntimeError('STATE_LOCKED: exclusive workspace ownership required')
    if schema_version not in (3,4):raise RuntimeError('SCHEMA_INCOMPATIBLE: unsupported initialization target')
    given = Path(state)
    if given.is_symlink() or any(p.is_symlink() for p in given.parents):
        raise RuntimeError('MIGRATION_BLOCKED: linked workspace path')
    state = given.resolve()
    state.mkdir(parents=True, exist_ok=True)
    allowed = {'.o3-gui.lock', 'workspace.json'}
    if any(p.name not in allowed or not p.is_file() or p.is_symlink() for p in state.iterdir()):
        raise RuntimeError('MIGRATION_BLOCKED: existing state requires explicit migration')
    from o3_core.engine import Engine
    with tempfile.TemporaryDirectory(prefix='.o3-new-', dir=state.parent) as tmp:
        staged = Path(tmp)
        with Engine(staged) as engine:
            db = engine.store.db
            with db:
                for statement in TABLES_SQL.split(';'):
                    if statement.strip(): db.execute(statement)
                metadata = {
                    'km_workspace_id': 'ws_' + uuid.uuid4().hex,
                    'km_workspace_revision': '0', 'km_core_epoch': '0',
                    'workspace_settings_v1': _json(DEFAULT_SETTINGS),
                    'calendar_sources_v1': _json({'primary': {
                        'scope': 'primary', 'path': 'Primary.ics', 'display_name': 'Primary',
                        'state': 'unavailable', 'revision': 1}}),
                    'km_schema_contract': CATALOG_FINGERPRINT,
                }
                for key, value in metadata.items():
                    db.execute('INSERT INTO engine_meta(key,value) VALUES(?,?)', (key, value))
                db.execute('PRAGMA user_version=3')
                validate_schema3(db)
                if schema_version==4:
                    from .workspace_types import extend_schema,validate_v4
                    extend_schema(db)
                    validate_v4(db)
            from .service import KMService
            service = KMService(engine, actor='system:empty-workspace-initialization')
            if not service.audit_workspace()['ok'] or not engine.audit_provenance()['ok']:
                raise RuntimeError('INTEGRITY_FAILED: new workspace validation failed')
            # Closing the only writer checkpoints WAL before atomic publication.
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        target = state / 'o3.sqlite'
        if target.exists():
            raise RuntimeError('STATE_LOCKED: workspace appeared during initialization')
        # The target is previously absent and exclusive ownership is held.
        os.rename(staged / 'o3.sqlite', target)
    return {'created': True, 'schema': schema_version, 'workspace_id': metadata['km_workspace_id']}


def _checkpoint_name(name):
    """Portable relative paths for a whole owned-workspace recovery package."""
    if not isinstance(name,str) or not name or '\\' in name or ':' in name:
        raise RuntimeError('BACKUP_VERIFICATION_FAILED: unsafe checkpoint path')
    parts=PurePosixPath(name).parts
    if (PurePosixPath(name).is_absolute() or PurePosixPath(name).as_posix()!=name or
        any(part in ('','.', '..') or part.endswith((' ','.')) or
            any(ord(c)<32 or c in '<>"|?*' for c in part) or
            PureWindowsPath(part).is_reserved() for part in parts)):
        raise RuntimeError('BACKUP_VERIFICATION_FAILED: unsafe checkpoint path')
    return name


def _checkpoint_layout(state):
    state=Path(state)
    if state.is_symlink() or any(p.is_symlink() for p in state.parents):
        raise RuntimeError('BACKUP_REQUIRED: linked Workspace path')
    state=state.resolve()
    marker=state.parent/'workspace-owner.json'
    if marker.exists() or marker.is_symlink():
        if state.name!='state' or marker.is_symlink() or not marker.is_file() or marker.stat().st_size>4096:
            raise RuntimeError('BACKUP_REQUIRED: invalid Workspace ownership marker')
        value=json.loads(marker.read_text(encoding='utf-8'))
        if (not isinstance(value,dict) or value.get('purpose')!='PERSISTENT_WORKSPACE' or
            value.get('format_version')!=1 or not isinstance(value.get('workspace_id'),str) or
            len(value['workspace_id'])!=32 or type(value.get('database_initialized')) is not bool):
            raise RuntimeError('BACKUP_REQUIRED: invalid Workspace ownership marker')
        return state.parent,'state',value['workspace_id']
    return state,'.',None


def prepare_alpha5_conversion(service):
    """Checkpoint recognized Alpha 4 state before its one audited conversion.

    Called on the existing, exclusively owned engine worker before BEGIN. It
    changes no metadata or settings. SQLite's backup API captures committed WAL
    data; the live lock and SQLite sidecars are deliberately not restorable data.
    The native ownership marker lives one directory above state and is included.
    """
    from .workspace_types import validate_v4
    check=getattr(service,'_startup_check',None)
    def phase(name):
        if check:check()
        callback=getattr(service,'_startup_phase',None)
        if callback is not None:
            try:callback(name)
            except Exception:pass  # Diagnostic I/O must not change conversion.
    if check:check()
    db=service.db
    if db.in_transaction:
        raise RuntimeError('STATE_LOCKED: checkpoint must precede the conversion transaction')
    old=_meta(db,'km_schema_contract')
    if old==CATALOG_FINGERPRINT:
        validate_v4(db)
        return {'required':False,'catalog':old}
    if old!=ALPHA4_CATALOG_FINGERPRINT:
        raise RuntimeError('SCHEMA_INCOMPATIBLE: unsupported Alpha 5 conversion input')
    validate_v4(db,allow_alpha4=True)
    state=Path(service.engine.store.root).resolve()
    phase('checkpoint_source_archive_verification')
    _validate_archive_boundaries(state,db,_check=check)
    phase('checkpoint_provenance_verification')
    _verify_core_provenance_readonly(state,db,_check=check,
        _progress=lambda counts:getattr(service,'_startup_progress',lambda *a:None)('checkpoint_provenance_verification',counts))
    root,state_rel,owner_id=_checkpoint_layout(state)
    phase('checkpoint_source_boundaries')
    roots=_source_roots(db)
    bound=_meta(db,'km_bound_source_root_v1')
    if bound:roots.add(Path(bound).resolve())
    config=state/'workspace.json'
    if config.is_file():
        saved=json.loads(config.read_text(encoding='utf-8'))
        if isinstance(saved,dict) and isinstance(saved.get('source_root'),str) and saved['source_root']:
            roots.add(Path(saved['source_root']).resolve())
    recovery=root.parent/'O3_Recovery'
    if recovery.is_symlink():raise RuntimeError('BACKUP_REQUIRED: linked recovery location')
    _no_overlap(recovery.resolve(),{root,*roots})
    recovery.mkdir(parents=True,exist_ok=True)
    destination=recovery/('alpha4-before-alpha5-'+uuid.uuid4().hex+'.zip')
    pending=destination.with_suffix('.zip.partial')
    db_name='o3.sqlite' if state_rel=='.' else state_rel+'/o3.sqlite'
    manifest={'format':'o3-alpha5-checkpoint/1','created_utc':_utc(),
              'state_relative':state_rel,'ownership_workspace_id':owner_id,
              'km_workspace_id':_meta(db,'km_workspace_id'),
              'workspace_revision':int(_meta(db,'km_workspace_revision','0')),
              'core_epoch':int(_meta(db,'km_core_epoch','0')),
              'catalog':old,'schema_version':4,'sha256':{}}
    try:
        with tempfile.TemporaryDirectory(prefix='o3-alpha5-snapshot-') as tmp:
            phase('checkpoint_database_snapshot')
            snapshot=Path(tmp)/'o3.sqlite'
            with closing(sqlite3.connect(snapshot)) as out, interruptible_sqlite(out,check):
                db.backup(out,pages=128,progress=(lambda *a:check()) if check else None)
                validate_v4(out,allow_alpha4=True)
            entries={db_name:snapshot}
            phase('checkpoint_inventory')
            for file in root.rglob('*'):
                if check:check()
                if file.is_symlink():raise RuntimeError('BACKUP_REQUIRED: linked Workspace entry')
                if not file.is_file():continue
                if file.stat().st_nlink>1:raise RuntimeError('BACKUP_REQUIRED: shared Workspace file')
                if file.parent==state and file.name in ('o3.sqlite','o3.sqlite-wal','o3.sqlite-shm','.o3-gui.lock'):
                    continue
                name=_checkpoint_name(file.relative_to(root).as_posix())
                if name=='checkpoint-manifest.json':
                    raise RuntimeError('BACKUP_REQUIRED: reserved checkpoint filename')
                entries[name]=file
            phase('checkpoint_write')
            with zipfile.ZipFile(pending,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
                for index,(name,file) in enumerate(sorted(entries.items()),1):
                    if check:check()
                    digest=hashlib.sha256()
                    with file.open('rb') as source,archive.open(name,'w',force_zip64=True) as output:
                        for chunk in iter(lambda:source.read(1024*1024),b''):
                            if check:check()
                            digest.update(chunk);output.write(chunk)
                    manifest['sha256'][name]=digest.hexdigest()
                    callback=getattr(service,'_startup_progress',None)
                    if callback:
                        try:callback('checkpoint_write',{'files_completed':index,'files_total':len(entries)})
                        except Exception:pass
                archive.writestr('checkpoint-manifest.json',_json(manifest))
            phase('checkpoint_verify')
            verify_alpha5_checkpoint(pending,_progress=getattr(service,'_startup_progress',None),_check=check)
            if check:check()
            os.replace(pending,destination)
    finally:pending.unlink(missing_ok=True)
    phase('starter_definition_conversion')
    return {'required':True,'path':str(destination),'sha256':_file_hash(destination),
            'verified':True,'format':manifest['format'],'catalog':old,
            'workspace_revision':manifest['workspace_revision'],
            'core_epoch':manifest['core_epoch'],'km_workspace_id':manifest['km_workspace_id'],
            'ownership_marker_included':owner_id is not None,
            'files':len(manifest['sha256'])}


def verify_alpha5_checkpoint(path, *, _extract_to=None, _progress=None, _check=None):
    """Verify every byte and the inherited schema before offering rollback."""
    from .workspace_types import validate_v4
    def report(stage, counts=None):
        if _check:_check()
        if _progress is not None:
            try:_progress(stage, counts)
            except Exception:pass  # Diagnostic failures must not affect verification.
    report('checkpoint_verify_manifest')
    with zipfile.ZipFile(path) as archive:
        names=archive.namelist()
        if (len(set(names))!=len(names) or len({n.casefold() for n in names})!=len(names) or
            names.count('checkpoint-manifest.json')!=1):
            raise RuntimeError('BACKUP_VERIFICATION_FAILED: checkpoint inventory')
        info=archive.getinfo('checkpoint-manifest.json')
        if info.file_size>8_000_000:
            raise RuntimeError('BACKUP_VERIFICATION_FAILED: checkpoint manifest size')
        manifest=json.loads(archive.read(info))
        if (not isinstance(manifest,dict) or manifest.get('format')!='o3-alpha5-checkpoint/1' or
            manifest.get('state_relative') not in ('.','state') or
            manifest.get('catalog')!=ALPHA4_CATALOG_FINGERPRINT or
            manifest.get('schema_version')!=4 or not isinstance(manifest.get('sha256'),dict) or
            set(names)!=set(manifest['sha256'])|{'checkpoint-manifest.json'}):
            raise RuntimeError('BACKUP_VERIFICATION_FAILED: checkpoint manifest')
        with tempfile.TemporaryDirectory(prefix='o3-checkpoint-verify-') as tmp:
            extracted=Path(_extract_to) if _extract_to is not None else Path(tmp)
            extracted.mkdir(parents=True,exist_ok=True)
            counts={'files_completed':0,'files_total':len(manifest['sha256']),
                    'bytes_completed':0,'bytes_total':sum(archive.getinfo(n).file_size for n in manifest['sha256'])}
            report('checkpoint_verify_extract',counts)
            for name,sha in manifest['sha256'].items():
                _checkpoint_name(name)
                info=archive.getinfo(name)
                if (info.is_dir() or (info.external_attr>>16)&0o170000==0o120000 or
                    not isinstance(sha,str) or len(sha)!=64):
                    raise RuntimeError('BACKUP_VERIFICATION_FAILED: checkpoint member')
                target=extracted/name;target.parent.mkdir(parents=True,exist_ok=True)
                digest=hashlib.sha256()
                with archive.open(name) as source,target.open('xb') as output:
                    for chunk in iter(lambda:source.read(1024*1024),b''):
                        digest.update(chunk);output.write(chunk)
                        counts['bytes_completed']+=len(chunk)
                        report('checkpoint_verify_extract',counts)
                if digest.hexdigest()!=sha:
                    raise RuntimeError('BACKUP_VERIFICATION_FAILED: checkpoint checksum')
                counts['files_completed']+=1
                report('checkpoint_verify_extract',counts)
            state=extracted if manifest['state_relative']=='.' else extracted/'state'
            if not (state/'o3.sqlite').is_file():
                raise RuntimeError('BACKUP_VERIFICATION_FAILED: checkpoint database missing')
            report('checkpoint_verify_database_open')
            with closing(sqlite3.connect((state/'o3.sqlite').as_uri()+'?mode=ro',uri=True)) as db, interruptible_sqlite(db,_check):
                report('checkpoint_verify_database_schema')
                validate_v4(db,allow_alpha4=True)
                report('checkpoint_verify_database_identity')
                if (_meta(db,'km_schema_contract')!=manifest['catalog'] or
                    _meta(db,'km_workspace_id')!=manifest.get('km_workspace_id') or
                    int(_meta(db,'km_workspace_revision','-1'))!=manifest.get('workspace_revision') or
                    int(_meta(db,'km_core_epoch','-1'))!=manifest.get('core_epoch')):
                    raise RuntimeError('BACKUP_VERIFICATION_FAILED: checkpoint identity mismatch')
                report('checkpoint_verify_archive_boundaries')
                _validate_archive_boundaries(state,db,_check=_check)
                report('checkpoint_verify_provenance')
                _verify_core_provenance_readonly(state,db,_check=_check,
                    _progress=lambda counts:report('checkpoint_verify_provenance',counts))
            report('checkpoint_verify_ownership')
            if manifest['state_relative']=='state':
                _,_,identity=_checkpoint_layout(state)
                if identity is None or identity!=manifest.get('ownership_workspace_id'):
                    raise RuntimeError('BACKUP_VERIFICATION_FAILED: checkpoint ownership mismatch')
            report('checkpoint_verify_cleanup')
    report('checkpoint_verify_complete')
    return {**manifest,'verified':True}


def restore_alpha5_checkpoint(backup, state, *, exclusive_writer_stopped=False, confirmed=False):
    """Offline whole-workspace rollback; keep the replaced Alpha 5 directory.

    This deliberately is not a running-session action. A native workspace's
    state, config, archive and ownership marker move together, so Alpha 4 can be
    reopened against its matching pre-conversion data after the app is closed.
    """
    if not exclusive_writer_stopped or not confirmed:
        raise RuntimeError('STATE_LOCKED: rollback requires confirmed stopped writer')
    root,state_rel,owner_id=_checkpoint_layout(state)
    backup=Path(backup).resolve()
    _no_overlap(backup,{root})
    check=verify_alpha5_checkpoint(backup)
    if check['state_relative']!=state_rel or check.get('ownership_workspace_id')!=owner_id:
        raise RuntimeError('MIGRATION_BLOCKED: checkpoint belongs to another Workspace owner')
    current=Path(state)/'o3.sqlite'
    if current.is_file():
        with closing(sqlite3.connect(current.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            if _meta(db,'km_workspace_id')!=check['km_workspace_id']:
                raise RuntimeError('MIGRATION_BLOCKED: checkpoint belongs to another Workspace')
            _no_overlap(root,_source_roots(db))
    staging=root.with_name(root.name+'.restore-'+uuid.uuid4().hex)
    rollback=root.with_name(root.name+'.pre-restore-'+uuid.uuid4().hex)
    try:
        verify_alpha5_checkpoint(backup,_extract_to=staging)
        os.replace(root,rollback)
        try:os.replace(staging,root)
        except Exception:
            os.replace(rollback,root);raise
        return {'restored':True,'state':str(Path(state).resolve()),
                'rollback_workspace':str(rollback),'catalog':check['catalog']}
    finally:
        if staging.exists():shutil.rmtree(staging)
