"""O3 Alpha 3: one native workspace, one serialized knowledge session.

Widgets only send queries and commands. Source ingestion, evidence, identity,
configuration validation, revisions and transactions remain KM/Core-owned.
"""
from __future__ import annotations
import copy,html,json,sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from PySide6.QtCore import Qt,QThread,Signal,QTimer,QByteArray,QRect
from PySide6.QtGui import QShortcut,QKeySequence,QAction,QFont,QTextCursor
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QFrame,QVBoxLayout,QHBoxLayout,
    QListWidget,QListWidgetItem,QLineEdit,QStackedWidget,QScrollArea,QMenu,QTextBrowser,QDialog,
    QPlainTextEdit,QTabWidget,QMessageBox,QFileDialog,QComboBox,QLabel,QSizePolicy,QTreeWidgetItemIterator,QProgressBar)
from o3_core.engine import default_state
from . import __version__
from .ui import text,btn,wrap_btn,icon,rule,section,RecordDelegate
from .presentation import FONT,CSS,stylesheet_for,native_palette,theme_tokens,application_icon,font_family
from .appearance import appearance_from_config,normalize_appearance
from .logic import TYPE_NAMES,SOURCE_FOLDERS,read_config,save_config,validate_source,discover_sources,retained_navigation_preferences
from .worker import EngineWorker
from .km_client import KMClient,WorkspaceLock
from .km_session import display_row
from .navigation import Navigation,LatestRead,reference,route_identity
from .record_content import overview_data,connection_caption,address_value,source_time,display_value,event_time
from .source_status import coverage_text
from .workspace_editor import WorkspaceConfiguration,SharedSettings,ViewEditor,ConfiguredViewTable,ViewParameters,RecordPicker,confirm
from .workspace_type_editor import WorkspaceTypeManager,WorkspaceRecordEditor
from .record_actions import ManualValues,LOEEditor,OrganizationEditor,CalendarSources
from .workbench_tools import EvidenceLens,HistoryLens,RuleLens,CompareDialog,CommandPalette
from .spatial import Neighborhood
from .display_preferences import view_layout
from .mail_workspace import MailWorkspace
from .workspace_surface import RecordSurface,list_schema_fields
from .interaction_actions import RecordActionController,SelectionContext,ProjectDropButton
from .human_customization import HumanViewEditor as ViewEditor, SavedViewsDialog

class ModeLabel(QLabel):
    """Supervisor's legacy mode label is translated into the current product title."""
    def setText(self,value):
        super().setText('Managed workspace' if value.startswith('NIPR') else value)
        self.window().setWindowTitle('O3 · '+__version__+(' · Managed workspace' if value.startswith('NIPR') else ''))

