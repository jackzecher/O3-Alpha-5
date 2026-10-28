"""Reusable native Workspace builders driven by KM's advertised typed metadata.

This module builds forms and serializes user choices. Only the worker's KM
validator/evaluator interprets conditions, commits facts, or owns revisions.
"""
from __future__ import annotations
import json
from html import escape
from copy import deepcopy
import uuid
from zoneinfo import available_timezones
from PySide6.QtCore import Qt, QTimer, QDate, Signal
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QDateEdit, QToolButton, QMenu,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from o3_gui.km_session import display_row
from o3_gui.display_preferences import (ColumnSpec, MAX_VIEW_COLUMNS, view_layout, view_columns_for, resolved_view_columns, view_column_value)
from o3_gui.builder_support import (TYPE_LABELS, GROUP_LABELS, OPERATOR_LABELS, EditHistory,
    humanize, field_label, value_text, condition_text, expression_lines, expression_size, check_draft_bounds, result_text, signature)

ROLE = Qt.ItemDataRole.UserRole


def label(text, *, wrap=True):
    item = QLabel(str(text)); item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(wrap)
    return item


def button(text, fn):
    item = QPushButton(text); item.setAutoDefault(False); item.clicked.connect(fn); return item


def choices(values, selected=None):
    item = QComboBox()
    for value in values: item.addItem(str(value).replace('_', ' ').title(), value)
    if selected in values: item.setCurrentIndex(item.findData(selected))
    return item


def text_lines(value):
    return [v.strip() for v in value.splitlines() if v.strip()]


def saved_changes_text(data):
    """Readable conflict evidence; never render raw registry JSON or identifiers."""
    labels={'workspace_timezone':'Time zone','week_start':'Week starts','self_addresses':'Your email addresses',
            'group_addresses':'Shared email addresses','label':'Name','display_label':'Name','name':'Name',
            'description':'Description','visible':'Shown in navigation','state':'Availability','active':'Active',
            'plural':'Collection name','title':'Title','display_name':'Title','include_inactive':'Includes older items'}
    lines=[]
    def visit(value,depth=0):
        if depth>5:return
        if isinstance(value,dict):
            for key,item in value.items():
                if key in labels:
                    rendered=', '.join(str(x) for x in item) if isinstance(item,list) else ('Yes' if item is True else 'No' if item is False else str(item))
                    lines.append(labels[key]+': '+rendered)
                elif isinstance(item,(dict,list)):
                    visit(item,depth+1)
        elif isinstance(value,list):
            for item in value[:80]:
                if isinstance(item,dict):
                    if lines:lines.append('')
                    visit(item,depth+1)
    visit(data)
    return '\n'.join(lines) or 'The saved information has changed. Close this editor and reopen it to see the latest details, or continue with your draft and review it before saving.'


def confirm(parent, title, message):
    box = QMessageBox(parent); box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText); box.setText(message)
    box.setIcon(QMessageBox.Icon.Question)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


class StatusMessage(QLabel):
    """Keep status space invisible when empty, visible for every real error.

    Editors also set message text outside the request callback, so visibility
    follows the text consistently rather than relying on individual callers.
    """
    def setText(self, value):
        text=str(value or '')
        self.setObjectName('dialog_status')
        super().setText(text)
        self.style().unpolish(self);self.style().polish(self)
        self.setVisible(bool(text.strip()))
    def information(self,value):
        self.setText(value);self.setObjectName('muted')
        self.style().unpolish(self);self.style().polish(self)


class ManagedDialog(QDialog):
    """Revision-aware edits; never silently retry or overwrite stale forms."""
    def __init__(self, client, title, parent=None):
        super().__init__(parent)
        self.client = client
        self.revision = client.revision
        self.setWindowTitle(title); self.resize(760, 620)
        self.layout_box = QVBoxLayout(self); self.layout_box.setContentsMargins(24,20,24,20); self.layout_box.setSpacing(12)
        self.message = StatusMessage(); self.message.setTextFormat(Qt.PlainText); self.message.setWordWrap(True); self.message.setObjectName('dialog_status')
        self.message.setVisible(False)
        self.message.setAccessibleName('Editor status')
        self.layout_box.addWidget(self.message)
        self.conflict_button=button('Review saved changes…',self.review_conflict)
        self.conflict_button.hide();self.layout_box.addWidget(self.conflict_button)
        self.busy = False
        self.finished.connect(lambda _: client.cancel_owner(self))

    def request(self, operation, params, done, *, mutation=False, expected=None, as_of=None):
        if self.busy: return
        self.busy = True
        self.request_state_changed(True,mutation)
        self.message.information('Loading…' if not mutation else 'Saving…')
        self.message.setVisible(True)
        def received(response):
            self.busy = False
            self.request_state_changed(False,mutation)
            if not response.get('ok'):
                error = response['error']
                self.message.setText('Something changed elsewhere while you were editing. Your draft is still here. Review the saved changes before saving again.'
                    if error['code'] == 'REVISION_CONFLICT' else
                    'O3 could not '+('save these changes' if mutation else 'load this information')+'. '+error['message'])
                self.message.setVisible(True)
                self.conflict_button.setVisible(error['code']=='REVISION_CONFLICT')
                return
            self.message.setText('')
            self.message.setVisible(False)
            if mutation: self.revision = response['workspace_revision']
            done(response)
        try:
            self.client.request(operation, params, received, owner=self,
                expected=(self.revision if expected is None else expected) if mutation else None,
                **({'as_of':as_of} if as_of is not None else {}))
        except (RuntimeError, ValueError) as exc:
            self.busy = False; self.request_state_changed(False,mutation); self.message.setText(str(exc))

    def request_state_changed(self,busy,mutation):
        pass

    def review_conflict(self):
        """Read the current authority, preserve the form, and require a new Save.

        Accepting a reviewed revision is explicit. It neither retries the rejected
        write nor overwrites the draft; any subsequent intervening write rejects
        again through the ordinary compare-and-swap path.
        """
        operation='configuration.get';params={}
        detail=getattr(self,'detail',None) or getattr(self,'record',None)
        owner=getattr(self,'owner',None) or getattr(self,'kind',None)
        if isinstance(detail,dict) and (detail.get('id') or detail.get('record_id')):
            operation='records.get';params={'record_id':detail.get('id') or detail['record_id'],
                'record_type':detail.get('target_kind') or detail.get('record_type') or owner or 'LineOfEffort'}
        elif owner:
            operation='registry.properties' if hasattr(self,'property_id') or hasattr(self,'properties') else 'registry.views'
            params={'record_type':owner}
        elif getattr(self,'type_id',None):operation='workspace.type.get';params={'type_id':self.type_id}
        def loaded(response):
            if not response.get('ok'):self.message.setText(response['error']['message']);return
            dialog=QDialog(self);dialog.setWindowTitle('Review saved changes');dialog.resize(820,590)
            layout=QVBoxLayout(dialog);layout.setContentsMargins(24,20,24,20)
            layout.addWidget(label('Someone or something saved changes while this editor was open. Your draft is still here. Review what is saved now; continuing will not save your draft.'))
            current=QPlainTextEdit();current.setReadOnly(True)
            current.setPlainText(saved_changes_text(response['result']));layout.addWidget(current,1)
            row=QHBoxLayout();row.addWidget(button('Back to my draft',dialog.reject));row.addStretch()
            row.addWidget(button('Continue with my draft',dialog.accept));layout.addLayout(row)
            if dialog.exec()==QDialog.Accepted:
                self.revision=response['workspace_revision'];self.conflict_button.hide()
                self.message.setText('Your draft is ready to review. Preview it, then choose Save when you are ready.')
                if hasattr(self,'preview_response'):self.preview_response=None
        self.client.request(operation,params,loaded,owner=self)

    def buttons(self, save=None, preview=None):
        row = QHBoxLayout()
        if preview:
            self.preview_button=button('Preview',preview);row.addWidget(self.preview_button)
        row.addStretch(1)
        row.addWidget(button('Cancel' if save else 'Close', self.reject))
        if save:
            self.save_button=button('Save',save);self.save_button.setObjectName('primary');row.addWidget(self.save_button)
        self.layout_box.addLayout(row)

    def show_result(self, title, data):
        dialog = QDialog(self); dialog.setWindowTitle(title); dialog.resize(650, 440)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(); text.setReadOnly(True)
        text.setPlainText(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        layout.addWidget(text); layout.addWidget(button('Close', dialog.accept)); dialog.exec()


class SearchChoice(QComboBox):
    """Search native choices without accepting arbitrary identifiers."""
    def __init__(self,parent=None):
        super().__init__(parent);self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(24);self.setMaxVisibleItems(16)
        self.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.lineEdit().setPlaceholderText('Type to find a field…')
        self.currentIndexChanged.connect(lambda *_:self.lineEdit().setCursorPosition(0))
    def selected_value(self):
        if self.currentIndex()<0 or self.currentText()!=self.itemText(self.currentIndex()):
            raise ValueError('Choose a matching field from the suggestions.')
        return self.currentData()


class ValueList(QWidget):
    """Native choice checklist or a small editable list; typed values stay intact."""
    changed=Signal()
    def __init__(self,spec,value=None,parent=None):
        super().__init__(parent);self.spec=spec
        self.limit=spec.get('max_count',50)
        options=spec.get('allowed_values')
        if options is None and spec.get('item_type')=='boolean':options=[True,False]
        self.checklist=options is not None
        box=QVBoxLayout(self);box.setContentsMargins(0,0,0,0);box.setSpacing(6)
        self.items=QListWidget();self.items.setMaximumHeight(136)
        self.items.setAccessibleName('Allowed values' if self.checklist else 'Configured values')
        self.items.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        box.addWidget(self.items)
        self.notice=label('');self.notice.setObjectName('sidebar_hint');box.addWidget(self.notice)
        values=list(value or [])
        if self.checklist:
            options=list(options)
            for option in values:
                if option not in options:options.append(option)
            for option in options:
                item=QListWidgetItem(value_text(option));item.setData(ROLE,option)
                item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if option in values else Qt.CheckState.Unchecked)
                self.items.addItem(item)
            self.items.itemChanged.connect(lambda _:self.changed.emit())
            self.notice.setText('Select every value that should match. Stored choice identifiers do not change.')
        else:
            row=QHBoxLayout();self.entry=QLineEdit();self.entry.setPlaceholderText('Enter a value, then press Enter')
            self.entry.setMaxLength(240);row.addWidget(self.entry,1)
            row.addWidget(button('Add',self.add));row.addWidget(button('Remove',self.remove));box.addLayout(row)
            self.entry.returnPressed.connect(self.add)
            for option in values:self.items.addItem(str(option))
            self.entry.textChanged.connect(lambda _:self.changed.emit())
            self.notice.setText(f'Up to {self.limit} values. Entries are kept exactly as supplied.')
    def add(self):
        value=self.entry.text().strip()
        if not value:return
        current=[self.items.item(i).text() for i in range(self.items.count())]
        if value in current:self.notice.setText('That value is already listed.');return
        if len(current)>=self.limit:self.notice.setText(f'The limit is {self.limit} values.');return
        self.items.addItem(value);self.entry.clear();self.changed.emit()
    def remove(self):
        for item in self.items.selectedItems():self.items.takeItem(self.items.row(item))
        self.changed.emit()
    def value(self):
        if self.checklist:
            return [self.items.item(i).data(ROLE) for i in range(self.items.count())
                    if self.items.item(i).checkState()==Qt.CheckState.Checked]
        result=[self.items.item(i).text() for i in range(self.items.count())]
        # Include the unfinished final entry on Save/Preview, not only after Add.
        pending=self.entry.text().strip()
        if pending and pending not in result:result.append(pending)
        if len(result)>self.limit:raise ValueError(f'Use at most {self.limit} values.')
        return result
    def toPlainText(self):return '\n'.join(str(x) for x in self.value())


class OptionalDate(QWidget):
    changed=Signal()
    def __init__(self,value=None,parent=None):
        super().__init__(parent);row=QHBoxLayout(self);row.setContentsMargins(0,0,0,0)
        self.use=QCheckBox('Set date');self.date=QDateEdit();self.date.setCalendarPopup(True)
        self.date.setDisplayFormat('dd MMM yyyy');self.date.setDateRange(QDate(1,1,1),QDate(9999,12,31))
        self.date.setDate(QDate.currentDate());self.invalid=None
        if value is not None:
            parsed=QDate.fromString(str(value),'yyyy-MM-dd')
            if parsed.isValid() and parsed.toString('yyyy-MM-dd')==value:self.date.setDate(parsed)
            else:self.invalid=value;self.date.setToolTip('The stored date is invalid. Select a valid replacement.')
            self.use.setChecked(True)
        self.date.setEnabled(self.use.isChecked());row.addWidget(self.use);row.addWidget(self.date,1)
        self.use.toggled.connect(self.toggle);self.date.dateChanged.connect(self.edit)
    def toggle(self,checked):
        self.date.setEnabled(checked)
        if not checked:self.invalid=None
        self.changed.emit()
    def edit(self,*_):self.invalid=None;self.changed.emit()
    def value(self):
        if not self.use.isChecked():return None
        if self.invalid is not None:raise ValueError('Choose a valid calendar date.')
        return self.date.date().toString('yyyy-MM-dd')


class BuilderDialog(ManagedDialog):
    """Draft-aware editor shell; read previews never silently rebase an edit."""
    def __init__(self,client,title,parent=None):
        super().__init__(client,title,parent);self.setSizeGripEnabled(True)
        self.draft_reader=None;self.baseline=None;self.pending_save=False;self.editor_body=None
        self.preview_panel=None
        heading=label(title);heading.setObjectName('heading');self.layout_box.insertWidget(0,heading)
        self.layout_box.setSpacing(11)
    def showEvent(self,event):
        screen=self.screen()
        if screen:
            area=screen.availableGeometry();self.resize(min(self.width(),max(600,area.width()-40)),min(self.height(),max(480,area.height()-60)))
        super().showEvent(event)
    def arm_draft(self,reader):
        self.draft_reader=reader;self.baseline=self.draft_signature()
    def draft_signature(self):
        if self.draft_reader is None:return None
        try:return signature(self.draft_reader())
        except (ValueError,TypeError,KeyError):return None
    def is_dirty(self):
        return self.draft_reader is not None and (self.draft_signature() is None or self.draft_signature()!=self.baseline)
    def reject(self):
        if self.pending_save:
            self.message.setText('Saving is in progress. The result will be shown before this editor closes.');return
        if self.is_dirty() and not confirm(self,'Discard unsaved changes?','Your draft has not been saved. Discard these changes?'):return
        super().reject()
    def request_state_changed(self,busy,mutation):
        self.pending_save=busy and mutation
        if self.editor_body is not None:self.editor_body.setEnabled(not self.pending_save)
        for name in ('save_button','preview_button'):
            item=getattr(self,name,None)
            if item:item.setEnabled(not busy)
    def preview_request(self,operation,params,render,*,as_of=None):
        original=self.draft_signature()
        def received(response):
            if original!=self.draft_signature():
                self.message.setText('The draft changed while the preview was running. Preview again.');return
            if response.get('workspace_revision')!=self.revision:
                self.message.setText('Workspace configuration changed. Reopen this editor before saving; your draft has not been overwritten.');return
            render(response)
        self.request(operation,params,received,as_of=as_of)
    def watch_inputs(self):
        for item in self.findChildren(QLineEdit):item.textChanged.connect(self.changed)
        for item in self.findChildren(QPlainTextEdit):item.textChanged.connect(self.changed)
        for item in self.findChildren(QComboBox):item.currentIndexChanged.connect(self.changed)
        for item in self.findChildren(QCheckBox):item.toggled.connect(self.changed)
        for item in self.findChildren(QSpinBox):item.valueChanged.connect(self.changed)
    def changed(self,*_):
        if self.preview_panel:self.preview_panel.mark_stale()


