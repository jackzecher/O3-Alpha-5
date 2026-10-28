"""Read-only KM 1.0 projections for the existing single SQLite and engine worker.

Queries never materialize another authoritative graph or cached Workspace results.
All reported rollups retain the supporting directed Core evidence paths.
"""
from __future__ import annotations
import base64
import datetime as dt
import hashlib
import heapq
import json
from collections import defaultdict
from functools import cmp_to_key
from zoneinfo import ZoneInfo
from .workspace_types import is_type_id
from .catalog import TYPES,PROPERTIES,PRESETS,VIEWS,VIEW_PRESET,CORE_PREDICATES,TARGETS
from .evaluation import Evaluator,KMInvalid,_datetime


def _json(x):return json.loads(x) if isinstance(x,str) else x

def _safe_stamp(t):
    if isinstance(t,str):return _datetime(t)
    if isinstance(t,dict):return _datetime(t.get('utc'))
    return None

def _enc(x):return base64.urlsafe_b64encode(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).decode().rstrip('=')

def _dec(s):
    if not isinstance(s,str) or len(s)>4096:raise KMInvalid('INVALID_VALUE','Malformed pagination token')
    try:return json.loads(base64.urlsafe_b64decode(s+'='*((-len(s))%4)))
    except (ValueError,TypeError):raise KMInvalid('INVALID_VALUE','Malformed pagination token')