class O3Window(QMainWindow):
    submit=Signal(str,object)
    shutdown=Signal()
    preferences_saved=Signal(bool,str)
    startup_finished=Signal(bool)
    def __init__(self,state,config,lock):
        super().__init__();self.state=Path(state);self.config=config;self.lock=lock
        self.initialized=False;self.closing=False;self.allow_close=False;self.last_error=None;self.storage_maintenance=False
        self.inline_pending={};self.inline_serial=0
        self.summary_data={};self.collection_data={};self.record_kinds={};self.home_data={};self.current_detail=None;self.selected_id=None;self.reader_record=None
        self.pending_maintenance=None;self.upgrade_dialog=None;self.scan_busy=False;self.active_dialog=None;self.restoring=False
        prefs=config.get('alpha3') if isinstance(config.get('alpha3'),dict) else {}
        self.appearance=appearance_from_config(config);self.scale=self.appearance['text_scale']
        self.preview_theme=None;self.preview_appearance_values=None;self.reader_body=''
        self.page='overview';self.navigation=Navigation(config.get('navigation'));self.nav_ready=False;self.page_positions={}
        self.route_generation=0;self.collection_generation=0;self.collection_ready=False;self.preferences_error=None
        self.reads={op:LatestRead() for op in ('home','list','reading','neighborhood','preview')};self.read_purpose='reader'
        self.search_timer=QTimer(self);self.search_timer.setSingleShot(True);self.search_timer.setInterval(250);self.search_timer.timeout.connect(self.find_records)
        self.preference_timer=QTimer(self);self.preference_timer.setSingleShot(True);self.preference_timer.setInterval(350);self.preference_timer.timeout.connect(self.persist_preferences)
        self.startup_timer=QTimer(self);self.startup_timer.setSingleShot(True);self.startup_timer.setInterval(45000);self.startup_timer.timeout.connect(lambda:self.on_failed('initialize','Workspace initialization did not finish within 45 seconds.'))
        self.startup_sample_timer=QTimer(self);self.startup_sample_timer.setInterval(5000);self.startup_sample_timer.timeout.connect(self.sample_startup)
        self.tick=QTimer(self);self.tick.setInterval(60000);self.tick.timeout.connect(self.periodic_scan)
        self.setWindowTitle('O3 · '+__version__);self.setWindowIcon(application_icon());self.resize(1360,860);self.setMinimumSize(760,640)
        self.build_ui();self.apply_appearance(self.appearance)
        # Alpha 2 stores a base64 window inside a geometry dictionary; earlier
        # Alpha 3 candidates store the window directly as a hex string.
        saved_geometry=config.get('geometry')
        if isinstance(saved_geometry,dict):
            saved_window=saved_geometry.get('window');decode_geometry=QByteArray.fromBase64
        else:
            saved_window=saved_geometry;decode_geometry=QByteArray.fromHex
        if isinstance(saved_window,str) and len(saved_window)<4096:
            try:self.restoreGeometry(decode_geometry(saved_window.encode('ascii')))
            except (ValueError,TypeError):pass
        self.km=KMClient(self.submit.emit,self);self.km.changed.connect(self.mutation_finished)
        self.record_actions=RecordActionController(self.km,self)
        self.record_actions.status.connect(self.action_status)
        self.record_actions.open_requested.connect(lambda context:self.open_record(context.records[0].record_id,context.records[0].record_type) if context.count else None)
        self.record_actions.configure_requested.connect(lambda context:self.open_view_editor(context.record_type,context.view_id))
        self.record_actions.exclude_requested.connect(self.exclude_context)
        self.record_actions.explain_requested.connect(self.record_actions.explain_property)
        self.record_actions.undo_available.connect(self.mail_workspace.undo_button.setVisible)
        self.thread=QThread(self);self.worker=EngineWorker(self.state);self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.initialize);self.submit.connect(self.worker.run);self.shutdown.connect(self.worker.close_engine);self.shutdown.connect(self.worker.request_stop,Qt.ConnectionType.DirectConnection)
        self.worker.ready.connect(self.on_ready);self.worker.result.connect(self.on_result);self.worker.failed.connect(self.on_failed);self.worker.maintenance_required.connect(self.on_maintenance);self.worker.upgrade_progress.connect(self.on_upgrade_progress)
        self.worker.closed.connect(self.thread.quit);self.thread.finished.connect(self.done_closing)
        self.thread.start();self.startup_timer.start();self.startup_sample_timer.start()

    def sample_startup(self):
        if self.initialized or self.closing or (not self.startup_timer.isActive() and not self.storage_maintenance):
            self.startup_sample_timer.stop();return
        diagnostic=getattr(self.worker,'startup_diagnostics',None)
        if diagnostic is not None:diagnostic.sample()

    def build_ui(self):
        root=QWidget();outer=QHBoxLayout(root);outer.setContentsMargins(0,0,0,0);outer.setSpacing(0)
        self.sidebar=QFrame();self.sidebar.setObjectName('sidebar');self.sidebar.setFixedWidth(208)
        nav=QVBoxLayout(self.sidebar);nav.setContentsMargins(16,24,16,18);nav.setSpacing(5)
        brand=text('O3');brand.setObjectName('brand');brand.setContentsMargins(12,0,0,20);nav.addWidget(brand)
        self.navbuttons={}
        for key,title,ico,fn in [('mail','Mail','mail',self.open_mail),('Person','People','person',lambda:self.open_collection('Person')),('Document','Documents','doc',lambda:self.open_collection('Document')),('Event','Calendar','calendar',lambda:self.open_collection('Event')),('LineOfEffort','Projects','effort',lambda:self.open_collection('LineOfEffort'))]:
            b=btn(title,fn,ico,'nav');b.setCheckable(True);self.navbuttons[key]=b;nav.addWidget(b)
        nav.addSpacing(25);self.project_heading=text('YOUR PROJECTS','small');nav.addWidget(self.project_heading);self.effort_host=QWidget();self.effort_host.setObjectName("efforts");self.effort_l=QVBoxLayout(self.effort_host);self.effort_l.setContentsMargins(0,3,0,0);self.effort_l.setSpacing(2);nav.addWidget(self.effort_host)
        self.perspective_btn=btn('Saved places',self.perspectives,'link','nav');nav.addWidget(self.perspective_btn)
        nav.addStretch();self.mode_chip=ModeLabel('');self.mode_chip.setProperty('role','small');nav.addWidget(self.mode_chip)
        nav.addWidget(btn('Your folders',self.open_sources,'sources','nav'));nav.addWidget(btn('Settings',self.open_settings,'sliders','nav'));outer.addWidget(self.sidebar)
        self.main=QWidget();self.main_l=QVBoxLayout(self.main);self.main_l.setContentsMargins(36,20,36,20);self.main_l.setSpacing(0)
        toolbar=QHBoxLayout();toolbar.setSpacing(12);self.workspace_menu=btn('Menu',self.open_navigation,'grid');toolbar.addWidget(self.workspace_menu);self.workspace_menu.hide()
        self.back_button=btn('Back',self.back,'back');toolbar.addWidget(self.back_button);self.back_button.hide()
        self.crumb=text('O3','muted');self.crumb.setMinimumWidth(0);self.crumb.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);toolbar.addWidget(self.crumb,1)
        self.search=QLineEdit();self.search.setPlaceholderText('Search O3');self.search.setAccessibleName('Search O3');self.search.addAction(icon('search'),QLineEdit.LeadingPosition);self.search.setMinimumWidth(150);self.search.setMaximumWidth(440);self.search.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed);self.search.setClearButtonEnabled(True);self.search.textChanged.connect(lambda:self.search_timer.start());self.search.setClearButtonEnabled(True)
        toolbar.addWidget(self.search,2)
        self.context_button=btn('Related',self.open_context,'link');toolbar.addWidget(self.context_button);self.context_button.hide()
        self.more=btn('Options',self.open_options,'sliders');toolbar.addWidget(self.more);self.main_l.addLayout(toolbar);self.main_l.addSpacing(20)
        self.stack=QStackedWidget();self.main_l.addWidget(self.stack,1)
        self.health_notice=text('','small',True);self.main_l.addWidget(self.health_notice);self.health_notice.hide()
        self.save_notice=QWidget();save_row=QHBoxLayout(self.save_notice);save_row.setContentsMargins(0,6,0,6);self.save_message=text('','small',True);save_row.addWidget(self.save_message,1);save_row.addWidget(btn('Retry saving',self.persist_preferences,kind='link'));self.main_l.addWidget(self.save_notice);self.save_notice.hide()
        self.project_notice=QWidget();project_row=QHBoxLayout(self.project_notice);project_row.setContentsMargins(0,6,0,6);project_row.addWidget(text('Your project is saved, but its related information could not be updated yet.','small',True),1);project_row.addWidget(btn('Retry project update',lambda:self.command('loe.reconcile',{}),kind='link'));self.main_l.addWidget(self.project_notice);self.project_notice.hide()
        self.status=text('Opening workspace…','small',True);self.main_l.addWidget(self.status);outer.addWidget(self.main,1);self.setCentralWidget(root)
        self.build_overview();self.build_reader();self.build_collection();self.spatial=Neighborhood(self);self.stack.addWidget(self.spatial);self.mail_workspace=MailWorkspace(self);self.stack.addWidget(self.mail_workspace)
        self.shortcuts=[]
        for key,cb in [('Escape',self.escape_current)]:
            s=QShortcut(QKeySequence(key),self);s.activated.connect(cb);self.shortcuts.append(s)
        self.install_commands()
        self.main.setEnabled(False)

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item=layout.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
            elif item.layout():O3Window.clear_layout(item.layout())

    def build_overview(self):
        self.overview=QWidget();l=QVBoxLayout(self.overview);l.setContentsMargins(0,0,0,0);l.setSpacing(0)
        top=QHBoxLayout();self.home_title=text('Workspace','title',True);top.addWidget(self.home_title,1);self.connections=btn('Connections',lambda:self.open_spatial((self.home_surface.widget.selected_ids() or [self.home_data.get('loe_id')])[0]),'link','secondary');top.addWidget(self.connections);l.addLayout(top)
        l.addSpacing(8);self.home_subtitle=text('Your records, in context.','muted',True);l.addWidget(self.home_subtitle);l.addSpacing(20)
        body=QHBoxLayout();body.setSpacing(36);left=QWidget();ll=QVBoxLayout(left);ll.setContentsMargins(0,0,0,0);ll.setSpacing(0)
        self.people=QWidget();self.people_l=QVBoxLayout(self.people);self.people_l.setContentsMargins(0,0,0,0);self.people_l.setSpacing(6);ll.addWidget(self.people)
        self.home_setup=QWidget();setup=QVBoxLayout(self.home_setup);setup.setContentsMargins(0,8,0,12);setup.addWidget(btn('Choose your folder',self.choose_source,'sources','primary'),0,Qt.AlignLeft);setup.addWidget(btn('Create a project',self.new_effort,'effort','secondary'),0,Qt.AlignLeft);ll.addWidget(self.home_setup);self.home_setup.hide()
        h=QHBoxLayout();h.addWidget(text('Correspondence','section'));h.addStretch();h.addWidget(text('Captured','small'));ll.addLayout(h);ll.addSpacing(10)
        self.home_surface=RecordSurface(self,self.overview);self.mail_list=self.home_surface.widget
        self.mail_list.selection_changed.connect(lambda ids:self.update_commands());ll.addWidget(self.mail_list,1)
        self.home_empty=text('No mail is connected to this project yet.','muted',True);ll.addWidget(self.home_empty);self.home_empty.hide();body.addWidget(left,1)
        self.right=QStackedWidget();self.right.setFixedWidth(300);self.context_home=QWidget();self.context_home_l=QVBoxLayout(self.context_home);self.context_home_l.setContentsMargins(0,0,0,0);self.context_home_l.setSpacing(10);self.right.addWidget(self.context_home)
        self.preview=QWidget();self.preview_l=QVBoxLayout(self.preview);self.preview_l.setContentsMargins(0,0,0,0);self.preview_l.setSpacing(12);self.right.addWidget(self.preview);body.addWidget(self.right);l.addLayout(body,1);self.stack.addWidget(self.overview)

    def build_reader(self):
        self.reader=QWidget();outer=QHBoxLayout(self.reader);outer.setContentsMargins(0,0,0,0);outer.setSpacing(36)
        self.reading=QWidget();l=QVBoxLayout(self.reading);l.setContentsMargins(0,0,0,0);l.setSpacing(0)
        self.reader_title=text('','readerTitle',True);self.reader_title.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard);l.addWidget(self.reader_title);self.title_expand=btn('Show full title',self.show_full_title,kind='link');l.addWidget(self.title_expand,0,Qt.AlignLeft);self.title_expand.hide();l.addSpacing(8)
        self.reader_metadata=text('','muted',True);l.addWidget(self.reader_metadata);l.addSpacing(12);l.addWidget(rule());l.addSpacing(10)
        self.reader_notice=text('','small',True);l.addWidget(self.reader_notice);self.reader_notice.hide()
        self.reader_text=QTextBrowser();self.reader_text.setAccessibleName('Readable captured content');self.reader_text.setOpenExternalLinks(False);self.reader_text.setOpenLinks(False);l.addWidget(self.reader_text,1)
        actions=QHBoxLayout();self.reader_more=btn('Continue reading',self.load_more_reader,kind='link');actions.addWidget(self.reader_more);self.reader_more.hide();self.source_details=btn('Source details',lambda:self.open_evidence(),kind='link');actions.addWidget(self.source_details);actions.addStretch();self.followup=btn('Follow-up',self.followup_menu,kind='secondary');actions.addWidget(self.followup);actions.addWidget(btn('Record actions',self.record_menu,'more'));l.addLayout(actions)
        outer.addWidget(self.reading,1);self.reader_side=QScrollArea();self.reader_side.setWidgetResizable(True);self.reader_side.setFixedWidth(294);self.reader_side.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.reader_context=QWidget();self.reader_context_l=QVBoxLayout(self.reader_context);self.reader_context_l.setContentsMargins(8,2,8,2);self.reader_context_l.setSpacing(12);self.reader_side.setWidget(self.reader_context);outer.addWidget(self.reader_side);self.stack.addWidget(self.reader)

    def make_list(self,name):
        w=QListWidget();w.setAccessibleName(name);w.setItemDelegate(RecordDelegate(w));w.setMouseTracking(True);w.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);return w

    def build_collection(self):
        self.collection=QWidget();l=QVBoxLayout(self.collection);l.setContentsMargins(0,0,0,0);l.setSpacing(12)
        top=QHBoxLayout();self.collection_title=text('Everything','title',True);top.addWidget(self.collection_title,1);self.collection_open=btn('Open item',self.open_selected,kind='secondary');self.collection_open.setEnabled(False);top.addWidget(self.collection_open);top.addWidget(btn('Views',self.views_menu,'sliders'));l.addLayout(top)
        row=QHBoxLayout();self.kind_choice=QComboBox();self.kind_choice.addItem('All categories',None)
        for key,label in TYPE_NAMES.items():self.kind_choice.addItem(label,key)
        self.kind_choice.currentIndexChanged.connect(self.change_kind);row.addWidget(self.kind_choice);row.addStretch();self.collection_state=text('','muted',True);self.collection_state.setMinimumWidth(0);self.collection_state.setAlignment(Qt.AlignRight|Qt.AlignVCenter);row.addWidget(self.collection_state);l.addLayout(row)
        self.collection_stack=QStackedWidget();self.collection_list=self.make_list('Workspace records');self.collection_list.itemActivated.connect(self.open_selected);self.collection_list.itemSelectionChanged.connect(self.update_collection_action);self.collection_stack.addWidget(self.collection_list)
        self.configured_table=ConfiguredViewTable();self.configured_table.selected_record.connect(self.open_selected);self.configured_table.layout_changed.connect(self.save_view_layout);self.collection_stack.addWidget(self.configured_table)
        from .knowledge_views import KnowledgeView
        self.structured_view=KnowledgeView();self.structured_view.selected_record.connect(self.open_selected);self.structured_view.selected_conversation.connect(self.open_conversation);self.collection_stack.addWidget(self.structured_view)
        for tree in (self.configured_table.tree,self.structured_view.tree):tree.itemSelectionChanged.connect(self.update_collection_action)
        self.record_surface=RecordSurface(self,self.collection);self.collection_stack.addWidget(self.record_surface.widget)
        self.record_surface.sort_callback=self.sort_collection
        self.record_surface.exclude_callback=self.exclude_collection
        self.record_surface.widget.selection_changed.connect(self.update_collection_action)
        self.collection_stack.currentChanged.connect(self.update_collection_action)
        coverage=QHBoxLayout();self.collection_coverage=text('','small',True);coverage.addWidget(self.collection_coverage,1)
        self.coverage_action=btn('Source coverage',self.open_sources,kind='link');coverage.addWidget(self.coverage_action);l.addLayout(coverage)
        self.collection_guidance=text('','muted',True);self.collection_guidance.hide();l.addWidget(self.collection_guidance)
        l.addWidget(self.collection_stack,1);bottom=QHBoxLayout();self.previous_page=btn('Previous',self.previous_records,kind='link');bottom.addWidget(self.previous_page);self.next_page=btn('Next',self.next_records,kind='link');bottom.addWidget(self.next_page);bottom.addStretch();self.configure_button=btn('Personalize this view',self.edit_view,kind='link');bottom.addWidget(self.configure_button);l.addLayout(bottom)
        self.stack.addWidget(self.collection);self.next_cursor=None;self.cursor_stack=[];self.current_cursor=None

    def type_name(self,kind):
        for t in self.summary_data.get('workspace_types',[]):
            if t.get('type_id')==kind:return t.get('label') or t.get('name') or 'Custom record'
        return TYPE_NAMES.get(kind,'Workspace record')

    def local_stamp(self,value,short=False):
        if not value:return ''
        try:
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            if dt.tzinfo:dt=dt.astimezone(ZoneInfo(self.summary_data.get('configuration',{}).get('settings',{}).get('workspace_timezone','America/New_York')))
            return dt.strftime('%d %b · %H:%M' if short else '%d %b %Y · %H:%M %Z').strip()
        except (ValueError,TypeError,KeyError):return str(value)

    def add_item(self,w,row):
        self.record_kinds[row['id']]=row['target_kind']
        record=row.get('record',{});p=record.get('properties',{});kind=row['kind'];sender=p.get('from',{})
        person=sender.get('name') or sender.get('address') if isinstance(sender,dict) else str(sender)
        date=(p.get('received_at_utc') or p.get('sent_at_utc')) if kind=='Email' else p.get('source',{}).get('modified_utc') if kind=='Document' else ''
        render={'title':row['label'],'kind':self.type_name(kind),'person':person or row.get('secondary',''),'capture_label':self.local_stamp(date,True)}
        item=QListWidgetItem(row['label']+' — '+render['person']);item.setData(Qt.UserRole,row['id']);item.setData(Qt.UserRole+1,render);item.setToolTip('<pre>'+html.escape(row['label']+'\n'+render['person'])+'</pre>');w.addItem(item)

    def request_inline(self,rid,kind,callback,operation='inline',**params):
        if not self.initialized or self.closing:return
        self.inline_serial+=1;token='inline-'+str(self.inline_serial)
        self.inline_pending[token]=(callback,self.route_generation)
        self.submit.emit(operation,{'id':rid,'kind':kind,'request_id':token,**params})

    def active_surface(self):
        if self.page=='mail':return self.mail_workspace.surface
        if self.page=='overview':return self.home_surface
        if self.page=='collection' and self.collection_stack.currentWidget() is self.record_surface.widget:return self.record_surface
        return None

    def active_reference(self):
        if self.page=='mail' and not self.mail_workspace.ready:return None
        if self.current_detail:return {'record_id':self.current_detail['id'],'record_type':self.current_detail['target_kind']}
        surface=self.active_surface()
        if surface:
            ids=surface.widget.selected_ids();rid=surface.widget.expanded_id or (ids[0] if ids else None)
            if rid:return {'record_id':rid,'record_type':surface.kind_for(rid)}
        return None

    def action_status(self,message,is_error=False):
        self.status.setText(message);self.status.show()
        self.mail_workspace.action_notice.setText(message)

    def escape_current(self):
        surface=self.active_surface()
        if surface and surface.widget.expanded_id:surface.widget.collapse()
        else:self.back()

    @staticmethod
    def sort_key(route):return route.get('view','all')+':'+str(route.get('kind') or 'all')

    def sort_collection(self,field,direction):
        r=self.navigation.current
        if r['view'].startswith('view:user:'):
            definition=next((v for v in self.summary_data.get('registry',{}).get('views',[]) if v['view_id']==r['view']),{})
            previous=definition.get('sorts') or []
            sorts=[{'field':field,'direction':direction}]+[item for item in previous if item['field']!=field][:2]
            def saved(response):
                if not response.get('ok'):self.action_status('Sort could not be saved. '+response.get('error',{}).get('message',''),True)
            self.km.request('view.edit',{'view_id':r['view'],'changes':{'sorts':sorts}},saved,expected=self.km.revision,owner=self)
            return
        sorts=self.config.setdefault('alpha5',{}).setdefault('view_sorts',{})
        previous=sorts.get(self.sort_key(r),[])
        sorts[self.sort_key(r)]=[{'field':field,'direction':direction}]+[s for s in previous if s['field']!=field][:2]
        self.preference_timer.start();r.update(preview=None,scroll=0,page_index=0);self.visit(r)

    def exclude_context(self,context):
        if not context.count:return
        if self.page=='mail':self.mail_workspace.exclude(context.records[0].record_id)
        else:self.exclude_collection(context.records[0].record_id)

    def exclude_collection(self,rid):
        owner=self.record_surface.kind_for(rid)
        if owner!='Email':
            self.open_view_editor(owner,self.navigation.current['view'],seed_exclusion={'field':'record_id','operator':'equals','value':rid});return
        props=self.record_surface.projected.get(rid,{}).get('record',{}).get('properties',{})
        sender=props.get('from') or {};address=sender.get('address') if isinstance(sender,dict) else None
        seed={'field':'from','operator':'email_matches','value':address} if address else {'field':'subject','operator':'equals','value':props.get('subject','')}
        self.open_view_editor('Email',self.navigation.current['view'],seed_exclusion=seed)

    def request_read(self,op,args):
        if not self.initialized or self.closing or self.storage_maintenance:return
        pending=self.reads[op].offer(args)
        if pending:self.submit.emit(op,pending)

    def on_ready(self,summary):
        if self.closing:return
        self.startup_sample_timer.stop()
        self.finish_upgrade_dialog()
        self.pending_maintenance=None
        self.initialized=True;self.storage_maintenance=False;self.startup_timer.stop();self.main.setEnabled(True)
        self.install_summary(summary);self.tick.start();self.status.hide()
        if not self.nav_ready:
            self.nav_ready=True
            if not self.config.get('navigation') or (not self.config.get('alpha3',{}).get('navigation_adopted') and not self.navigation.current['selected'] and self.navigation.current['view']=='all' and not self.navigation.current['search']):
                counts=summary.get('counts',{});first=next((kind for kind in ('Email','Person','Document','Event','LineOfEffort') if counts.get(kind)), 'Email')
                self.navigation.update({'section':first,'kind':first,'view':'mail:unanswered' if first=='Email' else 'all'})
            self.config.setdefault('alpha3',{})['navigation_adopted']=True
            self.render_route()
        else:self.refresh()
        self.startup_finished.emit(True)

    def install_summary(self,data):
        self.summary_data=data;self.km.configure(data['capabilities']);self.km.maintenance=False
        self.kind_choice.blockSignals(True);current=self.kind_choice.currentData()
        while self.kind_choice.count()>7:self.kind_choice.removeItem(7)
        for t in data.get('workspace_types',[]):
            if t.get('state')!='retired':self.kind_choice.addItem(t.get('label',t.get('name','Custom record')),t['type_id'])
        self.kind_choice.setCurrentIndex(max(0,self.kind_choice.findData(current)));self.kind_choice.blockSignals(False);self.update_notices();self.load_projects()

    def on_result(self,op,data):
        if op in ('inline','content') and data.get('request_id') in self.inline_pending:
            callback,generation=self.inline_pending.pop(data['request_id'])
            if generation==self.route_generation and not self.closing:callback(data)
            return
        if op=='km':self.km.receive(data);return
        if self.closing:return
        if op in self.reads:
            accept,next_item=self.reads[op].finish(data.get('request_id'))
            if next_item:self.submit.emit(op,next_item)
            if not accept:return
            if data.get('read_error'):
                self.status.setText('Unavailable · '+data['read_error']);self.status.show()
                if op=='home':self.overview.setEnabled(False);self.home_title.setText('Workspace unavailable')
                if op=='reading':self.reader_text.setPlainText('This record could not be read. Retry from View options.');self.reader_notice.setText(data['read_error']);self.reader_notice.show()
                if op=='list':self.collection_unavailable(data['read_error'])
                return
            if op=='home':self.paint_home(data)
            elif op=='list':self.paint_collection(data)
            elif op=='reading':
                self.current_detail=data
                if self.read_purpose=='reader':self.paint_reader(data)
                elif self.read_purpose=='preview':self.paint_preview(data)
                elif self.read_purpose=='evidence':self.evidence_dialog(data)
            elif op=='neighborhood':self.spatial.load(data);self.spatial.canvas.setEnabled(True);self.spatial.scroll.setFocus()
            elif op=='preview':self.show_data('Verified source capture',data.get('text',''),notice='Archive status: '+data.get('status','unknown')+' · SHA-256 '+str(data.get('source_sha') or 'not applicable'))
        elif op=='overview':self.install_summary(data);self.refresh();self.update_sources()
        elif op=='scan':
            self.scan_busy=False;self.source_failure=None;self.config['last_capture_check']=data.get('checked_utc');self.config['last_capture_result']={k:data.get(k) for k in ('complete','coverage','errors','pending_removals')};self.preference_timer.start();self.install_summary(data['summary']);self.refresh()
            self.status.setText('Capture check complete · upstream freshness unknown' if not data.get('errors') else 'Capture check partial · inspect Sources');self.status.show();self.update_sources()
        elif op in ('audit','rules','export_rdf','backup'):
            self.status.setText({'audit':'Audit completed','rules':'Rules loaded','export_rdf':'Graph exported','backup':'Verified backup created'}[op]);self.status.show();self.show_data(self.status.text(),data)
        elif op=='verify_backup':
            if self._restore_selection and confirm(self,'Restore Workspace','This replaces the current database, evidence archive and settings with the verified backup. Continue only with an authorized backup and after preserving the current Workspace.'):
                self.begin_restore(self._restore_selection)
        elif op=='restore':self.finish_restore()
        elif op=='reopen':
            if data.get('maintenance_required'):self.on_maintenance(data)
            else:self.on_ready(data)
        elif op=='migrate':
            self.storage_maintenance=False
            if data['summary'].get('maintenance_required'):self.on_maintenance(data['summary'])
            else:self.on_ready(data['summary'])
        self.update_notices();self.update_commands()

    def on_failed(self,op,error):
        if op=='initialize' and error=='Workspace initialization did not finish within 45 seconds.':
            diagnostic=getattr(self.worker,'startup_diagnostics',None)
            if diagnostic is not None:diagnostic.finish('timeout')
        self.last_error=(op,error);self.scan_busy=False;self.status.setText(op.replace('_',' ').capitalize()+' failed · '+error);self.status.show();self.update_sources()
        if op=='scan':self.source_failure=error;self.update_notices();self.submit.emit('overview',{})  # Re-read KM's recorded failed-scan coverage.
        if op=='initialize':self.startup_timer.stop();self.main.setEnabled(True);self.startup_finished.emit(False)
        if op=='upgrade_starters':
            self.storage_maintenance=False;self.startup_sample_timer.stop();self.finish_upgrade_dialog()
            self.show_information('Workspace update stopped',error+'\nOpen Your folders and choose Update saved data to retry.',True)
        if op in ('restore','migrate'):
            self.storage_maintenance=True;self.show_information('Maintenance stopped',error+'\nThe writer remains stopped. Preserve the backup and consult the recovery guide before reopening.',True)

    def mutation_finished(self,response):
        # Accepted writes complete even if their editor closed. No cancellation
        # is represented as an undo; all live perspectives refresh afterwards.
        self.status.setText('Saved');self.status.show()
        self.page_positions.clear();self.current_cursor=None;self.cursor_stack=[]
        self.submit.emit('overview',{})

    def paint_home(self,data):
        self.overview.setEnabled(True);self.home_setup.setVisible(not bool(data.get('loe_id')));self.home_data=data;self.clear_layout(self.effort_l)
        for row in data.get('efforts',[])[:6]:
            b=ProjectDropButton(row['label'],{'record_type':'LineOfEffort','record_id':row['id']},self.record_actions,self);b.clicked.connect(lambda checked=False,r=row['id']:self.go_home(r));b.setProperty('kind','nav');b.setCheckable(True);b.setProperty('effort_id',row['id']);b.setChecked(row['id']==data.get('loe_id'));self.effort_l.addWidget(b)
        if len(data.get('efforts',[]))>6:self.effort_l.addWidget(btn('All projects',lambda:self.open_collection('LineOfEffort'),kind='link'))
        if not data.get('loe_id'):
            self.home_title.setText('Your workspace');self.home_subtitle.setText('Your projects bring related mail, people and documents together.');self.connections.setEnabled(False);self.mail_list.set_rows([]);self.home_surface.projected={};self.home_empty.setText('Create a project when you want to organize related information. Mail, People, Documents and Calendar are ready without one.');self.home_empty.show();self.clear_layout(self.people_l);self.clear_layout(self.context_home_l);self.context_home_l.addWidget(btn('Choose sources',self.open_sources,kind='primary'));self.context_home_l.addWidget(btn('Create a project',self.new_effort,kind='link'));self.context_home_l.addStretch();return
        self.connections.setEnabled(True);p=data['core']['properties'];self.home_title.setText(p['name']);self.home_subtitle.setText(p.get('objective') or 'Mail, people and documents connected to this project.')
        self.clear_layout(self.people_l);records=data['records'];affiliations=[]
        for org in data['sections'].get('Organizations',[]):
            people=[]
            for path in org.get('paths',[]):
                for edge in path:
                    if edge.get('predicate')=='affiliated_with' and edge.get('source_id') in records:people.append(edge['source_id'])
            if people:affiliations.append((org['record_id'],list(dict.fromkeys(people))))
        if affiliations:
            oid,pids=affiliations[0];b=btn(records[oid]['label'],lambda:self.open_record(oid),'building','group');self.people_l.addWidget(b,0,Qt.AlignLeft)
            row=QHBoxLayout();row.setContentsMargins(20,0,0,14);row.setSpacing(18)
            def person_order(rid):
                paths=next((e['paths'] for e in data['sections']['People'] if e['record_id']==rid),[])
                senders=sum(any(edge.get('predicate') in ('sent_by','organized_by') for edge in path) for path in paths)
                return (-senders,records[rid]['label'])
            for pid in sorted(pids,key=person_order)[:2]:
                rec=records[pid];person=QWidget();pl=QHBoxLayout(person);pl.setContentsMargins(0,0,0,0);pl.setSpacing(8)
                avatar=text(''.join(x[0] for x in rec['label'].split()[:2]));avatar.setAlignment(Qt.AlignCenter);avatar.setFixedSize(34,34);avatar.setStyleSheet('background:#edf1f6;color:#536b87;border-radius:17px;font-size:12px;');pl.addWidget(avatar)
                pl.addWidget(btn(rec['label']+'\n'+rec['secondary'],lambda checked=False,r=pid:self.open_record(r)));row.addWidget(person)
            if len(pids)>2:row.addWidget(btn('+'+str(len(pids)-2),lambda:self.open_collection('LineOfEffort','view:loe:dashboard',{'loe_id':data['loe_id']}),kind='link'))
            row.addStretch();self.people_l.addLayout(row)
        else:self.people_l.addWidget(text('Connected people and organizations remain in Context.','small'))
        mail=[records[e['record_id']] for e in data['sections'].get('Email Traffic',[])]
        home_view='view:project-correspondence:'+data['loe_id']
        if home_view not in self.config.get('display',{}).get('views',{}):
            self.config.setdefault('display',{}).setdefault('views',{})[home_view]={'record_type':'Email','columns':{'order':['record','date'],'visible':['record','date'],'widths':{'record':420,'date':170}},'secondary':True,'density':'inherit'}
        self.home_surface.set_result('Email',home_view,mail)
        # This is a bounded project preview. Full query controls live in the linked collection.
        self.mail_list.set_columns([{**c,'sortable':False} for c in self.mail_list.columns])
        self.home_empty.setText(str(len(mail))+' of '+str(data.get('section_counts',{}).get('Email Traffic',len(mail)))+' connected messages shown. Open All connected records to continue.' if data.get('section_counts',{}).get('Email Traffic',0)>len(mail) else 'No mail is connected to this project yet.')
        self.home_empty.setVisible(not mail or data.get('section_counts',{}).get('Email Traffic',0)>len(mail));self.paint_home_context();self.right.setCurrentWidget(self.context_home)
        self.status.hide();self.home_surface.restore(self.navigation.current);self.adapt()

    def paint_home_context(self,layout=None):
        l=layout or self.context_home_l;self.clear_layout(l);d=self.home_data;records=d.get('records',{})
        l.addWidget(section('Recorded dates'))
        events=d.get('sections',{}).get('Events',[])
        for e in events[:2]:
            rec=records[e['record_id']];p=rec['record']['properties'];a=rec['record'].get('additional',{});stamp=a.get('start_utc') or p.get('dtstart')
            l.addWidget(text(self.local_stamp(stamp) if isinstance(stamp,str) else event_time(stamp),'muted',True));l.addWidget(wrap_btn(rec['label'],lambda checked=False,r=rec['id']:self.open_record(r),'calendar',kind='link'));l.addWidget(text('Recorded calendar event','small'))
        if not events:l.addWidget(text('No connected calendar event.','small',True))
        l.addSpacing(12);l.addWidget(section('Documents'))
        for e in d.get('sections',{}).get('Documents',[])[:4]:
            rec=records[e['record_id']];l.addWidget(wrap_btn(rec['label'],lambda checked=False,r=rec['id']:self.open_record(r),'doc'));ex=rec['record']['properties'].get('extraction',{});l.addWidget(text('Partial extraction' if ex.get('status')=='partial' else 'Captured supporting material','small',True))
        l.addStretch();l.addWidget(btn('All connected records',lambda:self.open_collection('LineOfEffort','view:loe:dashboard',{'loe_id':d.get('loe_id')}),kind='link'))

    def select_record(self,rid,open_compact=True):
        self.selected_id=rid;self.read_purpose='preview';self.current_detail=None;self.clear_layout(self.preview_l);self.preview_l.addWidget(text('Loading record…','muted'));self.preview_l.addStretch();self.right.setCurrentWidget(self.preview);self.request_read('reading',{'id':rid})
        if open_compact and self.compact:self.open_record(rid)

    def paint_preview(self,d):
        self.clear_layout(self.preview_l);top=QHBoxLayout();top.addWidget(text(self.type_name(d['kind']),'muted'));top.addStretch();top.addWidget(btn('Close',lambda:self.right.setCurrentWidget(self.context_home)));self.preview_l.addLayout(top)
        self.preview_l.addWidget(text(d['title'],'section',True));self.preview_l.addWidget(text(d['secondary'],'muted',True));self.preview_l.addWidget(btn('Read message' if d['kind']=='Email' else 'Open item',lambda:self.open_record(d['id']),kind='primary'));self.preview_l.addSpacing(12);self.preview_l.addWidget(text('Connected here','section'))
        for e in d['relationships'][:4]:self.add_connection(self.preview_l,d,e)
        self.preview_l.addStretch();self.preview_l.addWidget(btn('About the original item',lambda:self.open_evidence(d['id']),kind='link'))

    def checkpoint_navigation(self):
        if not self.nav_ready:return
        r=self.navigation.current
        if self.page=='mail':
            if not self.mail_workspace.ready:return
            listing=self.mail_workspace.listing;r['scroll']=listing.tree.verticalScrollBar().value();ids=listing.selected_ids()
            r['preview']={'record_type':'Email','record_id':ids[0]} if ids else None
            r.update(self.mail_workspace.surface.checkpoint())
            r['page_index']=len(self.mail_workspace.previous);self.page_positions[self.page_key(r)]=(self.mail_workspace.cursor,list(self.mail_workspace.previous))
        elif self.page=='overview':
            listing=self.mail_list;r['scroll']=listing.tree.verticalScrollBar().value();ids=listing.selected_ids()
            r['preview']={'record_type':'Email','record_id':ids[0]} if ids else None
            r.update(self.home_surface.checkpoint())
        elif self.page=='collection':
            # A pending/failed query must not overwrite the location being restored.
            if not self.collection_ready:return
            listing=self.active_collection();r['scroll']=listing.verticalScrollBar().value()
            r['preview']=self.selected_collection_reference()
            if self.collection_stack.currentWidget() is self.record_surface.widget:r.update(self.record_surface.checkpoint())
            r['page_index']=len(self.cursor_stack);r['expanded']=[]
            if listing is not self.collection_list:
                iterator=QTreeWidgetItemIterator(listing)
                while iterator.value():
                    item=iterator.value()
                    if item.childCount() and item.isExpanded():r['expanded'].append(self.tree_path(item))
                    iterator+=1
            self.page_positions[self.page_key(r)]=(self.current_cursor,list(self.cursor_stack))
        elif self.page=='reader':r['inspector_scroll']=self.reader_text.verticalScrollBar().value()
        elif self.page=='spatial':
            r['map_x']=self.spatial.scroll.horizontalScrollBar().value();r['map_y']=self.spatial.scroll.verticalScrollBar().value()
            e=self.spatial.edges[self.spatial.active] if self.spatial.active is not None else {}
            r['map_edge']=[e.get(k,'') for k in ('source','predicate','target')] if e else []
            if hasattr(self.spatial,'view_state'):r.update(self.spatial.view_state())
        self.navigation.update(r)

    @staticmethod
    def page_key(r):return route_identity(dict(r,selected=None,tab=0))

    @staticmethod
    def tree_path(item):
        path=[]
        while item:
            ref=reference(item.data(0,Qt.UserRole+2))
            path.append(ref['record_type']+':'+ref['record_id'] if ref else str(item.data(0,Qt.UserRole+3) or item.text(0)))
            item=item.parent()
        return list(reversed(path))

    def visit(self,route):
        self.checkpoint_navigation();self.navigation.visit(route);self.page_positions.pop(self.page_key(route),None);self.current_cursor=None;self.cursor_stack=[];self.render_route();self.preference_timer.start()

    def go_home(self,rid=None):
        if not self.initialized:return
        if isinstance(rid,bool):rid=None
        self.search_timer.stop();self.search.blockSignals(True);self.search.clear();self.search.blockSignals(False)
        self.visit({'section':'overview','parameters':{'loe_id':rid or self.config.get('alpha3',{}).get('home_effort')}})

    def open_collection(self,kind=None,view='all',parameters=None,search=''):
        if not self.initialized:return
        self.visit({'section':kind or 'records','kind':kind,'view':view,'parameters':parameters or {},'search':search})

    def open_mail(self,task='unanswered',options=None):
        if isinstance(task,bool):task='unanswered'
        if not self.initialized:return
        options=copy.deepcopy(options or {})
        if 'view_id' in options:options['custom_view_id']=options.pop('view_id')
        self.visit({'section':'Email','kind':'Email','view':'mail:'+task,'parameters':options})

    def open_review(self):
        self.open_mail()

    def setup_review(self):
        if self.initialized:
            from .review_setup import ReviewSetup
            self.run_dialog(ReviewSetup(self))

    def open_record(self,rid,kind=None):
        if not rid or not self.initialized:return
        if not kind:
            if self.current_detail and self.current_detail['id']==rid:kind=self.current_detail['target_kind']
            elif rid in self.home_data.get('records',{}):kind=self.home_data['records'][rid]['target_kind']
            elif rid in self.spatial.records:kind=self.spatial.records[rid]['target_kind']
        self.checkpoint_navigation();r=self.navigation.current;r['selected']={'record_id':rid,'record_type':kind or self.guess_kind(rid)};r['tab']=0;r['inspector_scroll']=0;self.navigation.seen(r['selected']);self.navigation.visit(r);self.render_route();self.preference_timer.start()

    def guess_kind(self,rid):
        if rid in self.record_kinds:return self.record_kinds[rid]
        for prefix,kind in [('mail_','Email'),('event_','Event'),('person_','Person'),('document_','Document'),('org_','Organization'),('loe_','LineOfEffort'),('wsorg_','WorkspaceOrganization')]:
            if rid.startswith(prefix):return kind
        return self.navigation.current.get('kind') or 'Email'

    def render_route(self):
        self.route_generation+=1;self.inline_pending.clear()
        for op in ('home','list','reading','neighborhood'):self.reads[op].invalidate()
        r=self.navigation.current;self.back_button.setVisible(self.navigation.can_back);self.search.blockSignals(True);self.search.setText(r['search']);self.search.blockSignals(False)
        self.selected_id=None;self.current_detail=None;self.reader_record=None
        navkey='mail' if r.get('kind')=='Email' else 'LineOfEffort' if r['section']=='overview' else r.get('kind')
        for key,b in self.navbuttons.items():b.setChecked(key==navkey)
        for i in range(self.effort_l.count()):
            b=self.effort_l.itemAt(i).widget()
            if b and hasattr(b,'isCheckable') and b.isCheckable():b.setChecked(r['section']=='overview' and b.property('effort_id')==(r['parameters'].get('loe_id') or self.home_data.get('loe_id')))
        self.crumb.setText('Workspace' if r['section']=='overview' else 'O3 / '+self.type_name(r['kind']) if r.get('kind') else 'O3 / Everything')
        if r['selected']:
            self.selected_id=self.reader_record=r['selected']['record_id']
            if r['tab']==5:
                self.page='spatial';self.stack.setCurrentWidget(self.spatial);self.spatial.canvas.setEnabled(False);self.spatial.subtitle.setText('Loading connections…');self.request_read('neighborhood',{'id':self.selected_id,'kind':r['selected']['record_type']})
            else:
                self.page='reader';self.read_purpose='reader';self.stack.setCurrentWidget(self.reader);self.reader_title.setText('');self.reader_metadata.setText('Loading…');self.reader_text.clear();self.reader_notice.hide();self.clear_layout(self.reader_context_l);self.followup.setEnabled(False);self.request_read('reading',{'id':self.selected_id,'kind':r['selected']['record_type']})
        elif r['view'].startswith('mail:'):
            self.page='mail';self.stack.setCurrentWidget(self.mail_workspace);cached=self.page_positions.get(self.page_key(r))
            if cached:self.mail_workspace.cursor,self.mail_workspace.previous=cached[0],list(cached[1])
            self.mail_workspace.load(r,reset=not bool(cached))
        elif r['section']=='overview':
            self.page='overview';self.stack.setCurrentWidget(self.overview);self.overview.setEnabled(False);self.home_title.setText('Loading workspace…');self.request_read('home',{'id':r['parameters'].get('loe_id')})
        elif r['section'] in ('health','settings'):
            self.page='overview';self.stack.setCurrentWidget(self.overview);self.request_read('home',{});QTimer.singleShot(0,self.open_sources if r['section']=='health' else self.open_settings)
        else:
            self.page='collection';cached=self.page_positions.get(self.page_key(r));self.current_cursor,previous=cached or (None,[]);self.cursor_stack=list(previous)
            self.restore_page_index=0 if cached else r.get('page_index',0)
            self.stack.setCurrentWidget(self.collection);self.kind_choice.blockSignals(True);self.kind_choice.setCurrentIndex(max(0,self.kind_choice.findData(r.get('kind'))));self.kind_choice.blockSignals(False);self.load_collection()
        self.adapt()
        self.update_commands()

    def restore_scroll(self):
        r=self.navigation.current;generation=self.route_generation
        widget=self.reader_text if self.page=='reader' else self.mail_list.tree
        value=r['inspector_scroll'] if self.page=='reader' else r['scroll']
        QTimer.singleShot(0,lambda:widget.verticalScrollBar().setValue(value) if generation==self.route_generation else None)

    def back(self):
        self.checkpoint_navigation()
        if self.navigation.move(-1):self.render_route()
    def forward(self):
        self.checkpoint_navigation()
        if self.navigation.move(1):self.render_route()
    def refresh(self):
        if self.initialized and not self.storage_maintenance:
            self.checkpoint_navigation();self.page_positions.clear();self.current_cursor=None;self.cursor_stack=[];self.render_route()
    def find_records(self):
        route=self.navigation.current;route.update(search=self.search.text().strip(),selected=None,preview=None,page_index=0,scroll=0)
        self.visit(route)
    def change_kind(self):self.open_collection(self.kind_choice.currentData())

    def load_collection(self):
        self.collection_generation+=1;self.clear_collection()
        r=self.navigation.current;self.collection_state.setText('Loading…')
        self.configure_button.setText('Set up Review' if r['view']=='view:email:needs_attention' else 'Personalize this view')
        name=self.view_name(r['view']);self.collection_title.setText('Search results' if r['search'] else name if r['view']!='all' else self.type_name(r['kind']) if r.get('kind') else 'Everything')
        self.request_read('list',{'kind':r.get('kind'),'view':r['view'],'view_parameters':r['parameters'],'search':r['search'],'inactive':r['inactive'],'cursor':self.current_cursor,'sorts':self.config.get('alpha5',{}).get('view_sorts',{}).get(self.sort_key(r))})

    def clear_collection(self):
        self.collection_ready=False;self.collection_data={};self.next_cursor=None
        self.collection_coverage.clear();self.coverage_action.hide()
        self.collection_guidance.clear();self.collection_guidance.hide()
        self.collection_list.clear();self.configured_table.tree.clear();self.configured_table.payload=None
        self.structured_view.tree.clear();self.structured_view.notice.clear()
        self.collection_stack.setCurrentWidget(self.collection_list)
        for button in (self.collection_open,self.next_page,self.previous_page):button.setEnabled(False)

    def collection_unavailable(self,reason):
        self.clear_collection();self.collection_state.setText('Unavailable · '+str(reason))

    def active_collection(self):
        surface=self.collection_stack.currentWidget()
        return surface if surface is self.collection_list else surface.tree

    def collection_reference(self,item):
        if item is None:return None
        if self.collection_stack.currentWidget() is self.record_surface.widget:
            rid=item.data(0,Qt.UserRole)
            return reference({'record_id':rid,'record_type':self.record_surface.kind_for(rid)}) if rid else None
        if self.active_collection() is self.collection_list:
            rid=item.data(Qt.UserRole)
            return reference({'record_id':rid,'record_type':self.record_kinds.get(rid)})
        return reference(item.data(0,Qt.UserRole+2))

    def selected_collection_reference(self):
        if self.page!='collection' or not self.collection_ready:return None
        listing=self.active_collection();item=listing.currentItem()
        return self.collection_reference(item) if item and item.isSelected() else None

    def update_collection_action(self,*_):
        self.collection_open.setEnabled(self.selected_collection_reference() is not None)
        if hasattr(self,'commands'):self.update_commands()

    def finish_collection(self,route,generation):
        if generation!=(self.route_generation,self.collection_generation) or self.page!='collection':return
        self.collection_ready=True;listing=self.active_collection();wanted=route.get('preview');selection=None
        if self.collection_stack.currentWidget() is self.record_surface.widget:
            self.record_surface.restore(route);self.next_page.setEnabled(bool(self.next_cursor));self.previous_page.setEnabled(bool(self.cursor_stack));self.update_collection_action();self.status.setVisible(bool(self.preferences_error));return
        listing.clearSelection();listing.setCurrentItem(None)
        if listing is not self.collection_list and route.get('expanded'):
            expanded={tuple(p) for p in route['expanded']};iterator=QTreeWidgetItemIterator(listing)
            while iterator.value():
                item=iterator.value()
                if item.childCount():item.setExpanded(tuple(self.tree_path(item)) in expanded)
                iterator+=1
        if wanted:
            if listing is self.collection_list:
                items=(listing.item(i) for i in range(listing.count()))
            else:
                def tree_items():
                    iterator=QTreeWidgetItemIterator(listing)
                    while iterator.value():
                        yield iterator.value();iterator+=1
                items=tree_items()
            selection=next((item for item in items if self.collection_reference(item)==wanted),None)
            if selection:
                if listing is not self.collection_list:
                    parent=selection.parent()
                    while parent:parent.setExpanded(True);parent=parent.parent()
                listing.setCurrentItem(selection)
            else:self.collection_state.setText(self.collection_state.text()+' · Previous selection is not on this page; results may have changed.')
        # Qt lays out a newly populated tree after set_result returns.
        QTimer.singleShot(0,lambda:listing.verticalScrollBar().setValue(route['scroll']) if generation==(self.route_generation,self.collection_generation) and self.page=='collection' else None)
        self.next_page.setEnabled(bool(self.next_cursor));self.previous_page.setEnabled(bool(self.cursor_stack))
        self.update_collection_action();self.status.setVisible(bool(self.preferences_error))

    def view_name(self,vid):
        for view in self.summary_data.get('registry',{}).get('views',[]):
            if view['view_id']==vid:return view.get('name') or view.get('label') or 'Saved view'
        return 'Connected records' if vid=='view:loe:dashboard' else 'Review' if vid=='view:email:needs_attention' else 'Saved view'

    def paint_collection(self,data):
        self.collection_data=data;self.next_cursor=data.get('next_cursor');state=data.get('state','available');reason=data.get('reason')
        if state=='available' and self.next_cursor and len(self.cursor_stack)<self.restore_page_index:
            self.cursor_stack.append(self.current_cursor);self.current_cursor=self.next_cursor;self.load_collection()
            self.collection_state.setText('Restoring saved page '+str(self.restore_page_index+1)+'…');return
        self.restore_page_index=0
        self.collection_state.setText((str(data.get('total',0))+' saved items' if state=='available' else state.replace('_',' ').capitalize())+(' · '+str(reason) if reason else ''))
        r=self.navigation.current;generation=(self.route_generation,self.collection_generation);structured=data.get('structured',{});self.collection_list.clear()
        coverage=coverage_text(data.get('coverage',self.summary_data.get('coverage')),r.get('kind'),r['view'],self.config.get('last_capture_result'))
        self.collection_coverage.setText(coverage);self.coverage_action.setVisible(bool(coverage))
        if structured.get('unknown_count'):self.collection_state.setText(self.collection_state.text()+' · '+str(structured['unknown_count'])+' unclassified')
        if (structured.get('kind') and structured['kind'] not in ('configured_table','list','table','records','standard','grouped_records')) or structured.get('sections') or structured.get('nodes') or structured.get('threads') or any('messages' in row for row in structured.get('rows',[])):
            self.collection_stack.setCurrentWidget(self.structured_view);self.structured_view.set_payload(structured,labels=data.get('labels'))
        else:
            self.collection_stack.setCurrentWidget(self.record_surface.widget)
            def loaded(response):
                if generation!=(self.route_generation,self.collection_generation) or self.page!='collection':return
                if not response.get('ok'):
                    self.collection_unavailable(response.get('error',{}).get('message','View fields could not be read.'));return
                sorts=self.config.get('alpha5',{}).get('view_sorts',{}).get(self.sort_key(r)) or structured.get('sorts') or []
                self.record_surface.set_result(r.get('kind'),r['view'],data['items'],list_schema_fields(response['result']),sorts=sorts,group_counts=structured.get('group_counts'))
                self.finish_collection(r,generation)
            self.km.request('builder.schema',{'record_type':r.get('kind') or 'Email'},loaded,owner=self)
            return
        if state in ('configuration_required','source_stale','unconfigured','not_evaluable','disabled','stale') and not data['items']:
            guidance='Open Unanswered in Mail to check replies without a custom setup. This older saved view needs its original saved settings.' if r['view']=='view:email:needs_attention' else 'Choose another view, or review the choices saved for this one.'
            self.collection_guidance.setText(guidance);self.collection_guidance.show()
        self.finish_collection(r,generation)

    def next_records(self):
        if self.next_cursor:
            self.checkpoint_navigation();self.cursor_stack.append(self.current_cursor);self.current_cursor=self.next_cursor;self.load_new_page()
    def previous_records(self):
        if self.cursor_stack:self.checkpoint_navigation();self.current_cursor=self.cursor_stack.pop();self.load_new_page()
    def load_new_page(self):
        r=self.navigation.current;r.update(preview=None,scroll=0,page_index=len(self.cursor_stack),expanded=[]);self.navigation.update(r);self.restore_page_index=0;self.load_collection()
    def open_selected(self,*_):
        selected=self.active_reference() or self.selected_collection_reference()
        if selected:self.open_record(selected['record_id'],selected['record_type'])

    def paint_reader(self,d):
        self.crumb.setText('O3 / '+self.type_name(d['kind']))
        self.reader_record=d['id'];self.reader_title.setText(d['title']);self.reader_title.setToolTip('<pre>'+html.escape(d['title'])+'</pre>');self.reader_title.setAccessibleName(d['title'])
        p=d['properties'];kind=d['kind'];stamp=(p.get('received_at_utc') or p.get('sent_at_utc')) if kind=='Email' else ''
        meta=address_value(p.get('from')) if kind=='Email' else d['secondary']
        self.reader_metadata.setText(meta+('  ·  '+self.local_stamp(stamp) if stamp else '  ·  '+('User-owned' if d['basis'] in ('workspace_manual','user_created','user_adopted') else 'Retained record')))
        content=d.get('content');overview=overview_data(d)
        if content:
            body=content.get('text') or 'No readable source text was supplied.'
            notice=content.get('notice','') if content.get('status')!='available' or content.get('truncated') else ''
            extraction=content.get('extraction_status');warnings=content.get('extraction_warnings') or []
            if (extraction and extraction!='complete') or warnings:
                warning=' · '.join(display_value(v) for v in warnings)
                notice=('Partial extraction' if extraction=='partial' else 'Source extraction: '+str(extraction or 'warnings supplied'))+(' · '+warning if warning else '')+(' · '+notice if notice else '')
        else:
            body='\n\n'.join(title+'\n'+'\n'.join(label+'  '+str(value) for label,value in values) for title,values in overview.get('sections',[]));notice=' · '.join(overview.get('notices',[]))
        self.reader_more.setVisible(bool(content and content.get('next_offset') is not None))
        self.reader_notice.setText(notice if len(notice)<=220 else notice[:217]+'… See Source details.');self.reader_notice.setToolTip('<pre>'+html.escape(notice)+'</pre>');self.reader_notice.setAccessibleName(notice);self.reader_notice.setVisible(bool(notice));self.reader_text.setHtml(self.body_html(body));self.draw_reader_context(d);self.followup.setVisible(kind=='Email');self.followup.setEnabled(True);self.reader_text.setFocus();self.restore_scroll();self.status.setVisible(bool(self.preferences_error));self.adapt()

    def load_more_reader(self):
        detail=self.current_detail;content=(detail or {}).get('content') or {}
        if content.get('next_offset') is None:return
        rid=detail['id'];fingerprint=content.get('source_sha');position=self.reader_text.verticalScrollBar().value()
        self.reader_more.setEnabled(False)
        def loaded(data):
            self.reader_more.setEnabled(True)
            if not self.current_detail or self.current_detail['id']!=rid:return
            if data.get('read_error') or data.get('source_sha')!=fingerprint:
                self.show_information('Reading interrupted','The saved content changed or could not be read. Reopen this item to continue.',True);return
            detail['content']={**data,'text':content.get('text','')+data.get('text','')}
            self.paint_reader(detail);QTimer.singleShot(0,lambda:self.reader_text.verticalScrollBar().setValue(position))
        self.request_inline(rid,detail['target_kind'],loaded,operation='content',offset=content['next_offset'],limit=200000)

    def body_html(self,body):
        self.reader_body=body
        escaped=html.escape(body).replace('\n\n','</p><p>').replace('\n','<br>')
        a=self.appearance;family=font_family(a,reader=True);color=theme_tokens(a['theme'],a)['body']
        return '<html><head><style>body{font-family:"'+family+'";font-size:'+str(round(17*self.scale*a['reader_scale']))+'px;color:'+color+'}p{line-height:'+str(round(a['line_spacing']*100))+'%;margin-bottom:'+str(round(14*self.scale))+'px}</style></head><body><p>'+escaped+'</p></body></html>'

    def add_connection(self,layout,d,e):
        layout.addWidget(text(connection_caption(d['kind'],e)+(' · by you' if e.get('authority')!='core' else ''),'small',True))
        layout.addWidget(wrap_btn(e['other_label'],lambda checked=False,edge=e,detail=d:self.show_connection(edge,detail),'link',kind='link'))

    def draw_reader_context(self,d,layout=None):
        l=layout or self.reader_context_l;self.clear_layout(l);l.addWidget(text('Related information','section'));
        mailrow=self.mail_workspace.rows.get(d['id'])
        if mailrow and mailrow.get('message_count',1)>1:
            key=mailrow['conversation_key']
            if key.get('mailbox_id') and key.get('conversation_id'):l.addWidget(btn('View conversation ('+str(mailrow['message_count'])+' messages)',lambda:self.open_conversation({'mailbox_id':key['mailbox_id'],'conversation_id':key['conversation_id']}),kind='secondary'))
        l.addWidget(btn('See connections together',lambda:self.open_spatial(d['id']),'link','link'))
        for e in d['relationships']:self.add_connection(l,d,e)
        if not d['relationships']:l.addWidget(text('No recorded connections.','muted',True))
        if d['kind']=='Event':l.addWidget(text('Recorded occurrence. Attendance is not established.','small',True))
        if d['kind']=='Email':l.addWidget(btn('Review coverage',self.open_sources,'sources','link'))
        l.addStretch();l.addWidget(btn('Edit details',lambda:self.run_dialog(ManualValues(self.km,d['target_kind'],d['id'],self)),kind='link'))

    def open_context(self):
        d=QDialog(self);d.setWindowTitle('Context');d.resize(420,650);l=QVBoxLayout(d);l.setContentsMargins(24,24,24,18)
        scroll=QScrollArea();scroll.setWidgetResizable(True);w=QWidget();wl=QVBoxLayout(w);scroll.setWidget(w);l.addWidget(scroll)
        if self.current_detail:self.draw_reader_context(self.current_detail,wl)
        else:self.paint_home_context(wl)
        l.addWidget(btn('Close',d.accept));self.run_dialog(d)

    def open_spatial(self,rid=None):
        selected=self.active_reference()
        rid=rid or (selected or {}).get('record_id') or self.reader_record or self.home_data.get('loe_id')
        if not rid:return
        self.checkpoint_navigation();r=self.navigation.current;r['selected']={'record_id':rid,'record_type':self.current_detail['target_kind'] if self.current_detail and self.current_detail['id']==rid else self.guess_kind(rid)};r['tab']=5;self.navigation.visit(r);self.render_route()

    def show_connection(self,e,d):
        from .relationship_evidence import RelationshipEvidence
        labels={k:{'label':v.get('title',v.get('label',k))} for k,v in {**self.home_data.get('records',{}),**self.spatial.records}.items()}
        labels[d.get('id',self.spatial.seed)]={'label':d.get('title','Selected record')};labels[e['other']]={'label':e['other_label']}
        dialog=RelationshipEvidence(connection_caption(d['kind'],e),e,labels,self);dialog.setWindowTitle('Why this connection?')
        actions=QHBoxLayout();actions.addWidget(btn('Open connected item',lambda:(dialog.accept(),self.open_record(e['other'],e['other_target_kind'])),kind='primary'));actions.addWidget(btn('About the original item',lambda:(dialog.accept(),self.open_evidence(e['source'])),kind='link'));actions.addStretch();dialog.layout().insertLayout(dialog.layout().count()-1,actions);self.run_dialog(dialog)

    def open_evidence(self,rid=None):
        rid=rid or (self.active_reference() or {}).get('record_id') or self.reader_record or self.selected_id
        if not rid:return
        if self.current_detail and self.current_detail['id']==rid:self.evidence_dialog(self.current_detail)
        else:self.read_purpose='evidence';self.request_read('reading',{'id':rid})

    def evidence_dialog(self,d):
        dialog=QDialog(self);dialog.setWindowTitle('About this information');dialog.resize(740,600)
        layout=QVBoxLayout(dialog);layout.setContentsMargins(24,20,24,20)
        layout.addWidget(text(d['title'],'section',True))
        layout.addWidget(text('This is the information saved in O3. Your original files are unchanged; newer information may exist elsewhere.','muted',True))
        reader=QTextBrowser();reader.setAccessibleName('Where this information came from')
        props=d.get('properties',{});lines=[]
        if d['kind']=='Email':
            for label,key in [('From','from'),('To','to'),('Copied to','cc')]:
                value=props.get(key)
                if value:lines.append(label+': '+(', '.join(address_value(v) for v in value) if isinstance(value,list) else address_value(value)))
            for label,key in [('Sent','sent_at_utc'),('Received','received_at_utc'),('Saved from source','captured_at_utc')]:
                if props.get(key):lines.append(label+': '+self.local_stamp(props[key]))
        else:
            for heading,values in overview_data(d).get('sections',[]):
                lines.append(heading)
                lines.extend(label+': '+str(value) for label,value in values)
        lines.append('\nCreated in O3.' if d.get('basis') in ('workspace_manual','user_created','user_adopted') else '\nSaved from your selected files.')
        content=d.get('content') or {}
        if content.get('notice'):lines.append(str(content['notice']))
        lines.append('\nConnections')
        lines.extend(connection_caption(d['kind'],edge)+': '+edge['other_label'] for edge in d.get('relationships',[]))
        if not d.get('relationships'):lines.append('No connections have been found for this item.')
        reader.setPlainText('\n'.join(lines));layout.addWidget(reader,1)
        actions=QHBoxLayout();actions.addWidget(btn('Your folders',lambda:(dialog.accept(),self.open_sources()),kind='link'));actions.addStretch();actions.addWidget(btn('Close',dialog.accept));layout.addLayout(actions);self.run_dialog(dialog)

    def views_menu(self):
        m=QMenu(self);kind=self.navigation.current.get('kind');m.addAction('All records of this type',lambda:self.open_collection(kind))
        for v in self.summary_data.get('registry',{}).get('views',[]):
            if (not kind or v.get('owner_type',v.get('record_type'))==kind) and 'property_id' not in v.get('parameter_schema',{}):m.addAction(v.get('name',v.get('label',v['view_id'])),lambda checked=False,view=v:self.choose_view(view))
        if kind and kind.startswith('wsrt_'):m.addAction('Dashboard',lambda:self.open_collection(kind,'workspace:dashboard'))
        m.addSeparator();m.addAction('Create view…',lambda:self.edit_view(new=True));m.addAction('Manage saved views…',self.configure);m.exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))

    def choose_view(self,v):
        parameters={}
        if v.get('parameter_schema'):
            d=ViewParameters(self.km,v,{},self)
            if d.exec()!=QDialog.Accepted:return
            parameters=d.parameters
        if v.get('owner_type',v.get('record_type'))=='Email' and v['view_id'].startswith('view:user:'):
            from .mail_workspace import STARTERS
            task=next((key for key,value in STARTERS.items() if value==v['view_id']),'all')
            self.open_mail(task,{'view_id':v['view_id']})
        else:self.open_collection(v.get('owner_type',v.get('record_type')),v['view_id'],parameters)

    def configure(self):
        kind=self.navigation.current.get('kind') or 'Email';self.run_dialog(SavedViewsDialog(self.km,kind,self,get_presentation=lambda vid,owner:self.get_view_layout(owner,vid),persist_presentation=lambda vid,owner,value:self.persist_view_layout(owner,vid,value),display_name=self.type_name))
    def configure_surface_columns(self,surface):
        from .workspace_editor import ViewColumns
        dialog=QDialog(self);dialog.setWindowTitle('Columns');dialog.resize(640,610)
        layout=QVBoxLayout(dialog);layout.setContentsMargins(24,24,24,24)
        layout.addWidget(text('Columns','title'));layout.addWidget(text('Choose the details you want in this project preview.','muted',True))
        editor=ViewColumns(surface.kind,surface.fields,self.get_view_layout(surface.kind,surface.view_id));layout.addWidget(editor,1)
        notice=text('','small',True);layout.addWidget(notice)
        def save():
            try:self.persist_view_layout(surface.kind,surface.view_id,editor.value())
            except (ValueError,OSError) as exc:notice.setText('Not saved. '+str(exc));return
            dialog.accept();self.refresh()
        buttons=QHBoxLayout();buttons.addWidget(btn('Cancel',dialog.reject));buttons.addStretch();buttons.addWidget(btn('Save',save,kind='primary'));layout.addLayout(buttons);self.run_dialog(dialog)

    def get_view_layout(self,owner,vid):
        key=vid if vid!='all' else 'view:all:'+str(owner or 'all')
        saved=self.config.get('display',{}).get('views',{}).get(key)
        if saved:return saved
        legacy=self.config.get('display',{}).get('columns',{}).get(owner or 'all')
        return view_layout(owner,{'columns':legacy} if legacy else None)
    def persist_view_layout(self,owner,vid,value):
        key=vid if vid!='all' else 'view:all:'+str(owner or 'all')
        self.config.setdefault('display',{}).setdefault('views',{})[key]=value
        self.persist_preferences(raise_on_error=True)
    def save_view_layout(self,value):
        if not self.collection_ready:return
        r=self.navigation.current
        try:self.persist_view_layout(r.get('kind') or 'Email',r['view'],value)
        except (ValueError,OSError):pass  # Retained in memory; persist_preferences displays the failure.
    def edit_view(self,new=False):
        if not self.initialized:return
        r=self.navigation.current;kind=r.get('kind') or 'Email'
        vid=self.mail_workspace.view_id if self.page=='mail' else r['view']
        self.open_view_editor(kind,vid,new=new)

    def open_view_editor(self,kind,vid,*,new=False,seed_exclusion=None):
        definition=None if new or vid=='all' else next((v for v in self.summary_data.get('registry',{}).get('views',[]) if v['view_id']==vid),None)
        def loaded(response):
            if not response.get('ok'):self.show_information('View unavailable',response['error']['message'],True);return
            dialog=ViewEditor(self.km,kind,response['result'],definition,self,presentation=self.get_view_layout(kind,vid),
                persist_presentation=lambda saved,owner,value:self.persist_view_layout(owner,saved,value),seed_exclusion=seed_exclusion,focus_section='exclusions' if seed_exclusion else None)
            if self.run_dialog(dialog)==QDialog.Accepted and getattr(dialog,'saved_id',None) and dialog.saved_id!=vid:
                self.open_mail('all',{'view_id':dialog.saved_id}) if kind=='Email' else self.open_collection(kind,dialog.saved_id)
        self.km.request('builder.schema',{'record_type':kind},loaded,owner=self)

    def record_menu(self):
        d=self.current_detail
        if not d:return
        self.update_commands();m=QMenu(self);m.addAction(self.commands['properties'])
        m.addAction('Pin record' if not self.navigation.pinned({'record_type':d['target_kind'],'record_id':d['id']}) else 'Unpin record',self.pin_current)
        m.addAction(self.commands['compare']);m.addAction(self.commands['evidence'])
        if d['kind']=='Email':m.addAction(self.commands['draft'])
        elif d['kind']=='LineOfEffort':
            m.addSeparator();m.addAction('Pin as home project',lambda:self.set_home(d['id']));m.addAction('Open project',lambda:self.go_home(d['id']));m.addAction('Edit project…',lambda:self.run_dialog(LOEEditor(self.km,d['km_record'],adopt=d['basis'] not in ('user_created','user_adopted'),parent=self)))
            m.addAction('Keep this project',lambda:self.command('loe.retain',{'record_id':d['id']}));m.addAction('Archive project',lambda:self.confirm_command('Archive project','Retain history and remove this effort from active results?', 'loe.archive' if d['basis'] in ('user_created','user_adopted') else 'loe.retire',{'record_id':d['id']}))
        elif d['kind']=='Organization':m.addAction('Curate organization…',lambda:self.run_dialog(OrganizationEditor(self.km,d['km_record'],self)))
        elif d['kind'].startswith('wsrt_'):
            t=next((t for t in self.summary_data.get('workspace_types',[]) if t['type_id']==d['kind']),None)
            if t:m.addAction('Edit record…',lambda:self.run_dialog(WorkspaceRecordEditor(self.km,t,d['km_record'],self)))
            m.addAction('Restore record' if not d['active'] else 'Archive record',lambda:self.confirm_command('Change record state','Keep the record and its history, and change its availability?', 'workspace.record.restore' if not d['active'] else 'workspace.record.archive',{'record_type':d['kind'],'record_id':d['id']}))
        m.exec(self.followup.mapToGlobal(self.followup.rect().topRight()))

    def followup_menu(self):
        d=self.current_detail
        if not d:return
        m=QMenu(self);row=self.mail_workspace.rows.get(d['id'])
        if row and row.get('eligible_record_ids') and self.mail_workspace.task!='all':
            action=m.addAction('Stop waiting' if self.mail_workspace.task=='waiting' else 'No reply needed',lambda:self.mail_workspace.dismiss(row));action.setEnabled(row.get('action_supported',True));action.setToolTip(row.get('action_unavailable_reason') or '')
        if row:m.addAction('Why is this here?',lambda:self.mail_workspace.explain(row))
        if self.mail_workspace.undo_token:m.addAction('Undo last mail choice',self.mail_workspace.undo)
        m.addAction('Draft a reply…',self.draft_reply);m.addAction('Your email addresses…',self.setup_review)
        m.exec(self.followup.mapToGlobal(self.followup.rect().topLeft()))

    def draft_reply(self,detail=None):
        d=detail or self.current_detail
        if not d:
            ref=self.active_reference()
            if ref and ref['record_type']=='Email':self.request_inline(ref['record_id'],'Email',lambda data:self.draft_reply(data) if not data.get('read_error') else self.action_status('Could not open this message.',True))
            return
        dialog=QDialog(self);dialog.setWindowTitle('Draft reply');dialog.resize(720,550);l=QVBoxLayout(dialog);l.setContentsMargins(24,24,24,20)
        l.addWidget(text('Draft reply','title'));l.addWidget(text('To '+address_value(d['properties'].get('from'))+' · Re: '+d['title'],'muted',True));l.addWidget(text('Local clipboard draft. Copying does not send or record a reply.','small',True));draft=QPlainTextEdit();draft.setPlaceholderText('Write your reply…');l.addWidget(draft,1)
        key=d['id'];draft.setPlainText(self.config.get('alpha3',{}).get('drafts',{}).get(key,''));notice=text('','small',True);l.addWidget(notice)
        def keep():
            value=draft.toPlainText()
            if len(value)>50000:notice.setText('Draft exceeds 50,000 characters. Shorten it before saving, or copy the full text.');return
            self.config.setdefault('alpha3',{}).setdefault('drafts',{})[key]=value
            notice.setText('Draft saved locally.' if self.persist_preferences() else 'Draft was not saved. Your text is still here; retry Save draft or copy it.')
        def clip():QApplication.clipboard().setText(draft.toPlainText());notice.setText('Copied to clipboard. Nothing was sent.')
        row=QHBoxLayout();row.addWidget(btn('Save draft',keep,kind='secondary'));row.addWidget(btn('Copy text',clip,kind='primary'));row.addStretch();row.addWidget(btn('Close',dialog.accept));l.addLayout(row);self.run_dialog(dialog)

    def command(self,op,params):
        def done(r):
            if not r['ok']:self.show_information('Change rejected',r['error']['message']+'\nYour saved records were not replaced.',True)
        self.km.request(op,params,done,expected=self.km.revision,owner=self)
    def confirm_command(self,title,message,op,params):
        if confirm(self,title,message):self.command(op,params)
    def new_effort(self):self.run_dialog(LOEEditor(self.km,parent=self))
    def set_home(self,rid):self.config.setdefault('alpha3',{})['home_effort']=rid;self.persist_preferences();self.go_home(rid)
    def pin_current(self):
        if self.current_detail:
            d=self.current_detail
            try:self.navigation.toggle_pin({'record_type':d['target_kind'],'record_id':d['id']});self.preference_timer.start()
            except ValueError as exc:self.show_information('Pins',str(exc),True)
    def compare(self):
        if not self.current_detail:return
        d=self.current_detail;picker=RecordPicker(self.km,[d['target_kind']],multiple=False,parent=self)
        if picker.exec()==QDialog.Accepted and picker.selected_records:self.run_dialog(CompareDialog(self.km,{'record_type':d['target_kind'],'record_id':d['id']},picker.selected_records[0],self))

    def perspectives(self):
        from .panels import Perspectives
        self.run_dialog(Perspectives(self))
    def open_settings(self):
        if self.initialized:
            from .panels import SettingsPanel
            self.run_dialog(SettingsPanel(self))
    def open_sources(self):
        from .panels import SourcesPanel
        self.sources_dialog=SourcesPanel(self);self.run_dialog(self.sources_dialog);self.sources_dialog=None
    def update_sources(self):
        if getattr(self,'sources_dialog',None):self.sources_dialog.refresh()
    def open_navigation(self):
        self.update_commands();m=QMenu(self)
        m.addAction('Mail',self.open_mail)
        for kind,label in [('Person','People'),('Document','Documents'),('Event','Calendar'),('LineOfEffort','Projects')]:m.addAction(label,lambda checked=False,k=kind:self.open_collection(k))
        m.addSeparator()
        for key in ('records','perspectives','sources','settings','palette'):m.addAction(self.commands[key])
        m.exec(self.workspace_menu.mapToGlobal(self.workspace_menu.rect().bottomLeft()))
    def open_options(self):
        self.update_commands();m=QMenu(self)
        for key in ('palette','refresh','context','perspectives'):m.addAction(self.commands[key])
        m.addSeparator()
        for scale,label in [(1.0,'Standard text'),(1.25,'Larger text · 125%'),(1.5,'Larger text · 150%')]:
            a=m.addAction(label,lambda checked=False,s=scale:self.apply_scale(s));a.setCheckable(True);a.setChecked(scale==self.scale)
        m.addSeparator();m.addAction('Configure rules and columns…',self.edit_view)
        a=m.addAction('Include retained / inactive records',self.toggle_inactive);a.setCheckable(True);a.setChecked(self.navigation.current['inactive']);m.addAction('Manage saved views…',self.configure);m.exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))
    def toggle_columns(self):self.edit_view()
    def open_conversation(self,params):self.open_collection('Email','view:email:conversations',params)
    def toggle_inactive(self):r=self.navigation.current;r['inactive']=not r['inactive'];r['selected']=None;self.visit(r)
    def install_commands(self):
        # Menus, keyboard shortcuts and the palette share the same actions.
        self.commands={}
        specs=[('home','Workspace',self.go_home,'','Open the main workspace'),
            ('records','Everything',lambda:self.open_collection(),'','Browse saved items'),
            ('review','Unanswered mail',self.open_review,'','Find mail with no reply from you found'),
            ('back','Back',self.back,'Alt+Left','Return to the previous location'),
            ('forward','Forward',self.forward,'Alt+Right','Return to the next location'),
            ('find','Find in workspace',self.search.setFocus,'Ctrl+K','Search captured records'),
            ('refresh','Refresh',self.refresh,'F5','Reload the current query'),
            ('perspectives','Saved perspectives',self.perspectives,'','Views, pins, recent records and saved locations'),
            ('sources','Sources',self.open_sources,'','Capture coverage and intake'),
            ('settings','Settings',self.open_settings,'Ctrl+,','Display, configuration and recovery'),
            ('open','Open item',self.open_selected,'','Open the selected collection record'),
            ('context','Context',self.open_context,'','People, dates and supporting records'),
            ('connections','Connections',lambda:self.open_spatial(),'','See related records together'),
            ('evidence','Source, evidence and history',self.open_evidence,'','Inspect retained evidence, changes and rule results'),
            ('compare','Compare records',self.compare,'','Compare the current record with another'),
            ('properties','Edit properties',self.edit_current_properties,'F2','Review or edit properties on the selected items'),
            ('draft','Draft a reply',self.draft_reply,'','Local draft and clipboard; does not send'),
            ('setup_review','Your email addresses',self.setup_review,'','Choose the email addresses you use'),
            ('configure','Configure this view',self.edit_view,'','Change rules, columns and ordering for this view'),
            ('palette','Commands',self.command_palette,'Ctrl+Shift+P','Find actions, saved views and records')]
        for key,title,callback,shortcut,description in specs:
            action=QAction(title,self);action.setStatusTip(description);action.setToolTip(description)
            if shortcut:action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda checked=False,fn=callback:fn());self.addAction(action);self.commands[key]=action
        self.more.setToolTip('View options · Commands: Ctrl+Shift+P');self.update_commands()

    def update_commands(self):
        if not hasattr(self,'commands'):return
        ready=self.initialized and not self.closing and not self.storage_maintenance
        for action in self.commands.values():action.setEnabled(ready)
        if not ready:return
        d=self.current_detail;ref=self.active_reference()
        available={'back':self.navigation.can_back,'forward':self.navigation.can_forward,
            'open':ref is not None or self.selected_collection_reference() is not None,
            'context':self.page in ('reader','overview') and bool(d or self.home_data.get('loe_id')),
            'connections':bool(ref or d or self.reader_record or (self.page=='overview' and self.home_data.get('loe_id'))),
            'evidence':bool(ref),'compare':bool(d),'properties':bool(ref),'draft':bool(ref and ref['record_type']=='Email')}
        for key,value in available.items():self.commands[key].setEnabled(value)

    def edit_current_properties(self):
        d=self.current_detail
        if d:self.run_dialog(ManualValues(self.km,d['target_kind'],d['id'],self))
        elif self.active_surface():self.record_actions.edit_properties(self.active_surface().context())

    def command_palette(self):
        if not self.initialized:return
        self.update_commands();entries=[]
        for key,action in self.commands.items():
            if key=='palette':continue
            shortcut=action.shortcut().toString(QKeySequence.NativeText)
            entries.append({'label':action.text(),'description':action.statusTip()+(' · '+shortcut if shortcut else '')+(' · Unavailable in this context' if not action.isEnabled() else ''),
                            'enabled':action.isEnabled(),'payload':{'command':key}})
        for v in self.summary_data.get('registry',{}).get('views',[]):
            if v.get('state')!='retired' and 'property_id' not in v.get('parameter_schema',{}):entries.append({'label':v.get('name',v.get('label','Saved view')),'description':'View · '+self.type_name(v.get('owner_type',v.get('record_type'))),'payload':{'view':v['view_id']}})
        d=CommandPalette(self.km,entries,record_search=True,parent=self)
        if self.run_dialog(d)==QDialog.Accepted and d.choice:
            if d.choice.get('ref'):self.open_record(d.choice['ref']['record_id'],d.choice['ref']['record_type'])
            elif d.choice.get('view'):
                view=next((v for v in self.summary_data.get('registry',{}).get('views',[]) if v['view_id']==d.choice['view']),None)
                if view:self.choose_view(view)
            elif d.choice.get('command') in self.commands:
                self.update_commands();self.commands[d.choice['command']].trigger()

    def apply_scale(self,scale,persist=True):
        value={**self.appearance,'text_scale':scale}
        self.apply_appearance(value,preview=self.preview_appearance_values is not None)
        if persist:self.config.setdefault('alpha3',{})['text_scale']=self.scale;self.preference_timer.start()

    def apply_appearance(self,value,preview=False):
        self.appearance=normalize_appearance(value);self.scale=self.appearance['text_scale'];theme=self.appearance['theme']
        self.preview_appearance_values=copy.deepcopy(self.appearance) if preview else None
        self.preview_theme=theme if preview else None
        app=QApplication.instance();app.setProperty('o3_theme',theme);app.setProperty('o3_appearance',self.appearance)
        app.setProperty('o3_tokens',theme_tokens(theme,self.appearance))
        font=QFont(font_family(self.appearance));font.setPixelSize(round(15*self.scale));app.setFont(font)
        app.setStyleSheet(stylesheet_for(scale=self.scale,theme=theme,appearance=self.appearance))
        app.setPalette(native_palette(theme,self.appearance))
        if self.current_detail and self.page=='reader':
            scroll=self.reader_text.verticalScrollBar().value();cursor=self.reader_text.textCursor()
            anchor,position=cursor.anchor(),cursor.position()
            self.reader_text.setHtml(self.body_html(self.reader_body))
            cursor=self.reader_text.textCursor();limit=self.reader_text.document().characterCount()-1
            cursor.setPosition(min(anchor,limit));cursor.setPosition(min(position,limit),QTextCursor.MoveMode.KeepAnchor)
            self.reader_text.setTextCursor(cursor);self.reader_text.verticalScrollBar().setValue(scroll)
        for listing in (self.collection_list,):
            listing.doItemsLayout();listing.viewport().update()
        for surface in (self.record_surface,self.mail_workspace.surface,self.home_surface):surface.widget.set_appearance(self.appearance)
        configured=self.configured_table
        if configured.layout_value:
            spacing=configured.layout_value['density'];spacing=self.appearance['density'] if spacing=='inherit' else spacing
            height={'compact':28,'balanced':34,'comfortable':42}[spacing]+(16 if configured.layout_value['secondary'] else 0)
            configured.tree.setStyleSheet('QTreeView::item { min-height: '+str(height)+'px; }')
        self.adapt();self.update()
    def resizeEvent(self,event):super().resizeEvent(event);self.adapt()
    def adapt(self):
        if not hasattr(self,'sidebar'):return
        self.compact=self.width()<round(1120*self.scale)
        compact=self.compact;quiet=self.appearance['navigation']=='focus'
        self.sidebar.setVisible(not compact and not quiet);self.workspace_menu.setVisible(compact or quiet)
        self.right.setVisible(not compact);self.reader_side.setVisible(not compact)
        self.context_button.setVisible(compact and self.page in ('reader','overview'))
        self.crumb.setVisible(not compact);self.main_l.setContentsMargins(20 if compact else 36,20,20 if compact else 36,20)
        self.reading.setMaximumWidth({'narrow':640,'comfortable':820,'wide':16777215}[self.appearance['reading_width']])
        if hasattr(self,'reader_title'):
            maximum=self.reader_title.fontMetrics().lineSpacing()*2+6
            self.reader_title.setMaximumHeight(maximum)
            needed=self.reader_title.fontMetrics().boundingRect(QRect(0,0,max(100,self.reader_title.width()),100000),Qt.TextWordWrap,self.reader_title.text()).height()
            self.title_expand.setVisible(needed>maximum)

    def apply_theme(self,theme,persist=True):
        value={**self.appearance,'theme':theme}
        self.apply_appearance(value,preview=not persist)
        if persist:self.config.setdefault('alpha3',{})['theme']=self.appearance['theme']
        if persist:self.persist_preferences()
        self.update()

    def show_full_title(self):
        if not self.current_detail:return
        dialog=QDialog(self);dialog.setWindowTitle('Full title');dialog.resize(680,400)
        layout=QVBoxLayout(dialog);reader=QTextBrowser();reader.setPlainText(self.current_detail['title']);reader.setAccessibleName('Full title');layout.addWidget(reader);layout.addWidget(btn('Close',dialog.accept));self.run_dialog(dialog)

    def load_projects(self):
        self.project_request=getattr(self,'project_request',0)+1;request=self.project_request
        def loaded(response):
            if request!=self.project_request or not response.get('ok') or self.closing:return
            rows=response['result'].get('rows',[]);self.clear_layout(self.effort_l);self.project_heading.setVisible(bool(rows))
            for row in rows[:6]:
                title=row.get('properties',{}).get('name') or 'Project'
                button=ProjectDropButton(title,{'record_type':'LineOfEffort','record_id':row['record_id']},self.record_actions,self)
                button.setProperty('kind','nav');button.clicked.connect(lambda checked=False,rid=row['record_id']:self.go_home(rid));self.effort_l.addWidget(button)
        self.km.request('records.query',{'record_type':'LineOfEffort','page_size':6},loaded,owner=self)

    def update_notices(self):
        if not hasattr(self,'health_notice'):return
        failure=getattr(self,'source_failure',None)
        result=self.config.get('last_capture_result') or {}
        errors=result.get('errors') or []
        if failure or errors:
            self.health_notice.setText('Some files could not be checked. Your saved information is still available. Open Your folders to retry.')
            self.health_notice.show()
        else:self.health_notice.hide()
        self.save_message.setText('Your latest preferences or draft have not been saved. They are still here; retry saving.')
        self.save_notice.setVisible(bool(self.preferences_error))
        if hasattr(self,'project_notice'):self.project_notice.setVisible((self.summary_data.get('project_reconciliation') or {}).get('state')=='pending')

    def run_dialog(self,dialog):
        self.active_dialog=dialog;result=dialog.exec();self.active_dialog=None;return result
    def show_data(self,title,value,notice=''):
        d=QDialog(self);d.setWindowTitle(title);d.resize(800,590);l=QVBoxLayout(d);l.setContentsMargins(24,20,24,18);l.addWidget(text(title,'section',True))
        if notice:l.addWidget(text(notice,'small',True))
        w=QPlainTextEdit();w.setReadOnly(True);w.setPlainText(value if isinstance(value,str) else json.dumps(value,indent=2,ensure_ascii=False,default=str));l.addWidget(w,1);l.addWidget(btn('Close',d.accept));self.run_dialog(d)
    def show_information(self,title,message,warning=False):
        box=QMessageBox(self);box.setTextFormat(Qt.PlainText);box.setWindowTitle(title);box.setText(message);box.setIcon(QMessageBox.Warning if warning else QMessageBox.Information);box.exec()

    def choose_source(self):
        path=QFileDialog.getExistingDirectory(self,'Choose your O3 export folder',self.config.get('source_root',''))
        if not path:return
        try:root,coverage=validate_source(Path(path),self.state)
        except (ValueError,OSError) as exc:self.show_information('Source unavailable',str(exc),True);return
        had_source='source_root' in self.config;previous_source=self.config.get('source_root')
        self.config['source_root']=str(root)
        if not self.persist_preferences():
            if had_source:self.config['source_root']=previous_source
            else:self.config.pop('source_root',None)
            self.update_sources()
            self.show_information('Source selection not saved',
                'The active source selection has not changed. No capture check was started. '
                'Resolve the save error and choose the folder again.',warning=True)
            return
        self.update_sources();self.scan_sources()
    def scan_sources(self):
        if not self.initialized or self.scan_busy or self.storage_maintenance or self.closing:return
        source=self.config.get('source_root')
        if not source:self.choose_source();return
        self.scan_busy=True;self.status.setText('Checking selected source captures…');self.status.show();self.submit.emit('scan',{'source':source});self.update_sources()
    def periodic_scan(self):
        if not self.config.get('paused',True) and self.config.get('source_root'):self.scan_sources()
    def persist_preferences(self,*,raise_on_error=False):
        if not self.initialized or self.storage_maintenance:
            if raise_on_error:raise ValueError('Workspace preferences are unavailable during initialization or recovery.')
            return False
        self.checkpoint_navigation();self.config['navigation']=retained_navigation_preferences(self.navigation.saved(),self.config.get('navigation'))
        saved_geometry=self.config.get('geometry')
        if isinstance(saved_geometry,dict):
            # Retain legacy pane settings and extension fields for continuity.
            self.config['geometry']={**saved_geometry,'window':bytes(self.saveGeometry().toBase64()).decode('ascii')}
        else:self.config['geometry']=bytes(self.saveGeometry().toHex()).decode('ascii')
        try:save_config(self.state,self.config)
        except (ValueError,OSError) as exc:
            self.preferences_error=str(exc);self.update_notices();self.status.setText('Local changes were not saved · '+str(exc));self.status.show()
            self.preferences_saved.emit(False,str(exc))
            if raise_on_error:raise
            return False
        if self.preferences_error:self.status.hide()
        self.preferences_error=None;self.update_notices();self.preferences_saved.emit(True,'');return True

    def on_maintenance(self,data):
        self.initialized=False;self.pending_maintenance=data;self.km.maintenance=True;self.startup_timer.stop();self.tick.stop();self.main.setEnabled(True);self.status.setText('Protected upgrade required · no writer opened');self.status.show()
        self.startup_finished.emit(False)
        if data.get('mode')=='starter_conversion_required':QTimer.singleShot(0,self.begin_starter_upgrade)
        else:QTimer.singleShot(0,self.open_sources)
    def begin_starter_upgrade(self):
        if self.closing or self.storage_maintenance or not self.pending_maintenance:return
        if self.pending_maintenance.get('mode')!='starter_conversion_required':return
        self.last_error=None;self.storage_maintenance=True;self.startup_sample_timer.start()
        dialog=QDialog(self);dialog.setWindowTitle('Updating your workspace')
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        layout=QVBoxLayout(dialog)
        layout.addWidget(text('Your saved workspace is being updated.'))
        layout.addWidget(text('A recovery copy is checked before changes are saved.'))
        self.upgrade_label=text('Preparing recovery copy…');layout.addWidget(self.upgrade_label)
        self.upgrade_bar=QProgressBar(dialog);self.upgrade_bar.setRange(0,0);layout.addWidget(self.upgrade_bar)
        layout.addWidget(btn('Close O3',self.close,kind='link'))
        dialog.rejected.connect(self.close)
        self.upgrade_dialog=dialog;dialog.resize(480,190);dialog.show()
        self.submit.emit('upgrade_starters',{})

    def finish_upgrade_dialog(self):
        dialog=self.upgrade_dialog
        self.upgrade_dialog=None
        if dialog is not None:dialog.accept();dialog.deleteLater()

    def on_upgrade_progress(self,data):
        if self.closing or self.upgrade_dialog is None:return
        labels={
            'checkpoint_source_archive_verification':'Checking saved source locations',
            'checkpoint_provenance_verification':'Checking saved evidence and history',
            'checkpoint_source_boundaries':'Checking recovery location',
            'checkpoint_database_snapshot':'Copying saved workspace',
            'checkpoint_inventory':'Collecting recovery files',
            'checkpoint_write':'Writing recovery copy',
            'checkpoint_verify':'Checking recovery copy',
            'checkpoint_verify_manifest':'Checking recovery file inventory',
            'checkpoint_verify_extract':'Checking recovery file contents',
            'checkpoint_verify_database_open':'Opening recovery database',
            'checkpoint_verify_database_schema':'Checking recovery database',
            'checkpoint_verify_database_identity':'Checking workspace identity',
            'checkpoint_verify_archive_boundaries':'Checking recovery source locations',
            'checkpoint_verify_provenance':'Checking recovered evidence and history',
            'checkpoint_verify_ownership':'Checking workspace ownership',
            'checkpoint_verify_cleanup':'Finishing recovery checks',
            'checkpoint_verify_complete':'Recovery copy verified',
            'starter_definition_conversion':'Updating saved views'}
        label=labels.get(data.get('stage'),'Updating your workspace')
        counts=data.get('counts') or {};done=counts.get('files_completed');total=counts.get('files_total')
        if type(done) is int and type(total) is int and total>0:
            self.upgrade_bar.setRange(0,total);self.upgrade_bar.setValue(done)
            label+=f' · {done:,} of {total:,} files'
        else:
            self.upgrade_bar.setRange(0,0)
            if type(done) is int:label+=f' · {done:,} files checked'
        self.upgrade_label.setText(label);self.status.setText(label);self.status.show()

    def begin_migration(self):
        if not self.pending_maintenance or self.storage_maintenance:return
        if self.pending_maintenance.get('mode')=='starter_conversion_required':
            self.begin_starter_upgrade();return
        source=self.config.get('source_root') or QFileDialog.getExistingDirectory(self,'Select the existing O3Data root')
        if not source:return
        backup,_=QFileDialog.getSaveFileName(self,'Create required pre-upgrade backup','','O3 backup (*.zip)')
        if not backup:return
        if not confirm(self,'Upgrade saved Workspace','Confirm the independent backup location is authorized. The existing database, evidence archive and settings will be backed up and verified before the supported upgrade. Continue?'):return
        self.storage_maintenance=True;self.submit.emit('migrate',{'source':source,'backup':backup,'approved':True,'confirmed':True})
    def do_backup(self):
        source=self.config.get('source_root')
        if not source:self.show_information('Source required','Choose the associated O3Data root first.');return
        path,_=QFileDialog.getSaveFileName(self,'Create complete Workspace backup',self.config.get('backup_root',''),'O3 backup (*.zip)')
        if path and confirm(self,'Backup destination','Confirm this destination is authorized and independently protected from the Workspace and read-only sources.'):
            self.config['backup_root']=str(Path(path).parent)
            if self.persist_preferences():self.submit.emit('backup',{'destination':path,'source':source,'approved':True})
    def do_restore(self):
        if not self.initialized or self.storage_maintenance:return
        source=self.config.get('source_root')
        if not source:self.show_information('Source required','Choose the associated O3Data root before restoring.');return
        path,_=QFileDialog.getOpenFileName(self,'Select a complete O3 backup',self.config.get('backup_root',''),'O3 backup (*.zip)')
        if path:self._restore_selection={'backup':path,'source':source,'confirmed':True};self.submit.emit('verify_backup',{'path':path})
    def begin_restore(self,selection):
        if self.closing or self.storage_maintenance:return
        if not self.persist_preferences():return
        self.preference_timer.stop();self.tick.stop();self.storage_maintenance=True;self.restoring=True;self.km.maintenance=True;self.km.invalidate()
        for reads in self.reads.values():reads.invalidate()
        self.lock.release_legacy();self.submit.emit('restore',selection)
    def finish_restore(self):
        if not self.lock.reacquire_legacy():self.show_information('Restore stopped','Could not reacquire the restored Workspace lock. Close O3 and preserve the backup.',True);return
        try:self.config=read_config(self.state)
        except (ValueError,OSError) as exc:self.show_information('Restored settings unavailable',str(exc),True);return
        self.navigation=Navigation(self.config.get('navigation'));self.nav_ready=False;self.initialized=False;self.restoring=False;self.storage_maintenance=False
        for reads in self.reads.values():reads.reset()
        self.submit.emit('reopen',{})
    def closeEvent(self,event):
        if self.allow_close:self.lock.unlock();event.accept();return
        event.ignore()
        if self.closing:return
        if self.initialized and not self.storage_maintenance and not self.persist_preferences():
            if not confirm(self,'Local changes are not saved','The Workspace settings could not be written. Local drafts and presentation changes remain in memory. Close and discard those unsaved changes? Saved knowledge transactions remain committed.'):return
        self.finish_upgrade_dialog()
        self.closing=True;self.tick.stop();self.preference_timer.stop();self.search_timer.stop();self.startup_timer.stop();self.main.setEnabled(False);self.status.setText('Closing workspace…');self.status.show();self.shutdown.emit()
    def done_closing(self):
        # QThread.finished precedes native thread-local cleanup. Join before
        # allowing QApplication/process teardown to release SQLite/Qt resources.
        if not self.thread.wait(5000):
            self.status.setText('Waiting for workspace shutdown to finish…');self.status.show()
            QTimer.singleShot(100,self.done_closing);return
        self.allow_close=True;self.close()


def run():
    state=default_state();state.mkdir(parents=True,exist_ok=True)
    try:config=read_config(state)
    except (ValueError,OSError) as exc:QMessageBox.critical(None,'O3 configuration error',str(exc));return 2
    lock=WorkspaceLock(state)
    if not lock.tryLock(0):QMessageBox.warning(None,'O3 already running','This Workspace is already open.');return 3
    win=O3Window(state,config,lock);win.show();return QApplication.instance().exec()
