"""Editable starter recipes installed through the existing definition authority."""
from copy import deepcopy
import hashlib
import json

from o3_core.store import encode
from .catalog import CATALOG_FINGERPRINT
from .evaluation import KMInvalid

STARTER_IDS = {name: 'view:user:starter-' + suffix for name, suffix in
               (('unanswered', 'unanswered'), ('waiting', 'waiting'), ('all', 'all-mail'))}
DISPOSITION_PROPERTIES = {'unanswered': 'ws:user:reply-not-required',
                          'waiting': 'ws:user:waiting-stopped'}
RECEIPT = 'alpha5_starter_conversion_v1'


def clause(field, operator, value=None):
    result = {'field': field, 'operator': operator}
    if value is not None: result['value'] = value
    return result


def factory(task, *, include_cc=False):
    if task not in STARTER_IDS: raise KMInvalid('INVALID_VALUE', 'Unknown starter')
    labels = {'unanswered': 'Unanswered', 'waiting': 'Waiting for replies', 'all': 'All mail'}
    spec = {'label': labels[task], 'record_type': 'Email', 'view_id': STARTER_IDS[task],
            'version': 1, 'visible': True, 'order': list(STARTER_IDS).index(task),
            'sorts': [{'field': 'source_time', 'direction': 'desc' if task == 'all' else 'asc'}],
            'group_by_fields': ['mailbox_id', 'conversation_id'],
            'representative_sort': {'field': 'source_time', 'direction': 'desc'},
            'unknown_policy': 'include', 'filter_expression': None}
    if task != 'all':
        recipient = clause('to', 'includes_self_address')
        if include_cc: recipient = {'any': [recipient, clause('cc', 'includes_self_address')]}
        prop = DISPOSITION_PROPERTIES[task]
        spec['filter_expression'] = {'all': [
            clause('direction', 'equals', 'Incoming' if task == 'unanswered' else 'Outgoing'),
            {'any':[recipient if task == 'unanswered' else clause('direction', 'external_recipient_visible'),
                    clause('sender_identity','equals','unknown')]},
            clause('reply_status', 'equals', 'no_reply_found'),
            {'not': {'all': [clause(prop, 'is_present'), clause(prop, 'equals', True)]}},
        ]}
    return spec


def metadata(view_id):
    task = next((key for key, value in STARTER_IDS.items() if value == view_id), None)
    return {'starter_id': task, 'factory_specification': factory(task)} if task else {}


def ensure(service, params, expected):
    if set(params) - {'legacy_mail_views'}:
        raise KMInvalid('INVALID_VALUE', 'Unsupported starter conversion option')
    legacy = params.get('legacy_mail_views', [])
    if not isinstance(legacy, list) or len(legacy) > 32:
        raise KMInvalid('INVALID_VALUE', 'Invalid saved mail view collection')
    fingerprint = hashlib.sha256(encode(legacy).encode()).hexdigest()
    saved = service._meta(RECEIPT)
    if saved:
        receipt = json.loads(saved)
        if receipt['legacy_fingerprint'] != fingerprint:
            raise KMInvalid('LEGACY_CONFIG_CHANGED', 'Legacy mail views changed after conversion; restore the matching settings or reconcile them explicitly')
        service._assert_revision(expected)
        return {'changed': False, 'new_workspace_revision': service.revision(),
                'starter_view_ids': STARTER_IDS, 'legacy_view_ids': receipt['legacy_view_ids'],
                'conversion': receipt}
    service._assert_revision(expected)
    from .migration import prepare_alpha5_conversion
    checkpoint = prepare_alpha5_conversion(service)
    mappings = {}; specifications = [factory(task) for task in STARTER_IDS]
    for item in legacy:
        if not isinstance(item, dict) or item.get('task') not in STARTER_IDS or not isinstance(item.get('id'), str):
            raise KMInvalid('INTEGRITY_FAILED', 'Invalid saved legacy mail view')
        old_id = item['id']
        if old_id in mappings: raise KMInvalid('INTEGRITY_FAILED', 'Duplicate saved legacy mail view identity')
        options = item.get('options', {})
        if not isinstance(options, dict) or set(options) - {'include_cc', 'sort'}:
            raise KMInvalid('INTEGRITY_FAILED', 'Unsupported saved legacy mail view option')
        if type(options.get('include_cc', False)) is not bool or options.get('sort', 'oldest') not in ('oldest', 'newest'):
            raise KMInvalid('INTEGRITY_FAILED', 'Invalid saved legacy mail view option')
        spec = factory(item['task'], include_cc=options.get('include_cc', False))
        spec['view_id'] = 'view:user:migrated-mail-' + hashlib.sha256(old_id.encode()).hexdigest()[:24]
        spec['label'] = item.get('name', 'Saved mail view')
        spec['sorts'] = [{'field': 'source_time', 'direction': 'desc' if options.get('sort', 'newest' if item['task'] == 'all' else 'oldest') == 'newest' else 'asc'}]
        spec['order'] = 10 + len(mappings)
        mappings[old_id] = spec['view_id']; specifications.append(spec)
    definitions = []
    for task, prop in DISPOSITION_PROPERTIES.items():
        spec = service._property_spec({'label': 'No reply needed' if task == 'unanswered' else 'Stop waiting',
              'mode': 'manual', 'value_type': 'boolean'}, 'Email', prop)
        definitions.append((prop, spec))
    for identifier in [key for key, _ in definitions] + [spec['view_id'] for spec in specifications]:
        if service._definition(identifier):
            raise KMInvalid('INTEGRITY_FAILED', 'Starter conversion identity already exists without its receipt')
    old_dispositions = [dict(row) for row in service.db.execute("SELECT key,value FROM engine_meta WHERE key GLOB 'mail_task_disposition_v1:*'")]
    receipt = {'version': 1, 'catalog': CATALOG_FINGERPRINT,
               'legacy_fingerprint': fingerprint, 'legacy_view_ids': mappings,
               'starter_view_ids': STARTER_IDS, 'checkpoint': checkpoint,
               'converted_disposition_count': len(old_dispositions)}

    check=getattr(service,'_startup_check',None)
    if check:check()
    def convert():
        if check:check()
        for prop, spec in definitions:
            service._put_def(prop, 'property', 'Email', 'custom', spec, 'enabled')
        for spec in specifications:
            spec['dependencies'] = sorted(service._validate_view_spec(spec))
            service._put_def(spec['view_id'], 'view', 'Email', 'custom', spec, 'enabled')
        for entry in old_dispositions:
            value = json.loads(entry['value']); prop = DISPOSITION_PROPERTIES[value['task']]
            service.db.execute('INSERT INTO ws_values VALUES(?,?,?,?,?)',
                               ('Email', value['anchor_record_id'], prop, 'true', 1))
            service.db.execute('DELETE FROM engine_meta WHERE key=?', (entry['key'],))
        service._setmeta('km_schema_contract', CATALOG_FINGERPRINT)
        service._setmeta(RECEIPT, encode(receipt))
        from .workspace_types import validate_v4
        validate_v4(service.db)
        return {'legacy_dispositions': old_dispositions}, receipt, {
            'views': [spec['view_id'] for spec in specifications],
            'properties': list(DISPOSITION_PROPERTIES.values()),
            'starter_view_ids': STARTER_IDS, 'legacy_view_ids': mappings, 'conversion': receipt}
    return service._commit('view.starters.ensure', 'workspace', service.wsid(), expected, convert)