def _hash(x):return hashlib.sha256(json.dumps(x,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()

class QueryMixin:
    def _bound_manual(self,record_type,template_id,params,key,value_type):
        """Resolve an optional user-owned field; never assume a recipe was installed.

        An explicit binding can target any compatible user-created property, not just
        one initially created from the named recipe. Ambiguous recipe variants have
        no implicit winner. This is a read-only presentation decision.
        """
        bindings=params.get('workspace_bindings',{})
        if not isinstance(bindings,dict) or any(not isinstance(k,str) or not isinstance(v,str)
                for k,v in bindings.items()):
            raise KMInvalid('INVALID_TYPE','Workspace field bindings must be a map of property IDs')
        chosen=bindings.get(key)
        if chosen is None:
            matches=self.installed_template_properties(template_id)
            chosen=matches[0] if len(matches)==1 else None
        if chosen is None:return None
        definition=self.property_definition(chosen)
        if (not definition or definition['owner_type']!=record_type or
            definition['mode']!='manual' or definition['value_type']!=value_type or
            definition['status']!='enabled'):
            raise KMInvalid('CONFIGURATION_REQUIRED','Workspace field binding is incompatible or disabled')
        return chosen

    def _snapshot(self,as_of):return {'core_epoch':self.epoch(),'workspace_revision':self.revision(),'as_of':as_of.isoformat(),'timezone':self.settings()['workspace_timezone']}

    def _page_request(self,params,as_of,query_id,sort=None):
        """Validate a cursor before scanning; total is checked once matches are known."""
        size=params.get('page_size',50)
        if type(size) is not int or not (1<=size<=200):
            raise KMInvalid('INVALID_VALUE','page_size must be an integer 1..200')
        fingerprint=_hash({'query':query_id,'parameters':{k:v for k,v in params.items() if k not in ('page_size','cursor')},'sort':sort})
        snap=self._snapshot(as_of);offset=0
        if params.get('cursor'):
            cur=_dec(params['cursor'])
            if not isinstance(cur,dict):
                raise KMInvalid('INVALID_VALUE','Invalid pagination cursor')
            if cur.get('snapshot')!=snap or cur.get('fingerprint')!=fingerprint:
                raise KMInvalid('SNAPSHOT_EXPIRED','Query state changed; start again at page one')
            offset=cur.get('offset',-1)
            if type(offset) is not int or offset<0:
                raise KMInvalid('INVALID_VALUE','Invalid pagination offset')
        return size,offset,snap,fingerprint

    def _page_window(self,params,as_of,query_id,total,sort=None):
        """One cursor validation path for SQL-bounded and evaluated pages."""
        size,offset,snap,fingerprint=self._page_request(params,as_of,query_id,sort)
        if offset>total:raise KMInvalid('INVALID_VALUE','Invalid pagination offset')
        next_offset=offset+size
        nxt=_enc({'snapshot':snap,'fingerprint':fingerprint,'offset':next_offset}) if next_offset<total else None
        return size,offset,{'total_count':total,'next_cursor':nxt,'snapshot':snap}

    def _page(self,items,params,as_of,query_id,sort=None):
        size,offset,metadata=self._page_window(params,as_of,query_id,len(items),sort=sort)
        return {'rows':items[offset:offset+size],**metadata}

    def _simple_record_page(self,kind,params,as_of,sort_field,descending):
        """Page only source-backed/Core rows needed by a plain list.

        Computed filters, full-text search, JSON field sorting and grouping stay
        on the existing evaluator path so their precise semantics do not change.
        """
        where=['1=1'];values=[]
        if kind is not None:
            where.append('record_type=?');values.append(kind)
        if not params.get('include_inactive',False):where.append('active=1')
        if params.get('record_id'):
            where.append('record_id=?');values.append(params['record_id'])
        condition=' AND '.join(where)
        count=self.db.execute('SELECT COUNT(*) FROM records WHERE '+condition,values).fetchone()[0]
        size,offset,metadata=self._page_window(params,as_of,'records.query:'+str(kind),count)
        order='DESC' if descending else 'ASC'
        # Both columns contain canonical ASCII identifiers / normalized ISO UTC
        # timestamps. Tie-breaker is the stable record ID, as before.
        selected=list(self.db.execute('SELECT * FROM records WHERE '+condition+
            f' ORDER BY {sort_field} COLLATE NOCASE {order}, record_id COLLATE NOCASE {order} LIMIT ? OFFSET ?',
            [*values,size,offset]))
        evaluator=Evaluator(self,as_of)
        evaluator.prepare_manual_page(kind,selected)
        return {'kind':'standard','group_by':params.get('group_by'),'group_counts':{},
                'rows':[self._decorate(r,evaluator) for r in selected],**metadata}

    def _records_by_id(self,record_ids):
        """Read bounded batches, not one query per reference; no lifetime cache."""
        ids=list(dict.fromkeys(record_ids));out={}
        for start in range(0,len(ids),200):
            chunk=ids[start:start+200]
            marks=','.join('?' for _ in chunk)
            out.update((r['record_id'],r) for r in self.db.execute(
                'SELECT * FROM records WHERE record_id IN ('+marks+')',chunk))
        return out

    def _reference_records(self,targets):
        """Lightweight typed references for existing neighbor-label presentation."""
        core=self._records_by_id(rid for rid,kind in targets.items() if kind!='WorkspaceOrganization')
        overlays={};ids=[rid for rid,kind in targets.items() if kind=='WorkspaceOrganization']
        for start in range(0,len(ids),200):
            chunk=ids[start:start+200];marks=','.join('?' for _ in chunk)
            overlays.update((r['ws_org_id'],r) for r in self.db.execute(
                'SELECT ws_org_id,display_name,archived FROM ws_organizations WHERE ws_org_id IN ('+marks+')',chunk))
        result={}
        for rid,kind in targets.items():
            if kind=='WorkspaceOrganization':
                row=overlays.pop(rid,None)
                if row is None:raise KMInvalid('UNKNOWN_RECORD','Unknown Workspace Organization')
                result[rid]={'record_id':rid,'record_type':'Organization',
                    'effective_kind':'workspace_overlay','display_name':row['display_name'],
                    'active':not bool(row['archived']),'authority':'workspace_manual'}
            else:
                row=core.pop(rid,None)
                if row is None:raise KMInvalid('UNKNOWN_RECORD','Unknown Core record')
                if row['record_type']!=kind:raise KMInvalid('INVALID_TYPE','Wrong reference type')
                result[rid]={'record_id':rid,'record_type':kind,'properties':_json(row['properties_json']),
                    'additional':_json(row['added_json']),'active':bool(row['active']),
                    'updated_at':row['updated_at'],'authority':'core'}
        return result

    def _record_rows(self,kind=None,include_inactive=False):
        if kind is not None and kind not in TYPES:raise KMInvalid('INVALID_TYPE','Unknown Core Record Type')
        sql='SELECT * FROM records WHERE '+('record_type=? AND ' if kind else '')+('1=1' if include_inactive else 'active=1')+' ORDER BY record_id'
        return list(self.db.execute(sql, (kind,) if kind else ()))

    def _decorate(self,r,evaluator=None):
        result={'record_type':r['record_type'],'record_id':r['record_id'],'scope':r['scope'],
          'active':bool(r['active']),'identity_basis':r['identity_basis'],
          'properties':_json(r['properties_json']),'additional':_json(r['added_json']),
          'updated_at':r['updated_at'],'authority':'core',
          'identity_key':r['identity_key'],'source_sha':r['source_sha'],'fingerprint':r['fingerprint']}
        if evaluator and r['active']:
            vals={}
            for prop in evaluator.properties_by_kind.get(r['record_type'],[]):
                vals[prop]=evaluator.evaluate(r,prop)
            result['workspace_properties']=vals
        if r['record_type']=='Organization':
            effective=self.effective_id(r['record_id'])
            result['assigned_ws_org_id']=effective if effective!=r['record_id'] else None
        if r['record_type']=='Email':
            from .reply_evidence import source_time,context
            result['properties']['source_time']=source_time(result['properties'],result['additional'])
            if evaluator and evaluator.include_reply_evidence:
                result['reply_evidence']=evaluator.reply_evidence(r)
                result['properties']['reply_status']=result['reply_evidence']['status']
                sender=evaluator.sender_identity(r)
                result['properties']['sender_identity']=sender['status']
                result['field_evidence']={'reply_status':result['reply_evidence'],'sender_identity':sender}
        return result

    def _relations(self,rid,include_history=False,*,edge_key=None):
        # One entry per asserted directed Core edge, preserving each owner's evidence.
        sql='''SELECT r.*,s.record_type source_type,t.record_type target_type,s.active sa,t.active ta
                 FROM relationships r JOIN records s ON s.record_id=r.source_id
                 JOIN records t ON t.record_id=r.target_id WHERE (r.source_id=? OR r.target_id=?)'''
        values=[rid,rid]
        if edge_key is not None:
            sql+=' AND r.source_id=? AND r.predicate=? AND r.target_id=?'
            values.extend(edge_key)
        out={}
        for r in self.db.execute(sql,values):
            if not include_history and (not r['sa'] or not r['ta']):continue
            key=(r['source_id'],r['predicate'],r['target_id'])
            obj=out.setdefault(key,{'source_id':key[0],'predicate':key[1], 'target_id':key[2],
                'source_type':r['source_type'],'target_type':r['target_type'],
                'direction':'outgoing' if key[0]==rid else 'incoming','authority':'core','claims':[]})
            obj['claims'].append({'owner':r['owner'],'evidence':_json(r['evidence_json'])})
        return [out[k] for k in sorted(out)]

    def _org_mapping(self):
        return {r['core_org_id']:r['ws_org_id'] for r in self.db.execute('SELECT core_org_id,ws_org_id FROM ws_org_members')}

    def _organization_effective_rows(self,include_inactive=False):
        assignment=self._org_mapping();overlays={}
        for r in self.db.execute('SELECT * FROM ws_organizations ORDER BY display_name,ws_org_id'):
            if r['archived'] and not include_inactive:continue
            overlays[r['ws_org_id']]={'record_type':'Organization','record_id':r['ws_org_id'],
                'effective_kind':'workspace_overlay','display_name':r['display_name'],
                'active':not bool(r['archived']),'core_org_ids':[], 'authority':'workspace_manual',
                'properties':{x:self.manual_value('WorkspaceOrganization',r['ws_org_id'],x) for x in
                               ('ws:parent_command','ws:hq_location','ws:enterprise','ws:poc_people')},
                'source_evidence':[]}
        visible=[]
        # Retained raw identities remain assigned to the user-owned overlay,
        # even when source support is temporarily or permanently inactive.
        for r in self._record_rows('Organization',True):
            oid=assignment.get(r['record_id']);props=_json(r['properties_json'])
            if oid in overlays:
                obj=overlays[oid];obj['core_org_ids'].append(r['record_id'])
                obj['source_evidence'].append({'core_org_id':r['record_id'],'name':props.get('name'),'active':bool(r['active'])})
            elif not oid and (include_inactive or r['active']):
                visible.append({'record_type':'Organization','record_id':r['record_id'],'display_name':props.get('name',r['record_id']),
                    'effective_kind':'raw_unassigned','active':bool(r['active']),'core_org_ids':[r['record_id']],
                    'authority':'core','properties':props,'source_evidence':[{'core_org_id':r['record_id']}]})
        for obj in overlays.values():
            obj['core_org_ids'].sort()
            obj['core_support']='present' if any(e['active'] for e in obj['source_evidence']) else 'no_current_core_support'
        return sorted([*overlays.values(),*visible], key=lambda r:(r['display_name'].casefold(),r['record_id']))

    def _raw_organizations(self,include_inactive=True):
        assignment=self._org_mapping()
        out=[]
        for r in self._record_rows('Organization',include_inactive):
            p=_json(r['properties_json']);parent=assignment.get(r['record_id'])
            out.append({'record_type':'Organization','record_id':r['record_id'],'name':p.get('name'),
                'kind':p.get('kind'),'scope':r['scope'],'active':bool(r['active']),
                'assigned_ws_org_id':parent,'mapping_state':'assigned' if parent else 'unassigned',
                'source_sha':r['source_sha'],'identity_basis':r['identity_basis'],'authority':'core'})
        return out

    def _listed(self,kind,params,as_of,*,render='standard'):
        if params.get('sorts') and kind is not None:
            return self._configured_view(as_of,params.get('view_id'),{'label':'Collection','record_type':kind,
                'sorts':params['sorts'],'filters':params.get('filters',[]),'group_by':params.get('group_by'),
                'include_inactive':params.get('include_inactive',False)},params)
        if kind is not None and kind not in TYPES:
            raise KMInvalid('INVALID_TYPE','Unknown Core Record Type')
        sort_field=params.get('sort','updated_at')
        if sort_field not in ('updated_at','record_id','name','title','start_utc'):
            raise KMInvalid('INVALID_VALUE','Unrecognized sort field')
        direction=params.get('sort_direction','desc')
        if direction not in ('asc','desc'):
            raise KMInvalid('INVALID_VALUE','Unrecognized sort direction')
        group_by=params.get('group_by')
        if group_by not in (None,'none','record_type','scope'):
            raise KMInvalid('INVALID_VALUE','Unsupported grouping')
        if (render=='standard' and params.get('search') is None and
            not params.get('filters') and group_by in (None,'none') and
            sort_field in ('updated_at','record_id')):
            return self._simple_record_page(kind,params,as_of,sort_field,direction=='desc')
        # Scan source rows lazily, evaluate only fields needed for inclusion, and
        # fully decorate only the requested page. Exact Unicode/JSON search and
        # Python filter semantics are intentionally retained (not SQL LIKE).
        search=params.get('search')
        if search is not None and (not isinstance(search,str) or len(search)>160):
            raise KMInvalid('INVALID_VALUE','Invalid search text')
        text=search.strip().casefold() if search is not None else None
        filters=params.get('filters',[])
        if filters and (not isinstance(filters,list) or len(filters)>12):
            raise KMInvalid('INVALID_VALUE','Invalid filters')
        for f in filters or []:
            if (not isinstance(f,dict) or not isinstance(f.get('field'),str) or
                f.get('operator') not in ('equals','not_equals','is_present','one_of')):
                raise KMInvalid('INVALID_VALUE','Unsupported record filter')
        query_id='records.query:'+str(kind)
        size,offset,_,_=self._page_request(params,as_of,query_id)
        evaluator=Evaluator(self,as_of)
        where=['1=1'];values=[]
        if kind is not None:where.append('record_type=?');values.append(kind)
        if not params.get('include_inactive',False):where.append('active=1')
        if params.get('record_id'):where.append('record_id=?');values.append(params['record_id'])
        ordered=group_by in (None,'none') and sort_field in ('updated_at','record_id')
        order='DESC' if direction=='desc' else 'ASC'
        # Canonical record IDs and normalized update timestamps are ASCII. Other
        # source-field sorts/grouping keep Python's full Unicode casefold order.
        sql='SELECT * FROM records WHERE '+' AND '.join(where)
        if ordered:sql+=f' ORDER BY {sort_field} COLLATE NOCASE {order},record_id {order}'
        groups={};count=0

        def matches(row,props,added):
            for f in filters or []:
                field=f['field'];value=added.get(field,props.get(field))
                if (row['active'] and field.startswith('ws:') and
                        field in evaluator.properties_by_kind.get(row['record_type'],())):
                    assessed=evaluator.evaluate(row,field)
                    value=assessed['value'] if assessed['state']=='evaluated' else None
                op=f['operator'];asked=f.get('value')
                answer=(value is not None if op=='is_present' else value==asked if op=='equals' else
                        value is not None and value!=asked if op=='not_equals' else
                        isinstance(asked,list) and value in asked)
                if not answer:return False
            return True

        def selected():
            nonlocal count
            for row in self.db.execute(sql,values):
                # A rule's transitive values are reused within this record only.
                # Do not let the evaluator retain results for the whole corpus.
                evaluator.cache.clear()
                props=_json(row['properties_json']);added=_json(row['added_json'])
                if text is not None and not (text in row['record_id'].casefold() or
                    text in json.dumps(props,ensure_ascii=False).casefold() or
                    text in json.dumps(added,ensure_ascii=False).casefold()):continue
                if not matches(row,props,added):continue
                position=count;count+=1
                if ordered:
                    if offset<=position<offset+size:yield row['record_id']
                else:
                    value=(row[sort_field] if sort_field in row.keys() else None) or added.get(sort_field) or props.get(sort_field) or ''
                    key=(str(value).casefold(),row['record_id'])
                    if group_by not in (None,'none'):
                        label=str(row[group_by] or 'Not supplied')
                        groups[label]=groups.get(label,0)+1
                        key=(label.casefold(),*key)
                    # Retain compact sort keys/IDs, never all decorated records.
                    yield key,row['record_id']
        if ordered:
            ids=list(selected())
        else:
            choose=heapq.nlargest if direction=='desc' else heapq.nsmallest
            chosen=choose(offset+size,selected(),key=lambda item:item[0])
            ids=[rid for _,rid in chosen[offset:offset+size]]
        _,_,metadata=self._page_window(params,as_of,query_id,count)
        evaluator.cache.clear()
        page=self._records_by_id(ids);rows=[]
        for rid in ids:
            evaluator.cache.clear()
            rows.append(self._decorate(page[rid],evaluator))
        return {'kind':render,'group_by':group_by,'group_counts':groups,'rows':rows,**metadata}

    def _event_interval(self,row):
        p=_json(row['properties_json']);a=_json(row['added_json'])
        start=_safe_stamp(a.get('start_utc')) or _safe_stamp(p.get('dtstart'))
        end=_safe_stamp(a.get('end_utc')) or _safe_stamp(p.get('dtend'))
        return start,end

    def _event_window(self,view,as_of):
        tz=ZoneInfo(self.settings()['workspace_timezone']);local=as_of.astimezone(tz)
        midnight=dt.datetime.combine(local.date(),dt.time.min,tzinfo=tz)
        if view=='today':nextday=midnight.date()+dt.timedelta(days=1);end=dt.datetime.combine(nextday,dt.time.min,tzinfo=tz)
        elif view=='this_week':
            week_start=0 if self.settings()['week_start']=='monday' else 6
            day=midnight.date()-dt.timedelta(days=(midnight.weekday()-week_start)%7)
            midnight=dt.datetime.combine(day,dt.time.min,tzinfo=tz)
            end=dt.datetime.combine(day+dt.timedelta(days=7),dt.time.min,tzinfo=tz)
        else:
            day=midnight.date().replace(day=1)
            midnight=dt.datetime.combine(day,dt.time.min,tzinfo=tz)
            next_month=(day.replace(year=day.year+1,month=1) if day.month==12 else day.replace(month=day.month+1))
            end=dt.datetime.combine(next_month,dt.time.min,tzinfo=tz)
        return midnight.astimezone(dt.timezone.utc),end.astimezone(dt.timezone.utc)

    def _event_rows(self,view,as_of):
        a,b=self._event_window(view,as_of)
        matched=[];unresolved=0
        for r in self._record_rows('Event'):
            start,end=self._event_interval(r)
            if start is None or end is None:unresolved+=1;continue
            if (start==end and a<=start<b) or (start<b and end>a and end>start):
                matched.append(self._decorate(r,Evaluator(self,as_of)))
        matched.sort(key=lambda r:(r['additional'].get('start_utc') or '',r['record_id']))
        return matched,unresolved

    def _last_correspondence(self,kind,rid,*,conversation_id=None):
        """Verified, scoped last observation; do not infer unverified identity edges."""
        if kind=='Conversation':
            if not rid or not conversation_id:return None
            candidates=[]
            for row in self._record_rows('Email'):
                prop=_json(row['properties_json']);addition=_json(row['added_json'])
                if (prop.get('mailbox_id'),prop.get('conversation_id'))!=(rid,conversation_id):continue
                direction=addition.get('direction')
                stamp=_safe_stamp(prop.get('sent_at_utc') if direction=='Outgoing' else prop.get('received_at_utc'))
                if stamp:candidates.append((stamp,row['record_id'],direction))
            if not candidates:return None
            stamp,mail,direction=max(candidates,key=lambda x:(x[0],x[1]))
            return {'email_id':mail,'direction':direction,'timestamp':stamp.isoformat(),
                    'verified_path':[{'type':'source_conversation_identity','mailbox_id':rid,
                                      'conversation_id':conversation_id,'email_id':mail}],
                    'authority':'source_scoped_group','coverage':self.coverage('Email')}
        people={rid:[]} if kind=='Person' else {}
        if kind in ('Organization','WorkspaceOrganization'):
            members={rid} if kind=='Organization' else set(self._get_ws_org_members(rid))
            for org in members:
                for rel in self._relations(org):
                    if rel['predicate'] in ('affiliated_with','member_of') and rel['source_type']=='Person' and rel['target_id']==org:
                        people.setdefault(rel['source_id'],[]).append(rel)
        best=None
        for person,path in people.items():
            # Select candidate source fields in one join, not one lookup per
            # edge. Keep only the maximum; retrieve all winning owner claims
            # through the same authoritative relation projection afterwards.
            sql="""SELECT DISTINCT r.source_id,r.predicate,r.target_id,
                          m.properties_json,m.added_json
                   FROM relationships r JOIN records m ON m.record_id=r.source_id
                   JOIN records p ON p.record_id=r.target_id
                   WHERE r.target_id=? AND m.record_type='Email' AND m.active=1
                     AND p.active=1 AND r.predicate IN ('sent_by','sent_to','copied_to')
                   ORDER BY r.source_id,r.predicate,r.target_id"""
            for source in self.db.execute(sql,(person,)):
                prop=_json(source['properties_json']);addition=_json(source['added_json'])
                direction=addition.get('direction')
                if direction not in ('Incoming','Outgoing'):continue
                stamp=_safe_stamp(prop.get('sent_at_utc') if direction=='Outgoing' else prop.get('received_at_utc'))
                if stamp and (best is None or (stamp,source['source_id'])>best[:2]):
                    edge_key=(source['source_id'],source['predicate'],source['target_id'])
                    best=(stamp,source['source_id'],direction,person,edge_key,path)
        if best is None:return None
        stamp,mail,direction,person,edge_key,path=best
        edge=self._relations(person,edge_key=edge_key)[0]
        verified_path=[edge,*path]
        return {'email_id':mail,'timestamp':stamp.isoformat(),'direction':direction,
                'verified_path':verified_path,'authority':'core_traversal','coverage':self.coverage('Email')}

    def _attachment_trace(self,r):
        """Separate source attachment descriptors from actually converted Document evidence."""
        p=_json(r['properties_json'])
        items=p.get('attachments') or []
        descriptors=items if isinstance(items,list) else []
        claimed=[]
        for edge in self._relations(r['record_id']):
            if edge['predicate']!='attached_to' or edge['target_id']!=r['record_id'] or edge['source_type']!='Document':continue
            doc=self.engine.store.row(edge['source_id'])
            if doc is None or not doc['active']:continue
            detail=_json(doc['properties_json']);origin=detail.get('origin') or {}
            descriptor_id=origin.get('attachment_id') if isinstance(origin,dict) else None
            matched=[a for a in descriptors if isinstance(a,dict) and a.get('attachment_id')==descriptor_id] if descriptor_id else []
            claimed.append({'document_id':doc['record_id'],'core_claim':edge,
                            'source_attachment_id':descriptor_id,
                            'descriptor_verified':len(matched)==1,
                            'binary_available':False,'authority':'core_attached_to'})
        return {'has_attachments_source':p.get('has_attachments'),
                'descriptors':descriptors,'converted_documents':claimed,
                'unmatched_descriptor_count':sum(1 for a in descriptors if isinstance(a,dict) and not any(c['source_attachment_id']==a.get('attachment_id') and c['descriptor_verified'] for c in claimed)),
                'source_binary_included':False}

    def _get_ws_org_members(self,rid):
        return [r[0] for r in self.db.execute('SELECT core_org_id FROM ws_org_members WHERE ws_org_id=?',(rid,))]

    def _manual_relationships(self,kind,rid):
        """Project typed reference values, preserving their sole ws_values authority."""
        result=[]
        sql="""SELECT v.target_kind,v.target_id,v.property_id,v.value_json,v.revision,
                      d.specification_json FROM ws_values v
               JOIN ws_definitions d ON d.definition_id=v.property_id
               WHERE d.definition_kind='property' AND d.state='enabled'
                 AND json_extract(d.specification_json,'$.mode')='manual'
                 AND json_extract(d.specification_json,'$.value_type') IN ('record_ref','record_ref_set')
                 AND ((v.target_kind=? AND v.target_id=?)
                   OR json_extract(v.value_json,'$.record_id')=?
                   OR EXISTS(SELECT 1 FROM json_each(v.value_json) j
                       WHERE j.type='object' AND json_extract(j.value,'$.record_id')=?))"""
        for value in self.db.execute(sql,(kind,rid,rid,rid)):
            definition=_json(value['specification_json']);stored=_json(value['value_json'])
            references=stored if definition['value_type']=='record_ref_set' else [stored]
            for reference in references:
                if not isinstance(reference,dict):continue
                source_kind=value['target_kind'];source=value['target_id']
                target_kind=reference['record_type'];target=reference['record_id']
                outgoing=(source_kind,source)==(kind,rid)
                if not outgoing and (target_kind,target)!=(kind,rid):continue
                result.append({'source_id':source,'source_type':source_kind,
                    'predicate':value['property_id'],'property_id':value['property_id'],
                    'label':definition.get('label',value['property_id']),
                    'target_id':target,'target_type':target_kind,
                    'direction':'outgoing' if outgoing else 'incoming',
                    'authority':'workspace_manual','editable':True,
                    'claims':[{'owner':value['property_id'],'authority':'workspace_manual',
                               'evidence':{'storage':'ws_values','value_revision':value['revision'],
                                           'target_kind':source_kind,'target_id':source,'property_id':value['property_id']}}]})
        return result

    def _relationships_for(self,kind,rid):
        self._assert_target(kind,rid,active=False)
        if kind=='WorkspaceOrganization':
            members=self._get_ws_org_members(rid)
            distinct={}
            for org in members:
                for edge in self._relations(org):
                    source=self.effective_id(edge['source_id']) if edge['source_type']=='Organization' else edge['source_id']
                    target=self.effective_id(edge['target_id']) if edge['target_type']=='Organization' else edge['target_id']
                    if source==target:continue  # same overlay: historical raw mapping remains auditable
                    key=(source,edge['predicate'],target)
                    rolled=distinct.setdefault(key,{'source_id':source,'predicate':edge['predicate'],'target_id':target,
                        'source_type':'WorkspaceOrganization' if source!=edge['source_id'] else edge['source_type'],
                        'target_type':'WorkspaceOrganization' if target!=edge['target_id'] else edge['target_type'],
                        'direction':'outgoing' if source==rid else 'incoming',
                        'authority':'workspace_rollup','claims':[],'supporting_core_edges':[]})
                    rolled['supporting_core_edges'].append(edge)
                    rolled['claims'].extend(edge['claims'])
            return {'target_id':rid,'authority':'workspace_rollup','core_org_ids':members,
                    'edges':[distinct[k] for k in sorted(distinct)]+self._manual_relationships(kind,rid)}
        return {'target_id':rid,'authority':'mixed','edges':self._relations(rid)+self._manual_relationships(kind,rid)}

    def _effective_rollups(self,kind,rid,params):
        """Union supported current paths; no new RDF edges or duplicated source facts."""
        if kind=='WorkspaceOrganization':members=set(self._get_ws_org_members(rid))
        elif kind=='Organization':members={rid}
        else:raise KMInvalid('INVALID_TYPE','Organization rollup target required')
        current={org for org in members if (row:=self.engine.store.row(org)) and row['active']}
        groups={k:{} for k in ('People','Email','Event','Document','LineOfEffort')}
        def add(group,record_id,path):
            data=groups[group]
            obj=data.setdefault(record_id,{'record_type':group,'record_id':record_id,
                    'effective_org_id':rid,'underlying_core_org_ids':sorted(members),
                    'authority':'workspace_rollup' if kind=='WorkspaceOrganization' else 'core_traversal',
                    'support_paths':[]})
            if path not in obj['support_paths']:obj['support_paths'].append(path)
        people={}
        for org in sorted(current):
            for edge in self._relations(org):
                if edge['target_id']==org and edge['source_type']=='Person' and edge['predicate'] in ('affiliated_with','member_of'):
                    add('People',edge['source_id'],[edge]);people.setdefault(edge['source_id'],[]).append(edge)
        for person,rawpaths in sorted(people.items()):
            for edge in self._relations(person):
                if edge['target_id']!=person:continue
                category=edge['source_type']
                predicates={'Email':('sent_by','sent_to','copied_to'),
                    'Event':('organized_by','invites'),'Document':('authored_by',)}
                if category not in predicates or edge['predicate'] not in predicates[category]:continue
                for prefix in rawpaths:
                    add(category,edge['source_id'],[prefix,edge])
                    for lnk in self._relations(edge['source_id']):
                        if lnk['source_id']==edge['source_id'] and lnk['target_type']=='LineOfEffort' and lnk['predicate']=='associated_with':
                            add('LineOfEffort',lnk['target_id'],[prefix,edge,lnk])
        # A document issuer is direct typed *derived evidence*, not an invented RDF predicate.
        for r in self.db.execute("SELECT source_id,result_json FROM derived_evidence WHERE producer='DocumentIssuer'"):
            doc=self.engine.store.row(r['source_id'])
            if doc is None or not doc['active']:continue
            material=_json(r['result_json'])
            for c in material.get('claims',[]):
                if c.get('record_id') in current:
                    add('Document',r['source_id'],[{'authority':'core_derived_evidence',
                        'producer':'DocumentIssuer','document_id':r['source_id'],
                        'core_org_id':c['record_id'],'field_path':'metadata.issuing_organization',
                        'source_sha256':doc['source_sha']}])
        size=params.get('rollup_page_size',100);offset=params.get('rollup_offset',0)
        if type(size) is not int or not 1<=size<=200 or type(offset) is not int or offset<0:
            raise KMInvalid('INVALID_VALUE','Invalid bounded rollup window')
        sections={}
        for group,memberships in groups.items():
            values=[memberships[k] for k in sorted(memberships)]
            sections[group]={'items':values[offset:offset+size], 'total_count':len(values),
                'next_offset':offset+size if offset+size<len(values) else None}
        return {'sections':sections,'rollup_page_size':size,'rollup_offset':offset,
                'core_support':'present' if current else 'no_current_core_support'}

    def _configured_view(self,as_of,view_id,spec,params):
        """One custom-query authority for saved views and unsaved previews.

        A view selects records; it does not assert new properties or relations.
        Only the requested page is decorated. Query options are fingerprinted
        with the existing snapshot cursor and are never saved by a preview.
        """
        spec=dict(spec)
        if 'sorts' in params:spec['sorts']=params['sorts']
        kind=spec['record_type']
        if is_type_id(kind):self._workspace_type_or_error(kind)
        try:dependencies=self._validate_view_spec(spec)
        except KMInvalid as exc:
            if view_id and exc.code in ('UNKNOWN_PROPERTY','CONFIGURATION_REQUIRED'):
                return {'view_id':view_id,'state':'configuration_required','reason':'A referenced Workspace definition is unavailable','rows':[],'next_cursor':None}
            raise
        computed=[]
        for field in sorted(dependencies):
            definition=self.property_definition(field)
            if definition is None or definition.get('status')=='disabled' and spec.get('unknown_policy')!='include':
                return {'view_id':view_id,'state':'configuration_required',
                        'reason':'Dependent Workspace property unavailable','rows':[],'next_cursor':None}
            if definition['mode']=='computed':
                if self.custom_rule_for(field) is None and spec.get('unknown_policy')!='include':
                    return {'view_id':view_id,'state':'configuration_required',
                            'reason':'Dependent computed Workspace property not enabled','rows':[],'next_cursor':None}
                computed.append(field)
        # Retain existing legacy query behavior. New drafts use the typed
        # expression/multi-sort path, which also backs the exact draft preview.
        if not is_type_id(kind) and 'sorts' not in spec and 'filter_expression' not in spec and not spec.get('group_by_fields') and not (spec.get('search') and params.get('search')):
            if computed:
                evaluator=Evaluator(self,as_of)
                for row in self.db.execute('SELECT * FROM records WHERE record_type=? AND active=1',(kind,)):
                    evaluator.cache.clear()
                    if any(evaluator.evaluate(row,f)['state'] not in ('evaluated','unset') for f in computed):
                        return {'view_id':view_id,'state':'source_stale','reason':'Some classification inputs are unavailable',
                                'rows':[],'next_cursor':None}
            query={**params,'filters':spec.get('filters',[]),'search':spec.get('search') or params.get('search'),
                   'sort':spec.get('sort','updated_at'),
                   'sort_direction':spec.get('sort_direction','desc'),'group_by':spec.get('group_by')}
            if 'include_inactive' in spec:query['include_inactive']=spec['include_inactive']
            return {'view_id':view_id,'state':'available',**self._listed(kind,query,as_of)}
        return self._configured_view_page(as_of,view_id,spec,params,computed)

    def _view_reference_summaries(self,rows,fields):
        """Readable typed links for this page, using the existing batched reader.

        Only label-bearing facts are returned; no history, rules, correspondence
        or archive read is needed. Unresolved overflow remains an explicit ID.
        """
        references={};per_row={}
        for row in rows:
            ids=[]
            for field,result in row.get('workspace_properties',{}).items():
                info=fields.get(field,{})
                if info.get('value_type') not in ('record_ref','record_ref_set') or result.get('state') not in ('evaluated','not_evaluable') or result.get('value') is None:continue
                kind=info.get('ref_type')
                if not kind:continue
                value=result.get('value');values=value if isinstance(value,list) else [value]
                for value in values:
                    rid=value.get('record_id') if isinstance(value,dict) else value
                    if not isinstance(rid,str):continue
                    ids.append(rid)
                    if rid in references or len(references)<200:references[rid]=kind
            per_row[row['record_id']]=ids
        summaries=self._reference_records(references) if references else {}
        for ref in summaries.values():
            props=ref.get('properties',{});added=ref.get('additional',{})
            compact={key:props[key] for key in ('subject','summary','displayName','name') if key in props}
            for section,key in (('metadata','title'),('source','filename')):
                if isinstance(props.get(section),dict) and key in props[section]:compact[section]={key:props[section][key]}
            ref['properties']=compact;ref['additional']={key:added[key] for key in ('display_label','display_title') if key in added}
        for row in rows:
            ids=per_row[row['record_id']]
            if ids:
                row['view_references']={rid:summaries[rid] for rid in dict.fromkeys(ids) if rid in summaries}
                row['view_references_truncated']=any(rid not in summaries for rid in ids)

    def _configured_view_page(self,as_of,view_id,spec,params,computed):
        """Select messages first; group selected identities; sort globally; page.

        A grouped row is represented by the newest eligible message by default.
        Unknown matches are retained only by an explicit view policy. They never
        become known matches or eligible targets for an automatic disposition.
        """
        kind=spec['record_type'];workspace=is_type_id(kind);fields={x['field_id']:x for x in self._view_field_catalog(kind)}
        if kind is None:fields={field:{'value_type':'timestamp' if field=='updated_at' else 'text'} for field in ('record_id','record_type','scope','updated_at')}
        sorts=spec.get('sorts') or [{'field':spec.get('sort','updated_at'),'direction':spec.get('sort_direction','desc')}]
        group=spec.get('group_by');group=None if group in (None,'none') else group
        compound=spec.get('group_by_fields',[])
        representative=spec.get('representative_sort') or {'field':'updated_at','direction':'desc'}
        expression=spec.get('filter_expression')
        if expression is None and spec.get('filters'):expression={'all':spec['filters']}
        include=spec.get('include_inactive',False);include_unknown=spec.get('unknown_policy','exclude')=='include'
        query={'specification':spec,'page_size':params.get('page_size',50),'search':params.get('search','')}
        if params.get('cursor'):query['cursor']=params['cursor']
        query_id='configured_view:'+str(view_id or 'draft')
        size,offset,_,_=self._page_request(query,as_of,query_id)
        evaluator=Evaluator(self,as_of);evaluator.include_reply_evidence=kind=='Email'
        zone=ZoneInfo(self.settings()['workspace_timezone'])
        saved_search=spec.get('search','').strip().casefold()
        current_search=params.get('search','')
        if not isinstance(current_search,str) or len(current_search)>500 or any(ord(c)<32 for c in current_search):
            raise KMInvalid('INVALID_VALUE','Invalid view search')
        current_search=current_search.strip().casefold()
        # Supported Email rule/sort/group fields do not consume body_text.
        # Search still examines the complete stored JSON. Full records are
        # fetched after paging, so readers and selected/excluded output retain
        # their original contents. Avoid decoding every body just to order mail.
        omit_body=kind=='Email' and not saved_search and not current_search and 'body_text' not in fields
        if kind is not None and not workspace and expression is None and not group and not compound and not include and not saved_search and not current_search and len(sorts)==1 and sorts[0]['field'] in ('updated_at','record_id'):
            field=sorts[0]['field'];direction='DESC' if sorts[0]['direction']=='desc' else 'ASC'
            total=self.db.execute('SELECT COUNT(*) FROM records WHERE record_type=? AND active=1',(kind,)).fetchone()[0]
            _,_,metadata=self._page_window(query,as_of,query_id,total)
            raw=list(self.db.execute('SELECT * FROM records WHERE record_type=? AND active=1 ORDER BY '+field+' '+direction+', record_id ASC LIMIT ? OFFSET ?',(kind,size,offset)))
            evaluator.prepare_manual_page(kind,raw)
            rows=[self._decorate(row,evaluator) for row in raw]
            self._view_reference_summaries(rows,fields)
            return {'view_id':view_id,'state':'available','kind':'configured_table','rows':rows,**metadata,
                    'group_by':None,'group_counts':{},'group_labels':{},'groups_truncated':False,'unknown_count':0,
                    'matched_record_count':total,'excluded_count':0,'excluded_examples':[],
                    'query_options':{'sorts':sorts,'group_by':None,'group_direction':spec.get('group_direction','asc'),
                                     'include_inactive':False,'workspace_timezone':str(zone),'missing_values':'last'}}
        evaluator.prepare_manual_kind(kind)
        unknown=0;stale=False;matched=0;excluded=0;group_counts={};group_labels={};excluded_examples=[]
        group_rows={};groups_truncated=False
        directions=([spec.get('group_direction','asc')] if group else [])+[item['direction'] for item in sorts]

        def scalar(row,field,*,grouping=False):
            info=fields.get(field,{'value_type':'text'});state='evaluated'
            if field.startswith('ws:'):
                if not row['active'] and info.get('mode')=='computed':return (1,'')
                output=evaluator.evaluate(row,field);value=output.get('value');state=output['state']
            else:value=evaluator.field_value(row,field)
            if state!='evaluated' or value is None:return (1,'')
            typ=info['value_type']
            if typ=='timestamp':
                stamp=_safe_stamp(value)
                if stamp is None:return (1,'')
                value=stamp.astimezone(zone).date().isoformat() if grouping else stamp.timestamp()
            elif typ=='date':
                try:value=dt.date.fromisoformat(value).isoformat()
                except (TypeError,ValueError):return (1,'')
            elif typ=='boolean':
                if type(value) is not bool:return (1,'')
                value=int(value)
            elif typ=='email':
                from .evaluation import _email
                value=_email(value)
                if value is None:return (1,'')
            elif not isinstance(value,str):return (1,'')
            return (0,value.casefold() if isinstance(value,str) else value)

        def compare_values(left,right,ordering):
            for av,bv,direction in zip(left,right,ordering):
                if av[0]!=bv[0]:return -1 if av[0]<bv[0] else 1
                if av[1]!=bv[1]:
                    order=-1 if av[1]<bv[1] else 1
                    return -order if direction=='desc' else order
            return 0

        def compare(left,right):
            return compare_values(left['sort_key'],right['sort_key'],directions) or ((left['id']>right['id'])-(left['id']<right['id']))

        def candidates():
            nonlocal unknown,stale,matched,excluded,groups_truncated
            columns=('record_id,record_type,scope,identity_basis,identity_key,fingerprint,source_sha,active,properties_json,'
                     "json_remove(added_json,'$.body_text') AS added_json,updated_at") if omit_body else '*'
            sql=('SELECT * FROM ws_records WHERE type_id=?' if workspace else 'SELECT '+columns+' FROM records WHERE record_type=?' if kind else 'SELECT * FROM records WHERE 1=1')+('' if include else ' AND active=1')+' ORDER BY record_id'
            for stored in self.db.execute(sql,(kind,) if kind else ()):
                if workspace:
                    from .workspace_types import _record_row
                    row=_record_row(stored)
                else:row=stored
                evaluator.cache.clear();evaluator._unknown=None;evaluator._evidence=[]
                if saved_search or current_search:
                    words=(row['record_id']+' '+row['properties_json']+' '+row['added_json']).casefold()
                    if (saved_search and saved_search not in words) or (current_search and current_search not in words):continue
                if not row['active']:
                    for field in computed:evaluator.cache[(row['record_id'],field)]={'state':'not_evaluable','value':None}
                if not include_unknown and row['active'] and any(evaluator.evaluate(row,f)['state'] not in ('evaluated','unset') for f in computed):stale=True
                evaluator._unknown=None
                answer=evaluator.expression(row,expression) if expression is not None else True
                if getattr(evaluator,'_unknown',None) and evaluator._unknown[0]=='source_stale':stale=True
                if answer is None:unknown+=1
                if answer is False or answer is None and not include_unknown:
                    excluded+=1
                    if len(excluded_examples)<10:
                        reason=evaluator._unknown[1] if answer is None and evaluator._unknown else 'Does not match the saved conditions.'
                        excluded_row=self.engine.store.row(row['record_id']) if omit_body else row
                        excluded_examples.append({'record':({'record_id':row['record_id'],'record_type':kind,'properties':_json(row['properties_json'])} if workspace else self._decorate(excluded_row)),'reason':reason,'state':'unknown' if answer is None else 'excluded'})
                    continue
                matched+=1
                match={'state':'matched' if answer is True else 'unknown',
                       'reason':'Matches this view.' if answer is True else evaluator._unknown[1] if evaluator._unknown else 'Some evidence or configured values are unavailable.',
                       'evidence':list(evaluator._evidence)}
                keys=[];gkey=glabel=None
                if group:
                    gv=scalar(row,group,grouping=True);keys.append(gv)
                    gkey=json.dumps(gv,ensure_ascii=False,separators=(',',':'))
                    glabel='Not supplied / unavailable' if gv[0] else str(gv[1])
                    if gkey in group_counts:group_counts[gkey]+=1
                    elif len(group_counts)<200:group_counts[gkey]=1;group_labels[gkey]=glabel
                    else:groups_truncated=True
                keys.extend(scalar(row,item['field']) for item in sorts)
                item={'sort_key':tuple(keys),'id':row['record_id'],'gkey':gkey,'glabel':glabel,'match':match}
                if not compound:
                    yield item;continue
                # Missing identities stay independent. Grouping can never invent
                # a conversation identity or reintroduce a filtered message.
                group_values=tuple(evaluator.field_value(row,field) for field in compound)
                ckey=group_values if all(value is not None and value!='' for value in group_values) else (*group_values,row['record_id'])
                rep=scalar(row,representative['field'])
                current=group_rows.get(ckey)
                if current is None:
                    current={**item,'rep':rep,'known':[],'uncertain':[],'group_values':group_values};group_rows[ckey]=current
                else:
                    prefer=(match['state']=='matched' and current['match']['state']=='unknown') or (
                        match['state']==current['match']['state'] and (compare_values([rep],[current['rep']],[representative['direction']])<0 or rep==current['rep'] and item['id']>current['id']))
                    if prefer:current.update({**item,'rep':rep})
                current['known' if answer is True else 'uncertain'].append(row['record_id'])
            if compound:
                yield from group_rows.values()
        # Keep compact identity/sort keys, never decorated bodies, before paging.
        # The complete order is global and deterministic; rendering stays bounded.
        after=_dec(params['cursor']).get('after') if params.get('cursor') else None
        if after is not None:
            if not isinstance(after,dict) or set(after)!={'sort_key','id'} or not isinstance(after['id'],str):
                raise KMInvalid('INVALID_VALUE','Invalid ordered cursor')
            selected=heapq.nsmallest(size,(item for item in candidates() if compare(item,after)>0),key=cmp_to_key(compare))
        else:
            selected=heapq.nsmallest(offset+size,candidates(),key=cmp_to_key(compare))[offset:offset+size]
        if stale and not include_unknown:
            return {'view_id':view_id,'state':'source_stale','reason':'Some classification inputs are unavailable; no incomplete match set is presented as current.',
                    'rows':[],'next_cursor':None,'unknown_count':unknown,'excluded_count':excluded,'excluded_examples':excluded_examples}
        total=len(group_rows) if compound else matched
        _,_,metadata=self._page_window(query,as_of,query_id,total)
        if metadata.get('next_cursor') and selected:
            cursor=_dec(metadata['next_cursor']);cursor['after']={key:selected[-1][key] for key in ('sort_key','id')}
            metadata['next_cursor']=_enc(cursor)
        if workspace:
            ids=[item['id'] for item in selected];marks=','.join('?' for _ in ids)
            records={row['record_id']:row for row in self.db.execute('SELECT * FROM ws_records WHERE record_id IN ('+marks+')',ids)} if ids else {}
        else:records=self._records_by_id([item['id'] for item in selected])
        rows=[]
        for item in selected:
            evaluator.cache.clear();raw=records[item['id']]
            row=self._workspace_decorate(raw,as_of,evaluator) if workspace else self._decorate(raw,evaluator)
            row['view_match']=item['match'];row['display_record_id']=item['id']
            known=item.get('known',[item['id']] if item['match']['state']=='matched' else [])
            uncertain=item.get('uncertain',[item['id']] if item['match']['state']=='unknown' else [])
            ids=known+uncertain
            row.update({'anchor_record_ids':ids,'eligible_record_ids':known,'uncertain_anchor_record_ids':uncertain,
                        'group_record_ids':ids,'group_count':len(ids),'message_count':len(ids),
                        'state':'candidate' if known else 'uncertain','reason':item['match']['reason']})
            if compound:
                row['group_identity']=dict(zip(compound,item['group_values']))
                row['conversation_key']={**row['group_identity'],'standalone_record_id':item['id'] if any(value in (None,'') for value in item['group_values']) else None}
            if group:row['view_group']={'key':item['gkey'],'label':item['glabel']}
            if not raw['active']:
                row['workspace_properties']={p:evaluator.evaluate(_record_row(raw) if workspace else raw,p) for p in evaluator.properties_by_kind.get(kind,[]) if evaluator._definitions[p]['mode']=='manual'}
            rows.append(row)
        self._view_reference_summaries(rows,fields)
        result={'view_id':view_id,'state':'available','kind':'configured_table','rows':rows,**metadata,
                'group_by':group,'group_by_fields':compound,'group_counts':group_counts,'group_labels':group_labels,'groups_truncated':groups_truncated,
                'matched_record_count':matched,'excluded_count':excluded,'excluded_examples':excluded_examples,
                'unknown_count':unknown,'uncertain_count':sum(not item['known'] for item in group_rows.values()) if compound else unknown if include_unknown else 0,
                'candidate_count':sum(bool(item['known']) for item in group_rows.values()) if compound else matched-(unknown if include_unknown else 0),
                'known_match_count':matched-(unknown if include_unknown else 0),
                'query_options':{'sorts':sorts,'group_by':group,'group_by_fields':compound,'representative_sort':representative,
                    'group_direction':spec.get('group_direction','asc'),'include_inactive':include,'workspace_timezone':str(zone),'missing_values':'last'}}
        if kind=='Email':
            from .mail_tasks import identity
            result.update({'identity':identity(self),'freshness':'Based on saved mail; newer mail may not be included.',
                           'coverage':self.coverage(),'remote_freshness':'unknown'})
        return result

    def _views(self,as_of,view_id,params):
        if not isinstance(view_id,str) or not view_id:
            raise KMInvalid('INVALID_TYPE','View ID must be a nonempty string')
        if view_id not in VIEWS:
            r=self._definition(view_id)
            if not r or r['definition_kind']!='view':
                raise KMInvalid('UNKNOWN_VIEW','Unknown custom view')
            if r['state']!='enabled':
                return {'view_id':view_id,'state':'disabled','reason':'Saved view is disabled','rows':[],'next_cursor':None}
            spec=_json(r['specification_json'])
            return self._configured_view(as_of,view_id,spec,params)
        v=VIEWS[view_id];kind=v['owner_type']
        ov=self._definition(view_id)
        if ov and ov['state']!='enabled':return {'view_id':view_id,'state':'disabled','rows':[], 'next_cursor':None}
        if view_id in VIEW_PRESET:
            # The view is *not* a preset evaluator. It reads a user-configured
            # computed Workspace property, whether seeded from a template or
            # created from scratch. Multiple variants require explicit binding.
            template_id=VIEW_PRESET[view_id]
            candidates=self.installed_template_properties(template_id)
            configured=params.get('property_id')
            if configured is None:
                configured=candidates[0] if len(candidates)==1 else None
            definition=self.property_definition(configured) if configured else None
            if not definition or definition['owner_type']!=kind or definition['mode']!='computed' or self.custom_rule_for(configured) is None:
                return {'view_id':view_id,'state':'configuration_required',
                        'reason':'Select an enabled computed Workspace property',
                        'installed_property_ids':candidates,'rows':[], 'next_cursor':None}
            if len(candidates)>1 and 'property_id' not in params:
                return {'view_id':view_id,'state':'configuration_required',
                        'reason':'Select a Workspace classification variant',
                        'installed_property_ids':candidates,'rows':[], 'next_cursor':None}
            evaluator=Evaluator(self,as_of);results=[];unavailable=[]
            for row in self._record_rows(kind):
                result=evaluator.evaluate(row,configured)
                if result['state'] not in ('evaluated','unset'):unavailable.append(result['state'])
                if result['state']=='evaluated' and result['value'] is True:
                    results.append(self._decorate(row,evaluator))
            if unavailable:
                disabled='disabled' in unavailable
                return {'view_id':view_id,'state':'configuration_required' if disabled else 'source_stale',
                        'reason':('Classification prerequisite is disabled' if disabled else
                                  'Some classification inputs are unavailable'),
                        'rows':[],'next_cursor':None,'unavailable_count':len(unavailable)}
            return {'view_id':view_id,'state':'available','property_id':configured,
                    **self._page(results,params,as_of,view_id)}
        ending=view_id.rsplit(':',1)[-1]
        if kind=='Event' and ending in ('today','this_week','this_month'):
            matched,unresolved=self._event_rows(ending,as_of)
            return {'view_id':view_id,'state':'available','unresolved_time_count':unresolved,
                    'recurrence_coverage':'stored_events_only','source_coverage':self.coverage('Event'),
                    **self._page(matched,params,as_of,view_id)}
        if kind=='Organization':
            if ending=='raw':rows=self._raw_organizations(params.get('include_inactive',True))
            else:
                rows=self._organization_effective_rows(params.get('include_inactive',False))
                if ending=='command_hierarchy':rows=[{**r,'parent_command':r['properties'].get('ws:parent_command')} for r in rows]
                if ending=='enterprise_map':rows=[{**r,'enterprise':r['properties'].get('ws:enterprise')} for r in rows]
                if ending=='pocs':rows=[r for r in rows if r['effective_kind']=='workspace_overlay' and r['properties'].get('ws:poc_people')]
            return {'view_id':view_id,'state':'available','kind':ending,
                    **self._page(rows,params,as_of,view_id)}
        if kind=='Email' and ending=='conversations':
            groups=defaultdict(list);ungrouped=[]
            for r in self._record_rows('Email'):
                p=_json(r['properties_json']);k=(p.get('mailbox_id'),p.get('conversation_id'))
                if all(isinstance(x,str) and x.strip() for x in k):groups[k].append(r)
                else:ungrouped.append(self._decorate(r))
            selected_mailbox=params.get('mailbox_id');selected_conversation=params.get('conversation_id')
            selected=selected_mailbox is not None or selected_conversation is not None
            if selected and (not isinstance(selected_mailbox,str) or not selected_mailbox or not isinstance(selected_conversation,str) or not selected_conversation):
                raise KMInvalid('INVALID_VALUE','Select an exact mailbox and conversation identity together')
            rows=[];message_page=None
            for k,arr in groups.items():
                if selected and k!=(selected_mailbox,selected_conversation):continue
                def stamp(r):
                    p=_json(r['properties_json']);ad=_json(r['added_json']);return (_safe_stamp(p.get('sent_at_utc' if ad.get('direction')=='Outgoing' else 'received_at_utc')) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),r['record_id'])
                seq=sorted(arr,key=stamp)
                latest=next((r for r in reversed(seq) if stamp(r)[0]>dt.datetime.min.replace(tzinfo=dt.timezone.utc)),None)
                last=None
                if latest is not None:
                    moment,_=stamp(latest)
                    direction=_json(latest['added_json']).get('direction')
                    last={'email_id':latest['record_id'],'direction':direction,'timestamp':moment.isoformat(),
                        'verified_path':[{'type':'source_conversation_identity','mailbox_id':k[0],
                                          'conversation_id':k[1],'email_id':latest['record_id']}],
                        'authority':'source_scoped_group','coverage':self.coverage('Email')}
                if selected:
                    size,offset,message_page=self._page_window(params,as_of,view_id+':messages',len(seq))
                    shown=seq[offset:offset+size]
                else:
                    # The GUI requests bounded previews; legacy callers retain
                    # their existing complete response unless they opt in.
                    preview=params.get('message_preview_limit')
                    if preview is not None and (type(preview) is not int or not 1<=preview<=200):raise KMInvalid('INVALID_VALUE','Invalid conversation preview limit')
                    shown=seq if preview is None else seq[:preview]
                rows.append({'mailbox_id':k[0],'conversation_id':k[1],
                             'messages':[self._decorate(r) for r in shown],'count':len(seq),'authority':'source_scoped_group',
                             'messages_truncated':len(shown)<len(seq),'last_correspondence':last})
            rows.sort(key=lambda v:(v['mailbox_id'],v['conversation_id']))
            if selected:
                return {'view_id':view_id,'state':'available','message_page':True,'rows':rows,
                    **(message_page or self._page([],params,as_of,view_id+':messages'))}
            return {'view_id':view_id,'state':'available','ungrouped_count':len(ungrouped),
                    **self._page(rows,params,as_of,view_id)}
        if kind=='LineOfEffort' and ending=='dashboard':
            return self._loe_dashboard(params,as_of)
        if kind=='Email' and ending in ('inbox','sent'):
            if params.get('sorts'):
                spec={'label':v['name'],'record_type':'Email','sorts':params['sorts'],
                      'filter_expression':{'all':[
                          {'field':'source_folder','operator':'equals','value':'Inbox' if ending=='inbox' else 'Sent'},
                          {'field':'direction','operator':'equals','value':'Incoming' if ending=='inbox' else 'Outgoing'}]}}
                return self._configured_view(as_of,view_id,spec,params)
            expected=ending
            rows=[self._decorate(r) for r in self._record_rows('Email') if str(_json(r['properties_json']).get('source_folder','')).casefold()==expected
                  and _json(r['added_json']).get('direction','').casefold()==('incoming' if ending=='inbox' else 'outgoing')]
            return {'view_id':view_id,'state':'available',**self._page(rows,params,as_of,view_id)}
        if kind=='Person' and ending=='key_contacts':
            configured=params.get('property_id')
            available=self.installed_template_properties('WS-PERSON-KEY-CONTACT-001')
            if configured is None and len(available)==1:configured=available[0]
            definition=self.property_definition(configured) if configured else None
            if not definition or definition['owner_type']!='Person' or definition['value_type']!='boolean' or definition['mode']!='manual' or definition['status']!='enabled':
                return {'view_id':view_id,'state':'configuration_required',
                        'reason':'Select an enabled Key Contact property','rows':[], 'next_cursor':None}
            rows=[self._decorate(r) for r in self._record_rows('Person') if self.manual_value('Person',r['record_id'],configured) is True]
            return {'view_id':view_id,'state':'available','property_id':configured,**self._page(rows,params,as_of,view_id)}
        if kind=='Person' and ending in ('by_org','stakeholder_map'):
            role_field=self._bound_manual('Person','WS-PERSON-ROLE-001',params,'role','text')
            key_field=self._bound_manual('Person','WS-PERSON-KEY-CONTACT-001',params,'key_contact','boolean')
            people=[];groups={}
            org_map=self._org_mapping()
            for r in self._record_rows('Person'):
                d=self._decorate(r)
                edges=[e for e in self._relations(r['record_id'])
                    if e['source_id']==r['record_id'] and e['predicate'] in ('affiliated_with','member_of')]
                d['verified_organization_edges']=edges
                d['effective_organization_paths']=[{'effective_org_id':org_map.get(e['target_id'],e['target_id']),
                    'core_org_id':e['target_id'],'core_edge':e,'authority':'core_bounded_traversal'} for e in edges]
                d['effective_organization_ids']=sorted({x['effective_org_id'] for x in d['effective_organization_paths']})
                d['workspace_role']=self.manual_value('Person',r['record_id'],role_field) if role_field else None
                d['key_contact']=self.manual_value('Person',r['record_id'],key_field) if key_field else None
                people.append(d)
                for effective in d['effective_organization_ids'] or ['unmatched']:
                    groups.setdefault(effective,[]).append(r['record_id'])
            return {'view_id':view_id,'state':'available','kind':ending,
                    'workspace_bindings':{'role':role_field,'key_contact':key_field},
                    'effective_groups':{k:sorted(set(v)) for k,v in sorted(groups.items())},
                    **self._page(people,params,as_of,view_id)}
        if kind=='Document' and ending in ('by_author','by_loe','concept_map'):
            rows=[]
            for r in self._record_rows('Document'):
                d=self._decorate(r);rels=self._relations(r['record_id'])
                pred=('authored_by',) if ending=='by_author' else ('associated_with',) if ending=='by_loe' else ('authored_by','associated_with','attached_to')
                selected=[e for e in rels if e['predicate'] in pred]
                if selected or ending=='concept_map':
                    d['verified_connections']=selected
                    if ending=='concept_map':
                        d['source_heading_anchors']=self._document_anchors(d['properties'])
                    rows.append(d)
            return {'view_id':view_id,'state':'available','kind':ending,**self._page(rows,params,as_of,view_id)}
        # Built-in All/Detailed/LOE Overview: reuse the one record-list query.
        result=self._listed(kind,params,as_of,render='detailed' if ending=='detailed' else 'standard')
        return {'view_id':view_id,'state':'available',**result}

    @staticmethod
    def _document_anchors(props):
        result=[]
        content=props.get('content',{}) if isinstance(props,dict) else {}
        for idx,unit in enumerate(content.get('units',[]) if isinstance(content,dict) else []):
            for n,element in enumerate(unit.get('elements',[]) if isinstance(unit,dict) else []):
                if isinstance(element,dict) and element.get('type')=='heading':
                    result.append({'path':f'content.units[{idx}].elements[{n}]','text':str(element.get('text',''))[:240],
                                   'authority':'source_document_structure'})
        return result

    def _loe_dashboard(self,params,as_of):
        rid=params.get('loe_id') or params.get('record_id')
        if not rid:raise KMInvalid('INVALID_VALUE','LOE ID required for dashboard')
        row=self._assert_target('LineOfEffort',rid,active=False)
        # Optional half-open UTC interval. All-time is the default.
        start_input=params.get('start_utc');end_input=params.get('end_utc')
        start=_safe_stamp(start_input) if start_input is not None else None
        end=_safe_stamp(end_input) if end_input is not None else None
        if start_input is not None and start is None or end_input is not None and end is None or start and end and start>=end:
            raise KMInvalid('INVALID_VALUE','Invalid LOE dashboard UTC date window')
        included={'Events':{},'Organizations':{},'Roles':{},'People':{},'Documents':{},'Email Traffic':{}}
        involved=[];unresolved_time=0
        for edge in self._relationships_for('LineOfEffort',rid)['edges']:
            if edge['target_id']!=rid or (edge['predicate']!='associated_with' and edge.get('authority')!='workspace_manual'):continue
            source=self.engine.store.row(edge['source_id'])
            if not source or not source['active']:continue
            group={'Email':'Email Traffic','Event':'Events','Document':'Documents','Person':'People','Organization':'Organizations'}.get(source['record_type'])
            if group:
                if start is not None or end is not None:
                    p=_json(source['properties_json']);a=_json(source['added_json'])
                    if group=='Email Traffic':
                        text=p.get('sent_at_utc') if a.get('direction')=='Outgoing' else p.get('received_at_utc')
                    elif group=='Events':text=a.get('start_utc') or p.get('dtstart')
                    else:
                        origin=p.get('source') if isinstance(p.get('source'),dict) else {}
                        text=origin.get('modified_utc') or origin.get('created_utc')
                    moment=_safe_stamp(text)
                    if moment is None:
                        unresolved_time+=1;continue
                    if start is not None and moment<start or end is not None and moment>=end:
                        continue
                included[group].setdefault(source['record_id'],{'record_id':source['record_id'],'record_type':source['record_type'],'paths':[]})['paths'].append([edge])
                involved.append(source['record_id'])
        for source in involved:
            for edge in self._relations(source):
                if edge['source_id']!=source or edge['target_type']!='Person':continue
                if edge['predicate'] in ('sent_by','sent_to','copied_to','invites','organized_by','authored_by'):
                    pid=edge['target_id']
                    included['People'].setdefault(pid,{'record_id':pid,'record_type':'Person','paths':[]})['paths'].append([edge])
        # A linked Document may explicitly produce an Organization identity.
        # Display the source producer path without inventing a Core RDF edge.
        for document_id in sorted(included['Documents']):
            doc=self.engine.store.row(document_id)
            if not doc or not doc['active']:continue
            for ev in self.db.execute("SELECT result_json FROM derived_evidence WHERE producer='DocumentIssuer' AND source_id=?",(document_id,)):
                producer=_json(ev['result_json'])
                for claim in producer.get('claims',[]):
                    org_id=claim.get('record_id')
                    org=self.engine.store.row(org_id)
                    if not org or not org['active'] or org['record_type']!='Organization':continue
                    effective=self._org_mapping().get(org_id,org_id)
                    item=included['Organizations'].setdefault(effective,
                        {'record_id':effective,'core_org_ids':[], 'paths':[]})
                    item['core_org_ids'].append(org_id)
                    item['paths'].append([{'authority':'core_source_producer',
                        'producer':'DocumentIssuer','document_id':document_id,
                        'core_org_id':org_id,'source_sha256':doc['source_sha'],
                        'field_path':'metadata.issuing_organization'}])
        manual_templates={'priority':('WS-LOE-PRIORITY-001','enum'),
             'effort_level':('WS-LOE-EFFORT-001','enum'),
             'work_status':('WS-LOE-STATUS-001','enum'),
             'target_date':('WS-LOE-TARGET-001','date'),
             'responsible_person_id':('WS-LOE-RESPONSIBLE-001','record_ref')}
        bindings={key:self._bound_manual('LineOfEffort',template,params,key,kind)
            for key,(template,kind) in manual_templates.items()}
        role_field=self._bound_manual('Person','WS-PERSON-ROLE-001',params,'role','text')
        responsible=bindings['responsible_person_id']
        person=self.manual_value('LineOfEffort',rid,responsible) if responsible else None
        if person:
            included['People'].setdefault(person['record_id'],{'record_id':person['record_id'],'record_type':'Person','paths':[]})['paths'].append([{'authority':'workspace_manual','property':responsible}])
        for pid in list(included['People']):
            role=self.manual_value('Person',pid,role_field) if role_field else None
            if role:included['Roles'].setdefault(role,{'role':role,'person_ids':[]})['person_ids'].append(pid)
            for edge in self._relations(pid):
                if edge['source_id']!=pid or edge['target_type']!='Organization' or edge['predicate'] not in ('affiliated_with','member_of'):continue
                oid=edge['target_id'];effective=self._org_mapping().get(oid,oid)
                included['Organizations'].setdefault(effective,{'record_id':effective,'core_org_ids':[], 'paths':[]})['paths'].append([edge])
                included['Organizations'][effective]['core_org_ids'].append(oid)
        for key in included:
            if key=='Roles':continue
            for entry in included[key].values():
                if 'core_org_ids' in entry:entry['core_org_ids']=sorted(set(entry['core_org_ids']))
        sections={k:list(v.values()) for k,v in included.items()}
        paging={}
        if params.get('bounded_sections'):
            counts={key:len(values) for key,values in sections.items()}
            size,offset,paging=self._page_window(params,as_of,'view:loe:dashboard:sections',max(counts.values(),default=0))
            sections={key:values[offset:offset+size] for key,values in sections.items()}
            paging.update(total_count=sum(counts.values()),section_counts=counts)
        return {'view_id':'view:loe:dashboard','state':'available','loe_id':rid,**paging,
           'core':self._decorate(row),'workspace_properties':{
                key:self.manual_value('LineOfEffort',rid,p) if p else None for key,p in bindings.items()},
           'workspace_bindings':{**bindings,'role':role_field},
           'sections':sections,
           'applied_window':{'start_utc':start_input,'end_utc':end_input},
           'unresolved_time_count':unresolved_time,'authority':'evidence_paths_only'}
