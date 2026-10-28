"""Bounded deterministic Workspace evaluator over current Core evidence.
No persistent computed-value cache, user scripts, or invented Core claims.
"""
from __future__ import annotations
import datetime as dt
import json
from zoneinfo import ZoneInfo
from .catalog import OPERATORS,RULE_LIMITS,email_address,patterns
from .reply_evidence import context as reply_context, address as _email, source_time

class KMInvalid(ValueError):
    def __init__(self,code,message):super().__init__(message);self.code=code

def _load(s):return json.loads(s) if isinstance(s,str) else s

def _datetime(value):
    try:
        if not isinstance(value,str):return None
        d=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return d if d.tzinfo is not None else None
    except (ValueError,OverflowError):return None

def _date_age(ts,as_of,tz):
    d=_datetime(ts)
    if d is None:return None
    return (as_of.astimezone(ZoneInfo(tz)).date()-d.astimezone(ZoneInfo(tz)).date()).days


class Evaluator:
    def __init__(self,service,as_of):
        self.s=service;self.as_of=as_of;self.db=service.db
        self.settings=service.settings()
        self._snapshot_revision=service.revision();self._snapshot_epoch=service.epoch()
        self._coverage_snapshot=service.coverage();self._reply_index=None
        self._decoded_id=None;self._decoded=None;self.include_reply_evidence=False
        self.cache={};self.calls=set()
        # Read definition IDs once per evaluation snapshot, not once per record row.
        self.properties_by_kind={};self._definitions={};self._manual_rows=None
        for owner,prop,spec,state in self.db.execute(
                "SELECT owner_type,definition_id,specification_json,state FROM ws_definitions WHERE definition_kind='property' AND state!='retired'"):
            self.properties_by_kind.setdefault(owner,[]).append(prop)
            self._definitions[prop]={**_load(spec),'status':state,'origin':'custom'}
        self._rules={_load(r['specification_json'])['output_property_id']:_load(r['specification_json']) for r in self.db.execute("SELECT specification_json FROM ws_definitions WHERE definition_kind='rule' AND state='enabled'")}

    def data(self,row):
        if self._decoded_id!=row['record_id']:
            self._decoded_id=row['record_id'];self._decoded=(_load(row['properties_json']),_load(row['added_json']))
        return self._decoded

    def prepare_manual_kind(self,kind):
        self._manual_rows={(rid,p):_load(value) for rid,p,value in self.db.execute('SELECT target_id,property_id,value_json FROM ws_values'+(' WHERE target_kind=?' if kind else ''),(kind,) if kind else ())}
        self._prepared_kind=kind;self._prepared_ids=None

    def prepare_manual_page(self,kind,rows):
        # Batch only the bounded page being decorated in this read snapshot.
        ids=list(dict.fromkeys(r['record_id'] for r in rows))
        self._manual_rows={}
        for start in range(0,len(ids),200):
            chunk=ids[start:start+200]
            if not chunk:continue
            marks=','.join('?' for _ in chunk)
            for rid,p,value in self.db.execute(
                'SELECT target_id,property_id,value_json FROM ws_values WHERE '+('target_kind=? AND ' if kind else '')+'target_id IN ('+marks+')',
                ((kind,*chunk) if kind else tuple(chunk))):
                self._manual_rows[(rid,p)]=_load(value)
        self._prepared_kind=kind
        self._prepared_ids=set(ids)

    def manual_value(self,kind,rid,p):
        if (self._manual_rows is not None and (self._prepared_kind is None or kind==self._prepared_kind) and (self._prepared_ids is None or rid in self._prepared_ids)):
            return self._manual_rows.get((rid,p))
        return self.s.manual_value(kind,rid,p)


    def _relation_rows(self,rid,direction='out'):
        sql='''SELECT r.owner,r.source_id,r.predicate,r.target_id,r.evidence_json
               FROM relationships r JOIN records s ON s.record_id=r.source_id
               JOIN records t ON t.record_id=r.target_id
               WHERE s.active=1 AND t.active=1 AND '''
        key='source_id' if direction=='out' else 'target_id'
        return [dict(x) for x in self.db.execute(sql+'r.'+key+'=?',(rid,))]

    def _coverage(self,kind):
        if kind=='Email':
            statuses=[self._coverage_snapshot.get(key,'unknown') for key in ('MailInbox','MailSent')]
            return 'complete' if all(value=='complete' for value in statuses) else 'unknown' if all(value=='unknown' for value in statuses) else 'partial'
        return self._coverage_snapshot.get({'Event':'CalendarICS','Person':'ContactsProfiles','Document':'DocumentCorpus'}.get(kind,kind),'unknown')

    def replies(self):
        if self._reply_index is None:self._reply_index=reply_context(self.s)
        return self._reply_index

    def _output(self,row,prop,state,value=None,reason=None,evidence=None,preset_id=None):
        result={'target':{'record_type':row['record_type'],'record_id':row['record_id']},
          'property_id':prop,'state':state,'value':value,'reason':reason,
          'producer_rule_id':preset_id,'producer_version':1 if preset_id else None,
          'settings_revision':self._snapshot_revision,'core_epoch':self._snapshot_epoch,
          'as_of':self.as_of.isoformat(),'authority':'workspace_computed',
          'evidence':evidence or []}
        return result

    def core_reply_observed(self,row):
        if row['record_type']!='Email':raise KMInvalid('INVALID_TYPE','Reply status only applies to Email')
        _,add=self.data(row);direction=add.get('direction','').casefold()
        if direction!='incoming':
            return {'state':'not_applicable','value':None,'reason':'Incoming Email only','evidence':[]}
        result=self.replies().observed(row['record_id'],policy='legacy_outgoing_direct')
        return {**result,'coverage':self._coverage('Email')}

    def reply_evidence(self,row):
        return self.replies().observed(row['record_id'])

    def sender_identity(self,row):
        index=self.replies()
        actor,reason=index.actor(row['record_id']) if row['record_id'] in index.records else (None,'inactive')
        from .reply_evidence import REASONS
        return {'state':'evaluated' if actor else 'not_evaluable','status':actor or 'unknown',
                'reason':REASONS.get(reason,'Based on the sender, saved folder and confirmed addresses.'),
                'fields':['from','source_folder'],'settings_revision':self._snapshot_revision}

    def field_value(self,row,field):
        props,added=self.data(row)
        if field=='source_time' and row['record_type']=='Email':return source_time(props,added)
        if field=='reply_status' and row['record_type']=='Email':return self.reply_evidence(row)['status']
        if field=='sender_identity' and row['record_type']=='Email':return self.sender_identity(row)['status']
        if field=='title' and row['record_type']=='Document':
            metadata=props.get('metadata')
            return props.get('title') or metadata.get('title') if isinstance(metadata,dict) else props.get('title')
        return row[field] if field in ('record_id','record_type','scope','updated_at') else props.get(field,added.get(field))

    def _verified_selection(self,row,params):
        selected_people=set(params.get('selected_person_ids',[]))
        selected_orgs=set(params.get('selected_organization_ids',[]))
        selected_loes=set(params.get('selected_loe_ids',[]))
        edges=self._relation_rows(row['record_id'])
        evidence=[]
        people=set()
        for edge in edges:
            if edge['target_id'] in selected_loes and edge['predicate']=='associated_with':evidence.append([edge])
            if row['record_type']=='Person' and edge['predicate'] in ('member_of','affiliated_with'):
                target=edge['target_id']
                if target in selected_orgs or self.s.effective_id(target) in selected_orgs:
                    evidence.append([edge,{'workspace_mapping':{'raw':target,'effective':self.s.effective_id(target)}}])
            if edge['predicate'] in ('sent_by','sent_to','copied_to','invites','organized_by','authored_by'):
                people.add(edge['target_id'])
                if edge['target_id'] in selected_people:evidence.append([edge])
        for p in people:
            for org_edge in self._relation_rows(p):
                if org_edge['predicate'] not in ('member_of','affiliated_with'):continue
                target=org_edge['target_id']
                effective=self.s.effective_id(target)
                if target in selected_orgs or effective in selected_orgs:
                    first=next((e for e in edges if e['target_id']==p),None)
                    if first:evidence.append([first,org_edge,{'workspace_mapping':{'raw':target,'effective':effective}}])
        return evidence

    def evaluate(self,row,property_id):
        if not isinstance(row,dict):row=dict(row)
        key=(row['record_id'],property_id)
        if key in self.cache:return self.cache[key]
        if key in self.calls:raise KMInvalid('DEPENDENCY_CYCLE','Detected recursive rule dependency')
        self.calls.add(key)
        try:
            result=self._evaluate(row,property_id)
            self.cache[key]=result
            return result
        finally:self.calls.remove(key)

    def _evaluate(self,row,p):
        definition=self._definitions.get(p)
        if not definition or definition['owner_type']!=row['record_type']:
            raise KMInvalid('UNKNOWN_PROPERTY','Property not configured for this record type')
        if definition.get('status',definition.get('state'))=='disabled':
            return self._output(row,p,'disabled',reason='Property disabled')
        if definition['mode']=='manual':
            value=self.manual_value(row['record_type'],row['record_id'],p)
            retained=self.s.retained_reference_ids(definition,value)
            return {'property_id':p,'target':{'record_type':row['record_type'],'record_id':row['record_id']},
                    'state':'not_evaluable' if retained else 'evaluated' if value is not None else 'unset','value':value,
                    'retained_reference_ids':retained,
                    'authority':'workspace_manual','evidence':[],
                    'reason':'Reference retained/inactive' if retained else None}
        return self._custom(row,p)

    def _custom(self,row,prop):
        # Preset-derived and user-created computed properties take *exactly* this
        # path. No branch uses property names or template IDs as rule selectors.
        producer=self._rules.get(prop)
        if producer is None:return self._output(row,prop,'disabled',reason='No enabled computed rule')
        previous=getattr(self,'_unknown',None)
        previous_evidence=getattr(self,'_evidence',None)
        self._evidence=[]
        self._unknown=None
        try:
            answer=self.expression(row,producer['expression'])
            failure=self._unknown
            evidence=self._evidence
        finally:self._unknown=previous;self._evidence=previous_evidence
        if answer is None:
            state,reason=failure if failure else ('not_evaluable','Required source or setting unavailable')
            return self._output(row,prop,state,reason=reason,evidence=evidence,preset_id=producer['rule_id'])
        return self._output(row,prop,'evaluated',bool(answer),
                    evidence=[{'workspace_rule_id':producer['rule_id']},*evidence],preset_id=producer['rule_id'])

    def _unavailable(self,state,reason):
        current=getattr(self,'_unknown',None)
        if current is None or state=='source_stale':self._unknown=(state,reason)
        return None

    def _primitive(self,row,field,op,expected):
        p,added=self.data(row)
        selves=set(self.settings['self_addresses']);groups=set(self.settings['group_addresses'])
        if op=='direct':
            direction=added.get('direction')
            if not selves or direction not in ('Incoming','Outgoing'):return None
            if direction=='Outgoing':return False
            to=p.get('to');cc=p.get('cc');sender=_email(p.get('from'))
            if not isinstance(to,list) or not isinstance(cc,list) or sender is None:return None
            dest=[_email(x) for x in to];copy=[_email(x) for x in cc]
            if None in dest or None in copy:return None
            # Count distinct *visible* To addresses, not duplicates of the same
            # Outlook address; Bcc remains outside the capture's scope.
            return sender not in selves and len(set(dest))==1 and next(iter(set(dest))) in selves and not copy
        if op=='reply_observed':
            reply=self.core_reply_observed(row)
            if reply['state']!='evaluated':return None
            if expected is False and reply['value'] is False and self._coverage('MailSent')!='complete':
                return self._unavailable('source_stale','Local Sent coverage incomplete')
            return reply['value'] is expected
        if op=='incoming_reply_missing':
            if added.get('direction')!='Outgoing':return None
            if self.replies().observed(row['record_id'],policy='legacy_incoming_direct')['value']:return False
            if self._coverage('MailInbox')!='complete':
                return self._unavailable('source_stale','Local Inbox coverage incomplete')
            return True
        if op=='external_recipient_visible':
            if not selves:return None
            visible=[];unknown=False
            for name in ('to','cc'):
                recipients=p.get(name)
                if not isinstance(recipients,list):unknown=True;continue
                for item in recipients:
                    address=_email(item)
                    if address is None:unknown=True
                    else:visible.append(address)
            if any(address not in selves for address in visible):return True
            return None if unknown else False
        if op in ('includes_self_address','includes_group_address'):
            raw=p.get(field)
            if raw is None and field=='attendee':raw=p.get('attendees')
            selected=selves if op=='includes_self_address' else groups
            if not selected or not isinstance(raw,list):return None
            matched={_email(a) for a in raw}
            if selected & matched:return True
            return None if None in matched else False
        if op=='event_owner_response':
            if not selves:return None
            requested=p.get('responseRequested',p.get('response_requested'))
            owner=p.get('responseType',p.get('response_type'))
            status=p.get('status')
            owner=owner.casefold() if isinstance(owner,str) else None
            status=status.casefold() if isinstance(status,str) else None
            if type(requested) is not bool or owner not in ('not_responded','pending','accepted','declined','tentative') or status not in ('confirmed','tentative','cancelled'):
                return None
            return requested and owner in ('not_responded','pending') and status!='cancelled'
        raise KMInvalid('INVALID_VALUE','Unsupported semantic test')

    def expression(self,row,expr):
        if 'not' in expr:
            value=self.expression(row,expr['not'])
            return None if value is None else not value
        if 'all' in expr:
            values=[self.expression(row,term) for term in expr['all']]
            return False if False in values else None if None in values else True
        if 'any' in expr:
            values=[self.expression(row,term) for term in expr['any']]
            return True if True in values else None if None in values else False
        p,added=self.data(row)
        field=expr['field'];op=expr['operator'];expected=expr.get('value')
        # Named evidence-backed tests are normal operators allowed in *any*
        # custom rule. No template/property ID is hardcoded in this evaluator.
        if op in ('direct','reply_observed','external_recipient_visible',
                  'incoming_reply_missing','includes_self_address','includes_group_address',
                  'event_owner_response'):
            return self._primitive(row,field,op,expected)
        if op=='verified_path_to_selected':
            if not expected:return None
            return bool(self._verified_selection(row,{
                'selected_person_ids':expected,'selected_organization_ids':expected,
                'selected_loe_ids':expected}))
        if field.startswith('ws:'):
            output=self.evaluate(row,field)
            if op=='is_present' and output['state']=='unset':return False
            if output['state']!='evaluated':
                return self._unavailable(output['state'] if output['state'] in ('disabled','source_stale') else 'not_evaluable',
                                         'Dependency '+field+' is '+output['state'])
            value=output['value']
        elif field in ('reply_status','sender_identity') and row['record_type']=='Email':
            evidence=self.reply_evidence(row) if field=='reply_status' else self.sender_identity(row)
            if isinstance(getattr(self,'_evidence',None),list):self._evidence.append(evidence)
            value=evidence['status']
            if value=='unknown' and not (op in ('equals','not_equals','one_of') and
                    (expected=='unknown' or isinstance(expected,list) and 'unknown' in expected)):
                return self._unavailable('not_evaluable',evidence['reason'])
        else:value=self.field_value(row,field)
        if op=='is_present':return value is not None
        if value is None:return None
        kind=(self._definitions[field]['value_type'] if field.startswith('ws:') else
              FIELD_KINDS.get(row['record_type'],{}).get(field))
        if kind=='email':
            value=_email(value)
            if value is None:return None
            if op in ('equals','not_equals','one_of'):
                comparands=expected if op=='one_of' else [expected]
                values=[_email(x) for x in comparands]
                if None in values:return None
                matches=value in values
                return matches if op!='not_equals' else not matches
        if op=='equals':return value==expected
        if op=='not_equals':return value!=expected
        if op=='one_of':return value in expected if isinstance(expected,list) else None
        if op in ('list_count_equals','list_count_at_least','list_count_at_most'):
            if not isinstance(value,list):return None
            return (len(value)==expected if op=='list_count_equals' else
                    len(value)>=expected if op=='list_count_at_least' else len(value)<=expected)
        if op=='list_contains':
            if not isinstance(value,list) or not all(isinstance(x,str) for x in value):return None
            return expected in value
        if op=='email_list_contains':
            if not isinstance(value,list):return None
            observed=[_email(x) for x in value]
            if _email(expected) in observed:return True
            return None if None in observed else False
        if op in ('contains','starts_with','ends_with'):
            if not isinstance(value,str):return None
            source=value.casefold();searched=expected.casefold()
            return (searched in source if op=='contains' else
                    source.startswith(searched) if op=='starts_with' else source.endswith(searched))
        if op in ('email_matches','email_matches_any'):
            email=_email(value)
            if email is None:return None
            choices=[expected] if op=='email_matches' else expected
            if not choices:return None
            return any(email==entry.casefold() or
                       entry.casefold().endswith('@*') and email.split('@')[0]==entry.casefold()[:-2]
                       for entry in choices)
        if op in ('local_date_age_gt','local_date_age_lte'):
            age=_date_age(value,self.as_of,self.settings['workspace_timezone'])
            return (age>expected if op=='local_date_age_gt' else age<=expected) if age is not None else None
        if op=='reference_contains':
            if not isinstance(value,list):return None
            target=expected.get('record_id') if isinstance(expected,dict) else expected
            return any((item.get('record_id') if isinstance(item,dict) else item)==target for item in value)
        if op=='reference_in_selected':
            if isinstance(value,dict):
                try:self.s._assert_target(value['record_type'],value['record_id'])
                except KMInvalid as error:
                    if error.code=='REFERENCE_INACTIVE':
                        return self._unavailable('not_evaluable','Selected reference is retained/inactive')
                    raise
                value=value['record_id']
            return value in expected if isinstance(value,str) else None
        raise KMInvalid('INVALID_VALUE','Unsupported evaluator operator')

