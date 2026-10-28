"""Secondary native surfaces over the same commands and presentation preferences."""
import json
from copy import deepcopy
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (QDialog,QWidget,QVBoxLayout,QHBoxLayout,QListWidget,QListWidgetItem,
    QStackedWidget,QComboBox,QCheckBox,QLineEdit,QFileDialog,QScrollArea)
from .ui import text,btn,rule,section,wrap_btn
from .workspace_editor import SharedSettings,ManagedDialog,WorkspaceConfiguration,confirm
from .workspace_type_editor import WorkspaceTypeManager,WorkspaceRecordEditor
from .record_actions import LOEEditor,OrganizationEditor,CalendarSources
from . import __version__
from .source_status import FAMILIES,STATES,source_states
from .human_customization import SavedViewsDialog, SECTION_NAMES
from .presentation import THEME_CHOICES
from .appearance import appearance_from_config, store_appearance, saved_looks
from .appearance_editor import AppearanceEditor


class SettingsNavigation(QListWidget):
    """Keep every settings label readable when the application text size changes."""
    def __init__(self,parent=None):
        super().__init__(parent);self.setObjectName('settingsNav')
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
    def fit_labels(self):
        font=QFont(self.font());font.setWeight(QFont.Weight.DemiBold)
        metrics=QFontMetrics(font)
        longest=max((metrics.horizontalAdvance(self.item(i).text()) for i in range(self.count())),default=0)
        self.setFixedWidth(max(175,longest+32+2*self.frameWidth()))
    def showEvent(self,event):
        super().showEvent(event);self.fit_labels()
    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange,QEvent.Type.ApplicationFontChange,QEvent.Type.StyleChange):
            self.fit_labels()


