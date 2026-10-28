"""Bounded display preferences and literal projections of existing record fields.

This module is deliberately Qt-free. ``workspace.json`` remains the preference
authority; these helpers validate its ``display`` object without opening a
database, interpreting a rule, or persisting a second model. Column order is
display order only. The existing query still owns the order of records.
"""
from __future__ import annotations
import re

from dataclasses import dataclass
import json

from o3_core.store import KINDS


DENSITY_NAMES = ('compact', 'balanced', 'comfortable')
DEFAULT_DENSITY = 'compact'


@dataclass(frozen=True)
class ColumnSpec:
    """Stable presentation key, visible label, logical width and default state."""
    key: str
    label: str
    width: int
    visible: bool = False


_STATE = ColumnSpec('state', 'Availability', 140)
_UPDATED = ColumnSpec('engine_updated', 'Updated in O3', 190)
_COLUMNS = {
    'all': (
        ColumnSpec('record', 'Item', 350, True),
        ColumnSpec('kind', 'Item type', 140, True), _STATE, _UPDATED,
    ),
    'Email': (
        ColumnSpec('record', 'Subject', 420, True),
        ColumnSpec('sender', 'Sender', 200, True),
        ColumnSpec('date', 'Message date', 190, True),
        ColumnSpec('received', 'Received', 210),
        ColumnSpec('folder', 'Mail folder', 105),
        ColumnSpec('sent', 'Sent', 184),
        ColumnSpec('direction', 'Incoming or sent', 130),
        ColumnSpec('importance', 'Importance', 145), _STATE, _UPDATED,
    ),
    'Event': (
        ColumnSpec('record', 'Event / start', 310, True),
        ColumnSpec('start', 'Start', 200, True),
        ColumnSpec('end', 'End', 200),
        ColumnSpec('location', 'Location', 220),
        ColumnSpec('status', 'Status', 130),
        ColumnSpec('recurrence', 'Repeating event', 140), _STATE, _UPDATED,
    ),
    'Person': (
        ColumnSpec('record', 'Person / role', 310, True),
        ColumnSpec('organization', 'Organization', 200, True),
        ColumnSpec('role', 'Role', 200),
        ColumnSpec('addresses', 'Known addresses', 260),
        ColumnSpec('department', 'Department', 180),
        ColumnSpec('location', 'Office location', 180), _STATE, _UPDATED,
    ),
    'Document': (
        ColumnSpec('record', 'Document / type', 310, True),
        ColumnSpec('extraction', 'Readable text', 150, True),
        ColumnSpec('file_type', 'File type', 110),
        ColumnSpec('origin', 'Where it came from', 160),
        ColumnSpec('source_modified', 'File modified', 190), _STATE, _UPDATED,
    ),
    'Organization': (
        ColumnSpec('record', 'Organization / kind', 350, True),
        ColumnSpec('organization_kind', 'Organization kind', 165, True), _STATE, _UPDATED,
    ),
    'LineOfEffort': (
        ColumnSpec('record', 'Project / reference', 350, True),
        ColumnSpec('reference_code', 'Reference', 160, True),
        ColumnSpec('loe_kind', 'Type', 140),
        ColumnSpec('namespace', 'Reference group', 190), _STATE, _UPDATED,
    ),
}


def columns_for(kind: str | None) -> tuple[ColumnSpec, ...]:
    """Return immutable definitions for a known type or ``None``/``all`` search."""
    key = 'all' if kind is None else kind
    if key not in _COLUMNS:
        if isinstance(key,str) and re.fullmatch(r'wsrt_[0-9a-f]{32}',key):
            return _COLUMNS['all']
        raise ValueError('Unknown record type for display columns')
    return _COLUMNS[key]