class BuilderPreview(QWidget):
    """Readable KM results. All condition verdicts come from KM, never this UI."""
    explain_requested=Signal(str)
    def __init__(self,fields,parent=None):
        super().__init__(parent);self.fields=fields;self.valid=False;self.selected_id=None
        self.rows=[];self.snapshot=None
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.summary=label('Preview your draft to check it against captured records. Nothing is saved.');layout.addWidget(self.summary)
        self.dependencies=label('');self.dependencies.setObjectName('muted');layout.addWidget(self.dependencies)
        split=QSplitter(Qt.Orientation.Vertical)
        self.table=QTableWidget(0,3);self.table.setHorizontalHeaderLabels(['Record','Result','Explanation'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide();self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.table.setAccessibleName('Preview results from KM')
        self.tree=QTreeWidget();self.tree.setHeaderLabels(['Condition','Result','Explanation'])
        self.tree.setAccessibleName('KM condition-by-condition explanation')
        self.tree.header().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        split.addWidget(self.table);split.addWidget(self.tree);split.setSizes([180,160]);layout.addWidget(split,1)
        self.note=label('Select a result to see its condition-by-condition explanation.');self.note.setObjectName('sidebar_hint');layout.addWidget(self.note)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(180)
        self.timer.timeout.connect(self.request_explanation)
        self.table.currentCellChanged.connect(self.select)
        self.explain_button=button('Explain selected result',self.request_explanation);self.explain_button.setEnabled(False);layout.addWidget(self.explain_button,0,Qt.AlignmentFlag.AlignLeft)
    def mark_stale(self):
        if self.valid:
            self.valid=False;self.timer.stop();self.tree.clear()
            self.summary.setText('Draft changed — preview again. Previous results are no longer current.')
            self.table.setEnabled(False);self.explain_button.setEnabled(False)
    def render(self,response):
        result=response['result'];self.snapshot={key:response.get(key) for key in ('workspace_id','workspace_revision','core_epoch','as_of')}
        self.rows=result.get('preview',[]);self.table.blockSignals(True);self.table.setRowCount(len(self.rows))
        self.table.setEnabled(True);self.tree.clear();self.selected_id=None;self.explain_button.setEnabled(False)
        deps=result.get('dependencies',[])
        self.dependencies.setText('Uses: '+', '.join(field_label(x,self.fields) for x in deps) if deps else 'No Workspace prerequisites.')
        if 'preview' in result:
            more=' More records are available.' if result.get('has_more') else ''
            status=''
            if result.get('property_state')=='disabled' or result.get('rule_state')=='disabled':
                status=' Proposed expression only; the saved configuration remains disabled.'
            self.summary.setText(f"Preview only · {len(self.rows)} active records · ordered by record ID.{more}{status}")
            if not self.rows:self.note.setText('No active records of this type are available. The definition was validated; there is no sample to evaluate.')
            else:self.note.setText('Select a result for its KM explanation. Preview does not change source data or saved definitions.')
            for i,row in enumerate(self.rows):
                name=display_row(row['record'])['label'] if row.get('record') else row.get('label',row['record_id'])
                reason=self.readable_reason(row.get('reason') or ('Condition matched' if row.get('value') is True else 'Condition did not match' if row.get('value') is False else 'Required input is unavailable'))
                for j,text in enumerate((name,result_text(row),reason)):
                    cell=QTableWidgetItem(str(text));cell.setToolTip(row['record_id'] if j==0 else str(text));self.table.setItem(i,j,cell)
        else:
            prop=result.get('property',{});self.summary.setText('Definition validated · manual values are entered per record · nothing saved.')
            self.tree.addTopLevelItem(QTreeWidgetItem([prop.get('label','Property'),TYPE_LABELS.get(prop.get('value_type'),''),'Manual values stay separate from Core facts.']))
            self.note.setText('Save to make this property available on records of this type.')
        self.table.blockSignals(False);self.valid=True
    def select(self,row,*_):
        if self.valid and 0<=row<len(self.rows):
            self.selected_id=self.rows[row]['record_id'];self.explain_button.setEnabled(True);self.tree.clear();self.note.setText('Loading the selected result’s explanation…');self.timer.start()
    def request_explanation(self):
        if self.valid and self.selected_id:self.explain_requested.emit(self.selected_id)
    def readable_reason(self,text):
        for key,info in self.fields.items():
            if key.startswith('ws:'):text=text.replace(key,field_label(key,self.fields))
        return text
    def render_explanation(self,response):
        detail=response['result'].get('explanation')
        if not self.valid or not detail or detail['record_id']!=self.selected_id:return
        # Settings/records may change between sample and selected-record reads.
        current={key:response.get(key) for key in ('workspace_id','workspace_revision','core_epoch','as_of')}
        if current!=self.snapshot:
            self.mark_stale();self.note.setText('Workspace data changed. Refresh the preview before comparing explanations.');return
        self.tree.clear()
        def add(parent,node):
            title=GROUP_LABELS.get(node.get('group')) or condition_text(node.get('condition',{}),self.fields)
            item=QTreeWidgetItem([title,result_text(node),self.readable_reason(node.get('reason') or '')])
            (parent.addChild(item) if parent else self.tree.addTopLevelItem(item))
            for child in node.get('children',[]):add(item,child)
        add(None,detail['tree']);self.tree.expandAll();self.note.setText('Each result above was evaluated by KM at the same preview snapshot.')



class RecordPicker(ManagedDialog):
    """Typed, searchable, bounded selection of stable IDs; never name matching."""
    def __init__(self, client, targets, selected=None, *, multiple=True, parent=None):
        super().__init__(client, 'Choose records', parent)
        self.multiple = multiple
        self.selected = {x['record_id']: dict(x) for x in (selected or [])}
        self.selected_records = []
        self.cursor = None; self.generation = 0; self.rows = []
        top = QHBoxLayout()
        self.target = choices(targets); top.addWidget(self.target)
        self.search = QLineEdit(); self.search.setPlaceholderText('Search stored records…'); self.search.setMaxLength(160)
        top.addWidget(self.search, 1); self.layout_box.addLayout(top)
        self.table = QTableWidget(0, 3); self.table.setHorizontalHeaderLabels(['SELECT', 'RECORD', 'TYPE'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide(); self.layout_box.addWidget(self.table, 1)
        self.table.itemChanged.connect(self.toggle_item)
        footer = QHBoxLayout(); self.count = label(''); footer.addWidget(self.count, 1)
        self.more = button('Load more', lambda: self.load(False)); footer.addWidget(self.more)
        self.layout_box.addLayout(footer)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(250)
        self.timer.timeout.connect(lambda: self.load(True))
        self.target.currentIndexChanged.connect(lambda _: self.load(True))
        self.search.textChanged.connect(lambda _: self.timer.start())
        self.buttons(self.select)
        self.load(True)

    def load(self, reset):
        if reset: self.generation += 1; self.cursor = None; self.rows = []
        elif len(self.rows)>=1000:self.rows=[]  # Keep explicit selections, not unbounded native rows.
        generation = self.generation
        kind = self.target.currentData()
        params = {'page_size': 100}
        if self.search.text().strip(): params['search'] = self.search.text().strip()
        if self.cursor: params['cursor'] = self.cursor
        operation = 'organization.effective' if kind == 'WorkspaceOrganization' else 'records.query'
        if kind == 'WorkspaceOrganization': params['overlays_only'] = True
        else: params['record_type'] = kind
        self.more.setEnabled(False)
        def received(response):
            if generation != self.generation: return
            if not response['ok']:
                if response['error']['code'] == 'SNAPSHOT_EXPIRED':
                    self.cursor=None;self.message.setText('Records changed. Edit the search to refresh the selection.');return
                self.message.setText(response['error']['code'] + ': ' + response['error']['message']); return
            result = response['result']; self.cursor = result.get('next_cursor')
            self.rows.extend(result.get('rows', [])); self.render()
            self.more.setEnabled(bool(self.cursor));self.more.setText('Next results' if len(self.rows)>=1000 else 'Load more')
        try: self.client.request(operation, params, received, owner=self)
        except (RuntimeError, ValueError) as exc: self.message.setText(str(exc))

    def render(self):
        self.table.blockSignals(True); self.table.setRowCount(len(self.rows))
        for n, raw in enumerate(self.rows):
            item = display_row(raw)
            selection = {'record_type': item['target_kind'], 'record_id': item['id'], 'label': item['label']}
            box = QTableWidgetItem(''); box.setFlags(box.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            box.setCheckState(Qt.CheckState.Checked if item['id'] in self.selected else Qt.CheckState.Unchecked)
            box.setData(ROLE, selection); self.table.setItem(n, 0, box)
            title = QTableWidgetItem(item['label']); title.setToolTip(item['id']); self.table.setItem(n, 1, title)
            self.table.setItem(n, 2, QTableWidgetItem(item['target_kind']))
        self.table.blockSignals(False); self.count.setText(f'{len(self.selected)} selected · {len(self.rows)} displayed')

    def toggle_item(self, item):
        if item.column() != 0: return
        data = item.data(ROLE)
        if item.checkState() == Qt.CheckState.Checked:
            if not self.multiple: self.selected.clear()
            self.selected[data['record_id']] = data
        else: self.selected.pop(data['record_id'], None)
        self.render()

    def select(self):
        self.selected_records = [self.selected[k] for k in sorted(self.selected)]
        if not self.multiple and len(self.selected_records) > 1:
            self.message.setText('Choose one record.'); return
        self.accept()


class ReferenceValue(QWidget):
    changed=Signal()
    def __init__(self, client, targets, value=None, *, multiple=True, ids_only=False, parent=None):
        super().__init__(parent)
        self.client, self.targets, self.multiple, self.ids_only = client, targets, multiple, ids_only
        self.refs = []; self.reference_generation=0; self.reference_labels={}
        self.line = QLineEdit(); self.line.setReadOnly(True)
        row = QHBoxLayout(self); row.setContentsMargins(0,0,0,0); row.addWidget(self.line,1)
        row.addWidget(button('Choose…', self.choose)); row.addWidget(button('Clear', self.clear))
        self.destroyed.connect(lambda *_:client.cancel_owner(self))
        if value is not None: self.set_value(value)

    def set_value(self, value):
        if not isinstance(value, list): value = [value]
        self.refs = [dict(x) if isinstance(x, dict) else {'record_id': x, 'record_type': self.targets[0]} for x in value]
        self.refresh()

    def refresh(self):
        self.reference_generation+=1;generation=self.reference_generation
        self.client.cancel_owner(self)
        waiting=[]
        for reference in self.refs:
            key=(reference['record_type'],reference['record_id'])
            if reference.get('label'):self.reference_labels[key]=(reference['label'],'')
            elif key not in self.reference_labels and key not in waiting:waiting.append(key)
        self.paint_references();self.changed.emit()
        # Four concurrent label reads keep large reference sets below the
        # shared request queue bound. These labels never change reference IDs.
        def next_reference():
            if generation!=self.reference_generation or not waiting:return
            key=waiting.pop(0)
            def loaded(response):
                if generation!=self.reference_generation:return
                if response.get('ok'):
                    record=response['result']['record'];projected=display_row(record)
                    self.reference_labels[key]=(projected['label'],'Retained record' if not record.get('active',True) else '')
                else:self.reference_labels[key]=('Unavailable linked record',response.get('error',{}).get('message','The saved reference is retained.'))
                self.paint_references();next_reference()
            try:self.client.request('records.get',{'record_type':key[0],'record_id':key[1]},loaded,owner=self)
            except (ValueError,RuntimeError) as error:
                self.reference_labels[key]=('Linked record unavailable',str(error));self.paint_references();next_reference()
        for _ in range(min(4,len(waiting))):next_reference()

    def paint_references(self):
        labels=[];details=[]
        for reference in self.refs:
            key=(reference['record_type'],reference['record_id'])
            name,notice=self.reference_labels.get(key,('Loading linked record…',''))
            labels.append(name+(' · retained' if notice=='Retained record' else ''))
            details.append(name+'\n'+key[0]+' · '+key[1]+('\n'+notice if notice else ''))
        self.line.setText('; '.join(labels));self.line.setCursorPosition(0)
        self.line.setToolTip('\n\n'.join(details))

    def choose(self):
        picker = RecordPicker(self.client, self.targets, self.refs, multiple=self.multiple, parent=self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.refs = picker.selected_records; self.refresh()

    def clear(self): self.refs = []; self.refresh()

    def value(self):
        if self.ids_only: return [x['record_id'] for x in self.refs]
        values = [{'record_type': x['record_type'], 'record_id': x['record_id']} for x in self.refs]
        return values if self.multiple else (values[0] if values else None)


class TypedValue(QWidget):
    changed=Signal()
    """An operand/value editor selected from KM schema, not from a preset name."""
    def __init__(self, client, spec, value=None, parent=None):
        super().__init__(parent)
        self.spec = spec; self.kind = spec.get('value_type', 'text')
        layout = QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        kind = self.kind
        if kind == 'none': self.control = label('No comparison value is needed.')
        elif kind == 'integer':
            self.control = QSpinBox(); self.control.setRange(spec.get('minimum',0),spec.get('maximum',36500))
            self.control.setValue(value if type(value) is int else spec.get('minimum',0))
        elif kind == 'boolean':
            self.control = QComboBox(); self.control.addItem('Not set',None)
            self.control.addItem('Yes',True); self.control.addItem('No',False)
            self.control.setCurrentIndex(1 if value is True else 2 if value is False else 0)
        elif kind == 'enum':
            self.control = QComboBox(); self.control.addItem('Not set',None)
            captions=spec.get('value_labels') or {}
            for option in spec.get('allowed_values',[]): self.control.addItem(captions.get(option,value_text(option)),option)
            if value is not None and self.control.findData(value)<0:self.control.addItem('Unavailable choice · '+str(value),value)
            index = self.control.findData(value); self.control.setCurrentIndex(max(0,index))
        elif kind in ('record_ref','record_ref_set','record_id_set'):
            targets = spec.get('reference_targets') or [spec.get('ref_type','Person')]
            self.control = ReferenceValue(client,targets,value,multiple=kind!='record_ref',ids_only=kind=='record_id_set')
        elif kind == 'list':
            self.control=ValueList(spec,value)
        elif kind == 'date':
            self.control=OptionalDate(value)
        else:
            self.control = QLineEdit(); self.control.setText('' if value is None else str(value))
            self.control.setMaxLength(spec.get('max_chars',4000))
            if kind == 'date': self.control.setPlaceholderText('YYYY-MM-DD')
            elif kind == 'timestamp': self.control.setPlaceholderText('ISO timestamp with time zone')
            elif kind == 'email_pattern': self.control.setPlaceholderText('Exact address or localpart@*')
        self.control.setAccessibleName(kind.replace('_',' ')+' value'); layout.addWidget(self.control)
        for signal_name in ('changed','textChanged','currentIndexChanged','valueChanged'):
            change=getattr(self.control,signal_name,None)
            if change is not None:
                change.connect(lambda *_:self.changed.emit());break

    def value(self):
        if self.kind == 'none': return None
        if self.kind in ('boolean','enum'): return self.control.currentData()
        if self.kind == 'integer': return self.control.value()
        if self.kind in ('record_ref','record_ref_set','record_id_set'): return self.control.value()
        if self.kind in ('list','date'):return self.control.value()
        return self.control.text().strip() or None


class ExpressionTree(QTreeWidget):
    """Move expression nodes without allowing multiple roots or leaf parents."""
    def __init__(self, editor):
        super().__init__(); self.editor=editor
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    def dropEvent(self,event):
        node=self.currentItem(); target=self.itemAt(event.position().toPoint())
        if node is None or node.parent() is None or target is None:
            event.ignore();return
        where=self.dropIndicatorPosition()
        parent=target if where==QAbstractItemView.DropIndicatorPosition.OnItem else target.parent()
        if parent is None or not parent.data(0,ROLE).get('group'):
            event.ignore();return
        if parent.data(0,ROLE).get('group')=='not' and parent.childCount() and node.parent() is not parent:
            self.editor.hint.setText('Not accepts one condition or group.');event.ignore();return
        if not self.editor.checkpoint():event.ignore();return
        previous=self.editor.expression();self.editor.loading=True;self.editor.current=None
        try:super().dropEvent(event)
        finally:self.editor.loading=False
        try:
            if self.topLevelItemCount()!=1:raise ValueError('Keep a single outer group.')
            check_draft_bounds(self.editor.raw_expression(),self.editor.schema.get('rule_limits') or {})
        except ValueError as error:
            self.editor.set_expression(previous);self.editor.hint.setText(str(error));return
        self.editor.select_node(self.currentItem(),None);self.editor.checkpoint()


class ExpressionEditor(QWidget):
    """A bounded native expression document. Only KM validates its meaning."""
    expressionChanged=Signal()
    def __init__(self,client,schema,expression=None,*,filters=False,parent=None):
        super().__init__(parent)
        self.client,self.schema,self.filters=client,schema,filters
        self.fields={x['field_id']:x for x in schema['fields']}
        self.loading=True;self.current=None;self.operand=None;self.history=None
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0);outer.setSpacing(8)
        toolbar=QHBoxLayout()
        toolbar.addWidget(button('+ Condition',self.add_condition))
        if not filters:
            groups=QToolButton();groups.setText('+ Group');groups.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu=QMenu(groups)
            for key,name in GROUP_LABELS.items():
                action=menu.addAction(name);action.triggered.connect(lambda checked=False,k=key:self.add_group(k))
            groups.setMenu(menu);toolbar.addWidget(groups)
        arrange=QToolButton();arrange.setText('Arrange');arrange.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        arrangement=QMenu(arrange)
        for title,callback in [('Duplicate',self.duplicate),('Move up',lambda:self.move(-1)),('Move down',lambda:self.move(1)),('Nest in previous group',self.indent),('Move out of group',self.outdent)]:
            arrangement.addAction(title,callback)
        arrange.setMenu(arrangement);toolbar.addWidget(arrange)
        toolbar.addWidget(button('Remove',self.remove));toolbar.addStretch(1)
        outer.addLayout(toolbar)
        split=QSplitter();self.tree=ExpressionTree(self);self.tree.setHeaderLabel('Rule conditions')
        self.tree.setAccessibleName('Nested rule conditions');self.tree.setMinimumWidth(240)
        self.tree.setAlternatingRowColors(False);split.addWidget(self.tree)
        pane=QWidget();self.form=QFormLayout(pane);self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows);self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.group_mode=QComboBox()
        for key,name in GROUP_LABELS.items():self.group_mode.addItem(name,key)
        self.form.addRow('Group behavior',self.group_mode)
        self.field=SearchChoice();self.field.setAccessibleName('Search Core fields and Workspace properties')
        for authority in ('core','workspace'):
            entries=[(key,info) for key,info in self.fields.items() if info.get('authority','core')==authority]
            if authority=='workspace' and entries and self.field.count():self.field.insertSeparator(self.field.count())
            for key,info in entries:
                caption=field_label(key,self.fields)
                self.field.addItem(caption,key);self.field.setItemData(self.field.count()-1,'Your property' if authority=='workspace' else 'Saved source information',Qt.ItemDataRole.ToolTipRole)
        self.operator=QComboBox();self.operator.setMinimumContentsLength(22)
        self.operator.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.value_host=QWidget();self.value_layout=QVBoxLayout(self.value_host);self.value_layout.setContentsMargins(0,0,0,0)
        self.form.addRow('Field',self.field);self.form.addRow('Condition',self.operator);self.form.addRow('Compare with',self.value_host)
        self.apply_button=button('Apply to draft',self.apply_condition);self.form.addRow(self.apply_button)
        self.hint=label('Select a condition to edit it. Changes are included automatically on Save or Preview.');self.hint.setObjectName('muted');self.form.addRow(self.hint)
        form_scroll=QScrollArea();form_scroll.setWidgetResizable(True);form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_scroll.setWidget(pane);form_scroll.setMinimumWidth(280)
        split.addWidget(form_scroll);split.setStretchFactor(0,1);split.setStretchFactor(1,1);split.setSizes([430,400]);outer.addWidget(split,1)
        footer=QHBoxLayout();self.undo_button=button('Undo',self.undo);self.redo_button=button('Redo',self.redo)
        footer.addWidget(self.undo_button);footer.addWidget(self.redo_button);self.size_label=label('');self.size_label.setObjectName('sidebar_hint');footer.addWidget(self.size_label,1);outer.addLayout(footer)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(350);self.timer.timeout.connect(self.checkpoint)
        self.tree.currentItemChanged.connect(self.select_node)
        self.field.currentIndexChanged.connect(self.change_field);self.field.lineEdit().textEdited.connect(self.condition_edited)
        self.operator.currentIndexChanged.connect(self.change_operator);self.group_mode.currentIndexChanged.connect(self.change_group)
        self.set_expression(expression if expression is not None else {'all':[] if filters else [self.default_leaf()]})
        self.history=EditHistory(self.raw_expression());self.refresh_history()
        self.tree.setToolTip('Drag conditions into groups, or use Arrange. Undo and Redo apply to this draft only.')
        self.arrange_shortcuts=[]
        for keys,callback in [('Alt+Up',lambda:self.move(-1)),('Alt+Down',lambda:self.move(1)),('Alt+Right',self.indent),('Alt+Left',self.outdent)]:
            shortcut=QShortcut(QKeySequence(keys),self.tree);shortcut.setContext(Qt.ShortcutContext.WidgetShortcut);shortcut.activated.connect(callback);self.arrange_shortcuts.append(shortcut)
    def default_leaf(self):
        key=next(iter(self.fields),'record_id');return {'field':key,'operator':'is_present'}
    def set_expression(self,expression):
        self.timer.stop();self.loading=True;self.current=None;self.tree.clear()
        self.add_node(None,deepcopy(expression));self.tree.expandAll();self.loading=False
        self.tree.setCurrentItem(self.tree.topLevelItem(0));self.refresh_history()
    def add_node(self,parent,expression):
        node=QTreeWidgetItem();group=next((k for k in GROUP_LABELS if k in expression),None)
        node.setData(0,ROLE,{'group':group} if group else deepcopy(expression))
        node.setText(0,GROUP_LABELS[group] if group else self.leaf_title(expression))
        if not group:node.setFlags(node.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
        if parent is None:node.setFlags(node.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
        if not group:node.setToolTip(0,str(expression.get('field','')))
        (parent.addChild(node) if parent else self.tree.addTopLevelItem(node))
        if group:
            for child in ([expression[group]] if group=='not' else expression[group]):self.add_node(node,child)
        return node
    def leaf_title(self,node):return condition_text(node,self.fields)
    def node_expression(self,node):
        data=deepcopy(node.data(0,ROLE));group=data.get('group')
        if not group:return data
        children=[self.node_expression(node.child(i)) for i in range(node.childCount())]
        if group=='not':
            if len(children)!=1:raise ValueError('An inverted group needs exactly one condition or group.')
            return {'not':children[0]}
        return {group:children}
    def raw_expression(self):
        root=self.tree.topLevelItem(0);return self.node_expression(root) if root else {'all':[]}
    def expression(self):self.commit_condition();return self.raw_expression()
    def refresh_history(self):
        if not hasattr(self,'size_label'):return
        self.undo_button.setEnabled(bool(self.history and self.history.can_undo));self.redo_button.setEnabled(bool(self.history and self.history.can_redo))
        try:leaves,depth=expression_size(self.raw_expression())
        except ValueError:self.size_label.setText('Complete the group before previewing.');return
        limits=self.schema.get('rule_limits') or {}
        self.size_label.setText(f"{leaves} conditions · {depth} levels")
        self.size_label.setToolTip(f"Up to {limits.get('max_leaves',32)} conditions and {limits.get('max_depth',5)} levels")
    def checkpoint(self):
        self.timer.stop()
        if self.loading or self.history is None:return True
        try:document=self.expression()
        except ValueError as exc:self.hint.setText(str(exc));return False
        limits=self.schema.get('rule_limits') or {}
        if self.filters:limits={'max_leaves':self.schema['view_options']['max_filters'],'max_depth':2,'max_group_children':self.schema['view_options']['max_filters']}
        try:check_draft_bounds(document,limits)
        except ValueError as exc:
            self.set_expression(self.history.current());self.hint.setText(str(exc));self.expressionChanged.emit();return False
        if self.history.push(document):self.expressionChanged.emit()
        self.refresh_history();return True
    def condition_edited(self,*_):
        if self.loading:return
        self.expressionChanged.emit();self.timer.start()
    def select_node(self,node,previous):
        if self.loading:return
        if previous is not None and self.current is previous and not self.checkpoint():
            self.tree.blockSignals(True);self.tree.setCurrentItem(previous);self.tree.blockSignals(False);return
        self.current=node
        if node is None:return
        self.loading=True;data=node.data(0,ROLE);group=data.get('group')
        for widget in (self.field,self.operator,self.value_host,self.apply_button):
            widget.setVisible(not bool(group))
            caption=self.form.labelForField(widget)
            if caption:caption.setVisible(not bool(group))
        self.group_mode.setVisible(bool(group));self.form.labelForField(self.group_mode).setVisible(bool(group))
        self.group_mode.setEnabled(not self.filters)
        if group:
            self.group_mode.setCurrentIndex(self.group_mode.findData(group))
            self.hint.setText('Add conditions inside this group, or select a condition to edit its field and value.')
        else:
            field=data.get('field');operator=data.get('operator')
            if self.field.findData(field)<0:self.field.addItem('Unavailable field · '+str(field),field)
            self.field.setCurrentIndex(self.field.findData(field))
            self.change_field();self.operator.setCurrentIndex(self.operator.findData(data.get('operator')))
            if field not in self.fields or operator not in self.fields[field].get('operators',[]):
                if self.operator.findData(operator)<0:self.operator.addItem(OPERATOR_LABELS.get(operator,humanize(operator)),operator)
                self.operator.setCurrentIndex(self.operator.findData(operator))
                self._preserved_leaf=deepcopy(data)
                self.build_operand(data.get('value'))
                self.hint.setText('This saved condition is unavailable. It is retained exactly; choose another field to replace it.')
            else:
                self._preserved_leaf=None
                self.build_operand(data.get('value'));self.hint.setText('Changes stay in this draft until you save. Preview checks their effect.')
        self.loading=False
    def change_field(self,*_):
        if not self.loading:self._preserved_leaf=None
        info=self.fields.get(self.field.currentData(),{})
        operators=info.get('operators',[])
        if self.filters:operators=[x for x in operators if x in self.schema['view_options']['filter_operators']]
        self.operator.blockSignals(True);self.operator.clear()
        for op in operators:self.operator.addItem(OPERATOR_LABELS.get(op,humanize(op)),op)
        self.operator.blockSignals(False);self.build_operand(None);self.condition_edited()
    def change_operator(self,*_):self.build_operand(None);self.condition_edited()
    def build_operand(self,value):
        while self.value_layout.count():
            child=self.value_layout.takeAt(0).widget()
            if child:child.setParent(None);child.deleteLater()
        info=self.fields.get(self.field.currentData(),{})
        preserved=getattr(self,'_preserved_leaf',None)
        if preserved and preserved.get('field')==self.field.currentData() and preserved.get('operator')==self.operator.currentData():
            self.operand=None;self.value_layout.addWidget(label(value_text(value,limit=240)));return
        spec=info.get('operator_specs',{}).get(self.operator.currentData(),{'value_type':'none'})
        self.operand=TypedValue(self.client,spec,value);self.operand.changed.connect(self.condition_edited);self.value_layout.addWidget(self.operand)
    def commit_condition(self):
        if self.current is None or self.current.data(0,ROLE).get('group'):return
        field=self.field.selected_value();operator=self.operator.currentData()
        preserved=getattr(self,'_preserved_leaf',None)
        if preserved and field==preserved.get('field') and operator==preserved.get('operator'):
            self.current.setData(0,ROLE,deepcopy(preserved));return
        if not field or not operator:raise ValueError('Choose a field and a compatible condition.')
        node={'field':field,'operator':operator}
        if self.operand.kind!='none':node['value']=self.operand.value()
        self.current.setData(0,ROLE,node);self.current.setText(0,self.leaf_title(node));self.current.setToolTip(0,field)
    def apply_condition(self):
        if self.checkpoint():self.hint.setText('Condition updated. Nothing is saved until Save is selected.')
    def change_group(self,*_):
        if self.loading or not self.current:return
        group=self.group_mode.currentData();node=self.current;data=node.data(0,ROLE)
        if not data.get('group'):return
        if group=='not' and node.childCount()!=1:
            self.loading=True;self.group_mode.setCurrentIndex(self.group_mode.findData(data['group']));self.loading=False
            self.hint.setText('An inverted group must contain exactly one child. Nest multiple conditions in an ALL or ANY group first.');return
        node.setData(0,ROLE,{'group':group});node.setText(0,GROUP_LABELS[group]);self.checkpoint()
    def container_node(self):
        node=self.tree.currentItem() or self.tree.topLevelItem(0)
        if not node.data(0,ROLE).get('group'):node=node.parent() or node
        if not node.data(0,ROLE).get('group'):
            self.set_expression({'all':[self.expression()]});node=self.tree.topLevelItem(0)
        if node.data(0,ROLE).get('group')=='not' and node.childCount():
            self.hint.setText('NOT accepts one child. Add conditions to an ALL or ANY group inside it.');return None
        return node
    def add_condition(self):
        if not self.checkpoint():return
        parent=self.container_node()
        if parent is not None:
            self.loading=True;node=self.add_node(parent,self.default_leaf());parent.setExpanded(True);self.loading=False
            self.tree.setCurrentItem(node);self.checkpoint()
    def add_group(self,group):
        if not self.checkpoint():return
        parent=self.container_node()
        if parent is not None:
            expression={group:self.default_leaf() if group=='not' else [self.default_leaf()]}
            self.loading=True;node=self.add_node(parent,expression);parent.setExpanded(True);node.setExpanded(True);self.loading=False
            self.tree.setCurrentItem(node);self.checkpoint()
    def remove(self):
        if not self.checkpoint():return
        node=self.tree.currentItem()
        if not node:return
        parent=node.parent()
        if parent and parent.data(0,ROLE).get('group')=='not' and parent.childCount()==1:
            self.hint.setText('Replace the child, or remove its NOT group. NOT must keep exactly one child.');return
        if parent:
            self.loading=True;self.current=None;parent.takeChild(parent.indexOfChild(node));self.loading=False
            self.tree.setCurrentItem(parent)
        else:self.set_expression({'all':[]})
        self.checkpoint()
    def duplicate(self):
        if not self.checkpoint():return
        node=self.tree.currentItem()
        if not node:return
        parent=node.parent()
        if parent is None or parent.data(0,ROLE).get('group')=='not':
            self.hint.setText('Select a condition or group inside an ALL or ANY group to duplicate it.');return
        self.loading=True;copy=self.add_node(parent,self.node_expression(node));copy.setExpanded(True);self.loading=False
        self.tree.setCurrentItem(copy);self.checkpoint()
    def move(self,direction):
        if not self.checkpoint():return
        node=self.tree.currentItem();parent=node.parent() if node else None
        if parent is None:return
        old=parent.indexOfChild(node);new=old+direction
        if not 0<=new<parent.childCount():return
        self.loading=True;self.current=None;parent.takeChild(old);parent.insertChild(new,node);self.loading=False
        self.tree.setCurrentItem(node);self.checkpoint()
    def indent(self):
        if not self.checkpoint():return
        node=self.tree.currentItem();parent=node.parent() if node else None
        if parent is None:return
        index=parent.indexOfChild(node)
        target=parent.child(index-1) if index>0 else None
        if target is None or target.data(0,ROLE).get('group') not in ('all','any'):
            self.hint.setText('Place an All or Any group immediately above this condition first.');return
        self.loading=True;self.current=None;parent.takeChild(index);target.addChild(node);target.setExpanded(True);self.loading=False
        self.tree.setCurrentItem(node);self.checkpoint()
    def outdent(self):
        if not self.checkpoint():return
        node=self.tree.currentItem();parent=node.parent() if node else None;grand=parent.parent() if parent else None
        if grand is None or grand.data(0,ROLE).get('group')=='not':return
        if parent.data(0,ROLE).get('group')=='not':
            self.hint.setText('Move the Not group itself to keep its meaning.');return
        self.loading=True;self.current=None;parent.takeChild(parent.indexOfChild(node));grand.insertChild(grand.indexOfChild(parent)+1,node);self.loading=False
        self.tree.setCurrentItem(node);self.checkpoint()
    def undo(self):
        if not self.checkpoint():
            self.set_expression(self.history.current());self.refresh_history();self.expressionChanged.emit();return
        self.set_expression(self.history.undo());self.refresh_history();self.expressionChanged.emit()
    def redo(self):
        if not self.checkpoint():return
        self.set_expression(self.history.redo());self.refresh_history();self.expressionChanged.emit()


class PropertyEditor(BuilderDialog):
    """One draft and one KM transaction for a property plus its optional rule."""
    def __init__(self,client,owner,schema,definition=None,parent=None,*,rules=None):
        super().__init__(client,'Edit Workspace property' if definition else 'Add Workspace property',parent)
        self.resize(1020,740);self.owner,self.schema,self.definition=owner,schema,definition
        self.property_id=(definition or {}).get('property_id') or 'ws:user:'+uuid.uuid4().hex
        self.draft_rule_id='ws:user:'+uuid.uuid4().hex;self.saved_id=None
        self.rule_ready=True;self.switching_rule=False;self.rule_baseline=None
        d=definition or {}
        context=label(humanize(owner)+' · changes apply to this Workspace only');context.setObjectName('muted');self.layout_box.insertWidget(1,context)
        self.editor_body=QTabWidget();self.layout_box.addWidget(self.editor_body,1)
        page=QWidget();layout=QVBoxLayout(page)
        layout.addWidget(label('Define the field, then choose whether values are entered manually or calculated by a rule.'))
        form_host=QWidget();self.form=QFormLayout(form_host)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name=QLineEdit(d.get('label',d.get('display_label','')));self.name.setPlaceholderText('For example: Needs review')
        self.name.setMaxLength(schema['property_constraints']['property_label_limit']);self.name.setAccessibleName('Property name')
        self.description=QPlainTextEdit(d.get('description',''));self.description.setObjectName('reading_text');self.description.setPlaceholderText('Optional description of what this property means')
        self.description.setMaximumHeight(78)
        self.kind=QComboBox()
        for item in schema['property_types']:self.kind.addItem(TYPE_LABELS.get(item['value_type'],humanize(item['value_type'])),item['value_type'])
        self.kind.setCurrentIndex(self.kind.findData(d.get('value_type','text')))
        self.mode=QComboBox();self.mode.addItem('Enter values manually','manual')
        if schema.get('condition_groups'):self.mode.addItem('Calculate with a rule','computed')
        self.mode.setCurrentIndex(self.mode.findData(d.get('mode','manual')))
        self.max_chars=QSpinBox();self.max_chars.setRange(1,schema['property_constraints']['text_limit']);self.max_chars.setValue(d.get('max_chars',160))
        self.enums=ValueList({'item_type':'text','max_count':schema['property_constraints']['enum_option_limit']},d.get('allowed_values',[]))
        self.enums.notice.setText('Use stable option names such as low, medium, high. Lowercase letters, digits and underscores are supported.')
        self.ref_type=choices(schema['reference_targets'],d.get('ref_type','Person'))
        self.max_count=QSpinBox();self.max_count.setRange(1,schema['property_constraints']['reference_set_limit']);self.max_count.setValue(d.get('max_count',100))
        for caption,widget in [('Name',self.name),('Description',self.description),('How values are set',self.mode),('Value type',self.kind),
                               ('Maximum characters',self.max_chars),('Choices',self.enums),('Linked record type',self.ref_type),('Maximum linked records',self.max_count)]:self.form.addRow(caption,widget)
        self.enabled=QCheckBox('Make this property available');self.enabled.setChecked(d.get('state','enabled')=='enabled');self.form.addRow('',self.enabled)
        scroller=QScrollArea();scroller.setWidgetResizable(True);scroller.setFrameShape(QFrame.Shape.NoFrame);scroller.setWidget(form_host);layout.addWidget(scroller,1)
        self.type_note=label('');self.type_note.setObjectName('muted');layout.addWidget(self.type_note)
        self.editor_body.addTab(page,'Property')
        self.rule_page=QWidget();rl=QVBoxLayout(self.rule_page)
        rule_form=QFormLayout();rl.addLayout(rule_form)
        self.rule_choice=QComboBox();self.rule_description=QLineEdit();self.rule_description.setMaxLength(2000)
        self.rule_description.setPlaceholderText('Optional explanation of this classification')
        rule_form.addRow('Rule',self.rule_choice);rule_form.addRow('Description',self.rule_description)
        self.expression=ExpressionEditor(client,schema);rl.addWidget(self.expression,1)
        self.rule_enabled=QCheckBox('Enable this rule');self.rule_enabled.setChecked(True);rl.addWidget(self.rule_enabled)
        rl.addWidget(label('Rules calculate True or False. Missing or unavailable evidence stays explicitly unknown; source facts are never rewritten.'))
        self.editor_body.addTab(self.rule_page,'Rule')
        self.preview_panel=BuilderPreview(self.expression.fields);self.editor_body.addTab(self.preview_panel,'3  Preview')
        self.preview_panel.explain_requested.connect(self.explain)
        self.known_rules=[];self.load_rules(rules or [])
        self.rule_choice.currentIndexChanged.connect(self.choose_rule)
        if definition:
            for widget in (self.kind,self.mode,self.ref_type):widget.setEnabled(False)
        self.kind.currentIndexChanged.connect(self.refresh_fields);self.mode.currentIndexChanged.connect(self.refresh_fields)
        self.refresh_fields();self.buttons(self.save,self.preview);self.refresh_fields()
        self.name.setFocus();self.expression.expressionChanged.connect(self.changed);self.enums.changed.connect(self.changed)
        self.watch_inputs();self.arm_draft(self.parameters)
        self.save_shortcut=QShortcut(QKeySequence('Ctrl+S'),self);self.save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut);self.save_shortcut.activated.connect(self.save)
        if definition and d.get('mode')=='computed' and rules is None:
            self.rule_ready=False;self.save_button.setEnabled(False);self.editor_body.setEnabled(False)
            def loaded(response):
                if response['workspace_revision']!=self.revision:
                    self.message.setText('Configuration changed while loading. Reopen this property before editing.');return
                self.load_rules(response['result']['workspace_rules']);self.rule_ready=True
                self.arm_draft(self.parameters);self.editor_body.setEnabled(True);self.save_button.setEnabled(True)
            self.request('registry.rules',{'record_type':owner},loaded)
    def load_rules(self,rules):
        self.known_rules=[r for r in rules if r.get('output_property_id')==self.property_id and r.get('state')!='retired']
        self.rule_choice.blockSignals(True);self.rule_choice.clear()
        active=[r for r in self.known_rules if r.get('state')=='enabled']
        if len(self.known_rules)>1 and not active:self.rule_choice.addItem('Choose the rule to edit…',None)
        for index,rule in enumerate(self.known_rules):
            title=rule.get('description') or f'Rule {index+1}'
            self.rule_choice.addItem(title[:90]+' · '+humanize(rule.get('state','enabled')),rule['rule_id'])
            self.rule_choice.setItemData(self.rule_choice.count()-1,rule['rule_id'],Qt.ItemDataRole.ToolTipRole)
        self.rule_choice.addItem('New rule for this property','__new__')
        choice=(active[0]['rule_id'] if len(active)==1 else self.known_rules[0]['rule_id'] if len(self.known_rules)==1 else '__new__' if not self.known_rules else None)
        self.rule_choice.setCurrentIndex(self.rule_choice.findData(choice));self.rule_choice.blockSignals(False)
        self._load_rule(choice)
    def _load_rule(self,rid):
        rule=next((r for r in self.known_rules if r['rule_id']==rid),{})
        self.rule_description.setText(rule.get('description',''));self.rule_enabled.setChecked(rule.get('state','enabled')=='enabled')
        self.expression.set_expression(rule.get('expression') or {'all':[]})
        self.expression.history=EditHistory(self.expression.raw_expression());self.expression.refresh_history()
        self.rule_baseline=signature(self.rule_draft());self.last_rule_content=signature(self.rule_draft(include_choice=False));self.last_rule_choice=rid
    def choose_rule(self,*_):
        if self.switching_rule:return
        new=self.rule_choice.currentData()
        try:changed=signature(self.rule_draft(include_choice=False))!=self.last_rule_content
        except (ValueError,AttributeError):changed=False
        if changed and not confirm(self,'Switch rules?','Discard the unsaved condition edits and open the other rule?'):
            self.switching_rule=True;self.rule_choice.setCurrentIndex(self.rule_choice.findData(self.last_rule_choice));self.switching_rule=False;return
        self._load_rule(new);self.changed()
    def rule_draft(self,include_choice=True):
        data={'description':self.rule_description.text(),'expression':self.expression.expression(),'enabled':self.rule_enabled.isChecked()}
        if include_choice:data['rule_id']=self.draft_rule_id if self.rule_choice.currentData()=='__new__' else self.rule_choice.currentData()
        return data
    def refresh_fields(self,*_):
        computed=self.mode.currentData()=='computed';kind=self.kind.currentData()
        if computed and kind!='boolean':self.kind.setCurrentIndex(self.kind.findData('boolean'));kind='boolean'
        if not self.definition:self.kind.setEnabled(not computed)
        for widget,visible in ((self.max_chars,kind=='text'),(self.enums,kind=='enum'),(self.ref_type,kind in ('record_ref','record_ref_set')),(self.max_count,kind=='record_ref_set')):
            self.form.setRowVisible(widget,visible)
        self.editor_body.setTabEnabled(1,computed)
        if hasattr(self,'save_button'):self.save_button.setText('Save property and rule' if computed else 'Save property')
        if not computed and self.editor_body.currentIndex()==1:self.editor_body.setCurrentIndex(0)
        self.type_note.setText('Calculated properties currently return True / False. Build the condition on the Rule tab.' if computed else
            'Values are entered per record. They do not change the source record or Core properties.')
        if self.definition:self.type_note.setText(self.type_note.text()+' Existing type and ownership stay fixed to protect saved data.')
    def spec(self):
        kind=self.kind.currentData()
        result={'label':self.name.text().strip(),'description':self.description.toPlainText(),'value_type':kind,'mode':self.mode.currentData()}
        if kind=='text':result['max_chars']=self.max_chars.value()
        if kind=='enum':result['allowed_values']=self.enums.value()
        if kind in ('record_ref','record_ref_set'):result['ref_type']=self.ref_type.currentData()
        if kind=='record_ref_set':result['max_count']=self.max_count.value()
        return result
    def parameters(self):
        if not self.rule_ready:raise ValueError('The property’s rules are still loading.')
        result={'record_type':self.owner,'property_id':self.property_id,'specification':self.spec(),'enabled':self.enabled.isChecked()}
        if self.mode.currentData()=='computed':
            rule=self.rule_draft()
            if not rule['rule_id']:raise ValueError('Choose which existing rule to edit, or create a new rule.')
            result['rule']=rule
        return result
    def validate(self):self.preview()
    def preview(self):
        try:params=self.parameters()
        except ValueError as exc:self.message.setText(str(exc));return
        def render(response):
            self.preview_panel.render(response);self.editor_body.setCurrentIndex(2)
            self.message.setText('Preview checked by KM. No changes saved.')
        self.preview_request('property.preview',{**params,'page_size':25},render)
    def explain(self,rid):
        if self.busy:self.preview_panel.note.setText('The current request is still running. Use Explain selected result when it finishes.');return
        try:params={**self.parameters(),'page_size':1,'explain_record_id':rid}
        except ValueError as exc:self.message.setText(str(exc));return
        self.preview_request('property.preview',params,self.preview_panel.render_explanation,as_of=self.preview_panel.snapshot['as_of'])
    def save(self):
        if self.busy:return
        try:params=self.parameters()
        except ValueError as exc:self.message.setText(str(exc));return
        def saved(response):self.saved_id=response['result']['property_id'];self.accept()
        self.request('property.configure',params,saved,mutation=True)


class RuleEditor(BuilderDialog):
    def __init__(self,client,owner,schema,properties,definition=None,parent=None):
        super().__init__(client,'Edit Workspace rule' if definition else 'Add Workspace rule',parent)
        self.resize(1020,740);self.owner,self.definition,self.schema=owner,definition,schema
        self.child_editor=None;d=definition or {}
        self.editor_body=QTabWidget();self.layout_box.addWidget(self.editor_body,1)
        page=QWidget();box=QVBoxLayout(page);form=QFormLayout();box.addLayout(form)
        self.output=QComboBox()
        for prop in properties:
            if prop.get('owner_type')==owner and prop.get('mode')=='computed' and prop.get('state',prop.get('status'))!='retired':
                self.output.addItem(prop.get('label',prop['property_id']),prop['property_id'])
        if d.get('output_property_id'):self.output.setCurrentIndex(self.output.findData(d['output_property_id']))
        self.output.setEnabled(not bool(definition));self.description=QLineEdit(d.get('description',''));self.description.setMaxLength(2000)
        form.addRow('Property to calculate',self.output);form.addRow('Description',self.description)
        if not definition:
            self.add_output=button('Add a property with its rule…',self.new_property);self.add_output.setObjectName('linkbutton');box.addWidget(self.add_output)
        self.expression=ExpressionEditor(client,schema,d.get('expression',{'all':[]}));box.addWidget(self.expression,1)
        self.editor_body.addTab(page,'Rule')
        self.preview_panel=BuilderPreview(self.expression.fields);self.preview_table=self.preview_panel.table
        self.editor_body.addTab(self.preview_panel,'Preview');self.preview_panel.explain_requested.connect(self.explain)
        self.layout_box.addWidget(label('This rule calculates one True / False Workspace property. It cannot change source facts or run scripts.'))
        self.buttons(self.save,self.preview);self.save_button.setText('Save rule')
        self.expression.expressionChanged.connect(self.changed);self.watch_inputs();self.arm_draft(self.raw_draft)
        self.save_shortcut=QShortcut(QKeySequence('Ctrl+S'),self);self.save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut);self.save_shortcut.activated.connect(self.save)
    def raw_draft(self):
        return {'output_property_id':self.output.currentData(),'description':self.description.text(),
                'expression':self.expression.expression()}
    def parameters(self):
        output=self.output.currentData()
        if output is None:raise ValueError('Choose a computed property, or add a property with its rule.')
        spec={'output_property_id':output,'description':self.description.text(),'expression':self.expression.expression()}
        result={'record_type':self.owner,'specification':spec}
        if self.definition:result['rule_id']=self.definition['rule_id']
        return result
    def preview(self):
        if self.busy:return
        try:params=self.parameters()
        except ValueError as exc:self.message.setText(str(exc));return
        def validated(response):
            self.preview_request('rule.preview',{**params,'page_size':25},render)
        def render(response):
            self.preview_panel.render(response);self.editor_body.setCurrentIndex(1)
            self.message.setText('Preview checked by KM. No changes saved.')
        self.preview_request('rule.validate',params,validated)
    def explain(self,rid):
        if self.busy:self.preview_panel.note.setText('The current request is still running. Use Explain selected result when it finishes.');return
        try:params={**self.parameters(),'page_size':1,'explain_record_id':rid}
        except ValueError as exc:self.message.setText(str(exc));return
        self.preview_request('rule.preview',params,self.preview_panel.render_explanation,as_of=self.preview_panel.snapshot['as_of'])
    def new_property(self):
        if self.is_dirty() and self.output.count() and not confirm(self,'Create a new output?','The new property has its own rule draft. Discard this rule draft and continue?'):return
        self.child_editor=PropertyEditor(self.client,self.owner,self.schema,parent=self)
        self.child_editor.mode.setCurrentIndex(self.child_editor.mode.findData('computed'))
        self.child_editor.arm_draft(self.child_editor.parameters)
        def finished(result):
            if result==QDialog.DialogCode.Accepted:self.accept()
        self.child_editor.finished.connect(finished);self.child_editor.open()
    def save(self):
        if self.busy:return
        try:params=self.parameters()
        except ValueError as exc:self.message.setText(str(exc));return
        if self.definition:
            operation='rule.edit';params={'rule_id':self.definition['rule_id'],'changes':{k:v for k,v in params['specification'].items() if k!='output_property_id'}}
        else:operation='rule.create'
        self.request(operation,params,lambda r:self.accept(),mutation=True)


class PresetEditor(BuilderDialog):
    def __init__(self,client,preset,properties,parent=None,*,schema=None):
        super().__init__(client,'Start from an editable preset',parent);self.resize(860,720)
        self.preset=preset;self.controls={};self.bindings={};self.saved_id=None;self.schema=schema or {}
        self.fields={x['field_id']:x for x in self.schema.get('fields',[])}
        for prop in properties:self.fields.setdefault(prop['property_id'],{'label':prop.get('label',prop.get('display_label',prop['property_id']))})
        self.editor_body=QTabWidget();self.layout_box.addWidget(self.editor_body,1)
        page=QWidget();box=QVBoxLayout(page);form_host=QWidget();form=QFormLayout(form_host)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name=QLineEdit(preset['property_schema']['display_label']);self.name.setMaxLength(160);form.addRow('Property name',self.name)
        self.copy=QCheckBox('Create a separate variant');self.copy.setChecked(preset.get('installed',False));form.addRow('',self.copy)
        self.enabled=QCheckBox('Enable the added property'+(' and rule' if preset['mode']=='computed' else ''));self.enabled.setChecked(True);form.addRow('',self.enabled)
        for key,spec in preset.get('setting_schema',{}).items():
            widget=TypedValue(client,spec,preset.get('default_settings',{}).get(key));self.controls[key]=widget;form.addRow(humanize(key),widget)
        for dep in preset.get('dependency_inputs',[]):
            widget=QComboBox();widget.addItem('Choose a prerequisite…',None)
            for prop in properties:
                if prop.get('mode')==dep['mode'] and prop.get('value_type')==dep['value_type'] and prop.get('state')!='retired':
                    widget.addItem(prop.get('label',prop['property_id'])+' · '+humanize(prop.get('state','enabled')),prop['property_id'])
            pos=widget.findData(dep['default_property_id'])
            if pos>=0:widget.setCurrentIndex(pos)
            self.bindings[dep['name']]=widget;form.addRow(humanize(dep['name'])+' prerequisite',widget)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame);scroll.setWidget(form_host);box.addWidget(scroll,1)
        box.addWidget(label('This creates ordinary editable definitions. The Properties and Rules editors can change them just like definitions you create yourself.'))
        self.editor_body.addTab(page,'Template settings')
        preview=QWidget();pl=QVBoxLayout(preview);self.recipe_notice=label('Preview shows exactly which property and rule the template will add. Nothing is saved.');pl.addWidget(self.recipe_notice)
        self.recipe_tree=QTreeWidget();self.recipe_tree.setHeaderLabels(['Configuration','Definition']);self.recipe_tree.header().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch);pl.addWidget(self.recipe_tree,1)
        self.editor_body.addTab(preview,'Preview')
        self.variant_id='ws:user:'+uuid.uuid4().hex
        self.buttons(self.apply,self.preview);self.save_button.setText('Add to Workspace');self.watch_inputs();self.arm_draft(self.params)
    def params(self):
        result={'preset_id':self.preset['preset_id'],'label':self.name.text().strip(),'enabled':self.enabled.isChecked(),
                'settings':{key:value.value() for key,value in self.controls.items()},
                'dependency_properties':{key:value.currentData() for key,value in self.bindings.items()}}
        if self.copy.isChecked():result['property_id']=self.variant_id
        return result
    def changed(self,*_):
        self.recipe_notice.setText('Settings changed — preview again before applying.')
    def preview(self):
        try:params=self.params()
        except ValueError as exc:self.message.setText(str(exc));return
        def render(response):
            result=response['result'];prop=result['property'];self.recipe_tree.clear()
            for key,value in [('Name',prop.get('label',prop.get('display_label',''))),('Value type',TYPE_LABELS.get(prop['value_type'],prop['value_type'])),('How values are set','Rule' if prop['mode']=='computed' else 'Manually'),('Initial state','Enabled' if self.enabled.isChecked() else 'Disabled')]:
                self.recipe_tree.addTopLevelItem(QTreeWidgetItem([key,value]))
            if result.get('rule'):
                root=QTreeWidgetItem(['Rule','Editable after application']);self.recipe_tree.addTopLevelItem(root)
                def add(parent,node):
                    child=QTreeWidgetItem([condition_text(node,self.fields),'']);parent.addChild(child)
                    group=next((key for key in GROUP_LABELS if key in node),None)
                    if group:
                        for item in ([node[group]] if group=='not' else node[group]):add(child,item)
                add(root,result['rule']['expression'])
            self.recipe_tree.expandAll();self.recipe_notice.setText('Template prepared by KM · ordinary editable definitions · no changes saved.')
            self.editor_body.setCurrentIndex(1)
        self.preview_request('preset.preview',params,render)
    def apply(self):
        if self.busy:return
        try:params=self.params()
        except ValueError as exc:self.message.setText(str(exc));return
        def saved(response):self.saved_id=response['result']['property_id'];self.accept()
        self.request('preset.apply',params,saved,mutation=True)


class ViewColumns(QWidget):
    """Explicit, bounded display columns. Dragging never changes query meaning."""
    changed=Signal()
    def __init__(self,owner,fields,layout=None,parent=None):
        super().__init__(parent);self.owner=owner;self.fields=fields;self.loading=False
        self.all_specs={c.key:c for c in view_columns_for(owner,fields)}
        self.layout_value=view_layout(owner,layout)
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0)
        outer.addWidget(label('Choose columns. Drag to reorder, or use Move up and Move down.'))
        tools=QHBoxLayout();self.find=QLineEdit();self.find.setPlaceholderText('Find a column…');self.find.setClearButtonEnabled(True)
        tools.addWidget(self.find,1);tools.addWidget(button('Reset layout',self.reset));outer.addLayout(tools)
        self.items=QListWidget();self.items.setAccessibleName('View column order and visibility')
        self.items.setMinimumHeight(150)
        self.items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.items.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection);outer.addWidget(self.items,1)
        moves=QHBoxLayout();moves.addWidget(button('Move up',lambda:self.move(-1)));moves.addWidget(button('Move down',lambda:self.move(1)))
        self.toggle_button=button('Hide column',self.toggle_selected);moves.addWidget(self.toggle_button)
        moves.addStretch(1);self.count=label('');moves.addWidget(self.count);outer.addLayout(moves)
        form=QFormLayout();self.width=QSpinBox();self.width.setRange(80,1600);self.width.setSuffix(' px')
        self.width.setAccessibleName('Selected column width');form.addRow('Column width',self.width)
        self.density=choices(['inherit','compact','balanced','comfortable'],self.layout_value['density'])
        self.density.setItemText(0,'Default spacing');form.addRow('Row spacing',self.density)
        self.secondary=QCheckBox('Show description below the title');self.secondary.setChecked(self.layout_value['secondary']);form.addRow(self.secondary)
        outer.addLayout(form);self.notice=label('These choices belong to this view.');self.notice.setObjectName('muted');outer.addWidget(self.notice)
        self.items.itemChanged.connect(self.toggled);self.items.currentItemChanged.connect(self.select)
        self.items.model().rowsMoved.connect(self.reordered);self.width.valueChanged.connect(self.resize_column)
        self.find.textChanged.connect(self.filter_columns);self.density.currentIndexChanged.connect(self.emit_changed);self.secondary.toggled.connect(self.emit_changed)
        self.populate()
    def populate(self):
        self.loading=True;self.items.clear();prefs=self.layout_value['columns']
        order=list(prefs['order'])+[k for k in self.all_specs if k not in prefs['order']]
        for key in order:
            spec=self.all_specs.get(key);item=QListWidgetItem(spec.label if spec else 'Unavailable field')
            item.setData(ROLE,key);item.setToolTip('<pre>'+escape((spec.label if spec else 'This saved detail is unavailable.'))+'</pre>')
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable|Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if key in prefs['visible'] else Qt.CheckState.Unchecked)
            self.items.addItem(item)
        self.loading=False;self.items.setCurrentRow(0);self.filter_columns();self.update_count()
    def current_columns(self):
        prefs=self.layout_value['columns'];order=[];visible=[]
        for n in range(self.items.count()):
            item=self.items.item(n);key=item.data(ROLE)
            # Persist only selected columns. Merely discovering many Workspace
            # fields must not fill the bounded display configuration.
            if item.checkState()==Qt.CheckState.Checked or key=='record':order.append(key);visible.append(key)
        return {'order':order,'visible':visible,'widths':dict(prefs['widths'])}
    def value(self):
        return view_layout(self.owner,{'columns':self.current_columns(),'density':self.density.currentData(),'secondary':self.secondary.isChecked()})
    def update_count(self):
        amount=sum(self.items.item(n).checkState()==Qt.CheckState.Checked for n in range(self.items.count()))
        self.count.setText(f'{amount} selected');self.count.setToolTip(f'Up to {MAX_VIEW_COLUMNS} columns')
    def emit_changed(self,*_):
        if not self.loading:self.changed.emit()
    def toggled(self,item):
        if self.loading:return
        key=item.data(ROLE)
        total=sum(self.items.item(n).checkState()==Qt.CheckState.Checked for n in range(self.items.count()))
        if (key=='record' and item.checkState()!=Qt.CheckState.Checked) or total>MAX_VIEW_COLUMNS:
            self.loading=True;item.setCheckState(Qt.CheckState.Checked if key=='record' else Qt.CheckState.Unchecked);self.loading=False
            self.notice.setText('Keep the Record column and choose up to '+str(MAX_VIEW_COLUMNS)+' columns.')
        self.update_count();self.changed.emit()
    def select(self,current,previous):
        if current is None:return
        key=current.data(ROLE);self.width.blockSignals(True)
        self.toggle_button.setEnabled(key!='record')
        self.toggle_button.setText('Hide column' if current.checkState()==Qt.CheckState.Checked else 'Show column')
        self.width.setRange(180,1600) if key=='record' else self.width.setRange(80,900)
        self.width.setValue(self.layout_value['columns']['widths'].get(key,self.all_specs.get(key,ColumnSpec(key,'',200)).width))
        self.width.blockSignals(False)
    def toggle_selected(self):
        item=self.items.currentItem()
        if item and item.data(ROLE)!='record':
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState()==Qt.CheckState.Checked else Qt.CheckState.Checked)
            self.select(item,None)
    def resize_column(self,value):
        item=self.items.currentItem()
        if item:self.layout_value['columns']['widths'][item.data(ROLE)]=value;self.emit_changed()
    def reordered(self,*_):self.emit_changed()
    def move(self,delta):
        if self.find.text().strip():self.notice.setText('Clear the column search before reordering.');return
        row=self.items.currentRow();target=row+delta
        if 0<=row<self.items.count() and 0<=target<self.items.count():
            item=self.items.takeItem(row);self.items.insertItem(target,item);self.items.setCurrentRow(target);self.emit_changed()
    def filter_columns(self,*_):
        text=self.find.text().strip().casefold()
        self.items.setDragEnabled(not bool(text))
        for n in range(self.items.count()):
            item=self.items.item(n);item.setHidden(bool(text and text not in item.text().casefold()))
    def reset(self):
        self.layout_value=view_layout(self.owner);self.density.setCurrentIndex(0);self.secondary.setChecked(True);self.find.clear();self.populate();self.changed.emit()
    def set_layout(self,value):
        self.layout_value=view_layout(self.owner,value);self.loading=True
        self.density.setCurrentIndex(max(0,self.density.findData(self.layout_value['density'])))
        self.secondary.setChecked(self.layout_value['secondary']);self.loading=False
        self.populate();self.changed.emit()