class SettingsPanel(QDialog):
    def __init__(self,w):
        super().__init__(w);self.w=w;self.setWindowTitle('Settings');self.resize(1080,850)
        self.opening_theme=w.config.get('alpha3',{}).get('theme','harbor');self.opening_scale=w.scale
        self.opening_appearance=appearance_from_config(w.config)
        self.appearance_committed=False;self.appearance_save_failed=False
        root=QVBoxLayout(self);root.setContentsMargins(28,24,28,20);root.setSpacing(18)
        root.addWidget(text('Settings','title'));root.addWidget(text('O3 is ready to use. Make it feel like yours when you want to.','muted',True))
        self.section_choice=QComboBox();self.section_choice.setAccessibleName('Settings section');root.addWidget(self.section_choice)
        body=QHBoxLayout();nav=SettingsNavigation();self.settings_navigation=nav
        for title in ('Appearance','Your details','Saved views','Data & backups','About'):nav.addItem(title)
        self.section_choice.addItems([nav.item(i).text() for i in range(nav.count())])
        self.section_choice.currentIndexChanged.connect(nav.setCurrentRow);nav.currentRowChanged.connect(self.section_choice.setCurrentIndex)
        nav.fit_labels()
        stack=QStackedWidget();body.addWidget(nav);body.addWidget(stack,1);root.addLayout(body,1);nav.currentRowChanged.connect(stack.setCurrentIndex)
        def page(title,description):
            widget=QWidget();l=QVBoxLayout(widget);l.setContentsMargins(20,4,12,4);l.setSpacing(14);l.addWidget(text(title,'section'));l.addWidget(text(description,'muted',True));scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(widget);stack.addWidget(scroll);return l
        self.appearance_editor=AppearanceEditor(self.opening_appearance,saved_looks(w.config.get('alpha3',{}).get('saved_looks')))
        self.theme=self.appearance_editor.theme;self.scale_choice=self.appearance_editor.scale_choice
        stack.addWidget(self.appearance_editor);self.appearance_editor.changed.connect(self.preview_appearance)
        l=page('Your details','Tell O3 which email addresses are yours, and how you want dates displayed.')
        l.addWidget(btn('Email addresses, dates & time…',lambda:w.run_dialog(SharedSettings(w.km,self)),kind='primary'));l.addWidget(btn('Calendar names…',lambda:w.run_dialog(CalendarSources(w.km,w.config.get('source_root'),self)),kind='secondary'))
        l.addWidget(rule());l.addWidget(text('Remembering your place','body'))
        restore=QCheckBox('Restore my last location when opening');restore.setChecked(w.navigation.restore_last);restore.toggled.connect(lambda value:(setattr(w.navigation,'restore_last',value),w.preference_timer.start()));l.addWidget(restore)
        l.addWidget(btn('Clear recent items',lambda:(w.navigation.forget_recent(),w.persist_preferences()),kind='link'));l.addStretch()
        l=page('Saved views','Keep useful ways of looking at your mail, people, documents and projects. No setup is required.')
        owner=QComboBox();owner.setAccessibleName('Collection for saved views')
        for kind,title in SECTION_NAMES.items():owner.addItem(title,kind)
        for item in w.summary_data.get('workspace_types',[]):
            if item.get('active',True):owner.addItem(item['plural'],item['type_id'])
        l.addWidget(owner)
        l.addWidget(btn('Manage saved views…',lambda:w.run_dialog(SavedViewsDialog(w.km,owner.currentData(),self,get_presentation=lambda vid,kind:w.get_view_layout(kind,vid),persist_presentation=lambda vid,kind,value:w.persist_view_layout(kind,vid,value))),kind='primary'))
        l.addWidget(rule());l.addWidget(text('Your own lists','body'));l.addWidget(text('Create a collection for things you want to keep track of yourself.','muted',True));l.addWidget(btn('Manage collections…',lambda:w.run_dialog(WorkspaceTypeManager(w.km,self)),kind='secondary'));l.addStretch()
        l=page('Data & backups','O3 reads supported files from a folder on your computer. Saved copies remain available when that folder is offline.')
        l.addWidget(btn('Manage data sources…',lambda:w.run_dialog(SourcesPanel(w)),kind='primary'));l.addWidget(rule())
        l.addWidget(text('A backup keeps your saved information and settings together. Close other copies of O3 before restoring.','muted',True))
        l.addWidget(btn('Create a backup…',w.do_backup,kind='secondary'));l.addWidget(btn('Restore a backup…',w.do_restore,kind='secondary'));l.addStretch()
        l=page('O3',__version__)
        l.addWidget(text('A calmer place to find your work and understand how it fits together.','body',True));l.addWidget(text('Your information is kept on this computer. This is a preview version.','muted',True));l.addStretch()
        self.save_notice=text('','small',True);root.addWidget(self.save_notice)
        self.retry_settings=btn('Try saving appearance again',self.save_appearance,kind='secondary');root.addWidget(self.retry_settings);w.preferences_saved.connect(self.saved)
        self.saved(not bool(w.preferences_error),w.preferences_error or '')
        nav.setCurrentRow(0);row=QHBoxLayout();row.addStretch();row.addWidget(btn('Cancel appearance',self.reject,kind='secondary'));row.addWidget(btn('Save appearance',self.save_appearance,kind='primary'));root.addLayout(row)
        self.adapt_settings()
    def adapt_settings(self):
        if not hasattr(self,'settings_navigation'):return
        compact=self.width()<round(920*self.w.scale)
        self.settings_navigation.setVisible(not compact);self.section_choice.setVisible(compact)
    def resizeEvent(self,event):
        super().resizeEvent(event);self.adapt_settings()
    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange,QEvent.StyleChange):self.adapt_settings()
    def preview_appearance(self,*_):
        self.w.apply_appearance(self.appearance_editor.get_value(),preview=True)
    def save_appearance(self):
        prefs=self.w.config.setdefault('alpha3',{})
        keys=('theme','text_scale','appearance','saved_looks')
        before={key:deepcopy(prefs[key]) for key in keys if key in prefs}
        store_appearance(self.w.config,self.appearance_editor.get_value())
        prefs['saved_looks']=deepcopy(self.appearance_editor.looks)
        self.appearance_save_failed=False
        if not self.w.persist_preferences():
            for key in keys:
                if key in before:prefs[key]=before[key]
                else:prefs.pop(key,None)
            self.appearance_save_failed=True;self.saved(False,'');return
        self.w.apply_appearance(self.appearance_editor.get_value());self.appearance_committed=True;super().accept()
    def reject(self):
        if not self.appearance_committed:
            self.w.apply_appearance(self.opening_appearance)
        super().reject()
    def saved(self,ok,error):
        if ok and self.appearance_save_failed:return
        self.save_notice.setText('' if ok else 'These settings could not be saved. Your choices are still here.');self.save_notice.setVisible(not ok);self.retry_settings.setVisible(not ok)
    def export(self):
        path,_=QFileDialog.getSaveFileName(self,'Export canonical RDF graph','','Turtle (*.ttl)')
        if path:self.w.submit.emit('export_rdf',{'path':path,'source':self.w.config.get('source_root')})

