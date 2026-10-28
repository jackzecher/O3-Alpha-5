"""O3 Alpha 3 native presentation tokens. Rendering helpers contain no knowledge evaluation."""
from PySide6.QtCore import Qt, QSize, QRectF, QPointF, QEvent
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QIcon, QIconEngine, QPixmap, QFont, QFontMetrics, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QFrame, QHBoxLayout, QVBoxLayout, QWidget, QStyledItemDelegate, QStyle, QSizePolicy

INK='#26303b'; MUTED='#65717e'; BLUE='#3265ad'; PALE='#e9f0fa'; LINE='#e8ecf0'

class _AdaptiveIcon(QIconEngine):
    """Draw from the current palette, including after a live appearance change."""
    def __init__(self, name):
        super().__init__(); self.name = name
    def clone(self): return _AdaptiveIcon(self.name)
    def pixmap(self, size, mode, state):
        pal = QApplication.palette()
        color = pal.color(QPalette.Disabled if mode == QIcon.Disabled else QPalette.Active, QPalette.WindowText)
        return icon(self.name, color.name(), max(size.width(), size.height())).pixmap(size)
    def paint(self, painter, rect, mode, state):
        painter.drawPixmap(rect, self.pixmap(rect.size(), mode, state))

def icon(name, color=None, size=20):
    if color is None: return QIcon(_AdaptiveIcon(name))
    pix=QPixmap(size*2,size*2);pix.fill(Qt.transparent)
    p=QPainter(pix);p.setRenderHint(QPainter.Antialiasing);p.scale(size*2/24,size*2/24)
    p.setPen(QPen(QColor(color),1.55,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.setBrush(Qt.NoBrush)
    if name=='search':
        p.drawEllipse(QRectF(4,4,11,11));p.drawLine(QPointF(13,13),QPointF(20,20))
    elif name=='mail':
        p.drawRoundedRect(QRectF(3,5,18,14),2,2);p.drawLine(3,6,12,13);p.drawLine(12,13,21,6)
    elif name=='effort':
        p.drawRoundedRect(QRectF(4,4,16,16),4,4);p.drawLine(8,12,11,15);p.drawLine(11,15,17,9)
    elif name=='grid':
        for x,y in [(4,4),(14,4),(4,14),(14,14)]:p.drawRoundedRect(QRectF(x,y,6,6),1,1)
    elif name=='doc':
        path=QPainterPath(QPointF(6,3));path.lineTo(14,3);path.lineTo(19,8);path.lineTo(19,21);path.lineTo(6,21);path.closeSubpath();p.drawPath(path)
        p.drawLine(14,3,14,8);p.drawLine(14,8,19,8);p.drawLine(9,12,16,12);p.drawLine(9,16,15,16)
    elif name=='calendar':
        p.drawRoundedRect(QRectF(3,5,18,16),2,2);p.drawLine(3,10,21,10);p.drawLine(8,3,8,7);p.drawLine(16,3,16,7)
    elif name=='building':
        p.drawRect(QRectF(5,3,14,18));p.drawLine(3,21,21,21)
        for x,y in [(9,7),(15,7),(9,11),(15,11),(9,15),(15,15)]:p.drawPoint(x,y)
    elif name=='person':
        p.drawEllipse(QRectF(8,3,8,8));p.drawArc(QRectF(4,12,16,15),0,180*16)
    elif name=='link':
        p.save();p.translate(12,12);p.rotate(-36);p.drawRoundedRect(QRectF(-9,-3,11,6),3,3);p.drawRoundedRect(QRectF(-2,-3,11,6),3,3);p.restore()
    elif name=='back':p.drawLine(19,12,5,12);p.drawLine(5,12,11,6);p.drawLine(5,12,11,18)
    elif name=='arrow':p.drawLine(5,12,19,12);p.drawLine(19,12,13,6);p.drawLine(19,12,13,18)
    elif name=='sliders':
        for y,x in [(6,9),(12,15),(18,7)]:
            p.drawLine(4,y,20,y);p.setBrush(QApplication.palette().color(QPalette.Window));p.drawEllipse(QRectF(x-2,y-2,4,4));p.setBrush(Qt.NoBrush)
    elif name=='sources':
        p.drawEllipse(QRectF(4,4,16,16));p.drawLine(12,8,12,13);p.drawPoint(12,17)
    elif name=='chevron':p.drawLine(9,7,14,12);p.drawLine(14,12,9,17)
    elif name=='more':
        for x in (6,12,18):p.setBrush(QColor(color));p.drawEllipse(QRectF(x-1,11,2,2))
    elif name=='close':p.drawLine(6,6,18,18);p.drawLine(18,6,6,18)
    elif name=='center':
        p.drawEllipse(QRectF(7,7,10,10));p.drawLine(12,2,12,6);p.drawLine(12,18,12,22);p.drawLine(2,12,6,12);p.drawLine(18,12,22,12)
    elif name=='list':
        for y in (6,12,18):p.drawPoint(4,y);p.drawLine(8,y,20,y)
    p.end();pix.setDevicePixelRatio(2);return QIcon(pix)

def text(s,role='body',wrap=False):
    w=QLabel(s);w.setProperty('role',role);w.setWordWrap(wrap);w.setTextFormat(Qt.PlainText);return w

class _SymbolLabel(QLabel):
    def __init__(self, name):
        super().__init__(); self.symbol_name = name
        self.setPixmap(icon(name).pixmap(18, 18))
    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange:
            self.setPixmap(icon(self.symbol_name).pixmap(18, 18))

def btn(s,callback=None,ico=None,kind='plain'):
    w=QPushButton(s);w.setProperty('kind',kind);w.setCursor(Qt.PointingHandCursor);w.setAccessibleName(s)
    if ico:w.setIcon(icon(ico));w.setIconSize(QSize(18,18))
    if callback:w.clicked.connect(callback)
    return w

def wrap_btn(s, callback=None, ico=None, kind='plain'):
    w=btn('',callback,None,kind);w.setAccessibleName(s);w.setToolTip(s)
    layout=QHBoxLayout(w);layout.setContentsMargins(8,5,8,5);layout.setSpacing(8)
    if ico:
        mark=_SymbolLabel(ico);mark.setFixedWidth(20);mark.setAttribute(Qt.WA_TransparentForMouseEvents);layout.addWidget(mark,0,Qt.AlignTop)
    lab=text(s,wrap=True);lab.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred);lab.setAttribute(Qt.WA_TransparentForMouseEvents);layout.addWidget(lab,1)
    w.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Minimum);w._label=lab
    return w


