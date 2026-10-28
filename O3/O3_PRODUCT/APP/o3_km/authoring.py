"""User-authored Core LOEs and Workspace Organization overlays.

All writes run through KMService._commit on the existing single SQLite connection.
No source-file writes, alternate database or additional rule engine.
"""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from collections import defaultdict
from o3_core.store import digest, encode, now
from o3_core.rules import loe as loe_rules
from .evaluation import KMInvalid


def _str(x, maximum=160):
    if not isinstance(x,str) or not 1 <= len(x.strip()) <= maximum or any(ord(c)<32 for c in x):
        raise KMInvalid('INVALID_VALUE','Invalid or empty bounded text')
    return x.strip()


def _row(s, org_id, active=True):
    row=s.db.execute('SELECT * FROM ws_organizations WHERE ws_org_id=?',(org_id,)).fetchone()
    if not row:raise KMInvalid('UNKNOWN_RECORD','Unknown Workspace Organization')
    if active and row['archived']:raise KMInvalid('REFERENCE_INACTIVE','Workspace Organization archived')
    return row


def _members(s, org_id):
    return [x[0] for x in s.db.execute('SELECT core_org_id FROM ws_org_members WHERE ws_org_id=? ORDER BY core_org_id',(org_id,))]


def _raw(s, rid):
    r=s.engine.store.row(rid)
    if r is None or r['record_type']!='Organization':raise KMInvalid('UNKNOWN_RECORD','Raw Core Organization not found')
    return r


def _unassigned(s, ids, *, old_org=None):
    if not isinstance(ids,list) or not ids or len(ids)>100 or len(ids)!=len(set(ids)):
        raise KMInvalid('INVALID_VALUE','Explicit unique raw Organization list required')
    for rid in ids:
        if not isinstance(rid,str):raise KMInvalid('INVALID_TYPE','Raw organization ID must be text')
        _raw(s,rid)
        a=s.db.execute('SELECT ws_org_id FROM ws_org_members WHERE core_org_id=?',(rid,)).fetchone()
        if a and a[0]!=old_org:raise KMInvalid('ORG_ALREADY_ASSIGNED','Core Organization assigned to another overlay')


