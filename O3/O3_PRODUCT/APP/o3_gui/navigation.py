"""Bounded presentation state. No Core/KM facts, queries or semantic rules.

Only stable references and a last location are persisted in workspace.json.
Full navigation history and labels are disposable, session-local UI state.
"""
from __future__ import annotations
from copy import deepcopy
import json
import re
from o3_core.store import KINDS

TARGETS = (*KINDS, 'WorkspaceOrganization')
SECTIONS = (*KINDS, 'overview', 'records', 'health', 'settings')
MAX_HISTORY = 80
MAX_RECENT = 24
MAX_PINS = 32
MAX_PAGE_ROWS = 1000
_TYPE_ID = re.compile(r'wsrt_[0-9a-f]{32}\Z')
_RECORD_ID = re.compile(r'wsrec_[0-9a-f]{32}\Z')

def _custom_type(value):
    return isinstance(value,str) and _TYPE_ID.fullmatch(value) is not None


def _text(value, limit=512):
    return value if isinstance(value, str) and len(value) <= limit and not any(ord(c) < 32 for c in value) else ''


def reference(value):
    """Reject malformed references; never infer/rebind identity from a label."""
    if not isinstance(value, dict):
        return None
    kind, rid = value.get('record_type'), _text(value.get('record_id'))
    if (kind not in TARGETS and not (_custom_type(kind) and _RECORD_ID.fullmatch(rid) is not None)) or not rid:
        return None
    return {'record_type': kind, 'record_id': rid}


def ref_key(value):
    value = reference(value)
    return (value['record_type'], value['record_id']) if value else None


def _references(values, limit):
    result, seen = [], set()
    for item in values[:256] if isinstance(values, list) else []:
        ref = reference(item)
        if ref and ref_key(ref) not in seen:
            seen.add(ref_key(ref)); result.append(ref)
            if len(result) == limit:
                break
    return result


def _safe_parameters(value):
    # Presentation selections only. KM still validates each advertised parameter.
    # Opaque snapshot cursors are deliberately never saved or replayed.
    if not isinstance(value, dict):
        return {}
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(text) > 8192:
            return {}
        result = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return {}
    for key in ('cursor', 'next_cursor', 'as_of', 'page_size', 'record_type', 'view_id'):
        result.pop(key, None)
    return result


def route(value=None):
    """Detached, bounded route, never a cached query result or graph."""
    value = value if isinstance(value, dict) else {}
    section = value.get('section', 'Email')
    if section not in SECTIONS and not _custom_type(section): section = 'Email'
    kind = value.get('kind', section if section in KINDS or _custom_type(section) else 'Email')
    if kind not in KINDS and not _custom_type(kind): kind = None if section=='records' else 'Email'
    tab = value.get('tab', 0)
    def integer(name, maximum):
        n = value.get(name, 0)
        return max(0, min(n, maximum)) if type(n) is int else 0
    return {'section': section, 'kind': kind,
            'view': _text(value.get('view'), 240) or 'all',
            'search': _text(value.get('search'), 160),
            'search_scope': 'current' if value.get('search_scope') == 'current' else 'all',
            'inactive': value.get('inactive') is True,
            'parameters': _safe_parameters(value.get('parameters')),
            'selected': reference(value.get('selected')),
            'preview': reference(value.get('preview')),
            'inline': reference(value.get('inline')),
            'anchor': {'record_id':_text((value.get('anchor') or {}).get('record_id')),
                       'offset':max(-10_000_000,min(10_000_000,(value.get('anchor') or {}).get('offset',0))) if type((value.get('anchor') or {}).get('offset',0)) is int else 0,
                       'scroll':integer('scroll',10_000_000),
                       'collapsed_groups':[_text(key,512) for key in (value.get('anchor') or {}).get('collapsed_groups',[])[:200] if isinstance(key,str)] if isinstance((value.get('anchor') or {}).get('collapsed_groups'),list) else []} if isinstance(value.get('anchor'),dict) else {},
            'map_edge': [_text(v) for v in value.get('map_edge', [])] if isinstance(value.get('map_edge'),list) and len(value['map_edge'])==3 else [],
            'map_x': integer('map_x', 10_000_000),
            'map_y': integer('map_y', 10_000_000),
            'map_mode': 'list' if value.get('map_mode')=='list' else 'map',
            'map_limit': max(4,integer('map_limit',200) or 12),
            'tab': tab if type(tab) is int and 0 <= tab < 6 else 0,
            'scroll': integer('scroll', 10_000_000),
            'page_index': integer('page_index', 10_000),
            'expanded': [[_text(k) for k in path[:12]] for path in value.get('expanded',[])[:1000]
                         if isinstance(path,list)] if isinstance(value.get('expanded'),list) else [],
            'inspector_scroll': integer('inspector_scroll', 10_000_000),
            'loaded': integer('loaded', MAX_PAGE_ROWS)}


