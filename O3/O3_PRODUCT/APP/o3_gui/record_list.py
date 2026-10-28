"""Shared native card rows. Membership, sort order and every write belong to KM.

The surface presents an already ordered page and emits explicit intents. It never
filters, reorders, evaluates rules, or changes records. One expanded child is
allocated for reading; ordinary rows remain lightweight model items.
"""
from __future__ import annotations

import copy
import json
import math
from PySide6.QtCore import (Qt, Signal, QSize, QRectF, QPoint, QMimeData, QTimer,
                            QVariantAnimation, QEasingCurve)
from PySide6.QtGui import (QColor, QPainter, QPen, QFont, QFontMetrics, QDrag,
                           QKeySequence, QTextCursor, QTextBlockFormat)
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QTreeWidget, QTreeWidgetItem, QStyledItemDelegate, QStyle, QAbstractItemView,
    QHeaderView, QLabel, QPushButton, QTextBrowser, QFrame, QMenu, QSizePolicy, QGridLayout)

ROLE = int(Qt.ItemDataRole.UserRole)
ROW = ROLE + 20
DETAIL = ROLE + 21
GROUP = ROLE + 22
MIME = 'application/x-o3-records'


def _tokens():
    app = QApplication.instance()
    value = app.property('o3_tokens') if app else None
    if isinstance(value, dict):
        return value
    from .presentation import theme_tokens
    return theme_tokens()


def _literal(value):
    if value is None:
        return 'Not supplied'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, dict):
        return str(value.get('label', value.get('text', value.get('value', 'Not supplied'))))
    if isinstance(value, (list, tuple)):
        return ' · '.join(_literal(x) for x in value) if value else 'Not set'
    return str(value) if str(value).strip() else 'Empty'


def _tooltip(value):
    return str(value['tooltip']) if isinstance(value,dict) and value.get('tooltip') else _literal(value)


def _scale():
    app = QApplication.instance()
    appearance = app.property('o3_appearance') if app else None
    return float((appearance or {}).get('text_scale', 1.0)) if isinstance(appearance, dict) else 1.0


class _FullHeightBrowser(QTextBrowser):
    """Selectable text that shares the list's scroll surface, without truncation."""
    height_changed = Signal()
    escape_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self._fitting = False

    def _fit(self, *_):
        if self._fitting:
            return
        self._fitting = True
        try:
            self.document().setTextWidth(max(40, self.viewport().width()))
            height = max(42, math.ceil(self.document().size().height()) + 8)
            if height != self.height():
                self.setFixedHeight(height)
                self.height_changed.emit()
            # Qt may retain a range from its pre-layout viewport while an
            # expanded item is resized. All document content now fits here.
            self.verticalScrollBar().setRange(0, 0)
        finally:
            self._fitting = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.escape_requested.emit();event.accept();return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        # Let the parent tree perform the single scrolling interaction.
        event.ignore()