def rule():
    w=QFrame();w.setObjectName('divider');w.setFixedHeight(1);return w

def section(title, action=None, callback=None):
    w=QWidget();l=QHBoxLayout(w);l.setContentsMargins(0,0,0,0);l.addWidget(text(title,'section'));l.addStretch()
    if action:l.addWidget(btn(action,callback,kind='link'))
    return w

class RecordDelegate(QStyledItemDelegate):
    """Rows measure their date and reflow metadata instead of clipping it."""
    def __init__(self, parent=None, compact=False):
        super().__init__(parent); self.compact = compact

    @staticmethod
    def reason(record):
        if record.get('explanation_on_demand'):
            from .presentation import current_appearance
            if not current_appearance()['show_mail_explanations']:
                return str(record.get('inline_notice') or '')
        value = str(record.get('reason') or '')
        if record.get('state') in {'unknown', 'uncertain', 'unresolved', 'couldnt_check', 'could_not_check'}:
            return "Couldn't check" + (' · ' + value if value else '')
        return value

    def layout_for(self, option, record, width=None):
        font = QFont(option.font)
        base = font.pixelSize() if font.pixelSize() > 0 else 15
        font.setPixelSize(base)
        small = QFont(font); small.setPixelSize(max(12, round(base * .87)))
        fm, sm = QFontMetrics(font), QFontMetrics(small)
        if width is None:
            parent = self.parent()
            width = parent.viewport().width() if parent and hasattr(parent, 'viewport') else 320
        width = max(80, width - 26)
        date = str(record.get('capture_label', record.get('date', '')) or '')
        date_width = sm.horizontalAdvance(date) + (2 if date else 0)
        separate_date = bool(date and (date_width > width * .43 or width - date_width - 20 < base * 9))
        date_lines = max(1, (sm.boundingRect(0, 0, width, 9999, Qt.TextWordWrap, date).height() + sm.height() - 1) // sm.height()) if separate_date else 1
        title_width = width if separate_date or not date else width - date_width - 20
        from .presentation import current_appearance
        spacing = {'compact':.72,'balanced':1.0,'comfortable':1.55}[current_appearance()['density']]
        gap, top = max(3, round(base * .22 * spacing)), max(5, round(base * .48 * spacing))
        title_y = top
        meta_y = title_y + fm.height() + gap
        date_y = meta_y + sm.height() + gap if separate_date else title_y + 1
        reason_y = (date_y + date_lines * sm.height() if separate_date else meta_y + sm.height()) + gap
        reason = self.reason(record)
        reason_lines = min(2, max(1, (sm.boundingRect(0, 0, width, 9999, Qt.TextWordWrap, reason).height() + sm.height() - 1) // sm.height())) if reason else 0
        height = reason_y + reason_lines * sm.height() + top if reason else (date_y + date_lines * sm.height() if separate_date else meta_y + sm.height()) + top
        return dict(font=font, small=small, width=width, title_width=title_width,
                    date=date, date_width=date_width, date_lines=date_lines, separate_date=separate_date,
                    title_y=title_y, meta_y=meta_y, date_y=date_y, reason_y=reason_y,
                    reason=reason, reason_lines=reason_lines, height=height)

    def sizeHint(self, option, index):
        record = index.data(Qt.UserRole + 1) or {}
        return QSize(260, self.layout_for(option, record)['height'])

    def paint(self, painter, option, index):
        record = index.data(Qt.UserRole + 1)
        if not record: return
        painter.save(); painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(0, 1, -2, -1)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        pal = option.palette
        from .presentation import current_tokens
        colors = current_tokens()
        if selected or hover:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors['selection'] if selected else colors['surface']))
            painter.drawRoundedRect(QRectF(rect), 8, 8)
        layout = self.layout_for(option, record, option.rect.width())
        x, y = rect.x() + 12, rect.y()
        painter.setFont(layout['font']); painter.setPen(QColor(colors['body']))
        fm = painter.fontMetrics()
        title = str(record.get('title') or 'Untitled')
        painter.drawText(QRectF(x, y + layout['title_y'], layout['title_width'], fm.height()),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         fm.elidedText(title, Qt.ElideRight, int(layout['title_width'])))
        painter.setFont(layout['small']); painter.setPen(QColor(colors['muted']))
        sm = painter.fontMetrics()
        kind = str(record.get('kind') or '')
        person = str(record.get('person') or '')
        meta = person if kind in {'Email', 'Mail'} else ' · '.join(p for p in (kind, person) if p)
        painter.drawText(QRectF(x, y + layout['meta_y'], layout['width'], sm.height()),
                         Qt.AlignLeft, sm.elidedText(meta, Qt.ElideRight, layout['width']))
        if layout['date']:
            dx = x if layout['separate_date'] else x + layout['width'] - layout['date_width']
            painter.drawText(QRectF(dx, y + layout['date_y'], min(layout['width'], layout['date_width']), layout['date_lines'] * sm.height()),
                             Qt.AlignLeft | (Qt.TextWordWrap if layout['separate_date'] else Qt.AlignVCenter), layout['date'])
        if layout['reason']:
            painter.setPen(QColor(colors['body']))
            # Two readable lines; the full explanation remains in accessible row text.
            lines = []
            rest = layout['reason']
            for number in range(layout['reason_lines']):
                if number == layout['reason_lines'] - 1:
                    lines.append(sm.elidedText(rest, Qt.ElideRight, layout['width'])); break
                words = rest.split(); line = ''
                while words and sm.horizontalAdvance((line + ' ' + words[0]).strip()) <= layout['width']:
                    line = (line + ' ' + words.pop(0)).strip()
                if not line and words: line = sm.elidedText(words.pop(0), Qt.ElideRight, layout['width'])
                lines.append(line); rest = ' '.join(words)
            for number, line in enumerate(lines):
                painter.drawText(QRectF(x, y + layout['reason_y'] + number * sm.height(), layout['width'], sm.height()), Qt.AlignLeft, line)
        if option.state & QStyle.State_HasFocus:
            painter.setPen(QPen(QColor(colors['focus']), 2)); painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(rect.adjusted(1, 1, -1, -1)), 8, 8)
        painter.restore()