class ConfiguredViewTable(QWidget):
    """The same bounded native result table in preview and in the workbench.

    Group membership and record order arrive from KM. The widget formats those
    facts only; it never filters, re-sorts or evaluates a Workspace property.
    """
    selected_record=Signal(str)
    layout_changed=Signal(object)
    def __init__(self,parent=None,*,open_on_select=False):
        super().__init__(parent);self.applying=False;self.open_on_select=open_on_select;self.owner=None
        self.payload=None;self.fields=[];self.layout_value=None;self.collapsed=set();self.view_key=None
        outer=QVBoxLayout(self);outer.setContentsMargins(4,4,4,4)
        self.tree=QTreeWidget();self.tree.setRootIsDecorated(False);self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection);self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(False);self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setAccessibleName('Configured view record results')
        self.tree.header().setSectionsMovable(True);self.tree.header().setFirstSectionMovable(True)
        self.tree.header().setStretchLastSection(False);self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setMinimumSectionSize(80);outer.addWidget(self.tree,1)
        controls=QHBoxLayout();self.expand=button('Expand groups',self.tree.expandAll);self.collapse=button('Collapse groups',self.tree.collapseAll)
        controls.addWidget(self.expand);controls.addWidget(self.collapse);controls.addStretch(1)
        self.info=label('');self.info.setObjectName('muted');controls.addWidget(self.info);outer.addLayout(controls)
        self.tree.itemActivated.connect(self.activate)
        if open_on_select:self.tree.itemSelectionChanged.connect(self.selected)
        self.tree.itemCollapsed.connect(self.group_collapsed);self.tree.itemExpanded.connect(self.group_expanded)
        self.tree.header().sectionResized.connect(self.header_changed);self.tree.header().sectionMoved.connect(self.header_changed)
    def selected(self):
        item=self.tree.currentItem()
        if item:self.activate(item)
    def activate(self,item,*_):
        if self.applying:return
        rid=item.data(0,ROLE)
        if isinstance(rid,str) and rid:self.selected_record.emit(rid)
    def group_collapsed(self,item):
        key=item.data(0,ROLE+1)
        if not self.applying and key is not None and len(self.collapsed)<200:self.collapsed.add(key)
    def group_expanded(self,item):
        if not self.applying:self.collapsed.discard(item.data(0,ROLE+1))
    def set_result(self,owner,fields,layout,data,*,key=None,density='compact'):
        current=self.tree.currentItem();selected=current.data(0,ROLE) if current else None
        scroll=self.tree.verticalScrollBar().value();self.applying=True;self.tree.blockSignals(True)
        try:
            if key!=self.view_key:self.collapsed.clear();scroll=0
            self.view_key=key;self.owner=owner;self.fields=fields;self.layout_value=view_layout(owner,layout);self.payload=data
            specs,prefs=resolved_view_columns(owner,fields,self.layout_value);self.specs=[c for c in specs if c.key in prefs['visible']]
            self.tree.clear();self.tree.setColumnCount(len(self.specs));self.tree.setHeaderLabels([c.label for c in self.specs])
            header=self.tree.header()
            for i,spec in enumerate(self.specs):
                # Reapply visual order rather than inheriting a previous view's header permutation.
                position=header.visualIndex(i)
                if position!=i:header.moveSection(position,i)
                self.tree.setColumnWidth(i,prefs['widths'].get(spec.key,spec.width))
            grouped=bool(data.get('group_by'));self.tree.setRootIsDecorated(grouped);self.expand.setVisible(grouped);self.collapse.setVisible(grouped)
            groups={};counts={};selection=None
            for raw in data.get('rows',[])[:1000]:
                if not raw.get('record_id') or not raw.get('record_type'):continue
                projected=display_row(raw)
                projected['reference_labels']={rid:display_row(ref)['label']+(' · retained' if not ref.get('active',True) else '') for rid,ref in raw.get('view_references',{}).items()}
                texts=[str(view_column_value(projected,c.key)) for c in self.specs]
                if self.layout_value['secondary'] and projected.get('secondary'):
                    for i,c in enumerate(self.specs):
                        if c.key=='record':texts[i]+='\n'+projected['secondary'];break
                item=QTreeWidgetItem([value if len(value)<400 else value[:399]+'…' for value in texts])
                item.setData(0,ROLE,projected['id'])
                item.setData(0,ROLE+2,{'record_id':projected['id'],'record_type':projected['target_kind']})
                for i,value in enumerate(texts):item.setToolTip(i,'<pre>'+escape(value[:6000])+'</pre>')
                grouping=raw.get('view_group')
                if not grouping and grouped:
                    value=raw.get(data['group_by']);grouping={'key':str(value),'label':str(value or 'Not supplied')}
                if grouping:
                    gkey=grouping['key'];counts[gkey]=counts.get(gkey,0)+1
                    if gkey not in groups:
                        node=QTreeWidgetItem([str(grouping['label'])]);node.setData(0,ROLE+1,gkey)
                        node.setFlags(node.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        self.tree.addTopLevelItem(node);groups[gkey]=node
                    groups[gkey].addChild(item)
                else:self.tree.addTopLevelItem(item)
                if projected['id']==selected:selection=item
            for gkey,node in groups.items():
                total=data.get('group_counts',{}).get(gkey)
                node.setText(0,node.text(0)+f' · {counts[gkey]} shown'+(f' / {total}' if total is not None else ''))
                node.setExpanded(gkey not in self.collapsed)
            spacing=self.layout_value['density'];spacing=density if spacing=='inherit' else spacing
            row_height={'compact':28,'balanced':34,'comfortable':42}.get(spacing,28)
            if self.layout_value['secondary']:row_height+=16
            self.tree.setStyleSheet('QTreeView::item { min-height: '+str(row_height)+'px; }')
            self.info.setText('Saved sort order · missing values remain explicit'+(' · some linked names use IDs (page limit)' if any(r.get('view_references_truncated') for r in data.get('rows',[])) else ''))
            if selection:self.tree.setCurrentItem(selection)
            self.tree.verticalScrollBar().setValue(scroll)
        finally:self.tree.blockSignals(False);self.applying=False
    def header_changed(self,*_):
        if self.applying or self.layout_value is None:return
        header=self.tree.header();value=deepcopy(self.layout_value)
        order=[self.specs[header.logicalIndex(n)].key for n in range(header.count())]
        value['columns']={'order':order,'visible':order,
            'widths':{c.key:self.tree.columnWidth(n) for n,c in enumerate(self.specs)}}
        self.layout_value=view_layout(self.owner,value);self.layout_changed.emit(self.layout_value)


class ViewEditor(BuilderDialog):
    """One readable view draft, shared predicates, exact KM preview, local layout."""
    def __init__(self,client,owner,schema,definition=None,parent=None,*,presentation=None,persist_presentation=None,duplicate=False):
        super().__init__(client,'Duplicate view' if duplicate else 'Edit view' if definition else 'Create view',parent)
        self.resize(1060,760);self.owner=owner;self.schema=schema;self.definition=None if duplicate else definition
        d=deepcopy(definition or {});self.builtin=bool(self.definition and not d['view_id'].startswith('view:user:'))
        self.persist_presentation=persist_presentation;self.saved_id=None;self.saved_presentation=None;self.query_committed=None
        self.preview_response=None;self.preview_semantics=None;self.preview_cursor=None;self.previous_cursors=[];self.preview_clock=None;self.child_editor=None
        self._legacy_query=None;self._legacy_selection=None
        self.editor_body=QTabWidget();self.layout_box.addWidget(self.editor_body,1)
        self.name=QLineEdit(d.get('display_label',d.get('label',d.get('name','')))+(' copy' if duplicate else ''));self.name.setMaxLength(160)
        self.name.setPlaceholderText('For example: Priority correspondence');self.name.setAccessibleName('Saved view name')
        self.description=QLineEdit(d.get('description',''));self.description.setMaxLength(600);self.description.setPlaceholderText('Optional purpose or audience for this view')
        self.order=QSpinBox();self.order.setRange(0,10000);self.order.setValue(d.get('order',100))
        self.visible=QCheckBox('Show in this record type’s navigation');self.visible.setChecked(d.get('visible',True))
        if d.get('protected'):self.visible.setChecked(True);self.visible.setEnabled(False)
        general=QWidget();gl=QVBoxLayout(general);form=QFormLayout();form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow('View name',self.name);form.addRow('Description',self.description);form.addRow('Sidebar order',self.order);form.addRow('',self.visible);gl.addLayout(form)
        gl.addWidget(label(humanize(owner)+' · smaller sidebar order numbers appear first. Saved views select existing records; they never change source facts.'))
        self.include=QCheckBox('Include retained / inactive records');self.include.setChecked(d.get('include_inactive',False));gl.addWidget(self.include)
        if self.builtin:
            self.description.setEnabled(False);self.include.setEnabled(False)
            gl.addWidget(label('This standard view’s record selection stays with KM. You may rename it, arrange it in navigation and customize table columns. Create a custom view for different conditions.'))
        gl.addStretch(1);self.editor_body.addTab(general,'1  View')
        filter_page=QWidget();fl=QVBoxLayout(filter_page)
        expression=d.get('filter_expression') or {'all':d.get('filters',[])}
        self.filter_enabled=QCheckBox('Only show records matching these conditions');self.filter_enabled.setChecked(bool(d.get('filter_expression') or d.get('filters')))
        fl.addWidget(self.filter_enabled)
        self.expression=ExpressionEditor(client,schema,expression);self.expression.tree.setHeaderLabel('View conditions');fl.addWidget(self.expression,1)
        links=QHBoxLayout();links.addWidget(button('Add Workspace property…',self.add_property));links.addWidget(button('Edit selected Workspace property…',self.edit_property));links.addStretch(1);fl.addLayout(links)
        fl.addWidget(label('Property changes are saved separately. Canceling this view does not undo a property you have saved.'))
        fl.addWidget(label('Use the same fields, typed inputs and ALL / ANY / NOT conditions as the rule builder. A view can use a property’s calculated result without copying its rule.'))
        self.expression.setEnabled(self.filter_enabled.isChecked());self.filter_enabled.toggled.connect(self.expression.setEnabled)
        self.editor_body.addTab(filter_page,'Conditions');self.editor_body.setTabEnabled(1,not self.builtin)
        arrange=QWidget();al=QVBoxLayout(arrange);sort_form=QFormLayout();al.addLayout(sort_form)
        available=schema['view_options'].get('ordering_fields',[]);self.sort_controls=[]
        chosen=d.get('sorts') or [{'field':d.get('sort','updated_at'),'direction':d.get('sort_direction','desc')}]
        
        for n in range(3):
            combo=SearchChoice();combo.setMinimumContentsLength(18)
            if n:combo.addItem('No additional ordering',None)
            for f in available:combo.addItem(f['label']+(' · Workspace' if f['authority']=='workspace' else ''),f['field_id'])
            choice=chosen[n] if n<len(chosen) else {'field':None,'direction':'asc'}
            at=combo.findData(choice['field'])
            if at<0 and choice['field'] is not None:
                combo.addItem('Unavailable ordering field',choice['field']);at=combo.findData(choice['field'])
            combo.setCurrentIndex(max(0,at));direction=choices(['asc','desc'],choice['direction'])
            direction.setItemText(0,'Ascending');direction.setItemText(1,'Descending')
            host=QWidget();row=QHBoxLayout(host);row.setContentsMargins(0,0,0,0);row.addWidget(combo,1);row.addWidget(direction)
            sort_form.addRow('Sort by' if n==0 else 'Then by',host);self.sort_controls.append((combo,direction))
        self.group=SearchChoice();self.group.addItem('No grouping',None)
        for f in schema['view_options'].get('grouping_fields',[]):
            self.group.addItem(f['label']+(' · calendar day' if f.get('grouping')=='local_calendar_day' else '')+(' · Workspace' if f['authority']=='workspace' else ''),f['field_id'])
        if d.get('group_by') not in (None,'none') and self.group.findData(d['group_by'])<0:self.group.addItem('Unavailable grouping field',d['group_by'])
        self.group.setCurrentIndex(max(0,self.group.findData(d.get('group_by'))));sort_form.addRow('Group by',self.group)
        self.group_direction=choices(['asc','desc'],d.get('group_direction','asc'));self.group_direction.setItemText(0,'Ascending');self.group_direction.setItemText(1,'Descending');sort_form.addRow('Group order',self.group_direction)
        al.addWidget(label('Missing values sort last. Record ID breaks remaining ties. Timestamp grouping uses calendar days in your Workspace timezone.'))
        self.columns=ViewColumns(owner,schema['view_options'].get('column_fields',[]),presentation);al.addWidget(self.columns,1)
        self.editor_body.addTab(arrange,'Presentation')
        if self.builtin:
            for combo,direction in self.sort_controls:combo.setEnabled(False);direction.setEnabled(False)
            self.group.setEnabled(False);self.group_direction.setEnabled(False)
            self.columns.setEnabled(d.get('presentation','table')=='table')
        preview=QWidget();pl=QVBoxLayout(preview);self.preview_note=label('Preview your exact selection and layout before saving. No records or definitions are changed.');pl.addWidget(self.preview_note)
        self.preview_table=ConfiguredViewTable();pl.addWidget(self.preview_table,1)
        self.special_preview=None
        if self.builtin and d.get('presentation','table')!='table':
            from .knowledge_views import KnowledgeView
            self.special_preview=KnowledgeView();pl.addWidget(self.special_preview,1);self.preview_table.hide()
        pager=QHBoxLayout();self.previous=button('Previous page',self.previous_page);self.next=button('Next page',self.next_page)
        self.previous.setEnabled(False);self.next.setEnabled(False);pager.addWidget(self.previous);pager.addWidget(self.next);pager.addStretch(1)
        pager.addWidget(button('Refresh preview',self.preview));pl.addLayout(pager);self.editor_body.addTab(preview,'Preview')
        self.buttons(self.save,self.preview);self.save_button.setText('Save view');self.preview_button.setText('Preview view')
        self.expression.expressionChanged.connect(self.changed);self.columns.changed.connect(self.changed)
        self.preview_table.layout_changed.connect(self.accept_preview_layout)
        self.watch_inputs()
        if d and not self.builtin and 'sorts' not in d and 'filter_expression' not in d:
            # Renaming or rearranging an older view must not silently replace
            # its original matching/tie-break semantics with the new query path.
            self._legacy_selection=self.selection_signature(self.spec())
            self._legacy_query={k:deepcopy(d[k]) for k in ('filters','sort','sort_direction','group_by','include_inactive') if k in d}
        self.arm_draft(self.draft);self.name.setFocus()
        self.save_shortcut=QShortcut(QKeySequence('Ctrl+S'),self);self.save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut);self.save_shortcut.activated.connect(self.save)
    @staticmethod
    def selection_signature(data):
        return signature({k:v for k,v in data.items() if k not in ('label','description','visible','order')})
    def spec(self):
        data={'label':self.name.text().strip(),'visible':self.visible.isChecked(),'order':self.order.value()}
        if not self.builtin:
            sorts=[]
            for combo,direction in self.sort_controls:
                field=combo.selected_value()
                if field is not None:sorts.append({'field':field,'direction':direction.currentData()})
            data.update({'record_type':self.owner,'description':self.description.text().strip(),'filters':[],
                'filter_expression':self.expression.expression() if self.filter_enabled.isChecked() else None,
                'sorts':sorts,'group_by':self.group.selected_value(),'group_direction':self.group_direction.currentData(),
                'include_inactive':self.include.isChecked()})
            if self._legacy_query is not None and self.selection_signature(data)==self._legacy_selection:
                data={**{k:data[k] for k in ('label','description','visible','order','record_type')},**deepcopy(self._legacy_query)}
        return data
    def draft(self):return {'query':self.spec(),'presentation':self.columns.value()}
    def accept_preview_layout(self,value):
        # A native header drag already painted its own change; don't rebuild its
        # tree mid-drag. Sync the draft controls without querying or repainting.
        self._preview_header_edit=True
        try:self.columns.set_layout(value)
        finally:self._preview_header_edit=False
    def changed(self,*_):
        if not hasattr(self,'preview_note') or getattr(self,'_preview_header_edit',False):return
        try:current=signature(self.spec())
        except (ValueError,TypeError,KeyError):current=None
        if self.preview_response and current==self.preview_semantics:
            self.render_preview(self.preview_response);return
        self.preview_note.setText('Preview is out of date. Refresh it to see your current conditions and ordering.')
        self.next.setEnabled(False);self.previous.setEnabled(False)
        self.preview_response=None
        if self.special_preview:self.special_preview.set_payload({'rows':[]},self.definition.get('presentation'))
        else:self.preview_table.set_result(self.owner,self.schema['view_options'].get('column_fields',[]),self.columns.value(),{'rows':[]},key='draft')
    def preview(self):
        if self.busy:return
        self.preview_cursor=None;self.previous_cursors=[];self.preview_clock=None;self.run_preview()
    def run_preview(self):
        try:spec=self.spec()
        except ValueError as exc:self.message.setText(str(exc));return
        stamp=self.preview_clock
        signature_now=signature(spec)
        params={'view_id':self.definition['view_id'],'page_size':50} if self.builtin else {'specification':spec,'page_size':50}
        if self.preview_cursor:params['cursor']=self.preview_cursor
        self.preview_response=None;self.next.setEnabled(False);self.previous.setEnabled(False)
        self.preview_note.setText('Checking the current selection…')
        if self.special_preview:self.special_preview.set_payload({'rows':[]},self.definition.get('presentation'))
        else:self.preview_table.set_result(self.owner,self.schema['view_options'].get('column_fields',[]),self.columns.value(),{'rows':[]},key='draft')
        def received(response):
            try:current=signature(self.spec())
            except (ValueError,TypeError,KeyError):current=None
            if signature_now!=current:self.message.setText('The selection changed during preview. Refresh it.');return
            if response.get('workspace_revision')!=self.revision:
                self.message.setText('Workspace configuration changed. Reopen this editor; your draft has not overwritten anything.');return
            self.preview_response=response;self.preview_clock=response.get('as_of');self.preview_semantics=signature_now;self.render_preview(response);self.editor_body.setCurrentIndex(3)
        self.request('view.query' if self.builtin else 'view.preview',params,received,as_of=stamp)
    def render_preview(self,response):
        data=response['result'];state=data.get('state','available');shown=len(data.get('rows',[]));total=data.get('total_count',shown)
        if state!='available':text=humanize(state)+' — '+str(data.get('reason') or 'Review configuration.')
        else:
            text=f'{shown} shown · {total:,} matching captured records · read-only preview'
            if data.get('unknown_count'):text+=f" · {data['unknown_count']} records could not be evaluated"
            if data.get('groups_truncated'):text+=' · group totals are bounded'
        self.preview_note.setText(text)
        if self.special_preview:self.special_preview.set_payload(data,'workspace_map' if self.definition.get('presentation')=='map' else self.definition.get('presentation'))
        else:self.preview_table.set_result(self.owner,self.schema['view_options'].get('column_fields',[]),self.columns.value(),data,key='draft')
        self.next.setEnabled(bool(data.get('next_cursor')) and len(self.previous_cursors)<40);self.previous.setEnabled(bool(self.previous_cursors))
    def next_page(self):
        if self.busy or not self.preview_response:return
        cursor=self.preview_response['result'].get('next_cursor')
        if cursor and len(self.previous_cursors)<40:self.previous_cursors.append(self.preview_cursor);self.preview_cursor=cursor;self.run_preview()
    def previous_page(self):
        if not self.busy and self.previous_cursors:self.preview_cursor=self.previous_cursors.pop();self.run_preview()
    def save(self):
        if self.busy or self.pending_save:return
        try:spec=self.spec();layout=self.columns.value()
        except ValueError as exc:self.message.setText(str(exc));return
        def finish():
            self.saved_presentation=layout
            try:
                if self.persist_presentation:self.persist_presentation(self.saved_id,self.owner,layout)
            except (OSError,ValueError) as exc:
                self.message.setText('The view query is saved, but its local layout could not be saved. Retry Save to store the layout without duplicating the view.');return
            self.accept()
        if self.saved_id and self.query_committed==signature(spec):finish();return
        def saved(response):
            self.saved_id=response['result'].get('definition_id') or (self.definition or {}).get('view_id')
            self.query_committed=signature(spec);self.definition={**(self.definition or {}),**spec,'view_id':self.saved_id}
            finish()
        if self.definition:op='view.edit';params={'view_id':self.definition['view_id'],'changes':spec}
        else:op='view.create';params={'record_type':self.owner,'specification':spec}
        self.request(op,params,saved,mutation=True)
    def add_property(self):self.open_property(None)
    def edit_property(self):
        try:field=self.expression.field.selected_value()
        except ValueError as exc:self.message.setText(str(exc));return
        if not isinstance(field,str) or not field.startswith('ws:'):
            self.message.setText('Choose a Workspace field in the condition editor. Core fields are read-only.');return
        self.request('property.get',{'property_id':field},lambda r:self.open_property({**r['result']['property'],'state':r['result']['property'].get('state',r['result']['property'].get('status','enabled'))}))
    def open_property(self,definition):
        if self.child_editor is not None:self.child_editor.raise_();return
        if self.busy:return
        try:self.expression.expression()
        except ValueError as exc:self.message.setText('Finish the current condition before opening a property: '+str(exc));return
        dialog=PropertyEditor(self.client,self.owner,self.schema,definition,self);self.child_editor=dialog
        dialog.revision=self.revision
        def finished(result):
            self.child_editor=None
            if result==QDialog.DialogCode.Accepted:
                self.revision=dialog.revision;self.reload_schema()
            dialog.deleteLater()
        dialog.finished.connect(finished);dialog.open()
    def reload_schema(self):
        document=self.expression.expression();layout=self.columns.value()
        def loaded(response):
            if response['workspace_revision']!=self.revision:self.message.setText('Workspace changed again. Reopen this view before saving.');return
            self.schema=response['result'];old=self.expression;host=old.parentWidget().layout();index=host.indexOf(old)
            self.expression=ExpressionEditor(self.client,self.schema,document);self.expression.setEnabled(self.filter_enabled.isChecked())
            self.expression.expressionChanged.connect(self.changed)
            try:self.filter_enabled.toggled.disconnect(old.setEnabled)
            except RuntimeError:pass
            self.filter_enabled.toggled.connect(self.expression.setEnabled);host.removeWidget(old);old.deleteLater();host.insertWidget(index,self.expression,1)
            self.columns.fields=self.schema['view_options'].get('column_fields',[]);self.columns.all_specs={x.key:x for x in view_columns_for(self.owner,self.columns.fields)}
            self.columns.set_layout(layout)
            # Existing sort selections survive; newly created scalar fields become selectable.
            for combo,_ in self.sort_controls:
                selected=combo.currentData();combo.blockSignals(True)
                for f in self.schema['view_options']['ordering_fields']:
                    index=combo.findData(f['field_id'])
                    caption=f['label']+(' · Workspace' if f['authority']=='workspace' else '')
                    if index<0:combo.addItem(caption,f['field_id'])
                    else:combo.setItemText(index,caption)
                combo.setCurrentIndex(max(0,combo.findData(selected)));combo.blockSignals(False)
            for f in self.schema['view_options']['grouping_fields']:
                if self.group.findData(f['field_id'])<0:self.group.addItem(f['label']+(' · Workspace' if f['authority']=='workspace' else ''),f['field_id'])
            self.preview_response=None;self.changed();self.message.setText('Property saved. Your unsaved view draft is retained; choose the new field in Conditions or Presentation.')
        self.request('builder.schema',{'record_type':self.owner},loaded)


