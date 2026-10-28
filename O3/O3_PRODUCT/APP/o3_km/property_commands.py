"""Atomic typed value commands shared by every interaction route."""
import json
import uuid

from o3_core.store import encode
from .evaluation import KMInvalid

MAX_BATCH = 5000


def batch(service, params, expected):
    if set(params) - {'records', 'property_id', 'operation', 'value', 'definition'}:
        raise KMInvalid('INVALID_VALUE', 'Unsupported property command option')
    records = params.get('records'); prop = params.get('property_id'); operation = params.get('operation')
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH:
        raise KMInvalid('INVALID_VALUE', 'Select between 1 and 5000 records')
    if operation not in ('set', 'clear', 'add', 'remove'):
        raise KMInvalid('INVALID_VALUE', 'Unsupported property operation')
    if not isinstance(prop, str): raise KMInvalid('INVALID_VALUE', 'Choose a property')
    seen = set(); targets = []
    for item in records:
        if not isinstance(item, dict) or set(item) != {'record_type', 'record_id'}:
            raise KMInvalid('INVALID_VALUE', 'Every target requires a typed record identity')
        key = (item['record_type'], item['record_id'])
        if not all(isinstance(value, str) for value in key) or key in seen:
            raise KMInvalid('INVALID_VALUE', 'Select distinct typed record identities')
        seen.add(key); targets.append(key)
    if len({kind for kind, _ in targets}) != 1:
        raise KMInvalid('INVALID_TYPE', 'This property applies to one record type')
    kind = targets[0][0]
    definition = service.property_definition(prop)
    created = False
    if definition is None:
        if not prop.startswith('ws:user:') or 'definition' not in params:
            raise KMInvalid('UNKNOWN_PROPERTY', 'Property is not configured')
        definition = service._property_spec(params['definition'], kind, prop); created = True
    elif 'definition' in params:
        proposed = service._property_spec(params['definition'], kind, prop)
        if any(proposed.get(key) != definition.get(key) for key in ('owner_type','mode','value_type','ref_type')):
            raise KMInvalid('INVALID_TYPE', 'The existing property has a different meaning')
    if definition['owner_type'] != kind or definition['mode'] != 'manual' or definition.get('status', 'enabled') != 'enabled':
        raise KMInvalid('INVALID_TYPE', 'Choose an enabled manual property for these records')
    if prop == 'ws:display_name':
        raise KMInvalid('INVALID_TYPE', 'Use the organization rename operation for its required name')
    if operation == 'clear' and definition.get('nullable') is False:
        raise KMInvalid('INVALID_VALUE', 'This required value cannot be cleared')
    if operation in ('add', 'remove') and definition['value_type'] != 'record_ref_set':
        raise KMInvalid('INVALID_TYPE', 'Add/remove applies to a set of typed record links')
    token = uuid.uuid4().hex

    def apply():
        before = []; after = []; changed_ids = []
        for target_kind, rid in targets:
            service._assert_target(target_kind, rid)
            row = service.db.execute('SELECT value_json,revision FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?',
                                     (target_kind, rid, prop)).fetchone()
            old = json.loads(row['value_json']) if row else None
            value = params.get('value')
            if operation == 'clear': value = None
            elif operation in ('add', 'remove'):
                refs = value if isinstance(value, list) else [value]
                refs = service._validate_manual(definition, refs, target_kind, rid, allow_retained_refs=operation=='remove')
                incoming = {item['record_id']: item for item in refs}
                current = {item['record_id']: item for item in (old or [])}
                if operation == 'add': current.update(incoming)
                else:
                    for record_id in incoming: current.pop(record_id, None)
                value = service._validate_manual(definition, list(current.values()), target_kind, rid, allow_retained_refs=True)
            else:
                value = service._validate_manual(definition, value, target_kind, rid)
            entry = {'record_type': target_kind, 'record_id': rid, 'property_id': prop,
                     'value': old, 'revision': row['revision'] if row else None}
            before.append(entry)
            if old == value:
                after.append(dict(entry)); continue
            changed_ids.append(rid)
            revision = (row['revision'] if row else 0) + 1
            if value is None:
                service.db.execute('DELETE FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?', (target_kind, rid, prop))
                revision = None
            else:
                service.db.execute('INSERT INTO ws_values VALUES(?,?,?,?,?) ON CONFLICT(target_kind,target_id,property_id) DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision',
                                   (target_kind, rid, prop, encode(value), revision))
            after.append({**entry, 'value': value, 'revision': revision})
        if created: service._put_def(prop, 'property', kind, 'custom', definition, 'enabled')
        return {'values': before, 'definition_created': False}, {'values': after, 'definition_created': created}, {
            'properties': [prop], 'core_ids': changed_ids,
            'dependencies': [target_kind + ':' + rid + ':' + prop for target_kind, rid in targets],
            'affected_count': len(changed_ids), 'affected_record_ids': changed_ids,
            'undo_token': token if changed_ids else None,
            'definition_created': created, 'undo_retains_definition': created}
    return service._commit('property.batch', 'property_batch', token, expected, apply)


