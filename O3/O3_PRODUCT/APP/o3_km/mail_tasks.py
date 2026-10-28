"""Compatibility names for pre-Alpha5 clients; all selection/actions are central.

No classifier, reply graph, disposition writer or saved-view store lives here.
Legacy disposition validation remains solely for preconversion integrity checks.
"""
from datetime import datetime, timezone
from copy import deepcopy
import json
from o3_core.store import encode
from .evaluation import KMInvalid
from .reply_evidence import context, address as _address, MAX_CHAIN
from .property_commands import MAX_BATCH as MAX_ACTION_ANCHORS

TASKS=('unanswered','waiting','all')
PREFIX='mail_task_disposition_v1:'

def _text(value):return value.strip() if isinstance(value,str) else ''
def _stamp(value):
    try:
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (AttributeError,ValueError,TypeError):return None

def _records(service):
    return {rid:{'record_id':rid,'properties':props,'additional':added}
            for rid,(props,added) in context(service).records.items()}

def identity(service, records=None):
    index=context(service) if records is None else None
    if index is not None and hasattr(index,'identity_result'):return deepcopy(index.identity_result)
    records = _records(service) if records is None else records
    settings = service.settings()
    suggestions = {}
    for rid, row in records.items():
        p = row['properties']; mailbox = _text(p.get('mailbox_id'))
        for address, reason in ((_address(mailbox), 'captured_mailbox_address'),
            (_address(p.get('from')) if row['additional'].get('direction') == 'Outgoing' else None,
             'captured_sent_sender')):
            if not address: continue
            item = suggestions.setdefault(address, {'address': address, 'mailbox_ids': set(),
                'evidence_record_ids': [], 'reason': reason, 'requires_confirmation': True})
            if mailbox: item['mailbox_ids'].add(mailbox)
            if len(item['evidence_record_ids']) < 5 and rid not in item['evidence_record_ids']:
                item['evidence_record_ids'].append(rid)
    own = settings['self_addresses']
    result={'configured_addresses': own, 'group_addresses': settings['group_addresses'],
        'suggestions': [{**v, 'mailbox_ids': sorted(v['mailbox_ids'])} for _, v in sorted(suggestions.items())],
        'state': 'ready' if own else 'confirmation_required',
        'message': 'Based on your confirmed email addresses.' if own else
            'Which email addresses are yours? All mail is available while you decide.'}
    if index is not None:index.identity_result=deepcopy(result)
    return result


def validate_dispositions(service):
    """Absent extension metadata is a valid populated Candidate 6 workspace."""
    receipts = {}
    for row in service.db.execute('SELECT key,value FROM engine_meta WHERE key GLOB ?', (PREFIX + '*',)):
        try:
            value = json.loads(row['value'])
            task, rid = row['key'][len(PREFIX):].split(':', 1)
            target = service.engine.store.row(rid)
            if (task not in TASKS[:2] or not target or target['record_type'] != 'Email'
                or set(value) != {'version', 'task', 'anchor_record_id', 'action_id', 'at_utc', 'actor'}
                or type(value['version']) is not int or value['version'] != 1 or value['task'] != task or value['anchor_record_id'] != rid
                or not isinstance(value['action_id'], str) or len(value['action_id']) != 32
                or _stamp(value['at_utc']) is None or not isinstance(value['actor'], str)):
                raise ValueError()
            token = value['action_id']
            if token not in receipts:
                audit = service.db.execute("SELECT after_json FROM ws_audit WHERE operation='mail.tasks.dismiss' AND subject_id=?",
                                           (token,)).fetchone()
                receipts[token] = json.loads(audit[0]) if audit else {}
            if receipts[token].get(row['key']) != value: raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise KMInvalid('INTEGRITY_FAILED', 'Invalid saved mail action') from None


def query(service,params,as_of):
    from .starters import STARTER_IDS
    allowed={'task','include_cc','page_size','cursor','search','mailbox_id','conversation_id','sort'}
    if set(params)-allowed:raise KMInvalid('INVALID_VALUE','Unsupported mail view option')
    task=params.get('task','unanswered')
    if task not in STARTER_IDS:raise KMInvalid('INVALID_VALUE','Unknown mail view')
    stored=service._definition(STARTER_IDS[task])
    if not stored:raise KMInvalid('CONFIGURATION_REQUIRED','Install the editable starter views before querying mail')
    spec=json.loads(stored['specification_json'])
    if type(params.get('include_cc',False)) is not bool:raise KMInvalid('INVALID_TYPE','Include copied messages must be Boolean')
    if params.get('include_cc'):
        def copied(node):
            if node=={'field':'to','operator':'includes_self_address'}:
                return {'any':[node,{'field':'cc','operator':'includes_self_address'}]}
            if isinstance(node,dict):return {key:copied(value) for key,value in node.items()}
            if isinstance(node,list):return [copied(value) for value in node]
            return node
        spec['filter_expression']=copied(spec.get('filter_expression'))
    restrictions=[{'field':field,'operator':'equals','value':params[field]} for field in ('mailbox_id','conversation_id') if params.get(field)]
    if restrictions:spec['filter_expression']={'all':([spec['filter_expression']] if spec.get('filter_expression') else [])+restrictions}
    options={key:params[key] for key in ('page_size','cursor','search') if key in params}
    if 'sort' in params:
        if params['sort'] not in ('oldest','newest'):raise KMInvalid('INVALID_VALUE','Invalid mail ordering')
        options['sorts']=[{'field':'source_time','direction':'desc' if params['sort']=='newest' else 'asc'}]
    result=service._configured_view(as_of,STARTER_IDS[task],spec,options)
    result['task']=task
    for row in result.get('rows',[]):
        row['record']={key:value for key,value in row.items() if key not in ('record',)}
        row['task']=task;row['task_key']=row['record_id'];row['additional_anchor_count']=max(0,row['group_count']-1)
        row['action_supported']=bool(row['eligible_record_ids']) and len(row['eligible_record_ids'])<=MAX_ACTION_ANCHORS
        row['source_time']=row['properties'].get('source_time')
    return result


def action(service,params,expected,as_of):
    from .starters import DISPOSITION_PROPERTIES
    from .property_commands import batch,undo
    if params.get('action')=='undo':return undo(service,{'undo_token':params.get('undo_token')},expected)
    if params.get('action')!='dismiss' or params.get('task') not in DISPOSITION_PROPERTIES:
        raise KMInvalid('INVALID_VALUE','Choose a supported manual disposition')
    return batch(service,{'records':[{'record_type':'Email','record_id':rid} for rid in params.get('anchor_record_ids',[])],
        'property_id':DISPOSITION_PROPERTIES[params['task']],'operation':'set','value':True},expected)
