"""In-process versioned o3-km contract. Caller owns the ONE Core Engine/SQLite writer.

KMService is invoked on the existing engine worker, not a GUI thread or server.
All mutations require an optimistic workspace revision. No implicit migrations.
"""
from __future__ import annotations
import base64
from datetime import datetime,date,timezone,timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from o3_core.engine import Engine
from o3_core.store import KINDS,digest,encode,now
from o3_core.rules import loe
from .catalog import (TYPES,TARGETS,PRESETS,PROPERTIES,TEMPLATES,VIEWS,VIEW_PRESET,CORE_PREDICATES,template_expression,
                      CATALOG_FINGERPRINT,DEFAULT_SETTINGS,addresses,patterns,email_address)
from .evaluation import Evaluator, KMInvalid,validate_expression,ALLOWED_FIELDS
from .migration import validate_schema3,verified_backup,verify_backup,restore_backup
from .queries import QueryMixin
from .workspace_types import WorkspaceTypesMixin,is_type_id,validate_v4,_record_row
from .property_commands import MAX_BATCH as MAX_ACTION_ANCHORS
from .reply_evidence import MAX_CHAIN

VERSION='o3-km/1.2'
def _plain(x):return json.loads(x) if isinstance(x,str) else x
def _dt(v):
    if isinstance(v,datetime):return v
    if not isinstance(v,str):raise KMInvalid('INVALID_VALUE','Invalid as_of timestamp')
    try:d=datetime.fromisoformat(v.replace('Z','+00:00'))
    except ValueError:raise KMInvalid('INVALID_VALUE','Invalid ISO timestamp')
    if d.tzinfo is None:raise KMInvalid('INVALID_VALUE','as_of requires time zone')
    return d.astimezone(timezone.utc)
def _id(prefix):return prefix+uuid.uuid4().hex
def _as(v):return v.isoformat(timespec='seconds').replace('+00:00','Z')
def _safe_text(v,maxlen):
    if not isinstance(v,str) or not 1<=len(v.strip())<=maxlen or any(ord(c)<32 for c in v):
        raise KMInvalid('INVALID_VALUE',f'Expected text of at most {maxlen} characters')
    return v.strip()

def _record(row):
    if row is None:return None
    return {'record_id':row['record_id'],'record_type':row['record_type'], 'scope':row['scope'],
      'identity_basis':row['identity_basis'],'active':bool(row['active']),
      'properties':_plain(row['properties_json']),'additional':_plain(row['added_json']),
      'updated_at':row['updated_at'],'authority':'core'}