# Supported source-field datatypes; names are source/normalized keys, not new facts.
# One type/operator authority is used by rule validation and registry introspection.
FIELD_KINDS={
 'Email':{'subject':'text','mailbox_id':'text','source_folder':'text','importance':'text',
          'from':'email','to':'list','cc':'list','sent_at_utc':'timestamp',
          'received_at_utc':'timestamp','conversation_id':'text','direction':'text',
          'has_attachments':'boolean','source_time':'timestamp','reply_status':'enum','sender_identity':'enum'},
 'Event':{'summary':'text','dtstart':'timestamp','dtend':'timestamp','status':'text',
          'organizer':'email','attendee':'list','responseRequested':'boolean',
          'responseType':'text','location':'text','start_utc':'timestamp',
          'end_utc':'timestamp','all_day':'boolean'},
 'Person':{'displayName':'text','mail':'email','companyName':'text',
           'department':'text','jobTitle':'text','accountEnabled':'boolean',
           'officeLocation':'text','userType':'text','addresses':'list'},
 'Document':{'metadata':'object','source':'object','origin':'object',
             'extraction':'object','title':'text','origin_type':'text'},
 'Organization':{'kind':'text','name':'text','parent_id':'record_ref'},
 'LineOfEffort':{'name':'text','kind':'text','reference_code':'text',
                 'objective':'text','aliases':'list'}}