class WorkspaceConfiguration(ManagedDialog):
    """The same registry-driven editor for all six record types."""
    def __init__(self,client,owner,parent=None,*,get_presentation=None,persist_presentation=None,display_name=None):
        super().__init__(client,(display_name or humanize(owner))+' · Workspace configuration',parent)
        self.get_presentation=get_presentation;self.persist_presentation=persist_presentation
        self.resize(1020,700);self.setSizeGripEnabled(True)
        self.owner=owner;self.bundle={};self.generation=0;self.tables={};self.loaded_revisions=set()
        self.detail_labels={};self.detail_text={};self.empty_labels={};self.group_splits={};self.actions={};self.child_dialog=None;self.selection_ids={}
        title=label((display_name or humanize(owner))+' Workspace');title.setObjectName('heading');self.layout_box.insertWidget(0,title)
        intro=label('Properties, rules, views and presets');intro.setObjectName('dialog_description')
        self.layout_box.insertWidget(1,intro)
        controls=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('Find a definition…');self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName('Filter Workspace configuration');controls.addWidget(self.search,1)
        self.show_retired=QCheckBox('Show retired');controls.addWidget(self.show_retired);self.layout_box.addLayout(controls)
        if owner=='Organization':
            self.owner_box=QComboBox();self.owner_box.addItem('Raw Core Organizations','Organization');self.owner_box.addItem('Effective Workspace Organizations','WorkspaceOrganization')
            self.layout_box.addWidget(self.owner_box);self.owner_box.currentIndexChanged.connect(lambda _:self.change_owner())
        self.tabs=QTabWidget();self.tabs.setObjectName('workspace_config_tabs')
        self.tabs.setDocumentMode(True)
        self.tabs.setAccessibleName('Workspace configuration sections')
        self.tabs.tabBar().hide()  # Keep the complete underlying QTabWidget API.
        shell=QWidget();shell.setObjectName('workspace_config_shell')
        shell_row=QHBoxLayout(shell);shell_row.setContentsMargins(0,0,0,0);shell_row.setSpacing(12)
        self.tab_nav=QFrame();self.tab_nav.setObjectName('workspace_section_nav');self.tab_nav.setFixedWidth(153)
        tab_nav_layout=QVBoxLayout(self.tab_nav)
        tab_nav_layout.setContentsMargins(7,9,7,9);tab_nav_layout.setSpacing(4)
        self.tab_nav_buttons=[];tab_button_group=QButtonGroup(self);tab_button_group.setExclusive(True)
        self.tab_names=('Properties','Rules','Views','Presets')
        for index,name in enumerate(self.tab_names):
            control=QPushButton(name);control.setObjectName('workspace_section_button')
            control.setCheckable(True);control.setAccessibleName('Show '+name+' configuration')
            control.clicked.connect(lambda _checked=False,i=index:self.tabs.setCurrentIndex(i))
            tab_button_group.addButton(control);tab_nav_layout.addWidget(control)
            self.tab_nav_buttons.append(control)
        tab_nav_layout.addStretch(1)
        shell_row.addWidget(self.tab_nav)
        shell_row.addWidget(self.tabs,1)
        self.layout_box.addWidget(shell,1)
        self.tabs.currentChanged.connect(lambda i:[button.setChecked(j==i) for j,button in enumerate(self.tab_nav_buttons)])
        self.tab_nav_buttons[0].setChecked(True)
        for group in self.tab_names:
            page=QWidget();page.setObjectName('workspace_editor_page')
            layout=QVBoxLayout(page);layout.setContentsMargins(16,15,12,12);layout.setSpacing(10)
            row=QHBoxLayout();row.setSpacing(7);actions={}
            if group=='Presets':
                actions['new']=button('Use preset…',self.apply_preset);actions['new'].setObjectName('primary');row.addWidget(actions['new'])
            else:
                singular={'Properties':'property','Rules':'rule','Views':'view'}[group]
                actions['new']=button('Add '+singular+'…',lambda checked=False,g=group:self.edit(g,True));actions['new'].setObjectName('primary');row.addWidget(actions['new'])
                actions['edit']=button('Edit…',lambda checked=False,g=group:self.edit(g,False));row.addWidget(actions['edit'])
                more=QToolButton();more.setText('More');more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup);menu=QMenu(more)
                for operation in ('enable','disable','retire'):
                    action=menu.addAction(operation.title());action.triggered.connect(lambda checked=False,g=group,a=operation:self.mutate(g,a));actions[operation]=action
                more.setMenu(menu);row.addWidget(more)
                if group=='Views':
                    actions['duplicate']=button('Duplicate…',self.duplicate_view);row.addWidget(actions['duplicate'])
            row.addStretch(1);layout.addLayout(row);self.actions[group]=actions
            split=QSplitter();table=QTableWidget(0,3);table.setHorizontalHeaderLabels(['Name','Values / behavior','State'])
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
            table.setColumnWidth(1,166);table.setColumnWidth(2,84)
            table.verticalHeader().hide();table.setAccessibleName(group+' configuration list');self.tables[group]=table;split.addWidget(table)
            detail=QWidget();detail.setObjectName('workspace_definition_detail')
            detail.setMinimumWidth(230);detail.setMaximumWidth(380)
            dl=QVBoxLayout(detail);dl.setContentsMargins(17,14,13,10);dl.setSpacing(11)
            heading=label('');heading.setObjectName('section');self.detail_labels[group]=heading;dl.addWidget(heading)
            text=QPlainTextEdit();text.setReadOnly(True);text.setObjectName('definition_description')
            text.setFrameShape(QFrame.Shape.NoFrame)
            text.setAccessibleName(group+' selected configuration description')
            dl.addWidget(text,1);self.detail_text[group]=text;split.addWidget(detail);split.setSizes([630,300]);split.setStretchFactor(0,1)
            # Small definition sets should not produce a giant empty technical
            # table. Larger collections remain scrollable in the same widget.
            split.setMaximumHeight(440)
            layout.addWidget(split);self.group_splits[group]=split
            empty=label('');empty.setObjectName('empty_state')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(145)
            empty.setAccessibleName(group+' empty state')
            layout.addWidget(empty);self.empty_labels[group]=empty
            layout.addStretch(1)
            table.itemSelectionChanged.connect(lambda g=group:self.describe(g))
            table.cellDoubleClicked.connect(lambda _r,_c,g=group:self.apply_preset() if g=='Presets' else self.edit(g,False))
            self.tabs.addTab(page,group)
        bottom=QHBoxLayout();bottom.setSpacing(9)
        advanced=button('Shared settings',self.settings);advanced.setObjectName('quiet_action')
        bottom.addWidget(advanced)
        refresh=button('Refresh',self.reload);refresh.setObjectName('quiet_action');bottom.addWidget(refresh)
        bottom.addStretch();bottom.addWidget(button('Close',self.accept));self.layout_box.addLayout(bottom)
        self.search.textChanged.connect(self.filter_rows);self.show_retired.toggled.connect(self.filter_rows)
        self.shortcuts=[]
        for key,fn in [('Ctrl+F',self.search.setFocus),('F5',self.reload),('Ctrl+N',self.new_current)]:
            item=QShortcut(QKeySequence(key),self);item.activated.connect(fn);self.shortcuts.append(item)
        self.reload()
    def new_current(self):
        name=self.tab_names[self.tabs.currentIndex()]
        if name=='Presets':self.apply_preset()
        else:self.edit(name,True)
    def change_owner(self):self.owner=self.owner_box.currentData();self.selection_ids={};self.reload()
    def reload(self,preferred=None):
        self.generation+=1;generation=self.generation
        for group in self.tables:
            selected=self.selected(group)
            if selected:self.selection_ids[group]=self.identifier(group,selected)
        if isinstance(preferred,dict):self.selection_ids.update(preferred)
        self.bundle={};self.loaded_revisions=set();self.tabs.setEnabled(False)
        self.tab_nav.setEnabled(False)
        self.message.setText('Loading Workspace configuration…');self.message.setVisible(True)
        for key,operation in (('schema','builder.schema'),('Properties','registry.properties'),('Rules','registry.rules'),('Views','registry.views'),('Presets','registry.presets')):
            def receive(response,key=key):
                if generation!=self.generation:return
                if not response['ok']:
                    self.message.setText(response['error']['code']+': '+response['error']['message']);return
                self.bundle[key]=response['result'];self.loaded_revisions.add(response['workspace_revision'])
                if len(self.bundle)==5:
                    if len(self.loaded_revisions)!=1:
                        self.message.setText('Configuration changed during loading. Refresh to read one consistent revision.');return
                    self.revision=response['workspace_revision'];self.populate();self.tabs.setEnabled(True)
                    self.tab_nav.setEnabled(True)
                    self.message.setText('');self.message.setVisible(False)
            try:self.client.request(operation,{'record_type':self.owner},receive,owner=self)
            except (ValueError,RuntimeError) as exc:self.message.setText(str(exc))
    @staticmethod
    def identifier(group,row):return row.get({'Properties':'property_id','Rules':'rule_id','Views':'view_id','Presets':'preset_id'}[group])
    def title_for(self,group,row):
        if group=='Presets':return row['property_schema']['display_label']
        if group=='Rules':
            prop=next((p for p in self.bundle['Properties']['workspace_properties'] if p['property_id']==row.get('output_property_id')),None)
            return (prop.get('label',prop.get('display_label','Property'))+' · rule') if prop else row.get('description') or 'Workspace rule'
        return row.get('label') or row.get('display_label') or row.get('name') or 'Unnamed definition'
    def populate(self):
        for group,key in (('Properties','workspace_properties'),('Rules','workspace_rules'),('Views','views'),('Presets','presets')):
            rows=self.bundle[group][key];table=self.tables[group];table.blockSignals(True);table.setRowCount(len(rows))
            selected_index=0
            for i,row in enumerate(rows):
                if self.identifier(group,row)==self.selection_ids.get(group):selected_index=i
                if group=='Properties':behavior=TYPE_LABELS.get(row.get('value_type'),'')+' · '+('Rule' if row.get('mode')=='computed' else 'Manual')
                elif group=='Rules':behavior='True / false classification'
                elif group=='Presets':behavior='Property + rule' if row['mode']=='computed' else 'Manual property'
                else:behavior=humanize(row.get('kind','View'))
                state=row.get('state','Installed' if row.get('installed') else 'Available')
                for j,value in enumerate((self.title_for(group,row),behavior,humanize(state))):
                    cell=QTableWidgetItem(str(value));cell.setData(ROLE,row);cell.setToolTip(str(self.identifier(group,row)));table.setItem(i,j,cell)
            if rows:table.selectRow(selected_index)
            table.blockSignals(False);self.describe(group)
        self.filter_rows()
    def filter_rows(self,*_):
        query=self.search.text().strip().casefold()
        for group,table in self.tables.items():
            visible=[]
            for i in range(table.rowCount()):
                row=table.item(i,0).data(ROLE)
                text=' '.join(table.item(i,j).text() for j in range(table.columnCount()))+' '+str(row.get('description',''))
                hidden=(query not in text.casefold()) or (row.get('state')=='retired' and not self.show_retired.isChecked())
                table.setRowHidden(i,hidden)
                if not hidden:visible.append(i)
            current=table.currentRow()
            if current<0 or table.isRowHidden(current):
                if visible:table.selectRow(visible[0])
                else:table.clearSelection()
            total=table.rowCount();number=len(visible)
            empty='No '+group.lower()+' yet. '+('Add a property or start with a preset.' if group=='Properties' else 'Use the controls above to configure this record type.')
            self.empty_labels[group].setText(empty if not total else 'No matching definitions.' if not number else '')
            self.empty_labels[group].setVisible(number==0)
            self.group_splits[group].setVisible(number>0)
            if number:
                self.group_splits[group].setMaximumHeight(min(440,max(185,number*35+105)))
            self.describe(group)
    def selected(self,group):
        table=self.tables[group]
        if not table.selectedItems():return None
        item=table.item(table.currentRow(),0)
        return item.data(ROLE) if item and not table.isRowHidden(table.currentRow()) else None
    def describe(self,group):
        row=self.selected(group);actions=self.actions[group]
        if 'edit' in actions:actions['edit'].setEnabled(bool(row and row.get('state')!='retired'))
        if 'duplicate' in actions:actions['duplicate'].setEnabled(bool(row and row.get('view_id','').startswith('view:user:') and row.get('state')!='retired'))
        for operation in ('enable','disable','retire'):
            if operation in actions:
                enabled=bool(row and row.get('state')!='retired')
                if row and operation=='enable':enabled=enabled and row.get('state')!='enabled'
                if row and operation=='disable':enabled=enabled and row.get('state')!='disabled' and not row.get('protected')
                if row and operation=='retire':enabled=enabled and not row.get('protected')
                actions[operation].setEnabled(enabled)
        if group=='Presets':actions['new'].setEnabled(bool(row))
        if group=='Rules':actions['new'].setEnabled(bool(self.bundle.get('schema',{}).get('condition_groups')))
        if group=='Views':actions['new'].setEnabled(self.owner!='WorkspaceOrganization')
        if not row:
            self.detail_labels[group].setText('Select a '+{'Properties':'property','Rules':'rule','Views':'view','Presets':'preset'}[group])
            self.detail_text[group].setPlainText('');return
        self.detail_labels[group].setText(self.title_for(group,row));parts=[]
        if group=='Presets':
            parts=[row.get('description') or row.get('property_schema',{}).get('description',''),
                   'Already installed.' if row.get('installed') else 'Available to add.']
        elif group=='Rules':
            parts=[row.get('description',''),'Condition summary:']
            fields={item['field_id']:item for item in self.bundle.get('schema',{}).get('fields',[])}
            if row.get('expression'):parts+=expression_lines(row['expression'],fields)
            # Rule authorization and immutable Core protections remain in KM.
        else:
            parts=[row.get('description',''),('Calculated from a rule.' if row.get('mode')=='computed' else 'Values are entered manually per record.') if group=='Properties' else 'Saved view settings control presentation, not source facts.']
            if row.get('state')=='retired':parts+=['Retired · retained for audit.']
        self.detail_text[group].setPlainText('\n\n'.join(str(x) for x in parts if x))
    def open_child(self,dialog,group=None):
        if self.child_dialog is not None and self.child_dialog.isVisible():
            dialog.deleteLater();self.child_dialog.raise_();self.child_dialog.activateWindow();return
        self.child_dialog=dialog;dialog.revision=self.revision
        def finished(result):
            if result==QDialog.DialogCode.Accepted:
                saved=getattr(dialog,'saved_id',None)
                self.reload({group or 'Properties':saved} if saved else None)
            self.child_dialog=None;dialog.deleteLater()
        dialog.finished.connect(finished);dialog.open()
    def edit(self,group,new):
        if not self.bundle.get('schema') or not self.tabs.isEnabled():return
        definition=None if new else self.selected(group)
        if not new and not definition:return
        if definition and definition.get('state')=='retired':self.message.setText('Retired definitions remain auditable and cannot be edited.');return
        schema=self.bundle['schema'];props=self.bundle['Properties']['workspace_properties']
        if group=='Properties':dialog=PropertyEditor(self.client,self.owner,schema,definition,self,rules=self.bundle['Rules']['workspace_rules'])
        elif group=='Rules':
            if not schema.get('condition_groups'):self.message.setText('This target supports manual values, not computed rules.');return
            dialog=RuleEditor(self.client,self.owner,schema,props,definition,self)
        elif group=='Views':
            if self.owner=='WorkspaceOrganization':self.message.setText('Choose Raw Core Organizations to configure Organization views.');return
            display=self.get_presentation((definition or {}).get('view_id'),self.owner) if self.get_presentation else None
            from .human_customization import HumanViewEditor
            dialog=HumanViewEditor(self.client,self.owner,schema,definition,self,presentation=display,persist_presentation=self.persist_presentation)
        else:return
        self.open_child(dialog,group)
    def duplicate_view(self):
        definition=self.selected('Views')
        if not definition or not definition.get('view_id','').startswith('view:user:'):return
        display=self.get_presentation(definition['view_id'],self.owner) if self.get_presentation else None
        from .human_customization import HumanViewEditor
        dialog=HumanViewEditor(self.client,self.owner,self.bundle['schema'],definition,self,
                          presentation=display,persist_presentation=self.persist_presentation,duplicate=True)
        self.open_child(dialog,'Views')
    def mutate(self,group,action):
        row=self.selected(group)
        if not row:return
        family={'Properties':'property','Rules':'rule','Views':'view'}[group];identifier=row[family+'_id']
        text='This changes the current Workspace configuration and preserves its history.'
        if action=='retire':text+=' Retirement is permanent without an explicit migration. Live dependencies may block it.'
        if not confirm(self,action.title()+' '+family,text+' Continue?'):return
        self.request(family+'.'+action,{family+'_id':identifier},lambda r:self.reload(),mutation=True)
    def apply_preset(self):
        row=self.selected('Presets')
        if not row:return
        dialog=PresetEditor(self.client,row,self.bundle['Properties']['workspace_properties'],self,schema=self.bundle['schema'])
        self.open_child(dialog,'Presets')
    def settings(self):self.open_child(SharedSettings(self.client,self))


