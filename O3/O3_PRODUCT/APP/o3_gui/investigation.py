"""Read-only evidence and exact-difference presentation, independent of Qt.

These functions organize data already returned by KM; they assert no relations,
classifications, source completeness, historical events or corrected facts.
"""
from __future__ import annotations
from copy import deepcopy
import json

MAX_ENTRIES = 200
MAX_DIFFS = 500
MAX_VISITS = 8000
MISSING = object()


def plain(value, limit=20000):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f'\n[Display limited to {limit:,} characters.]'
    parts=[];length=0
    for part in json.JSONEncoder(indent=2,ensure_ascii=False,default=str).iterencode(value):
        remaining=limit-length
        if len(part)>remaining:
            parts.append(part[:remaining]);return ''.join(parts)+f'\n[Display limited to {limit:,} characters.]'
        parts.append(part);length+=len(part)
    return ''.join(parts)


def decode(value):
    if isinstance(value, str):
        try: return json.loads(value)
        except (ValueError, RecursionError): return value
    return deepcopy(value)


def comparison_record(record):
    """Do not conflate normalized, rule-added, and Workspace-owned fields."""
    identity={k:record[k] for k in ('record_id','record_type','active','identity_basis','scope','effective_kind','authority') if k in record}
    if record.get('effective_kind')=='workspace_overlay':
        return {'identity':identity,'workspace_manual':{
            **{k:deepcopy(record[k]) for k in ('display_name','core_org_ids') if k in record},
            'fields':deepcopy(record.get('properties',{}))}}
    return {'identity':identity,
            'core':deepcopy(record.get('properties',{})),
            'core_rule_added':deepcopy(record.get('additional',record.get('added',{}))),
            'workspace':deepcopy(record.get('workspace_properties',{}))}


