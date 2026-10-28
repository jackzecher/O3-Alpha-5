"""Workspace-owned record types and instances; never alter Core ontology or records.

One small extension of the existing Workspace definition/value/audit authority.
Dynamic type IDs are immutable; properties, rules and views use the same
ws_definitions / ws_values and evaluator as the six protected Core types.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import re
import sqlite3
import uuid
from pathlib import Path
from .evaluation import KMInvalid, Evaluator
from .catalog import TYPES
from o3_core.store import encode, now

TYPE_PREFIX='wsrt_'
RECORD_PREFIX='wsrec_'
TYPE_NAME=re.compile(r'^wsrt_[0-9a-f]{32}$')
RECORD_NAME=re.compile(r'^wsrec_[0-9a-f]{32}$')

# No independent semantic store: these two small tables provide only the
# identities/lifecycle that existing ws_definitions and ws_values do not hold.
EXT_SQL='''
CREATE TABLE ws_record_types(
 type_id TEXT PRIMARY KEY,
 name TEXT NOT NULL,
 plural TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '',
 icon TEXT NOT NULL DEFAULT 'layers',
 nav_order INTEGER NOT NULL DEFAULT 100,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 dashboard_json TEXT NOT NULL DEFAULT '[]',
 created_at_utc TEXT NOT NULL,
 updated_at_utc TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0)
);
CREATE TABLE ws_records(
 record_id TEXT PRIMARY KEY,
 type_id TEXT NOT NULL REFERENCES ws_record_types(type_id),
 title TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at_utc TEXT NOT NULL,
 updated_at_utc TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0)
);
CREATE INDEX ws_records_type_active ON ws_records(type_id,active,title,record_id);
'''

def is_type_id(value):return isinstance(value,str) and TYPE_NAME.fullmatch(value) is not None

def _text(value,limit):
    if not isinstance(value,str) or not 1<=len(value.strip())<=limit or any(ord(c)<32 for c in value):
        raise KMInvalid('INVALID_VALUE','Enter nonempty text (maximum '+str(limit)+' characters)')
    return value.strip()

def _type_public(row):
    if row is None:return None
    return {'type_id':row['type_id'],'name':row['name'],'plural':row['plural'],
      'description':row['description'],'icon':row['icon'],'nav_order':row['nav_order'],
      'active':bool(row['active']), 'dashboard':json.loads(row['dashboard_json']),
      'revision':row['revision']}

def _record_row(row):
    return {'record_id':row['record_id'],'record_type':row['type_id'],
       'scope':'workspace','identity_basis':'workspace_user_created',
       'active':bool(row['active']), 'properties_json':encode({'title':row['title'],
           'created_at_utc':row['created_at_utc'],'updated_at_utc':row['updated_at_utc']}),
       'added_json':'{}','updated_at':row['updated_at_utc'],
       'identity_key':row['record_id'],'source_sha':''}

def validate_v4(db, *, allow_alpha4=False):
    from .migration import validate_workspace_schema, NAMES, CORE_V2_TABLES
    validate_workspace_schema(db,version=4,allow_alpha4=allow_alpha4)
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not (set(NAMES)|CORE_V2_TABLES|{'ws_record_types','ws_records'}).issubset(tables):
        raise RuntimeError('SCHEMA_INCOMPATIBLE: incomplete Workspace v4 schema')
    expected={'ws_record_types':{'type_id','name','plural','description','icon','nav_order','active','dashboard_json','created_at_utc','updated_at_utc','revision'},
              'ws_records':{'record_id','type_id','title','active','created_at_utc','updated_at_utc','revision'}}
    for table,columns in expected.items():
        if {r[1] for r in db.execute('PRAGMA table_info('+table+')')}!=columns:
            raise RuntimeError('SCHEMA_INCOMPATIBLE: invalid '+table+' columns')
    for table,clause in [('ws_definitions','owner_type'),('ws_values','target_kind')]:
        # v3 fixed owner constraints must have been removed during migration.
        sql=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
        if "'WorkspaceOrganization'" in sql:
            raise RuntimeError('SCHEMA_INCOMPATIBLE: old fixed '+clause+' constraint')
    # validate_workspace_schema above already runs both whole-database checks.
    # The extension checks are read-only; repeating those scans here doubles
    # the integrity work on every preflight and every service construction.


def extend_schema(db):
    """Execute inside one caller-owned transaction after full v3 preflight."""
    # Drop fixed allowlists only at the Workspace seam. All dynamic IDs are
    # validated by the service; Core still owns exactly six source types.
    for table in ('ws_definitions','ws_values'):
        db.execute('ALTER TABLE '+table+' RENAME TO _old_'+table)
    db.execute('''CREATE TABLE ws_definitions(
      definition_id TEXT PRIMARY KEY,
      definition_kind TEXT NOT NULL CHECK(definition_kind IN ('property','rule','view','preset')),
      owner_type TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('custom','builtin_override')),
      specification_json TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('enabled','disabled','retired')),
      revision INTEGER NOT NULL CHECK(revision>=1))''')
    db.execute('INSERT INTO ws_definitions SELECT * FROM _old_ws_definitions')
    db.execute('DROP TABLE _old_ws_definitions')
    db.execute('''CREATE TABLE ws_values(
      target_kind TEXT NOT NULL,target_id TEXT NOT NULL,property_id TEXT NOT NULL,
      value_json TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=1),
      PRIMARY KEY(target_kind,target_id,property_id))''')
    db.execute('INSERT INTO ws_values SELECT * FROM _old_ws_values')
    db.execute('DROP TABLE _old_ws_values')
    
    for statement in EXT_SQL.split(';'):
        if statement.strip(): db.execute(statement)  # never implicit-commit the migration
    db.execute('PRAGMA user_version=4')


def _dashboard_spec(service,type_id,items,*,require_enabled=True):
    if not isinstance(items,list) or len(items)>8:
        raise KMInvalid('INVALID_VALUE','Choose up to eight dashboard panels')
    seen=set();normalized=[]
    for item in items:
        if not isinstance(item,dict) or set(item)!={'view_id','mode'} or item['mode'] not in ('count','table'):
            raise KMInvalid('INVALID_VALUE','Dashboard panel must select a view and presentation')
        vid=item['view_id']
        if vid in seen or not isinstance(vid,str):raise KMInvalid('INVALID_VALUE','Duplicate/invalid dashboard view')
        row=service._definition(vid)
        if not row or row['definition_kind']!='view' or (require_enabled and row['state']!='enabled') or row['owner_type']!=type_id:
            raise KMInvalid('UNKNOWN_VIEW','Dashboard requires an enabled view belonging to this record type')
        seen.add(vid);normalized.append({'view_id':vid,'mode':item['mode']})
    return normalized

class WorkspaceTypesMixin:
    def _has_workspace_schema(self):
        return self.db.execute('PRAGMA user_version').fetchone()[0]==4

    def _workspace_type(self,type_id,*,allow_retired=False):
        if not self._has_workspace_schema() or not is_type_id(type_id):return None
        row=self.db.execute('SELECT * FROM ws_record_types WHERE type_id=?',(type_id,)).fetchone()
        if row and (allow_retired or row['active']):return row
        return None

    def _valid_owner(self,owner,*,allow_retired=False):
        from .catalog import TARGETS
        return owner in TARGETS or self._workspace_type(owner,allow_retired=allow_retired) is not None

    def _workspace_row(self,kind,rid,*,allow_retired=False):
        if not is_type_id(kind) or not isinstance(rid,str) or not RECORD_NAME.fullmatch(rid):
            raise KMInvalid('INVALID_TYPE','Invalid user-defined record reference')
        row=self.db.execute('SELECT * FROM ws_records WHERE type_id=? AND record_id=?',(kind,rid)).fetchone()
        if row is None:raise KMInvalid('UNKNOWN_RECORD','Custom record ID/type not found')
        if not self._workspace_type(kind,allow_retired=allow_retired):
            raise KMInvalid('REFERENCE_INACTIVE','Record type is retired')
        return row

    def _workspace_decorate(self,row,as_of,evaluator=None):
        raw=_record_row(row); evaluator=evaluator or Evaluator(self,as_of)
        result={'record_type':row['type_id'],'workspace_type_id':row['type_id'],
          'record_id':row['record_id'],'scope':'workspace',
          'active':bool(row['active']) and self._workspace_type(row['type_id']) is not None,
          'availability':'current' if row['active'] and self._workspace_type(row['type_id']) else 'retained',
          'display_name':row['title'],'properties':json.loads(raw['properties_json']),
          'additional':{},'updated_at':row['updated_at_utc'],
          'identity_basis':'workspace_user_created','authority':'workspace_manual',
          'workspace_properties':{}}
        if result['active']:
            result['workspace_properties']={p:evaluator.evaluate(raw,p)
              for p in evaluator.properties_by_kind.get(row['type_id'],[])}
        else:
            for p in evaluator.properties_by_kind.get(row['type_id'],[]):
                d=evaluator._definitions.get(p)
                if d and d['mode']=='manual':result['workspace_properties'][p]=evaluator.evaluate(raw,p)
        return result

    def _workspace_listing(self,params,as_of):
        typ=params.get('record_type');self._workspace_type_or_error(typ)
        if params.get('sorts'):
            return self._configured_view(as_of,None,{'label':'Collection','record_type':typ,'sorts':params['sorts'],'include_inactive':params.get('include_inactive',False)},params)
        inactive=params.get('include_inactive',False)
        if type(inactive) is not bool:raise KMInvalid('INVALID_TYPE','Invalid inactive filter')
        needle=params.get('search','')
        if not isinstance(needle,str) or len(needle)>160:raise KMInvalid('INVALID_VALUE','Invalid search')
        where='type_id=?'+('' if inactive else ' AND active=1')
        values=[typ]
        if needle:
            where+=' AND (instr(lower(title),lower(?))>0 OR instr(record_id,?)>0)'
            values.extend([needle,needle])
        order=params.get('sort','title')
        if order not in ('title','created_at_utc','updated_at_utc','record_id'):
            raise KMInvalid('INVALID_VALUE','Invalid sort field')
        count=self.db.execute('SELECT COUNT(*) FROM ws_records WHERE '+where,values).fetchone()[0]
        size,offset,meta=self._page_window(params,as_of,'workspace.records:'+typ,count)
        selected=self.db.execute('SELECT * FROM ws_records WHERE '+where+
            ' ORDER BY '+order+' COLLATE NOCASE,record_id LIMIT ? OFFSET ?',(*values,size,offset)).fetchall()
        evaluator=Evaluator(self,as_of);evaluator.prepare_manual_page(typ,selected)
        return {'kind':'workspace_records','rows':[self._workspace_decorate(r,as_of,evaluator) for r in selected],**meta}

    def _workspace_type_or_error(self,type_id,*,allow_retired=False):
        row=self._workspace_type(type_id,allow_retired=allow_retired)
        if row is None:raise KMInvalid('INVALID_TYPE','Unknown or retired custom record type')
        return row

    def _workspace_view(self,params,as_of,*,spec=None):
        """Workspace and source records use the same typed view executor."""
        vid=params.get('view_id')
        if spec is None:
            stored=self._definition(vid)
            if not stored or stored['definition_kind']!='view' or stored['state']!='enabled':
                raise KMInvalid('UNKNOWN_VIEW','Unknown or disabled Workspace view')
            spec=json.loads(stored['specification_json'])
        return self._configured_view(as_of,vid,spec,params)

    def _workspace_dispatch(self,op,params,as_of,expected):
        if not self._has_workspace_schema():
            raise KMInvalid('MIGRATION_BLOCKED','Workspace record types require the guarded schema v4 upgrade')
        if op=='workspace.types':
            include=params.get('include_retired',False)
            return {'record_types':[_type_public(r) for r in self.db.execute(
                 'SELECT * FROM ws_record_types'+('' if include else ' WHERE active=1')+
                 ' ORDER BY nav_order,lower(plural),type_id')]}
        if op=='workspace.type.get':
            return {'record_type':_type_public(self._workspace_type_or_error(params.get('type_id'),allow_retired=True))}
        if op=='workspace.records.query':return self._workspace_listing(params,as_of)
        if op=='workspace.records.get':
            rid=params.get('record_id');typ=params.get('record_type')
            row=self._workspace_row(typ,rid,allow_retired=True)
            evaluator=Evaluator(self,as_of);evaluator.prepare_manual_page(typ,[row])
            obj=self._workspace_decorate(row,as_of,evaluator)
            audit=[dict(x) for x in self.db.execute('SELECT * FROM ws_audit WHERE subject_id=? ORDER BY event_id',(rid,))]
            # Explicit user references are Workspace values, not Core ontology edges.
            links=[]
            for p,value in obj.get('workspace_properties',{}).items():
                d=self.property_definition(p)
                if d and d['value_type'] in ('record_ref','record_ref_set') and value.get('value') is not None:
                    refs=value['value'] if d['value_type']=='record_ref_set' else [value['value']]
                    retained=set(value.get('retained_reference_ids',[]))
                    links.extend({'property_id':p,'target':ref,'authority':'workspace_manual',
                                  'availability':'retained' if ref['record_id'] in retained else 'current'} for ref in refs)
            detail={'evidence':{'authority':'workspace_manual','source_trace':{},'user_operations':audit},
                    'history':{'workspace_audit':audit,'core_trace':{}},
                    'rules':{'workspace_results':obj.get('workspace_properties',{}),'core_rules':[]},
                    'relations':{**self._relationships_for(typ,rid),'workspace_references':links},'neighbors':{}}
            return {'record':obj,'detail':detail}
        if op=='workspace.dashboard':
            typ=self._workspace_type_or_error(params.get('type_id'))
            panels=json.loads(typ['dashboard_json']);items=[]
            for item in panels:
                existing=self._definition(item['view_id'])
                r=(self._workspace_view({'view_id':item['view_id'],'page_size':min(20,params.get('page_size',20))},as_of)
                    if existing and existing['state']=='enabled' else {'state':'configuration_required','reason':'Dashboard view disabled or retired'})
                items.append({'view_id':item['view_id'],
                  'label':json.loads(existing['specification_json']).get('label',item['view_id']) if existing else item['view_id'],
                  'mode':item['mode'],
                  'total_count':r.get('total_count') if r['state']=='available' else None,
                  'state':r['state'],'rows':r.get('rows',[]) if item['mode']=='table' else [],
                  'reason':r.get('reason')})
            return {'type_id':typ['type_id'],'panels':items}
        if op=='workspace.view.query':return self._workspace_view(params,as_of,spec=params.get('specification'))
        action=op.split('.')[-1]
        if op.startswith('workspace.type.'):
            typ=params.get('type_id')
            before=_type_public(self._workspace_type(typ,allow_retired=True)) if typ else None
            if action=='create':
                typ=typ or TYPE_PREFIX+uuid.uuid4().hex
                if not is_type_id(typ) or before or self._workspace_type(typ,allow_retired=True):
                    raise KMInvalid('INVALID_VALUE','Invalid or duplicate Workspace record-type identity')
                changes={k:params.get(k) for k in ('name','plural','description','icon','nav_order')}
                after={'type_id':typ,'name':_text(changes['name'],120),'plural':_text(changes['plural'],120),
                       'description':(changes['description'] or '').strip(),'icon':changes['icon'] or 'layers',
                       'nav_order':changes['nav_order'] if changes['nav_order'] is not None else 100,
                       'active':True,'dashboard':[],'revision':1}
            else:
                if before is None:raise KMInvalid('INVALID_TYPE','Record type not found')
                after=dict(before)
                if action=='edit':
                    for k in ('name','plural','description','icon','nav_order','dashboard'):
                        if k in params:after[k]=params[k]
                elif action=='retire':after['active']=False
                elif action=='restore':after['active']=True
                else:raise KMInvalid('UNKNOWN_OPERATION','Unknown record-type action')
                after['revision']=before['revision']+1
            after['name']=_text(after['name'],120);after['plural']=_text(after['plural'],120)
            # The rail must not offer indistinguishable Core or Workspace entries.
            from .catalog import TYPES
            reserved={t.casefold() for t in TYPES}
            reserved.update({'line of effort','line of efforts','loe','loes'})
            for name in (after['name'],after['plural']):
                if name.casefold() in reserved:
                    raise KMInvalid('INVALID_VALUE','Name conflicts with a protected Core record type')
            for item in self.db.execute('SELECT type_id,name,plural FROM ws_record_types'):
                if item['type_id'] != typ and {after['name'].casefold(),after['plural'].casefold()} & {item['name'].casefold(),item['plural'].casefold()}:
                    raise KMInvalid('INVALID_VALUE','A Workspace record type already uses this name')
            if not isinstance(after['description'],str) or len(after['description'])>600:
                raise KMInvalid('INVALID_VALUE','Invalid record-type description')
            if after['icon'] not in ('layers','file-text','check-square','flag','briefcase','users','clipboard-list','bookmark','folder','target'):
                raise KMInvalid('INVALID_VALUE','Unsupported record-type icon')
            if type(after['nav_order']) is not int or not 0<=after['nav_order']<=10000:
                raise KMInvalid('INVALID_VALUE','Invalid navigation order')
            if action=='edit' and 'dashboard' in params:
                after['dashboard']=_dashboard_spec(self,typ,after['dashboard'])
            if before and before['active'] is False and action=='edit':
                raise KMInvalid('CONFIGURATION_REQUIRED','Restore retired record type before editing')
            def write():
                if before is None:
                    self.db.execute('INSERT INTO ws_record_types VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (typ,after['name'],after['plural'],after['description'],after['icon'],after['nav_order'],1,encode([]),now(),now(),1))
                elif {k:v for k,v in before.items() if k!='revision'}!={k:v for k,v in after.items() if k!='revision'}:
                    self.db.execute('''UPDATE ws_record_types SET name=?,plural=?,description=?,icon=?,nav_order=?,active=?,dashboard_json=?,updated_at_utc=?,revision=revision+1 WHERE type_id=?''',
                        (after['name'],after['plural'],after['description'],after['icon'],after['nav_order'],int(after['active']),encode(after['dashboard']),now(),typ))
                return before,after if before is None or {k:v for k,v in before.items() if k!='revision'}!={k:v for k,v in after.items() if k!='revision'} else before,{'type_id':typ,'dependencies':[typ]}
            return self._commit(op,'workspace_type',typ,expected,write)
        if op.startswith('workspace.record.'):
            typ=params.get('record_type');self._workspace_type_or_error(typ)
            rid=params.get('record_id');before=None
            if action=='create':
                rid=rid or RECORD_PREFIX+uuid.uuid4().hex
                if not RECORD_NAME.fullmatch(rid) or self.db.execute('SELECT 1 FROM ws_records WHERE record_id=?',(rid,)).fetchone():
                    raise KMInvalid('INVALID_VALUE','Invalid or duplicate record identifier')
                title=_text(params.get('title'),240);after={'record_id':rid,'record_type':typ,'title':title,'active':True,'revision':1}
            else:
                existing=self._workspace_row(typ,rid,allow_retired=True)
                before={'record_id':rid,'record_type':typ,'title':existing['title'],'active':bool(existing['active']),'revision':existing['revision']}
                after=dict(before)
                if action=='edit':after['title']=_text(params.get('title'),240)
                elif action=='archive':after['active']=False
                elif action=='restore':after['active']=True
                else:raise KMInvalid('UNKNOWN_OPERATION','Unknown Workspace record operation')
                after['revision']=before['revision']+1
            def write():
                if before is None:self.db.execute('INSERT INTO ws_records VALUES(?,?,?,?,?,?,?)',
                    (rid,typ,after['title'],1,now(),now(),1))
                elif any(before[k]!=after[k] for k in ('title','active')):
                    self.db.execute('UPDATE ws_records SET title=?,active=?,updated_at_utc=?,revision=revision+1 WHERE record_id=?',
                        (after['title'],int(after['active']),now(),rid))
                else:return before,before,{'dependencies':[rid]}
                return before,after,{'record_id':rid,'dependencies':[rid,typ]}
            return self._commit(op,'workspace_record',rid,expected,write)
        raise KMInvalid('UNKNOWN_OPERATION','Unknown Workspace record operation')


def initialize_workspace_v4(state, *, exclusive_writer_stopped=False):
    """Initializes a new empty Workspace with no silent upgrade of existing state."""
    from .migration import initialize_workspace
    from o3_core.engine import Engine,Settings
    state=Path(state)
    return initialize_workspace(state,exclusive_writer_stopped=exclusive_writer_stopped,schema_version=4)


def migrate_v3_to_v4(state,backup,*,independent_location_approved=False,
                     source_root=None,exclusive_writer_stopped=False,actor='migration:authorized'):
    """Safe opt-in migration; verified independently survivable backup before writes."""
    from .migration import validate_schema3,verified_backup,_validate_archive,_verify_core_provenance_readonly
    if not exclusive_writer_stopped:
        raise RuntimeError('STATE_LOCKED: migration requires exclusive stopped writer')
    state=Path(state).resolve(); dbfile=state/'o3.sqlite'
    if source_root is None or not dbfile.is_file():
        raise RuntimeError('MIGRATION_BLOCKED: existing state and source root required')
    with sqlite3.connect(dbfile.as_uri()+'?mode=ro',uri=True) as db:
        validate_schema3(db,allow_alpha4=True)
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('INTEGRITY_FAILED: pre-migration SQLite state')
        _validate_archive(state,db)
        _verify_core_provenance_readonly(state,db)
        before=[db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in
            ('records','relationships','source_observations','record_history','ws_definitions','ws_values','ws_audit')]
    receipt=verified_backup(state,Path(backup),
                 source_root=Path(source_root),independent_location_approved=independent_location_approved)
    with sqlite3.connect(dbfile,timeout=10) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=OFF')  # only while rebuilding unreferenced Workspace tables
        db.execute('BEGIN IMMEDIATE')
        try:
            validate_schema3(db,allow_alpha4=True)
            extend_schema(db)
            current=[db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in
                ('records','relationships','source_observations','record_history','ws_definitions','ws_values','ws_audit')]
            if current!=before:raise RuntimeError('MIGRATION_BLOCKED: state rows changed')
            validate_v4(db,allow_alpha4=True)
            db.commit()
        except Exception:
            db.rollback();raise
        finally:db.execute('PRAGMA foreign_keys=ON')
    return {'migrated':True,'schema':4,'backup':receipt,'preserved_counts':True}