class _InlineDetail(QFrame):
    resized = Signal()
    action = Signal(str, object)
    collapse_requested = Signal()

    def __init__(self, payload, parent=None):
        super().__init__(parent)
        self.setObjectName('o3InlineDetail')
        self._responsive_grids=[]
        self._responsive_columns=0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 14, 28, 24)
        layout.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(10)
        label = QLabel(str(payload.get('title') or 'Reading'))
        label.setProperty('role', 'section')
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        top.addWidget(label, 1)
        collapse = QPushButton('⌃  Collapse')
        collapse.setProperty('kind', 'link')
        collapse.setAccessibleName('Collapse expanded item')
        collapse.clicked.connect(self.collapse_requested)
        top.addWidget(collapse)
        layout.addLayout(top)
        for field in ('subtitle', 'notice'):
            if payload.get(field):
                label = QLabel(str(payload[field]))
                label.setProperty('role', 'muted')
                label.setWordWrap(True)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
                layout.addWidget(label)
        widgets=[value for value in payload.get('widgets',[]) if isinstance(value,QWidget)]
        if widgets:
            group=QWidget();group.setObjectName('o3InlineProperties');grid=QGridLayout(group);grid.setContentsMargins(0,0,0,0);grid.setHorizontalSpacing(12);grid.setVerticalSpacing(8)
            self._responsive_grids.append((grid,widgets));layout.addWidget(group)
        if payload.get('html') is not None or payload.get('body') is not None:
            self.browser = _FullHeightBrowser()
            self.browser.setAccessibleName('Expanded item content. Select and copy text normally.')
            if payload.get('html') is not None:
                self.browser.setHtml(str(payload['html']))
            else:
                self.browser.setPlainText(str(payload.get('body', '')))
            self.browser.height_changed.connect(self.resized)
            self.browser.escape_requested.connect(self.collapse_requested)
            self.browser.anchorClicked.connect(lambda url: self.action.emit('open_link', url.toString()))
            layout.addWidget(self.browser)
        actions = payload.get('actions') or []
        if actions:
            group=QWidget();group.setObjectName('o3InlineActions');grid=QGridLayout(group)
            grid.setContentsMargins(0,0,0,0);grid.setHorizontalSpacing(16);grid.setVerticalSpacing(3)
            buttons=[]
            for spec in actions:
                button = QPushButton(str(spec.get('label', spec.get('id', 'Open'))))
                button.setProperty('kind', 'link')
                button.setEnabled(spec.get('enabled', True) is not False)
                button.clicked.connect(lambda checked=False, value=dict(spec): self.action.emit(value.get('id', 'open'), value.get('payload')))
                buttons.append(button)
            self._responsive_grids.append((grid,buttons));layout.addWidget(group)
        self._reflow()
        self._refresh_style()

    def _reflow(self):
        columns=3 if self.width()>=round(850*_scale()) else 2
        if columns==self._responsive_columns:return
        self._responsive_columns=columns
        for grid,widgets in self._responsive_grids:
            while grid.count():grid.takeAt(0)
            for index,widget in enumerate(widgets):grid.addWidget(widget,index//columns,index%columns)
            for column in range(3):grid.setColumnStretch(column,1 if column<columns else 0)
        self.resized.emit()

    def resizeEvent(self,event):
        super().resizeEvent(event);self._reflow()

    def _refresh_style(self):
        c = _tokens()
        self.setStyleSheet('QFrame#o3InlineDetail { background: '+c['surface']+'; border: 1px solid '+c['border_soft']+'; border-radius: 10px; }'
                           'QFrame#o3InlineDetail QTextBrowser { background: transparent; border: 0; }'
                           'QWidget#o3InlineActions, QWidget#o3InlineProperties { background: transparent; }')
        if hasattr(self,'browser'):
            from .presentation import current_appearance,font_family
            appearance=current_appearance();font=QFont(font_family(appearance,reader=True))
            font.setPixelSize(round(15*appearance['text_scale']*appearance['reader_scale']))
            self.browser.document().setDefaultFont(font)
            saved=self.browser.textCursor();cursor=QTextCursor(self.browser.document());cursor.select(QTextCursor.SelectionType.Document)
            block=QTextBlockFormat();block.setLineHeight(appearance['line_spacing']*100,QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            cursor.mergeBlockFormat(block);self.browser.setTextCursor(saved);self.browser._fit()


class _CardDelegate(QStyledItemDelegate):
    def __init__(self, owner):
        super().__init__(owner.tree)
        self.owner = owner

    def sizeHint(self, option, index):
        if index.data(DETAIL):
            return QSize(80, self.owner._detail_height)
        if index.data(GROUP):
            return QSize(120,round(43*_scale()))
        row = self.owner.row((index.data(ROW) or {}).get('record_id')) or {}
        scale = _scale()
        base = {'compact': 61, 'balanced': 74, 'comfortable': 88}.get(self.owner.density, 74)
        if not (self.owner.secondary and row.get('subtitle')):
            base -= 10
        if row.get('notice'):
            base += 19
        return QSize(140, round(base * scale))

    def paint(self, painter, option, index):
        if index.data(DETAIL):
            return
        if index.data(GROUP):
            self.paint_group(painter,option,index);return
        owner = self.owner
        row = self.owner.row((index.data(ROW) or {}).get('record_id')) or {}
        if not row:
            return
        c, scale = _tokens(), _scale()
        rect = QRectF(option.rect)
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        rid = row['record_id']
        expanded = rid == owner.expanded_id
        dropping = rid == owner.tree.drop_target
        surface = c['selection'] if selected else c['hover'] if hover else c['surface']
        # Draw a single visual card across all native header sections. Qt clips
        # the delegate to each section, preserving proper selection and resizing.
        entire = QRectF(-owner.tree.horizontalScrollBar().value()+3, rect.y()+3,
                        max(owner.tree.header().length(), owner.tree.viewport().width())-6, rect.height()-6)
        painter.setPen(QPen(QColor(c['accent'] if dropping else c['border_soft']), 1.5 if dropping else .8))
        painter.setBrush(QColor(surface))
        painter.drawRoundedRect(entire, 9, 9)
        key = owner.columns[index.column()]['key']
        text_rect = rect.adjusted(14*scale, 8*scale, -14*scale, -8*scale)
        if key == owner.title_key:
            x = text_rect.x()
            cy = rect.center().y() - (9*scale if row.get('notice') else 0)
            painter.setPen(QPen(QColor(c['accent'] if expanded else c['muted']), 1.5))
            chevron_y = cy - (3 if expanded else 0)
            if expanded:
                painter.drawLine(QPoint(round(x),round(chevron_y+4)),QPoint(round(x+4),round(chevron_y)))
                painter.drawLine(QPoint(round(x+4),round(chevron_y)),QPoint(round(x+8),round(chevron_y+4)))
            else:
                painter.drawLine(QPoint(round(x),round(chevron_y-3)),QPoint(round(x+4),round(chevron_y+1)))
                painter.drawLine(QPoint(round(x+4),round(chevron_y+1)),QPoint(round(x+8),round(chevron_y-3)))
            text_rect.adjust(23*scale, 0, 0, 0)
            font = QFont(option.font)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor(c['body']))
            fm = QFontMetrics(font)
            subtitle = str(row.get('subtitle') or '') if owner.secondary else ''
            title_height = fm.height()+2
            title_y = cy - (title_height if subtitle else title_height/2)
            title = fm.elidedText(str(row.get('title') or 'Untitled item'), Qt.TextElideMode.ElideRight, max(10,round(text_rect.width())))
            painter.drawText(QRectF(text_rect.x(),title_y,text_rect.width(),title_height),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,title)
            if subtitle:
                font.setWeight(QFont.Weight.Normal)
                font.setPixelSize(max(11,round(13*scale)))
                painter.setFont(font)
                painter.setPen(QColor(c['muted']))
                subtitle = QFontMetrics(font).elidedText(subtitle,Qt.TextElideMode.ElideRight,max(10,round(text_rect.width())))
                painter.drawText(QRectF(text_rect.x(),cy+2,text_rect.width(),title_height),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,subtitle)
            if row.get('notice'):
                font.setPixelSize(max(10,round(12*scale)))
                painter.setFont(font)
                painter.setPen(QColor(c['warn']))
                notice = QFontMetrics(font).elidedText(str(row['notice']),Qt.TextElideMode.ElideRight,max(10,round(text_rect.width())))
                painter.drawText(QRectF(text_rect.x(),rect.bottom()-27*scale,text_rect.width(),19*scale),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,notice)
        else:
            value = (row.get('cells') or {}).get(key)
            label = _literal(value)
            font = QFont(option.font)
            font.setPixelSize(max(11,round(13*scale)))
            painter.setFont(font)
            metrics = QFontMetrics(font)
            label = metrics.elidedText(label,Qt.TextElideMode.ElideRight,max(10,round(text_rect.width())))
            chip = isinstance(value,dict) and (value.get('value_type') in ('enum','record_ref','record_ref_set','boolean') or value.get('chip'))
            if chip:
                pill = QRectF(text_rect.x(),rect.center().y()-13*scale,min(text_rect.width(),metrics.horizontalAdvance(label)+20*scale),26*scale)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(c['selection']))
                painter.drawRoundedRect(pill,6,6)
                painter.setPen(QColor(c['accent']))
                painter.drawText(pill.adjusted(9*scale,0,-6*scale,0),Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,label)
            else:
                painter.setPen(QColor(c['muted'] if value is None or label in ('Not supplied','Not set','Unavailable') else c['body']))
                painter.drawText(text_rect,Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,label)
        if dropping and key == owner.title_key:
            target = owner.drop_targets.get(rid,{})
            label = str(target.get('label') or 'Assign here')
            painter.setPen(QColor(c['accent']))
            painter.drawText(rect.adjusted(16,0,-12,-5),Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignBottom,label)
        if selected and owner.tree.hasFocus():
            painter.setPen(QPen(QColor(c['focus']),1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(entire.adjusted(1,1,-1,-1),8,8)
        painter.restore()


    def paint_group(self,painter,option,index):
        group=index.data(GROUP) or {};item=self.owner.tree.itemFromIndex(index)
        c,scale=_tokens(),_scale();rect=QRectF(option.rect)
        painter.save();painter.setClipRect(option.rect);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(rect,QColor(c['canvas']))
        cy=rect.center().y()+2*scale;x=rect.x()+14*scale
        painter.setPen(QPen(QColor(c['muted']),1.5))
        if item.isExpanded():
            painter.drawLine(QPoint(round(x),round(cy-3)),QPoint(round(x+4),round(cy+1)))
            painter.drawLine(QPoint(round(x+4),round(cy+1)),QPoint(round(x+8),round(cy-3)))
        else:
            painter.drawLine(QPoint(round(x+2),round(cy-5)),QPoint(round(x+6),round(cy-1)))
            painter.drawLine(QPoint(round(x+6),round(cy-1)),QPoint(round(x+2),round(cy+3)))
        font=QFont(option.font);font.setPixelSize(round(13*scale));font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font);painter.setPen(QColor(c['body']))
        label=str(group.get('label','Group'));count=str(group.get('count_label',''))
        metrics=QFontMetrics(font);count_width=QFontMetrics(font).horizontalAdvance(count)+32*scale
        label=metrics.elidedText(label,Qt.TextElideMode.ElideRight,max(20,round(rect.width()-count_width-55*scale)))
        painter.drawText(QRectF(x+23*scale,rect.y()+7*scale,rect.width()-count_width-45*scale,rect.height()-10*scale),Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,label)
        font.setWeight(QFont.Weight.Normal);painter.setFont(font);painter.setPen(QColor(c['muted']))
        count_x=x+23*scale+metrics.horizontalAdvance(label)+14*scale
        painter.drawText(QRectF(count_x,rect.y()+7*scale,max(30,rect.right()-count_x-12),rect.height()-10*scale),Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,count)
        if self.owner.tree.currentItem()==item and self.owner.tree.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush);painter.setPen(QPen(QColor(c['focus']),1))
            painter.drawRoundedRect(rect.adjusted(2,4,-2,-2),6,6)
        painter.restore()


