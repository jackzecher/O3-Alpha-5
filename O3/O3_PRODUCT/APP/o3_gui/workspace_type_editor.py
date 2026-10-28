"""One native parent-level builder for Workspace record types.

The existing KM service persists identities, rules, values, views and audit.
This UI contains no semantic evaluator, duplicate definition authority or SQL.
"""
from __future__ import annotations
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox,QDialog,QFormLayout,QHBoxLayout,QLabel,
    QLineEdit,QListWidget,QListWidgetItem,QPlainTextEdit,QPushButton,QSpinBox,
    QTabWidget,QVBoxLayout,QWidget,QFrame,QButtonGroup)
from .workspace_editor import ManagedDialog, BuilderDialog, WorkspaceConfiguration, button, label, confirm

ROLE=Qt.ItemDataRole.UserRole
ICONS=('layers','file-text','check-square','flag','briefcase','users','clipboard-list','bookmark','folder','target')


class WorkspaceRecordEditor(ManagedDialog):
    """An instance editor; existing ManualValues edits all configured properties."""
    def __init__(self,client,type_info,record=None,parent=None):
        super().__init__(client,'Edit '+type_info['name'] if record else 'New '+type_info['name'],parent)
        self.type_info=type_info;self.record=record;self.saved_id=None
        self.layout_box.addWidget(label('Add something you want to keep track of in this collection.'))
        form=QFormLayout();self.title=QLineEdit(record.get('display_name','') if record else '')
        self.title.setMaxLength(240);self.title.setPlaceholderText('Give it a name')
        form.addRow('Title',self.title);self.layout_box.addLayout(form)
        self.buttons(self.save)
    def save(self):
        params={'record_type':self.type_info['type_id'],'title':self.title.text().strip()}
        if self.record:params['record_id']=self.record['record_id']
        def saved(response):
            self.saved_id=response['result'].get('record_id') or params.get('record_id')
            self.accept()
        self.request('workspace.record.edit' if self.record else 'workspace.record.create',params,saved,mutation=True)