class SourcesPanel(QDialog):
    def __init__(self,w):
        super().__init__(w);self.w=w;self.setWindowTitle('Data sources');self.resize(790,610)
        l=QVBoxLayout(self);l.setContentsMargins(28,24,28,20);l.setSpacing(15);l.addWidget(text('Your data','title'));l.addWidget(text('Choose the folder containing your supported mail, contacts, calendar and document exports. O3 reads the copies in that folder; it does not connect to your live accounts.','muted',True))
        self.path=text('','body',True);l.addWidget(self.path);self.checked=text('','small',True);l.addWidget(self.checked);l.addWidget(rule());self.coverage=QWidget();self.coverage_l=QVBoxLayout(self.coverage);self.coverage_l.setContentsMargins(0,0,0,0);self.coverage_l.setSpacing(16);l.addWidget(self.coverage,1)
        self.pause=QCheckBox('Pause automatic folder checks');self.pause.setChecked(w.config.get('paused',True));self.pause.toggled.connect(self.set_pause);l.addWidget(self.pause)
        self.notice=text('','muted',True);l.addWidget(self.notice)
        row=QHBoxLayout();self.check=btn('Check for new files',w.scan_sources,kind='primary');row.addWidget(self.check);row.addWidget(btn('Choose export folder…',w.choose_source,kind='secondary'));row.addStretch();row.addWidget(btn('Close',self.accept));l.addLayout(row)
        if w.pending_maintenance:l.addWidget(btn('Update saved data…',w.begin_migration,kind='link'))
        self.refresh_timer=QTimer(self);self.refresh_timer.setInterval(1000);self.refresh_timer.timeout.connect(self.refresh)
        self.finished.connect(lambda _:self.refresh_timer.stop());self.refresh_timer.start()
        self.refresh()
    def set_pause(self,value):self.w.config['paused']=value;self.w.persist_preferences()
    def refresh(self):
        w=self.w;self.path.setText(w.config.get('source_root') or 'Choose an export folder to get started.');self.checked.setText('Last checked: '+w.local_stamp(w.config.get('last_capture_check')) if w.config.get('last_capture_check') else 'This folder has not been checked yet.')
        w.clear_layout(self.coverage_l);result=w.config.get('last_capture_result',{});coverage=source_states(w.summary_data.get('coverage'),result);counts=w.summary_data.get('counts',{})
        for key,title in FAMILIES.items():
            line=QHBoxLayout();line.addWidget(text(title,'body'));line.addStretch();state=STATES.get(coverage.get(key),'not checked').capitalize();line.addWidget(text(state,'small'));self.coverage_l.addLayout(line)
        self.coverage_l.addWidget(text(str(sum(counts.values()))+' saved items · changes in your live accounts appear after you export them again.','small',True));self.coverage_l.addStretch()
        self.check.setEnabled(w.initialized and bool(w.config.get('source_root')) and not w.scan_busy and not w.storage_maintenance);self.check.setText('Checking…' if w.scan_busy else 'Check for new files')
        errors=result.get('errors') or [];pending=result.get('pending_removals')
        self.notice.setText(('Some files could not be read. Check the folder is available and try again. ' if errors else '')+('O3 will check again before removing copies of missing files. ' if pending else '')+('Finish updating saved data before checking for new files.' if w.storage_maintenance or w.pending_maintenance else ''))