def column_preferences(kind: str | None, raw=None) -> dict:
    """Restore known keys only; keep Record visible and widths in logical bounds.

    Omitted preferences use current defaults. An intentionally empty ``visible``
    list hides optional columns, while malformed values use defaults. New column
    definitions are appended to a prior saved ordering without changing its order.
    """
    specs = columns_for(kind)
    defaults = {column.key: column for column in specs}
    keys = list(defaults)
    source = raw if isinstance(raw, dict) else {}
    requested_order = source.get('order')
    order = []
    if isinstance(requested_order, list):
        for key in requested_order:
            if isinstance(key, str) and key in defaults and key not in order:
                order.append(key)
    order.extend(key for key in keys if key not in order)
    requested_visible = source.get('visible')
    if isinstance(requested_visible, list):
        visible = {key for key in requested_visible if isinstance(key, str) and key in defaults}
    else:
        visible = {column.key for column in specs if column.visible}
    visible.add('record')
    widths = {}
    requested_widths = source.get('widths')
    requested_widths = requested_widths if isinstance(requested_widths, dict) else {}
    for key, column in defaults.items():
        value = requested_widths.get(key, column.width)
        if type(value) is not int:
            value = column.width
        minimum, maximum = (180, 1600) if key == 'record' else (80, 900)
        widths[key] = max(minimum, min(maximum, value))
    return {'order': order, 'visible': [key for key in order if key in visible], 'widths': widths}


def validate_display_preferences(raw=None) -> dict:
    """Return a detached, JSON-safe display object with safe older-config defaults."""
    source = raw if isinstance(raw, dict) else {}
    density = source.get('density')
    if not isinstance(density, str) or density not in DENSITY_NAMES:
        density = DEFAULT_DENSITY
    columns = source.get('columns')
    columns = columns if isinstance(columns, dict) else {}
    result={'density': density,
            'record_presentation': record_presentation_preferences(source.get('record_presentation')),
            'columns': {kind: column_preferences(kind, columns.get(kind))
                        for kind in ('all', *KINDS)}}
    views=source.get('views')
    if isinstance(views,dict):
        clean={}
        for key,value in list(views.items())[:256]:
            if (isinstance(key,str) and key.startswith('view:') and len(key)<=240 and
                    isinstance(value,dict) and (value.get('record_type') in (None,'all',*KINDS) or
                    (isinstance(value.get('record_type'),str) and re.fullmatch(r'wsrt_[0-9a-f]{32}',value['record_type'])))):
                clean[key]=view_layout(value.get('record_type'),value)
        if clean:result['views']=clean
    return result


# Native UI presentation settings only; no impact on Core/KM query semantics.
RECORD_MODES = ('table', 'cards')
ROW_DETAIL_LEVELS = ('compact', 'comfortable', 'detailed')
RECORD_SORTS = ('updated_desc', 'updated_asc', 'source_desc', 'source_asc',
                'sender_asc', 'sender_desc', 'subject_asc', 'subject_desc')


def record_presentation_preferences(raw=None):
    """Bounded per-Core-type GUI display choices with backward-compatible defaults.

    Sorting other than the KM-owned default is explicitly for currently loaded
    pages, not a second full-database query or persistent sort authority.
    """
    source = raw if isinstance(raw, dict) else {}
    result = {}
    for kind in ('all', *KINDS):
        item = source.get(kind)
        item = item if isinstance(item, dict) else {}
        mode = item.get('mode')
        table_detail = item.get('table_detail')
        card_detail = item.get('card_detail')
        order = item.get('sort')
        result[kind] = {
            'mode': mode if mode in RECORD_MODES else 'table',
            'table_detail': table_detail if table_detail in ROW_DETAIL_LEVELS else 'compact',
            'card_detail': card_detail if card_detail in ROW_DETAIL_LEVELS else 'comfortable',
            'sort': order if order in RECORD_SORTS else 'updated_desc',
        }
    return result


_MISSING = object()


def _text(value=_MISSING) -> str:
    if value is _MISSING or value is None:
        return 'Not supplied'
    if isinstance(value, str):
        return value if value.strip() else 'Empty'
    if isinstance(value, list):
        return '; '.join(_text(item) for item in value) if value else 'Empty'
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else 'Empty'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    return str(value)


def _get(mapping: dict, key: str):
    return mapping.get(key, _MISSING)


