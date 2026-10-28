"""A focus-first connection map with an equivalent, keyboard-accessible list.

The service owns relationships and their direction. This widget only arranges
them; it never infers a relationship or silently changes a bounded result.
"""
from PySide6.QtCore import Qt, QPointF, QTimer, QEvent
from PySide6.QtGui import QPainter, QPen, QPainterPath, QPalette, QFontMetrics, QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QSizePolicy
from .ui import text, btn, wrap_btn, rule
from .presentation import current_tokens


def _identity(edge):
    return tuple(edge.get(key, '') for key in ('source', 'predicate', 'target'))


def _wrapped_title(value, metrics, width, lines=3):
    """Deliberate truncation with an ellipsis; full title stays accessible."""
    words = str(value).split()
    result = []
    while words and len(result) < lines:
        if len(result) == lines - 1:
            result.append(metrics.elidedText(' '.join(words), Qt.ElideRight, width)); break
        line = ''
        while words and metrics.horizontalAdvance((line + ' ' + words[0]).strip()) <= width:
            line = (line + ' ' + words.pop(0)).strip()
        if not line and words:
            line = metrics.elidedText(words.pop(0), Qt.ElideRight, width)
        result.append(line)
    return '\n'.join(result) or str(value)


class Neighborhood(QWidget):
    def __init__(self, workspace):
        super().__init__()
        self.workspace = workspace
        self.seed = None; self.edges = []; self.records = {}; self.nodes = {}
        self.edge_buttons = []; self.active = None; self.mode = 'map'
        self.direct_total = 0; self._map_ids = []; self._load_serial = 0
        self._map_edge_indices = []; self.map_limit = 4
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(9)
        heading = text('Connections', 'title', True)
        heading.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(heading)
        self.subtitle = text('', 'muted', True); layout.addWidget(self.subtitle)
        controls = QHBoxLayout(); controls.setSpacing(6)
        self.map_button = btn('Map', lambda: self.set_mode('map'), 'link', 'secondary')
        self.list_button = btn('List', lambda: self.set_mode('list'), 'list', 'secondary')
        for control in (self.map_button, self.list_button):
            control.setCheckable(True); controls.addWidget(control)
        self.center_button = wrap_btn('Center on this item', self.center_on_seed, 'center', 'link')
        controls.addWidget(self.center_button); controls.addStretch()
        layout.addLayout(controls)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setAccessibleName('Connections around the selected item')
        self.canvas = MapCanvas(self); self.scroll.setWidget(self.canvas); layout.addWidget(self.scroll, 1)
        self.legend = text('Arrows show connection direction. Dashed line: added by you.', 'small', True)
        layout.addWidget(self.legend)
        layout.addWidget(rule())
        self.detail = text('Select a connection to see why it appears.', 'muted', True)
        self.detail.setAccessibleName('Selected connection explanation'); layout.addWidget(self.detail)
        actions = QHBoxLayout()
        self.evidence = wrap_btn('Why this connection?', self.show_evidence, kind='link')
        self.evidence.hide(); actions.addWidget(self.evidence)
        self.open_related = wrap_btn('Open connected item', self.open_active, kind='link')
        self.open_related.hide(); actions.addWidget(self.open_related); actions.addStretch()
        layout.addLayout(actions)

    def _title(self, key):
        return str(self.records.get(key, {}).get('title') or 'Untitled item')

    def _caption(self, edge):
        return str(edge.get('label') or edge.get('predicate', '').replace('_', ' '))

    def _description(self, edge):
        owner = 'Added by you' if edge.get('authority') != 'core' else 'Found in your saved material'
        return self._title(edge['source']) + ' → ' + self._caption(edge) + ' → ' + self._title(edge['target']) + '\n' + owner

    def load(self, data):
        self._load_serial += 1
        self.seed = data['seed']; self.active = None
        self.evidence.hide(); self.open_related.hide()
        self.detail.setText('Select a connection to see why it appears.')
        for widget in list(self.nodes.values()) + self.edge_buttons:
            widget.hide(); widget.deleteLater()
        self.nodes = {}; self.edge_buttons = []
        self.records = dict(data['nodes']); self._map_ids = list(self.records)
        self.edges = list(data['edges'])
        self._map_identities = {_identity(edge) for edge in data['edges']}
        seen = {_identity(edge) for edge in self.edges}
        # The seed detail carries the complete direct list. Context edges from
        # the bounded map stay separate and are labelled as such.
        for edge in self.records[self.seed].get('relationships', []):
            if _identity(edge) not in seen:
                self.edges.append(edge); seen.add(_identity(edge))
            other = edge.get('other')
            if other and other not in self.records:
                self.records[other] = dict(id=other, title=edge.get('other_label', 'Untitled item'),
                    kind=edge.get('other_type', ''), target_kind=edge.get('other_target_kind', ''))
        self.direct_total = int(data.get('direct_total', 0))
        for key in self._map_ids:
            record = self.records[key]
            button = wrap_btn(record['title'], lambda checked=False, rid=key: self.workspace.open_record(rid), kind='mapnode')
            button.setParent(self.canvas); button._full_title = record['title']
            kind = self.workspace.type_name(record.get('kind', ''))
            heading = ('This item · ' if key == self.seed else '') + kind
            button.layout().insertWidget(0, text(heading, 'small', True))
            button.layout().setDirection(QVBoxLayout.TopToBottom); button.layout().setSpacing(4)
            button._label.setProperty('role', 'section')
            button._label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            button.setProperty('selected', key == self.seed)
            button.setAccessibleName(('Current item: ' if key == self.seed else '') + record['title'])
            button.setAccessibleDescription('Press Enter to open. Use arrow keys to move between items.')
            button.installEventFilter(self); button.show(); self.nodes[key] = button
        for index, edge in enumerate(self.edges):
            caption = self._caption(edge)
            button = wrap_btn(caption, lambda checked=False, i=index: self.inspect(i), kind='edge')
            button.setParent(self.canvas)
            button.setAccessibleName(self._description(edge).replace('\n', '. '))
            button.setToolTip(self._description(edge)); button.installEventFilter(self)
            button.show(); self.edge_buttons.append(button)
        route = self.workspace.navigation.current
        self.mode = route.get('map_mode', 'map') if route.get('map_mode') in {'map', 'list'} else 'map'
        self.map_limit = max(4, min(24, route.get('map_limit', 4)))
        self.set_mode(self.mode, restore=True)
        for index, edge in enumerate(self.edges):
            if list(_identity(edge)) == route.get('map_edge'):
                self.inspect(index, reveal=False); break
        serial = self._load_serial
        def restore():
            if serial != self._load_serial: return
            self.canvas.arrange()
            # New visits start at the fully visible focal card. Existing routes
            # retain their vertical position; Center is always one action away.
            self.scroll.verticalScrollBar().setValue(route.get('map_y', 0))
        QTimer.singleShot(0, restore)

    def view_state(self):
        return {'map_mode': self.mode, 'map_limit': self.map_limit}

    def set_mode(self, mode, restore=False):
        if mode not in {'map', 'list'}: return
        self.mode = mode
        self.map_button.setChecked(mode == 'map'); self.list_button.setChecked(mode == 'list')
        self.legend.setVisible(mode == 'map')
        self.canvas.arrange()
        if not restore:
            self.scroll.verticalScrollBar().setValue(0)

    def center_on_seed(self):
        self.set_mode('map')
        if self.seed in self.nodes:
            self.scroll.ensureWidgetVisible(self.nodes[self.seed], 8, 8)
            self.nodes[self.seed].setFocus(Qt.ShortcutFocusReason)

    def inspect(self, index, reveal=True):
        if not 0 <= index < len(self.edges): return
        self.active = index; edge = self.edges[index]
        origin = 'Added by you. This does not change the original material.' if edge.get('authority') != 'core' else 'Found in your saved material.'
        shorten = lambda title: title if len(title) <= 48 else title[:47].rstrip() + '…'
        self.detail.setText(shorten(self._title(edge['source'])) + ' → ' +
            shorten(self._title(edge['target'])) + '\n' + self._caption(edge) + ' · ' + origin)
        self.detail.setToolTip(self._description(edge))
        self.evidence.show(); self.open_related.show(); self.canvas.update()
        for number, button in enumerate(self.edge_buttons):
            button.setProperty('active', number == index)
            button.style().unpolish(button); button.style().polish(button)
        if reveal and self.edge_buttons[index].isVisible():
            self.scroll.ensureWidgetVisible(self.edge_buttons[index], 12, 12)

    def open_active(self):
        if self.active is None: return
        edge = self.edges[self.active]
        self.workspace.open_record(edge['target'] if edge['target'] != self.seed else edge['source'])

    def show_evidence(self):
        if self.active is not None:
            self.workspace.show_connection(self.edges[self.active], self.records[self.seed])

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            widgets = ([self.nodes[key] for key in self._map_ids if key in self.nodes] if self.mode == 'map'
                       else self.edge_buttons)
            widgets = [widget for widget in widgets if widget.isVisible()]
            if obj in widgets:
                key = event.key()
                if key in (Qt.Key_Return, Qt.Key_Enter):
                    obj.click(); return True
                if key in (Qt.Key_Left, Qt.Key_Up, Qt.Key_Right, Qt.Key_Down):
                    delta = -1 if key in (Qt.Key_Left, Qt.Key_Up) else 1
                    target = widgets[(widgets.index(obj) + delta) % len(widgets)]
                    target.setFocus(Qt.TabFocusReason); self.scroll.ensureWidgetVisible(target, 12, 12)
                    return True
                if key == Qt.Key_Home:
                    self.center_on_seed(); return True
                if key == Qt.Key_End and widgets:
                    widgets[-1].setFocus(Qt.TabFocusReason); self.scroll.ensureWidgetVisible(widgets[-1], 12, 12)
                    return True
        return super().eventFilter(obj, event)