class SharedSettings(BuilderDialog):
    """Human identity and dates; suggestions always require a deliberate choice."""
    def __init__(self,client,parent=None):
        super().__init__(client,'Your details',parent);self.resize(650,590)
        self.suggestions=[];self.settings_loaded=False
        self.editor_body=QTabWidget();self.layout_box.addWidget(self.editor_body,1)
        identity=QWidget();body=QVBoxLayout(identity);body.setSpacing(12)
        body.addWidget(label('Which email addresses are yours?'))
        body.addWidget(label('O3 uses these to tell messages from you apart from messages sent to you. Add the addresses you send from, including any aliases.'))
        self.aliases=QPlainTextEdit();self.aliases.setPlaceholderText('you@example.com\nAnother address, if you use one');self.aliases.setMaximumHeight(100);self.aliases.setAccessibleName('Your email addresses, one per line');body.addWidget(self.aliases)
        self.suggestion_note=label('');body.addWidget(self.suggestion_note)
        self.suggestion_box=QVBoxLayout();body.addLayout(self.suggestion_box)
        self.groups=QPlainTextEdit();self.groups.setPlaceholderText('For example: team@example.com');self.groups.setMaximumHeight(75);self.groups.setAccessibleName('Shared email addresses, one per line')
        body.addWidget(label('Shared email addresses (optional)'));body.addWidget(self.groups)
        body.addWidget(label('Add an address here only when it belongs to a group or shared mailbox. It will not count as your personal address.'));body.addStretch()
        identity_scroll=QScrollArea();identity_scroll.setWidgetResizable(True);identity_scroll.setFrameShape(QFrame.Shape.NoFrame);identity_scroll.setWidget(identity)
        self.editor_body.addTab(identity_scroll,'Email addresses')
        dates=QWidget();dl=QVBoxLayout(dates);form=QFormLayout();dl.addLayout(form)
        self.tz=QComboBox();self.tz.setEditable(True);self.tz.addItems(sorted(available_timezones()) or ['America/New_York'])
        self.tz.setAccessibleName('Time zone');self.week=choices(['monday','sunday']);self.week.setAccessibleName('First day of the week')
        form.addRow('Time zone',self.tz);form.addRow('Week starts on',self.week)
        dl.addWidget(label('Dates use your saved time zone. The starting choice is Eastern time in the United States; change it if you work elsewhere.'));dl.addStretch()
        date_scroll=QScrollArea();date_scroll.setWidgetResizable(True);date_scroll.setFrameShape(QFrame.Shape.NoFrame);date_scroll.setWidget(dates)
        self.editor_body.addTab(date_scroll,'Dates and time');self.buttons(self.save);self.save_button.setText('Save details');self.watch_inputs()
        self.request('configuration.get',{},self.loaded)
    def details(self):
        addresses=text_lines(self.aliases.toPlainText())
        for checkbox,address in self.suggestions:
            if checkbox.isChecked() and address.casefold() not in {a.casefold() for a in addresses}:addresses.append(address)
        return {'workspace_timezone':self.tz.currentText().strip(),'week_start':self.week.currentData(),
                'self_addresses':addresses,'group_addresses':text_lines(self.groups.toPlainText())}
    def loaded(self,response):
        settings=response['result']['settings'];self.revision=response['workspace_revision']
        self.tz.setCurrentText(settings['workspace_timezone']);self.week.setCurrentIndex(self.week.findData(settings['week_start']))
        self.aliases.setPlainText('\n'.join(settings['self_addresses']));self.groups.setPlainText('\n'.join(settings['group_addresses']))
        self.settings_loaded=True;self.save_button.setEnabled(True);self.arm_draft(self.details)
        # Optional identity suggestions are not adopted, and cannot replace a
        # user's edits even if the request completes after typing has started.
        def suggested(result):
            if not result.get('ok'):return
            known={a.casefold() for a in text_lines(self.aliases.toPlainText())}
            for suggestion in result['result'].get('suggestions',[]):
                address=suggestion.get('address')
                if not address or address.casefold() in known:continue
                checkbox=QCheckBox(address);checkbox.setAccessibleName('Confirm '+address+' is yours')
                self.suggestion_box.addWidget(checkbox);self.suggestions.append((checkbox,address));known.add(address.casefold())
            self.suggestion_note.setText('O3 found these addresses in your mail. Select only the ones that are yours.' if self.suggestions else '')
        self.client.request('mail.identity',{},suggested,owner=self)
    def save(self):
        if not self.settings_loaded:return
        self.request('configuration.save',self.details(),lambda r:self.accept(),mutation=True)
    def request_state_changed(self,busy,mutation):
        super().request_state_changed(busy,mutation)
        if hasattr(self,'save_button'):self.save_button.setEnabled(not busy and self.settings_loaded)