class WorkspaceTypeEditor(BuilderDialog):
    """Settings / Properties & Rules / Views / Dashboard for one custom parent."""
    def __init__(self,client,definition=None,parent=None):
        super().__init__(client,'Change collection' if definition else 'New collection',parent)
        self.resize(850,690);self.definition=definition;self.type_id=definition['type_id'] if definition else None
        self.saved=False;self.available_views=[]
        introduction=label('A place for things you want to keep track of yourself.');introduction.setObjectName('dialog_description')
        self.layout_box.addWidget(introduction)
        self.tabs=QTabWidget();self.tabs.setObjectName('workspace_type_tabs')
        self.editor_body=self.tabs
        self.tabs.setDocumentMode(True);self.tabs.tabBar().hide()
        shell=QWidget();row=QHBoxLayout(shell)
        row.setContentsMargins(0,0,0,0);row.setSpacing(12)
        navigation=QFrame();navigation.setObjectName('workspace_section_nav');navigation.setFixedWidth(164)
        nav=QVBoxLayout(navigation);nav.setContentsMargins(7,9,7,9);nav.setSpacing(4)
        self.tab_nav_buttons=[];selection=QButtonGroup(self);selection.setExclusive(True)
        for i,name in enumerate(('Collection','Additional details','Saved views','Overview')):
            btn=QPushButton(name.replace('&','&&'));btn.setObjectName('workspace_section_button')
            btn.setCheckable(True);btn.setAccessibleName('Show '+name)
            btn.clicked.connect(lambda _checked=False,index=i:self.tabs.setCurrentIndex(index))
            selection.addButton(btn);nav.addWidget(btn);self.tab_nav_buttons.append(btn)
        nav.addStretch(1);row.addWidget(navigation);row.addWidget(self.tabs,1)
        self.layout_box.addWidget(shell,1)
        self.tabs.currentChanged.connect(lambda index:[b.setChecked(i==index) for i,b in enumerate(self.tab_nav_buttons)])
        self.tab_nav_buttons[0].setChecked(True)
        settings=QWidget();settings_layout=QVBoxLayout(settings)
        settings_layout.setContentsMargins(22,22,22,22);settings_layout.setSpacing(12)
        form=QFormLayout();form.setSpacing(12)
        settings_layout.addLayout(form);settings_layout.addStretch(1)
        self.name=QLineEdit(definition['name'] if definition else '');self.name.setMaxLength(120)
        self.plural=QLineEdit(definition['plural'] if definition else '');self.plural.setMaxLength(120)
        self.description=QPlainTextEdit(definition.get('description','') if definition else '');self.description.setMaximumHeight(90)
        self.icon=QComboBox()
        for icon in ICONS:self.icon.addItem(icon.replace('-',' ').title(),icon)
        self.icon.setCurrentIndex(max(0,self.icon.findData(definition.get('icon','layers') if definition else 'layers')))
        self.position=QSpinBox();self.position.setRange(0,10000);self.position.setValue(definition.get('nav_order',100) if definition else 100)
        for caption,widget in [('One item is called',self.name),('Collection name',self.plural),('Description',self.description),('Icon',self.icon)]:form.addRow(caption,widget)
        self.name.setPlaceholderText('For example: Idea');self.plural.setPlaceholderText('For example: Ideas')
        self.position.setParent(self);self.position.hide()
        self.tabs.addTab(settings,'Record type')
        config=QWidget();cl=QVBoxLayout(config)
        cl.addWidget(label('Add properties and rules for this record type.'))
        cl.addWidget(button('Open Properties / Rules / Presets…',self.configure_fields))
        cl.addStretch(1);self.tabs.addTab(config,'Properties & Rules')
        views=QWidget();vl=QVBoxLayout(views)
        vl.addWidget(label('Save useful ways of looking at the items in this collection.'))
        vl.addWidget(button('Manage saved views…',self.configure_views))
        vl.addStretch(1);self.tabs.addTab(views,'Views')
        dashboard=QWidget();dl=QVBoxLayout(dashboard)
        dl.addWidget(label('Choose saved views to show on this collection’s overview.'))
        self.panels=QListWidget();self.panels.setAccessibleName('Dashboard panels in display order');dl.addWidget(self.panels,1)
        row=QHBoxLayout();self.panel_view=QComboBox();self.panel_view.setAccessibleName('Saved view for dashboard panel');row.addWidget(self.panel_view,2)
        self.panel_mode=QComboBox();self.panel_mode.addItem('Number of items','count');self.panel_mode.addItem('List of items','table');row.addWidget(self.panel_mode,1)
        row.addWidget(button('Add panel',self.add_panel));dl.addLayout(row)
        row=QHBoxLayout()
        for text,fn in [('Remove',self.remove_panel),('Move up',lambda:self.move_panel(-1)),('Move down',lambda:self.move_panel(1)),('Refresh views',self.reload_views)]:row.addWidget(button(text,fn))
        dl.addLayout(row);self.tabs.addTab(dashboard,'Dashboard')
        self._set_panels(definition.get('dashboard',[]) if definition else [])
        # Existing detailed definitions remain saved. The everyday collection
        # editor does not expose the low-level property/rule/preset machinery.
        self.tabs.setTabVisible(1,False);self.tab_nav_buttons[1].hide()
        actions=QHBoxLayout();self.status=label('Save the collection to add items and saved views.');actions.addWidget(self.status,1)
        if definition:
            actions.addWidget(button('Hide collection…' if definition['active'] else 'Restore collection…',self.lifecycle))
        self.save_button=button('Save collection',self.save);self.save_button.setObjectName('primary');actions.addWidget(self.save_button)
        actions.addWidget(button('Close',self.reject));self.layout_box.addLayout(actions)
        self.arm_draft(self.draft)
        if self.type_id:self.reload_views()
    def _set_panels(self,items):
        self.panels.clear()
        for item in items:
            view_name=next((v.get('label',v.get('name','Saved view')) for v in self.available_views if v['view_id']==item['view_id']),'Saved view unavailable')
            entry=QListWidgetItem(view_name+'  ·  '+('Count' if item['mode']=='count' else 'Records'))
            entry.setData(ROLE,item);self.panels.addItem(entry)
    def chosen_panels(self):return [self.panels.item(i).data(ROLE) for i in range(self.panels.count())]
    def reload_views(self):
        if not self.type_id:return
        def loaded(response):
            self.available_views=[v for v in response['result']['views'] if v.get('state')=='enabled' and v.get('record_type')==self.type_id]
            self.panel_view.clear()
            for v in self.available_views:self.panel_view.addItem(v.get('label',v.get('name',v['view_id'])),v['view_id'])
            self._set_panels(self.chosen_panels())
        self.request('registry.views',{'record_type':self.type_id},loaded)
    def add_panel(self):
        vid=self.panel_view.currentData()
        if vid is None:return
        entries=self.chosen_panels()
        if any(x['view_id']==vid for x in entries):self.message.setText('That view is already on the dashboard.');return
        self._set_panels(entries+[{'view_id':vid,'mode':self.panel_mode.currentData()}])
    def remove_panel(self):
        i=self.panels.currentRow()
        if i>=0:items=self.chosen_panels();items.pop(i);self._set_panels(items)
    def move_panel(self,delta):
        i=self.panels.currentRow();items=self.chosen_panels();new=i+delta
        if not (0<=i<len(items) and 0<=new<len(items)):return
        items[i],items[new]=items[new],items[i];self._set_panels(items);self.panels.setCurrentRow(new)
    def configure_fields(self):
        if not self.type_id:self.message.setText('Save the record type first.');return
        WorkspaceConfiguration(self.client,self.type_id,self,display_name=self.name.text().strip()).exec();self.reload_views()
    def configure_views(self):
        if not self.type_id:self.message.setText('Save the collection first.');return
        from .human_customization import SavedViewsDialog
        dialog=SavedViewsDialog(self.client,self.type_id,self,display_name=self.name.text().strip())
        dialog.exec();self.reload_views()
    def draft(self):
        return {'name':self.name.text().strip(),'plural':self.plural.text().strip(),
                'description':self.description.toPlainText().strip(),'icon':self.icon.currentData(),
                'nav_order':self.position.value(),'dashboard':self.chosen_panels()}
    def save(self):
        params={'name':self.name.text().strip(),'plural':self.plural.text().strip(),
                'description':self.description.toPlainText().strip(),'icon':self.icon.currentData(),
                'nav_order':self.position.value()}
        if self.type_id:
            params.update(type_id=self.type_id,dashboard=self.chosen_panels())
        def done(response):
            self.type_id=response['result']['type_id'];self.saved=True
            self.status.setText('Collection saved. You can now add items and saved views.');self.arm_draft(self.draft)
            self.reload_views()
        self.request('workspace.type.edit' if self.type_id else 'workspace.type.create',params,done,mutation=True)
    def lifecycle(self):
        if not self.type_id:return
        action='retire' if self.definition.get('active',True) else 'restore'
        if action=='retire' and not confirm(self,'Hide collection?','This collection will leave the sidebar. Its contents are kept, and you can restore it later.'):
            return
        def finished(response):
            self.definition['active']=action=='restore';self.saved=True
            self.status.setText('Collection '+('restored' if action=='restore' else 'hidden')+'. Its contents are kept.')
        self.request('workspace.type.'+action,{'type_id':self.type_id},finished,mutation=True)


