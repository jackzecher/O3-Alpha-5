"""O3 Universal — native synthetic-only GUI preview, no operational state.

Uses the exact shared O3Window.build_ui(), QListWidget delegate, native Qt
styles and inspector widgets. This is not a NIPR source-access mechanism.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtWidgets import QApplication, QMainWindow, QListWidgetItem

KIT=Path(__file__).resolve().parent.parent/'O3_PRODUCT'
sys.path.insert(0,str(KIT/'APP'))
from o3_core.store import KINDS
from o3_km.catalog import VIEWS
from o3_gui.app import O3Window, EmailStackDelegate, Navigation, LatestRead, KMClient, TYPE_NAMES
from o3_gui.display_preferences import validate_display_preferences
from o3_gui.presentation import native_palette, stylesheet_for, density_metrics

SUBJECTS=('Updated coordination timeline','Weekly integration summary',
          'Re: Site visit coordination','Information request — final response',
          'Meeting minutes and decisions','Draft coordination brief',
          'Upcoming milestone review','Re: Documentation handoff',
          'Planning update','Status notes','Open actions','Technical review')
PEOPLE=('Jordan Taylor','Morgan Ellis','Avery Brooks','Casey Morgan',
        'Riley Chen','Sam Rivera','Drew Bennett','Quinn Parker')

def records():
    output=[]
    for kind in KINDS:
        for i in range(24 if kind=='Email' else 6):
            sender=PEOPLE[i%len(PEOPLE)]
            date=(datetime(2026,10,22,14,35)-timedelta(minutes=i*59)).strftime('%Y-%m-%d %H:%M')
            title=SUBJECTS[i%len(SUBJECTS)] if kind=='Email' else f'{TYPE_NAMES[kind]} {i+1:02d}'
            extra={'sender':sender,'received':date,'folder':'Inbox' if i%5 else 'Sent'} if kind=='Email' else {'received':date}
            id=f'synthetic:{kind.lower()}:{i+1:03d}'
            output.append({'id':id,'kind':kind,'label':title,'title':title,
                'secondary':sender+' · Fictional data','active':True,'updated':date+'Z',
                'fields':extra,'basis':'synthetic_gui_preview','scope':'synthetic',
                'identity_key':id,'source_sha':'0'*64,
                'properties':{'source':'SYNTHETIC','sender':sender,'date':date},
                'added':{'body_text':'Synthetic demonstration message. No operational data is opened.'},
                'relationships':[],'km_rules':[],'workspace_properties':{},
                'trace':{'rule_evaluations':[]},
                'km_history':{'core_trace':{'record_history':[],'relationship_history':[]},'workspace_audit':[]}})
    return output

def preview_catalog():
    return [{**v,'state':'enabled','visible':True,'display_label':v['name']}
            for v in VIEWS.values() if v.get('enabled_default') and v.get('kind')!='parameterized']

class NativePreview(O3Window):
    def __init__(self):
        # Skip O3Window's operational __init__: it starts EngineWorker/QThread
        # which would open the actual SQLite workspace. Build the Qt UI only.
        QMainWindow.__init__(self)
        self.state=Path('SYNTHETIC_PREVIEW_NO_REAL_STATE')
        self.config={'paused':True,'source_root':'','backup_root':''}
        self.lock=None
        self.initialized=False;self.closing=False;self.allow_close=True
        self.scan_pending=False;self.paused=True
        self.current_kind='Email';self.selected_id='';self.selected_detail=None
        self.offset=0;self.total=0;self.pages=[];self.last_scan=None
        self._list_generation=0;self._list_key=None;self._refresh_target=200
        self.audit=None;self.rules_catalog=None;self.last_error=None;self.last_backup=None
        self.search_text='';self._search_editing=False
        self.search_timer=QTimer(self);self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.commit_search)
        self.navigation=Navigation(None)
        self._nav_ready=False;self._restoring_location=False;self._restore_location=None
        self._selected_reference=None;self._pending_detail=False;self._failed_detail=False;self._record_labels={}
        self._list_reads=LatestRead();self._detail_reads=LatestRead()
        self._current_detail_token=-1;self._preview_token=0;self._preview_context=None;self._preview_inflight=False
        self._link_offset=0;self._link_rows=[];self._syncing_links=False
        self._comparison_ref=None;self._window_cursor=None;self._page_capacity=100
        self.registry=preview_catalog();self.view_fields={}
        self.view_by_kind={kind:next((v['view_id'] for v in self.registry
                                      if v['owner_type']==kind),'all') for kind in KINDS}
        self.current_view=self.view_by_kind['Email'];self.active_section='Email'
        self.workspace_types={};self._workspace_rail_signature=None
        self._view_editor=None;self._view_editor_loading=False
        self.next_cursor=None;self.view_parameters={};self.structured_pages=[];self.dashboard_pages={}
        self.storage_maintenance=False;self.restoring=False;self.pending_maintenance=None
        self.km=KMClient(lambda op,payload:None,self)
        self.display=validate_display_preferences(None);self.density='compact'
        self.config['display']=self.display
        self._restoring_columns=False;self._column_kind=None;self._column_specs=()
        self._narrow=False;self._narrow_pane='results';self._sidebar_override=False
        self.sidebar_collapsed=False;self.inspector_collapsed=False;self.results_collapsed=False
        self.sidebar_width=202;self._record_sizes=[700,420]
        self.preference_timer=QTimer(self)
        self.setObjectName('O3MainWindow')
        self.setWindowTitle('O3 Universal · AI GUI Preview · SYNTHETIC ONLY')
        self.resize(1440,850);self.setMinimumSize(640,540)
        self.samples=records()
        self.build_ui()                  # The same Qt Widgets GUI as the actual O3 app
        self.apply_density('compact',persist=False)
        self.initialized=True
        self.mode_chip.setText('SYNTHETIC GUI PREVIEW')
        self.status.setText('SYNTHETIC ONLY · NO DATABASE OR OPERATIONAL SOURCES')
        self.scan_button.setEnabled(False)
        self.custom_type_button.setEnabled(False)
        self.workspace_rule_button.setEnabled(False)
        self.view_options_button.setEnabled(False)
        self.pages_stack.setCurrentIndex(1)  # Show the real Results + Inspector Qt workbench.
        self._configure_sidebar('Email')
        self.load_list(reset=True)
        if self.pages:self.open_id(self.pages[0]['id'])
        self.tabs.setCurrentIndex(0)
        self.update_pane_layout()

    # No disk persistence, KM worker, or operational Core requests whatsoever.
    def send(self,*args,**kwargs):return None
    def queue_preferences(self,*args,**kwargs):return None
    def persist_preferences(self,*args,**kwargs):return None
    def checkpoint_navigation(self,*args,**kwargs):return None
    def visit_navigation(self,*args,**kwargs):return None
    def choose_source(self,*args,**kwargs):return None
    def choose_backup(self,*args,**kwargs):return None
    def request_scan(self,*args,**kwargs):return None
    def do_backup(self,*args,**kwargs):return None
    def do_restore(self,*args,**kwargs):return None
    def export_rdf(self,*args,**kwargs):return None
    def export_diag(self,*args,**kwargs):return None
    def do_audit(self,*args,**kwargs):return None
    def show_rules(self,*args,**kwargs):return None
    def manage_workspace_types(self,*args,**kwargs):return None
    def configure_workspace_rules(self,*args,**kwargs):return None
    def set_paused(self,*args,**kwargs):return None
    def toggle_pause(self,*args,**kwargs):return None
    def configure_view_parameters(self,*args,**kwargs):return None
    def edit_current_view(self,*args,**kwargs):return None
    def create_record(self,*args,**kwargs):return None
    def manage_record(self,*args,**kwargs):return None
    def edit_values(self,*args,**kwargs):return None
    def begin_migration(self,*args,**kwargs):return None
    def show_palette(self,*args,**kwargs):return None
    def request_source_preview(self,*args,**kwargs):return None
    def closeEvent(self,event):
        self.search_timer.stop()
        event.accept()

    def change_search_scope(self,*args):
        self.update_search_hints();self.load_list(reset=True)

    def load_list(self,reset=False):
        if not self.initialized:return
        query=self.search.text().strip().casefold()
        all_kinds=bool(query) and self.search_scope.currentData()=='all'
        self.pages=[item for item in self.samples
                    if (all_kinds or item['kind']==self.current_kind)
                    and (not query or query in (item['label']+' '+item['secondary']).casefold())]
        self.total=len(self.pages);self.next_cursor=None
        if self.current_kind=='Email' and not all_kinds:
            self.result_stack.setCurrentIndex(4)
            self.populate_email_stack()
        else:
            self.result_stack.setCurrentIndex(0)
            if self._column_kind!=self.current_kind:self.apply_columns(self.current_kind)
            self.populate_table()
        self.kind_crumb.setText(TYPE_NAMES[self.current_kind])
        self.records_count.setText('All Mail' if self.current_kind=='Email' else TYPE_NAMES[self.current_kind])
        self.record_count_small.setText(f'{len(self.pages)} / {len(self.pages)}')
        self.list_hint.setText('Fictional records for native Qt layout inspection')
        self.load_more.setEnabled(False)

    def open_id(self,rid,kind=None,tab=None):
        data=next((item for item in self.samples if item['id']==rid),None)
        if data is None:return
        self.selected_id=rid;self.selected_detail=data
        self.detail_title.setText(data['label'])
        self.detail_meta.setText('Synthetic GUI Preview · Fictional record')
        self.detail_secondary.setText(data['secondary'])
        self.selection_context.setText('Preview only')
        self.pin_button.setEnabled(False)
        self.record_overview.set_record(data)
        self.fill_properties(data)
        self.overview_text.setPlainText(json.dumps(data['properties'],indent=2))
        self.rule_text.setPlainText('No KM evaluation: synthetic-only design preview')
        self.evidence_lens.clear();self.history_lens.clear()
        self.graph.set_links({'id':rid,'relationships':[]})
        self.graph_meta.setText('NO OPERATIONAL RELATIONSHIPS')
        self.link_table.setRowCount(0)
        self.workspace_summary.clear();self.workspace_summary.setVisible(False)
        if tab is not None:self.tabs.setCurrentIndex(tab)
        for idx,item in enumerate(self.pages):
            if item['id']==rid:
                self.email_stack.blockSignals(True)
                self.email_stack.setCurrentRow(idx)
                self.email_stack.blockSignals(False)
                break


def run_preview(emit,screenshot_path=None):
    app=QApplication(sys.argv[:1])
    app.setApplicationName('O3 Universal Synthetic GUI Preview')
    app.setStyle('Fusion')
    app.setPalette(native_palette())
    app.setStyleSheet(stylesheet_for('compact'))
    win=NativePreview();win.show()
    def after_paint():
        emit('READY',100,'Native synthetic GUI visible')
        if screenshot_path is not None:
            try:
                dest=Path(screenshot_path).expanduser().resolve()
                if (dest.suffix.lower()!='.png' or not dest.parent.is_dir()
                        or dest.is_relative_to(KIT.parent)):
                    raise ValueError()
                if not win.grab().save(str(dest),'PNG'):
                    raise OSError()
            except (OSError,ValueError):
                emit('FAIL',code='PREVIEW_SCREENSHOT_FAILED')
                app.exit(2)
                return
            app.quit()
    QTimer.singleShot(700,after_paint)
    result=app.exec()
    if result==0:emit('EXIT',100,'Synthetic GUI closed')
    return result