class MapCanvas(QWidget):
    def __init__(self, owner):
        super().__init__(); self.owner = owner; self.paths = []; self._arranging = False
        self.setMinimumWidth(0); self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

    def resizeEvent(self, event):
        super().resizeEvent(event); self.arrange()

    def _card_height(self, button, width, scale):
        inner = max(40, width - round(36 * scale))
        lines = 3
        if button is self.owner.nodes.get(self.owner.seed) and self.owner.scroll.viewport().height() < round(220 * scale):
            lines = 1 if self.owner.scroll.viewport().height() < round(130 * scale) else 2
        button._label.setText(_wrapped_title(button._full_title, button._label.fontMetrics(), inner, lines))
        return max(round(64 * scale), button.layout().heightForWidth(width) + round(8 * scale))

    def arrange(self):
        if self._arranging or not self.owner.seed or self.owner.seed not in self.owner.nodes: return
        self._arranging = True
        try:
            owner = self.owner; scale = max(1.0, float(getattr(owner.workspace, 'scale', 1.0)))
            width = max(120, self.width()); pad = min(round(22 * scale), width // 12)
            usable = width - 2 * pad; self.paths = []
            for button in owner.nodes.values(): button.setVisible(owner.mode == 'map')
            for button in owner.edge_buttons: button.hide()
            direct = sum(1 for edge in owner.edges if owner.seed in (edge['source'], edge['target']))
            context = len(owner.edges) - direct
            if owner.mode == 'list':
                y = pad
                for index, edge in enumerate(owner.edges):
                    button = owner.edge_buttons[index]
                    prefix = '' if owner.seed in (edge['source'], edge['target']) else 'Related context · '
                    button._label.setText(prefix + owner._description(edge))
                    button._label.setProperty('role', 'body')
                    height = max(round(82 * scale), button.layout().heightForWidth(usable) + round(16 * scale))
                    button.setGeometry(pad, y, usable, height); button.show(); y += height + round(8 * scale)
                self.setMinimumHeight(max(1, y + pad))
                owner.subtitle.setText(str(direct) + ' of ' + str(owner.direct_total) + ' direct connections' +
                    (' · ' + str(context) + ' related context' if context else '') +
                    (' · No connections found yet.' if not owner.edges else ''))
                return

            focal = owner.nodes[owner.seed]
            side_layout = owner.scroll.viewport().height() < round(340 * scale) and usable >= 480
            side_gap = round(52 * scale)
            focal_width = (usable - side_gap) // 2 if side_layout else min(usable, round(380 * scale))
            focal_height = self._card_height(focal, focal_width, scale)
            focal_y = pad + round(38 * scale) if side_layout else pad
            focal.setGeometry(pad if side_layout else (width - focal_width) // 2, focal_y, focal_width, focal_height)
            neighbors = [key for key in owner._map_ids if key != owner.seed]
            # Layout grows vertically, never as an off-screen fixed-size plane.
            # The chosen text size is retained at every viewport width.
            gap = round(20 * scale)
            columns = max(1, min(3, int((usable + gap) / (250 * scale + gap))))
            card_width = (usable - (columns - 1) * gap) // columns
            row_y = pad + focal_height + round(64 * scale)
            if side_layout:
                columns = 1; card_width = focal_width; row_y = focal_y
            for row in range(0, len(neighbors), columns):
                keys = neighbors[row:row + columns]
                heights = [self._card_height(owner.nodes[key], card_width, scale) for key in keys]
                for col, key in enumerate(keys):
                    x = pad + focal_width + side_gap if side_layout else pad + col * (card_width + gap)
                    owner.nodes[key].setGeometry(x, row_y, card_width, heights[col])
                row_y += max(heights) + round(64 * scale)
            self.setMinimumHeight(max(pad + focal_height + pad, row_y - round(40 * scale)))
            indices = []
            for index, edge in enumerate(owner.edges):
                if _identity(edge) not in owner._map_identities: continue
                if edge['source'] not in owner.nodes or edge['target'] not in owner.nodes: continue
                indices.append(index)
                source = owner.nodes[edge['source']].geometry(); target = owner.nodes[edge['target']].geometry()
                down = source.center().y() < target.center().y()
                start = QPointF(source.center().x(), source.bottom() if down else source.top())
                end = QPointF(target.center().x(), target.top() if down else target.bottom())
                turn_y = end.y() - round(42 * scale) if down else start.y() - round(42 * scale)
                # A gutter keeps long paths from cutting through intervening cards.
                if side_layout and owner.seed in (edge['source'], edge['target']):
                    right = source.center().x() < target.center().x()
                    start = QPointF(source.right() if right else source.left(), source.center().y())
                    end = QPointF(target.left() if right else target.right(), target.center().y())
                    middle = (start.x() + end.x()) / 2
                    points = [start, QPointF(middle, start.y()), QPointF(middle, end.y()), end]
                elif abs(source.center().y() - target.center().y()) > round(240 * scale):
                    gutter = max(5, pad // 3)
                    points = [start, QPointF(start.x(), start.y() + round(12 * scale)),
                              QPointF(gutter, start.y() + round(12 * scale)), QPointF(gutter, turn_y),
                              QPointF(end.x(), turn_y), end]
                else:
                    points = [start, QPointF(start.x(), turn_y), QPointF(end.x(), turn_y), end]
                self.paths.append((points, edge.get('authority') != 'core', index))
                neighbor = target if edge['target'] != owner.seed else source
                button = owner.edge_buttons[index]
                label = owner._caption(edge) + (' · by you' if edge.get('authority') != 'core' else '')
                chip_width = min(neighbor.width(), max(round(90 * scale), button._label.fontMetrics().horizontalAdvance(label) + round(34 * scale)))
                button._label.setText(_wrapped_title(label, button._label.fontMetrics(), max(40, chip_width - 20), 2))
                button._label.setProperty('role', 'small')
                chip_height = min(round(50 * scale), max(round(36 * scale), button.layout().heightForWidth(chip_width)))
                button.setGeometry(neighbor.center().x() - chip_width // 2, max(pad, neighbor.top() - chip_height - round(7 * scale)), chip_width, chip_height)
                button.show(); button.raise_()
            owner._map_edge_indices = indices
            shown = sum(1 for index in indices if owner.seed in (owner.edges[index]['source'], owner.edges[index]['target']))
            contextual = len(indices) - shown
            owner.subtitle.setText(str(shown) + ' of ' + str(owner.direct_total) + ' direct connections shown' +
                (' · ' + str(contextual) + ' related context' if contextual else '') +
                (' · List shows every available connection.' if direct > shown else ''))
        finally:
            self._arranging = False; self.update()

    def paintEvent(self, event):
        if self.owner.mode != 'map': return
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        colors = current_tokens()
        for points, user, index in self.paths:
            color = QColor(colors['accent'] if self.owner.active == index else colors['edge'])
            painter.setPen(QPen(color, 2 if self.owner.active == index else 1.6, Qt.DashLine if user else Qt.SolidLine))
            path = QPainterPath(points[0])
            for point in points[1:]: path.lineTo(point)
            painter.drawPath(path)
            end, previous = points[-1], points[-2]
            dx, dy = end.x() - previous.x(), end.y() - previous.y()
            length = max(1, (dx * dx + dy * dy) ** .5); ux, uy = dx / length, dy / length
            painter.setPen(QPen(color, 1.8))
            for side in (-1, 1):
                painter.drawLine(end, QPointF(end.x() - 7 * ux + side * 4 * uy, end.y() - 7 * uy - side * 4 * ux))
        painter.end()