class Perspectives(QDialog):
    def __init__(self,w):
        super().__init__(w);self.w=w;self.setWindowTitle('Saved views & places');self.resize(680,590)
        l=QVBoxLayout(self);l.setContentsMargins(28,24,28,20);l.setSpacing(14);l.addWidget(text('Saved views & places','title'));tabs=QComboBox();tabs.addItems(['Views','Pinned records','Recent records','Saved locations']);l.addWidget(tabs)
        self.items=QListWidget();self.items.setObjectName('savedPerspectives');self.items.setWordWrap(True);l.addWidget(self.items,1);self.items.itemActivated.connect(self.activate);self.mode=tabs;tabs.currentIndexChanged.connect(self.populate)
        self.notice=text('','small',True);l.addWidget(self.notice)
        self.retry=btn('Retry saving location',self.commit_location,kind='secondary');l.addWidget(self.retry);self.retry.setVisible(bool(getattr(w,'pending_location',None)))
        if self.retry.isVisibleTo(self):self.notice.setText('An unsaved location is retained for retry.')
        row=QHBoxLayout();row.addWidget(btn('Save current location…',self.save_location,kind='link'));row.addWidget(btn('Remove',self.remove_saved,kind='link'));row.addStretch();row.addWidget(btn('Open',self.activate,kind='primary'));row.addWidget(btn('Close',self.accept));l.addLayout(row);self.populate()
    def populate(self):
        self.items.clear();w=self.w;mode=self.mode.currentIndex()
        if mode==0:
            for v in w.summary_data.get('registry',{}).get('views',[]):
                if v.get('state')!='retired':self.add(v.get('name',v.get('label',v['view_id'])),{'view':v})
        elif mode in (1,2):
            for ref in (w.navigation.pins if mode==1 else w.navigation.recent):
                item=self.add('Loading saved item…',{'ref':ref})
                def loaded(r,item=item,mode=mode):
                    if r.get('ok') and self.mode.currentIndex()==mode:
                        try:
                            from .km_session import display_row
                            item.setText(display_row(r['result']['record'])['label'])
                        except RuntimeError:pass
                w.km.request('records.get',ref,loaded,owner=self)
        else:
            for p in w.config.get('alpha3',{}).get('perspectives',[]):self.add(p['name'],{'route':p['route']})
    def add(self,label,payload):
        item=QListWidgetItem(label);item.setData(Qt.UserRole,payload);self.items.addItem(item);return item
    def activate(self,*_):
        item=self.items.currentItem()
        if not item:return
        p=item.data(Qt.UserRole);self.accept()
        if 'view' in p:self.w.choose_view(p['view'])
        elif 'ref' in p:self.w.open_record(p['ref']['record_id'],p['ref']['record_type'])
        else:self.w.visit(p['route'])
    def remove_saved(self):
        item=self.items.currentItem()
        if not item:return
        payload=item.data(Qt.UserRole);mode=self.mode.currentIndex();prior_pins=deepcopy(self.w.navigation.pins);prior=deepcopy(self.w.config.get('alpha3',{}).get('perspectives',[]))
        if mode==1:self.w.navigation.toggle_pin(payload['ref'])
        elif mode==3:
            saved=self.w.config.get('alpha3',{}).get('perspectives',[])
            if 0<=self.items.currentRow()<len(saved):saved.pop(self.items.currentRow())
        else:
            self.w.show_information('Saved views & places','Manage views in Settings → Saved views. Clear recent items in Settings → Appearance.');return
        if not self.w.persist_preferences():
            self.w.navigation.pins=prior_pins;self.w.config.setdefault('alpha3',{})['perspectives']=prior
            self.notice.setText('Removal was not saved. The saved entry is unchanged; retry Remove.');return
        self.notice.setText('Removed from saved locations.' if mode==3 else 'Pin removed.');self.populate()
    def save_location(self):
        from PySide6.QtWidgets import QInputDialog
        name,ok=QInputDialog.getText(self,'Save location','Name',text=(getattr(self.w,'pending_location',None) or {}).get('name',''))
        if ok and name.strip():
            if len(name.strip())>120:self.notice.setText('Use a location name of 120 characters or fewer.');return
            if self.w.page=='collection' and not self.w.collection_ready:
                self.notice.setText('Wait for this collection to finish loading before saving its location.');return
            self.w.checkpoint_navigation()
            self.w.pending_location={'name':name.strip(),'route':self.w.navigation.current}
            self.commit_location()
    def commit_location(self):
        pending=getattr(self.w,'pending_location',None)
        if not pending:return
        saved=self.w.config.setdefault('alpha3',{}).setdefault('perspectives',[])
        if len(saved)>=32:self.notice.setText('Remove an older saved location before adding another.');self.retry.show();return
        prior=deepcopy(saved);saved.append(deepcopy(pending))
        if not self.w.persist_preferences():
            self.w.config['alpha3']['perspectives']=prior
            self.notice.setText('Location was not saved. “'+pending['name']+'” and its position are retained for retry.');self.retry.show();return
        self.w.pending_location=None;self.retry.hide();self.notice.setText('Location saved.');self.mode.setCurrentIndex(3);self.populate()

