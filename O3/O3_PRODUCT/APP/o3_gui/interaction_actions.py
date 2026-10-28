"""Shared interaction routes for explicit record selections.

Widgets describe an intent. KM validates and commits the operation. No SQL,
classification, relationship inference, or second property store lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable

from PySide6.QtCore import QObject, Signal, Qt, QEvent
from PySide6.QtWidgets import QDialog, QMenu, QToolButton, QPushButton, QSizePolicy

RECORD_MIME = 'application/x-o3-records'


@dataclass(frozen=True)
class RecordRef:
    record_type: str
    record_id: str
    label: str = ''

    def payload(self):
        return {'record_type': self.record_type, 'record_id': self.record_id}


@dataclass(frozen=True)
class SelectionContext:
    """A captured selection, never the currently open detail behind a menu."""
    records: tuple[RecordRef, ...]
    view_id: str = ''
    revision: int | None = None
    scope_note: str = ''

    @classmethod
    def from_rows(cls, rows: Iterable[dict], view_id='', revision=None):
        records = []
        seen = set(); grouped = False
        for row in rows:
            if isinstance(row, RecordRef):
                ref = row
            elif isinstance(row, dict):
                group_ids = row.get('group_record_ids') or row.get('anchor_record_ids') or []
                rid = row.get('record_id') or row.get('id')
                kind = row.get('record_type') or row.get('target_kind') or row.get('kind')
                grouped = grouped or (kind == 'Email' and isinstance(group_ids, (list, tuple)) and len(group_ids) > 1)
                if not isinstance(rid, str) or not rid or not isinstance(kind, str) or not kind:
                    continue
                ref = RecordRef(kind, rid, str(row.get('label') or row.get('title') or ''))
            else:
                continue
            key = (ref.record_type, ref.record_id)
            if key not in seen:
                seen.add(key); records.append(ref)
        return cls(tuple(records), str(view_id or ''), revision,
                   'Applies to the shown message in each selected conversation.' if grouped else '')

    @property
    def count(self):
        return len(self.records)

    @property
    def record_type(self):
        kinds = {item.record_type for item in self.records}
        return next(iter(kinds)) if len(kinds) == 1 else None

    @property
    def item_nouns(self):
        return {'Email':('message','messages'), 'Person':('person','people'),
                'Document':('document','documents'), 'Event':('event','events'),
                'LineOfEffort':('project','projects'), 'Organization':('company','companies'),
                'WorkspaceOrganization':('company','companies')}.get(self.record_type,('item','items'))

    @property
    def scope_label(self):
        if self.count == 1:
            return self.records[0].label or 'Selected ' + self.item_nouns[0]
        return f'{self.count:,} selected ' + self.item_nouns[1]

    def payload(self):
        return [ref.payload() for ref in self.records]


def records_from_mime(mime):
    """Decode only explicit O3 record drags; never infer IDs from plain text."""
    if not mime.hasFormat(RECORD_MIME):
        return ()
    raw = bytes(mime.data(RECORD_MIME))
    if len(raw) > 1024 * 1024:
        return ()
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return ()
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('records'), list):
        return ()
    if not 1 <= len(data['records']) <= 5000:
        return ()
    return SelectionContext.from_rows(data['records']).records


def property_presentation(definition, result):
    """Presentation only: retain KM state and the source/manual distinction."""
    definition = definition or {}; result = result or {}
    state = result.get('state', 'not_set')
    value = result.get('value')
    label = definition.get('label') or definition.get('display_label') or 'Property'
    kind = definition.get('value_type', 'text')
    mode = definition.get('mode', 'source')
    editable = mode == 'manual' and definition.get('state', 'enabled') == 'enabled'
    state_labels = {'not_set': 'Not set', 'unset': 'Not set', 'missing': 'Not supplied',
                    'unavailable': 'Unavailable', 'unknown': 'Unknown',
                    'not_applicable': 'Not applicable', 'disabled': 'Disabled',
                    'retired': 'Retired', 'error': 'Unavailable',
                    'not_evaluable': 'Unable to determine', 'source_stale': 'Source unavailable'}
    if state in state_labels:
        text = state_labels[state]
    elif value is None:
        text = 'Not set' if mode == 'manual' else 'Not supplied'
    elif isinstance(value, bool):
        text = 'Yes' if value else 'No'
    elif isinstance(value, dict):
        text = value.get('label') or value.get('display_name') or 'Linked item'
    elif isinstance(value, list):
        if not value:
            text = 'No linked items' if kind.startswith('record_ref') else 'No values'
        else:
            text = ', '.join(str(item.get('label') or item.get('display_name') or 'Linked item')
                             if isinstance(item, dict) else str(item) for item in value)
    else:
        text = str(value) if str(value).strip() else 'Empty'
    authority = 'Entered by you' if mode == 'manual' else 'Calculated from a rule' if mode == 'computed' else 'From your files'
    return {'label': label, 'text': text, 'state': state, 'value_type': kind,
            'editable': editable, 'authority': authority, 'reason': result.get('reason'),
            'property_id': definition.get('property_id') or definition.get('definition_id'),
            'value': value}


class TypedPropertyChip(QToolButton):
    """Same typed visual control for a value, tag or user-authored reference.

    Activation requests editing for manual values or evidence for read-only
    values. Optional drop semantics are explicitly supplied by the host.
    """
    activated = Signal(str, bool)
    records_dropped = Signal(object, str)

    def __init__(self, definition, result, parent=None):
        super().__init__(parent)
        self._chip_ready = False; self._restyling = False; self._drop_active = False
        self.definition = dict(definition or {})
        self.result = dict(result or {})
        self.drop_label = ''; self.drop_operation = None; self.drop_record_type = self.definition.get('owner_type')
        self.setObjectName('propertyChip')
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self._activated)
        self.refresh(); self._chip_ready = True

    def refresh(self):
        self.presentation = property_presentation(self.definition, self.result)
        data = self.presentation
        full_text = data['label'] + ': ' + data['text']
        self.setText(full_text if len(full_text) <= 76 else full_text[:75] + '…')
        self.setMaximumWidth(440)
        action = 'Edit value' if data['editable'] else 'Inspect supporting information'
        self.setToolTip(full_text + '\n' + data['authority'] + '\n' + action +
                        ('\n' + str(data['reason']) if data['reason'] else ''))
        self.setAccessibleName(self.text() + '. ' + data['authority'] + '. ' + action)
        self.setProperty('valueType', data['value_type'])
        self.setProperty('valueState', data['state'])
        self.setProperty('editableValue', data['editable'])
        self._paint_drop(False)

    def _activated(self):
        self.activated.emit(str(self.presentation.get('property_id') or ''), self.presentation['editable'])

    def set_drop_action(self, label, operation='add'):
        """Enable a host-selected meaningful drop. KM still validates it."""
        self.drop_label = str(label or '')
        self.drop_operation = operation if operation in ('set', 'add', 'remove', 'clear') else None
        self.setAcceptDrops(bool(self.presentation['editable'] and self.drop_label and self.drop_operation))

    def _paint_drop(self, active):
        # Re-read authoritative theme tokens. A literal palette color captured
        # at creation becomes stale when an already expanded item changes theme.
        self._drop_active = active
        if self._restyling: return
        from .presentation import current_tokens
        tokens = current_tokens()
        edge = tokens['accent'] if active else tokens['border']
        style = (f"QToolButton#propertyChip {{ border: 1px solid {edge}; border-radius: 10px; "
                 f"padding: 5px 10px; background: {tokens['surface']}; color: {tokens['body']}; }}"
                 f"QToolButton#propertyChip:hover {{ background: {tokens['hover']}; }}"
                 f"QToolButton#propertyChip:focus {{ border: 2px solid {tokens['focus']}; }}")
        if style == self.styleSheet(): return
        self._restyling = True
        try: self.setStyleSheet(style)
        finally: self._restyling = False

    def changeEvent(self, event):
        super().changeEvent(event)
        if (getattr(self, '_chip_ready', False) and not getattr(self, '_restyling', False)
                and event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
                                     QEvent.Type.StyleChange, QEvent.Type.FontChange)):
            self._paint_drop(self._drop_active)

    def _valid_drop(self, records):
        return bool(records and len({ref.record_type for ref in records}) == 1 and
                    (not self.drop_record_type or all(ref.record_type == self.drop_record_type for ref in records)))

    def dragEnterEvent(self, event):
        if self.acceptDrops() and self._valid_drop(records_from_mime(event.mimeData())):
            self.setText(self.drop_label); self._paint_drop(True); event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.refresh(); event.accept()

    def dropEvent(self, event):
        records = records_from_mime(event.mimeData())
        if self.acceptDrops() and self._valid_drop(records):
            self.records_dropped.emit(records, self.drop_operation)
            event.acceptProposedAction()
        else:
            event.ignore()
        self.refresh()


class RecordActionController(QObject):
    """One dispatch path for context, direct, keyboard and drop actions."""
    status = Signal(str, bool)
    changed = Signal(object)
    open_requested = Signal(object)
    exclude_requested = Signal(object)
    configure_requested = Signal(object)
    explain_requested = Signal(object, str)
    undo_available = Signal(bool)

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.window = parent
        self._undo_token = None
        self._busy = False

    def context(self, rows, view_id='', revision=None):
        return SelectionContext.from_rows(rows, view_id, self.client.revision if revision is None else revision)

    def create_menu(self, context, parent=None, *, allow_exclude=True):
        """Every callback closes over the captured target, including right-click."""
        menu = QMenu(parent or self.window)
        heading = menu.addAction(context.scope_label); heading.setEnabled(False)
        if context.scope_note:
            note = menu.addAction(context.scope_note); note.setEnabled(False)
        menu.addSeparator()
        if context.count == 1:
            menu.addAction('Open in full', lambda: self.open_requested.emit(context))
        editable = menu.addAction('Edit properties…', lambda: self.edit_properties(context))
        editable.setEnabled(bool(context.record_type))
        project = menu.addAction('Add to project…', lambda: self.choose_project(context))
        project.setEnabled(bool(context.record_type))
        if allow_exclude and context.view_id and context.count and context.record_type == 'Email':
            menu.addSeparator()
            menu.addAction("Don't show these here…", lambda: self.exclude_requested.emit(context))
        if context.view_id:
            menu.addAction('Configure this view…', lambda: self.configure_requested.emit(context))
        menu.addSeparator()
        undo = menu.addAction('Undo last property change', self.undo)
        undo.setEnabled(self._undo_token is not None and not self._busy)
        return menu

    menu = create_menu

    def edit_properties(self, context, property_id=None):
        if not context.count:
            return
        if not context.record_type:
            self.status.emit('Choose items from the same collection to edit a shared property.', True)
            return
        from .record_actions import SelectionProperties
        dialog = SelectionProperties(self.client, context, controller=self,
                                     property_id=property_id, parent=self.window)
        dialog.exec()

    def edit_inline(self, context, property_id, global_pos):
        """Open the shared typed control beside a chip/cell, preserving its scope."""
        if not context.count or not context.record_type or not property_id:
            self.status.emit('Choose a property on one collection to edit.', True); return None
        from .record_actions import PropertyPopover
        previous = getattr(self, '_inline_editor', None)
        if previous is not None and previous.isVisible():
            # Keep an existing draft rather than silently replacing it.
            previous.raise_(); previous.activateWindow()
            self.status.emit('Finish or cancel the open property edit first.', False)
            return previous
        editor = PropertyPopover(self.client, context, property_id, self, self.window)
        self._inline_editor = editor
        editor.show_at(global_pos)
        return editor

    def property_chip(self, context, definition, result, parent=None):
        """A bound, typed value with direct editing and an explicit tag drop."""
        from PySide6.QtGui import QCursor
        chip = TypedPropertyChip(definition, result, parent)
        chip.drop_record_type = context.record_type
        def activate(property_id, editable):
            if editable: self.edit_inline(context, property_id, QCursor.pos())
            else: self.explain_requested.emit(context, property_id)
        chip.activated.connect(activate)
        property_id = chip.presentation.get('property_id')
        if chip.presentation['editable'] and property_id:
            if definition.get('value_type') == 'boolean':
                # A tag is an ordinary true manual value, not a separate entity.
                chip.set_drop_action('Apply ' + chip.presentation['label'], 'set')
                chip.records_dropped.connect(lambda refs, _operation:
                    self.assign(SelectionContext(tuple(refs), revision=self.client.revision), property_id, True))
            def clear_value(): self.assign(context, property_id, operation='clear')
            chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            def menu_at(position):
                menu = QMenu(chip)
                menu.addAction('Edit value…', lambda: self.edit_inline(context, property_id, chip.mapToGlobal(position)))
                if definition.get('nullable') is not False and property_id != 'ws:display_name':
                    menu.addAction('Remove tag' if definition.get('value_type') == 'boolean' else 'Clear value', clear_value)
                menu.exec(chip.mapToGlobal(position))
            chip.customContextMenuRequested.connect(menu_at)
        return chip

    def explain_property(self, context, property_id):
        if context.count != 1:
            self.status.emit('Choose one item to inspect its supporting information.', True); return None
        from .record_actions import PropertyExplanation
        dialog = PropertyExplanation(self.client, context, property_id, self.window)
        self._explanation = dialog
        dialog.show()
        return dialog

    def choose_project(self, context):
        if not context.count or not context.record_type:
            return
        from .workspace_editor import RecordPicker
        picker = RecordPicker(self.client, ['LineOfEffort'], multiple=False, parent=self.window)
        picker.setWindowTitle('Add to project')
        if picker.exec() == QDialog.DialogCode.Accepted and picker.selected_records:
            self.associate_project(context, picker.selected_records[0])

    def associate_project(self, context, project):
        """Create/use an ordinary typed property in the same audited batch."""
        if not context.record_type:
            self.status.emit('Choose items from the same collection to add a shared project.', True)
            return
        if isinstance(project, RecordRef):
            project = project.payload()
        if not isinstance(project, dict) or project.get('record_type') != 'LineOfEffort':
            self.status.emit('Choose a project from O3.', True); return
        reference = {'record_type': 'LineOfEffort', 'record_id': project.get('record_id')}
        if not reference['record_id']:
            self.status.emit('The selected project is unavailable.', True); return
        definition = {'label': 'Projects', 'mode': 'manual', 'value_type': 'record_ref_set',
                      'ref_type': 'LineOfEffort'}
        self.assign(context, 'ws:user:projects-' + context.record_type.lower(), [reference],
                    operation='add', definition=definition, description='Project added')

    def assign(self, context, property_id, value=None, *, operation='set', definition=None,
               description=None, done=None):
        if self._busy:
            self.status.emit('The previous change is still saving.', True); return False
        if not context.count or context.count > 5000 or not context.record_type:
            self.status.emit('Select between 1 and 5,000 items from the same collection.', True); return False
        if operation not in ('set', 'clear', 'add', 'remove'):
            self.status.emit('This property operation is unavailable.', True); return False
        if not property_id:
            self.status.emit('Choose a property first.', True); return False
        params = {'records': context.payload(), 'property_id': property_id, 'operation': operation}
        if operation != 'clear': params['value'] = value
        if definition is not None: params['definition'] = dict(definition)
        command = 'property.batch'
        if property_id == 'ws:display_name':
            if context.count != 1 or context.record_type != 'WorkspaceOrganization' or operation != 'set':
                self.status.emit('A company name can be changed for one company at a time and cannot be cleared.', True)
                return False
            command = 'organization.rename'
            params = {'ws_org_id': context.records[0].record_id, 'display_name': value}
        self._busy = True
        self.status.emit('Saving ' + context.scope_label.lower() + '…', False)
        expected = context.revision if context.revision is not None else self.client.revision
        def received(response):
            self._busy = False
            if not response.get('ok'):
                error = response.get('error', {})
                message = ('The saved information changed. Your selection and draft are unchanged; review the latest values before trying again.'
                           if error.get('code') == 'REVISION_CONFLICT' else
                           error.get('message') or 'This change could not be saved. Nothing was changed.')
                self.status.emit(message, True)
            else:
                result = response.get('result', {})
                if result.get('undo_token'):
                    self._undo_token = result['undo_token']; self.undo_available.emit(True)
                elif result.get('changed'):
                    self._undo_token = None; self.undo_available.emit(False)
                count = result.get('affected_count', 1 if command == 'organization.rename' and result.get('changed') else len(result.get('affected_record_ids', [])))
                if count:
                    title = description or ('Value cleared' if operation == 'clear' else 'Properties updated')
                    self.status.emit(f'{title} · {count:,} ' + context.item_nouns[0 if count == 1 else 1], False)
                else:
                    self.status.emit('Already up to date. Nothing needed to change.', False)
                self.changed.emit(response)
            if done: done(response)
        try:
            self.client.request(command, params, received, expected=expected, owner=self)
        except (RuntimeError, ValueError) as exc:
            self._busy = False; self.status.emit(str(exc), True)
            if done: done({'ok': False, 'error': {'code': 'REQUEST_UNAVAILABLE', 'message': str(exc)}})
            return False
        return True

    def undo(self):
        if self._busy or self._undo_token is None:
            return
        token = self._undo_token; self._busy = True
        def received(response):
            self._busy = False
            if response.get('ok'):
                self._undo_token = None; self.undo_available.emit(False)
                self.status.emit('Property change undone.', False); self.changed.emit(response)
            else:
                self.status.emit(response.get('error', {}).get('message') or 'The change could not be undone.', True)
        try:
            self.client.request('property.undo', {'undo_token': token}, received,
                                expected=self.client.revision, owner=self)
        except (RuntimeError, ValueError) as exc:
            self._busy = False; self.status.emit(str(exc), True)


class ProjectDropButton(QPushButton):
    """A normal project navigation button with one explicit canonical drop."""
    def __init__(self, text, project, controller, parent=None):
        super().__init__(str(text), parent)
        self.project = dict(project)
        self.controller = controller
        self.normal_text = str(text)
        self.setAcceptDrops(self.project.get('record_type') == 'LineOfEffort' and bool(self.project.get('record_id')))
        self.setAccessibleName(self.normal_text + '. Open project; drop selected items to add them to this project.')
        self.setToolTip('Open project · Drag items here to add a Projects association')

    def _restore(self):
        self.setText(self.normal_text); self.setProperty('dropActive', False)
        self.style().unpolish(self); self.style().polish(self); self.update()

    def dragEnterEvent(self, event):
        refs = records_from_mime(event.mimeData())
        if self.acceptDrops() and refs and len({ref.record_type for ref in refs}) == 1:
            self.setText(self.fontMetrics().elidedText('Add to ' + self.normal_text, Qt.TextElideMode.ElideRight, max(40,self.width()-24)))
            self.setProperty('dropActive', True)
            self.style().unpolish(self); self.style().polish(self); self.update()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._restore(); event.accept()

    def dropEvent(self, event):
        refs = records_from_mime(event.mimeData())
        self._restore()
        if not self.acceptDrops() or not refs or len({ref.record_type for ref in refs}) != 1:
            event.ignore(); return
        context = SelectionContext(tuple(refs), revision=self.controller.client.revision)
        self.controller.associate_project(context, self.project)
        event.acceptProposedAction()
