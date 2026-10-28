"""Immutable KM 1.0 shipped semantic catalog; no executable user rules."""
from __future__ import annotations
import hashlib, json, re

TYPES=('Email','Event','Person','Document','Organization','LineOfEffort')
TARGETS=TYPES+('WorkspaceOrganization',)

# (owner, property, mode, type, options)
_SPEC = [
 ('Email','ws:direct','computed','boolean',{}),
 ('Email','ws:noise','computed','boolean',{}),
 ('Email','ws:needs_attention','computed','boolean',{}),
 ('Email','ws:priority_correspondence','computed','boolean',{}),
 ('Email','ws:waiting_on_response','computed','boolean',{}),
 ('Event','ws:personally_listed','computed','boolean',{}),
 ('Event','ws:group_listed','computed','boolean',{}),
 ('Event','ws:response_needed','computed','boolean',{}),
 ('Event','ws:priority_event','computed','boolean',{}),
 ('Person','ws:role','manual','text',{'max_chars':160}),
 ('Person','ws:key_contact','manual','boolean',{}),
 ('Person','ws:organization_focus','computed','boolean',{}),
 ('WorkspaceOrganization','ws:display_name','manual','text',{'max_chars':160,'required':True,'storage':'ws_organizations.display_name'}),
 ('WorkspaceOrganization','ws:parent_command','manual','record_ref',{'ref_type':'WorkspaceOrganization'}),
 ('WorkspaceOrganization','ws:hq_location','manual','text',{'max_chars':240}),
 ('WorkspaceOrganization','ws:enterprise','manual','text',{'max_chars':120}),
 ('WorkspaceOrganization','ws:poc_people','manual','record_ref_set',{'ref_type':'Person','max_count':100}),
 ('LineOfEffort','ws:priority','manual','enum',{'allowed_values':['low','medium','high']}),
 ('LineOfEffort','ws:effort_level','manual','enum',{'allowed_values':['low','medium','high']}),
 ('LineOfEffort','ws:work_status','manual','enum',{'allowed_values':['active','on_hold','completed']}),
 ('LineOfEffort','ws:target_date','manual','date',{}),
 ('LineOfEffort','ws:responsible_person_id','manual','record_ref',{'ref_type':'Person'})
]
PROPERTIES={p:{'property_id':p,'owner_type':o,'mode':m,'value_type':t,'namespace':'ws',
                'cardinality':{'min':1 if x.get('required') else 0,'max':x.get('max_count',1)},
                'nullable':not x.get('required',False),'default':None,'display_label':p[3:].replace('_',' ').title(),
                'version':1,'status':'enabled','origin':'builtin',**x}
            for o,p,m,t,x in _SPEC}

_PRESETS=[
 ('WS-EMAIL-DIRECT-001','Email','ws:direct',[],{}),
 ('WS-EMAIL-NOISE-001','Email','ws:noise',[],{'sender_patterns':['noreply@*','no-reply@*','no_reply@*','donotreply@*']}),
 ('WS-EMAIL-ATTENTION-001','Email','ws:needs_attention',['WS-EMAIL-DIRECT-001','WS-EMAIL-NOISE-001'],{'age_calendar_days':2}),
 ('WS-EMAIL-PRIORITY-001','Email','ws:priority_correspondence',['WS-EMAIL-DIRECT-001'],{'selected_person_ids':[],'selected_organization_ids':[],'selected_loe_ids':[]}),
 ('WS-EMAIL-WAITING-001','Email','ws:waiting_on_response',[],{'age_calendar_days':3}),
 ('WS-EVENT-PERSONAL-001','Event','ws:personally_listed',[],{}),
 ('WS-EVENT-GROUP-001','Event','ws:group_listed',[],{}),
 ('WS-EVENT-RESPONSE-001','Event','ws:response_needed',[],{}),
 ('WS-EVENT-PRIORITY-001','Event','ws:priority_event',[],{'selected_person_ids':[],'selected_organization_ids':[],'selected_loe_ids':[]}),
 ('WS-PERSON-ORG-FOCUS-001','Person','ws:organization_focus',[],{'selected_organization_ids':[]})
]
PRESETS={i:{'preset_id':i,'owner_type':o,'property_id':p,'dependencies':deps,
            'default_settings':settings,'enabled_default':False,'rule_version':1,'origin':'builtin'}
         for i,o,p,deps,settings in _PRESETS}
PRESET_FOR_PROPERTY={p['property_id']:id for id,p in PRESETS.items()}

