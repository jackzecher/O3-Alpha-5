"""Mail uses ordinary editable KM views and the shared record surface."""
from __future__ import annotations
import copy
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QMenu
from .ui import text,btn
from .workspace_surface import RecordSurface,list_schema_fields

TASKS={'unanswered':'Unanswered','waiting':'Waiting for replies','all':'All mail'}
STARTERS={'unanswered':'view:user:starter-unanswered','waiting':'view:user:starter-waiting','all':'view:user:starter-all-mail'}
SUBTITLES={'unanswered':'A clear view of the conversations that may need you.',
    'waiting':'Keep sight of the replies you are waiting for.','all':'Your saved conversations, together in one place.'}

def mail_row_notice(row):
    match=row.get('view_match') or {}
    return ('Reply evidence is incomplete' if (row.get('reply_evidence') or {}).get('status')=='unknown' else 'Some rule values are unavailable') if match.get('state')=='unknown' else ''

class MailWorkspace(QWidget):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.task='unanswered';self.serial=0
        self.rows={};self.cursor=None;self.previous=[];self.next_cursor=None;self.payload={}
        self.undo_token=None;self.ready=False;self.options={};self.fields=[];self.view_id=STARTERS[self.task]
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(12)
        heading=QHBoxLayout();self.heading=text('Mail','title',True);heading.addWidget(self.heading,1)
        self.check_button=btn('Check for updates',host.scan_sources,kind='secondary');heading.addWidget(self.check_button);layout.addLayout(heading)
        self.subtitle=text(SUBTITLES[self.task],'muted',True);layout.addWidget(self.subtitle)
        tabs=QHBoxLayout();self.tabs={}
        for task,label in TASKS.items():
            button=btn(label,lambda checked=False,value=task:host.open_mail(value),kind='secondary');button.setCheckable(True);self.tabs[task]=button;tabs.addWidget(button)
        tabs.addStretch()
        self.result_count=text('','small',True);heading.insertWidget(1,self.result_count)
        options=tabs
        self.saved_button=btn('Saved views',self.saved_views,kind='link');options.addWidget(self.saved_button)
        self.personalize_button=btn('Configure',self.customize,'sliders','secondary');options.addWidget(self.personalize_button);layout.addLayout(tabs)
        self.setup=QWidget();setup=QVBoxLayout(self.setup);setup.setContentsMargins(20,20,20,20)
        setup.addWidget(text('Bring your mail into O3','section'))
        setup.addWidget(text('Choose your O3 export folder. O3 works with these saved files; your original mail stays unchanged.','muted',True))
        setup.addWidget(btn('Choose mail folder',host.choose_source,'sources','primary'),0,Qt.AlignLeft);layout.addWidget(self.setup)
        self.identity=QWidget();identity=QHBoxLayout(self.identity);identity.setContentsMargins(0,0,0,0)
        self.identity_message=text('Confirm your addresses to check for replies.','muted',True);identity.addWidget(self.identity_message,1)
        identity.addWidget(btn('Your email addresses',host.setup_review,kind='link'));layout.addWidget(self.identity)
        self.surface=RecordSurface(host,self);self.listing=self.surface.widget;self.surface.sort_callback=self.sort_changed;self.surface.exclude_callback=self.exclude
        self.listing.selection_changed.connect(self.selection_changed);layout.addWidget(self.listing,1)
        self.empty=text('','muted',True);layout.addWidget(self.empty)
        bottom=QHBoxLayout();self.previous_button=btn('Previous',self.page_back,kind='link');bottom.addWidget(self.previous_button)
        self.next_button=btn('Next',self.page_next,kind='link');bottom.addWidget(self.next_button)
        self.coverage=text('Based on your saved mail. Newer mail may not be included.','small',True);bottom.addWidget(self.coverage,1)
        self.why_button=btn('Why here?',self.explain,kind='link');bottom.addWidget(self.why_button)
        self.action_button=btn('No reply needed',self.dismiss,kind='secondary');bottom.addWidget(self.action_button);layout.addLayout(bottom)
        undo=QHBoxLayout();self.action_notice=text('','small',True);undo.addWidget(self.action_notice,1)
        self.undo_button=btn('Undo',self.undo,kind='link');undo.addWidget(self.undo_button);layout.addLayout(undo)
        self.undo_button.hide();self.selection_changed();self.update_journey()
    def update_journey(self):
        has_source=bool(self.host.config.get('source_root'));self.setup.setVisible(not has_source);self.check_button.setVisible(has_source)
        if not has_source:self.identity.hide()
    def definition(self):
        return next((v for v in self.host.summary_data.get('registry',{}).get('views',[]) if v['view_id']==self.view_id),{})
    def load(self,route,reset=True):
        self.task=route['view'].split(':',1)[-1];self.task=self.task if self.task in TASKS else 'unanswered'
        self.options=copy.deepcopy(route.get('parameters') or {});self.view_id=self.options.get('custom_view_id') or STARTERS[self.task]
        if reset:self.cursor=None;self.previous=[]
        self.restore_page_index=route.get('page_index',0) if reset else len(self.previous)
        self.ready=False;self.listing.setEnabled(False);self.host.update_commands();self.serial+=1;serial,generation=self.serial,self.host.route_generation
        is_starter=self.view_id==STARTERS[self.task]
        definition=self.definition();self.heading.setText('Mail' if is_starter else definition.get('name',definition.get('label','Saved mail view')))
        self.subtitle.setText(SUBTITLES[self.task] if is_starter else 'Mail selected by your saved rules.');self.result_count.setText('Loading saved conversations…');self.empty.hide()
        for task,button in self.tabs.items():button.setChecked(self.view_id==STARTERS[task])
        self.previous_button.setEnabled(False);self.next_button.setEnabled(False);self.update_journey()
        params={'view_id':self.view_id,'page_size':100}
        if route.get('search'):params['search']=route['search']
        if self.cursor:params['cursor']=self.cursor
        if self.options.get('sorts'):params['sorts']=self.options['sorts']
        def loaded(response):
            if serial!=self.serial or generation!=self.host.route_generation or self.host.page!='mail':return
            if not response.get('ok'):
                self.result_count.setText('This view could not be loaded');self.empty.setText(response.get('error',{}).get('message','Please try again.'));self.empty.show();return
            data=response['result']
            if self.fields:self.paint(data,route)
            else:
                def schema(response):
                    if serial!=self.serial or generation!=self.host.route_generation:return
                    if response.get('ok'):self.fields=list_schema_fields(response['result'])
                    self.paint(data,route)
                self.host.km.request('builder.schema',{'record_type':'Email'},schema,owner=self)
        self.host.km.request('view.query',params,loaded,owner=self)
    def paint(self,data,route):
        self.payload=data;self.next_cursor=data.get('next_cursor')
        if self.next_cursor and len(self.previous)<getattr(self,'restore_page_index',0):
            self.previous.append(self.cursor);self.cursor=self.next_cursor;target=self.restore_page_index;self.load(route,False);self.restore_page_index=target;return
        self.rows={};records=[];notices={}
        for row in data.get('rows',[]):
            record=row.get('record') or row;rid=record.get('record_id') or row.get('display_record_id')
            if not rid:continue
            self.rows[rid]=row;records.append(record);notices[rid]=mail_row_notice(row)
        definition=self.definition();sorts=self.options.get('sorts') or definition.get('sorts') or []
        self.surface.set_result('Email',self.view_id,records,self.fields,sorts=sorts,notices=notices,group_counts=data.get('group_counts'))
        identity=data.get('identity') or {};self.identity.setVisible(identity.get('state')=='confirmation_required')
        total=data.get('total_count',len(self.rows));unknown=data.get('unknown_count',0)
        unit='conversations' if data.get('group_by_fields') else 'messages'
        self.result_count.setText(str(total)+' '+unit+(' · '+str(unknown)+' messages need checking' if unknown else ''))
        self.empty.setText('Nothing matches this view. Configure the rules or choose All mail.' if self.host.config.get('source_root') else 'Choose a mail folder to get started.')
        self.empty.setVisible(not self.rows);self.listing.setVisible(bool(self.rows));self.previous_button.setEnabled(bool(self.previous));self.next_button.setEnabled(bool(self.next_cursor));self.ready=True;self.listing.setEnabled(True)
        wanted=(route.get('preview') or {}).get('record_id')
        self.surface.restore(route)
        self.selection_changed();self.update_journey()
    def selected_row(self):
        ids=self.listing.selected_ids();return self.rows.get(ids[0]) if ids else None
    def selection_changed(self,*_):
        self.host.update_commands()
        row=self.selected_row();self.why_button.setEnabled(bool(row));self.action_button.setVisible(self.task!='all')
        self.action_button.setText('Stop waiting' if self.task=='waiting' else 'No reply needed');self.action_button.setEnabled(bool(row and row.get('eligible_record_ids')))
    def sort_changed(self,field,direction):
        existing=self.definition().get('sorts') or []
        sorts=[{'field':field,'direction':direction}]+[item for item in existing if item['field']!=field][:2]
        def saved(response):
            if not response.get('ok'):
                self.host.action_status('Sort could not be saved. '+response.get('error',{}).get('message',''),True)
                self.load(self.host.navigation.current)
            else:
                route=self.host.navigation.current;route['parameters'].pop('sorts',None)
                self.host.navigation.update(route)
        self.host.km.request('view.edit',{'view_id':self.view_id,'changes':{'sorts':sorts}},saved,expected=self.host.km.revision,owner=self)
    def customize(self,*_,seed_exclusion=None):self.host.open_view_editor('Email',self.view_id,seed_exclusion=seed_exclusion)
    def exclude(self,rid):
        props=self.surface.projected.get(rid,{}).get('record',{}).get('properties',{});sender=props.get('from') or {}
        address=sender.get('address') if isinstance(sender,dict) else None
        expression={'field':'from','operator':'email_matches','value':address} if address else {'field':'subject','operator':'equals','value':props.get('subject','')}
        self.customize(seed_exclusion=expression)
    def saved_views(self):
        menu=QMenu(self)
        for view in self.host.summary_data.get('registry',{}).get('views',[]):
            if view.get('owner_type',view.get('record_type'))=='Email' and view.get('state')!='retired':
                menu.addAction(view.get('name',view.get('label','Saved view')),lambda checked=False,v=view:self.host.choose_view(v))
        menu.addSeparator();menu.addAction('Create a view…',lambda:self.host.edit_view(new=True));menu.addAction('Manage views…',self.host.configure)
        menu.exec(self.saved_button.mapToGlobal(self.saved_button.rect().bottomLeft()))
    def explain(self,row=None):
        row=row or self.selected_row()
        if not row:return
        rid=(row.get('record') or row).get('record_id') or row.get('display_record_id')
        self.host.record_actions.explain(self.surface.context([rid])) if hasattr(self.host.record_actions,'explain') else self.host.open_evidence(rid)
    def dismiss(self,row=None):
        row=row or self.selected_row()
        if not row or self.task=='all':return
        ids=row.get('eligible_record_ids') or [(row.get('record') or row).get('record_id')]
        property_id='ws:user:reply-not-required' if self.task=='unanswered' else 'ws:user:waiting-stopped'
        self.host.record_actions.assign(self.surface.context([rid for rid in ids if rid]),property_id,True,operation='set')
    def undo(self):self.host.record_actions.undo()
    def page_next(self):
        if self.next_cursor:self.previous.append(self.cursor);self.cursor=self.next_cursor;self.load(self.host.navigation.current,False)
    def page_back(self):
        if self.previous:self.cursor=self.previous.pop();self.load(self.host.navigation.current,False)