class _RecordTree(QTreeWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.drop_target = None
        self._pressed = None
        self._dragged = False
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setExpandsOnDoubleClick(False)
        self.setUniformRowHeights(False)
        self.setAllColumnsShowFocus(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setIndentation(0)
        self.setSortingEnabled(False)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def mousePressEvent(self,event):
        self._dragged = False
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton:
            if item and not item.data(0,DETAIL) and item.isSelected():
                self._pressed = None
                return
        self._pressed = (event.position().toPoint(),item,event.modifiers()) if event.button()==Qt.MouseButton.LeftButton else None
        super().mousePressEvent(event)
        if item and item.data(0,GROUP) and event.button()==Qt.MouseButton.LeftButton:
            self.setCurrentItem(item,0,self.selectionModel().SelectionFlag.NoUpdate)

    def mouseReleaseEvent(self,event):
        pressed = self._pressed
        super().mouseReleaseEvent(event)
        self._pressed = None
        if event.button()!=Qt.MouseButton.LeftButton or not pressed or self._dragged:
            return
        start,item,modifiers = pressed
        if modifiers & (Qt.KeyboardModifier.ControlModifier|Qt.KeyboardModifier.ShiftModifier|Qt.KeyboardModifier.MetaModifier):
            return
        if (event.position().toPoint()-start).manhattanLength() >= QApplication.startDragDistance():
            return
        if item and item == self.itemAt(event.position().toPoint()) and item.data(0,GROUP):
            self.owner.toggle_group(item);return
        if item and item == self.itemAt(event.position().toPoint()) and not item.data(0,DETAIL):
            row = self.owner.row((item.data(0,ROW) or {}).get('record_id')) or {}
            column = self.columnAt(event.position().toPoint().x())
            key = self.owner.columns[column]['key'] if column>=0 else None
            value = (row.get('cells') or {}).get(key)
            if isinstance(value,dict) and value.get('editable') is True and value.get('property_id'):
                self.owner.action_requested.emit('edit_property',[row['record_id']],{'property_id':value['property_id']})
                return
            self.owner.toggle(row.get('record_id'))

    def mouseDoubleClickEvent(self,event):
        # A single disclosure gesture is sufficient; never navigate away or
        # toggle twice because a double click was delivered.
        self._pressed = None
        event.accept()

    def keyPressEvent(self,event):
        current=self.currentItem()
        if current and current.data(0,GROUP) and event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter,Qt.Key.Key_Space,Qt.Key.Key_Left,Qt.Key.Key_Right):
            expanded=False if event.key()==Qt.Key.Key_Left else True if event.key()==Qt.Key.Key_Right else not current.isExpanded()
            self.owner.toggle_group(current,expanded);event.accept();return
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter):
            item = self.currentItem()
            if item and not item.data(0,DETAIL):
                self.owner.toggle((item.data(0,ROW) or {}).get('record_id'))
            event.accept();return
        if event.key() == Qt.Key.Key_Escape and self.owner.expanded_id:
            self.owner.collapse();event.accept();return
        if event.key() == Qt.Key.Key_Menu or (event.key()==Qt.Key.Key_F10 and event.modifiers()&Qt.KeyboardModifier.ShiftModifier):
            item=self.currentItem()
            if item:
                self.owner._context(self.visualItemRect(item).center())
            event.accept();return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.selectAll();event.accept();return
        super().keyPressEvent(event)

    def startDrag(self,supportedActions):
        self._dragged=True
        rows=[self.owner.row(rid) for rid in self.owner.selected_ids()]
        refs=[{'record_id':r['record_id'],'record_type':r.get('record_type',r.get('kind')),'label':r.get('title','Selected item'),
               'group_record_ids':r.get('group_record_ids',[])} for r in rows if r]
        if not refs:return
        mime=QMimeData();mime.setData(MIME,json.dumps({'schema':1,'records':refs,'origin':'record-list'}).encode('utf-8'))
        mime.setText('Add shown message(s):\n'+'\n'.join(str(r.get('title','Record')) for r in rows) if all(r.get('record_type',r.get('kind'))=='Email' for r in rows) else '\n'.join(str(r.get('title','Record')) for r in rows))
        drag=QDrag(self);drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def _drop_payload(self,event):
        if not event.mimeData().hasFormat(MIME):return None
        raw=bytes(event.mimeData().data(MIME))
        if len(raw)>1024*1024:return None
        try:data=json.loads(raw)
        except (ValueError,UnicodeDecodeError):return None
        if not isinstance(data,dict) or data.get('schema')!=1:return None
        records=data.get('records')
        if not isinstance(records,list) or not 1<=len(records)<=1000:return None
        if any(not isinstance(x,dict) or not isinstance(x.get('record_id'),str) or len(x['record_id'])>256 for x in records):return None
        return data

    def dragEnterEvent(self,event):
        if self._drop_payload(event):event.acceptProposedAction()
        else:event.ignore()

    def dragMoveEvent(self,event):
        data=self._drop_payload(event);item=self.itemAt(event.position().toPoint())
        rid=(item.data(0,ROW) or {}).get('record_id') if item and not item.data(0,DETAIL) else None
        self.drop_target=rid if data and rid in self.owner.drop_targets and rid not in [x['record_id'] for x in data['records']] else None
        self.viewport().update()
        if self.drop_target:event.acceptProposedAction()
        else:event.ignore()

    def dragLeaveEvent(self,event):
        self.drop_target=None;self.viewport().update();super().dragLeaveEvent(event)

    def dropEvent(self,event):
        data=self._drop_payload(event);rid=self.drop_target
        self.drop_target=None;self.viewport().update()
        if data and rid in self.owner.drop_targets:
            self.owner.drop_requested.emit([x['record_id'] for x in data['records']],rid,{'records':data['records'],'target':dict(self.owner.drop_targets[rid])})
            event.setDropAction(Qt.DropAction.CopyAction);event.accept()
        else:event.ignore()


