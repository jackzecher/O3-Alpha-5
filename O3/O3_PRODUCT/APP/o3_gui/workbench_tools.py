"""Small native exploration tools over existing KM results and inspectors.

No SQL, semantic matching or persisted result cache. Dialog requests run through
KMClient and the same serialized worker; closing cancels presentation callbacks.
"""
from __future__ import annotations
from datetime import datetime, timezone
from html import escape
import json
from PySide6.QtCore import Qt, QTimer, QEvent, Signal
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QButtonGroup, QToolButton,
)
from o3_gui.km_session import display_row
from o3_gui.navigation import reference, ref_key, LatestRead
from o3_gui.investigation import (plain, decode, evidence_entries, lens_match,
                                  core_snapshots, historical_difference,
                                  comparison_record, differences, same_snapshot, MAX_ENTRIES)

ROLE = Qt.ItemDataRole.UserRole


def label(text='', name='muted'):
    w = QLabel(str(text)); w.setTextFormat(Qt.TextFormat.PlainText)
    w.setWordWrap(True); w.setObjectName(name)
    return w


def button(text, action):
    w = QPushButton(text); w.setAccessibleName(text); w.clicked.connect(action); return w


def read_only(name):
    w = QPlainTextEdit(); w.setReadOnly(True); w.setAccessibleName(name)
    return w