class ViewParameters(ManagedDialog):
    """Temporary query parameters, never a second saved-view authority."""
    def __init__(self,client,view,current,parent=None):
        super().__init__(client,'View options',parent);self.parameters=dict(current);self.controls={};self.specs=view.get('parameter_schema',{})
        self.layout_box.addWidget(label('These options apply to the current query. Use Workspace Configuration → Views to create an audited saved view. Blank optional bindings use KM’s unambiguous preset selection, if available.'))
        self.form=QFormLayout();self.layout_box.addLayout(self.form);self.properties_ready=not any(x['value_type']=='property_selector' for x in self.specs.values())
        for key,spec in self.specs.items():
            value=current.get('workspace_bindings',{}).get(key.split('.',1)[1]) if key.startswith('workspace_bindings.') else current.get(key)
            if spec['value_type']=='property_selector':
                control=QComboBox();control.addItem('Automatic / not assigned',None)
            else:
                if spec.get('serialize_id') and value:value={'record_type':spec['ref_type'],'record_id':value}
                control=TypedValue(client,spec,value)
            self.controls[key]=control;self.form.addRow(key.replace('workspace_bindings.','').replace('_',' ').title(),control)
        self.buttons(self.apply)
        if not self.properties_ready:
            def loaded(response):
                properties=response['result']['workspace_properties']
                for key,spec in self.specs.items():
                    if spec['value_type']!='property_selector':continue
                    control=self.controls[key]
                    for d in properties:
                        if d.get('state')=='enabled' and d['owner_type']==spec['owner_type'] and d['mode']==spec['mode'] and d['value_type']==spec['property_type']:
                            control.addItem(d.get('label',d['property_id']),d['property_id'])
                    old=current.get('workspace_bindings',{}).get(key.split('.',1)[1]) if '.' in key else current.get(key)
                    if old:control.setCurrentIndex(max(0,control.findData(old)))
                self.properties_ready=True
            self.request('registry.properties',{},loaded)
    def apply(self):
        if not self.properties_ready:self.message.setText('Property choices have not finished loading.');return
        params={}
        try:
            for key,spec in self.specs.items():
                control=self.controls[key];value=control.currentData() if spec['value_type']=='property_selector' else control.value()
                if value is None:continue
                if spec.get('serialize_id'):value=value['record_id']
                if key.startswith('workspace_bindings.'):params.setdefault('workspace_bindings',{})[key.split('.',1)[1]]=value
                else:params[key]=value
        except (ValueError,KeyError) as exc:self.message.setText(str(exc));return
        self.parameters=params;self.accept()
