"""Shared, readable view authoring over KM's typed preview/save contracts.

This module serializes choices only. It never evaluates records or duplicates
the knowledge model. Advanced expressions retain their exact saved meaning.
"""
from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QListWidget, QListWidgetItem, QLineEdit, QScrollArea, QFrame, QCheckBox, QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QSplitter
from .workspace_editor import (ViewEditor, ManagedDialog, button, label,
                               signature, ROLE, ExpressionEditor, EditHistory, display_row)
from .display_preferences import view_columns_for

SECTION_NAMES = {'Email':'Mail', 'Person':'People', 'Document':'Documents',
                 'Event':'Calendar', 'LineOfEffort':'Projects', 'Organization':'Organizations'}
QUERY_KEYS = ('filters', 'filter_expression', 'sort', 'sort_direction', 'sorts',
              'group_by', 'group_direction', 'include_inactive', 'search', 'parameters')
FIELD_NAMES = {'subject':'Subject','from':'Sender','to':'To','cc':'Cc','has_attachments':'Attachments',
    'importance':'Importance','received_at_utc':'Received','sent_at_utc':'Sent','updated_at':'Updated in O3',
    'displayName':'Name','mail':'Email address','companyName':'Organization','department':'Department',
    'jobTitle':'Job title','officeLocation':'Office','title':'Title','all_day':'All-day event','location':'Location',
    'organizer':'Organizer','start_utc':'Start','end_utc':'End','summary':'Event name','status':'Status',
    'name':'Name','objective':'Objective','reference_code':'Reference','kind':'Type'}


def human_field(info):
    key=info.get('field_id','')
    return {**info,'label':FIELD_NAMES.get(key,info.get('label','Additional detail'))}


def scroll_page(body):
    scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame);scroll.setWidget(body)
    return scroll