def _nested(mapping: dict, key: str) -> dict:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def _timestamp(value=_MISSING) -> str:
    """Only format an existing literal; never infer or convert its timezone."""
    if not isinstance(value, str) or not value.strip():
        return _text(value)
    if len(value) >= 11 and value[4:5] == '-' and value[7:8] == '-' and value[10:11] == 'T':
        value = value[:10] + ' ' + value[11:]
        if value.endswith('Z'):
            value = value[:-1] + ' UTC'
        elif value.endswith('+00:00'):
            value = value[:-6] + ' UTC'
    return value


def _event_time(value=_MISSING) -> str:
    if not isinstance(value, dict):
        return _timestamp(value)
    local = value.get('local', _MISSING)
    if value.get('kind') == 'date':
        return _text(local) + ' · all-day'
    if local is _MISSING:
        return _text(value)
    zone = value.get('timezone')
    label = _timestamp(local)
    if zone == 'floating' or zone is None:
        return label + ' · timezone not supplied'
    if zone == 'UTC':
        return label if label.endswith(' UTC') else label + ' UTC'
    label += ' · ' + _text(zone)
    if not value.get('utc'):
        label += ' · UTC unresolved'
    return label


def _sender(value=_MISSING) -> str:
    if not isinstance(value, dict):
        return _text(value)
    name, address = value.get('name'), value.get('address')
    if name and address:
        return f'{name} <{address}>'
    if name:
        return _text(name)
    if address:
        return _text(address)
    return 'Empty'


def project_list_fields(kind: str, properties: dict, added: dict, *, updated='', active=True) -> dict[str, str]:
    """Project display columns from supplied normalized/added values only.

    The query provides the primary ``label`` and ``secondary``; neither is
    regenerated here. Returned values are inert strings for native item text
    and tooltips, with no length truncation (visual delegates elide them).
    """
    columns_for(kind)  # Reject unknown kinds instead of silently presenting them.
    p = properties if isinstance(properties, dict) else {}
    a = added if isinstance(added, dict) else {}
    fields = {'kind': 'Line of Effort' if kind == 'LineOfEffort' else kind,
              'engine_updated': _timestamp(updated),
              'state': 'Active' if active else 'Retained inactive'}
    if kind == 'Email':
        fields.update(date=_timestamp(_get(p, 'source_time')),
                      sender=_sender(_get(p, 'from')),
                      folder=_text(_get(p, 'source_folder')),
                      received=_timestamp(_get(p, 'received_at_utc')),
                      sent=_timestamp(_get(p, 'sent_at_utc')),
                      direction=_text(_get(a, 'direction')),
                      importance=_text(_get(p, 'importance')))
    elif kind == 'Event':
        fields.update(start=_event_time(p.get('dtstart', a.get('start_utc', _MISSING))),
                      end=_event_time(p.get('dtend', a.get('end_utc', _MISSING))),
                      location=_text(_get(p, 'location')),
                      status=_text(_get(p, 'status')),
                      recurrence=_text(_get(a, 'recurrence_role')))
    elif kind == 'Person':
        fields.update(role=_text(_get(p, 'jobTitle')),
                      organization=_text(_get(p, 'companyName')),
                      addresses=_text(a.get('addresses', p.get('mail', p.get('userPrincipalName', _MISSING)))),
                      department=_text(_get(p, 'department')),
                      location=_text(_get(p, 'officeLocation')))
    elif kind == 'Document':
        source, extraction, origin = (_nested(p, name) for name in ('source', 'extraction', 'origin'))
        origin_type = origin.get('type', _MISSING)
        fields.update(file_type=_text(_get(source, 'file_type')),
                      extraction=_text(_get(extraction, 'status')),
                      origin=({'file': 'File', 'email_attachment': 'Email attachment'}.get(origin_type, _text(origin_type))
                              if isinstance(origin_type, str) else _text(origin_type)),
                      source_modified=_timestamp(_get(source, 'modified_utc')))
    elif kind == 'Organization':
        fields['organization_kind'] = _text(_get(p, 'kind'))
    elif kind == 'LineOfEffort':
        fields.update(reference_code=_text(_get(p, 'reference_code')),
                      loe_kind=_text(_get(p, 'kind')),
                      namespace=_text(_get(p, 'namespace')))
    return fields