def organization_mutation(s, action, params, expected):
    """Audit-aware and exclusive assignment operations; never rewrite Core Organizations."""
    rid=params.get('ws_org_id') or params.get('organization_id')
    if action in ('create','absorb'):
        chosen=params.get('core_org_ids')
        if action=='absorb':
            main=params.get('base_core_org_id') or params.get('core_org_id')
            if not params.get('confirm_explicit_membership'):
                raise KMInvalid('CONFIGURATION_REQUIRED','Explicit Organization absorption confirmation required')
            _raw(s,main)
            extra=params.get('additional_core_org_ids',[])
            if not isinstance(extra,list):raise KMInvalid('INVALID_TYPE','Additional members must be a list')
            chosen=[main,*extra] if chosen is None else chosen
            if main not in chosen:raise KMInvalid('INVALID_VALUE','Base Organization must be included')
            existing=s.db.execute('SELECT ws_org_id FROM ws_org_members WHERE core_org_id=?',(main,)).fetchone()
            if existing:
                s._assert_revision(expected)
                # An already absorbed base keeps its one authoritative overlay; explicit add is supported separately.
                if set(chosen)!={main}:
                    raise KMInvalid('ORG_ALREADY_ASSIGNED','Use add_member for an existing overlay')
                return {'changed':False,'new_workspace_revision':s.revision(),
                        'ws_org_id':existing[0],'affected_core_ids':[], 'affected_property_ids':[], 'affected_view_ids':[]}
        _unassigned(s,chosen)
        if action=='absorb' and params.get('display_name') is None:
            label=json.loads(_raw(s,chosen[0])['properties_json']).get('name') or chosen[0]
        else:label=params.get('display_name')
        label=_str(label)
        new_id='wsorg_'+uuid.uuid4().hex
        def fn():
            stamp=now()
            s.db.execute('INSERT INTO ws_organizations VALUES(?,?,?,?,?,?)',(new_id,label,0,stamp,stamp,1))
            for org in chosen:
                s.db.execute('INSERT INTO ws_org_members VALUES(?,?,?)',(org,new_id,expected+1))
            return None,{'ws_org_id':new_id,'display_name':label,'members':sorted(chosen),'archived':False}, \
                   {'ws_org_id':new_id,'core_ids':sorted(chosen),'dependencies':sorted(chosen)}
        return s._commit('organization.'+action,'WorkspaceOrganization',new_id,expected,fn)

    if action=='undo':
        return undo_organization(s,params,expected)
    r=_row(s,rid,active=action!='restore')
    previous=_members(s,rid)
    if action=='rename':
        # The column is authoritative and required even when the optional
        # Workspace display-name recipe has not been selected.
        display=s.property_definition('ws:display_name')
        limit=min(160,display.get('max_chars',160)) if display else 160
        name=_str(params.get('display_name'),limit)
        def fn():
            before={'name':r['display_name']};after={'name':name}
            if before!=after:s.db.execute('UPDATE ws_organizations SET display_name=?,updated_at_utc=?,revision=revision+1 WHERE ws_org_id=?',(name,now(),rid))
            return before,after,{'ws_org_id':rid,'dependencies':[rid],'properties':['ws:display_name']}
        return s._commit('organization.rename','WorkspaceOrganization',rid,expected,fn)
    if action=='set_field':
        # Reuse the same typed owner-checked Workspace manual editor and audit contract.
        field=params.get('property_id')
        if field=='ws:display_name':return organization_mutation(s,'rename',{'ws_org_id':rid,'display_name':params.get('value')},expected)
        if field not in ('ws:parent_command','ws:hq_location','ws:enterprise','ws:poc_people'):
            raise KMInvalid('UNKNOWN_PROPERTY','Organization field not supported')
        return s._manual_mutation({'record_type':'WorkspaceOrganization','record_id':rid,
                                   'property_id':field,'value':params.get('value')},expected,
                                  clear=params.get('clear',False))
    if action=='add_member':
        source=params.get('core_org_id');_unassigned(s,[source])
        def fn():
            s.db.execute('INSERT INTO ws_org_members VALUES(?,?,?)',(source,rid,expected+1))
            s.db.execute('UPDATE ws_organizations SET updated_at_utc=?,revision=revision+1 WHERE ws_org_id=?',(now(),rid))
            return previous,sorted(previous+[source]),{'ws_org_id':rid,'core_ids':[source],'dependencies':[source]}
        return s._commit('organization.add_member','WorkspaceOrganization',rid,expected,fn)
    if action=='remove_member':
        source=params.get('core_org_id')
        if source not in previous:raise KMInvalid('UNKNOWN_RECORD','Core Organization not assigned here')
        if len(previous)==1:raise KMInvalid('ORG_EMPTY_MEMBERSHIP','Active overlay requires a member; archive to release the last')
        def fn():
            s.db.execute('DELETE FROM ws_org_members WHERE core_org_id=? AND ws_org_id=?',(source,rid))
            s.db.execute('UPDATE ws_organizations SET updated_at_utc=?,revision=revision+1 WHERE ws_org_id=?',(now(),rid))
            return previous,[p for p in previous if p!=source],{'ws_org_id':rid,'core_ids':[source],'dependencies':[source]}
        return s._commit('organization.remove_member','WorkspaceOrganization',rid,expected,fn)
    if action=='move_member':
        source=params.get('core_org_id');target=params.get('target_ws_org_id')
        if source not in previous:raise KMInvalid('UNKNOWN_RECORD','Member does not belong to source overlay')
        _row(s,target)
        if target==rid:raise KMInvalid('INVALID_VALUE','Cannot move member to same overlay')
        if not params.get('confirmed'):raise KMInvalid('CONFIGURATION_REQUIRED','Explicit move confirmation required')
        if len(previous)==1 and not params.get('archive_empty_source'):
            raise KMInvalid('ORG_EMPTY_MEMBERSHIP','Archive emptied source overlay explicitly')
        if len(previous)==1:
            children=[x['target_id'] for x in s.db.execute("SELECT target_id,value_json FROM ws_values WHERE property_id='ws:parent_command'")
                      if json.loads(x['value_json']).get('record_id')==rid]
            if children:raise KMInvalid('CONFIGURATION_REQUIRED','Reparent child commands before archiving their parent overlay')
        dest_members=_members(s,target)
        def fn():
            s.db.execute('UPDATE ws_org_members SET ws_org_id=?,assigned_revision=? WHERE core_org_id=? AND ws_org_id=?',
                         (target,expected+1,source,rid))
            if len(previous)==1:s.db.execute('UPDATE ws_organizations SET archived=1 WHERE ws_org_id=?',(rid,))
            s.db.execute('UPDATE ws_organizations SET updated_at_utc=?,revision=revision+1 WHERE ws_org_id IN (?,?)',(now(),rid,target))
            return {'from':rid,'to':None,'member':source},{'from':rid,'to':target,'member':source,'old_empty_archived':len(previous)==1},\
                   {'ws_org_id':target,'core_ids':[source],'dependencies':[rid,target,source]}
        return s._commit('organization.move_member','WorkspaceOrganization',rid,expected,fn)
    if action=='archive':
        if not params.get('confirmed') or params.get('membership_disposition')!='release':
            raise KMInvalid('CONFIGURATION_REQUIRED','Archive needs explicit confirmation and membership release')
        dependents=[x['target_id'] for x in s.db.execute("SELECT target_id,value_json FROM ws_values WHERE property_id='ws:parent_command'")
                    if json.loads(x['value_json']).get('record_id')==rid]
        if dependents:raise KMInvalid('CONFIGURATION_REQUIRED','Detach dependent child command first')
        def fn():
            s.db.execute('DELETE FROM ws_org_members WHERE ws_org_id=?',(rid,))
            s.db.execute('UPDATE ws_organizations SET archived=1,updated_at_utc=?,revision=revision+1 WHERE ws_org_id=?',(now(),rid))
            return {'archived':False,'members':previous},{'archived':True,'members':[]},\
                   {'ws_org_id':rid,'core_ids':previous,'prior_members':previous,'dependencies':previous}
        return s._commit('organization.archive','WorkspaceOrganization',rid,expected,fn)
    if action=='restore':
        if not r['archived']:raise KMInvalid('INVALID_VALUE','Not archived')
        requested=params.get('core_org_ids')
        if requested is None:
            recent=s.db.execute("SELECT before_json FROM ws_audit WHERE operation='organization.archive' AND subject_id=? ORDER BY event_id DESC LIMIT 1",(rid,)).fetchone()
            requested=json.loads(recent[0])['members'] if recent and recent[0] else []
        _unassigned(s,requested)
        def fn():
            s.db.execute('UPDATE ws_organizations SET archived=0,updated_at_utc=?,revision=revision+1 WHERE ws_org_id=?',(now(),rid))
            for m in requested:s.db.execute('INSERT INTO ws_org_members VALUES(?,?,?)',(m,rid,expected+1))
            return {'archived':True,'members':[]},{'archived':False,'members':sorted(requested)},\
                   {'ws_org_id':rid,'core_ids':requested,'dependencies':requested}
        return s._commit('organization.restore','WorkspaceOrganization',rid,expected,fn)
    raise KMInvalid('INVALID_VALUE','Unsupported Organization operation')