def undo(service, params, expected):
    if set(params) != {'undo_token'} or not isinstance(params['undo_token'], str):
        raise KMInvalid('INVALID_VALUE', 'An undo token is required')
    token = params['undo_token']
    audit = service.db.execute("SELECT * FROM ws_audit WHERE operation='property.batch' AND subject_id=?", (token,)).fetchone()
    if not audit: return undo_legacy(service, token, expected)
    before, after = json.loads(audit['before_json']), json.loads(audit['after_json'])
    dependencies = set(json.loads(audit['dependencies_json']))

    def reverse():
        for later in service.db.execute('SELECT dependencies_json FROM ws_audit WHERE workspace_revision>?', (audit['workspace_revision'],)):
            if dependencies.intersection(json.loads(later[0])):
                raise KMInvalid('REVISION_CONFLICT', 'An affected value changed later; this action cannot be undone')
        changed = []
        for old, current in zip(before['values'], after['values']):
            key = (current['record_type'], current['record_id'], current['property_id'])
            row = service.db.execute('SELECT value_json,revision FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?', key).fetchone()
            observed = json.loads(row['value_json']) if row else None
            if observed != current['value'] or (row['revision'] if row else None) != current['revision']:
                raise KMInvalid('REVISION_CONFLICT', 'An affected value no longer matches this action')
            if old['value'] == current['value']: continue
            definition = service.property_definition(current['property_id'])
            if not definition or definition.get('status') != 'enabled':
                raise KMInvalid('CONFIGURATION_REQUIRED', 'Restore the property before undoing this action')
            if old['value'] is None:
                service.db.execute('DELETE FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?', key)
            else:
                value = service._validate_manual(definition, old['value'], key[0], key[1], allow_retained_refs=True)
                service.db.execute('INSERT INTO ws_values VALUES(?,?,?,?,?) ON CONFLICT(target_kind,target_id,property_id) DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision',
                                   (*key, encode(value), (row['revision'] if row else 0) + 1))
            changed.append(current['record_id'])
        return after, before, {'dependencies': sorted(dependencies), 'core_ids': changed,
                               'properties': list({item['property_id'] for item in before['values']}),
                               'affected_count': len(changed), 'affected_record_ids': changed,
                               'undo_token': None, 'definition_retained': after['definition_created']}
    return service._commit('property.undo', 'property_batch', token, expected, reverse)


def undo_legacy(service, token, expected):
    """Recover an outstanding Alpha4 undo token from its immutable audit evidence.

    The original metadata is no longer live state. Eligibility is established by
    the conversion audit plus subsequent value-command history, never by keeping
    a second disposition store in sync with ws_values.
    """
    from .starters import DISPOSITION_PROPERTIES
    audit = service.db.execute("SELECT * FROM ws_audit WHERE operation='mail.tasks.dismiss' AND subject_id=?", (token,)).fetchone()
    conversion = service.db.execute("SELECT * FROM ws_audit WHERE operation='view.starters.ensure' ORDER BY workspace_revision LIMIT 1").fetchone()
    if not audit or not conversion:
        raise KMInvalid('UNKNOWN_RECORD', 'This property action was not found')
    before, after = json.loads(audit['before_json']), json.loads(audit['after_json'])
    captured = {entry['key']:json.loads(entry['value']) for entry in json.loads(conversion['before_json']).get('legacy_dispositions',[])}
    if any(captured.get(key) != value for key,value in after.items()):
        raise KMInvalid('REVISION_CONFLICT', 'This earlier mail choice was already replaced before conversion')
    dependencies = {'Email:'+value['anchor_record_id']+':'+DISPOSITION_PROPERTIES[value['task']] for value in after.values()}

    def reverse():
        for later in service.db.execute('SELECT dependencies_json FROM ws_audit WHERE workspace_revision>?', (conversion['workspace_revision'],)):
            if dependencies.intersection(json.loads(later[0])):
                raise KMInvalid('REVISION_CONFLICT', 'An affected value changed after conversion')
        changed=[]; old_values=[]; new_values=[]
        for key,value in after.items():
            rid=value['anchor_record_id'];prop=DISPOSITION_PROPERTIES[value['task']]
            current=service.db.execute('SELECT value_json,revision FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?',('Email',rid,prop)).fetchone()
            if not current or json.loads(current['value_json']) is not True or current['revision'] != 1:
                raise KMInvalid('REVISION_CONFLICT','The converted mail choice has changed')
            prior=before[key] is not None
            old_values.append({'record_id':rid,'property_id':prop,'value':True})
            new_values.append({'record_id':rid,'property_id':prop,'value':True if prior else None})
            if not prior:
                service.db.execute('DELETE FROM ws_values WHERE target_kind=? AND target_id=? AND property_id=?',('Email',rid,prop))
                changed.append(rid)
        return {'legacy_token':token,'values':old_values}, {'legacy_token':token,'values':new_values,'undone':True}, {
            'dependencies':sorted(dependencies),'properties':sorted({DISPOSITION_PROPERTIES[value['task']] for value in after.values()}),
            'core_ids':changed,'affected_count':len(changed),'affected_record_ids':changed,'undo_token':None}
    return service._commit('property.undo_legacy','property_batch',token,expected,reverse)