class HumanViewEditor(ViewEditor):
    """One lossless view draft: typed rules, exclusions, layout and exact preview.

    Include/exclude are a presentation of the canonical expression document.
    No verdict is calculated here. The same class is used by configuration,
    saved views and contextual exclusions.
    """
    QUERY_KEYS = QUERY_KEYS + ('unknown_policy', 'group_by_fields', 'representative_sort')

    def __init__(self, client, owner, schema, definition=None, parent=None, *,
                 seed_exclusion=None, focus_section=None, **kwargs):
        self._human_ready = False
        self.original_selection = self.query_part(definition or {})
        self.factory_specification = deepcopy((definition or {}).get('factory_specification'))
        schema = deepcopy(schema)
        schema['fields'] = [human_field(info) for info in schema.get('fields', [])]
        for key in ('ordering_fields', 'grouping_fields', 'column_fields'):
            schema['view_options'][key] = [human_field(info) for info in schema['view_options'].get(key, [])]
        super().__init__(client, owner, schema, definition, parent, **kwargs)
        self.resize(1040, 760)
        self.setWindowTitle('New view' if not self.definition else 'Configure view')
        for heading in self.findChildren(type(self.preview_note)):
            if heading.objectName() == 'heading': heading.setText(self.windowTitle())
        self.editor_body.setTabText(0, 'View')
        self.editor_body.setTabText(1, 'Rules')
        self.editor_body.setTabText(2, 'Layout')
        self.editor_body.setTabText(3, 'Preview')
        self.name.setPlaceholderText('Give this view a useful name')
        self.visible.setText('Show in ' + SECTION_NAMES.get(owner, 'this collection'))
        self.include.setText('Include retained or inactive records')

        # Reuse the authoritative expression editor, without the former flat
        # field/operator allowlist or read-only treatment of nested expressions.
        self.old_rules_page = self.editor_body.widget(1)
        self.editor_body.removeTab(1)
        self.old_rules_page.setParent(self); self.old_rules_page.hide()
        self.filter_enabled.hide()
        rules_page = QWidget(); rules_layout = QVBoxLayout(rules_page)
        rules_layout.setContentsMargins(12, 12, 12, 12)
        self.rules_note = label('Choose what belongs here. Exclusions affect only this view and apply to existing and future matching records.')
        self.rules_note.setObjectName('muted'); rules_layout.addWidget(self.rules_note)
        self.rule_sections = QTabWidget(); rules_layout.addWidget(self.rule_sections, 1)
        self.expression.setEnabled(not self.builtin)
        self.rule_sections.addTab(self.expression, 'Include')
        self.exclusions = ExpressionEditor(client, schema, {'any': []})
        self.rule_sections.addTab(self.exclusions, 'Exclude')
        self.expression.tree.setHeaderLabel('Include matching records')
        self.exclusions.tree.setHeaderLabel('Exclude matching records')
        self.unknown = QCheckBox('Keep records whose conditions could not be determined')
        self.unknown.setToolTip('Unknown is separate from false. The preview shows how many records could not be checked.')
        self.unknown.setChecked((definition or {}).get('unknown_policy', 'exclude') == 'include')
        rules_layout.addWidget(self.unknown)
        search_row = QFormLayout(); self.saved_search = QLineEdit((definition or {}).get('search', ''))
        self.saved_search.setMaxLength(160); self.saved_search.setClearButtonEnabled(True)
        self.saved_search.setPlaceholderText('Optional text that every matching record must contain')
        search_row.addRow('Search within this view', self.saved_search)
        general_layout = self.editor_body.widget(0).layout()
        general_layout.insertLayout(1, search_row)
        property_row = QHBoxLayout()
        property_row.addWidget(button('Add property…', self.add_property))
        property_row.addWidget(button('Edit selected property…', self.edit_property))
        property_row.addStretch(1); general_layout.insertLayout(2, property_row)
        self.editor_body.insertTab(1, rules_page, 'Rules')
        self.editor_body.setTabEnabled(1, not self.builtin)
        if self.builtin:
            self.rules_note.setText('This specialized view has a fixed query. Its name and supported layout remain editable.')
        self._group_fields = deepcopy((definition or {}).get('group_by_fields') or [])
        if self._group_fields:
            self.group.addItem('Conversations' if owner == 'Email' else 'Saved field combination', '__combined__')
            self.group.setCurrentIndex(self.group.findData('__combined__'))
        self.summary_note = label('')
        self.summary_note.setObjectName('muted')
        old_layout_page = self.editor_body.widget(2)
        sort_form = old_layout_page.layout().takeAt(0).layout(); sort_form.setParent(None)
        self.editor_body.removeTab(2); old_layout_page.setParent(self); old_layout_page.hide()
        arrangement = QWidget(); arrangement_layout = QVBoxLayout(arrangement)
        arrangement_layout.setContentsMargins(8, 10, 8, 8); arrangement_layout.addWidget(self.summary_note)
        layout_split = QSplitter(); arrangement_layout.addWidget(layout_split, 1)
        order_panel = QWidget(); order_layout = QVBoxLayout(order_panel)
        order_layout.setContentsMargins(0, 0, 12, 0)
        sort_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        for index, (combo, direction) in enumerate(self.sort_controls):
            combo.setMinimumContentsLength(6)
            direction.setMinimumContentsLength(7)
            direction.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            if index and combo.itemData(0) is None: combo.setItemText(0, 'None')
        self.group.setMinimumContentsLength(12)
        order_layout.addLayout(sort_form)
        ordering_note = label('Missing values sort last. Stable record identity breaks ties.')
        ordering_note.setObjectName('muted'); order_layout.addWidget(ordering_note); order_layout.addStretch(1)
        order_scroll = scroll_page(order_panel); order_scroll.setMinimumWidth(420)
        layout_split.addWidget(order_scroll); layout_split.addWidget(scroll_page(self.columns))
        layout_split.setStretchFactor(0, 2); layout_split.setStretchFactor(1, 3); layout_split.setSizes([420, 560])
        self.editor_body.insertTab(2, arrangement, 'Layout')
        self._set_expression_parts((definition or {}).get('filter_expression') or {'all': (definition or {}).get('filters', [])})
        self.exclusions.expressionChanged.connect(self.changed)
        self.unknown.toggled.connect(self.changed)
        self.saved_search.textChanged.connect(self.changed)
        self.rule_sections.currentChanged.connect(self.update_rule_hint)

        # The preview displays only evidence supplied by the query service.
        self.preview_page = self.editor_body.widget(3)
        self.examples = QTabWidget()
        preview_layout = self.preview_page.layout()
        preview_layout.removeWidget(self.preview_table)
        preview_layout.insertWidget(1, self.examples, 1)
        self.examples.addTab(self.preview_table, 'Matching')
        self.excluded_examples = QTreeWidget(); self.excluded_examples.setHeaderLabels(['Excluded record', 'Reason'])
        self.excluded_examples.setRootIsDecorated(False); self.excluded_examples.setWordWrap(True)
        self.excluded_examples.setAccessibleName('Excluded examples from the exact draft preview')
        self.excluded_examples.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.excluded_examples.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.examples.addTab(self.excluded_examples, 'Excluded')
        self.preview_reason = label('Select a preview record to see why it is here.')
        self.preview_reason.setObjectName('muted'); preview_layout.insertWidget(2, self.preview_reason)
        self.preview_table.tree.itemSelectionChanged.connect(self.show_preview_reason)
        self.excluded_examples.itemSelectionChanged.connect(self.show_excluded_reason)
        if self.special_preview:
            self.examples.hide(); self.preview_reason.hide()
        self._preview_reasons = {}

        actions = self.layout_box.itemAt(self.layout_box.count() - 1).layout()
        self.restore_button = button('Restore starter defaults', self.restore_factory)
        self.restore_button.setVisible(bool(self.factory_specification)); actions.insertWidget(0, self.restore_button)
        self.save_new_button = button('Save as a separate view', self.save_as_new)
        self.save_new_button.setVisible(bool(self.definition) and not self.builtin)
        actions.insertWidget(2, self.save_new_button)
        self.preview_button.setText('Preview changes')
        self.save_button.setText('Save this view' if self.definition else 'Create view')
        self._human_ready = True
        self._initial_controls = signature(self.control_query())
        self.arm_draft(self.draft)
        self.editor_body.setCurrentIndex(1 if not self.builtin else 0)
        self.update_rule_hint()
        if seed_exclusion is not None: self.seed_exclusion(seed_exclusion)
        if focus_section in ('exclude', 'exclusions'): self.rule_sections.setCurrentIndex(1)
        elif focus_section == 'layout': self.editor_body.setCurrentIndex(2)

    @classmethod
    def query_part(cls, definition):
        return {key: deepcopy(definition[key]) for key in cls.QUERY_KEYS if key in definition}

    @staticmethod
    def split_expression(expression):
        """Recognize only our exact two-part shape; never simplify an expression."""
        expression = deepcopy(expression or {'all': []})
        if set(expression) == {'not'}: return {'all': []}, expression['not']
        children = expression.get('all') if isinstance(expression, dict) else None
        if (isinstance(children, list) and len(children) == 2 and
                isinstance(children[1], dict) and set(children[1]) == {'not'}):
            return children[0], children[1]['not']
        return expression, {'any': []}

    def _set_expression_parts(self, expression):
        included, excluded = self.split_expression(expression)
        for editor, document in ((self.expression, included), (self.exclusions, excluded)):
            editor.set_expression(document); editor.history = EditHistory(document); editor.refresh_history()

    @staticmethod
    def empty_expression(expression):
        return expression in ({'all': []}, {'any': []}, None)

    def combined_expression(self):
        included = self.expression.expression()
        excluded = self.exclusions.expression()
        if self.empty_expression(excluded):
            return None if self.empty_expression(included) else included
        if self.empty_expression(included): return {'not': excluded}
        return {'all': [included, {'not': excluded}]}

    def control_query(self):
        sorts = [{'field': combo.selected_value(), 'direction': direction.currentData()}
                 for combo, direction in self.sort_controls if combo.selected_value() is not None]
        grouping = self.group.selected_value()
        result = {'filters': [], 'filter_expression': self.combined_expression(), 'sorts': sorts,
                  'group_by': None if grouping == '__combined__' else grouping,
                  'group_direction': self.group_direction.currentData(),
                  'include_inactive': self.include.isChecked(),
                  'unknown_policy': 'include' if self.unknown.isChecked() else 'exclude'}
        if grouping == '__combined__': result['group_by_fields'] = deepcopy(self._group_fields)
        if self.original_selection.get('representative_sort') and grouping == '__combined__':
            result['representative_sort'] = deepcopy(self.original_selection['representative_sort'])
        if self.saved_search.text().strip(): result['search'] = self.saved_search.text().strip()
        if 'parameters' in self.original_selection: result['parameters'] = deepcopy(self.original_selection['parameters'])
        return result

    def spec(self):
        if not self._human_ready: return super().spec()
        data = {'label': self.name.text().strip(), 'visible': self.visible.isChecked(), 'order': self.order.value()}
        if self.builtin: return data
        query = self.control_query()
        if self.original_selection and signature(query) == self._initial_controls:
            query = deepcopy(self.original_selection)
        return {**data, 'record_type': self.owner, 'description': self.description.text().strip(), **query}

    def update_rule_hint(self, *_):
        if not self._human_ready: return
        excluded = self.rule_sections.currentIndex() == 1
        self.rules_note.setText('Exclude records matching these conditions. This changes only this view; it does not delete records or mark mail answered.' if excluded else
            'Include records matching these conditions. Use All, Any, or nested groups; an empty Include section includes everything.')
        if self._group_fields and self.group.currentData() == '__combined__':
            self.summary_note.setText('Conditions apply to individual messages before conversation grouping. Each row uses the newest matching message; other eligible messages remain visible.')
        else: self.summary_note.setText('Column order, widths and row spacing belong to this view.')

    def seed_exclusion(self, expression):
        if self.builtin:
            self.message.setText('This specialized view does not support editable conditions.'); return
        if not isinstance(expression, dict): raise ValueError('An exclusion must be a condition or a group.')
        previous = self.exclusions.expression()
        document = deepcopy(expression) if self.empty_expression(previous) else {'any': [previous, deepcopy(expression)]}
        self.exclusions.set_expression(document); self.exclusions.checkpoint()
        self.editor_body.setCurrentIndex(1); self.rule_sections.setCurrentIndex(1)
        self.message.information('Review the suggested exclusion, then preview its effect. Nothing has been saved.')
        self.changed()

    def edit_property(self):
        editor = self.exclusions if self.rule_sections.currentIndex() == 1 else self.expression
        try: field = editor.field.selected_value()
        except ValueError as exc: self.message.setText(str(exc)); return
        if not isinstance(field, str) or not field.startswith('ws:'):
            self.message.setText('Select a user property condition to edit its definition.'); return
        self.request('property.get', {'property_id': field}, lambda r: self.open_property({**r['result']['property'], 'state': r['result']['property'].get('state', r['result']['property'].get('status', 'enabled'))}))

    def reload_schema(self):
        documents = [self.expression.expression(), self.exclusions.expression()]
        layout = self.columns.value(); selected_section = self.rule_sections.currentIndex()
        def loaded(response):
            if response['workspace_revision'] != self.revision:
                self.message.setText('The workspace changed again. Your draft is retained; review the saved changes before continuing.'); return
            self.schema = deepcopy(response['result'])
            self.schema['fields'] = [human_field(info) for info in self.schema.get('fields', [])]
            for key in ('ordering_fields', 'grouping_fields', 'column_fields'):
                self.schema['view_options'][key] = [human_field(info) for info in self.schema['view_options'].get(key, [])]
            self._human_ready = False
            for index, (attribute, document, caption) in enumerate(zip(('expression', 'exclusions'), documents, ('Include', 'Exclude'))):
                old = getattr(self, attribute); self.rule_sections.removeTab(index)
                editor = ExpressionEditor(self.client, self.schema, document)
                editor.expressionChanged.connect(self.changed); setattr(self, attribute, editor)
                self.rule_sections.insertTab(index, editor, caption); old.deleteLater()
            self.rule_sections.setCurrentIndex(selected_section)
            self.columns.fields = self.schema['view_options'].get('column_fields', [])
            self.columns.all_specs = {spec.key: spec for spec in view_columns_for(self.owner, self.columns.fields)}
            self.columns.set_layout(layout)
            for combo, _ in self.sort_controls:
                selected = combo.currentData(); combo.blockSignals(True)
                for field in self.schema['view_options'].get('ordering_fields', []):
                    index = combo.findData(field['field_id'])
                    if index < 0: combo.addItem(field['label'], field['field_id'])
                    else: combo.setItemText(index, field['label'])
                combo.setCurrentIndex(max(0, combo.findData(selected))); combo.blockSignals(False)
            for field in self.schema['view_options'].get('grouping_fields', []):
                if self.group.findData(field['field_id']) < 0: self.group.addItem(field['label'], field['field_id'])
            self._human_ready = True; self.preview_response = None; self.changed()
            self.message.information('Property saved. Both Include and Exclude drafts are retained; the property is available in Rules and Layout.')
        self.request('builder.schema', {'record_type': self.owner}, loaded)

    def changed(self, *_):
        if not self._human_ready: return
        super().changed()
        self.update_rule_hint()
        if self.preview_response is None:
            self.excluded_examples.clear(); self._preview_reasons = {}
            self.preview_reason.setText('Preview the current draft to see examples and reasons.')

    @staticmethod
    def readable_reason(value):
        if isinstance(value, str): return value
        if isinstance(value, dict):
            for key in ('reason', 'explanation', 'label', 'summary'):
                if value.get(key): return HumanViewEditor.readable_reason(value[key])
            children = value.get('conditions') or value.get('reasons') or value.get('children') or []
            if children: return '; '.join(HumanViewEditor.readable_reason(child) for child in children)
            state = value.get('state')
            if state: return str(state).replace('_', ' ').capitalize()
        if isinstance(value, list): return '; '.join(HumanViewEditor.readable_reason(item) for item in value)
        return ''

    def render_preview(self, response):
        super().render_preview(response)
        if not hasattr(self, 'excluded_examples'): return
        data = response['result']; self.excluded_examples.clear(); self._preview_reasons = {}
        for row in data.get('rows', []):
            rid = row.get('record_id') or (row.get('record') or {}).get('record_id')
            self._preview_reasons[rid] = self.readable_reason(row.get('view_match') or row.get('reason'))
        for example in data.get('excluded_examples', []):
            record = example.get('record') or example
            projected = display_row(record) if record.get('record_id') and record.get('record_type') else {}
            title = projected.get('label') or record.get('label') or record.get('title') or 'Record'
            reason = self.readable_reason(example.get('reason') or example.get('view_match')) or 'Excluded by the draft conditions.'
            item = QTreeWidgetItem([str(title), reason]); item.setData(0, ROLE, reason)
            item.setToolTip(0, str(title)); item.setToolTip(1, reason); self.excluded_examples.addTopLevelItem(item)
        self.examples.setTabText(1, 'Excluded' + (' · ' + str(data['excluded_count']) if 'excluded_count' in data else ''))
        if data.get('state', 'available') == 'available':
            count = data.get('total_count', len(data.get('rows', [])))
            parts = [f'{count:,} matching ' + ('conversations' if self.group.currentData() == '__combined__' and self.owner == 'Email' else 'records')]
            if 'matched_record_count' in data and data['matched_record_count'] != count: parts.append(f"{data['matched_record_count']:,} matching messages")
            if 'excluded_count' in data: parts.append(f"{data['excluded_count']:,} excluded")
            if data.get('unknown_count'): parts.append(f"{data['unknown_count']:,} could not be determined")
            self.preview_note.setText(' · '.join(parts) + ' · preview only')
        self.preview_reason.setText('Select a preview record to see its explanation.')

    def show_preview_reason(self):
        item = self.preview_table.tree.currentItem()
        if item:
            reason = self._preview_reasons.get(item.data(0, ROLE))
            self.preview_reason.setText(reason or 'The query returned this record as a match. No additional explanation was supplied.')

    def show_excluded_reason(self):
        item = self.excluded_examples.currentItem()
        if item: self.preview_reason.setText(item.data(0, ROLE) or '')

    def restore_factory(self):
        if self.busy or not self.factory_specification: return
        d = deepcopy(self.factory_specification)
        self._human_ready = False
        self.original_selection = self.query_part(d)
        self.name.setText(d.get('label') or d.get('name') or self.name.text())
        self.description.setText(d.get('description', '')); self.visible.setChecked(d.get('visible', True))
        self.order.setValue(d.get('order', 100)); self.include.setChecked(d.get('include_inactive', False))
        self.unknown.setChecked(d.get('unknown_policy', 'exclude') == 'include'); self.saved_search.setText(d.get('search', ''))
        self._group_fields = deepcopy(d.get('group_by_fields') or [])
        grouping = '__combined__' if self._group_fields else d.get('group_by')
        if self._group_fields and self.group.findData(grouping) < 0: self.group.addItem('Conversations' if self.owner == 'Email' else 'Saved field combination', grouping)
        self.group.setCurrentIndex(max(0, self.group.findData(grouping)))
        self.group_direction.setCurrentIndex(max(0, self.group_direction.findData(d.get('group_direction', 'asc'))))
        sorts = d.get('sorts') or [{'field': d.get('sort', 'updated_at'), 'direction': d.get('sort_direction', 'desc')}]
        for index, (combo, direction) in enumerate(self.sort_controls):
            selection = sorts[index] if index < len(sorts) else {'field': None, 'direction': 'asc'}
            combo.setCurrentIndex(max(0, combo.findData(selection['field'])))
            direction.setCurrentIndex(max(0, direction.findData(selection['direction'])))
        self._set_expression_parts(d.get('filter_expression') or {'all': d.get('filters', [])})
        self.columns.reset()
        self._initial_controls = signature(self.control_query())
        self._human_ready = True; self.changed(); self.editor_body.setCurrentIndex(1)
        self.message.information('Starter defaults are in your draft. Preview and save to apply them, or Cancel to keep the saved view.')

    def preview_is_current(self):
        try: current = signature(self.spec())
        except (ValueError, TypeError, KeyError) as error: self.message.setText(str(error)); return False
        if not self.preview_response or self.preview_semantics != current:
            self.message.setText('Preview the current draft, then choose Save.'); self.preview(); return False
        return True

    def save(self):
        self.save_view(copy=False)

    def save_as_new(self):
        self.save_view(copy=True)

    def save_view(self, *, copy=False):
        if self.busy or self.pending_save: return
        if not self.name.text().strip(): self.message.setText('Give this view a name.'); self.name.setFocus(); return
        if not self.preview_is_current(): return
        spec = self.spec(); layout = self.columns.value()
        if copy and self.builtin:
            self.message.setText('This specialized view has no editable definition to copy.'); return
        def finish():
            self.saved_presentation = layout
            try:
                if self.persist_presentation: self.persist_presentation(self.saved_id, self.owner, layout)
            except (OSError, ValueError):
                self.message.setText('The view is saved, but its layout could not be saved. Retry Save; the view will not be duplicated.'); return
            self.accept()
        if self.saved_id and self.query_committed == signature(spec): finish(); return
        def saved(response):
            self.saved_id = response['result'].get('definition_id') or (self.definition or {}).get('view_id')
            self.query_committed = signature(spec)
            self.definition = {**(self.definition or {}), **spec, 'view_id': self.saved_id}
            finish()
        if self.definition and not copy:
            operation, params = 'view.edit', {'view_id': self.definition['view_id'], 'changes': spec}
        else:
            operation, params = 'view.create', {'record_type': self.owner, 'specification': spec}
        self.request(operation, params, saved, mutation=True)

    def request_state_changed(self, busy, mutation):
        super().request_state_changed(busy, mutation)
        for name in ('save_new_button', 'restore_button'):
            control = getattr(self, name, None)
            if control: control.setEnabled(not busy)