def undo_organization(s,params,expected):
    # Limited guarded inverse: last add or remove member only; other operations need explicit repair.
    rid=params.get('ws_org_id');_row(s,rid)
    last=s.db.execute('SELECT * FROM ws_audit ORDER BY event_id DESC LIMIT 1').fetchone()
    if not last or last['subject_id']!=rid or last['workspace_revision']!=expected:
        raise KMInvalid('REVISION_CONFLICT','Only latest unmodified Organization operation can be undone')
    typ=last['operation'];before=json.loads(last['before_json']) if last['before_json'] else None
    after=json.loads(last['after_json']) if last['after_json'] else None
    if typ not in ('organization.add_member','organization.remove_member'):
        raise KMInvalid('CONFIGURATION_REQUIRED','No safe automatic inverse; use an explicit edit')
    before=set(before);after=set(after)
    to_add=before-after;to_remove=after-before
    for m in to_add:_unassigned(s,[m])
    if len(_members(s,rid))==len(to_remove):raise KMInvalid('ORG_EMPTY_MEMBERSHIP','Undo would empty overlay')
    def fn():
        for m in to_add:s.db.execute('INSERT INTO ws_org_members VALUES(?,?,?)',(m,rid,expected+1))
        for m in to_remove:s.db.execute('DELETE FROM ws_org_members WHERE core_org_id=? AND ws_org_id=?',(m,rid))
        s.db.execute('UPDATE ws_organizations SET updated_at_utc=?,revision=revision+1 WHERE ws_org_id=?',(now(),rid))
        return sorted(after),sorted(before),{'ws_org_id':rid,'core_ids':sorted(to_add|to_remove),'dependencies':sorted(to_add|to_remove)}
    return s._commit('organization.undo','WorkspaceOrganization',rid,expected,fn)