def differences(before, after, *, limit=MAX_DIFFS):
    """JSON Pointer field paths; missing, null, false and 0 remain distinct.

    Arrays are compared as ordered values, never silently sorted or interpreted.
    The display is bounded; callers must display `truncated`, not claim equality
    from a partial comparison. Original data remains available read-only.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_DIFFS:
        raise ValueError('Difference display limit is outside its bounds')
    changes = []; visits = 0; truncated = False
    def walk(left, right, path, depth=0):
        nonlocal visits, truncated
        visits += 1
        if visits > MAX_VISITS or depth > 32:
            truncated = True; return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right), key=str):
                escaped = str(key).replace('~', '~0').replace('/', '~1')
                walk(left.get(key, MISSING), right.get(key, MISSING), path+'/'+escaped, depth+1)
                if truncated: break
            return
        if left is not MISSING and right is not MISSING:
            # Canonical JSON comparison avoids Python's bool==int conflation.
            if json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False): return
        if left is MISSING and right is MISSING: return
        if len(changes)>=limit:
            truncated=True;return
        changes.append({'path': path or '/', 'change': 'added' if left is MISSING else 'removed' if right is MISSING else 'changed',
                        'before_present': left is not MISSING, 'after_present': right is not MISSING,
                        'before': None if left is MISSING else deepcopy(left),
                        'after': None if right is MISSING else deepcopy(right)})
    walk(before, after, '')
    return {'changes': changes, 'truncated': truncated, 'visited_fields': visits}


def core_snapshots(history, offset=0, limit=None):
    rows = (history.get('core_trace') or {}).get('record_history', [])
    rows = rows[offset:] if limit is None else rows[offset:offset+limit]
    result = []
    for row in rows:
        if not isinstance(row, dict): continue
        result.append({'label': f"Core #{row.get('event_id', '?')} · {row.get('changed_at', '')} · {row.get('change_type', '')}",
                       'baseline': row.get('change_type') == 'baseline_v1', 'raw': row,
                       'record_id': row.get('record_id'),
                       'snapshot': {'active': None if row.get('active') is None else bool(row['active']),
                                    'provenance':{k:row[k] for k in ('source_sha','fingerprint','record_type') if k in row},
                                    'core': decode(row.get('properties_json', '{}')),
                                    'core_rule_added': decode(row.get('added_json', '{}'))}})
    return result


def historical_difference(history, selected_index):
    rows = (history.get('core_trace') or {}).get('record_history', [])
    if type(selected_index) is not int or not 0 <= selected_index < len(rows):
        return {'available': False, 'reason': 'No captured Core snapshot is selected.'}
    snapshots=core_snapshots(history,max(0,selected_index-1),2)
    selected = snapshots[-1] if selected_index else snapshots[0]
    if selected_index == 0 or selected['baseline']:
        return {'available': False, 'reason': 'No earlier captured snapshot. A migration baseline is not reconstructed history.', 'selected': selected}
    prior = snapshots[0]
    if prior['record_id'] != selected['record_id']:
        return {'available': False, 'reason': 'Snapshots belong to different record identities.'}
    return {'available': True, 'prior': prior, 'selected': selected,
            **differences(prior['snapshot'], selected['snapshot'])}


def evidence_entries(detail, limit=MAX_ENTRIES, offset=0):
    """Return a bounded Lens index while preserving authority and owner claims."""
    if type(limit) is not int or not 1<=limit<=MAX_ENTRIES or type(offset) is not int or offset<0:
        raise ValueError('Evidence page is outside its bounds')
    rows = []; total = 0
    def add(group, title, authority, data, ref=None, sha=None):
        nonlocal total
        total += 1
        if offset < total <= offset + limit:
            rows.append({'group': group, 'title': str(title), 'authority': authority, 'data': data,
                         'reference': ref, 'source_sha': sha})
    rid, kind = detail.get('id'), detail.get('target_kind', detail.get('kind'))
    trace = detail.get('trace') or {}
    traces = [trace] if trace.get('source_observations') or trace.get('current_derived_support') else []
    for child in detail.get('km_evidence', {}).get('core_organizations', []):
        if isinstance(child, dict): traces.append(child)
    for source in traces:
        owner = {'record_id': source.get('record_id', rid), 'record_type': source.get('record_type', kind)}
        for item in source.get('source_observations', []):
            add('Source observations', item.get('observed_at', 'Observed source'), 'Core source observation', item,
                owner, item.get('source_sha'))
        for item in source.get('current_derived_support', []):
            add('Derived support', item.get('producer', 'Source producer'), 'Core producer evidence', item,
                {'record_id': item.get('source_record_id'), 'record_type': None}, item.get('source_sha'))
    for edge in detail.get('relationships', []):
        title = f"{edge.get('source')} → {edge.get('predicate')} → {edge.get('target')}"
        authority = edge.get('authority', 'core')
        add('Current connections', title, authority, edge,
            {'record_id': edge.get('other'), 'record_type': edge.get('other_target_kind', edge.get('other_type'))})
        for claim in edge.get('evidence', []):
            add('Connection claims', str(claim.get('owner', 'Owner claim'))+' · '+edge.get('predicate', ''), authority, claim)
    for operation in detail.get('km_history', {}).get('workspace_audit', []):
        add('Manual / configuration history', f"{operation.get('operation', 'Operation')} · revision {operation.get('workspace_revision', '?')}",
            'Audited user operation (historical)', operation)
    for evaluation in trace.get('rule_evaluations', []):
        add('Core rule evaluations', f"Evaluation #{evaluation.get('run_id', '?')}", 'Captured Core rule output', evaluation)
    rules = detail.get('km_rules', {}).get('workspace_results', {})
    for prop, result in rules.items():
        add('Workspace evaluations', prop+' · '+str(result.get('state', '')), 'On-demand Workspace result; not historical truth', result)
    return {'rows': rows, 'total': total, 'offset': offset, 'truncated': total > len(rows),
            'has_next': offset + len(rows) < total}


def lens_match(entry, query):
    # Filter the displayed index only; never an alternate knowledge search.
    words = str(query).casefold().split()
    text = ' '.join(str(entry.get(k, '')) for k in ('group', 'title', 'authority')).casefold()
    return all(word in text for word in words)


def same_snapshot(first, second):
    return all(first.get(k) is not None and first.get(k) == second.get(k)
               for k in ('workspace_id', 'workspace_revision', 'core_epoch', 'as_of'))