def stylesheet(scale=1.0, font='Nimbus Sans', tokens=None):
    # Lazy import preserves older direct callers without a presentation import cycle.
    if tokens is None:
        from .presentation import theme_tokens
        tokens = theme_tokens()
    c = tokens
    sz = lambda value: round(value * scale)
    return f'''
    QWidget {{ background:{c['canvas']}; color:{c['body']}; font-family:"{font}"; font-size:{sz(15)}px; }}
    QLabel {{ background:transparent; }}
    QLabel[role="title"] {{ font-size:{sz(30)}px; font-weight:600; letter-spacing:-0.6px; }}
    QLabel[role="readerTitle"] {{ font-size:{sz(24)}px; font-weight:600; letter-spacing:-0.3px; }}
    QLabel[role="section"] {{ font-size:{sz(17)}px; font-weight:600; }}
    QLabel[role="body"] {{ font-size:{sz(15)}px; }}
    QLabel[role="muted"] {{ color:{c['muted']}; font-size:{sz(14)}px; }}
    QLabel[role="small"] {{ color:{c['muted']}; font-size:{sz(13)}px; }}
    QLabel[role="largeDate"] {{ font-size:{sz(29)}px; font-weight:500; color:{c['accent']}; }}
    QFrame#sidebar {{ background:{c['rail']}; border-right:1px solid {c['border_soft']}; }}
    QFrame#sidebar QLabel {{ background:transparent; }}
    QWidget#efforts {{ background:transparent; }}
    QLabel#brand {{ font-size:{sz(26)}px; font-weight:600; letter-spacing:-1px; }}
    QFrame#divider {{ background:{c['border_soft']}; border:0; }}
    QPushButton {{ border:2px solid transparent; border-radius:7px; padding:6px 8px; background:transparent; text-align:left; }}
    QPushButton:hover {{ background:{c['hover']}; }}
    QPushButton:focus, QToolButton:focus {{ border:2px solid {c['focus']}; }}
    QPushButton:disabled {{ color:{c['disabled']}; }}
    QPushButton[kind="nav"] {{ padding:9px 11px; color:{c['muted']}; }}
    QPushButton[kind="nav"]:checked {{ color:{c['accent']}; background:{c['selection']}; font-weight:600; }}
    QPushButton[kind="group"] {{ font-size:{sz(17)}px; font-weight:600; padding:5px 0; }}
    QPushButton[kind="link"] {{ color:{c['accent']}; padding:5px 1px; }}
    QPushButton[kind="link"] QLabel {{ color:{c['accent']}; }}
    QPushButton[kind="mapnode"] {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:12px; padding:6px; }}
    QPushButton[kind="mapnode"][selected="true"] {{ background:{c['selection']}; border:2px solid {c['accent']}; }}
    QPushButton[kind="mapnode"]:hover, QPushButton[kind="mapnode"]:focus {{ border:2px solid {c['focus']}; }}
    QPushButton[kind="edge"] {{ background:{c['canvas']}; color:{c['muted']}; font-size:{sz(13)}px; padding:3px 6px; }}
    QPushButton[kind="edge"][active="true"] {{ color:{c['accent']}; background:{c['selection']}; border-color:{c['accent']}; }}
    QPushButton[kind="edge"][active="true"] QLabel {{ color:{c['accent']}; }}
    QPushButton[kind="primary"] {{ background:{c['accent']}; color:{c['on_accent']}; padding:9px 15px; text-align:center; font-weight:600; }}
    QPushButton[kind="primary"]:hover {{ background:{c['accent_hover']}; }}
    QPushButton[kind="primary"] QLabel {{ color:{c['on_accent']}; }}
    QPushButton[kind="secondary"] {{ background:{c['control']}; color:{c['body']}; border:1px solid {c['border']}; text-align:center; padding:8px 12px; }}
    QLineEdit {{ background:{c['control']}; border:1px solid {c['border']}; border-radius:7px; padding:8px 12px; selection-background-color:{c['selection']}; selection-color:{c['body']}; }}
    QLineEdit:focus {{ background:{c['canvas']}; border:2px solid {c['focus']}; }}
    QListWidget {{ border:0; background:transparent; outline:0; }}
    QListWidget::item {{ border:0; background:transparent; padding:0; }}
    QListWidget::item:selected {{ background:{c['selection']}; color:{c['body']}; }}
    QScrollArea {{ border:0; }}
    QScrollBar:vertical {{ background:transparent; width:10px; margin:0; }}
    QScrollBar::handle:vertical {{ background:{c['scrollbar']}; min-height:30px; border-radius:5px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
    QScrollBar:horizontal {{ background:transparent; height:10px; margin:0; }}
    QScrollBar::handle:horizontal {{ background:{c['scrollbar']}; min-width:30px; border-radius:5px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:transparent; }}
    QTextBrowser {{ border:0; background:{c['canvas']}; selection-background-color:{c['selection']}; selection-color:{c['body']}; padding:0; }}
    QComboBox {{ background:{c['control']}; border:1px solid {c['border']}; border-radius:6px; padding:8px 28px 8px 10px; }}
    QComboBox:focus {{ border:2px solid {c['focus']}; }}
    QComboBox QAbstractItemView {{ background:{c['canvas']}; color:{c['body']}; selection-background-color:{c['selection']}; selection-color:{c['body']}; }}
    QMenu {{ background:{c['canvas']}; border:1px solid {c['border']}; padding:6px; }}
    QMenu::item {{ padding:9px 20px; }}
    QMenu::item:selected {{ background:{c['selection']}; color:{c['body']}; }}
    QCheckBox {{ spacing:10px; padding:5px; }}
    QCheckBox:focus {{ outline:2px solid {c['focus']}; }}
    QToolTip {{ background:{c['tooltip']}; color:{c['tooltip_text']}; border:0; padding:7px; }}
    '''

