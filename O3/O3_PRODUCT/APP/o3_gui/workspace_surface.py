"""Shared record-list presentation and explicit action targets.

All membership, sorting, evidence and writes are dispatched to the owning KM
worker. This adapter formats returned values and never evaluates a view.
"""
from __future__ import annotations
import copy
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QMenu
from .record_list import RecordList
from .display_preferences import resolved_view_columns, view_column_value, record_list_columns, record_list_rows
from .km_session import display_row
from .record_content import overview_data, address_value

def list_schema_fields(schema):
    """Combine the catalog's display authority and supported ordering metadata."""
    fields={value['field_id']:dict(value) for value in schema.get('fields',[])}
    options=schema.get('view_options',{})
    for value in options.get('column_fields',[]):
        fields[value['field_id']]={**fields.get(value['field_id'],{}),**value}
    ordering={value['field_id'] for value in options.get('ordering_fields',[])}
    for key,value in fields.items():value['sortable']=key in ordering
    return list(fields.values())

class RecordSurface(QObject):
    """Binds a RecordList to its captured query/selection, not reader globals."""
    def __init__(self,host,parent=None):
        super().__init__(parent or host)
        self.host=host;self.widget=RecordList(parent or host)
        self.kind=None;self.view_id='all';self.projected={};self.fields=[];self.layout={};self.property_definitions={}
        self.restore_state=None;self.pending={};self.details={};self.serial=0;self.sort_callback=None;self.exclude_callback=None
        self.widget.detail_requested.connect(self.load_detail)
        self.widget.open_requested.connect(lambda rid:host.open_record(rid,self.kind_for(rid)))
        self.widget.context_requested.connect(self.context_menu)
        self.widget.sort_requested.connect(self.sort_changed)
        self.widget.layout_changed.connect(self.layout_changed)
        self.widget.action_requested.connect(self.action)
        self.widget.drop_requested.connect(self.drop)

    def kind_for(self,rid):return self.projected.get(rid,{}).get('target_kind') or self.host.guess_kind(rid)
    def set_result(self,kind,view_id,records,fields=(),*,sorts=(),notices=None,group_counts=None):
        changed=(kind,view_id)!=(self.kind,self.view_id)
        if changed:self.widget.collapse()
        self.kind,self.view_id=kind,view_id;self.fields=list(fields)
        self.layout=self.host.get_view_layout(kind,view_id)
        columns,prefs=record_list_columns(kind,fields,self.layout)
        self.sort_fields={column['key']:column['sort_field'] for column in columns if column.get('sortable')}
        self.widget.set_columns(columns);self.widget.set_layout(self.layout)
        density=self.layout.get('density','inherit');self.widget.set_density(self.host.appearance['density'] if density=='inherit' else density)
        self.projected={}
        for raw in records:
            shown=raw if 'id' in raw and 'label' in raw else display_row(raw.get('record') or raw)
            rid=shown['id'];self.projected[rid]=shown;self.host.record_kinds[rid]=shown['target_kind']
        rows=record_list_rows(self.projected.values(),columns,notices=notices)
        for row in rows:
            shown=self.projected[row['record_id']]
            row['group_record_ids']=shown.get('record',{}).get('group_record_ids',[])
            grouping=shown.get('record',{}).get('view_group')
            if grouping:row['view_group']={**grouping,'total_count':(group_counts or {}).get(grouping['key'])}
            for key in ('date','received','sent','engine_updated'):
                value=shown.get('updated') if key=='engine_updated' else shown.get('record',{}).get('properties',{}).get({'date':'source_time','received':'received_at_utc','sent':'sent_at_utc'}[key])
                if value and key in row['cells']:row['cells'][key]=self.host.local_stamp(value,True)
        self.widget.set_rows(rows,preserve_anchor=not changed)
        if self.widget.expanded_id:self.widget.refresh_detail()
        self.widget.set_drop_targets({rid:{'label':'Add to '+row['label'],'action':'associate_project'} for rid,row in self.projected.items() if row['target_kind']=='LineOfEffort'})
        if sorts:
            key=next((key for key,field in self.sort_fields.items() if field==sorts[0]['field']),None)
            if key:self.widget.set_sort(key,sorts[0].get('direction','asc'))

    def checkpoint(self):
        rid=self.widget.expanded_id
        return {'anchor':self.widget.save_anchor(),
                'inline':{'record_id':rid,'record_type':self.kind_for(rid)} if rid else None}
    def restore(self,route):
        wanted=(route.get('preview') or {}).get('record_id')
        if wanted:self.widget.select_record(wanted)
        self.restore_state=copy.deepcopy(route.get('anchor') or {'scroll':route.get('scroll',0)})
        expanded=(route.get('inline') or {}).get('record_id')
        if expanded and expanded in self.projected and self.widget.expanded_id!=expanded:self.widget.toggle(expanded)
        anchor=copy.deepcopy(self.restore_state)
        # Group disclosure is restored once. A later reader response must not
        # replay old group state over a user's subsequent expand/collapse.
        self.restore_state={key:value for key,value in anchor.items() if key!='collapsed_groups'} if expanded and expanded in self.projected else None
        QTimer.singleShot(0,lambda:self.widget.restore_anchor(anchor))

    def selected_refs(self,ids=None):
        return [{'record_type':self.kind_for(rid),'record_id':rid,'label':self.projected.get(rid,{}).get('label','Selected item'),'group_record_ids':self.projected.get(rid,{}).get('record',{}).get('group_record_ids',[])} for rid in (ids if ids is not None else self.widget.selected_ids())]
    def context(self,ids=None):
        from .interaction_actions import SelectionContext
        return SelectionContext.from_rows(self.selected_refs(ids),view_id=self.view_id,revision=self.host.km.revision)
    def load_detail(self,rid,request_id):
        generation=self.host.route_generation
        def done(data):
            if generation!=self.host.route_generation:return
            if data.get('read_error'):
                self.widget.set_detail(rid,{'notice':'This item could not be read. '+data['read_error']},request_id);return
            self.details[rid]=data;self.paint_detail(rid,request_id)
            if data.get('workspace_properties'):
                def properties(response):
                    if generation!=self.host.route_generation or not response.get('ok'):return
                    self.property_definitions={p['property_id']:p for p in response['result'].get('workspace_properties',[]) if p.get('property_id')}
                    self.paint_detail(rid,request_id)
                self.host.km.request('registry.properties',{'record_type':data['target_kind']},properties,owner=self)
        self.host.request_inline(rid,self.kind_for(rid),done)
    def paint_detail(self,rid,token):
        if self.widget.expanded_id!=rid:return
        detail=self.details[rid];content=detail.get('content');overview=overview_data(detail)
        body=content.get('text','') if content else '\n\n'.join(title+'\n'+'\n'.join(label+'  '+str(value) for label,value in values) for title,values in overview.get('sections',[]))
        notice=(content.get('notice','') if content.get('status')!='available' else '') if content else ' · '.join(overview.get('notices',[]))
        actions=[{'id':'properties','label':'Edit properties'},{'id':'connections','label':'Connections'},{'id':'evidence','label':'Source details'},{'id':'open','label':'Open in full'}]
        if detail['kind']=='Email':actions.insert(0,{'id':'draft','label':'Draft reply'})
        if content and content.get('next_offset') is not None:actions.insert(0,{'id':'more_text','label':'Continue reading'})
        widgets=[]
        for pid,result in detail.get('workspace_properties',{}).items():
            definition=self.property_definitions.get(pid)
            if not definition or result.get('state') in ('unset','disabled','retired','not_set') or result.get('value') in (None,[]):continue
            display_result=copy.deepcopy(result)
            labels={edge['other']:edge.get('other_label') for edge in detail.get('relationships',[]) if edge.get('other')}
            def named(value):
                if isinstance(value,dict) and value.get('record_id') in labels:return {**value,'label':labels[value['record_id']]}
                if isinstance(value,list):return [named(item) for item in value]
                return value
            display_result['value']=named(display_result.get('value'))
            widgets.append(self.host.record_actions.property_chip(self.context([rid]),definition,display_result))
            if len(widgets)>=8:break
        self.widget.set_detail(rid,{'widgets':widgets,'title':detail['title'],'subtitle':detail['secondary'],'body':body,
            'notice':notice,'actions':actions},token)
        if self.restore_state:
            anchor=self.restore_state;self.restore_state=None
            QTimer.singleShot(0,lambda:self.widget.restore_anchor(anchor))
    def action(self,name,ids,payload=None):
        ids=list(ids or self.widget.selected_ids())
        if name=='undo':self.host.record_actions.undo();return
        if name=='configure_view':
            if self.view_id.startswith('view:project-correspondence:'):self.host.configure_surface_columns(self)
            else:self.host.open_view_editor(self.kind or 'Email',self.view_id)
            return
        if name=='open_link':
            self.host.show_information('Link in saved content',str(payload));return
        if not ids:return
        if name=='edit_property':
            pid=payload.get('property_id') if isinstance(payload,dict) else payload
            from PySide6.QtGui import QCursor
            self.host.record_actions.edit_inline(self.context(ids),pid,QCursor.pos());return
        rid=ids[0]
        if name in ('open','open_full'):self.host.open_record(rid,self.kind_for(rid))
        elif name=='connections':self.host.open_spatial(rid)
        elif name=='evidence':
            if rid in self.details:self.host.evidence_dialog(self.details[rid])
            else:self.host.open_evidence(rid)
        elif name=='properties':self.host.record_actions.edit_properties(self.context(ids))
        elif name=='exclude' and self.exclude_callback:self.exclude_callback(rid)
        elif name=='draft':
            detail=self.details.get(rid)
            if detail:self.host.draft_reply(detail)
        elif name=='more_text':
            detail=self.details.get(rid);content=(detail or {}).get('content') or {}
            if content.get('next_offset') is None:return
            original=content.get('source_sha');generation=self.host.route_generation
            def loaded(data):
                if generation!=self.host.route_generation or rid not in self.details:return
                if data.get('read_error') or data.get('source_sha')!=original:
                    self.host.show_information('Reading interrupted','The saved content changed or could not be read. Reopen this item to continue.',True);return
                merged={**data,'text':content.get('text','')+data.get('text','')}
                detail['content']=merged;self.paint_detail(rid,None)
            self.host.request_inline(rid,self.kind_for(rid),loaded,operation='content',offset=content['next_offset'],limit=200000)
    def sort_changed(self,key,direction):
        field=self.sort_fields.get(key)
        if field and self.sort_callback:self.sort_callback(field,direction)
    def layout_changed(self,value):
        if not self.projected:return
        # Row widget state is presentation only. Filter and sort definitions live in KM.
        value={**self.layout,'columns':value,'record_type':self.kind}
        try:self.host.persist_view_layout(self.kind,self.view_id,value)
        except (OSError,ValueError):pass
    def context_menu(self,rid,ids,position):
        context=self.context(ids or [rid])
        menu=self.host.record_actions.create_menu(context,allow_exclude=False)
        menu.addSeparator()
        menu.addAction('See connections',lambda:self.host.open_spatial(rid))
        if self.exclude_callback and self.kind:menu.addAction("Don’t show items like this here…",lambda:self.exclude_callback(rid))
        menu.exec(position)
    def drop(self,ids,target,payload):
        if self.kind_for(target)=='LineOfEffort':
            from .interaction_actions import SelectionContext
            context=SelectionContext.from_rows((payload or {}).get('records') or self.selected_refs(ids),view_id=self.view_id,revision=self.host.km.revision)
            self.host.record_actions.associate_project(context,{'record_type':'LineOfEffort','record_id':target})