# Exactly 26 named views; LOE dashboard is parameterized, not an extra persistent view.
_VIEWS={
 'Email':[('all','All Mail','standard'),('inbox','Inbox','standard'),('sent','Sent','standard'),('conversations','Conversations','standard'),('needs_attention','Needs Attention','workspace'),('priority','Priority','workspace'),('waiting','Waiting on Response','workspace')],
 'Event':[('today','Today','standard'),('this_week','This Week','standard'),('this_month','This Month','standard')],
 'Person':[('all','All','standard'),('detailed','Detailed List','standard'),('by_org','By Org','standard'),('key_contacts','Key Contacts','workspace'),('stakeholder_map','Stakeholder Map','workspace')],
 'Document':[('all','All','standard'),('detailed','Detailed List','standard'),('by_author','By Author','workspace'),('by_loe','By LOE','workspace'),('concept_map','Concept Map','workspace')],
 'Organization':[('all','Organizations','standard'),('command_hierarchy','Command Hierarchy','workspace'),('enterprise_map','Enterprise Map','workspace'),('pocs','Org POCs','workspace'),('raw','Raw Organizations','audit')],
 'LineOfEffort':[('overview','Overview','standard')]
}
VIEW_TYPES={k:{'Email':'email','Event':'event','Person':'person','Document':'document','Organization':'organization','LineOfEffort':'loe'}[k] for k in TYPES}
VIEWS={f'view:{VIEW_TYPES[o]}:{key}':{'view_id':f'view:{VIEW_TYPES[o]}:{key}',
        'owner_type':o,'name':name,'kind':kind,'protected':kind in ('standard','audit'),
        'order':n,'enabled_default':kind in ('standard','audit'),'version':1}
        for o,items in _VIEWS.items() for n,(key,name,kind) in enumerate(items)}
assert len(VIEWS)==26
VIEWS['view:loe:dashboard']={'view_id':'view:loe:dashboard','owner_type':'LineOfEffort',
   'name':'LOE Dashboard','kind':'parameterized','protected':True,'version':1}
VIEW_PRESET={'view:email:needs_attention':'WS-EMAIL-ATTENTION-001',
             'view:email:priority':'WS-EMAIL-PRIORITY-001',
             'view:email:waiting':'WS-EMAIL-WAITING-001'}

DEFAULT_SETTINGS={'workspace_timezone':'America/New_York','week_start':'monday','self_addresses':[],
                  'group_addresses':[], 'calendar_display_names':{}}
CORE_PREDICATES=('affiliated_with','associated_with','attached_to','authored_by','copied_to',
                 'exception_of','invites','member_of','organized_by','part_of','related_to',
                 'replies_to','sent_by','sent_to')
OPERATORS=('equals','not_equals','is_present','one_of','email_matches','local_date_age_gt',
           'reference_in_selected','verified_path_to_selected','list_count_equals',
           'direct','reply_observed','event_owner_response')
# All Workspace fields (including formerly named built-ins) are offered as
# optional user-installable recipes. Installed definitions live in ws_definitions
# and use precisely the same validator, editor, evaluator and audit as user-made
# definitions. The catalog owns only immutable template identifiers/defaults.
_MANUAL_TEMPLATE_IDS={
 'ws:role':'WS-PERSON-ROLE-001',
 'ws:key_contact':'WS-PERSON-KEY-CONTACT-001',
 'ws:display_name':'WS-ORG-DISPLAY-001',
 'ws:parent_command':'WS-ORG-PARENT-001',
 'ws:hq_location':'WS-ORG-HQ-001',
 'ws:enterprise':'WS-ORG-ENTERPRISE-001',
 'ws:poc_people':'WS-ORG-POC-001',
 'ws:priority':'WS-LOE-PRIORITY-001',
 'ws:effort_level':'WS-LOE-EFFORT-001',
 'ws:work_status':'WS-LOE-STATUS-001',
 'ws:target_date':'WS-LOE-TARGET-001',
 'ws:responsible_person_id':'WS-LOE-RESPONSIBLE-001',
}
TEMPLATES={
 **{k:{**v,'mode':'computed','property_schema':dict(PROPERTIES[v['property_id']])}
    for k,v in PRESETS.items()},
 **{identifier:{'preset_id':identifier,'owner_type':PROPERTIES[prop]['owner_type'],
     'property_id':prop,'mode':'manual','property_schema':dict(PROPERTIES[prop]),
     'default_settings':{},'dependencies':[]}
    for prop,identifier in _MANUAL_TEMPLATE_IDS.items()}
}