def _loe_normalize(s,p,old=None):
    if not isinstance(p,dict):raise KMInvalid('INVALID_VALUE','LOE fields must be an object')
    forbidden=set(p)-{'name','objective','kind','reference_code','aliases'}
    if forbidden:raise KMInvalid('INVALID_VALUE','Unknown user-authored LOE fields')
    data={} if old is None else {k:v for k,v in old.items() if k!='__record_id'}
    data.update(p)
    name=_str(data.get('name'),120)
    if not loe_rules.valid_name(name):raise KMInvalid('INVALID_VALUE','Invalid LOE name')
    kind=data.get('kind','loe')
    if kind not in ('loe','project','program','initiative','workstream'):
        raise KMInvalid('INVALID_VALUE','Invalid LOE kind')
    code=data.get('reference_code')
    if code is not None and code!='':
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{1,31}',code):
            raise KMInvalid('INVALID_VALUE','Invalid LOE reference code')
    else:code=None
    aliases=data.get('aliases',[])
    if not isinstance(aliases,list) or len(aliases)>32 or len(set(aliases))!=len(aliases):
        raise KMInvalid('INVALID_VALUE','Invalid aliases')
    aliases=[_str(x,120) for x in aliases]
    if any(not loe_rules.valid_name(x) for x in aliases):raise KMInvalid('INVALID_VALUE','Invalid LOE alias')
    objective=data.get('objective')
    if objective is not None:objective=_str(objective,4000)
    output={'name':name,'kind':kind,'reference_code':code,'aliases':aliases,'objective':objective,
            'namespace':s.engine.settings.loe_namespace}
    # Do not create aliases/codes with ambiguous exact matching in same namespace.
    registry=[]
    for row in s.db.execute("SELECT record_id,properties_json,added_json,active FROM records WHERE record_type='LineOfEffort'"):
        if old and row['record_id']==old.get('__record_id'):continue
        if not row['active']:continue
        addition=json.loads(row['added_json'])
        if addition.get('km_ownership')=='legacy_retired':continue
        other=json.loads(row['properties_json'])
        if other.get('namespace',row['record_id'])!=output['namespace']:continue
        registry.append(other)
    code_key=code.casefold() if code else None
    all_labels={loe_rules.norm_key(name),*[loe_rules.norm_key(v) for v in aliases]}
    for other in registry:
        if code_key and other.get('reference_code') and other['reference_code'].casefold()==code_key:
            raise KMInvalid('LOE_REFERENCE_AMBIGUOUS','Duplicate LOE code')
        other_labels={loe_rules.norm_key(v) for v in ([other.get('name','')]+other.get('aliases',[])) if isinstance(v,str)}
        if all_labels & other_labels:raise KMInvalid('LOE_REFERENCE_AMBIGUOUS','Conflicting explicit LOE alias')
    return output


