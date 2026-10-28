"""Bounded presentation helpers for Workspace editors; never evaluates a rule.

Stable IDs and typed expression payloads remain unchanged. KM is the only
validation/evaluation authority. Undo history exists only while a form is open.
"""
from __future__ import annotations
from copy import deepcopy
import json
import re

TYPE_LABELS = {'boolean':'Yes / no','text':'Text','enum':'Choice list',
               'date':'Date','record_ref':'One linked record','record_ref_set':'Linked records'}
GROUP_LABELS = {'all':'All of these conditions', 'any':'Any of these conditions',
                'not':'Not this condition'}
OPERATOR_LABELS = {
    'equals':'is','not_equals':'is not','is_present':'has a value',
    'one_of':'is one of','contains':'contains text','starts_with':'starts with',
    'ends_with':'ends with','local_date_age_gt':'is older than (calendar days)',
    'local_date_age_lte':'is at most this many calendar days old',
    'list_count_equals':'has exactly this many entries','list_count_at_least':'has at least this many entries',
    'list_count_at_most':'has at most this many entries','list_contains':'contains an entry',
    'email_matches':'matches address','email_matches_any':'matches any address',
    'email_list_contains':'includes address','reference_in_selected':'is a selected record',
    'verified_path_to_selected':'has a verified link to selected records',
    'direct':'meets the Direct correspondence test','reply_observed':'has captured reply',
    'external_recipient_visible':'has a visible non-self recipient',
    'incoming_reply_missing':'has no captured incoming reply',
    'includes_self_address':'includes your configured address',
    'includes_group_address':'includes a configured group address',
    'event_owner_response':'has a source-confirmed pending response',
}

def humanize(value):
    text=re.sub(r'([a-z])([A-Z])',r'\1 \2',str(value)).replace('_',' ')
    return text[:1].upper()+text[1:]

def field_label(field, fields):
    info=fields.get(field,{})
    return str(info.get('label') or info.get('display_label') or humanize(field))

def value_text(value, *, limit=90):
    if value is None:return 'Not set'
    if type(value) is bool:return 'Yes' if value else 'No'
    if isinstance(value,list):
        text=', '.join(value_text(x,limit=40) for x in value[:5])
        if len(value)>5:text+=f' (+{len(value)-5} more)'
    elif isinstance(value,dict):text=str(value.get('label') or value.get('record_id') or 'Linked record')
    else:text={'replied':'Reply found','no_reply_found':'No reply found','unknown':'Couldn’t determine'}.get(value,str(value)) if isinstance(value,str) else str(value)
    return text if len(text)<=limit else text[:limit-1]+'…'

def condition_text(node, fields):
    group=next((k for k in GROUP_LABELS if k in node),None)
    if group:return GROUP_LABELS[group]
    operator=node.get('operator','')
    text=field_label(node.get('field','Choose a field'),fields)+' '+OPERATOR_LABELS.get(operator,humanize(operator))
    if 'value' in node:text+=' · '+value_text(node['value'])
    return text

def expression_lines(expression, fields, depth=0):
    lines=['  '*depth+condition_text(expression,fields)]
    group=next((key for key in GROUP_LABELS if key in expression),None)
    if group:
        children=[expression[group]] if group=='not' else expression[group]
        for child in children:lines.extend(expression_lines(child,fields,depth+1))
    return lines

def expression_size(expression):
    group=next((key for key in GROUP_LABELS if key in expression),None)
    if not group:return 1,1
    children=[expression[group]] if group=='not' else expression[group]
    sizes=[expression_size(x) for x in children]
    return sum(x[0] for x in sizes),1+max((x[1] for x in sizes),default=0)

def check_draft_bounds(expression, limits):
    """Bound local tree editing using advertised limits, not semantic evaluation.

    Incomplete/empty groups are allowed while drafting. KM still owns the final
    shape/type/reference validation. The node cap also bounds empty-group drafts.
    """
    leaf_limit=limits.get('max_leaves',32);depth_limit=limits.get('max_depth',5)
    children_limit=limits.get('max_group_children',32)
    leaves=nodes=0;stack=[(expression,1)]
    while stack:
        node,depth=stack.pop();nodes+=1
        if depth>depth_limit or nodes>leaf_limit*depth_limit+1:
            raise ValueError(f'Keep the draft within {leaf_limit} conditions and {depth_limit} levels.')
        group=next((key for key in GROUP_LABELS if key in node),None)
        if group:
            children=[node[group]] if group=='not' else node[group]
            if len(children)>children_limit:raise ValueError(f'A group supports at most {children_limit} entries.')
            stack.extend((child,depth+1) for child in children)
        else:
            leaves+=1
            if leaves>leaf_limit:raise ValueError(f'This draft supports at most {leaf_limit} conditions.')


def result_text(result):
    if result.get('state')=='evaluated':return value_text(result.get('value'))
    return {'not_evaluable':'Not evaluable','source_stale':'Source unavailable',
            'disabled':'Disabled','unset':'Not set'}.get(result.get('state'),humanize(result.get('state','Unknown')))

def signature(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))

class EditHistory:
    """At most 50 detached snapshots of a small, bounded expression document."""
    def __init__(self, expression, limit=50):
        if type(limit) is not int or not 2<=limit<=100:raise ValueError('Invalid history bound')
        self.limit=limit;self.items=[deepcopy(expression)];self.index=0
    @property
    def can_undo(self):return self.index>0
    @property
    def can_redo(self):return self.index<len(self.items)-1
    def current(self):return deepcopy(self.items[self.index])
    def push(self, expression):
        if expression==self.items[self.index]:return False
        self.items=self.items[:self.index+1]+[deepcopy(expression)]
        if len(self.items)>self.limit:self.items=self.items[-self.limit:]
        self.index=len(self.items)-1;return True
    def undo(self):
        if self.can_undo:self.index-=1
        return self.current()
    def redo(self):
        if self.can_redo:self.index+=1
        return self.current()
