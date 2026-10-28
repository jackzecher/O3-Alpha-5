"""Native, revision-checked commands. Forms never mutate Core or SQLite directly."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QFileDialog,QFormLayout,
    QHBoxLayout,QLineEdit,QListWidget,QListWidgetItem,QPlainTextEdit,QTableWidget,
    QTableWidgetItem,QHeaderView,QAbstractItemView,QWidget,QVBoxLayout)
from .workspace_editor import (ManagedDialog,RecordPicker,TypedValue,WorkspaceConfiguration,
                              button,label,choices,confirm,text_lines,ROLE)


class SelectionProperties(ManagedDialog):
    """Typed property editing for one captured selection through shared actions."""
    def __init__(self, client, context, *, controller=None, property_id=None, parent=None):
        from .interaction_actions import RecordActionController
        super().__init__(client, 'Edit properties', parent)
        self.context = context
        self.kind = context.record_type
        self.rid = context.records[0].record_id if context.count == 1 else None
        self.revision = context.revision if context.revision is not None else client.revision
        self.controller = controller or RecordActionController(client, self)
        self.definition = None; self.control = None; self.generation = 0
        self.selected_property = property_id
        self.resize(790, 420)
        self.layout_box.addWidget(label(context.scope_label))
        if context.scope_note: self.layout_box.addWidget(label(context.scope_note))
        self.layout_box.addWidget(label('Your properties are saved in O3. Source facts and calculated values keep their own authority.'))
        row = QHBoxLayout(); self.properties = QComboBox(); self.properties.setMinimumContentsLength(24)
        self.properties.setAccessibleName('Property to inspect or edit')
        row.addWidget(self.properties, 1)
        row.addWidget(button('Manage properties…', self.configure))
        self.layout_box.addLayout(row)
        self.authority = label(''); self.authority.setObjectName('muted')
        self.layout_box.addWidget(self.authority)
        self.value_host = QWidget(); self.value_layout = QVBoxLayout(self.value_host)
        self.value_layout.setContentsMargins(0, 8, 0, 8)
        self.layout_box.addWidget(self.value_host)
        self.result = label('Choose a property to see its value.')
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout_box.addWidget(self.result)
        self.layout_box.addStretch(1)
        self.batch_note = label('')
        self.layout_box.addWidget(self.batch_note)
        row = QHBoxLayout()
        self.clear_button = button('Clear value' if context.count == 1 else f'Clear from {context.count:,} items', self.clear)
        row.addWidget(self.clear_button); row.addStretch()
        row.addWidget(button('Close', self.reject))
        self.set_button = button('Save value' if context.count == 1 else f'Apply to {context.count:,} items', self.save)
        self.set_button.setObjectName('primary'); row.addWidget(self.set_button)
        self.layout_box.addLayout(row)
        self.properties.currentIndexChanged.connect(self.select_property)
        self.reload()

    def configure(self):
        WorkspaceConfiguration(self.client, self.kind, self).exec()
        self.reload()

    def reload(self):
        chosen = self.definition.get('property_id') if self.definition else self.selected_property
        def done(response):
            self.revision = response['workspace_revision']; self.properties.blockSignals(True); self.properties.clear()
            definitions = [d for d in response['result']['workspace_properties'] if d.get('state') != 'retired']
            for definition in sorted(definitions, key=lambda d: (d.get('mode') != 'manual', d.get('label','').casefold())):
                caption = definition.get('label', definition['property_id'])
                if definition.get('mode') != 'manual': caption += '  ·  calculated'
                elif definition.get('state') != 'enabled': caption += '  ·  disabled'
                self.properties.addItem(caption, definition)
            for index in range(self.properties.count()):
                if self.properties.itemData(index).get('property_id') == chosen:
                    self.properties.setCurrentIndex(index); break
            self.properties.blockSignals(False); self.select_property()
        self.request('registry.properties', {'record_type': self.kind}, done)

    def select_property(self, *_):
        from .interaction_actions import property_presentation
        self.generation += 1; generation = self.generation
        self.definition = self.properties.currentData()
        self.set_button.setEnabled(False); self.clear_button.setEnabled(False)
        while self.value_layout.count():
            widget = self.value_layout.takeAt(0).widget()
            if widget: widget.deleteLater()
        self.control = None; self.batch_note.setText('')
        if not self.definition:
            self.authority.setText('No properties yet')
            self.result.setText('Use Manage properties to add a tag, choice, date, linked project or another useful detail.')
            return
        definition = self.definition
        manual = definition.get('mode') == 'manual' and definition.get('state') == 'enabled'
        if definition.get('property_id') == 'ws:display_name' and self.context.count != 1: manual = False
        def render(result):
            presentation = property_presentation(definition, result)
            from .builder_support import TYPE_LABELS
            self.authority.setText(presentation['authority'] + ' · ' + TYPE_LABELS.get(presentation['value_type'], presentation['value_type'].replace('_',' ')))
            self.result.setText(presentation['text'] + ('\n' + str(presentation['reason']) if presentation['reason'] else ''))
            if manual:
                self.control = TypedValue(self.client, definition, result.get('value'))
                self.value_layout.addWidget(self.control)
                self.result.setText(str(presentation['reason'] or ''))
            else:
                self.result.setText(self.result.text() + '\n\nThis value is read-only here. Manage its rule to change how it is calculated.')
            self.result.setVisible(bool(self.result.text()))
            self.set_button.setEnabled(manual); self.clear_button.setEnabled(manual and definition.get('nullable') is not False and definition.get('property_id') != 'ws:display_name')
        if self.context.count > 1:
            render({'state': 'unset', 'value': None})
            self.result.setText('Each selected item may currently have a different value.'); self.result.show()
            self.batch_note.setText(f'Saving replaces this property on exactly {self.context.count:,} selected items. Other properties stay unchanged.')
            return
        def loaded(response):
            if generation != self.generation: return
            if not response.get('ok'):
                self.message.setText(response.get('error',{}).get('message','This value could not be loaded.')); return
            self.revision = response['workspace_revision']; render(response['result'])
        self.client.request('property.value', {'record_type': self.kind, 'record_id': self.rid,
                            'property_id': definition['property_id']}, loaded, owner=self)

    def _write(self, operation, value=None):
        from dataclasses import replace
        if self.busy or not self.definition: return
        context = replace(self.context, revision=self.revision)
        self.busy = True; self.set_button.setEnabled(False); self.clear_button.setEnabled(False)
        self.message.setText('Saving…')
        def saved(response):
            self.busy = False
            if not response.get('ok'):
                error = response.get('error', {})
                self.message.setText('The saved information changed. Your draft is still here. Review saved changes before trying again.'
                    if error.get('code') == 'REVISION_CONFLICT' else error.get('message') or 'The change could not be saved.')
                self.conflict_button.setVisible(error.get('code') == 'REVISION_CONFLICT')
                self.set_button.setEnabled(True); self.clear_button.setEnabled(self.definition.get('nullable') is not False and self.definition.get('property_id') != 'ws:display_name')
                return
            self.revision = response['workspace_revision']; self.conflict_button.hide()
            count = response.get('result', {}).get('affected_count', 0)
            self.message.setText(f'Saved · {count:,} item' + ('' if count == 1 else 's') if count else 'Already up to date.')
            self.select_property()
        accepted = self.controller.assign(context, self.definition['property_id'], value,
                                          operation=operation, done=saved)
        if not accepted and self.busy:
            self.busy = False; self.set_button.setEnabled(True); self.clear_button.setEnabled(self.definition.get('nullable') is not False and self.definition.get('property_id') != 'ws:display_name')
            self.message.setText('The change could not be started. Try again after the current operation finishes.')

    def save(self):
        if not self.control: return
        try: value = self.control.value()
        except ValueError as exc: self.message.setText(str(exc)); return
        self._write('clear' if value is None else 'set', value)

    def clear(self):
        if self.definition: self._write('clear')

    def explain(self):
        if self.definition: self.controller.explain_requested.emit(self.context, self.definition['property_id'])


class PropertyPopover(ManagedDialog):
    """A compact anchored typed editor; Save is the same audited batch action."""
    def __init__(self, client, context, property_id, controller, parent=None):
        super().__init__(client, 'Edit property', parent)
        # A tool panel remains open when its typed record picker or conflict
        # review takes focus; a Qt.Popup silently hides and strands the draft.
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setObjectName('propertyPopover')
        self.setFixedWidth(440); self.setMinimumHeight(120)
        self.context = context; self.controller = controller; self.property_id = property_id
        self.kind = context.record_type; self.rid = context.records[0].record_id if context.count == 1 else None
        self.revision = context.revision if context.revision is not None else client.revision
        self.definition = None; self.control = None
        self.layout_box.setContentsMargins(18, 16, 18, 16); self.layout_box.setSpacing(10)
        self.heading = label('Loading property…'); self.heading.setProperty('role', 'section')
        self.layout_box.addWidget(self.heading)
        self.scope = label(context.scope_label + ('\n' + context.scope_note if context.scope_note else '')); self.scope.setProperty('role', 'small')
        self.layout_box.addWidget(self.scope)
        self.value_host = QWidget(); self.value_layout = QVBoxLayout(self.value_host)
        self.value_layout.setContentsMargins(0, 4, 0, 4)
        self.layout_box.addWidget(self.value_host)
        self.authority = label(''); self.authority.setProperty('role', 'small')
        self.layout_box.addWidget(self.authority)
        row = QHBoxLayout(); self.clear_button = button('Remove value', self.clear)
        self.clear_button.setEnabled(False); row.addWidget(self.clear_button); row.addStretch()
        row.addWidget(button('Cancel', self.reject)); self.save_button = button('Save', self.save)
        self.save_button.setObjectName('primary'); self.save_button.setEnabled(False); row.addWidget(self.save_button)
        self.layout_box.addLayout(row)
        self.request('property.get', {'property_id': property_id}, self._definition_loaded)

    def _definition_loaded(self, response):
        self.definition = response['result'].get('property')
        if not self.definition:
            self.message.setText('This property is no longer available.'); return
        definition = self.definition; definition['state'] = definition.get('state', definition.get('status', 'enabled'))
        self.heading.setText(definition.get('label', 'Property'))
        self.revision = response['workspace_revision']
        if self.rid:
            self.request('property.value', {'record_type': self.kind, 'record_id': self.rid,
                         'property_id': self.property_id}, self._value_loaded)
        else:
            self._value_loaded({'workspace_revision':self.revision,'result':{'state':'unset','value':None}})

    def _value_loaded(self, response):
        from .interaction_actions import property_presentation
        self.revision = response['workspace_revision']; value = response['result']
        presentation = property_presentation(self.definition, value)
        if not presentation['editable']:
            self.value_layout.addWidget(label(presentation['text']))
            self.authority.setText(presentation['authority'] + '. Read-only here.'); self.adjustSize(); return
        self.control = TypedValue(self.client, self.definition, value.get('value'))
        self.value_layout.addWidget(self.control)
        self.authority.setText('Entered by you · saved only when you choose Save.' if self.context.count == 1 else
                               f'Replaces this value on exactly {self.context.count:,} selected items.')
        self.save_button.setEnabled(True)
        self.clear_button.setEnabled(self.definition.get('nullable') is not False and self.property_id != 'ws:display_name')
        self.adjustSize(); self.control.setFocus()

    def save(self):
        if not self.control: return
        try: value = self.control.value()
        except ValueError as exc: self.message.setText(str(exc)); self.adjustSize(); return
        self._write('clear' if value is None else 'set', value)

    def clear(self):
        if self.control: self._write('clear')

    def _write(self, operation, value=None):
        from dataclasses import replace
        if self.busy: return
        self.busy = True; self.save_button.setEnabled(False); self.clear_button.setEnabled(False)
        self.message.setText('Saving…')
        def saved(response):
            self.busy = False
            if response.get('ok'):
                self.revision = response['workspace_revision']; self.accept(); return
            error = response.get('error', {})
            self.message.setText('The saved value changed. Your draft is still here. Review saved changes before trying again.'
                if error.get('code') == 'REVISION_CONFLICT' else error.get('message') or 'This value could not be saved.')
            self.conflict_button.setVisible(error.get('code') == 'REVISION_CONFLICT')
            self.save_button.setEnabled(True)
            self.clear_button.setEnabled(self.definition.get('nullable') is not False and self.property_id != 'ws:display_name')
            self.adjustSize()
        accepted = self.controller.assign(replace(self.context, revision=self.revision), self.property_id,
                                          value, operation=operation, done=saved)
        if not accepted and self.busy:
            self.busy = False; self.save_button.setEnabled(True)
            self.message.setText('Another change is still saving. Your draft is here; try again shortly.'); self.adjustSize()

    def show_at(self, position):
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.screenAt(position) or QGuiApplication.primaryScreen()
        self.show(); self.adjustSize()
        if screen:
            area = screen.availableGeometry()
            x = max(area.left()+8, min(position.x(), area.right()-self.width()-8))
            y = max(area.top()+8, min(position.y(), area.bottom()-self.height()-8))
            self.move(x,y)
        else: self.move(position)
        self.activateWindow()


class PropertyExplanation(ManagedDialog):
    """Explain one current KM result without mixing it with captured history."""
    def __init__(self, client, context, property_id, parent=None):
        super().__init__(client, 'Why this value?', parent)
        self.kind = context.record_type; self.context = context; self.property_id = property_id
        self.definition = None; self.evaluation = None; self.schema = {}
        self.resize(680, 540)
        self.heading = label('Loading property…'); self.heading.setProperty('role','section')
        self.layout_box.addWidget(self.heading)
        self.value = label(''); self.value.setProperty('role','readerTitle'); self.layout_box.addWidget(self.value)
        self.reason = label(''); self.layout_box.addWidget(self.reason)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setAccessibleName('Rule and supporting reasons'); self.layout_box.addWidget(self.details,1)
        self.technical = QPlainTextEdit(); self.technical.setReadOnly(True); self.technical.hide()
        self.layout_box.addWidget(self.technical,1)
        row = QHBoxLayout(); row.addWidget(button('Technical evidence…', lambda:self.technical.setVisible(not self.technical.isVisible())))
        row.addStretch(); row.addWidget(button('Close',self.reject)); self.layout_box.addLayout(row)
        self.request('property.get',{'property_id':property_id},self._definition_loaded)

    def _definition_loaded(self,response):
        self.definition = response['result'].get('property')
        if not self.definition:
            self.message.setText('This property is no longer available.'); return
        self.heading.setText(self.definition.get('label','Property'))
        self.request('property.value', {**self.context.records[0].payload(),'property_id':self.property_id}, self._value_loaded)

    def _value_loaded(self,response):
        import json
        from .interaction_actions import property_presentation
        self.evaluation = response['result']; presentation = property_presentation(self.definition,self.evaluation)
        self.value.setText(presentation['text'])
        self.reason.setText(presentation['reason'] or presentation['authority'])
        lines = ['Current result from your available O3 information. This is not a claim about an uncaptured message or historical state.']
        if self.definition.get('description'): lines.append(self.definition['description'])
        reasons = []
        def collect(value):
            if isinstance(value,dict):
                for key in ('reason','coverage_scope'):
                    if isinstance(value.get(key),str) and value[key] not in reasons: reasons.append(value[key])
                for child in value.values():
                    if isinstance(child,(dict,list)):collect(child)
            elif isinstance(value,list):
                for child in value:collect(child)
        collect(self.evaluation.get('evidence',[]))
        if reasons: lines.extend(['Supporting information',*reasons])
        elif self.evaluation.get('reason'): lines.append(str(self.evaluation['reason']))
        self.details.setPlainText('\n\n'.join(lines))
        self.technical.setPlainText(json.dumps(self.evaluation,indent=2,ensure_ascii=False,default=str))
        if self.definition.get('mode')=='computed':
            self.request('builder.schema',{'record_type':self.kind},self._schema_loaded)

    def _schema_loaded(self,response):
        self.schema = response['result']
        self.request('registry.rules',{'record_type':self.kind},self._rules_loaded)

    def _rules_loaded(self,response):
        from .builder_support import expression_lines
        producer = next((item for item in response['result'].get('workspace_rules',[])
                         if item.get('output_property_id')==self.property_id and item.get('state')=='enabled'),None)
        if producer and producer.get('expression'):
            fields={item['field_id']:item for item in self.schema.get('fields',[])}
            self.details.appendPlainText('\nSaved rule\n'+ '\n'.join(expression_lines(producer['expression'],fields)))
        elif not producer:
            self.details.appendPlainText('\nNo enabled rule currently produces this value.')


class ManualValues(SelectionProperties):
    """Constructor compatibility for existing item and organization actions."""
    def __init__(self, client, kind, rid, parent=None):
        from .interaction_actions import SelectionContext
        context = SelectionContext.from_rows([{'record_type': kind, 'record_id': rid}], revision=client.revision)
        super().__init__(client, context, parent=parent)


class LOEEditor(ManagedDialog):
    def __init__(self,client,record=None,*,adopt=False,parent=None):
        super().__init__(client,'Keep this project' if adopt else 'Edit project' if record else 'Create a project',parent)
        self.record,self.adopt=record,adopt;self.created_id=None
        self.layout_box.addWidget(label('Give your project a recognizable name. O3 uses its name and other names to find related mail and documents you already have.'))
        p=record.get('properties',{}) if record else {};form=QFormLayout();self.layout_box.addLayout(form)
        self.name=QLineEdit(p.get('name',''));self.name.setMaxLength(120)
        self.kind=choices(['loe','project','program','initiative','workstream'],p.get('kind','project'))
        self.objective=QPlainTextEdit(p.get('objective') or '');self.objective.setMaximumHeight(160)
        self.code=QLineEdit(p.get('reference_code') or '');self.code.setMaxLength(32)
        self.aliases=QPlainTextEdit('\n'.join(p.get('aliases',[])));self.aliases.setMaximumHeight(100)
        for title,w in [('Name',self.name),('Type',self.kind),('Objective',self.objective),('Reference code',self.code),('Other names, one per line',self.aliases)]:form.addRow(title,w)
        self.buttons(self.save)
    def save(self):
        fields={'name':self.name.text().strip(),'kind':self.kind.currentData(),'objective':self.objective.toPlainText().strip() or None,
                'reference_code':self.code.text().strip() or None,'aliases':text_lines(self.aliases.toPlainText())}
        if self.adopt and not confirm(self,'Keep this project','Keep this project in O3 so you can edit its details? Its original source and history will be preserved.'):return
        op='loe.adopt' if self.adopt else 'loe.edit' if self.record else 'loe.create'
        params={'changes' if op=='loe.edit' else 'fields':fields}
        if self.record:params['record_id']=self.record['record_id']
        def saved(response):
            result=response['result'];self.created_id=result.get('loe_id') or result.get('record_id') or (result.get('affected_core_ids') or [None])[0];self.accept()
        self.request(op,params,saved,mutation=True)


class OrganizationEditor(ManagedDialog):
    def __init__(self,client,record=None,parent=None):
        super().__init__(client,'Company details',parent)
        self.record=record;self.wsid=(record.get('record_id') if record.get('effective_kind')=='workspace_overlay' else record.get('assigned_ws_org_id')) if record else None
        self.seed=record if record and not self.wsid else None;self.members=[];self.member_generation=0
        self.layout_box.addWidget(label('Choose which saved company entries belong together. Their original names and sources stay unchanged.'))
        self.name=QLineEdit(record.get('display_name',record.get('properties',{}).get('name','')) if record else '');self.name.setMaxLength(160)
        form=QFormLayout();form.addRow('Display name',self.name);self.layout_box.addLayout(form)
        self.members_list=QListWidget();self.layout_box.addWidget(self.members_list,1)
        row=QHBoxLayout()
        for title,handler in [('Choose / add…',self.add),('Remove',self.remove),('Move…',self.move),('Refresh',self.reload)]:row.addWidget(button(title,handler))
        self.layout_box.addLayout(row)
        row=QHBoxLayout()
        row.addWidget(button('Your details…',self.properties));row.addWidget(button('Archive / restore',self.lifecycle));row.addWidget(button('Undo last membership edit',self.undo));self.layout_box.addLayout(row)
        self.buttons(self.save)
        if self.wsid:self.reload()
        elif self.seed:self.members=[self.seed['record_id']];self.paint_members()

    def reload(self):
        if not self.wsid:self.paint_members();return
        def done(response):
            self.revision=response['workspace_revision'];self.record=response['result']['organization']
            self.name.setText(self.record.get('display_name',''));self.members=list(self.record.get('core_org_ids',[]));self.paint_members()
        self.request('organization.effective',{'record_id':self.wsid},done)

    def paint_members(self):
        self.member_generation+=1
        self.members_list.clear()
        for rid in self.members:
            item=QListWidgetItem('Loading company…');item.setData(ROLE,rid);self.members_list.addItem(item)
            def loaded(response,item=item):
                if response.get('ok'):
                    from .km_session import display_row
                    item.setText(display_row(response['result']['record'])['label'])
            # The list may be replaced before the response; generation is checked.
            generation=self.member_generation
            def safe(response,item=item,generation=generation,loaded=loaded):
                if generation==self.member_generation:loaded(response)
            self.client.request('records.get',{'record_type':'Organization','record_id':rid},safe,owner=self)

    def add(self):
        picker=RecordPicker(self.client,['Organization'],multiple=not bool(self.wsid),parent=self)
        if picker.exec()!=QDialog.DialogCode.Accepted:return
        ids=[r['record_id'] for r in picker.selected_records]
        if not ids:return
        if self.wsid:
            self.request('organization.add_member',{'ws_org_id':self.wsid,'core_org_id':ids[0]},lambda r:self.reload(),mutation=True)
        else:self.members=list(dict.fromkeys(self.members+ids));self.paint_members()
    def selected_member(self):
        item=self.members_list.currentItem();return item.data(ROLE) if item else None
    def remove(self):
        rid=self.selected_member()
        if not rid:return
        if self.wsid:
            if confirm(self,'Separate entry','Show this company entry separately again? Its original information will be preserved.'):
                self.request('organization.remove_member',{'ws_org_id':self.wsid,'core_org_id':rid},lambda r:self.reload(),mutation=True)
        else:self.members.remove(rid);self.paint_members()
    def move(self):
        rid=self.selected_member()
        if not self.wsid or not rid:return
        picker=RecordPicker(self.client,['WorkspaceOrganization'],multiple=False,parent=self)
        if picker.exec()!=QDialog.DialogCode.Accepted or not picker.selected_records:return
        empty=len(self.members)==1
        if not confirm(self,'Move entry','Group this company entry with the selected company?'+(' The previous empty group will be archived.' if empty else '')):return
        self.request('organization.move_member',{'ws_org_id':self.wsid,'core_org_id':rid,'target_ws_org_id':picker.selected_records[0]['record_id'],
            'confirmed':True,'archive_empty_source':empty},lambda r:self.reload(),mutation=True)
    def properties(self):
        if self.wsid:
            ManualValues(self.client,'WorkspaceOrganization',self.wsid,self).exec();self.reload()
        else:self.message.setText('Save this company group before adding your own details.')
    def lifecycle(self):
        if not self.wsid:return
        archived=bool(self.record.get('archived',not self.record.get('active',True)))
        if not confirm(self,'Restore' if archived else 'Archive','Restore the entries that have not since been grouped elsewhere?' if archived else 'Archive this company group and show its entries separately? History is retained. O3 will explain if another saved view needs it.'):return
        self.request('organization.restore' if archived else 'organization.archive',{'ws_org_id':self.wsid,'confirmed':True,'membership_disposition':'release'},lambda r:self.reload(),mutation=True)
    def undo(self):
        if self.wsid and confirm(self,'Undo membership edit','Undo the last entry you added or removed? Other changes will remain as they are.'):
            self.request('organization.undo',{'ws_org_id':self.wsid},lambda r:self.reload(),mutation=True)
    def save(self):
        params={'display_name':self.name.text().strip()}
        if self.wsid:params['ws_org_id']=self.wsid;op='organization.rename'
        elif self.seed:
            op='organization.absorb';params.update(base_core_org_id=self.seed['record_id'],core_org_ids=self.members,confirm_explicit_membership=True)
        else:op='organization.create';params['core_org_ids']=self.members
        if not self.wsid and not confirm(self,'Save company group','Group these selected entries as one company in O3? Original information will be preserved.'):return
        def saved(response):
            self.created_id=response['result'].get('ws_org_id',self.wsid);self.accept()
        self.request(op,params,saved,mutation=True)


class CalendarSources(ManagedDialog):
    def __init__(self,client,source_root,parent=None):
        super().__init__(client,'Calendar sources',parent);self.source_root=source_root;self.sources=[];self.settings={}
        self.table=QTableWidget(0,3);self.table.setHorizontalHeaderLabels(['CALENDAR','SOURCE','STATE']);self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.layout_box.addWidget(self.table,1)
        self.name=QLineEdit();form=QFormLayout();form.addRow('Display name',self.name);self.layout_box.addLayout(form)
        row=QHBoxLayout();row.addWidget(button('Save display name',self.rename));row.addWidget(button('Relink moved source…',self.relink));row.addWidget(button('Refresh',self.reload));row.addWidget(button('Close',self.accept));self.layout_box.addLayout(row)
        self.table.itemSelectionChanged.connect(self.select);self.reload()
    def reload(self):
        def configured(response):
            self.settings=response['result']['settings'];self.revision=response['workspace_revision']
            self.request('calendar.sources',{},loaded)
        def loaded(response):
            if response['workspace_revision']!=self.revision:self.message.setText('Configuration changed. Refresh before editing.');return
            raw=response['result']['sources'];self.sources=list(raw.values()) if isinstance(raw,dict) else raw
            self.table.setRowCount(len(self.sources))
            for i,row in enumerate(self.sources):
                for j,key in enumerate(('display_name','path','state')):self.table.setItem(i,j,QTableWidgetItem(str(row.get(key,''))))
            if self.sources:self.table.selectRow(0)
        self.request('configuration.get',{},configured)
    def selected(self):
        i=self.table.currentRow();return self.sources[i] if 0<=i<len(self.sources) else None
    def select(self):
        row=self.selected();self.name.setText(row.get('display_name','') if row else '')
    def rename(self):
        row=self.selected()
        if not row:return
        names=dict(self.settings.get('calendar_display_names',{}));names[row['scope']]=self.name.text().strip()
        self.request('configuration.save',{'calendar_display_names':names},lambda r:self.reload(),mutation=True)
    def relink(self):
        row=self.selected()
        if not row or not self.source_root:self.message.setText('Choose your export folder first.');return
        path,_=QFileDialog.getOpenFileName(self,'Select relocated calendar',str(Path(self.source_root)/'Calendars'),'Calendar (*.ics)')
        if not path:return
        p=Path(path);root=Path(self.source_root)/'Calendars'
        if p.parent.resolve()!=root.resolve():self.message.setText('Choose a calendar file directly inside your selected Calendars folder.');return
        if not confirm(self,'Relink calendar','Use this file for the existing calendar while keeping its saved events? The original file must no longer be in use.'):return
        self.request('calendar.relink',{'calendar_scope':row['scope'],'new_path':p.name,'source_root':str(self.source_root),'confirmed':True},lambda r:self.reload(),mutation=True)