# Per-view *presentation* remains in the existing workspace.json authority.
# Query meaning, filters, order of records and dependencies remain with KM.
MAX_VIEW_COLUMNS = 64


def view_columns_for(kind, fields=()):
    """Native column metadata. IDs, not user-facing labels, key saved preferences."""
    result=list(columns_for(kind));seen={x.key for x in result}
    for info in fields:
        key='field:'+str(info.get('field_id',''))
        if key in seen or key=='field:':continue
        seen.add(key)
        result.append(ColumnSpec(key,str(info.get('label') or 'Additional detail'),200))
    return tuple(result)


def _view_key(key):
    return isinstance(key,str) and 0<len(key)<=260 and not any(ord(c)<32 for c in key)


def view_layout(kind, raw=None):
    """Bounded persistence validation, independent of a loaded KM registry.

    Unknown field IDs are retained as references, not interpreted. A missing or
    retired field is marked unavailable by the renderer until the user removes
    it; it is never silently rebound to a similarly named property.
    """
    columns_for(kind)
    source=raw if isinstance(raw,dict) else {};column=source.get('columns',{})
    column=column if isinstance(column,dict) else {}
    defaults=column_preferences(kind)
    order=[]
    asked=column.get('order',defaults['order'])
    for key in (asked if isinstance(asked,list) else defaults['order']):
        if _view_key(key) and key not in order and len(order)<MAX_VIEW_COLUMNS:order.append(key)
    if 'record' not in order:order=['record',*order[:MAX_VIEW_COLUMNS-1]]
    visible=column.get('visible',defaults['visible'])
    visible=visible if isinstance(visible,list) else defaults['visible']
    visible=[key for key in order if key in visible or key=='record']
    widths={};asked_widths=column.get('widths',{})
    asked_widths=asked_widths if isinstance(asked_widths,dict) else {}
    for key in order:
        value=asked_widths.get(key,defaults['widths'].get(key,200))
        low,high=(180,1600) if key=='record' else (80,900)
        widths[key]=max(low,min(high,value)) if type(value) is int else defaults['widths'].get(key,200)
    density=source.get('density','inherit')
    if density not in ('inherit',*DENSITY_NAMES):density='inherit'
    return {'record_type':kind,'columns':{'order':order,'visible':visible,'widths':widths},
            'density':density,'secondary':source.get('secondary',True) is True}


def resolved_view_columns(kind, fields, layout):
    """Keep a small explicit configuration; append available columns for selection."""
    specs={x.key:x for x in view_columns_for(kind,fields)};prefs=view_layout(kind,layout)['columns']
    result=[]
    for key in prefs['order']:
        result.append(specs.get(key,ColumnSpec(key,'Unavailable field',200)))
    return tuple(result),prefs


def view_column_value(projected, key):
    """Inert formatting of KM-returned facts only; never evaluate a Workspace rule."""
    if key=='record':return str(projected.get('label','Untitled record'))
    if not key.startswith('field:'):return projected.get('fields',{}).get(key,'Not supplied')
    field=key[6:];record=projected.get('record',{})
    if field.startswith('ws:'):
        result=record.get('workspace_properties',{}).get(field)
        if result is None:return 'Unavailable'
        if result.get('state')!='evaluated':
            return {'unset':'Not set','disabled':'Disabled','not_evaluable':'Not evaluable',
                    'source_stale':'Source unavailable'}.get(result.get('state'),'Unavailable')
        value=result.get('value');links=projected.get('reference_labels',{})
        if isinstance(value,str) and value in links:return links[value]
        if isinstance(value,dict) and 'record_id' in value:return links.get(value['record_id'],value['record_id'])
        if isinstance(value,list) and any(isinstance(v,dict) and 'record_id' in v for v in value):
            return '; '.join(links.get(v['record_id'],v['record_id']) if isinstance(v,dict) and 'record_id' in v else _text(v) for v in value)
        return ('Yes' if value else 'No') if type(value) is bool else _text(value)
    if field in ('record_id','record_type','scope','updated_at'):value=record.get(field)
    else:value=record.get('properties',{}).get(field,record.get('additional',{}).get(field))
    if field in ('from','organizer'):return _sender(value)
    if isinstance(value,list):
        return '; '.join(_sender(x) if isinstance(x,dict) and ('address' in x or 'name' in x) else _text(x) for x in value) if value else 'Empty'
    if isinstance(value,dict) and 'local' in value:return _event_time(value)
    return _timestamp(value) if isinstance(value,str) else _text(value)