class SavedViewsDialog(ManagedDialog):
    """Manage personal views without exposing registry/rule authoring."""
    def __init__(self, client, owner='Email', parent=None, *, get_presentation=None, persist_presentation=None, display_name=None):
        super().__init__(client, 'Saved views', parent); self.resize(740, 570)
        self.owner = owner; self.get_presentation = get_presentation; self.persist_presentation = persist_presentation
        host = parent
        while host is not None:
            if hasattr(host,'get_view_layout') and hasattr(host,'persist_view_layout'):
                if self.get_presentation is None: self.get_presentation = lambda vid,kind:host.get_view_layout(kind,vid)
                if self.persist_presentation is None: self.persist_presentation = lambda vid,kind,value:host.persist_view_layout(kind,vid,value)
                break
            host = host.parentWidget()
        self.schema = None; self.views = []; self.child_dialog = None
        self.layout_box.addWidget(label('Keep the views you use most. Standard views are already set up.'))
        self.search = QLineEdit(); self.search.setPlaceholderText('Find a view…'); self.search.setClearButtonEnabled(True); self.layout_box.addWidget(self.search)
        self.items = QListWidget(); self.items.setAccessibleName('Saved views'); self.layout_box.addWidget(self.items, 1)
        self.empty = label(''); self.layout_box.addWidget(self.empty)
        row = QHBoxLayout()
        self.new_button = button('New view', lambda:self.edit(new=True)); self.edit_button = button('Change view', self.edit)
        self.copy_button = button('Make a copy', self.duplicate); self.hide_button = button('Hide view', self.toggle_view)
        for item in (self.new_button,self.edit_button,self.copy_button,self.hide_button): row.addWidget(item)
        self.layout_box.addLayout(row); self.buttons(); self.items.itemActivated.connect(lambda _:self.edit()); self.items.currentItemChanged.connect(self.update_actions); self.search.textChanged.connect(self.filter)
        self.reload()
    def selected(self):
        item = self.items.currentItem(); return item.data(ROLE) if item else None
    def update_actions(self, *_):
        selected = self.selected(); owned = bool(selected and selected['view_id'].startswith('view:user:'))
        self.new_button.setEnabled(bool(self.schema) and not self.busy); self.edit_button.setEnabled(bool(selected) and not self.busy)
        self.copy_button.setEnabled(owned and not self.busy); self.hide_button.setEnabled(owned and not self.busy)
        self.hide_button.setText('Show view' if selected and selected.get('state') == 'disabled' else 'Hide view')
    def reload(self):
        self.items.clear(); self.schema = None; self.update_actions()
        def schema_loaded(response):
            self.schema = response['result']; self.revision = response['workspace_revision']
            self.request('registry.views', {'record_type':self.owner}, views_loaded)
        def views_loaded(response):
            if response['workspace_revision'] != self.revision:
                self.message.setText('Your saved views changed while loading. Close and reopen Saved views.'); self.schema = None; self.update_actions(); return
            self.views = [v for v in response['result'].get('views',[]) if v.get('record_type',v.get('owner_type')) == self.owner and v.get('state') != 'retired'
                          and (v['view_id'].startswith('view:user:') or not v.get('parameter_schema'))]
            for v in self.views:
                name = v.get('display_label') or v.get('label') or v.get('name') or 'Saved view'
                suffix = ' · hidden' if v.get('state') == 'disabled' else (' · standard' if not v['view_id'].startswith('view:user:') else '')
                item = QListWidgetItem(name+suffix); item.setData(ROLE,v); self.items.addItem(item)
            self.empty.setText('No saved views yet. Your normal collection is ready to use.' if not self.views else '')
            self.items.setCurrentRow(0); self.filter(); self.update_actions()
        self.request('builder.schema', {'record_type':self.owner}, schema_loaded)
    def filter(self, *_):
        query = self.search.text().strip().casefold()
        for i in range(self.items.count()): self.items.item(i).setHidden(query not in self.items.item(i).text().casefold())
    def edit(self, *_ , new=False, duplicate=False):
        if not self.schema or self.busy: return
        if self.child_dialog: self.child_dialog.raise_(); return
        definition = None if new else self.selected()
        if not new and not definition: return
        layout = self.get_presentation((definition or {}).get('view_id'),self.owner) if self.get_presentation else None
        dialog = HumanViewEditor(self.client,self.owner,self.schema,definition,self,presentation=layout,persist_presentation=self.persist_presentation,duplicate=duplicate)
        dialog.revision = self.revision; self.child_dialog = dialog
        def finished(result):
            self.child_dialog = None
            if result: self.reload()
            dialog.deleteLater()
        dialog.finished.connect(finished); dialog.open()
    def duplicate(self): self.edit(duplicate=True)
    def toggle_view(self):
        selected = self.selected()
        if not selected or not selected['view_id'].startswith('view:user:'): return
        operation = 'enable' if selected.get('state') == 'disabled' else 'disable'
        self.request('view.'+operation, {'view_id':selected['view_id']}, lambda _:self.reload(), mutation=True)