class FollowUp(ManagedDialog):
    """Explicit user classification saved by the canonical typed-property commands."""
    PROPERTY='ws:user:o3-review-disposition'
    def __init__(self,w,detail):
        super().__init__(w.km,'Follow-up',w);self.w=w;self.detail=detail;self.definition=None;self.resize(520,300)
        self.layout_box.addWidget(text('Follow-up','section'));self.layout_box.addWidget(text(detail['title'],'muted',True));self.layout_box.addWidget(text('Choose how you want to keep track of this message.','small',True));self.choice=QComboBox()
        for title,key in [('Follow up','follow_up'),('Reviewed','reviewed'),('No follow-up needed','no_follow_up')]:self.choice.addItem(title,key)
        self.layout_box.addWidget(self.choice);self.buttons(self.save);self.save_button.setEnabled(False)
        def loaded(r):
            self.definition=next((d for d in r['result']['workspace_properties'] if d['property_id']==self.PROPERTY),None)
            if self.definition and (self.definition.get('value_type')!='enum' or self.definition.get('mode')!='manual' or self.definition.get('allowed_values')!=['follow_up','reviewed','no_follow_up']):
                self.message.setText('Your saved follow-up choices use a different format. Nothing has been changed.');return
            self.save_button.setEnabled(True)
            if self.definition:
                self.request('property.value',{'record_type':'Email','record_id':detail['id'],'property_id':self.PROPERTY},self.value_loaded)
        self.request('registry.properties',{'record_type':'Email'},loaded)
    def value_loaded(self,r):self.choice.setCurrentIndex(max(0,self.choice.findData(r['result'].get('value'))))
    def save(self):
        def assign(_=None):self.request('property.set',{'record_type':'Email','record_id':self.detail['id'],'property_id':self.PROPERTY,'value':self.choice.currentData()},lambda r:self.accept(),mutation=True)
        if self.definition:assign()
        else:
            def created(r):self.definition={'property_id':self.PROPERTY};assign()
            self.request('property.create',{'record_type':'Email','definition_id':self.PROPERTY,'specification':{'label':'Follow-up','mode':'manual','value_type':'enum','allowed_values':['follow_up','reviewed','no_follow_up']}},created,mutation=True)