def loe_mutation(s,action,params,expected):
    rid=params.get('record_id') or params.get('loe_id')
    if action=='create':
        p=_loe_normalize(s,params.get('fields',{}))
        new_id='loe_user_'+uuid.uuid4().hex
        fp=digest(encode({'authored_version':1, 'fields':p}).encode())
        def fn():
            s.engine.store.upsert('LineOfEffort',new_id,p['namespace'],'user_created',new_id,fp,'',p,
                                  {'km_ownership':'user_created'},None,None)
            return None,{'record_id':new_id,'fields':p},{'core_ids':[new_id],'loe_id':new_id,'dependencies':[new_id]}
        return s._commit('loe.create','LineOfEffort',new_id,expected,fn,core=True)
    row=s._assert_target('LineOfEffort',rid,active=False)
    owner=row['identity_basis'];p=json.loads(row['properties_json']);a=json.loads(row['added_json'])
    if action=='review':
        return {'changed':False,'new_workspace_revision':s.revision(),'record_id':rid,'review_status':a.get('km_ownership','legacy_derived'),
                'source_evidence':s.engine.trace_record(rid)['current_derived_support']}
    if action in ('retain','adopt','retire'):
        if owner not in ('scoped_explicit_label','scoped_reference_code','user_created','user_adopted'):
            raise KMInvalid('INVALID_VALUE','Unsupported LOE identity ownership')
        if action=='adopt' and owner in ('user_created','user_adopted'):
            return {'changed':False,'new_workspace_revision':s.revision(),'record_id':rid}
        if action=='retire' and owner=='user_created':return loe_mutation(s,'archive',params,expected)
        if action=='adopt':
            p=_loe_normalize(s,params.get('fields',{}),{**p,'__record_id':rid})
            new_owner='user_adopted';new_add={**a,'km_ownership':'user_adopted'};active=1
        elif action=='retire':
            new_owner=owner;new_add={**a,'km_ownership':'legacy_retired'};active=0
        else:
            new_owner=owner;new_add={**a,'km_ownership':'legacy_retained'};active=row['active']
        def fn():
            before={'identity_basis':owner,'added':a,'active':bool(row['active'])}
            after={'identity_basis':new_owner,'added':new_add,'active':bool(active)}
            if before!=after:
                s.db.execute('UPDATE records SET identity_basis=?,added_json=?,active=?,updated_at=? WHERE record_id=?',
                             (new_owner,encode(new_add),active,now(),rid))
                if action=='adopt':
                    s.db.execute('UPDATE records SET properties_json=?,fingerprint=? WHERE record_id=?',
                                 (encode(p),digest(encode({'authored_version':1,'fields':p}).encode()),rid))
            return before,after,{'core_ids':[rid],'dependencies':[rid]}
        return s._commit('loe.'+action,'LineOfEffort',rid,expected,fn,core=True)
    if owner not in ('user_created','user_adopted'):
        raise KMInvalid('INVALID_VALUE','Legacy LOE must be adopted before manual editing')
    if action=='edit':
        new=_loe_normalize(s,params.get('changes',{}),{**p,'__record_id':rid})
        def fn():
            if p!=new:
                s.db.execute('UPDATE records SET properties_json=?,fingerprint=?,updated_at=? WHERE record_id=?',
                             (encode(new),digest(encode({'authored_version':1,'fields':new}).encode()),now(),rid))
            return p,new,{'core_ids':[rid],'dependencies':[rid]}
        return s._commit('loe.edit','LineOfEffort',rid,expected,fn,core=True)
    if action=='archive':
        before={'active':bool(row['active'])};after={'active':False}
        def fn():
            if before!=after:s.db.execute('UPDATE records SET active=0,updated_at=? WHERE record_id=?',(now(),rid))
            return before,after,{'core_ids':[rid],'dependencies':[rid]}
        return s._commit('loe.archive','LineOfEffort',rid,expected,fn,core=True)
    raise KMInvalid('INVALID_VALUE','Unknown LOE mutation')