# Stable projection-to-query mappings. This is display metadata only: the KM
# field catalog still decides whether a field supports sorting.
RECORD_LIST_SORT_FIELDS = {
    'Email': {'record':'subject','sender':'from','date':'source_time','received':'received_at_utc','sent':'sent_at_utc',
              'folder':'source_folder','direction':'direction','importance':'importance'},
    'Person': {'record':'displayName','organization':'companyName','role':'jobTitle',
               'department':'department','location':'officeLocation'},
    'Document': {'record':'title','file_type':'file_type','source_modified':'source_modified'},
    'Event': {'record':'summary','start':'start_utc','end':'end_utc','location':'location','status':'status'},
    'LineOfEffort': {'record':'name','reference_code':'reference_code','loe_kind':'kind','namespace':'namespace'},
    'Organization': {'record':'name','organization_kind':'kind'},
}


def record_list_columns(kind, fields=(), layout=None):
    """Return RecordList column descriptors and normalized presentation state.

    Sorts reference canonical fields, never formatted dates or rendered labels.
    If a catalog was supplied, unsupported fields are explicitly non-sortable.
    """
    specs,prefs=resolved_view_columns(kind,fields,layout)
    known={item['field_id']:item for item in fields}
    columns=[]
    for spec in specs:
        field=spec.key[6:] if spec.key.startswith('field:') else RECORD_LIST_SORT_FIELDS.get(kind,{}).get(spec.key)
        if spec.key=='engine_updated':field='updated_at'
        if spec.key=='kind':field='record_type'
        if spec.key=='record' and isinstance(kind,str) and kind.startswith('wsrt_'):field='title'
        info=known.get(field,{})
        supported=bool(field) and (not known or field in known) and info.get('sortable',True) is not False
        columns.append({'key':spec.key,'label':spec.label,'width':prefs['widths'].get(spec.key,spec.width),
                        'sortable':supported,'sort_field':field if supported else None,
                        'property_id':field if isinstance(field,str) and field.startswith('ws:') else None,
                        'value_type':info.get('value_type'),'mode':info.get('mode'),'visible':spec.key in prefs['visible']})
    return columns,prefs


def record_list_rows(projected_rows, columns, *, notices=None):
    """Shape already queried display_row results without changing their order."""
    result=[]
    for shown in projected_rows:
        rid=shown.get('id',shown.get('record_id'))
        if not isinstance(rid,str):continue
        cells={}
        for column in columns:
            key=column['key'];value=view_column_value(shown,key)
            field=column.get('property_id')
            if field:
                state=(shown.get('workspace_properties') or shown.get('record',{}).get('workspace_properties',{})).get(field,{})
                cells[key]={'label':value,'property_id':field,'value_type':column.get('value_type'),
                    'state':state.get('state','unavailable'),'editable':column.get('mode')=='manual'}
            elif key=='sender' and shown.get('kind')=='Email':
                sender=shown.get('record',{}).get('properties',{}).get('from')
                cells[key]={'label':str(sender['name']),'tooltip':value,'value_type':'email'} if isinstance(sender,dict) and sender.get('name') else value
            else:cells[key]=value
        subtitle=shown.get('secondary','')
        if shown.get('kind')=='Email' and any(c['key']=='sender' and c.get('visible',True) for c in columns):subtitle=''
        result.append({'record_id':rid,'record_type':shown.get('target_kind',shown.get('kind')),
            'title':shown.get('label','Untitled item'),'subtitle':subtitle,
            'kind':shown.get('target_kind',shown.get('kind')),'cells':cells,
            'notice':(notices or {}).get(rid,''),'projected':shown})
    return result