class CommandPalette(QDialog):
    """One picker for commands, views, pins, recent records and the visit trail."""
    def __init__(self, client, entries, *, record_search=False, kind=None, inactive=False, title='Commands', parent=None):
        super().__init__(parent)
        self.client = client; self.entries = list(entries); self.record_search = record_search
        self.kind = kind; self.inactive = inactive; self.choice = None; self.remote = []
        self.reads = LatestRead(); self.closed = False
        self.setWindowTitle(title); self.resize(670, 460); self.setMinimumSize(400, 280)
        layout = QVBoxLayout(self); layout.setContentsMargins(14, 12, 14, 12)
        self.query = QLineEdit(); self.query.setMaxLength(160); self.query.setClearButtonEnabled(True)
        self.query.setPlaceholderText('Find commands, views or captured records…' if record_search else 'Type a command or view…')
        self.query.setAccessibleName(self.query.placeholderText()); layout.addWidget(self.query)
        self.items = QListWidget(); self.items.setObjectName('commandResults');self.items.setAccessibleName(title+' results')
        self.items.setWordWrap(True); self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.items, 1)
        self.notice = label('Enter to open · Esc to close'); layout.addWidget(self.notice)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept_item); box.rejected.connect(self.reject); layout.addWidget(box)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(200)
        self.timer.timeout.connect(self.search_remote)
        self.query.textChanged.connect(self.changed); self.query.installEventFilter(self)
        self.items.itemActivated.connect(self.accept_item)
        self.finished.connect(self.dispose)
        self.populate(); self.query.setFocus()

    def dispose(self, *_):
        self.closed = True; self.timer.stop(); self.reads.invalidate(); self.client.cancel_owner(self)

    def eventFilter(self, watched, event):
        if watched is self.query and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                step = 1 if key == Qt.Key.Key_Down else -1
                self.items.setCurrentRow(max(0, min(self.items.count()-1, self.items.currentRow()+step)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.accept_item(); return True
        return super().eventFilter(watched, event)

    def changed(self, *_):
        self.remote.clear(); self.reads.invalidate(); self.populate()
        if self.record_search and self.query.text().strip(): self.timer.start()
        else: self.timer.stop()

    def populate(self):
        terms = self.query.text().casefold().split(); self.items.clear(); seen = set()
        choices = [e for e in self.entries if all(t in (e['label']+' '+e.get('description','')).casefold() for t in terms)]
        for entry in [*choices, *self.remote]:
            key = json.dumps(entry['payload'], sort_keys=True)
            if key in seen: continue
            seen.add(key)
            item = QListWidgetItem(entry['label']+'\n'+entry.get('description',''))
            item.setData(ROLE, entry['payload']); item.setToolTip('<pre>'+escape(entry['label'])+'</pre>')
            if not entry.get('enabled',True):item.setFlags(item.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsSelectable)
            self.items.addItem(item)
            if self.items.count() >= 100: break
        first=next((i for i in range(self.items.count()) if self.items.item(i).flags() & Qt.ItemIsEnabled),-1)
        self.items.setCurrentRow(first)
        self.notice.setText('Enter to open · Esc to close' if self.items.count() else 'No matching entries.')

    def search_remote(self):
        text = self.query.text().strip()
        if not text or self.closed: return
        payload = self.reads.offer({'text': text})
        if payload: self.dispatch(payload)

    def dispatch(self, payload):
        self.notice.setText('Searching captured, stored fields…')
        params = {'search': payload['text'], 'page_size': 50, 'include_inactive': self.inactive}
        if self.kind: params['record_type'] = self.kind
        def completed(response):
            accept, following = self.reads.finish(payload['request_id'])
            if self.closed: return
            if accept and payload['text'] == self.query.text().strip():
                if response.get('ok'):
                    result = response['result']; self.remote = []
                    for record in result.get('rows', []):
                        display = display_row(record)
                        self.remote.append({'label': display['label'],
                            'description': display['target_kind']+' · '+('Current' if display['active'] else 'Retained inactive'),
                            'payload': {'ref': {'record_type': display['target_kind'], 'record_id': display['id']}}})
                    self.populate()
                    if result.get('next_cursor'): self.notice.setText('First 50 matching records. Refine the search to narrow them.')
                else:
                    self.populate(); self.notice.setText(response['error']['code']+': '+response['error']['message'])
            if following: self.dispatch(following)
        try: self.client.request('records.query', params, completed, owner=self)
        except (ValueError, RuntimeError) as exc:
            self.reads.finish(payload['request_id']); self.notice.setText(str(exc))

    def accept_item(self, *_):
        item = self.items.currentItem()
        if item is not None and item.flags() & Qt.ItemIsEnabled:
            self.choice = item.data(ROLE); self.accept()


class DifferenceTable(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(['Field', 'Change', 'Before', 'After'])
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.verticalHeader().hide(); self.setWordWrap(False)
        header=self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(54)
        header.setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1,QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(1,70)
        for i,description in enumerate(('Exact JSON field path','Added / removed / changed',
                                         'Stored value before the event','Stored value after the event')):
            self.horizontalHeaderItem(i).setToolTip(description)
        self.setAccessibleName('Exact field differences; missing and null are distinct')

    def display(self, result):
        rows = result.get('changes', []); self.setRowCount(len(rows))
        for i, row in enumerate(rows):
            vals = [row['path'], row['change'],
                    plain(row['before'], 4000) if row['before_present'] else '⟨not present⟩',
                    plain(row['after'], 4000) if row['after_present'] else '⟨not present⟩']
            for j, val in enumerate(vals):
                item = QTableWidgetItem(val); item.setToolTip('<pre>'+escape(val)+'</pre>'); self.setItem(i, j, item)


class CompareDialog(QDialog):
    """Two current KM records, same fixed clock; mismatched epochs are explicit."""
    def __init__(self, client, left, right, parent=None):
        super().__init__(parent)
        self.client = client; self.refs = [reference(left), reference(right)]; self.responses = {}; self.generation = 0
        self.setWindowTitle('Compare records — read-only'); self.resize(1000, 620)
        layout = QVBoxLayout(self); headers = QHBoxLayout(); self.labels = [label('', 'section'), label('', 'section')]
        for item in self.labels: headers.addWidget(item, 1)
        layout.addLayout(headers); self.notice = label(); layout.addWidget(self.notice)
        self.table = DifferenceTable(); layout.addWidget(self.table, 1)
        layout.addWidget(label('Exact stored-field comparison, not a merge or inferred relationship. Arrays retain their source order.'))
        row = QHBoxLayout(); row.addWidget(button('Refresh both', self.refresh)); row.addStretch(); row.addWidget(button('Close', self.accept)); layout.addLayout(row)
        self.finished.connect(lambda _: client.cancel_owner(self)); self.refresh()

    def refresh(self):
        self.generation += 1; generation = self.generation
        self.client.cancel_owner(self); self.responses.clear(); self.table.setRowCount(0)
        stamp = datetime.now(timezone.utc).isoformat(); self.notice.setText('Reading both records through KM…')
        for i, ref in enumerate(self.refs):
            self.labels[i].setText(('Left' if i == 0 else 'Right')+' · '+str(ref['record_id']))
            def loaded(response, index=i):
                if generation != self.generation: return
                self.responses[index] = response
                if not response.get('ok'):
                    self.notice.setText(response['error']['code']+': '+response['error']['message']); return
                self.labels[index].setText(display_row(response['result']['record'])['label']+'\n'+self.refs[index]['record_type'])
                if len(self.responses) != 2 or not all(r.get('ok') for r in self.responses.values()): return
                if not same_snapshot(self.responses[0], self.responses[1]):
                    self.notice.setText('Workspace changed between reads. Refresh both before comparing; no mixed-snapshot differences are shown.'); return
                result = differences(*(comparison_record(self.responses[k]['result']['record']) for k in (0, 1)))
                self.table.display(result)
                self.notice.setText(f"{len(result['changes'])} differing fields · Same Core epoch and Workspace revision · {stamp}"+
                                    (' · Display limit reached; not a complete comparison.' if result['truncated'] else ''))
            try: self.client.request('records.get', ref, loaded, owner=self, as_of=stamp)
            except (ValueError, RuntimeError) as exc: self.notice.setText(str(exc))


class EvidenceLens(QWidget):
    open_record = Signal(str)
    preview_requested = Signal(str, object)

    def __init__(self, raw, parent=None):
        super().__init__(parent)
        self.raw = raw; self.detail = {}; self.offset = 0; self.rows = []; self.choice = None
        layout = QVBoxLayout(self); layout.setContentsMargins(8, 7, 8, 7)
        self.filter = QLineEdit(); self.filter.setPlaceholderText('Filter visible evidence…'); self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName('Filter displayed evidence entries'); self.filter.textChanged.connect(self.populate)
        layout.addWidget(self.filter)
        self.notice = label(); layout.addWidget(self.notice)
        self.empty_state = label('No captured evidence in this record.\n\nThis does not establish source completeness.', 'empty_state')
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setAccessibleName('No captured evidence for the selected record')
        layout.addWidget(self.empty_state,1)
        split = QSplitter(Qt.Orientation.Vertical)
        self.split=split
        self.tree = QTreeWidget();self.tree.setObjectName('evidence_entries')
        self.tree.setHeaderLabels(['Evidence', 'Authority'])
        self.tree.setAccessibleName('Evidence Lens; source, producer, manual and computed authorities')
        self.tree.setColumnWidth(0, 270); self.tree.itemSelectionChanged.connect(self.select)
        split.addWidget(self.tree)
        self.raw_group=QWidget();detail_layout=QVBoxLayout(self.raw_group)
        detail_layout.setContentsMargins(0,0,0,0)
        detail_layout.addWidget(label('Source facts / evidence payload','eyebrow'))
        detail_layout.addWidget(raw)
        split.addWidget(self.raw_group);split.setStretchFactor(0,2);split.setStretchFactor(1,2)
        self.technical_toggle=QToolButton();self.technical_toggle.setObjectName('disclosure')
        self.technical_toggle.setText('Show technical evidence ▸');self.technical_toggle.setCheckable(True)
        self.technical_toggle.setAccessibleName('Toggle source and evidence technical details')
        self.technical_toggle.toggled.connect(self.toggle_technical)
        layout.addWidget(split,1);layout.addWidget(self.technical_toggle)
        self.raw_group.hide()
        self.actions_bar=QWidget();row=QHBoxLayout(self.actions_bar)
        row.setContentsMargins(0,0,0,0)
        self.previous = button('Previous', lambda: self.page(-1)); self.next = button('Next', lambda: self.page(1))
        row.addWidget(self.previous); row.addWidget(self.next); row.addStretch()
        self.open = button('Open record', self.navigate); row.addWidget(self.open)
        self.preview = button('Verify & preview source', self.request_preview); row.addWidget(self.preview)
        layout.addWidget(self.actions_bar)
        self.clear()

    def toggle_technical(self,enabled):
        self.raw_group.setVisible(enabled)
        self.technical_toggle.setText('Hide technical evidence ▾' if enabled else 'Show technical evidence ▸')

    def clear(self):
        self.detail = {}; self.offset = 0; self.rows = []; self.choice = None; self.tree.clear(); self.raw.clear()
        self.notice.setText('')
        self._show_evidence_state(total=0,matched=0)
        for w in (self.previous, self.next, self.open, self.preview): w.setEnabled(False)

    def _show_evidence_state(self,total,matched):
        available = total > 0
        # Keep the filter when the current page contains entries but none match.
        self.filter.setVisible(available)
        self.split.setVisible(available and matched>0)
        self.empty_state.setVisible(not (available and matched>0))
        self.empty_state.setText(('No evidence matches the current filter.' if available else
            'No captured evidence in this record.\n\nThis does not establish source completeness.'))
        self.technical_toggle.setVisible(available and matched>0)
        self.actions_bar.setVisible(available and matched>0)
        if not (available and matched>0):
            self.technical_toggle.setChecked(False)

    def set_detail(self, detail):
        same = detail.get('id') == self.detail.get('id'); self.detail = detail
        if not same: self.offset = 0; self.filter.clear()
        self.populate()

    def page(self, delta):
        self.offset = max(0, self.offset + delta*MAX_ENTRIES); self.populate()

    def populate(self, *_):
        result = evidence_entries(self.detail, offset=self.offset)
        if self.offset >= result['total'] and self.offset:
            self.offset = max(0, ((result['total']-1)//MAX_ENTRIES)*MAX_ENTRIES); result = evidence_entries(self.detail, offset=self.offset)
        self.rows = result['rows']; self.tree.clear(); self.choice = None; self.raw.clear()
        groups = {}; first = None
        for entry in self.rows:
            if not lens_match(entry, self.filter.text()): continue
            group = entry['group']
            if group not in groups:
                groups[group] = QTreeWidgetItem([group, '']); self.tree.addTopLevelItem(groups[group]); groups[group].setExpanded(True)
            item = QTreeWidgetItem([entry['title'], entry['authority']]); item.setData(0, ROLE, entry)
            item.setToolTip(0, '<pre>'+escape(entry['title'])+'</pre>'); groups[group].addChild(item)
            if first is None: first = item
        self.previous.setEnabled(self.offset > 0); self.next.setEnabled(result['has_next'])
        self.notice.setText((f"{self.offset+1}–{self.offset+len(self.rows)} of {result['total']} captured items"
                             if self.rows else 'No captured evidence in this record') )
        self.notice.setToolTip('Filtering applies only to this page. Captured claims and derived views remain separate.')
        self.open.setEnabled(False); self.preview.setEnabled(False)
        self._show_evidence_state(result['total'],1 if first is not None else 0)
        if first is not None: self.tree.setCurrentItem(first)

    def select(self):
        item = self.tree.currentItem(); self.choice = item.data(0, ROLE) if item is not None else None
        choice = self.choice or {}; ref = choice.get('reference') or {}
        self.open.setEnabled(bool(ref.get('record_id')))
        self.preview.setEnabled(bool(ref.get('record_id') and choice.get('source_sha') and choice.get('group')=='Source observations'))
        self.raw.setPlainText(plain(choice.get('data')) if choice else '')

    def navigate(self):
        ref = (self.choice or {}).get('reference') or {}
        if ref.get('record_id'): self.open_record.emit(ref['record_id'])

    def inspect_connection(self, edge):
        self.choice={'group':'Current connections','authority':edge.get('authority','core'),
                     'data':edge,'reference':{'record_id':edge['other'],'record_type':edge.get('other_target_kind',edge.get('other_type'))}}
        self.open.setEnabled(True);self.preview.setEnabled(False)
        # A selected relationship can be inspected even if the selected record
        # has no separate Evidence Lens entries.
        self.empty_state.hide();self.split.show();self.actions_bar.show()
        self.technical_toggle.show();self.technical_toggle.setChecked(True)
        self.raw.setPlainText(plain(edge));self.notice.setText('Selected '+edge.get('authority','core')+' connection · all supplied owner claims are retained below. No inferred edge or archive.')

    def request_preview(self):
        choice = self.choice or {}; ref = choice.get('reference') or {}
        if ref.get('record_id') and choice.get('source_sha'):
            self.notice.setText('Verifying the selected immutable archive bytes…')
            self.preview_requested.emit(ref['record_id'], choice['source_sha'])

    def show_preview(self, result):
        self.raw.setPlainText(result['text'])
        self.notice.setText('Archive '+result.get('status','unavailable').replace('_',' ')+' · '+str(result.get('source_sha') or '')+
                            ' · Read-only source inspection, not a current-source certification.')


class HistoryLens(QWidget):
    """Chronological, keyboard-navigable history with an explicit authority filter.

    Actual captured history is displayed without reinterpreting event order,
    reconstructing missing snapshots, or evaluating any additional KM rules.
    """
    FAMILIES=('Core','Workspace','Relationships')
    def __init__(self, raw, parent=None):
        super().__init__(parent)
        self.raw=raw;self.history={};self.events=[];self.offset=0;self.total=0;self.family_index=0
        layout=QVBoxLayout(self);layout.setContentsMargins(10,9,10,9);layout.setSpacing(9)
        filters=QHBoxLayout();filters.setSpacing(4)
        self.family_buttons=[];group=QButtonGroup(self);group.setExclusive(True)
        for index,name in enumerate(self.FAMILIES):
            family_button=QPushButton(name);family_button.setObjectName('history_filter');family_button.setCheckable(True)
            family_button.setAccessibleName('History: '+name)
            family_button.setToolTip(('Core snapshots','Workspace operations','Relationship history')[index])
            family_button.clicked.connect(lambda _,i=index:self.change_family(i))
            group.addButton(family_button);filters.addWidget(family_button);self.family_buttons.append(family_button)
        self.family_buttons[0].setChecked(True)
        layout.addLayout(filters)
        self.notice=label('Select a captured event.');layout.addWidget(self.notice)
        self.timeline_bar=QWidget();controls=QHBoxLayout(self.timeline_bar)
        controls.setContentsMargins(0,0,0,0)
        controls.addWidget(label('Timeline','section'),1)
        self.previous=button('Previous',lambda:self.page(-1));self.next=button('Next',lambda:self.page(1))
        controls.addWidget(self.previous);controls.addWidget(self.next)
        layout.addWidget(self.timeline_bar)
        split=QSplitter(Qt.Orientation.Vertical);self.split=split
        self.event=QListWidget();self.event.setObjectName('history_timeline')
        self.event.setAccessibleName('Chronological history events; latest first')
        self.event.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.event.setAlternatingRowColors(False)
        self.event.currentRowChanged.connect(self.select)
        split.addWidget(self.event)
        result=QWidget();viewer=QVBoxLayout(result);viewer.setContentsMargins(0,0,0,0);viewer.setSpacing(5)
        viewer.addWidget(label('Changes in selected event','eyebrow'))
        self.table=DifferenceTable();viewer.addWidget(self.table,1)
        self.raw_toggle=QPushButton('Technical event details ▸')
        self.raw_toggle.setObjectName('disclosure');self.raw_toggle.setCheckable(True)
        self.raw_toggle.setChecked(False)
        self.raw_toggle.toggled.connect(self.show_raw)
        viewer.addWidget(self.raw_toggle)
        viewer.addWidget(raw);self.raw.hide()
        split.addWidget(result);split.setStretchFactor(0,2);split.setStretchFactor(1,3)
        layout.addWidget(split,1)
        self.empty_state=label('No captured history for this record.\n\nUnavailable history must not be interpreted as an absence of activity.', 'empty_state')
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setAccessibleName('No captured history for the selected record')
        layout.addWidget(self.empty_state,1)
        self.clear()

    def show_raw(self,visible):
        self.raw.setVisible(visible)
        self.raw_toggle.setText('Technical event details ▾' if visible else 'Technical event details ▸')

    def change_family(self,index):
        if index not in range(len(self.FAMILIES)):return
        self.family_index=index
        for i,button in enumerate(self.family_buttons):button.setChecked(i==index)
        self.reset_page()

    def clear(self):self.history={};self.events=[];self.offset=0;self.populate()
    def set_history(self,history):self.history=history if isinstance(history,dict) else {};self.offset=0;self.populate(latest=True)
    def reset_page(self,*_):self.offset=0;self.populate(latest=True)
    def page(self,delta):self.offset=max(0,self.offset+delta*200);self.populate()

    def populate(self,*_,latest=False):
        family=self.family_index;trace=self.history.get('core_trace') or {}
        rows=trace.get('record_history',[]) if family==0 else self.history.get('workspace_audit',[]) if family==1 else trace.get('relationship_history',[])
        self.total=len(rows)
        if latest:self.offset=max(0,(self.total-1)//200*200)
        self.offset=min(self.offset,max(0,(self.total-1)//200*200))
        self.events=core_snapshots(self.history,self.offset,200) if family==0 else rows[self.offset:self.offset+200]
        self.previous.setEnabled(self.offset>0);self.next.setEnabled(self.offset+200<self.total)
        self.split.setVisible(bool(self.events))
        self.timeline_bar.setVisible(bool(self.events))
        self.empty_state.setVisible(not self.events)
        self.notice.setVisible(bool(self.events))
        if not self.events:self.raw_toggle.setChecked(False)
        self.event.blockSignals(True);self.event.clear()
        # Show most recent captured event first, but retain original ascending
        # source indices to calculate historical differences correctly.
        for index,item in reversed(list(enumerate(self.events))):
            title=item['label'] if family==0 else f"{item.get('at_utc',item.get('changed_at',''))} · {item.get('operation',item.get('change_type',''))} · #{item.get('event_id','?')}"
            entry=QListWidgetItem(title);entry.setData(ROLE,index)
            entry.setToolTip('<pre>'+escape(title)+'</pre>');self.event.addItem(entry)
        self.event.setCurrentRow(0 if self.events else -1)
        self.event.blockSignals(False);self.select()

    def select(self,*_):
        selected=self.event.currentItem();index=selected.data(ROLE) if selected else -1
        self.table.setRowCount(0)
        if type(index) is not int or index<0 or index>=len(self.events):
            self.raw.clear();self.notice.setText('No captured events in this authority. Missing history is not proof that nothing happened.');return
        family=self.family_index;item=self.events[index]
        if family==0:
            result=historical_difference(self.history,self.offset+index);raw=item['raw']
            if result['available']:self.table.display(result)
            self.notice.setText(result.get('reason','Changes from the preceding captured Core snapshot.')+
                                (' Display limit reached.' if result.get('truncated') else ''))
        elif family==1:
            raw=item;result=differences(decode(item.get('before_json')),decode(item.get('after_json')))
            self.table.display(result)
            self.notice.setText('Audited operation delta; not a reconstructed full record snapshot.'+
                                (' Display limit reached.' if result['truncated'] else ''))
        else:
            raw=item;self.notice.setText('Recorded assertion, retraction or evidence change; missing prior state is not inferred.')
        self.notice.setText(self.notice.text()+f' · Event {self.offset+index+1} of {self.total}')
        self.raw.setPlainText(plain(raw))


class RuleLens(QWidget):
    """Human-readable captured evaluations, not a second evaluator."""
    def __init__(self,parent=None):
        super().__init__(parent)
        layout=QVBoxLayout(self);layout.setContentsMargins(11,10,11,10);layout.setSpacing(8)
        title=label('Captured rule outcomes','section');layout.addWidget(title)
        self.notice=label('Select a record to inspect its captured rule outcomes.')
        layout.addWidget(self.notice)
        self.rows=QTreeWidget();self.rows.setObjectName('rule_outcomes')
        self.rows.setHeaderLabels(['Rule / evaluation','Recorded outcome'])
        self.rows.header().setStretchLastSection(False)
        self.rows.header().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.rows.setColumnWidth(1,110)
        self.rows.setAccessibleName('Captured Core and KM rule results')
        self.rows.itemSelectionChanged.connect(self.show_selection)
        layout.addWidget(self.rows,1)
        self.empty_state=label('No captured rule results for this record.\n\nNo additional rules were evaluated by this interface.', 'empty_state')
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_state,1)
        self.more=QToolButton();self.more.setObjectName('disclosure');self.more.setCheckable(True)
        self.more.setText('Show technical rule payload ▸')
        self.more.toggled.connect(self.show_raw);layout.addWidget(self.more)
        self.raw=read_only('Selected captured rule payload');self.raw.hide();layout.addWidget(self.raw,1)
        self.clear()

    def clear(self):
        self.rows.clear();self.raw.clear()
        self.notice.setText('')
        self.rows.hide();self.more.hide();self.empty_state.show()
        self.more.setChecked(False)

    def show_raw(self,show):
        self.raw.setVisible(show)
        self.more.setText('Hide technical rule payload ▾' if show else 'Show technical rule payload ▸')

    def set_results(self,core,workspace):
        self.rows.blockSignals(True);self.rows.clear()
        groups=(('Captured Core evaluations',core),('KM Workspace results',workspace))
        count=0
        for title,values in groups:
            group=QTreeWidgetItem([title,'']);self.rows.addTopLevelItem(group)
            if isinstance(values,dict):
                items=[(str(k),v) for k,v in values.items()]
            elif isinstance(values,list):
                items=[('',v) for v in values]
            else:
                items=[] if values is None else [('',values)]
            for i,(field,value) in enumerate(items[:200]):
                if isinstance(value,dict):
                    rule=str(value.get('rule_id') or value.get('id') or value.get('property_id') or field or f'Evaluation {i+1}')
                    state=str(value.get('state') or value.get('status') or value.get('result_state') or 'Recorded')
                else:
                    rule=field or f'Entry {i+1}'
                    state=str(value) if isinstance(value,(bool,int,float,str)) else 'Recorded'
                item=QTreeWidgetItem([rule,state]);item.setData(0,ROLE,value)
                group.addChild(item);count+=1
            if not items:QTreeWidgetItem(group,['No captured entries',''])
            if len(items)>200:QTreeWidgetItem(group,[f'{len(items)-200} additional entries omitted in this view','See source record'])
            group.setExpanded(True)
        self.rows.blockSignals(False)
        self.notice.setText(f'{count} captured results' if count else '')
        self.notice.setToolTip('These are stored outcomes. The GUI does not evaluate rules.')
        self.rows.setVisible(count>0);self.more.setVisible(count>0)
        self.empty_state.setVisible(count==0)
        if not count:self.more.setChecked(False)
        self.raw.clear()

    def show_selection(self):
        item=self.rows.currentItem()
        data=item.data(0,ROLE) if item is not None else None
        self.raw.setPlainText(plain(data) if data is not None else '')