class RecordList(QWidget):
    """Aligned configurable card rows with asynchronous inline reading.

    ``set_rows`` accepts normalized dictionaries with record_id/title, optional
    subtitle/record_type/notice, and cells keyed by column key. Values may be
    strings or display descriptors (label/value_type/property_id/editable).
    ``set_detail`` returns False when its request no longer belongs to the
    expanded record, so late callbacks cannot overwrite a different reader.
    """
    detail_requested=Signal(str,int)
    context_requested=Signal(str,list,QPoint)
    sort_requested=Signal(str,str)
    layout_changed=Signal(dict)
    selection_changed=Signal(list)
    open_requested=Signal(str)
    action_requested=Signal(str,list,object)
    drop_requested=Signal(list,str,object)
    expansion_changed=Signal(object)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.columns=[];self.title_key='record';self._rows={};self._items={};self._preferred_widths={};self._groups=[]
        self.expanded_id=None;self._request_id=0;self._detail_item=None;self._detail_widget=None
        self._detail_height=1;self._detail_payload=None;self._sizing_detail=False;self._size_again=False;self._updating=False;self._sort_key=None;self._sort_direction='asc'
        self.density='balanced';self.secondary=True;self.drop_targets={};self._animate=True
        self._animation=QVariantAnimation(self);self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._animate_height)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        self.tree=_RecordTree(self);layout.addWidget(self.tree)
        self.tree.setAccessibleName('Items. Click or press Enter to expand. Use arrow keys to select, Control or Shift for multiple items, and Escape to collapse.')
        self.tree.setItemDelegate(_CardDelegate(self))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context)
        self.tree.itemSelectionChanged.connect(lambda:self.selection_changed.emit(self.selected_ids()))
        header=self.tree.header();header.setSectionsMovable(True);header.setSectionsClickable(True)
        header.setStretchLastSection(True);header.setMinimumSectionSize(80)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.sectionClicked.connect(self._sort)
        header.sectionMoved.connect(self._layout_changed)
        header.sectionResized.connect(self._section_resized)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        self.set_columns([{'key':'record','label':'Item','width':460,'sortable':True}])
        self.set_appearance()

    def set_columns(self,columns):
        cleaned=[];keys=set()
        for raw in columns or []:
            spec=dict(raw) if isinstance(raw,dict) else {'key':raw.key,'label':raw.label,'width':raw.width,'sortable':True}
            key=str(spec.get('key',''))
            if not key or key in keys:continue
            keys.add(key);spec['key']=key;spec.setdefault('label',key);spec.setdefault('width',180)
            cleaned.append(spec)
        if not cleaned:cleaned=[{'key':'record','label':'Item','width':460,'sortable':True}]
        requested_order=[column['key'] for column in cleaned]
        # Native full-width child/group spans use logical column zero. Keep
        # the required title there while allowing any visual header ordering.
        if 'record' in keys:cleaned=sorted(cleaned,key=lambda column:column['key']!='record')
        self._updating=True
        self._preferred_widths={column['key']:max(80,min(1600,int(column.get('width',180)))) for column in cleaned}
        self.columns=cleaned;self.title_key='record' if 'record' in keys else cleaned[0]['key']
        self.tree.setColumnCount(len(cleaned));self.tree.setHeaderLabels([str(c['label']) for c in cleaned])
        for i,c in enumerate(cleaned):self.tree.setColumnWidth(i,max(80,min(1600,int(c.get('width',180)))))
        actual=[column['key'] for column in cleaned];header=self.tree.header()
        for visual,key in enumerate(requested_order):header.moveSection(header.visualIndex(actual.index(key)),visual)
        self._updating=False
        self.set_sort(self._sort_key,self._sort_direction)

    def set_layout(self,value):
        if not isinstance(value,dict):return
        if 'columns' in value:
            self.set_density(value.get('density',self.density));self.secondary=value.get('secondary',True) is not False
            value=value['columns']
        header=self.tree.header();keys=[c['key'] for c in self.columns]
        self._updating=True
        for visual,key in enumerate(k for k in value.get('order',keys) if k in keys):
            header.moveSection(header.visualIndex(keys.index(key)),visual)
        visible=value.get('visible',keys);widths=value.get('widths',{})
        for index,key in enumerate(keys):
            self.tree.setColumnHidden(index,key not in visible and key!=self.title_key)
            width=widths.get(key,self.columns[index].get('width',180))
            if type(width)is int:
                self._preferred_widths[key]=max(80,min(1600,width));self.tree.setColumnWidth(index,self._preferred_widths[key])
        self._updating=False;self.tree.doItemsLayout();self._size_detail()

    def layout_state(self):
        header=self.tree.header();keys=[c['key'] for c in self.columns]
        order=[keys[header.logicalIndex(i)] for i in range(len(keys))]
        visible=[key for key in order if not self.tree.isColumnHidden(keys.index(key))]
        stretched=visible[-1] if visible else None
        return {'order':order,'visible':visible,
                'widths':{key:self._preferred_widths.get(key,self.tree.columnWidth(i)) if key==stretched else self.tree.columnWidth(i) for i,key in enumerate(keys)}}

    def set_sort(self,key,direction='asc'):
        self._sort_key=key;self._sort_direction='desc' if direction=='desc' else 'asc'
        keys=[c['key'] for c in self.columns];header=self.tree.header()
        header.setSortIndicatorShown(key in keys)
        if key in keys:header.setSortIndicator(keys.index(key),Qt.SortOrder.DescendingOrder if direction=='desc' else Qt.SortOrder.AscendingOrder)

    def _sort(self,index):
        if not 0<=index<len(self.columns):return
        column=self.columns[index]
        if column.get('sortable',True) is False:return
        key=column['key'];direction='desc' if self._sort_key==key and self._sort_direction=='asc' else 'asc'
        self.set_sort(key,direction);self.sort_requested.emit(key,direction)

    def set_rows(self,rows,columns=None,preserve_anchor=True):
        anchor=self.save_anchor() if preserve_anchor else None
        selected=set(self.selected_ids());expanded=self.expanded_id
        collapsed=set(self.collapsed_groups()) if preserve_anchor else set()
        detail_payload=copy.deepcopy(self._detail_payload) if isinstance(self._detail_payload,dict) else None
        if self._detail_widget is not None:self._detail_widget.hide()
        self._animation.stop();self._detail_widget=None;self._detail_item=None;self.expanded_id=None
        self._request_id+=1
        self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True);self.tree.clear();self._items={};self._rows={};self._groups=[]
        current_group=None;previous_group_key=None
        if columns is not None:self.set_columns(columns)
        for source in rows or []:
            if not isinstance(source,dict) or not isinstance(source.get('record_id'),str):continue
            row=dict(source);rid=row['record_id']
            if rid in self._rows:continue
            self._rows[rid]=row
            grouping=row.get('view_group')
            if isinstance(grouping,dict) and 'key' in grouping:
                group_key=str(grouping['key'])
                if current_group is None or group_key!=previous_group_key:
                    group={'key':group_key,'label':str(grouping.get('label') or 'Not set'),'total_count':grouping.get('total_count')}
                    current_group=QTreeWidgetItem([group['label']]+['']*(len(self.columns)-1));current_group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    self.tree.addTopLevelItem(current_group);current_group.setFirstColumnSpanned(True)
                    current_group.setData(0,GROUP,group);current_group.setData(0,Qt.ItemDataRole.AccessibleDescriptionRole,'Group. Press Enter to expand or collapse.')
                    self._groups.append(current_group);previous_group_key=group_key
            else:current_group=None;previous_group_key=None
            item=QTreeWidgetItem([str(row.get('title') or 'Untitled item') if c['key']==self.title_key else _literal((row.get('cells') or {}).get(c['key'])) for c in self.columns])
            item.setData(0,ROLE,rid)
            for i in range(len(self.columns)):
                # Keep large KM payloads in Python once. QVariant recursively
                # copying the same evidence tree into every column stalls Qt.
                item.setData(i,ROW,{'record_id':rid})
                item.setToolTip(i,'\n'.join(str(x) for x in (row.get('title'),row.get('subtitle'),row.get('notice')) if x) if self.columns[i]['key']==self.title_key else _tooltip((row.get('cells')or{}).get(self.columns[i]['key'])))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable|Qt.ItemFlag.ItemIsDragEnabled|Qt.ItemFlag.ItemIsDropEnabled)
            if current_group is not None:current_group.addChild(item)
            else:self.tree.addTopLevelItem(item)
            self._items[rid]=item
            if rid in selected:item.setSelected(True)
        for group_item in self._groups:
            group=dict(group_item.data(0,GROUP));shown=group_item.childCount();total=group.get('total_count')
            group['count_label']=str(shown)+' shown of '+str(total) if type(total)is int and total>shown else str(shown)
            group_item.setData(0,GROUP,group);group_item.setText(0,group['label']+' · '+group['count_label'])
            group_item.setExpanded(group['key'] not in collapsed)
        self.tree.blockSignals(False)
        if expanded in self._items and self._row_is_visible(self._items[expanded]):
            self.expanded_id=expanded
            self._attach_detail(_InlineDetail(detail_payload or {'title':'Refreshing item…','body':'Loading the saved content.'}),animate=False)
            self.detail_requested.emit(expanded,self._request_id)
        self.tree.doItemsLayout()
        if anchor:self.restore_anchor(anchor)
        elif not preserve_anchor:self.tree.verticalScrollBar().setValue(0)
        self.tree.setUpdatesEnabled(True)
        self.selection_changed.emit(self.selected_ids())

    def collapsed_groups(self):
        return list(dict.fromkeys(str(item.data(0,GROUP)['key']) for item in self._groups if not item.isExpanded()))

    def toggle_group(self,item,expanded=None):
        if not item or not item.data(0,GROUP):return
        expanded=not item.isExpanded() if expanded is None else bool(expanded)
        if not expanded and self.expanded_id and self._items.get(self.expanded_id) and self._items[self.expanded_id].parent()==item:
            self.collapse(restore=False)
        y=self.tree.visualItemRect(item).top();bar=self.tree.verticalScrollBar()
        item.setExpanded(expanded);self.tree.doItemsLayout()
        bar.setValue(bar.value()+self.tree.visualItemRect(item).top()-y);self.tree.viewport().update()

    def _row_is_visible(self,item):
        parent=item.parent()
        while parent:
            if not parent.isExpanded():return False
            parent=parent.parent()
        return not item.isHidden()

    def _expand_parents(self,item):
        parent=item.parent()
        while parent:parent.setExpanded(True);parent=parent.parent()

    def row(self,record_id):return self._rows.get(record_id)
    def rows(self):return list(self._rows.values())
    def selected_ids(self):return [(item.data(0,ROW) or {})['record_id'] for item in self.tree.selectedItems() if (item.data(0,ROW) or {}).get('record_id')]
    def select_record(self,record_id,scroll=False):
        item=self._items.get(record_id)
        if item:
            if scroll:self._expand_parents(item)
            self.tree.setCurrentItem(item)
            if scroll:self.tree.scrollToItem(item,QAbstractItemView.ScrollHint.EnsureVisible)
    def selected_record(self):
        item=self.tree.currentItem();return (item.data(0,ROW)or{}).get('record_id') if item else None

    def save_anchor(self,record_id=None):
        item=self._items.get(record_id) if record_id else None
        if item is None:
            for candidate in self._items.values():
                if not self._row_is_visible(candidate):continue
                rect=self.tree.visualItemRect(candidate)
                if rect.height()>0 and rect.bottom()>=0:item=candidate;break
        return {'record_id':(item.data(0,ROW)or{}).get('record_id') if item else None,
                'offset':self.tree.visualItemRect(item).top() if item else 0,
                'scroll':self.tree.verticalScrollBar().value(),'collapsed_groups':self.collapsed_groups()}

    def restore_anchor(self,anchor):
        if not isinstance(anchor,dict):return
        if isinstance(anchor.get('collapsed_groups'),list):
            collapsed={str(key) for key in anchor['collapsed_groups']}
            for group in self._groups:group.setExpanded(str(group.data(0,GROUP)['key']) not in collapsed)
        self.tree.doItemsLayout();item=self._items.get(anchor.get('record_id'))
        bar=self.tree.verticalScrollBar()
        if item and self._row_is_visible(item):bar.setValue(bar.value()+self.tree.visualItemRect(item).top()-int(anchor.get('offset',0)))
        else:bar.setValue(int(anchor.get('scroll',0)))

    def toggle(self,record_id):
        if not record_id or record_id not in self._items:return
        if self.expanded_id==record_id:self.collapse();return
        self._expand_parents(self._items[record_id])
        visible=self.tree.visualItemRect(self._items[record_id])
        if visible.bottom()<0 or visible.top()>self.tree.viewport().height():
            self.tree.scrollToItem(self._items[record_id],QAbstractItemView.ScrollHint.PositionAtTop)
        anchor=self.save_anchor(record_id)
        self.collapse(restore=False)
        self.expanded_id=record_id;self._request_id+=1
        loading=_InlineDetail({'title':'Opening item…','body':'Loading the saved content.'})
        self._attach_detail(loading,animate=True)
        self.restore_anchor(anchor)
        self.expansion_changed.emit(record_id)
        self.detail_requested.emit(record_id,self._request_id)

    def collapse(self,restore=True):
        if not self.expanded_id:return
        rid=self.expanded_id;anchor=self.save_anchor(rid)
        self._animation.stop();self._request_id+=1
        parent=self._items.get(rid)
        if self._detail_widget is not None:
            self._detail_widget.hide();self.tree.removeItemWidget(self._detail_item,0);self._detail_widget.deleteLater()
        if parent and self._detail_item:parent.removeChild(self._detail_item)
        self._detail_item=None;self._detail_widget=None;self.expanded_id=None;self._detail_height=1;self._detail_payload=None
        if parent:parent.setExpanded(False)
        self.tree.doItemsLayout()
        if restore:
            self.restore_anchor(anchor);self.tree.setFocus(Qt.FocusReason.OtherFocusReason)
        self.tree.viewport().update();self.expansion_changed.emit(None)

    def refresh_detail(self):
        """Refresh the currently disclosed record after a new authoritative page."""
        if self.expanded_id:
            self._request_id+=1
            self.detail_requested.emit(self.expanded_id,self._request_id)

    def set_detail(self,record_id,content,request_id=None):
        if record_id!=self.expanded_id or (request_id is not None and request_id!=self._request_id):
            return False
        anchor=self.save_anchor(record_id)
        self._animation.stop()
        if self._detail_widget is not None:
            self._detail_widget.hide();self.tree.removeItemWidget(self._detail_item,0);self._detail_widget.deleteLater()
        parent=self._items.get(record_id)
        if not parent:return False
        if self._detail_item:parent.removeChild(self._detail_item)
        self._detail_item=None
        self._detail_payload={key:copy.deepcopy(value) for key,value in content.items() if key!='widgets'} if isinstance(content,dict) else None
        widget=content if isinstance(content,QWidget) else _InlineDetail(content if isinstance(content,dict) else {'body':str(content)})
        self._attach_detail(widget,animate=False);self.restore_anchor(anchor)
        return True

    def _attach_detail(self,widget,animate=False):
        parent=self._items.get(self.expanded_id)
        if parent is None:return
        item=QTreeWidgetItem(['']*len(self.columns));item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        for i in range(len(self.columns)):item.setData(i,DETAIL,True)
        parent.addChild(item);item.setFirstColumnSpanned(True)
        self._detail_item=item;self._detail_widget=widget
        if isinstance(widget,_InlineDetail):
            widget.collapse_requested.connect(self.collapse)
            widget.action.connect(lambda action,payload:self.action_requested.emit(action,[self.expanded_id] if self.expanded_id else [],payload))
            widget.resized.connect(self._size_detail)
        parent.setExpanded(True);self.tree.setItemWidget(item,0,widget);widget.show()
        self._size_detail(animate=animate)
        QTimer.singleShot(0,self._size_detail)

    def _size_detail(self,*_,animate=False):
        if self._detail_widget is None or self._detail_item is None:return
        if self._sizing_detail:
            self._size_again=True
            return
        self._sizing_detail=True
        self._size_again=False
        try:
            # QTreeWidget owns the child width. Forcing a competing width here
            # produces an 8px resize oscillation when fonts/themes change.
            width=max(100,self._detail_widget.width())
            layout=self._detail_widget.layout()
            if layout:layout.activate()
            hint=self._detail_widget.sizeHint().height()
            if self._detail_widget.hasHeightForWidth():hint=max(hint,self._detail_widget.heightForWidth(width))
            height=max(90,hint+8)
            if animate and self._animate and height<1000:
                self._animation.setStartValue(1);self._animation.setEndValue(height);self._animation.start()
            elif self._animation.state()==QVariantAnimation.State.Running:self._animation.setEndValue(height)
            else:self._apply_height(height)
        finally:
            self._sizing_detail=False
        if self._size_again:
            self._size_again=False
            QTimer.singleShot(0,self._size_detail)

    def _animate_height(self,value):self._apply_height(round(value))
    def _apply_height(self,height):
        if self._detail_item is None:return
        height=max(1,height)
        if height==self._detail_height and self._detail_item.sizeHint(0).height()==height:return
        self._detail_height=height;self._detail_item.setSizeHint(0,QSize(80,self._detail_height))
        self.tree.doItemsLayout()

    def _context(self,pos):
        item=self.tree.itemAt(pos)
        if item is None or item.data(0,DETAIL) or item.data(0,GROUP):return
        row=item.data(0,ROW)or{};rid=row.get('record_id')
        if not rid:return
        if not item.isSelected():self.tree.clearSelection();item.setSelected(True)
        self.context_requested.emit(rid,self.selected_ids(),self.tree.viewport().mapToGlobal(pos))

    def _header_menu(self,pos):
        menu=QMenu(self)
        for index,column in enumerate(self.columns):
            action=menu.addAction(str(column['label']));action.setCheckable(True)
            action.setChecked(not self.tree.isColumnHidden(index));action.setEnabled(column['key']!=self.title_key)
            action.toggled.connect(lambda checked,i=index:self._visibility(i,checked))
        menu.addSeparator();menu.addAction('Configure view…',lambda:self.action_requested.emit('configure_view',self.selected_ids(),{'section':'columns'}))
        menu.exec(self.tree.header().mapToGlobal(pos))

    def _visibility(self,index,visible):
        self.tree.setColumnHidden(index,not visible);self._layout_changed()
    def _layout_changed(self,*_):
        if not self._updating:self.layout_changed.emit(self.layout_state())
    def _section_resized(self,index,old,new):
        # Stretching to fill a different window size is not a saved user width.
        # Retaining the stretched width would force horizontal scrolling when
        # the same view is later opened in a smaller window.
        if (not self._updating and 0<=index<len(self.columns) and self.tree.header().underMouse()
                and QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            self._preferred_widths[self.columns[index]['key']]=new;self._layout_changed()
        self._size_detail()
    def set_drop_targets(self,targets):self.drop_targets=dict(targets) if isinstance(targets,dict) else {}
    def set_density(self,value):
        if value=='inherit':value=(QApplication.instance().property('o3_appearance')or{}).get('density','balanced')
        self.density=value if value in ('compact','balanced','comfortable') else 'balanced'
        self.tree.doItemsLayout();self.tree.viewport().update()
    def set_appearance(self,appearance=None):
        appearance=appearance or QApplication.instance().property('o3_appearance') or {}
        self.set_density(appearance.get('density',self.density));self._animate=appearance.get('reduce_motion') is not True
        c=_tokens();scale=_scale()
        self.tree.setStyleSheet('QTreeWidget { background: transparent; border: 0; outline: 0; } QTreeWidget::item { border: 0; padding: 0; } '
            'QHeaderView { background: transparent; } QHeaderView::section { background: '+c['canvas']+'; color: '+c['muted']+'; border: 0; border-bottom: 1px solid '+c['border_soft']+'; padding: '+str(round(10*scale))+'px 14px; font-size: '+str(round(12*scale))+'px; font-weight: 500; }')
        if isinstance(self._detail_widget,_InlineDetail):self._detail_widget._refresh_style()
        self.tree.doItemsLayout();self._size_detail();self.tree.viewport().update()
    def resizeEvent(self,event):
        super().resizeEvent(event)
        QTimer.singleShot(0,self._size_detail)