class KMService(WorkspaceTypesMixin,QueryMixin):
    def __init__(self,engine:Engine,*,actor:str,allow_alpha4_catalog=False):
        if not isinstance(actor,str) or not actor.strip() or len(actor)>128:
            raise KMInvalid('INVALID_VALUE','An authorized actor identity is required')
        self.engine=engine;self.db=engine.store.db;self.actor=actor.strip()
        (validate_v4(self.db,allow_alpha4=allow_alpha4_catalog) if self._has_workspace_schema() else validate_schema3(self.db,allow_alpha4=allow_alpha4_catalog))
        if self.db.execute('PRAGMA foreign_key_check').fetchone():raise KMInvalid('INTEGRITY_FAILED','Foreign-key integrity failure')
        self._validate_ws_integrity()
        self._check_legacy_config_boundary()

    def _check_legacy_config_boundary(self):
        """A retained pre-v3 JSON is recovery evidence, never live configuration."""
        file=Path(self.engine.store.root)/'workspace-rules.json'
        if file.exists():
            if file.is_symlink() or not file.is_file():
                raise KMInvalid('LEGACY_CONFIG_CHANGED','Legacy configuration location is invalid')
            receipt=self._meta('km_legacy_config_receipt_v1')
            sha=hashlib.sha256(file.read_bytes()).hexdigest()
            if receipt!=sha:
                raise KMInvalid('LEGACY_CONFIG_CHANGED','Legacy settings changed after migration; explicit reconciliation is required')

    def _validate_ws_integrity(self):
        """Audit the authoritative Workspace state before exposing KM operations."""
        if self.revision()<0 or self.epoch()<0 or not self.wsid():
            raise KMInvalid('INTEGRITY_FAILED','Invalid Workspace metadata')
        saved=self.settings()
        if not isinstance(saved,dict) or set(saved)!=set(DEFAULT_SETTINGS):
            raise KMInvalid('INTEGRITY_FAILED','Invalid Workspace settings structure')
        try:
            if not isinstance(saved['workspace_timezone'],str):raise ValueError()
            ZoneInfo(saved['workspace_timezone'])
            if saved['week_start'] not in ('monday','sunday'):raise ValueError()
            for key in ('self_addresses','group_addresses'):
                if addresses(saved[key])!=saved[key]:raise ValueError()
            labels=saved['calendar_display_names']
            if not isinstance(labels,dict):raise ValueError()
            sources=_plain(self._meta('calendar_sources_v1','{}'))
            if not isinstance(sources,dict) or set(labels)-set(sources):raise ValueError()
            if any(not isinstance(k,str) or not isinstance(v,str) or
                   not 1<=len(v.strip())<=120 or any(ord(c)<32 for c in v)
                   for k,v in labels.items()):raise ValueError()
        except (ValueError,KeyError,TypeError,ZoneInfoNotFoundError):
            raise KMInvalid('INTEGRITY_FAILED','Invalid saved Workspace setting') from None
        audit_rows=self.db.execute('SELECT workspace_revision FROM ws_audit ORDER BY workspace_revision').fetchall()
        if audit_rows:
            numbers=[int(row[0]) for row in audit_rows]
            if numbers!=list(range(1,self.revision()+1)):
                raise KMInvalid('INTEGRITY_FAILED','Workspace audit revision discontinuity')
        elif self.revision()!=0:
            raise KMInvalid('INTEGRITY_FAILED','Missing Workspace revision audit')
        org_ids={r[0] for r in self.db.execute('SELECT ws_org_id FROM ws_organizations')}
        counts={r['ws_org_id']:r['n'] for r in self.db.execute('SELECT ws_org_id,COUNT(*) n FROM ws_org_members GROUP BY ws_org_id')}
        for r in self.db.execute('SELECT ws_org_id,archived FROM ws_organizations'):
            number=counts.get(r['ws_org_id'],0)
            if (r['archived'] and number) or (not r['archived'] and not number):
                raise KMInvalid('INTEGRITY_FAILED','Workspace Organization membership invariant')
        for r in self.db.execute('SELECT core_org_id,ws_org_id FROM ws_org_members'):
            src=self.engine.store.row(r['core_org_id'])
            if src is None or src['record_type']!='Organization' or r['ws_org_id'] not in org_ids:
                raise KMInvalid('INTEGRITY_FAILED','Organization membership references wrong kind')
        parents={}
        for r in self.db.execute("SELECT target_id,value_json FROM ws_values WHERE property_id='ws:parent_command'"):
            v=_plain(r['value_json'])
            dest=v.get('record_id') if isinstance(v,dict) else v
            if r['target_id'] not in org_ids or dest not in org_ids:
                raise KMInvalid('INTEGRITY_FAILED','Workspace hierarchy reference missing')
            parents[r['target_id']]=dest
        for child in parents:
            visited=set();p=child
            while p in parents:
                if p in visited:raise KMInvalid('ORG_HIERARCHY_CYCLE','Workspace hierarchy cycle')
                visited.add(p);p=parents[p]
        for r in self.db.execute('SELECT definition_id,definition_kind,owner_type,origin,state,specification_json FROM ws_definitions'):
            try:spec=_plain(r['specification_json'])
            except (ValueError,TypeError):raise KMInvalid('INTEGRITY_FAILED','Invalid Workspace definition JSON')
            if not isinstance(spec,dict):raise KMInvalid('INTEGRITY_FAILED','Invalid Workspace definition shape')
            if r['origin']=='builtin_override':
                sources={'preset':PRESETS,'view':VIEWS}
                if r['definition_kind'] not in sources or r['definition_id'] not in sources[r['definition_kind']]:
                    raise KMInvalid('INTEGRITY_FAILED','Unknown built-in definition override')
            elif r['origin']!='custom':raise KMInvalid('INTEGRITY_FAILED','Unknown definition provenance')
            if r['definition_kind']=='property' and r['origin']=='custom':
                # Stored definitions must remain valid even with zero current values.
                # Recipe attribution and immutable IDs are not editable schema fields.
                create_keys={'label','description','mode','value_type','max_chars',
                    'allowed_values','ref_type','max_count','nullable'}
                if (spec.get('property_id')!=r['definition_id'] or
                    spec.get('owner_type')!=r['owner_type'] or spec.get('origin')!='custom'):
                    raise KMInvalid('INTEGRITY_FAILED','Saved property identity mismatch')
                try:self._property_spec({k:v for k,v in spec.items() if k in create_keys},
                                         r['owner_type'],r['definition_id'],allow_retired=True)
                except (KMInvalid,ValueError,KeyError,TypeError):
                    raise KMInvalid('INTEGRITY_FAILED','Invalid saved Workspace property') from None
                if r['definition_id']=='ws:display_name' and (
                    r['owner_type']!='WorkspaceOrganization' or spec.get('mode')!='manual' or
                    spec.get('value_type')!='text' or spec.get('max_chars',160)>160):
                    raise KMInvalid('INTEGRITY_FAILED','Invalid required Organization display-name constraint')
            if r['definition_kind']=='view' and r['origin']=='custom' and r['state']=='enabled':
                try:self._validate_view_spec(spec,allow_retired=True)
                except KMInvalid as exc:
                    # A previously valid view may outlive a retired property;
                    # query returns configuration_required, never false-empty.
                    if exc.code!='UNKNOWN_PROPERTY':
                        raise KMInvalid('INTEGRITY_FAILED','Invalid saved Workspace view') from None
                except (ValueError,KeyError,TypeError):
                    raise KMInvalid('INTEGRITY_FAILED','Invalid saved Workspace view') from None
        if self._has_workspace_schema():
            seen_type_labels=set()
            for r in self.db.execute('SELECT * FROM ws_record_types'):
                from .workspace_types import _text,_dashboard_spec
                try:
                    if not is_type_id(r['type_id']):raise ValueError()
                    if _text(r['name'],120)!=r['name'] or _text(r['plural'],120)!=r['plural']:raise ValueError()
                    if not isinstance(r['description'],str) or len(r['description'])>600:raise ValueError()
                    if r['icon'] not in ('layers','file-text','check-square','flag','briefcase','users','clipboard-list','bookmark','folder','target'):raise ValueError()
                    if type(r['nav_order']) is not int or not 0<=r['nav_order']<=10000:raise ValueError()
                    if r['active'] not in (0,1) or r['revision']<1:raise ValueError()
                    labels={r['name'].casefold(),r['plural'].casefold()}
                    if seen_type_labels & labels or labels & ({k.casefold() for k in TYPES}|{'line of effort','line of efforts','loe','loes'}):
                        raise ValueError()
                    seen_type_labels.update(labels)
                    _dashboard_spec(self,r['type_id'],_plain(r['dashboard_json']),require_enabled=False)
                except (ValueError,TypeError,KMInvalid):
                    raise KMInvalid('INTEGRITY_FAILED','Invalid saved custom type') from None
            for r in self.db.execute('SELECT * FROM ws_records'):
                from .workspace_types import RECORD_NAME,_text
                try:
                    if (not self._workspace_type(r['type_id'],allow_retired=True)
                        or not isinstance(r['record_id'],str) or not RECORD_NAME.fullmatch(r['record_id'])
                        or _text(r['title'],240)!=r['title']):
                        raise ValueError()
                    # A record with an archived parent remains historically preserved;
                    # the parent's lifecycle controls present-day availability.
                    if r['active'] not in (0,1) or r['revision']<1:
                        raise ValueError()
                except (ValueError,KMInvalid,TypeError):
                    raise KMInvalid('INTEGRITY_FAILED','Orphan or malformed Workspace record') from None
        for r in self.db.execute('SELECT definition_id,owner_type FROM ws_definitions'):
            if not self._valid_owner(r['owner_type'],allow_retired=True):
                raise KMInvalid('INTEGRITY_FAILED','Workspace definition references unknown record type')
        producers={}
        for r in self.db.execute("SELECT * FROM ws_definitions WHERE definition_kind='rule' AND state='enabled'"):
            spec=_plain(r['specification_json']);p=spec.get('output_property_id')
            if p in producers:
                raise KMInvalid('DUPLICATE_PRODUCER','Duplicate active Workspace rule producer')
            producers[p]=r['definition_id']
            fields=self._expression_fields(r['owner_type'])
            definition=self.property_definition(p)
            if not definition or definition.get('mode')!='computed' or definition['owner_type']!=r['owner_type']:
                raise KMInvalid('INTEGRITY_FAILED','Workspace rule output missing or incompatible')
            deps=validate_expression(spec.get('expression'),r['owner_type'],fields)['dependencies']
            if deps!=set(spec.get('dependencies',[])):
                raise KMInvalid('INTEGRITY_FAILED','Workspace rule dependency metadata mismatch')
            if p in deps:raise KMInvalid('DEPENDENCY_CYCLE','Self-referencing Workspace rule')
        for r in self.db.execute("SELECT * FROM ws_definitions WHERE definition_kind='rule' AND state='enabled'"):
            spec=_plain(r['specification_json'])
            self._check_rule_cycles(r['definition_id'],spec['output_property_id'],set(spec['dependencies']))
        for r in self.db.execute('SELECT target_kind,target_id,property_id,value_json FROM ws_values'):
            definition=self.property_definition(r['property_id'],include_retired=True)
            if definition is None or definition.get('owner_type')!=r['target_kind'] or definition.get('mode')!='manual':
                raise KMInvalid('INTEGRITY_FAILED','Invalid persisted Workspace value')
            if r['target_kind']=='WorkspaceOrganization':
                if r['target_id'] not in org_ids:raise KMInvalid('INTEGRITY_FAILED','Missing Workspace Organization')
            else:
                if is_type_id(r['target_kind']):
                    item=self.db.execute('SELECT type_id FROM ws_records WHERE record_id=?',(r['target_id'],)).fetchone()
                    valid=item is not None and item['type_id']==r['target_kind']
                else:
                    row=self.engine.store.row(r['target_id'])
                    valid=row is not None and row['record_type']==r['target_kind']
                if not valid:
                    raise KMInvalid('INTEGRITY_FAILED','Workspace value references missing or wrong record type')
            try:
                value=_plain(r['value_json'])
                # The database stores only valid, typed manual values. Audits do not repair.
                canonical=self._validate_manual(definition,value,r['target_kind'],r['target_id'],allow_retained_refs=True)
                if canonical!=value:
                    raise KMInvalid('INTEGRITY_FAILED','Persisted Workspace reference is not canonical')
            except (ValueError,TypeError,KeyError,KMInvalid):
                raise KMInvalid('INTEGRITY_FAILED','Invalid persisted Workspace value or reference')

        receipt=self._meta('alpha5_starter_conversion_v1')
        if receipt:
            try:
                converted=_plain(receipt)
                audit=self.db.execute("SELECT after_json FROM ws_audit WHERE operation='view.starters.ensure' ORDER BY workspace_revision LIMIT 1").fetchone()
                if not audit or _plain(audit[0])!=converted or converted.get('catalog')!=CATALOG_FINGERPRINT:
                    raise ValueError()
            except (ValueError,KeyError,TypeError):
                raise KMInvalid('INTEGRITY_FAILED','Invalid shared-view conversion receipt') from None
        from .mail_tasks import validate_dispositions
        validate_dispositions(self)

    def _meta(self,key,default=None):
        r=self.db.execute('SELECT value FROM engine_meta WHERE key=?',(key,)).fetchone()
        return r[0] if r else default
    def _setmeta(self,key,value):
        self.db.execute('INSERT INTO engine_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key,str(value)))
    def contract_version(self):return VERSION if self._has_workspace_schema() else 'o3-km/1.1'
    def wsid(self):return self._meta('km_workspace_id')
    def revision(self):return int(self._meta('km_workspace_revision',0))
    def epoch(self):return int(self._meta('km_core_epoch',0))
    def settings(self):return _plain(self._meta('workspace_settings_v1'))
    def coverage(self,kind=None):
        c=_plain(self._meta('km_source_coverage_v1','{}'))
        if kind=='Email':
            statuses=(c.get('MailInbox','unknown'),c.get('MailSent','unknown'))
            return ('complete' if all(x=='complete' for x in statuses) else
                    'unknown' if all(x=='unknown' for x in statuses) else 'partial')
        aliases={'Event':'CalendarICS','Person':'ContactsProfiles','Document':'DocumentCorpus'}
        return c.get(aliases.get(kind,kind),'unknown') if kind else c

    def property_definition(self,p,*,include_retired=False):
        # Templates never become active properties until installed by the user.
        r=self.db.execute("SELECT * FROM ws_definitions WHERE definition_id=? AND definition_kind='property'",(p,)).fetchone()
        if not r or (r['state']=='retired' and not include_retired):return None
        return {**_plain(r['specification_json']),'status':r['state'],'origin':'custom'}

    def installed_template_properties(self,preset_id):
        """Configured user properties sourced from a selectable recipe."""
        found=[]
        for row in self.db.execute("SELECT definition_id,specification_json FROM ws_definitions WHERE definition_kind='property' AND state!='retired'"):
            if _plain(row['specification_json']).get('source_preset_id')==preset_id:
                found.append(row['definition_id'])
        return sorted(found)

    def custom_rule_for(self,property_id):
        for r in self.db.execute("SELECT * FROM ws_definitions WHERE definition_kind='rule' AND state='enabled'"):
            spec=_plain(r['specification_json'])
            if spec.get('output_property_id')==property_id:return spec
        return None

    def effective_id(self,core_org_id):
        row=self.db.execute('SELECT ws_org_id FROM ws_org_members WHERE core_org_id=?',(core_org_id,)).fetchone()
        return row[0] if row else core_org_id

    def manual_value(self,kind,rid,p):
        if kind=='WorkspaceOrganization' and p=='ws:display_name':
            r=self.db.execute('SELECT display_name FROM ws_organizations WHERE ws_org_id=?',(rid,)).fetchone()
            return r[0] if r else None
        r=self.db.execute('SELECT value_json FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?',
                          (kind,rid,p)).fetchone()
        return _plain(r[0]) if r else None

    def retained_reference_ids(self,definition,value):
        """Qualify saved manual links without treating source inactivity as data loss."""
        t=definition.get('value_type')
        if t not in ('record_ref','record_ref_set') or value is None:return []
        refs=value if t=='record_ref_set' else [value]
        retained=[]
        for ref in refs:
            row=self._assert_target(ref['record_type'],ref['record_id'],active=False)
            if (bool(row['archived']) if ref['record_type']=='WorkspaceOrganization' else
                (not bool(row['active']) or (is_type_id(ref['record_type']) and not self._workspace_type(ref['record_type'])))):
                retained.append(ref['record_id'])
        return retained

    def _assert_target(self,kind,rid,active=True):
        if not self._valid_owner(kind,allow_retired=True):raise KMInvalid('INVALID_TYPE','Unsupported target type')
        if is_type_id(kind):
            r=self._workspace_row(kind,rid,allow_retired=True)
            if active and (not r['active'] or not self._workspace_type(kind)):
                raise KMInvalid('REFERENCE_INACTIVE','Custom record or type is inactive')
            return _record_row(r)
        if kind=='WorkspaceOrganization':
            r=self.db.execute('SELECT * FROM ws_organizations WHERE ws_org_id=?',(rid,)).fetchone()
            if r is None:raise KMInvalid('UNKNOWN_RECORD','Unknown Workspace Organization')
            if active and r['archived']:raise KMInvalid('REFERENCE_INACTIVE','Workspace Organization archived')
        else:
            r=self.engine.store.row(rid)
            if r is None or r['record_type']!=kind:raise KMInvalid('UNKNOWN_RECORD','Record ID/type not found')
            if active and not r['active']:raise KMInvalid('REFERENCE_INACTIVE','Record is inactive')
        return r

    def _assert_revision(self,expected):
        if isinstance(expected,bool) or not isinstance(expected,int):
            raise KMInvalid('REVISION_CONFLICT','Expected numeric Workspace revision is required')
        if expected!=self.revision():raise KMInvalid('REVISION_CONFLICT','Workspace revision has changed')

    def _commit(self,op,subject_kind,subject_id,expected,fn,*,core=False):
        self._assert_revision(expected)
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self._assert_revision(expected)
            before,after,extra=fn()
            if before==after:
                self.db.rollback()
                return {'changed':False,'new_workspace_revision':expected,'affected_core_ids':[],
                        'affected_property_ids':[],'affected_view_ids':[],**extra}
            newrev=expected+1
            self.db.execute('''INSERT INTO ws_audit(workspace_revision,actor,operation,subject_kind,subject_id,
                before_json,after_json,dependencies_json,at_utc) VALUES(?,?,?,?,?,?,?,?,?)''',
                (newrev,self.actor,op,subject_kind,subject_id,
                 encode(before) if before is not None else None,encode(after) if after is not None else None,
                 encode(extra.get('dependencies',[])),now()))
            self._setmeta('km_workspace_revision',newrev)
            if core:self._setmeta('km_core_epoch',self.epoch()+1)
            self.db.commit()
            return {'changed':True,'new_workspace_revision':newrev,
                    'affected_core_ids':extra.get('core_ids',[]),'affected_property_ids':extra.get('properties',[]),
                    'affected_view_ids':extra.get('views',[]),**{k:v for k,v in extra.items() if k not in ('core_ids','properties','views')}}
        except Exception:
            self.db.rollback()
            raise

    def _definition(self,id):
        r=self.db.execute('SELECT * FROM ws_definitions WHERE definition_id=?',(id,)).fetchone()
        return dict(r) if r else None
    def _def_obj(self,r):return {**dict(r),'specification':_plain(r['specification_json'])} if r else None
    def _put_def(self,id,kind,owner,origin,spec,state):
        row=self._definition(id)
        rev=row['revision']+1 if row else 1
        self.db.execute('''INSERT INTO ws_definitions VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(definition_id) DO UPDATE SET specification_json=excluded.specification_json,
              state=excluded.state, revision=excluded.revision''',
              (id,kind,owner,origin,encode(spec),state,rev))

    def _save_settings(self,params,expected):
        if set(params)-{'workspace_timezone','week_start','self_addresses','group_addresses','calendar_display_names'}:
            raise KMInvalid('INVALID_VALUE','Unknown shared setting')
        prev=self.settings();n=dict(prev)
        for k,v in params.items():
            if k=='workspace_timezone':
                if not isinstance(v,str):raise KMInvalid('INVALID_VALUE','Invalid time zone')
                try:ZoneInfo(v)
                except (ZoneInfoNotFoundError,ValueError):raise KMInvalid('INVALID_VALUE','Unknown IANA time zone')
            elif k=='week_start' and v not in ('monday','sunday'):
                raise KMInvalid('INVALID_VALUE','Invalid week start')
            elif k in ('self_addresses','group_addresses'):
                try:v=addresses(v)
                except ValueError:raise KMInvalid('INVALID_VALUE','Invalid configured email addresses')
            elif k=='calendar_display_names':
                if not isinstance(v,dict):raise KMInvalid('INVALID_TYPE','Calendar display map required')
                registry=_plain(self._meta('calendar_sources_v1','{}'))
                if set(v)-set(registry):raise KMInvalid('CALENDAR_SCOPE_CONFLICT','Unknown registered calendar')
                v={a:_safe_text(label,120) for a,label in v.items()}
            n[k]=v
        def fn():
            self._setmeta('workspace_settings_v1',encode(n))
            return prev,n,{'properties':[],'dependencies':list(params)}
        return self._commit('configuration.save','configuration','workspace',expected,fn)

    def _validate_ref(self,ref,allowed,*,allow_retained=False):
        if isinstance(ref,str):
            self._assert_target(allowed,ref,active=not allow_retained)
            return {'record_type':allowed,'record_id':ref}
        if not isinstance(ref,dict) or set(ref)!={'record_type','record_id'} or ref['record_type']!=allowed:
            raise KMInvalid('INVALID_TYPE','Incorrect typed record reference')
        self._assert_target(allowed,ref['record_id'],active=not allow_retained)
        return {'record_type':allowed,'record_id':ref['record_id']}

    def _validate_manual(self,definition,value,target_kind=None,target_id=None,*,allow_retained_refs=False):
        if definition['mode']!='manual':raise KMInvalid('INVALID_TYPE','Computed property cannot be manually assigned')
        t=definition['value_type']
        if t=='boolean':
            if not isinstance(value,bool):raise KMInvalid('INVALID_TYPE','Manual boolean requires true or false')
        elif t=='text':value=_safe_text(value,definition.get('max_chars',240))
        elif t=='enum':
            if value not in definition.get('allowed_values',[]):raise KMInvalid('INVALID_VALUE','Unrecognized enum ID')
        elif t=='date':
            try:
                if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:raise ValueError()
            except ValueError:raise KMInvalid('INVALID_VALUE','Expected ISO calendar date')
        elif t=='record_ref':value=self._validate_ref(value,definition['ref_type'],allow_retained=allow_retained_refs)
        elif t=='record_ref_set':
            if not isinstance(value,list) or len(value)>definition.get('max_count',100):
                raise KMInvalid('INVALID_VALUE','Invalid reference count')
            vals=[self._validate_ref(v,definition['ref_type'],allow_retained=allow_retained_refs) for v in value]
            value=sorted({x['record_id']:x for x in vals}.values(),key=lambda x:x['record_id'])
        else:raise KMInvalid('INVALID_TYPE','Unsupported property type')
        if definition['property_id']=='ws:parent_command' and value is not None:
            if value['record_id']==target_id:raise KMInvalid('ORG_HIERARCHY_CYCLE','Self-parent forbidden')
            seen={target_id};current=value['record_id']
            while current:
                if current in seen:raise KMInvalid('ORG_HIERARCHY_CYCLE','Parent command cycle')
                seen.add(current)
                ref=self.manual_value('WorkspaceOrganization',current,'ws:parent_command')
                current=ref['record_id'] if isinstance(ref,dict) else None
        return value

    def _manual_mutation(self,params,expected,clear=False):
        if params.get('property_id')!='ws:display_name':
            from .property_commands import batch
            return batch(self,{'records':[{'record_type':params.get('record_type'),'record_id':params.get('record_id')}],
                'property_id':params.get('property_id'),'operation':'clear' if clear else 'set',
                **({'value':params.get('value')} if not clear else {})},expected)
        kind=params.get('record_type');rid=params.get('record_id');p=params.get('property_id')
        definition=self.property_definition(p)
        if not definition:raise KMInvalid('UNKNOWN_PROPERTY','Property does not exist')
        if definition['owner_type']!=kind:raise KMInvalid('INVALID_TYPE','Property owner mismatch')
        if clear and (definition.get('nullable') is False or p=='ws:display_name'):
            raise KMInvalid('INVALID_VALUE','Required property cannot be cleared')
        self._assert_target(kind,rid)
        value=None if clear else self._validate_manual(definition,params.get('value'),kind,rid)
        previous=self.manual_value(kind,rid,p)
        def fn():
            if value is None:
                self.db.execute('DELETE FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?',(kind,rid,p))
            elif p=='ws:display_name':
                self.db.execute('UPDATE ws_organizations SET display_name=?, updated_at_utc=?,revision=revision+1 WHERE ws_org_id=?',
                                (value,now(),rid))
            else:
                r=self.db.execute('SELECT revision FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?',(kind,rid,p)).fetchone()
                self.db.execute('''INSERT INTO ws_values VALUES(?,?,?,?,?) ON CONFLICT(target_kind,target_id,property_id)
                    DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision''',
                    (kind,rid,p,encode(value),r[0]+1 if r else 1))
            return previous,value,{'properties':[p],'core_ids':[rid] if kind!='WorkspaceOrganization' else [],'dependencies':[rid,p]}
        return self._commit('manual.clear' if clear else 'manual.set',kind,rid,expected,fn)

    def _def_mutation(self,kind,action,params,expected):
        if kind=='preset':return self._preset_mutation(action,params,expected)
        if kind=='property' and action in ('set','clear'):
            return self._manual_mutation(params,expected,clear=action=='clear')
        id=params.get('definition_id') or params.get(kind+'_id')
        if action=='create':
            expected_prefix='view:user:' if kind=='view' else 'ws:user:'
            if id and (not isinstance(id,str) or not id.startswith(expected_prefix)):
                raise KMInvalid('INVALID_VALUE','Cannot create reserved definition')
            id=id or _id('view:user:' if kind=='view' else 'ws:user:')
            if self._definition(id):raise KMInvalid('INVALID_VALUE','Definition ID already in use')
            owner=params.get('record_type') or params.get('owner_type')
            if not self._valid_owner(owner):raise KMInvalid('INVALID_TYPE','Owner must be a supported record type')
            spec=params.get('specification')
            if not isinstance(spec,dict):raise KMInvalid('INVALID_VALUE','Definition required')
            spec=dict(spec)
            if kind=='property':
                spec=self._property_spec(spec,owner,id)
            elif kind=='rule':
                p=spec.get('output_property_id');d=self.property_definition(p)
                if not d or d['owner_type']!=owner or d['mode']!='computed':
                    raise KMInvalid('INVALID_TYPE','Computed output property required')
                fields=self._expression_fields(owner)
                deps=validate_expression(spec.get('expression'),owner,fields)['dependencies']
                self._check_expression_references(spec['expression'],owner)
                if p in deps:raise KMInvalid('DEPENDENCY_CYCLE','Rule cannot depend on own output')
                if self.custom_rule_for(p):raise KMInvalid('DUPLICATE_PRODUCER','Producer already exists')
                spec.update({'rule_id':id,'target_record_type':owner,'dependencies':sorted(deps),'rule_version':1})
                self._check_rule_cycles(id,p,deps)
            elif kind=='view':
                spec.update({'view_id':id,'record_type':owner,'version':1})
                spec['dependencies']=sorted(self._validate_view_spec(spec))
            state='enabled' if params.get('enabled',True) else 'disabled'
            def fn():
                self._put_def(id,kind,owner,'custom',spec,state)
                return None,{'specification':spec,'state':state},{'definition_id':id,'properties':[id] if kind=='property' else [],'views':[id] if kind=='view' else [],'dependencies':spec.get('dependencies',[])}
            return self._commit(kind+'.create',kind,id,expected,fn)
        row=self._definition(id)
        if kind=='view' and id in VIEWS:return self._builtin_view(action,id,params,expected)
        if row is None:
            raise KMInvalid('UNKNOWN_'+('VIEW' if kind=='view' else 'RULE' if kind=='rule' else 'PROPERTY'),'Unknown definition')
        if row['definition_kind']!=kind or row['origin']!='custom':raise KMInvalid('INVALID_VALUE','Cannot redefine built-in entry')
        prev=self._def_obj(row)
        if is_type_id(row['owner_type']) and not self._workspace_type(row['owner_type']):
            raise KMInvalid('CONFIGURATION_REQUIRED','Restore retired record type before changing its definitions')
        if row['state']=='retired' and action!='retire':
            raise KMInvalid('CONFIGURATION_REQUIRED','Retired definitions cannot be reactivated without migration')
        if kind=='view' and action in ('disable','retire') and is_type_id(row['owner_type']):
            type_row=self._workspace_type(row['owner_type'],allow_retired=True)
            if type_row and id in [p['view_id'] for p in _plain(type_row['dashboard_json'])]:
                raise KMInvalid('CONFIGURATION_REQUIRED','Remove the view from its dashboard before disabling or retiring it')
        if action in ('disable','retire') and action=='retire':
            for r in self.db.execute("SELECT * FROM ws_definitions WHERE definition_kind IN ('rule','view') AND state='enabled'"):
                definition=_plain(r['specification_json'])
                dep=set(definition.get('dependencies',[]))
                if r['definition_kind']=='view':
                    dep.update(f.get('field') for f in definition.get('filters',[]) if isinstance(f,dict)
                               and isinstance(f.get('field'),str) and f['field'].startswith('ws:'))
                # Rules depend on their output property, not on the producer's rule ID.
                if kind=='rule' and r['definition_kind']=='rule':
                    target=_plain(row['specification_json']).get('output_property_id')
                    if target in dep and r['definition_id']!=id:
                        raise KMInvalid('CONFIGURATION_REQUIRED','Live dependent rule must be disabled first')
                elif id in dep:
                    raise KMInvalid('CONFIGURATION_REQUIRED','Live dependent definition must be disabled first')
        spec=dict(prev['specification'])
        if action=='edit':
            updates=params.get('changes',{})
            if not isinstance(updates,dict):raise KMInvalid('INVALID_VALUE','Changes required')
            if kind=='property':
                self._check_property_changes(id,spec,updates)
            if kind=='rule' and set(updates)-{'expression','description'}:
                raise KMInvalid('INVALID_VALUE','Rule output identity immutable')
            spec.update(updates)
            if kind=='rule':
                fields=self._expression_fields(row['owner_type'])
                deps=validate_expression(spec['expression'],row['owner_type'],fields)['dependencies']
                self._check_expression_references(spec['expression'],row['owner_type'])
                self._check_rule_cycles(id,spec['output_property_id'],deps)
                spec['dependencies']=sorted(deps)
            if kind=='view':
                if spec.get('record_type')!=row['owner_type'] or spec.get('view_id')!=id:
                    raise KMInvalid('INVALID_VALUE','View identity and record type are immutable')
                spec['dependencies']=sorted(self._validate_view_spec(spec))
        elif action not in ('disable','enable','retire'):
            raise KMInvalid('INVALID_VALUE','Unsupported definition mutation')
        state='retired' if action=='retire' else 'disabled' if action=='disable' else 'enabled' if action=='enable' else row['state']
        if kind=='rule' and state=='enabled':
            self._check_expression_references(spec['expression'],row['owner_type'])
            deps=validate_expression(spec['expression'],row['owner_type'],self._expression_fields(row['owner_type']))['dependencies']
            if deps!=set(spec.get('dependencies',[])) or spec['output_property_id'] in deps:
                raise KMInvalid('DEPENDENCY_CYCLE','Rule dependencies must be consistent and acyclic')
            self._check_rule_cycles(id,spec['output_property_id'],deps)
            for other in self.db.execute("SELECT definition_id,specification_json FROM ws_definitions WHERE definition_kind='rule' AND state='enabled'"):
                if other['definition_id']!=id and _plain(other['specification_json']).get('output_property_id')==spec.get('output_property_id'):
                    raise KMInvalid('DUPLICATE_PRODUCER','Only one active rule may produce a computed property')
        after={**prev,'specification':spec,'state':state};
        def fn():
            if prev['specification']==spec and prev['state']==state:
                return prev,prev,{'definition_id':id,'dependencies':spec.get('dependencies',[])}
            self._put_def(id,kind,row['owner_type'],'custom',spec,state)
            return prev,after,{'definition_id':id,'dependencies':spec.get('dependencies',[])}
        return self._commit(kind+'.'+action,kind,id,expected,fn)

    def _check_property_changes(self,id,spec,updates):
        # Compatible settings can be edited in place. Type, cardinality,
        # owner and stable ID remain immutable once defined.
        if set(updates)-{'label','description','max_chars','max_count','allowed_values'}:
            raise KMInvalid('INVALID_VALUE','Property type/identity changes require migration')
        if 'label' in updates:_safe_text(updates['label'],160)
        if 'description' in updates and (not isinstance(updates['description'],str) or len(updates['description'])>2000):
            raise KMInvalid('INVALID_VALUE','Invalid description')
        if 'max_chars' in updates:
            limit=updates['max_chars']
            if (spec.get('value_type')!='text' or type(limit) is not int or
                not 1<=limit<=(160 if id=='ws:display_name' else 240)):
                raise KMInvalid('INVALID_VALUE','Text limit requires a bounded text property')
            for val in self.db.execute('SELECT value_json FROM ws_values WHERE property_id=?',(id,)):
                item=_plain(val[0])
                if isinstance(item,str) and len(item)>limit:
                    raise KMInvalid('CONFIGURATION_REQUIRED','Existing text exceeds proposed limit')
        if 'allowed_values' in updates:
            choices=updates['allowed_values']
            if (spec.get('value_type')!='enum' or not isinstance(choices,list) or
                not 1<=len(choices)<=32 or len(set(str(x) for x in choices))!=len(choices) or
                any(not isinstance(x,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}',x) for x in choices)):
                raise KMInvalid('INVALID_VALUE','Invalid enum configuration')
            for val in self.db.execute('SELECT value_json FROM ws_values WHERE property_id=?',(id,)):
                if _plain(val[0]) not in choices:
                    raise KMInvalid('CONFIGURATION_REQUIRED','Existing enum value cannot be removed')
        if 'max_count' in updates:
            count=updates['max_count']
            if spec.get('value_type')!='record_ref_set' or type(count) is not int or not 1<=count<=100:
                raise KMInvalid('INVALID_VALUE','Reference count requires a bounded reference set')
            for val in self.db.execute('SELECT value_json FROM ws_values WHERE property_id=?',(id,)):
                if len(_plain(val[0]))>count:
                    raise KMInvalid('CONFIGURATION_REQUIRED','Existing references exceed proposed limit')

    def _configuration_plan(self,params):
        """Compose the existing typed property/rule definitions, without writing.

        This is a narrowly scoped atomic form operation, not a batch executor.
        The original property/rule operations remain supported. No Core writes.
        """
        allowed={'record_type','property_id','specification','rule','enabled',
                 'page_size','explain_record_id'}
        if set(params)-allowed:
            raise KMInvalid('INVALID_VALUE','Unknown property configuration option')
        owner=params.get('record_type');pid=params.get('property_id')
        if not self._valid_owner(owner) or not isinstance(pid,str) or not pid or len(pid)>180:
            raise KMInvalid('INVALID_TYPE','A typed property and stable draft identifier are required')
        existing=self._definition(pid)
        if existing:
            if (existing['definition_kind']!='property' or existing['origin']!='custom' or
                existing['owner_type']!=owner or existing['state']=='retired'):
                raise KMInvalid('INVALID_VALUE','Property cannot be configured in this target')
        elif not pid.startswith('ws:user:'):
            raise KMInvalid('INVALID_VALUE','New properties require a custom identifier')
        supplied=params.get('specification')
        proposed=self._property_spec(supplied,owner,pid)
        oldspec=_plain(existing['specification_json']) if existing else None
        if oldspec:
            for key in ('mode','value_type','ref_type'):
                if proposed.get(key)!=oldspec.get(key):
                    raise KMInvalid('INVALID_VALUE','Existing type, ownership and reference target are immutable')
            if 'nullable' in supplied and supplied['nullable']!=oldspec.get('nullable',True):
                raise KMInvalid('INVALID_VALUE','Existing nullability is immutable')
            changes={key:value for key,value in supplied.items()
                     if key in ('label','description','max_chars','max_count','allowed_values')}
            self._check_property_changes(pid,oldspec,changes)
            # Keep recipe attribution and any existing noneditable metadata.
            proposed={**oldspec,**changes}
        enabled=params.get('enabled',existing['state']=='enabled' if existing else True)
        if type(enabled) is not bool:raise KMInvalid('INVALID_TYPE','Enabled must be Boolean')
        state='enabled' if enabled else 'disabled'
        before={'property':{'specification':oldspec,'state':existing['state']} if existing else None,
                'rule':None}
        after={'property':{'specification':proposed,'state':state},'rule':None}
        rule=params.get('rule');rid=None;deps=set()
        if rule is not None:
            if proposed['mode']!='computed' or not isinstance(rule,dict):
                raise KMInvalid('INVALID_TYPE','Only a computed property can have a rule')
            if set(rule)-{'rule_id','expression','description','enabled'}:
                raise KMInvalid('INVALID_VALUE','Unknown rule configuration option')
            rid=rule.get('rule_id')
            if not isinstance(rid,str) or not rid.startswith('ws:user:') or len(rid)>180 or rid==pid:
                raise KMInvalid('INVALID_VALUE','A distinct custom rule identifier is required')
            prior=self._definition(rid)
            oldrule=_plain(prior['specification_json']) if prior else None
            if prior and (prior['definition_kind']!='rule' or prior['owner_type']!=owner or
                          prior['origin']!='custom' or prior['state']=='retired' or
                          oldrule.get('output_property_id')!=pid):
                raise KMInvalid('INVALID_VALUE','Rule does not belong to this property or is retired')
            description=rule.get('description',oldrule.get('description','') if oldrule else '')
            if not isinstance(description,str) or len(description)>2000:
                raise KMInvalid('INVALID_VALUE','Rule description must be bounded text')
            rule_enabled=rule.get('enabled',prior['state']=='enabled' if prior else True)
            if type(rule_enabled) is not bool:raise KMInvalid('INVALID_TYPE','Rule enabled must be Boolean')
            fields=self._expression_fields(owner);fields[pid]='boolean'
            deps=validate_expression(rule.get('expression'),owner,fields)['dependencies']
            self._check_expression_references(rule['expression'],owner)
            if pid in deps:raise KMInvalid('DEPENDENCY_CYCLE','Rule cannot depend on its own output')
            self._check_rule_cycles(rid,pid,deps)
            if rule_enabled:
                for other in self.db.execute("SELECT definition_id,specification_json FROM ws_definitions WHERE definition_kind='rule' AND state='enabled'"):
                    if other['definition_id']!=rid and _plain(other['specification_json']).get('output_property_id')==pid:
                        raise KMInvalid('DUPLICATE_PRODUCER','Only one enabled rule may produce a property')
            newrule={**(oldrule or {}),'rule_id':rid,'target_record_type':owner,
                     'output_property_id':pid,'expression':rule['expression'],
                     'description':description,'dependencies':sorted(deps),
                     'rule_version':oldrule.get('rule_version',1) if oldrule else 1}
            before['rule']={'specification':oldrule,'state':prior['state']} if prior else None
            after['rule']={'specification':newrule,'state':'enabled' if rule_enabled else 'disabled'}
        elif proposed['mode']=='computed' and not existing:
            raise KMInvalid('CONFIGURATION_REQUIRED','Add a rule to the new computed property')
        return owner,pid,rid,before,after,deps

    def _configure_property(self,params,expected,as_of,*,preview=False):
        if preview:
            owner,pid,rid,before,after,deps=self._configuration_plan(params)
            result={'valid':True,'property':after['property']['specification'],
                    'rule':after['rule']['specification'] if rid else None,
                    'property_state':after['property']['state'],
                    'rule_state':after['rule']['state'] if rid else None,
                    'dependencies':sorted(deps),'no_mutation':True}
            if rid:
                # Preview the proposed expression even if saved definitions are disabled.
                # The status above makes that distinction explicit to the form.
                result.update(self._preview_rule({'record_type':owner,
                    'specification':{'expression':after['rule']['specification']['expression']},
                    'page_size':params.get('page_size',25),
                    **({'explain_record_id':params['explain_record_id']} if params.get('explain_record_id') else {})},as_of))
            return result
        # The complete plan is revalidated under the existing serialized writer lock.
        def write():
            owner,pid,rid,before,after,deps=self._configuration_plan(params)
            if before!=after:
                if before['property']!=after['property']:
                    self._put_def(pid,'property',owner,'custom',after['property']['specification'],after['property']['state'])
                if rid and before['rule']!=after['rule']:
                    self._put_def(rid,'rule',owner,'custom',after['rule']['specification'],after['rule']['state'])
            return before,after,{'property_id':pid,'rule_id':rid,'properties':[pid],
                                 'dependencies':sorted(deps)}
        return self._commit('property.configure','property',params.get('property_id'),expected,write)

    def _preview_rule(self,params,as_of):
        """Bounded, read-only preview using the one existing evaluator.

        Detailed explanations are generated only for the explicitly selected
        record, not for every row. No UI-side evaluation or cached results.
        """
        expr=params.get('expression') or params.get('specification',{}).get('expression')
        owner=params.get('record_type') or params.get('owner_type')
        fields=self._expression_fields(owner)
        valid=validate_expression(expr,owner,fields)
        self._check_expression_references(expr,owner)
        from .catalog import RULE_LIMITS
        cap=params.get('page_size',RULE_LIMITS['preview_rows'])
        if type(cap) is not int or not 1<=cap<=RULE_LIMITS['preview_rows']:
            raise KMInvalid('INVALID_VALUE','Preview size exceeds the supported bound')
        evaluator=Evaluator(self,as_of)
        selected=(list(self.db.execute('SELECT * FROM ws_records WHERE type_id=? AND active=1 ORDER BY record_id LIMIT ?',(owner,cap+1)))
                  if is_type_id(owner) else list(self.db.execute("SELECT * FROM records WHERE record_type=? AND active=1 ORDER BY record_id LIMIT ?",(owner,cap+1))))
        more=len(selected)>cap;selected=selected[:cap]
        rows=[]
        for row in selected:
            if is_type_id(owner):row=_record_row(row)
            evaluator._unknown=None
            value=evaluator.expression(dict(row),expr);failure=evaluator._unknown
            status='evaluated' if value is not None else (failure[0] if failure else 'not_evaluable')
            # Reuse the sampled row. Preview labels need no participant traversal,
            # correspondence lookup, source body or full record-detail payload.
            properties=_plain(row['properties_json']);added=_plain(row['added_json'])
            label_properties={key:properties[key] for key in ('subject','summary','displayName','name') if key in properties}
            for section,key in (('metadata','title'),('source','filename')):
                if isinstance(properties.get(section),dict) and key in properties[section]:
                    label_properties[section]={key:properties[section][key]}
            ref={'record_id':row['record_id'],'record_type':owner,'active':bool(row['active']),
                 'properties':label_properties,
                 'additional':{key:added[key] for key in ('display_label','display_title') if key in added}}
            rows.append({'record_id':row['record_id'],'record_type':owner,
                         'record':ref,
                         'state':status,'value':value,
                         'reason':failure[1] if value is None and failure else None})
        result={'preview':rows,'preview_limit':cap,'has_more':more,'no_mutation':True,
                'dependencies':sorted(valid['dependencies']),
                'sample_order':'record_id','as_of':_as(as_of)}
        if params.get('explain_record_id') is not None:
            rid=params['explain_record_id']
            if not isinstance(rid,str):raise KMInvalid('INVALID_TYPE','A selected record identifier is required')
            row=self._assert_target(owner,rid)
            # Evaluate each bounded subtree through the same evaluator. This is
            # deliberate on-demand diagnostic work, not another rule interpreter.
            def explain(node):
                evaluator._unknown=None;value=evaluator.expression(dict(row),node)
                failure=evaluator._unknown
                result={'state':'evaluated' if value is not None else (failure[0] if failure else 'not_evaluable'),
                        'value':value,'reason':failure[1] if value is None and failure else None}
                group=next((key for key in ('all','any','not') if key in node),None)
                if group:
                    result['group']=group
                    result['children']=[explain(term) for term in ([node[group]] if group=='not' else node[group])]
                else:result['condition']=node
                return result
            result['explanation']={'record_id':rid,'tree':explain(expr)}
        return result

    def _check_rule_cycles(self,new_id,output,deps):
        graph={}
        for r in self.db.execute("SELECT * FROM ws_definitions WHERE definition_kind='rule' AND state='enabled'"):
            spec=_plain(r['specification_json'])
            if r['definition_id']!=new_id:graph[spec['output_property_id']]=set(spec.get('dependencies',[]))
        graph[output]=set(deps)
        def check(node,seen):
            if node in seen:raise KMInvalid('DEPENDENCY_CYCLE','Workspace rule cycle')
            for dep in graph.get(node,()):check(dep,seen|{node})
        for node in graph:check(node,set())

    def _expression_fields(self,owner):
        """Only explicitly configured fields may be referenced by a user rule."""
        return {r['definition_id']:_plain(r['specification_json']).get('value_type')
            for r in self.db.execute("SELECT definition_id,specification_json FROM ws_definitions WHERE definition_kind='property' AND owner_type=? AND state!='retired'",(owner,))}

    def _view_field_catalog(self,owner):
        """Typed view fields; presentation labels are metadata, not a second schema.

        Sorting/grouping is restricted to scalar facts. Reference values remain
        available as columns and predicates, never sorted by guessed display names.
        """
        from .evaluation import FIELD_KINDS,SCALAR_KINDS,fields_for
        if not (owner in TYPES or self._workspace_type(owner,allow_retired=True)):return []
        fields={name:{'field_id':name,'value_type':kind,'authority':'workspace' if is_type_id(owner) else 'core',
                    'label':re.sub(r'(?<=[a-z])(?=[A-Z])',' ',name).replace('_',' ').capitalize(),
                    'state':'available'} for name,kind in fields_for(owner).items()}
        for name,kind,label in (('record_id','text','Record ID'),('record_type','text','Record type'),
                                ('scope','text','Source scope'),('updated_at','timestamp','Engine updated (UTC)')):
            fields[name]={'field_id':name,'value_type':kind,'label':label,'authority':'core','state':'available'}
        if owner=='Email':
            fields['reply_status'].update({'origin':'evidence_computation','read_only':True,'label':'Reply status','allowed_values':['replied','no_reply_found','unknown']})
            fields['sender_identity'].update({'origin':'evidence_computation','read_only':True,'label':'Sender identity','allowed_values':['self','external','unknown']})
            fields['source_time'].update({'origin':'normalized_source_time','read_only':True,'label':'Sent / received'})
        for row in self.db.execute("SELECT definition_id,specification_json,state FROM ws_definitions WHERE definition_kind='property' AND owner_type=? AND state!='retired' ORDER BY definition_id",(owner,)):
            d=_plain(row['specification_json']);name=row['definition_id']
            fields[name]={'field_id':name,'value_type':d['value_type'],'authority':'workspace',
                          'label':d.get('label',d.get('display_label',name)), 'state':row['state'],
                          'mode':d['mode'], **({'ref_type':d['ref_type']} if d.get('ref_type') else {})}
        return [{**field,'sortable':field['value_type'] in SCALAR_KINDS,
                 'groupable':field['value_type'] in SCALAR_KINDS,
                 'grouping':'local_calendar_day' if field['value_type']=='timestamp' else 'value'}
                for field in fields.values()]

    def _validate_view_spec(self,spec,*,allow_retired=False):
        # Query options that cannot be applied must never be silently persisted.
        supported={'label','name','record_type','view_id','version','dependencies',
                   'description','visible','pinned','order','section','kind','filters',
                   'filter_expression','sorts','sort','sort_direction','group_by',
                   'group_direction','include_inactive','search','parameters','group_by_fields','representative_sort','unknown_policy'}
        if not isinstance(spec,dict) or set(spec)-supported:
            raise KMInvalid('INVALID_VALUE','Unsupported saved view setting')
        if not isinstance(spec.get('label',spec.get('name')),str):
            raise KMInvalid('INVALID_VALUE','View label required')
        _safe_text(spec.get('label',spec.get('name')),160)
        owner=spec.get('record_type')
        if not self._valid_owner(owner,allow_retired=allow_retired) or owner=='WorkspaceOrganization':raise KMInvalid('INVALID_TYPE','Custom views require one supported record type')
        if spec.get('description'):
            _safe_text(spec['description'],600)
        for key in ('visible','include_inactive'):
            if key in spec and type(spec[key]) is not bool:
                raise KMInvalid('INVALID_TYPE',key+' must be Boolean')
        search=spec.get('search','')
        if not isinstance(search,str) or len(search)>160 or any(ord(c)<32 for c in search):
            raise KMInvalid('INVALID_VALUE','Invalid saved view search')
        filters=spec.get('filters',[])
        if not isinstance(filters,list) or len(filters)>12:raise KMInvalid('INVALID_VALUE','Invalid view filters')
        dependencies=set();expression=spec.get('filter_expression')
        if expression is not None:
            if filters:raise KMInvalid('INVALID_VALUE','Use one filter expression, not two competing filters')
            valid=validate_expression(expression,owner,self._expression_fields(owner))
            self._check_expression_references(expression,owner,allow_retired=allow_retired);dependencies.update(valid['dependencies'])
        for f in filters:
            if not isinstance(f,dict) or f.get('operator') not in ('equals','not_equals','is_present','one_of'):
                raise KMInvalid('INVALID_VALUE','Unsupported legacy view filter')
            checked=validate_expression(f,owner,self._expression_fields(owner))
            self._check_expression_references(f,owner,allow_retired=allow_retired);dependencies.update(checked['dependencies'])
        fields={x['field_id']:x for x in self._view_field_catalog(owner)}
        if spec.get('unknown_policy','exclude') not in ('include','exclude'):
            raise KMInvalid('INVALID_VALUE','Unknown-result policy must be include or exclude')
        compound=spec.get('group_by_fields',[])
        if not isinstance(compound,list) or len(compound)>3 or len(set(compound))!=len(compound) or any(not isinstance(f,str) or f not in fields or not fields[f]['groupable'] for f in compound):
            raise KMInvalid('INVALID_VALUE','Choose up to three typed grouping fields')
        if compound and spec.get('group_by') not in (None,'none'):
            raise KMInvalid('INVALID_VALUE','Choose one grouping definition')
        representative=spec.get('representative_sort')
        if representative is not None and (not isinstance(representative,dict) or set(representative)!={'field','direction'} or representative['field'] not in fields or not fields[representative['field']]['sortable'] or representative['direction'] not in ('asc','desc')):
            raise KMInvalid('INVALID_VALUE','Invalid group representative ordering')
        dependencies.update(field for field in compound if field.startswith('ws:'))
        if representative and representative['field'].startswith('ws:'):dependencies.add(representative['field'])
        sorts=spec.get('sorts')
        if sorts is not None:
            if not isinstance(sorts,list) or not 1<=len(sorts)<=3:
                raise KMInvalid('INVALID_VALUE','Choose one to three ordering fields')
            seen=set()
            for item in sorts:
                if not isinstance(item,dict) or set(item)!={'field','direction'}:
                    raise KMInvalid('INVALID_VALUE','Ordering requires a field and direction')
                field=item['field']
                if not isinstance(field,str) or field not in fields or not fields[field]['sortable']:
                    raise KMInvalid('INVALID_VALUE','Unsupported ordering field')
                if field in seen:raise KMInvalid('INVALID_VALUE','Each ordering field may be selected once')
                seen.add(field)
                if item['direction'] not in ('asc','desc'):raise KMInvalid('INVALID_VALUE','Invalid sort direction')
                if field.startswith('ws:'):dependencies.add(field)
        if spec.get('sort_direction','asc') not in ('asc','desc'):
            raise KMInvalid('INVALID_VALUE','Invalid sort direction')
        if 'sort' in spec:
            field=spec['sort']
            if not isinstance(field,str) or field not in fields or not fields[field]['sortable']:
                raise KMInvalid('INVALID_VALUE','Unsupported sort key')
        grouping=spec.get('group_by')
        if grouping not in (None,'none'):
            if not isinstance(grouping,str) or grouping not in fields or not fields[grouping]['groupable']:
                raise KMInvalid('INVALID_VALUE','Unsupported grouping')
            if grouping.startswith('ws:'):dependencies.add(grouping)
        if spec.get('group_direction','asc') not in ('asc','desc'):
            raise KMInvalid('INVALID_VALUE','Invalid group direction')
        if spec.get('order') is not None and (type(spec['order']) is not int or not 0<=spec['order']<=10000):
            raise KMInvalid('INVALID_VALUE','View order out of bounds')
        return dependencies

    def _builtin_view(self,action,id,params,expected):
        b=VIEWS[id]
        if action not in ('edit','disable','enable') or b.get('protected') and action=='disable':
            raise KMInvalid('INVALID_VALUE','Protected view cannot be disabled/retired')
        existing=self._definition(id);prev=self._def_obj(existing) if existing else None
        spec={'visible':True,'order':b.get('order',0)} if not prev else dict(prev['specification'])
        if action=='edit':
            changes=params.get('changes',{})
            if not isinstance(changes,dict) or set(changes)-{'visible','order','label'}:
                raise KMInvalid('INVALID_VALUE','Only display overrides allowed')
            spec.update(changes)
            if type(spec.get('visible')) is not bool:raise KMInvalid('INVALID_TYPE','Visibility must be Boolean')
            if type(spec.get('order')) is not int or not 0<=spec['order']<=10000:raise KMInvalid('INVALID_VALUE','View order out of bounds')
            if 'label' in spec:_safe_text(spec['label'],160)
            if b.get('protected') and spec.get('visible') is False:raise KMInvalid('INVALID_VALUE','Required view cannot be hidden')
        else:spec['visible']=action=='enable'
        after={'specification':spec,'state':'enabled' if spec['visible'] else 'disabled'}
        def fn():
            if (prev is not None and prev['specification']==spec and prev['state']==after['state']) or (prev is None and spec=={'visible':b.get('enabled_default',False),'order':b.get('order',0)} and after['state']==('enabled' if b.get('enabled_default') else 'disabled')):
                return prev,prev,{'views':[id]}
            self._put_def(id,'view',b['owner_type'],'builtin_override',spec,after['state'])
            return prev,after,{'views':[id]}
        return self._commit('view.'+action,'view',id,expected,fn)

    def _preset_mutation(self,action,params,expected):
        """Optional template instantiation, not a separate rule or property engine.

        A single transaction creates ordinary, editable property/rule definitions.
        Applying a recipe never runs a built-in evaluator or installs an implicit
        view, and does not affect any other Workspace or Core record.
        """
        preset=params.get('preset_id')
        if not isinstance(preset,str) or not preset:
            raise KMInvalid('INVALID_TYPE','Template ID must be a nonempty string')
        if preset not in TEMPLATES:raise KMInvalid('UNKNOWN_RULE','Unknown Workspace template')
        template=TEMPLATES[preset]
        settings=params.get('settings',{})
        if not isinstance(settings,dict):raise KMInvalid('INVALID_VALUE','Template settings must be an object')
        defaults=template.get('default_settings',{})
        if set(settings)-set(defaults):raise KMInvalid('INVALID_VALUE','Unknown template parameter')
        settings={**defaults,**settings}
        if 'sender_patterns' in settings:
            try:settings['sender_patterns']=patterns(settings['sender_patterns'])
            except (ValueError,TypeError):raise KMInvalid('INVALID_VALUE','Invalid sender pattern list')
        if 'age_calendar_days' in settings and (type(settings['age_calendar_days']) is not int
                                            or not 0<=settings['age_calendar_days']<=365):
            raise KMInvalid('INVALID_VALUE','Invalid calendar age threshold')
        for key in ('selected_person_ids','selected_organization_ids','selected_loe_ids'):
            if key not in settings:continue
            values=settings[key]
            if not isinstance(values,list) or len(values)>100 or any(not isinstance(v,str) for v in values):
                raise KMInvalid('INVALID_TYPE','Selected references must be a bounded ID list')
            values=sorted(set(values));settings[key]=values
            if key=='selected_person_ids':type_name='Person'
            elif key=='selected_loe_ids':type_name='LineOfEffort'
            else:type_name=None
            for rid in values:
                if type_name:self._assert_target(type_name,rid)
                else:
                    core=self.engine.store.row(rid)
                    if not (core and core['record_type']=='Organization'):
                        self._assert_target('WorkspaceOrganization',rid)
        # A user can select a different Workspace ID to create multiple
        # independent variants of the same template, each editable normally.
        pid=params.get('property_id',template['property_id'])
        if not isinstance(pid,str) or not (pid==template['property_id'] or pid.startswith('ws:user:')):
            raise KMInvalid('INVALID_VALUE','Invalid configured property ID')
        if action!='preview' and (self._definition(pid) or self.property_definition(pid)):
            raise KMInvalid('INVALID_VALUE','Workspace property ID already installed')
        owner=template['owner_type']
        schema={**template['property_schema'],'property_id':pid,'owner_type':owner,
                'source_preset_id':preset,'origin':'custom',
                'label':params.get('label',template['property_schema']['display_label']),
                'version':1}
        _safe_text(schema['label'],160)
        enabled=params.get('enabled',True)
        if not isinstance(enabled,bool):raise KMInvalid('INVALID_TYPE','Enabled must be Boolean')
        expression=None;rule_id=None;depends=set()
        if template['mode']=='computed':
            overrides=params.get('dependency_properties',{})
            if not isinstance(overrides,dict) or set(overrides)-{'direct','noise'}:
                raise KMInvalid('INVALID_VALUE','Invalid dependency binding')
            if preset not in ('WS-EMAIL-ATTENTION-001','WS-EMAIL-PRIORITY-001') and overrides:
                raise KMInvalid('INVALID_VALUE','This template has no Workspace dependencies')
            try:expression=template_expression(preset,settings,overrides)
            except (ValueError,TypeError,KeyError) as exc:
                raise KMInvalid('INVALID_VALUE','Invalid template expression settings') from exc
            # Validate dependency IDs/types through the same custom rule path.
            rule_id=params.get('rule_id') or _id('ws:user:rule-')
            if not isinstance(rule_id,str) or not rule_id.startswith('ws:user:') or (action!='preview' and self._definition(rule_id)) or rule_id==pid:
                raise KMInvalid('INVALID_VALUE','Rule identifier already used or reserved')
            fields=self._expression_fields(owner)
            fields[pid]=schema['value_type']
            depends=validate_expression(expression,owner,fields)['dependencies']
            self._check_expression_references(expression,owner)
            if pid in depends:raise KMInvalid('DEPENDENCY_CYCLE','Template rule cannot depend on itself')
            for dep in depends:
                d=self.property_definition(dep)
                if d is None or d['owner_type']!=owner or d['mode']!='computed':
                    raise KMInvalid('CONFIGURATION_REQUIRED','Install a compatible prerequisite Workspace property')
            if action!='preview':self._check_rule_cycles(rule_id,pid,depends)
            rule={'rule_id':rule_id,'target_record_type':owner,'output_property_id':pid,
                  'expression':expression,'dependencies':sorted(depends), 'rule_version':1,
                  'description':f'Editable initial recipe: {preset}'}
        if action=='preview':
            return {'preset_id':preset,'property':schema,
                    'rule':rule if template['mode']=='computed' else None,
                    'settings':settings,'no_mutation':True}
        if action!='apply':raise KMInvalid('INVALID_VALUE','Use preset.apply, then ordinary property/rule operations')
        def write():
            self._put_def(pid,'property',owner,'custom',schema,'enabled' if enabled else 'disabled')
            if rule_id:self._put_def(rule_id,'rule',owner,'custom',rule,'enabled' if enabled else 'disabled')
            after={'property_id':pid,'rule_id':rule_id,'source_preset_id':preset,
                   'property':schema,'rule':rule if rule_id else None,'state':'enabled' if enabled else 'disabled'}
            return None,after,{'properties':[pid],'rule_id':rule_id,
                 'property_id':pid,'source_preset_id':preset,'dependencies':sorted(depends)}
        return self._commit('preset.apply','preset',preset,expected,write)

    def _registry(self,op,params):
        owner=params.get('record_type') or params.get('owner_type')
        if owner is not None and not self._valid_owner(owner,allow_retired=True):
            raise KMInvalid('INVALID_TYPE','Unknown requested owner type')
        if op=='registry.record_types':
            return {'record_types':list(TYPES),'workspace_overlay_type':'WorkspaceOrganization',
                    'workspace_record_types':self._workspace_dispatch('workspace.types',{},None,None)['record_types'] if self._has_workspace_schema() else []}
        if op=='registry.properties':
            builtin=[]  # Optional templates are listed under registry.presets, not installed here.
            custom=[]
            for r in self.db.execute("SELECT * FROM ws_definitions WHERE definition_kind='property' ORDER BY definition_id"):
                if owner and r['owner_type']!=owner:continue
                custom.append({**_plain(r['specification_json']),'state':r['state'],'revision':r['revision']})
            # Core metadata remains sourced from shipped vocabulary, never copied into Workspace authority.
            from rdflib.namespace import RDF,OWL,RDFS
            graph=self.engine.ontology.definition
            core=[]
            for sub in graph.subjects(RDF.type,OWL.DatatypeProperty):
                domain=graph.value(sub,RDFS.domain)
                core_owner=str(domain).rsplit(':',1)[-1] if domain is not None else None
                if owner and core_owner!=owner:continue
                valtype=graph.value(sub,RDFS.range)
                def _count(name):
                    from rdflib import URIRef
                    item=graph.value(sub,URIRef('urn:o3:ontology:'+name))
                    return int(item) if item is not None else None
                core.append({'property_id':'core:'+str(sub).rsplit(':',1)[-1],
                    'owner_type':core_owner,'mode':'source','authority':'core',
                    'datatype':str(valtype) if valtype else None,
                    'cardinality':{'min':_count('minCount'),'max':_count('maxCount')},
                    'display_label':str(graph.value(sub,RDFS.label) or str(sub).rsplit(':',1)[-1]),
                    'description':str(graph.value(sub,RDFS.comment) or ''),
                    'editable':False})
            return {'core_property_names':sorted(x['property_id'][5:] for x in core),
                    'core_properties':sorted(core,key=lambda x:x['property_id']),
                    'workspace_properties':builtin+custom,
                    'configuration_types':['boolean','text','enum','date','record_ref','record_ref_set']}
        if op=='registry.rules':
            core=self.engine.rules()
            custom=[{**_plain(r['specification_json']), 'state':r['state'], 'revision':r['revision']}
                for r in self.db.execute("SELECT * FROM ws_definitions WHERE definition_kind='rule' ORDER BY definition_id")
                if owner is None or r['owner_type']==owner]
            return {'core_rules':[r for r in core if owner is None or r.get('record_type',r.get('owner'))==owner],
                    'workspace_rules':custom,'supported_operators':list(self._operator_names())}
        if op=='registry.presets':
            # A template is a recipe, not a second live property/rule authority.
            installed={}
            for r in self.db.execute("SELECT definition_id,specification_json FROM ws_definitions WHERE definition_kind='property' AND state!='retired'"):
                info=_plain(r['specification_json'])
                if info.get('source_preset_id'):
                    installed.setdefault(info['source_preset_id'],[]).append(r['definition_id'])
            return {'presets':[{**dict(v),
                                 'dependency_inputs':[{'name':PRESETS[d]['property_id'][3:],
                                     'default_property_id':PRESETS[d]['property_id'],
                                     'owner_type':v['owner_type'],'mode':'computed','value_type':'boolean'}
                                     for d in v.get('dependencies',[])],
                                 'setting_schema':self._template_setting_schema(v),
                                 'installed_property_ids':installed.get(k,[]),
                                 'installed':bool(installed.get(k))}
                       for k,v in TEMPLATES.items() if owner is None or v['owner_type']==owner]}
        if op=='registry.views':
            ordered=[]
            for k,v in VIEWS.items():
                if owner and v['owner_type']!=owner:continue
                row=self._definition(k)
                override=_plain(row['specification_json']) if row else {}
                ordered.append({**v,**self._view_form_metadata(k),'state':row['state'] if row else ('enabled' if v.get('enabled_default') else 'disabled'),
                                'order':override.get('order',v.get('order',0)),
                                'visible':override.get('visible',v.get('enabled_default',False)),
                                'display_label':override.get('label',v.get('name'))})
            for r in self.db.execute("SELECT * FROM ws_definitions WHERE definition_kind='view' AND origin='custom' ORDER BY definition_id"):
                if owner and r['owner_type']!=owner:continue
                from .starters import metadata
                ordered.append({**_plain(r['specification_json']),'state':r['state'],'revision':r['revision'],**metadata(r['definition_id'])})
            return {'views':sorted(ordered,key=lambda v:(v.get('owner_type',v.get('record_type','')),
                                                  v.get('order',0),v.get('view_id',''))),
                    'fields_by_type':{kind:self._view_field_catalog(kind) for kind in
                        ((*TYPES,*(r['type_id'] for r in self.db.execute('SELECT type_id FROM ws_record_types'))) if self._has_workspace_schema() else TYPES)
                        if owner is None or owner==kind}}
        raise KMInvalid('UNKNOWN_OPERATION','Unknown registry operation')

    @staticmethod
    def _view_form_metadata(view_id):
        """Display/parameter descriptors for existing query branches, not new rules."""
        from .catalog import VIEW_PRESET
        presentation='table';parameters={}
        suffix=view_id.rsplit(':',1)[-1]
        if suffix in ('stakeholder_map','concept_map','command_hierarchy','enterprise_map'):presentation='map'
        elif suffix in ('conversations','by_org','by_author','by_loe','pocs'):presentation='grouped'
        elif suffix=='dashboard':presentation='dashboard'
        def prop(owner,mode,kind,key):
            parameters[key]={'value_type':'property_selector','owner_type':owner,'mode':mode,'property_type':kind,'optional':True}
        if view_id in VIEW_PRESET:prop('Email','computed','boolean','property_id')
        if view_id=='view:person:key_contacts':prop('Person','manual','boolean','property_id')
        if view_id in ('view:person:by_org','view:person:stakeholder_map'):
            prop('Person','manual','text','workspace_bindings.role')
            prop('Person','manual','boolean','workspace_bindings.key_contact')
        if view_id=='view:loe:dashboard':
            parameters['loe_id']={'value_type':'record_ref','ref_type':'LineOfEffort','serialize_id':True}
            parameters['start_utc']={'value_type':'timestamp','optional':True}
            parameters['end_utc']={'value_type':'timestamp','optional':True}
            for key,kind in (('priority','enum'),('effort_level','enum'),('work_status','enum'),('target_date','date'),('responsible_person_id','record_ref')):
                prop('LineOfEffort','manual',kind,'workspace_bindings.'+key)
            prop('Person','manual','text','workspace_bindings.role')
        return {'presentation':presentation,'parameter_schema':parameters}

    def _builder_schema(self,params):
        """Descriptive configuration contract for a future native form builder.

        The UI discovers fields/operators from the same registries used by rule
        validation and evaluation. No duplicate UI-side rule interpreter.
        """
        from .catalog import RULE_LIMITS
        from .evaluation import FIELD_KINDS,allowed_operators,fields_for
        owner=params.get('record_type')
        if not self._valid_owner(owner,allow_retired=True):
            raise KMInvalid('INVALID_TYPE','Choose a Core record type or Workspace Organization overlay')
        fields=self._expression_fields(owner)
        source_fields={**fields_for(owner),'record_id':'text'} if owner!='WorkspaceOrganization' else {}
        available=[]
        for name,kind in sorted({**source_fields,**fields}.items()):
            is_ws=name.startswith('ws:')
            defined=self.property_definition(name) if is_ws else ({'allowed_values':['replied','no_reply_found','unknown'] if name=='reply_status' else ['self','external','unknown']} if name in ('reply_status','sender_identity') else None)
            available.append({'field_id':name,'value_type':kind,
              'label':(defined.get('label',defined.get('display_label',name.replace('_',' ').capitalize())) if defined else
                       re.sub(r'(?<=[a-z])(?=[A-Z])',' ',name).replace('_',' ').replace('.',' / ').capitalize()),
              'authority':'workspace' if is_ws else 'core',
              'read_only':not is_ws,'state':defined.get('status','available') if defined else 'available',
              **({'origin':'evidence_computation','allowed_values':defined['allowed_values']} if name in ('reply_status','sender_identity') else {}),
              'operators':list(allowed_operators(owner,name,fields)),
              'operator_specs':self._builder_operator_specs(owner,name,kind,fields,defined)})
        return {'record_type':owner,'property_types':[
             {'value_type':v,'editable':True,'computed_output':v=='boolean' and owner!='WorkspaceOrganization'}
             for v in ('boolean','text','enum','date','record_ref','record_ref_set')],
             'property_constraints':{'text_limit':240,'enum_option_limit':32,
                   'reference_set_limit':100,'property_label_limit':160},
             'condition_groups':['all','any','not'] if owner!='WorkspaceOrganization' else [],
             'rule_limits':dict(RULE_LIMITS) if owner!='WorkspaceOrganization' else None,
             'fields':available,
             'templates':[v['preset_id'] for v in TEMPLATES.values() if v['owner_type']==owner],
             'preview_operations':['property.validate','property.preview','rule.validate','rule.preview','preset.preview','view.validate','view.preview'],
             'property_configuration':{'save':'property.configure','preview':'property.preview','atomic':True},
             'reference_targets':list(TARGETS)+([r['type_id'] for r in self.db.execute('SELECT type_id FROM ws_record_types WHERE active=1')] if self._has_workspace_schema() else []),
             'view_options':{'filter_operators':['equals','not_equals','is_present','one_of'],
                 'max_filters':12,'sort_fields':['updated_at','record_id','name','title','start_utc'],
                 'group_fields':['none','record_type','scope'],'sort_directions':['asc','desc'],
                 'expression_supported':owner!='WorkspaceOrganization','max_sorts':3,'preview_page_size':50,
                 'ordering_fields':[x for x in self._view_field_catalog(owner) if x['sortable']],
                 'grouping_fields':[x for x in self._view_field_catalog(owner) if x['groupable']],
                 'column_fields':self._view_field_catalog(owner)},
             'unsupported':['arbitrary Python','SQL','SPARQL','external APIs','inferred relation paths']}

    @staticmethod
    def _template_setting_schema(template):
        result = {}
        targets = {'selected_person_ids':'Person','selected_organization_ids':'Organization',
                   'selected_loe_ids':'LineOfEffort'}
        for key in template.get('default_settings', {}):
            if key == 'sender_patterns':
                result[key] = {'value_type':'list','item_type':'email_pattern','min_count':1,'max_count':64}
            elif key == 'age_calendar_days':
                result[key] = {'value_type':'integer','minimum':0,'maximum':365}
            elif key in targets:
                result[key] = {'value_type':'record_id_set','max_count':100,
                    'reference_targets':[targets[key], 'WorkspaceOrganization'] if targets[key]=='Organization' else [targets[key]]}
        return result

    @staticmethod
    def _builder_operator_specs(owner, field, value_type, fields, definition):
        """Descriptive operand controls, exported by the same KM rule authority.

        This is metadata only. Preview/save still use the existing validator.
        No GUI consumer needs to embed rule semantics to construct typed forms.
        """
        from .evaluation import allowed_operators
        from .catalog import RULE_LIMITS
        definition = definition or {}
        result = {}
        for operator in allowed_operators(owner, field, fields):
            argument = {'value_type': 'none'}
            if operator in ('equals', 'not_equals'):
                argument = {'value_type': value_type}
            elif operator == 'one_of':
                argument = {'value_type': 'list', 'item_type': value_type, 'min_count': 1, 'max_count': 50}
            elif operator == 'reference_contains':
                argument = {'value_type':'record_ref','ref_type':definition.get('ref_type'),'reference_targets':[definition['ref_type']] if definition.get('ref_type') else []}
            elif operator == 'reply_observed':
                argument = {'value_type': 'boolean'}
            elif operator in ('local_date_age_gt','local_date_age_lte','list_count_equals',
                              'list_count_at_least','list_count_at_most'):
                argument = {'value_type': 'integer', 'minimum': 0, 'maximum': 36500}
            elif operator in ('reference_in_selected','verified_path_to_selected'):
                argument = {'value_type': 'record_id_set', 'max_count': RULE_LIMITS['max_selected_ids'],
                    'reference_targets': [definition['ref_type']] if definition.get('ref_type') else
                        ['Person','Organization','LineOfEffort','WorkspaceOrganization']}
            elif operator == 'email_matches_any':
                argument = {'value_type': 'list', 'item_type': 'email_pattern', 'min_count': 1, 'max_count': 64}
            elif operator in ('email_matches','email_list_contains'):
                argument = {'value_type': 'email_pattern' if operator == 'email_matches' else 'email'}
            elif operator in ('contains','starts_with','ends_with','list_contains'):
                argument = {'value_type': 'text', 'max_chars': 240}
            if definition.get('allowed_values'):
                argument['allowed_values'] = definition['allowed_values']
            result[operator] = argument
        return result

    def _check_expression_references(self,expr,owner,*,allow_retired=False):
        """Validate named Core/Workspace reference IDs before storing a rule."""
        if 'not' in expr:
            return self._check_expression_references(expr['not'],owner,allow_retired=allow_retired)
        for group in ('all','any'):
            if group in expr:
                for child in expr[group]:self._check_expression_references(child,owner,allow_retired=allow_retired)
                return
        operator=expr.get('operator')
        if operator=='reference_contains':
            definition=self.property_definition(expr['field'])
            if not definition or definition['value_type']!='record_ref_set':raise KMInvalid('INVALID_TYPE','A typed reference-set property is required')
            self._validate_ref(expr['value'],definition['ref_type'],allow_retained=allow_retired)
            return
        if operator not in ('verified_path_to_selected','reference_in_selected'):return
        if operator=='reference_in_selected':
            property_def=self.property_definition(expr['field'])
            if not property_def or property_def.get('value_type')!='record_ref':
                raise KMInvalid('INVALID_TYPE','Selected references require a configured typed record reference')
            allowed=(property_def['ref_type'],)
        else:allowed=('Person','Organization','LineOfEffort','WorkspaceOrganization')
        if is_type_id(owner) and operator=='verified_path_to_selected':
            raise KMInvalid('INVALID_TYPE','Custom records have no inferred Core relationship paths')
        for rid in expr['value']:
            if any(self._reference_is_active(kind,rid,allow_retired=allow_retired) for kind in allowed):continue
            raise KMInvalid('UNKNOWN_RECORD','Selected reference has no active record of the required type')

    def _reference_is_active(self,kind,rid,*,allow_retired=False):
        try:self._assert_target(kind,rid,active=not allow_retired);return True
        except KMInvalid:return False

    @staticmethod
    def _operator_names():
        from .catalog import OPERATORS
        return OPERATORS

    def _explain(self,op,params,as_of,*,trace=None,row=None,evaluator=None,results=None):
        kind=params.get('record_type');rid=params.get('record_id')
        if kind=='WorkspaceOrganization':
            self._assert_target(kind,rid,active=False)
            if op=='history.get':
                items=[dict(r) for r in self.db.execute("SELECT * FROM ws_audit WHERE subject_id=? ORDER BY event_id",(rid,))]
                return {'target_id':rid,'authority':'workspace_manual','workspace_audit':items}
            if op=='evidence.get':
                members=self._get_ws_org_members(rid)
                return {'target_id':rid,'authority':'workspace_overlay','core_organizations':[self.engine.store.trace(k) for k in members]}
            if op=='rules.explain':
                return {'target_id':rid,'results':{},'authority':'workspace_manual'}
        else:
            if row is None:row=self._assert_target(kind,rid,active=False)
        if trace is None:trace=self.engine.trace_record(rid)
        if op=='history.get':
            items=[dict(r) for r in self.db.execute("SELECT * FROM ws_audit WHERE subject_id=? ORDER BY event_id",(rid,))]
            return {'record_id':rid,'core_trace':trace,'workspace_audit':items}
        if op=='evidence.get':
            return {'record_id':rid,'authority':'core','source_trace':trace,'active':bool(row['active'])}
        if op=='rules.explain':
            evalu=evaluator if evaluator is not None else Evaluator(self,as_of)
            if results is None:
                results={p:evalu.evaluate(row,p) for p in evalu.properties_by_kind.get(kind,[])}
            p=params.get('property_id')
            if p and p not in results:raise KMInvalid('UNKNOWN_PROPERTY','No property for selected target')
            return {'record_id':rid,'core_rules':trace.get('evaluations',[]),
                    'workspace_results':{p:results[p]} if p else results,
                    'revision':self.revision()}
        raise KMInvalid('UNKNOWN_OPERATION','Unknown explanation operation')

    def _get_record(self,params,as_of,*,include_detail=False):
        """One optional read bundle; standalone record/explanation contracts remain intact.

        The evaluator and trace are local to this call, never cached on the service.
        Neighbor references omit costly, unused correspondence and rule projections.
        """
        kind=params.get('record_type');rid=params.get('record_id')
        row=self._assert_target(kind,rid,active=False)
        evaluator=None
        if kind=='WorkspaceOrganization':
            record=next(x for x in self._organization_effective_rows(True) if x['record_id']==rid)
        else:
            evaluator=Evaluator(self,as_of);evaluator.include_reply_evidence=kind=='Email'
            record=self._decorate(row,evaluator if row['active'] else None)
            if kind=='Email':
                if _plain(row['added_json']).get('direction')=='Incoming':
                    record['reply_observed']=evaluator.core_reply_observed(row)
                record['attachment_trace']=self._attachment_trace(row)
            if kind in ('Person','Organization'):
                record['last_correspondence']=self._last_correspondence(kind,rid)
        result={'record':record}
        if not include_detail:return result
        trace=None if kind=='WorkspaceOrganization' else self.engine.trace_record(rid)
        shared={'trace':trace,'row':row,'evaluator':evaluator}
        evidence=self._explain('evidence.get',params,as_of,**shared)
        history=self._explain('history.get',params,as_of,**shared)
        rules=self._explain('rules.explain',params,as_of,
            results=record.get('workspace_properties'),**shared)
        relations=self._relationships_for(kind,rid)
        targets={}
        for edge in relations['edges']:
            side='target' if edge['source_id']==rid else 'source'
            targets[edge[side+'_id']]=edge[side+'_type']
        # Only display fields needed for labels. No full records.get per neighbor.
        neighbors=self._reference_records(targets)
        result['detail']={'evidence':evidence,'history':history,'rules':rules,
                          'relations':relations,'neighbors':neighbors}
        return result

    def _core_queries(self,op,params,as_of):
        kind=params.get('record_type');rid=params.get('record_id')
        if op=='records.query':
            if params.get('sorts') and kind is None:
                sorts=params['sorts']
                if not isinstance(sorts,list) or not 1<=len(sorts)<=3 or any(not isinstance(item,dict) or set(item)!={'field','direction'} or item['field'] not in ('record_id','record_type','scope','updated_at') or item['direction'] not in ('asc','desc') for item in sorts):
                    raise KMInvalid('INVALID_VALUE','Mixed collections can sort by type, identity, scope or update time')
                return self._configured_view_page(as_of,None,{'label':'All records','record_type':None,'sorts':sorts,'filters':params.get('filters',[]),'include_inactive':params.get('include_inactive',False)},params,[])
            if params.get('sorts') and kind:
                spec={'label':'Collection','record_type':kind,'sorts':params['sorts'],
                      'filters':params.get('filters',[]),'include_inactive':params.get('include_inactive',False)}
                return self._configured_view(as_of,None,spec,params)
            return self._workspace_listing(params,as_of) if is_type_id(kind) else self._listed(kind,params,as_of)
        if op=='records.get' and is_type_id(kind):return self._workspace_dispatch('workspace.records.get',params,as_of,None)
        if op=='records.get':
            include_detail=params.get('include_detail',False)
            if type(include_detail) is not bool:
                raise KMInvalid('INVALID_TYPE','include_detail must be Boolean')
            return self._get_record(params,as_of,include_detail=include_detail)
        if op=='relations.for_record':return self._relationships_for(kind,rid)
        if op=='relations.explain':
            # Claims are only ever from recorded directed Core edges, never a guessed join.
            key=(params.get('source_id'),params.get('predicate'),params.get('target_id'))
            if None in key:raise KMInvalid('INVALID_VALUE','Directed edge triple required')
            definition=self.property_definition(key[1]) if isinstance(key[1],str) and key[1].startswith('ws:') else None
            if definition and definition['value_type'] in ('record_ref','record_ref_set'):
                edge=next((edge for edge in self._manual_relationships(definition['owner_type'],key[0]) if (edge['source_id'],edge['predicate'],edge['target_id'])==key),None)
                if edge is None:raise KMInvalid('UNKNOWN_RECORD','No current manual association')
                return edge
            rows=self.db.execute('''SELECT owner,evidence_json FROM relationships
                                   WHERE source_id=? AND predicate=? AND target_id=? ORDER BY owner''',key).fetchall()
            if not rows:raise KMInvalid('UNKNOWN_RECORD','No current directed Core assertion')
            return {'source_id':key[0],'predicate':key[1],'target_id':key[2],
                    'claims':[{'owner':x['owner'],'evidence':_plain(x['evidence_json'])} for x in rows],
                    'authority':'core'}
        if op in ('history.get','evidence.get','rules.explain'):return self._explain(op,params,as_of)
        if op=='organization.effective':
            if rid:
                r=next((x for x in self._organization_effective_rows(True) if x['record_id']==rid),None)
                if r is None:raise KMInvalid('UNKNOWN_RECORD','Effective Organization not found')
                target_kind='WorkspaceOrganization' if r['effective_kind']=='workspace_overlay' else 'Organization'
                return {'organization':r,
                        'verified_connections':self._relationships_for(target_kind,rid),
                        'rollups':self._effective_rollups(target_kind,rid,params),
                        'last_correspondence':self._last_correspondence(target_kind,rid)}
            rows=self._organization_effective_rows(params.get('include_inactive',False))
            if params.get('overlays_only'):
                rows=[r for r in rows if r['effective_kind']=='workspace_overlay']
            if params.get('search'):
                text=params['search']
                if not isinstance(text,str) or len(text)>160:
                    raise KMInvalid('INVALID_VALUE','Invalid Organization search text')
                text=text.strip().casefold()
                rows=[r for r in rows if text in r['display_name'].casefold() or text in r['record_id'].casefold()]
            return self._page(rows,params,as_of,op)
        if op=='organization.raw_audit':return self._page(self._raw_organizations(params.get('include_inactive',True)),params,as_of,op)
        if op=='calendar.sources':
            # Workspace calendar labels are presentation only, not source identities.
            labels=self.settings().get('calendar_display_names',{})
            sources=_plain(self._meta('calendar_sources_v1','{}'))
            return {'sources':[{**source,'display_name':labels.get(scope,source['display_name'])}
                    for scope,source in sorted(sources.items())],
                    'coverage':self.coverage('Event'), 'remote_freshness':'unknown'}
        if op in ('view.query','view.preview'):
            if op=='view.preview' and ('specification' in params or 'proposed_filters' in params):
                # The exact saved-query path, including empty filters, ordering,
                # grouping, missing-evidence states and snapshot-bound paging.
                spec=params.get('specification')
                if spec is None:
                    spec={'label':'Preview','record_type':params.get('record_type'),
                          'filters':params['proposed_filters'],
                          **{k:params[k] for k in ('sort','sort_direction','group_by','include_inactive') if k in params}}
                self._validate_view_spec(spec)
                return self._configured_view(as_of,params.get('view_id'),spec,params)
            return self._views(as_of,params.get('view_id'),params)
        raise KMInvalid('UNKNOWN_OPERATION','Unknown query operation')

    def _calendar_relink(self,params,expected):
        scope=params.get('calendar_scope');new=params.get('new_path')
        if not isinstance(scope,str) or not isinstance(new,str):raise KMInvalid('INVALID_VALUE','Calendar scope/path required')
        from pathlib import PurePosixPath
        path=PurePosixPath(new.replace('\\','/'))
        if len(path.parts)!=1 or not new.lower().endswith('.ics') or path.name!=new or new in ('.','..'):
            raise KMInvalid('INVALID_VALUE','Calendar path must be one top-level .ics filename')
        sources=_plain(self._meta('calendar_sources_v1','{}'))
        if scope not in sources:raise KMInvalid('CALENDAR_SCOPE_CONFLICT','Unknown calendar scope')
        if any(v['path'].casefold()==new.casefold() and k!=scope for k,v in sources.items()):
            raise KMInvalid('CALENDAR_SCOPE_CONFLICT','New path already registered')
        old=sources[scope]
        if old['path']==new:
            self._assert_revision(expected)
            return {'changed':False,'new_workspace_revision':expected,'affected_core_ids':[], 'affected_property_ids':[], 'affected_view_ids':[]}
        if not params.get('confirmed'):
            raise KMInvalid('CONFIGURATION_REQUIRED','Explicit calendar relink confirmation required')
        root=params.get('source_root')
        if not isinstance(root,str) or not root:
            raise KMInvalid('CONFIGURATION_REQUIRED','Real local source root required for safe calendar relink')
        base=Path(root).expanduser().resolve()
        old_file=base/'Calendars'/old['path']
        new_file=base/'Calendars'/new
        if base.is_symlink() or old_file.is_symlink() or new_file.is_symlink() or            old_file.is_file() or not new_file.is_file():
            raise KMInvalid('CALENDAR_SCOPE_CONFLICT','Original calendar must be absent and replacement must be locally readable')
        try:
            from o3_core.rules.event import parse_calendar
            _header,events=parse_calendar(new_file.read_bytes())
            if not events:raise ValueError('Calendar contains no observed events')
        except (ValueError,OSError,UnicodeDecodeError) as exc:
            raise KMInvalid('CALENDAR_SCOPE_CONFLICT','Replacement calendar cannot be validated') from exc
        updated={**old,'path':new,'revision':old.get('revision',1)+1,'state':'active'}
        def fn():
            sources[scope]=updated;self._setmeta('calendar_sources_v1',encode(sources))
            return old,updated,{'dependencies':[scope],'calendar_scope':scope}
        return self._commit('calendar.relink','calendar',scope,expected,fn,core=True)

    def _reconcile_projects(self,expected):
        """Recheck retained evidence only; never scan or unpause an input folder.

        The bridge owns its Core transaction. An audited pending receipt is
        committed first, so interruption/failure remains visible and retryable.
        The epoch advances before work and again on completion.
        """
        key='mail_product_project_reconciliation_v1'
        previous=_plain(self._meta(key,'null'))
        pending={'state':'pending','requested_at':now(),'request_id':uuid.uuid4().hex}
        def begin():
            self._setmeta(key,encode(pending))
            return previous,pending,{}
        self._commit('loe.reconcile.begin','configuration','project_connections',expected,begin,core=True)
        try:
            from .core_bridge import loes
            count=loes(self.engine)
            # Match the source-scan pipeline's active-target authority boundary.
            with self.db:
                self.db.execute('''DELETE FROM relationships WHERE
                    source_id IN (SELECT record_id FROM records WHERE active=0) OR
                    target_id IN (SELECT record_id FROM records WHERE active=0)''')
            completed={**pending,'state':'complete','completed_at':now(),'updated_source_count':count}
        except Exception:
            completed={**pending,'state':'pending','reason':'Project saved. Connections could not be refreshed. Retry checking saved project connections.'}
        def finish():
            self._setmeta(key,encode(completed))
            return pending,completed,{'project_reconciliation':completed}
        return self._commit('loe.reconcile.finish','configuration','project_connections',self.revision(),finish,core=True)

    def _dispatch(self,op,params,as_of,expected):
        if self._meta('km_schema_contract')!=CATALOG_FINGERPRINT and op not in ('view.starters.ensure','system.capabilities','system.audit'):
            raise KMInvalid('CONFIGURATION_REQUIRED','Complete the verified Workspace conversion before using this version')
        if op=='view.starters.ensure':
            from .starters import ensure
            return ensure(self,params,expected)
        if op in ('property.batch','property.undo'):
            from .property_commands import batch,undo
            return (batch if op=='property.batch' else undo)(self,params,expected)

        if op=='mail.tasks.query':
            from .mail_tasks import query
            return query(self,params,as_of)
        if op=='mail.identity':
            from .mail_tasks import identity
            if params:raise KMInvalid('INVALID_VALUE','Mail identity takes no filters')
            return identity(self)
        if op=='mail.tasks.action':
            from .mail_tasks import action
            return action(self,params,expected,as_of)
        if op=='system.capabilities':
            return {'contract':self.contract_version(),'schema_version':4 if self._has_workspace_schema() else 3,'core_record_types':list(TYPES),
                    'workspace_id':self.wsid(),'workspace_revision':self.revision(),'core_epoch':self.epoch(),
                    'operations':list(self.operations()),'catalog_fingerprint':CATALOG_FINGERPRINT,
                    'source_coverage':self.coverage(),'remote_freshness':'unknown',
                    'mail_task_limits':{'max_action_anchors':MAX_ACTION_ANCHORS,'max_reply_depth':MAX_CHAIN},
                    'project_reconciliation':_plain(self._meta('mail_product_project_reconciliation_v1','null')),
                    'record_get_options':{'include_detail':True}}
        if op.startswith('workspace.'):
            return self._workspace_dispatch(op,params,as_of,expected)
        if op=='builder.schema':return self._builder_schema(params)
        if op.startswith('registry.'):return self._registry(op,params)
        if op in ('records.query','records.get','view.query','view.preview','relations.for_record',
                  'relations.explain','evidence.get','history.get','rules.explain',
                  'organization.effective','organization.raw_audit','calendar.sources'):
            return self._core_queries(op,params,as_of)
        if op=='configuration.get':return {'settings':self.settings(),'revision':self.revision()}
        if op=='configuration.save':return self._save_settings(params,expected)
        if op=='property.get':
            return {'property':self.property_definition(params.get('property_id'))}
        if op=='property.value':
            kind=params.get('record_type');rid=params.get('record_id');p=params.get('property_id')
            self._assert_target(kind,rid,active=False)
            d=self.property_definition(p)
            if d is None or d['owner_type']!=kind:raise KMInvalid('UNKNOWN_PROPERTY','Property not available for target')
            if d.get('status',d.get('state'))=='disabled':
                return {'state':'disabled','value':None,'property_id':p,'reason':'Property disabled'}
            if d['mode']=='manual':
                value=self.manual_value(kind,rid,p)
                retained=self.retained_reference_ids(d,value)
                return {'state':'not_evaluable' if retained else 'evaluated' if value is not None else 'unset',
                        'value':value,'property_id':p,'retained_reference_ids':retained,
                        'reason':'Reference retained/inactive' if retained else None}
            if kind=='WorkspaceOrganization':return {'state':'not_evaluable','value':None,'reason':'No computed overlay preset'}
            return Evaluator(self,as_of).evaluate(self._assert_target(kind,rid,active=False),p)
        if op in ('property.configure','property.preview'):
            return self._configure_property(params,expected,as_of,preview=op.endswith('.preview'))
        if op.startswith(('property.','rule.','view.','preset.')):
            kind,action=op.split('.',1)
            if action=='validate':
                # Run the same validator on read-only proposed definitions without a write.
                if kind=='rule':
                    expr=params.get('expression') or params.get('specification',{}).get('expression')
                    owner=params.get('record_type') or params.get('owner_type')
                    valid=validate_expression(expr,owner,self._expression_fields(owner))
                    self._check_expression_references(expr,owner)
                    proposed=params.get('specification') or {}
                    output=proposed.get('output_property_id',params.get('output_property_id'))
                    if output is not None:
                        definition=self.property_definition(output)
                        if (not definition or definition['owner_type']!=owner or
                            definition['mode']!='computed' or definition['value_type']!='boolean'):
                            raise KMInvalid('INVALID_TYPE','Compatible computed output property required')
                        existing_id=params.get('definition_id') or params.get('rule_id')
                        existing=self._definition(existing_id) if existing_id else None
                        if existing and (existing['definition_kind']!='rule' or existing['origin']!='custom' or
                                         _plain(existing['specification_json']).get('output_property_id')!=output):
                            raise KMInvalid('INVALID_TYPE','Rule validation cannot change output identity')
                        if output in valid['dependencies']:
                            raise KMInvalid('DEPENDENCY_CYCLE','Rule cannot depend on its own output')
                        for row in self.db.execute("SELECT definition_id,specification_json FROM ws_definitions WHERE definition_kind='rule' AND state='enabled'"):
                            if row['definition_id']!=existing_id and _plain(row['specification_json']).get('output_property_id')==output:
                                raise KMInvalid('DUPLICATE_PRODUCER','Only one enabled producer per computed output')
                        self._check_rule_cycles(existing_id or '__rule_preview__',output,valid['dependencies'])
                    return {'valid':True,'dependencies':sorted(valid['dependencies']),
                            'leaves':valid['leaves'],'relation_joins':valid['joins']}
                if kind=='view':
                    dependencies=self._validate_view_spec(params.get('specification',{}))
                    return {'valid':True,'dependencies':sorted(dependencies)}
                if kind=='property':
                    spec=params.get('specification',{})
                    owner=params.get('record_type') or params.get('owner_type')
                    self._property_spec(spec,owner,params.get('definition_id','ws:user:preview'))
                    return {'valid':True}
                raise KMInvalid('INVALID_VALUE','Preset validation uses configuration preview')
            if action=='preview':
                if kind=='rule':
                    return self._preview_rule(params,as_of)
                if kind=='preset':return self._preset_mutation('preview',params,None)
                if kind=='view':return self._core_queries('view.preview',params,as_of)
                raise KMInvalid('INVALID_VALUE','Preview not supported')
            if kind=='preset':return self._preset_mutation(action,params,expected)
            return self._def_mutation(kind,action,params,expected)
        if op.startswith('organization.'):
            from .authoring import organization_mutation
            return organization_mutation(self,op.split('.',1)[1],params,expected)
        if op.startswith('loe.'):
            if op=='loe.reconcile':
                if params:raise KMInvalid('INVALID_VALUE','Project connection retry takes no parameters')
                return self._reconcile_projects(expected)
            from .authoring import loe_mutation
            result=loe_mutation(self,op.split('.',1)[1],params,expected)
            if result.get('changed') and op in ('loe.create','loe.edit','loe.adopt','loe.retire','loe.archive'):
                reconciled=self._reconcile_projects(self.revision())
                result.update({'new_workspace_revision':self.revision(),
                               'project_reconciliation':reconciled['project_reconciliation']})
            return result
        if op=='calendar.relink':return self._calendar_relink(params,expected)
        if op=='system.audit':
            return {'sqlite_quick_check':self.db.execute('PRAGMA quick_check').fetchone()[0],
                    'core_provenance':self.engine.audit_provenance(),
                    'rdf':self.engine.audit_rdf(), 'workspace':self.audit_workspace()}
        if op=='system.backup':
            if not params.get('source_root'):
                raise KMInvalid('BACKUP_REQUIRED','Validated O3Data source root is required for backup')
            return verified_backup(Path(self.engine.store.root),Path(params['destination']),
                independent_location_approved=params.get('independent_location_approved',False),
                source_root=Path(params['source_root']))
        if op=='system.restore':
            raise KMInvalid('STATE_LOCKED','Restore must run offline with the writer stopped; use restore_backup explicitly')
        raise KMInvalid('UNKNOWN_OPERATION','Unknown contract operation')

    @staticmethod
    def operations():
        return ['system.capabilities','system.audit','system.backup','system.restore',
            'mail.tasks.query','mail.identity','mail.tasks.action','loe.reconcile',
            'builder.schema','view.starters.ensure','property.batch','property.undo',
            'workspace.types','workspace.type.get','workspace.type.create','workspace.type.edit','workspace.type.retire','workspace.type.restore',
            'workspace.record.create','workspace.record.edit','workspace.record.archive','workspace.record.restore',
            'workspace.records.query','workspace.records.get','workspace.view.query','workspace.dashboard',
            'registry.record_types','registry.properties','registry.rules','registry.presets','registry.views',
            'records.query','records.get','view.query','view.preview','relations.for_record','relations.explain',
            'evidence.get','history.get','rules.explain','organization.effective','organization.raw_audit',
            'calendar.sources','calendar.relink','configuration.get','configuration.save','property.get','property.value',
            *[f'{kind}.{action}' for kind,actions in {
                'property':['create','edit','disable','enable','retire','set','clear','validate','configure','preview'],
                'rule':['create','edit','disable','enable','retire','preview','validate'],
                'view':['create','edit','disable','enable','retire','preview','validate'],
                'preset':['apply','preview'],
                'organization':['create','absorb','add_member','remove_member','move_member','rename','set_field','archive','restore','undo'],
                'loe':['create','edit','archive','review','retain','adopt','retire']}.items() for action in actions]]

    def _property_spec(self,spec,owner,identifier,*,allow_retired=False):
        """One strict property schema for create and read-only form validation."""
        if not self._valid_owner(owner,allow_retired=allow_retired) or not isinstance(spec,dict):
            raise KMInvalid('INVALID_TYPE','A supported record-type owner is required')
        valid_types=('boolean','text','enum','date','record_ref','record_ref_set')
        if spec.get('mode') not in ('manual','computed') or spec.get('value_type') not in valid_types:
            raise KMInvalid('INVALID_TYPE','Invalid property mode or value type')
        if spec['mode']=='computed' and spec['value_type']!='boolean':
            raise KMInvalid('INVALID_TYPE','Computed rules initially produce Boolean classifications')
        if owner=='WorkspaceOrganization' and spec['mode']=='computed':
            raise KMInvalid('INVALID_TYPE','Organization overlays support manual configuration; Core Organization supports computed rules')
        allowed={'label','description','mode','value_type','max_chars','allowed_values',
                 'ref_type','max_count','nullable'}
        if set(spec)-allowed:
            raise KMInvalid('INVALID_VALUE','Unknown/immutable property specification keys')
        label=_safe_text(spec.get('label'),160)
        description=spec.get('description','')
        if not isinstance(description,str) or len(description)>2000:
            raise KMInvalid('INVALID_VALUE','Description must be bounded text')
        result={**spec,'label':label,'description':description,
                'property_id':identifier,'owner_type':owner,'origin':'custom','version':1}
        if 'nullable' in spec and not isinstance(spec['nullable'],bool):
            raise KMInvalid('INVALID_TYPE','Nullable must be Boolean')
        if spec['value_type']=='text':
            size=spec.get('max_chars',160)
            if type(size) is not int or not 1<=size<=240:
                raise KMInvalid('INVALID_VALUE','Invalid maximum text length')
            result['max_chars']=size
        elif 'max_chars' in spec:
            raise KMInvalid('INVALID_TYPE','Text limit requires text value type')
        if spec['value_type']=='enum':
            values=spec.get('allowed_values')
            if (not isinstance(values,list) or not 1<=len(values)<=32 or
                any(not isinstance(x,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}',x) for x in values) or
                len(set(values))!=len(values)):
                raise KMInvalid('INVALID_VALUE','Invalid enum options')
        elif 'allowed_values' in spec:
            raise KMInvalid('INVALID_TYPE','Enum choices require enum value type')
        if spec['value_type'] in ('record_ref','record_ref_set'):
            if not self._valid_owner(spec.get('ref_type'),allow_retired=allow_retired):
                raise KMInvalid('INVALID_TYPE','Typed reference target is required')
            if spec['value_type']=='record_ref_set':
                count=spec.get('max_count',100)
                if type(count) is not int or not 1<=count<=100:
                    raise KMInvalid('INVALID_VALUE','Invalid reference-set bound')
                result['max_count']=count
            elif 'max_count' in spec:
                raise KMInvalid('INVALID_TYPE','Set bound requires a reference set')
        elif 'ref_type' in spec or 'max_count' in spec:
            raise KMInvalid('INVALID_TYPE','Reference configuration requires reference type')
        return result

    def _safe_prop_validate(self,spec):
        # Kept as a small compatibility wrapper for existing technician tooling.
        return self._property_spec(spec,spec.get('owner_type'),spec.get('property_id','ws:user:preview'))

    def audit_workspace(self):
        try:
            self._validate_ws_integrity()
            return {'ok':True,'issues':[],'checked':{'definitions':self.db.execute('SELECT COUNT(*) FROM ws_definitions').fetchone()[0],
                'manual_values':self.db.execute('SELECT COUNT(*) FROM ws_values').fetchone()[0],
                'organizations':self.db.execute('SELECT COUNT(*) FROM ws_organizations').fetchone()[0],
                'audit_events':self.db.execute('SELECT COUNT(*) FROM ws_audit').fetchone()[0]}}
        except (KMInvalid,ValueError,KeyError,RuntimeError) as exc:
            return {'ok':False,'issues':[{'code':getattr(exc,'code','INTEGRITY_FAILED'),'message':str(exc)}]}

    def _report_startup_error(self, exc):
        callback=getattr(self,'_startup_error',None)
        if callback is not None:
            try:callback(exc)
            except Exception:pass

    def call(self,request):
        """Keep multi-query read results and their envelope in one SQLite snapshot.

        No additional connection, writer, cache or persisted state is created. A
        caller-owned transaction is left open; only our own read transaction ends.
        """
        operation=request.get('operation') if isinstance(request,dict) else None
        snapshot_read=operation in ('workspace.records.query','workspace.records.get','workspace.view.query','workspace.dashboard','workspace.types',
                                    'mail.tasks.query','mail.identity',
                                    'records.query','records.get','view.query','view.preview',
                                    'organization.effective','rule.preview','property.preview','preset.preview')
        owned=False
        try:
            if snapshot_read and not self.db.in_transaction:
                self.db.execute('BEGIN')
                owned=True
            return self._call(request)
        except sqlite3.Error as exc:
            self._report_startup_error(exc)
            return {'contract':self.contract_version(),'ok':False,'error':{'code':'KM_FAILURE',
                    'message':'KM read failed; consult protected diagnostics'}}
        finally:
            if owned and self.db.in_transaction:self.db.rollback()

    def _call(self,request):
        """Never leak raw runtime/database exceptions through the KM-to-GUI contract."""
        base={'contract':self.contract_version(),'workspace_id':self.wsid(),
              'workspace_revision':self.revision(),'core_epoch':self.epoch(),
              'coverage':{'local_status':self.coverage(),'remote_freshness':'unknown'},
              'warnings':[]}
        if not isinstance(request,dict):
            return {**base,'ok':False,'error':{'code':'INVALID_VALUE','message':'Expected a request object'}}
        if request.get('contract') not in (VERSION,'o3-km/1.1') or (
            isinstance(request.get('operation'),str) and request['operation'].startswith('workspace.') and request.get('contract')!=self.contract_version()):
            return {**base,'ok':False,'error':{'code':'CONTRACT_INCOMPATIBLE','message':'Expected '+VERSION}}
        try:
            op=request.get('operation')
            if not isinstance(op,str) or not op:
                raise KMInvalid('INVALID_VALUE','Missing operation')
            params=request.get('params')
            if params is None:params={}
            if not isinstance(params,dict):
                raise KMInvalid('INVALID_VALUE','Parameters must be an object')
            requested_as_of=request.get('as_of')
            if requested_as_of is None and isinstance(params.get('cursor'),str) and op in (
                    'records.query','workspace.records.query','view.query','workspace.view.query','organization.effective','organization.raw_audit','mail.tasks.query'):
                from .queries import _dec
                try:requested_as_of=_dec(params['cursor'])['snapshot']['as_of']
                except (KeyError,TypeError):requested_as_of=None
            as_of=_dt(requested_as_of if requested_as_of is not None else datetime.now(timezone.utc))
            base['as_of']=_as(as_of)
            if request.get('workspace_id') not in (None,self.wsid()):
                raise KMInvalid('INVALID_VALUE','Workspace identity mismatch')
            result=self._dispatch(op,params,as_of,request.get('expected_workspace_revision'))
            return {**base,'ok':True,'result':result,
                    'workspace_revision':self.revision(),'core_epoch':self.epoch()}
        except (KMInvalid,ValueError,KeyError,TypeError,IndexError,RuntimeError,sqlite3.Error) as exc:
            self._report_startup_error(exc)
            if isinstance(exc,KMInvalid):
                code,message=exc.code,str(exc)[:256]
            elif isinstance(exc,RuntimeError) and str(exc).split(':',1)[0] in (
                'BACKUP_REQUIRED','BACKUP_VERIFICATION_FAILED','MIGRATION_BLOCKED',
                'INTEGRITY_FAILED','STATE_LOCKED','SCHEMA_INCOMPATIBLE'):
                code=str(exc).split(':',1)[0]
                message=code.replace('_',' ').capitalize()+'; consult protected diagnostics'
            else:
                code='INVALID_TYPE' if isinstance(exc,TypeError) else 'INVALID_VALUE' if isinstance(exc,ValueError) else 'KM_FAILURE'
                message=('Invalid request type' if code=='INVALID_TYPE' else
                         'Invalid request value' if code=='INVALID_VALUE' else
                         'KM operation failed; consult protected diagnostics')
            return {**base,'ok':False,'error':{'code':code,'message':message}}