def template_expression(preset_id, settings=None, dependency_properties=None):
    """Build a *normal editable expression* for a selected computed template.

    This is a recipe-construction operation only; preset IDs are never inspected
    by the runtime Workspace evaluator.
    """
    if preset_id not in PRESETS:raise ValueError('Computed preset not found')
    from copy import deepcopy
    settings={**deepcopy(PRESETS[preset_id]['default_settings']),**(settings or {})}
    deps=dependency_properties or {}
    def dep(name, fallback):
        value=deps.get(name,fallback)
        if not isinstance(value,str) or not value.startswith('ws:'):
            raise ValueError('Dependency must be a Workspace property ID')
        return value
    def clause(field,operator,value=None):
        d={'field':field,'operator':operator}
        if value is not None:d['value']=value
        return d
    if preset_id=='WS-EMAIL-DIRECT-001':return clause('direction','direct')
    if preset_id=='WS-EMAIL-NOISE-001':
        return clause('from','email_matches_any',settings['sender_patterns'])
    if preset_id=='WS-EMAIL-ATTENTION-001':
        return {'all':[clause(dep('direct','ws:direct'),'equals',True),
                       clause(dep('noise','ws:noise'),'equals',False),
                       clause('direction','reply_observed',False),
                       clause('received_at_utc','local_date_age_gt',settings['age_calendar_days'])]}
    if preset_id=='WS-EMAIL-PRIORITY-001':
        ids=sorted(set(settings.get('selected_person_ids',[])+settings.get('selected_organization_ids',[])+settings.get('selected_loe_ids',[])))
        return {'all':[clause(dep('direct','ws:direct'),'equals',True),
                       clause('record_id','verified_path_to_selected',ids)]}
    if preset_id=='WS-EMAIL-WAITING-001':
        return {'all':[clause('direction','equals','Outgoing'),
                       clause('direction','external_recipient_visible'),
                       clause('sent_at_utc','local_date_age_gt',settings['age_calendar_days']),
                       clause('direction','incoming_reply_missing')]}
    if preset_id=='WS-EVENT-PERSONAL-001':return clause('attendee','includes_self_address')
    if preset_id=='WS-EVENT-GROUP-001':return clause('attendee','includes_group_address')
    if preset_id=='WS-EVENT-RESPONSE-001':return clause('responseType','event_owner_response')
    if preset_id in ('WS-EVENT-PRIORITY-001','WS-PERSON-ORG-FOCUS-001'):
        ids=sorted(set(settings.get('selected_person_ids',[])+settings.get('selected_organization_ids',[])+settings.get('selected_loe_ids',[])))
        return clause('record_id','verified_path_to_selected',ids)
    raise ValueError('Unsupported template')

OPERATORS=tuple(dict.fromkeys(OPERATORS+('email_matches_any','external_recipient_visible',
    'incoming_reply_missing','includes_self_address','includes_group_address',
    'contains','starts_with','ends_with','local_date_age_lte',
    'list_contains','email_list_contains','list_count_at_least','list_count_at_most','reference_contains')))
# Deliberate safety bounds, widened from the initial draft after inspecting
# the advanced-builder use cases. These are shared by schema, validation and tests.
RULE_LIMITS={'max_leaves':32,'max_depth':5,'max_relation_joins':4,
             'max_group_children':32,'max_selected_ids':100,'preview_rows':200}

CATALOG_FINGERPRINT=hashlib.sha256(json.dumps({'properties':PROPERTIES,'templates':TEMPLATES,
    'views':VIEWS,'operators':OPERATORS,'template_contract':4,'rule_limits':RULE_LIMITS},sort_keys=True,separators=(',',':')).encode()).hexdigest()

EMAIL_RE=re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$",re.ASCII)
PART_RE=re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$",re.ASCII)
def email_address(address:str)->str:
    if not isinstance(address,str):raise ValueError('Invalid email address')
    value=address.strip().casefold()
    if len(value)>254 or not EMAIL_RE.fullmatch(value) or '..' in value:
        raise ValueError('Invalid email address')
    return value

def addresses(values:list)->list[str]:
    if not isinstance(values,list) or len(values)>64:raise ValueError('Invalid address list')
    return sorted({email_address(v) for v in values})

def patterns(values:list)->list[str]:
    if not isinstance(values,list) or len(values)>64:raise ValueError('Invalid sender patterns')
    out=[]
    for v in values:
        if not isinstance(v,str):raise ValueError('Invalid sender pattern')
        v=v.strip().casefold()
        if v.endswith('@*'):
            if not PART_RE.fullmatch(v[:-2]):raise ValueError('Invalid sender pattern')
        else:v=email_address(v)
        out.append(v)
    return sorted(set(out))