class WorkspaceTypeManager(ManagedDialog):
    def __init__(self,client,parent=None):
        super().__init__(client,'Your collections',parent)
        self.resize(740,570);self.items={};self.saved=False
        self.layout_box.addWidget(label('Create a list for your ideas, plans, or anything else you want to keep track of.'))
        self.search=QLineEdit();self.search.setPlaceholderText('Find a collection…');self.search.setClearButtonEnabled(True)
        self.layout_box.addWidget(self.search)
        self.types=QListWidget();self.types.setAccessibleName('Your collections');self.layout_box.addWidget(self.types,1)
        row=QHBoxLayout()
        for text,fn in [('New collection…',self.add),('Change collection…',self.edit),('Add item…',self.add_record),('Refresh',self.reload)]:row.addWidget(button(text,fn))
        self.layout_box.addLayout(row);self.layout_box.addWidget(button('Close',self.accept))
        self.search.textChanged.connect(self.filter);self.types.itemDoubleClicked.connect(lambda *_:self.edit());self.reload()
    def filter(self):
        query=self.search.text().strip().casefold()
        for i in range(self.types.count()):
            item=self.types.item(i);item.setHidden(query not in item.text().casefold())
    def reload(self):
        def received(response):
            self.revision=response['workspace_revision'];rows=response['result']['record_types'];self.items={r['type_id']:r for r in rows}
            self.types.clear()
            for r in rows:
                item=QListWidgetItem(r['plural']+'  ·  '+('Shown' if r['active'] else 'Hidden'))
                item.setData(ROLE,r['type_id']);self.types.addItem(item)
            self.filter()
        self.request('workspace.types',{'include_retired':True},received)
    def selected(self):
        item=self.types.currentItem();return self.items.get(item.data(ROLE)) if item else None
    def add(self):
        dialog=WorkspaceTypeEditor(self.client,parent=self)
        dialog.exec()
        if dialog.saved:self.saved=True;self.reload()
    def edit(self):
        r=self.selected()
        if not r:return
        dialog=WorkspaceTypeEditor(self.client,r,self);dialog.exec()
        if dialog.saved:self.saved=True;self.reload()
    def add_record(self):
        typ=self.selected()
        if not typ or not typ['active']:self.message.setText('Choose a collection that is shown first.');return
        editor=WorkspaceRecordEditor(self.client,typ,parent=self)
        if editor.exec()==QDialog.DialogCode.Accepted:self.saved=True;self.reload()