WORKSPACE_FIELDS={'title':'text','created_at_utc':'timestamp','updated_at_utc':'timestamp',
                  'updated_at':'timestamp','scope':'text','record_type':'text'}
def fields_for(kind):
    return FIELD_KINDS.get(kind, WORKSPACE_FIELDS if isinstance(kind,str) and kind.startswith('wsrt_') else {})
ALLOWED_FIELDS={kind:set(fields)|{'record_id'} for kind,fields in FIELD_KINDS.items()}
# Lists of contact identities are not string lists. Expose the correct predicate.
EMAIL_LIST_FIELDS={('Email','to'),('Email','cc'),('Event','attendee')}
STRING_LIST_FIELDS={('Person','addresses'),('LineOfEffort','aliases')}
SCALAR_KINDS={'text','email','timestamp','date','enum','boolean'}

def allowed_operators(record_type,field,property_ids=None):
    if field=='record_id' and (record_type in FIELD_KINDS or (isinstance(record_type,str) and record_type.startswith('wsrt_'))):
        return ('is_present','equals','not_equals','one_of','verified_path_to_selected')
    if isinstance(field,str) and field.startswith('ws:'):
        definitions=property_ids if isinstance(property_ids,dict) else {}
        kind=definitions.get(field)
        if kind is None:return ()
    else:kind=fields_for(record_type).get(field)
    if kind is None:return ()
    result=['is_present']
    if kind in SCALAR_KINDS:result.extend(('equals','not_equals','one_of'))
    if kind in ('text','email','enum'):result.extend(('contains','starts_with','ends_with'))
    if kind=='email':result.append('email_matches')
    if kind in ('timestamp','date'):result.extend(('local_date_age_gt','local_date_age_lte'))
    if kind in ('list','record_ref_set'):
        result.extend(('list_count_equals','list_count_at_least','list_count_at_most'))
        if (record_type,field) in STRING_LIST_FIELDS:result.append('list_contains')
        if (record_type,field) in EMAIL_LIST_FIELDS:result.append('email_list_contains')
    if kind=='record_ref':result.append('reference_in_selected')
    if kind=='record_ref_set':result.append('reference_contains')
    if (record_type,field)==('Email','direction'):
        result.extend(('direct','reply_observed','external_recipient_visible','incoming_reply_missing'))
    if (record_type,field) in (('Event','attendee'),('Email','to'),('Email','cc')):
        result.extend(('includes_self_address','includes_group_address'))
    if (record_type,field)==('Event','responseType'):
        result.append('event_owner_response')
    if kind=='email':result.append('email_matches_any')
    return tuple(result)