def route_identity(value):
    """Scroll, tab and loaded rows update the same stop, not another visit."""
    r = route(value)
    return tuple(r[k] if k != 'parameters' else json.dumps(r[k], sort_keys=True)
                 for k in ('section', 'kind', 'view', 'search', 'search_scope', 'inactive', 'parameters')) + (ref_key(r['selected']), r['tab']==5)


def preferences(value=None):
    value = value if isinstance(value, dict) else {}
    return {'version': 1, 'last': route(value.get('last')),
            'pins': _references(value.get('pins'), MAX_PINS),
            'recent': _references(value.get('recent'), MAX_RECENT),
            'restore_last': value.get('restore_last', True) is not False}


class Navigation:
    """Browser-like Back/Forward and an explicitly nonsemantic visit trail."""
    def __init__(self, saved=None):
        saved = preferences(saved)
        self.pins, self.recent = saved['pins'], saved['recent']
        self.restore_last = saved['restore_last']
        self.entries = [saved['last'] if self.restore_last else route()]
        self.index = 0

    @property
    def current(self):
        return deepcopy(self.entries[self.index])

    @property
    def can_back(self): return self.index > 0

    @property
    def can_forward(self): return self.index + 1 < len(self.entries)

    def update(self, location):
        self.entries[self.index] = route(location)

    def visit(self, location):
        location = route(location)
        if route_identity(location) == route_identity(self.entries[self.index]):
            self.update(location); return False
        self.entries = self.entries[:self.index+1] + [location]
        self.entries = self.entries[-MAX_HISTORY:]
        self.index = len(self.entries)-1
        return True

    def move(self, delta):
        return self.jump(self.index + delta)

    def jump(self, index):
        if type(index) is not int or not 0 <= index < len(self.entries): return None
        self.index = index
        return self.current

    def seen(self, value):
        ref = reference(value)
        if ref:
            self.recent = [ref] + [r for r in self.recent if ref_key(r) != ref_key(ref)]
            self.recent = self.recent[:MAX_RECENT]

    def pinned(self, value): return ref_key(value) in {ref_key(r) for r in self.pins}

    def toggle_pin(self, value):
        ref = reference(value)
        if not ref: raise ValueError('Select a valid record to pin')
        if self.pinned(ref):
            self.pins = [r for r in self.pins if ref_key(r) != ref_key(ref)]; return False
        if len(self.pins) >= MAX_PINS:
            raise ValueError(f'The {MAX_PINS}-pin limit is reached. Unpin a record first.')
        self.pins.append(ref); return True

    def forget_recent(self):
        self.recent.clear()
        self.entries = [self.current]; self.index = 0

    def saved(self):
        return preferences({'pins': self.pins, 'recent': self.recent,
                            'last': self.current, 'restore_last': self.restore_last})


class LatestRead:
    """At most one running and one replacement presentation read.

    This is not an engine job queue. Writes never pass through it. The worker
    remains serialized; obsolete paints are suppressed while the newest read
    waits for that worker. A token prevents A→B→A responses repainting old data.
    """
    def __init__(self):
        self.serial = 0
        self.active = None
        self.waiting = None
        self.latest = 0

    def offer(self, payload):
        self.serial += 1; self.latest = self.serial
        item = {**deepcopy(payload), 'request_id': self.serial}
        if self.active is None:
            self.active = item
            return deepcopy(item)
        self.waiting = item
        return None

    def finish(self, token):
        if self.active is None or token != self.active['request_id']:
            return False, None
        accept = token == self.latest
        self.active = self.waiting; self.waiting = None
        return accept, deepcopy(self.active)

    def invalidate(self):
        # A queued Qt signal cannot be unsent. Its completion still drains the
        # active slot; only its paint and any queued replacement are discarded.
        self.serial += 1; self.latest = self.serial; self.waiting = None

    def reset(self):
        # Only after the serialized owner has entered/reopened maintenance.
        self.invalidate(); self.active = None