# Expression validator is shared by preview and save. No arbitrary code, nesting, joins or operators.
def validate_expression(expr,record_type,property_ids,depth=1,counter=None):
    """Validate one bounded, typed boolean expression tree; no arbitrary code.

    property_ids is a mapping of *installed* Workspace property IDs to value
    types. Template catalog entries that were never installed are not fields.
    """
    if counter is None:counter={'leaves':0,'joins':0,'dependencies':set()}
    if not fields_for(record_type) or not isinstance(property_ids,dict):
        raise KMInvalid('INVALID_TYPE','Invalid expression record type or property registry')
    if not isinstance(expr,dict) or depth>RULE_LIMITS['max_depth']:
        raise KMInvalid('INVALID_VALUE','Invalid expression depth')
    group=[k for k in ('all','any','not') if k in expr]
    if group:
        if len(expr)!=1 or len(group)!=1:
            raise KMInvalid('INVALID_VALUE','Invalid expression group')
        name=group[0]
        if name=='not':
            validate_expression(expr[name],record_type,property_ids,depth+1,counter)
        else:
            nodes=expr[name]
            if not isinstance(nodes,list) or not 1<=len(nodes)<=RULE_LIMITS['max_group_children']:
                raise KMInvalid('INVALID_VALUE','Invalid condition group width')
            for node in nodes:validate_expression(node,record_type,property_ids,depth+1,counter)
        return counter
    if set(expr)-{'field','operator','value'}:
        raise KMInvalid('INVALID_VALUE','Unknown condition keys')
    field=expr.get('field');op=expr.get('operator')
    if not isinstance(field,str) or op not in OPERATORS:
        raise KMInvalid('INVALID_VALUE','Unknown expression field/operator')
    if field.startswith('ws:'):
        if field not in property_ids:raise KMInvalid('UNKNOWN_PROPERTY','Unknown/uninstalled Workspace reference')
        counter['dependencies'].add(field)
        kind=property_ids[field]
    else:
        if field not in set(fields_for(record_type))|{'record_id'}:
            raise KMInvalid('INVALID_VALUE','Field not allowed for this record type')
        kind=fields_for(record_type).get(field,'text')
    if op not in allowed_operators(record_type,field,property_ids):
        raise KMInvalid('INVALID_VALUE','Operator incompatible with typed field')
    need_value=op in ('equals','not_equals','one_of','email_matches','email_matches_any',
        'reply_observed','local_date_age_gt','local_date_age_lte','list_contains',
        'list_count_equals','list_count_at_least','list_count_at_most','email_list_contains',
        'reference_in_selected','reference_contains','verified_path_to_selected','contains','starts_with','ends_with')
    if ('value' in expr)!=need_value:
        raise KMInvalid('INVALID_VALUE','Incorrect number of operator operands')
    v=expr.get('value')
    if op=='reference_contains' and (not isinstance(v,dict) or set(v)!={'record_type','record_id'} or not all(isinstance(x,str) and x for x in v.values())):
        raise KMInvalid('INVALID_VALUE','A typed reference is required')
    if op=='reply_observed' and type(v) is not bool:
        raise KMInvalid('INVALID_VALUE','Reply comparison requires Boolean value')
    if op in ('reference_in_selected','verified_path_to_selected'):
        counter['joins']+=1
        if (not isinstance(v,list) or not 1<=len(v)<=RULE_LIMITS['max_selected_ids'] or
            any(not isinstance(x,str) or not x for x in v) or len(set(v))!=len(v)):
            raise KMInvalid('INVALID_VALUE','Invalid selected reference IDs')
    if op in ('local_date_age_gt','local_date_age_lte',
              'list_count_equals','list_count_at_least','list_count_at_most'):
        if type(v) is not int or not 0<=v<=36500:
            raise KMInvalid('INVALID_VALUE','Date/count comparison requires bounded integer')
    scalar_kind=kind in ('text','email','timestamp','date','enum')
    if op in ('equals','not_equals','one_of'):
        values=v if op=='one_of' else [v]
        if op=='one_of' and (not isinstance(v,list) or not 1<=len(v)<=50):
            raise KMInvalid('INVALID_VALUE','one_of requires 1..50 typed values')
        if not all((type(x) is bool if kind=='boolean' else isinstance(x,str) if scalar_kind else
                    isinstance(x,(str,dict)) if kind=='record_ref' else False) for x in values):
            raise KMInvalid('INVALID_VALUE','Comparison value has incompatible type')
    if op in ('contains','starts_with','ends_with','list_contains','email_list_contains'):
        if not isinstance(v,str) or not v or len(v)>240:
            raise KMInvalid('INVALID_VALUE','Nonempty bounded text comparison required')
    if op=='email_list_contains':
        try:email_address(v)
        except (ValueError,TypeError):raise KMInvalid('INVALID_VALUE','Invalid selected email address') from None
    if op in ('email_matches','email_matches_any'):
        values=[v] if op=='email_matches' else v
        try:
            if not isinstance(values,list) or not 1<=len(values)<=64:raise ValueError()
            for x in values:
                if isinstance(x,str) and '*' in x.removesuffix('@*'):raise ValueError()
            patterns(values)
        except (ValueError,TypeError):
            raise KMInvalid('INVALID_VALUE','Invalid address pattern') from None
    counter['leaves']+=1
    if (counter['leaves']>RULE_LIMITS['max_leaves'] or
        counter['joins']>RULE_LIMITS['max_relation_joins']):
        raise KMInvalid('INVALID_VALUE','Expression exceeds safe execution bounds')
    return counter
