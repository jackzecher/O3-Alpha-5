"""A visual appearance editor; all changes stay staged until Settings saves."""
from copy import deepcopy
import uuid
from PySide6.QtCore import Qt, Signal, QRectF, QTimer, QEvent, QSize
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics, QPixmap, QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QComboBox, QCheckBox, QPushButton, QAbstractButton, QTabWidget, QScrollArea,
    QListWidget, QListWidgetItem, QLineEdit, QColorDialog, QInputDialog, QSizePolicy)
from .appearance import (THEMES, THEME_CHOICES, DEFAULTS, TEXT_SCALES, FONT_CHOICES,
                         READER_FONTS, normalize_appearance, saved_looks)
from .presentation import theme_tokens, font_family, current_appearance
from .ui import text, btn, rule


class ThemeCard(QAbstractButton):
    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self.setCheckable(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setText(THEMES[key][0])
        self.setAccessibleName(f'{self.text()}, {THEMES[key][1]} theme')
        self.setToolTip(self.accessibleName())
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.fit()

    def fit(self):
        font=QFont(font_family(current_appearance()));font.setPixelSize(round(15*current_appearance()['text_scale']))
        self.setFont(font);self.setFixedHeight(82 + QFontMetrics(font).height() + 20)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange): self.fit()

    def paintEvent(self, event):
        colors = theme_tokens(self.key)
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setBrush(QColor(colors['canvas']))
        p.setPen(QPen(QColor(colors['accent'] if self.isChecked() or self.hasFocus() else colors['border']), 3 if self.isChecked() or self.hasFocus() else 1))
        p.drawRoundedRect(r, 10, 10)
        p.setPen(Qt.NoPen); p.setBrush(QColor(colors['rail']))
        p.drawRoundedRect(QRectF(13, 14, 28, 53), 5, 5)
        p.setBrush(QColor(colors['accent'])); p.drawRoundedRect(QRectF(20, 23, 14, 5), 2, 2)
        p.setBrush(QColor(colors['muted']))
        for y in (36, 46, 56): p.drawRoundedRect(QRectF(20, y, 14, 3), 1, 1)
        p.setBrush(QColor(colors['selection'])); p.drawRoundedRect(QRectF(49, 14, r.width() - 61, 23), 4, 4)
        p.setBrush(QColor(colors['accent'])); p.drawRoundedRect(QRectF(58, 24, min(59, r.width() - 82), 4), 2, 2)
        p.setBrush(QColor(colors['muted']))
        for y, length in ((46, .63), (58, .42)):
            p.drawRoundedRect(QRectF(50, y, (r.width() - 62) * length, 3), 1, 1)
        font = QFont(font_family(current_appearance()));font.setPixelSize(round(15*current_appearance()['text_scale']));font.setWeight(QFont.DemiBold);p.setFont(font)
        p.setPen(QColor(colors['body']))
        p.drawText(QRectF(13, 77, r.width() - 23, self.fontMetrics().height() + 8), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(font).elidedText(self.text(), Qt.ElideRight, int(r.width() - 23)))


class ThemeGallery(QWidget):
    chosen = Signal(str)

    def __init__(self):
        super().__init__(); self.mode = 'all'; self.columns = 0
        self.grid = QGridLayout(self); self.grid.setContentsMargins(0, 0, 0, 0); self.grid.setSpacing(12); self.grid.setAlignment(Qt.AlignTop)
        self.cards = {}
        for key in THEMES:
            card = ThemeCard(key, self)
            card.clicked.connect(lambda checked=False, name=key: self.chosen.emit(name))
            self.cards[key] = card
        self.reflow()

    def filter(self, mode):
        self.mode = mode; self.reflow()

    def reflow(self):
        minimum = max(158, self.fontMetrics().horizontalAdvance('Olive Linen') + 38)
        columns = max(1, min(4, (max(1, self.width()) + 12) // (minimum + 12)))
        while self.grid.count(): self.grid.takeAt(0)
        shown = [key for key, spec in THEMES.items() if self.mode == 'all' or spec[1] == self.mode]
        for key, card in self.cards.items(): card.setVisible(key in shown)
        for i, key in enumerate(shown): self.grid.addWidget(self.cards[key], i // columns, i % columns)
        self.columns = columns
        rows=(len(shown)+columns-1)//columns
        self.setFixedHeight(rows*(next(iter(self.cards.values())).height()+12)-12 if rows else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event); self.reflow()


class AppearancePreview(QWidget):
    """Decorative sample with an accessible text equivalent; the real app previews too."""
    def __init__(self):
        super().__init__(); self.value = dict(DEFAULTS); self.setMinimumWidth(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed); self.setFixedHeight(170)

    def set_value(self, value):
        self.value = normalize_appearance(value)
        self.setAccessibleName(f"Appearance sample: {THEMES[self.value['theme']][0]}, {round(self.value['text_scale'] * 100)} percent text, {self.value['density']} spacing.")
        self.update()

    def paintEvent(self, event):
        c = theme_tokens(self.value['theme'], self.value)
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(c['border']), 1)); p.setBrush(QColor(c['canvas'])); p.drawRoundedRect(r, 12, 12)
        focus = self.value['navigation'] == 'focus'
        left = 20 if focus else 76
        if not focus:
            p.setPen(Qt.NoPen); p.setBrush(QColor(c['rail'])); p.drawRoundedRect(QRectF(12, 12, 46, 146), 6, 6)
            p.setBrush(QColor(c['accent'])); p.drawEllipse(QRectF(25, 27, 19, 19))
            p.setBrush(QColor(c['muted']))
            for y in (64, 85, 106): p.drawRoundedRect(QRectF(24, y, 21, 4), 2, 2)
        font = QFont(font_family(self.value)); font.setPixelSize(19); font.setWeight(QFont.DemiBold)
        p.setFont(font); p.setPen(QColor(c['body']))
        p.drawText(QRectF(left, 18, self.width() - left - 16, 29), Qt.AlignVCenter, 'Your work, connected.')
        p.setPen(Qt.NoPen); p.setBrush(QColor(c['selection']))
        p.drawRoundedRect(QRectF(left, 57, self.width() - left - 18, 38), 6, 6)
        p.setPen(QColor(c['accent'])); font.setPixelSize(13); p.setFont(font)
        p.drawText(QRectF(left + 12, 57, self.width() - left - 40, 38), Qt.AlignVCenter, 'Unanswered  ·  A clearer view of your day')
        font = QFont(font_family(self.value, reader=True)); font.setPixelSize(round(14 * self.value['reader_scale']))
        p.setFont(font); p.setPen(QColor(c['body']))
        p.drawText(QRectF(left, 108, self.width() - left - 22, 40), Qt.AlignLeft | Qt.TextWordWrap, 'A little room to think. Everything in its place.')


def choice(items, name):
    combo = QComboBox(); combo.setAccessibleName(name)
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(5); combo.setMinimumWidth(0)
    for key, label in items: combo.addItem(label, key)
    return combo


class AppearanceEditor(QWidget):
    changed = Signal(dict)

    def __init__(self, value, looks=(), parent=None):
        super().__init__(parent); self.updating = True; self.value = normalize_appearance(value)
        self.looks = saved_looks(list(looks)); self.accent = self.value['accent']; self.controls = {}
        root = QVBoxLayout(self); root.setContentsMargins(12, 0, 0, 0); root.setSpacing(12)
        root.addWidget(text('Make O3 feel like yours.','section',True))
        self.preview = AppearancePreview()
        root.addWidget(text('Preview your choices in O3. Save them when you are happy, or cancel to go back.','small',True))
        self.tabs = QTabWidget(); self.tabs.setAccessibleName('Appearance categories'); root.addWidget(self.tabs,1)
        self.tabs.tabBar().setExpanding(False); self.tabs.tabBar().setUsesScrollButtons(True)

        def page(name):
            area = QScrollArea(); area.setWidgetResizable(True); area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            area.setAccessibleName(name + ' appearance settings')
            content = QWidget(); layout = QVBoxLayout(content); layout.setContentsMargins(4,16,16,12); layout.setSpacing(12)
            area.setWidget(content); self.tabs.addTab(area,name); return layout

        def field(layout, key, name, choices, description=None):
            lab = text(name,'body',True); control = choice(choices,name)
            lab.setBuddy(control); layout.addWidget(lab); layout.addWidget(control)
            if description: layout.addWidget(text(description,'small',True))
            self.controls[key] = control; control.currentIndexChanged.connect(self.emit_change)
            return control

        colors = page('Themes')
        colors.addWidget(text('24 palettes · 12 light and 12 dark','muted',True))
        self.theme = field(colors,'theme','Color theme',[(key,f'{name} · {THEMES[key][1].title()}') for key,name in THEME_CHOICES])
        self.filter = choice([('all','All themes'),('light','Light themes'),('dark','Dark themes')],'Show themes')
        colors.addWidget(self.filter); self.gallery = ThemeGallery(); colors.addWidget(self.gallery)
        self.filter.currentIndexChanged.connect(lambda: self.gallery.filter(self.filter.currentData()))
        self.gallery.chosen.connect(lambda key: self.theme.setCurrentIndex(self.theme.findData(key)))
        colors.addStretch()
        colors=page('Colors');colors.addWidget(text('Make the color your own','section',True))
        self.color_preview=AppearancePreview();colors.addWidget(self.color_preview)
        self.accent_button = btn('Choose an accent color…',self.choose_accent,kind='secondary'); colors.addWidget(self.accent_button)
        colors.addWidget(btn('Use the theme’s accent color',self.clear_accent,kind='link'))
        self.accent_note = text('','small',True); colors.addWidget(self.accent_note)
        self.contrast = QCheckBox('Stronger contrast'); self.contrast.setAccessibleName('Stronger contrast')
        self.contrast.toggled.connect(self.emit_change); colors.addWidget(self.contrast)
        colors.addWidget(text('Clearer text, more visible borders, and the same color character.','small',True)); colors.addStretch()

        typography = page('Text')
        typography.addWidget(self.preview)
        self.scale_choice = field(typography,'text_scale','Text size',[(v, f'{round(v*100)}%' + (' · Standard' if v == 1 else '')) for v in TEXT_SCALES], 'Changes text throughout O3.')
        field(typography,'font','O3 lettering',FONT_CHOICES)
        typography.addWidget(rule()); typography.addWidget(text('Reading messages & documents','section',True))
        field(typography,'reader_font','Reading font',READER_FONTS)
        field(typography,'reader_scale','Reading size',[(v, f'{round(v*100)}%' + (' · Match text size' if v == 1 else '')) for v in TEXT_SCALES], 'An extra size adjustment for the text you read; buttons keep their usual size.')
        field(typography,'line_spacing','Space between lines',[(1.25,'Close'),(1.45,'Balanced'),(1.7,'Relaxed'),(2.0,'Extra room')])
        typography.addStretch()

        layout = page('Layout')
        field(layout,'density','Space between items',[('compact','Compact'),('balanced','Balanced'),('comfortable','Roomy')], 'Changes list rows, tables, and navigation spacing.')
        field(layout,'navigation','Navigation',[('automatic','Sidebar when there is room'),('focus','Quiet layout · use the Menu button')], 'The Menu button always keeps every destination within reach.')
        field(layout,'reading_width','Reading width',[('narrow','Narrow · shorter lines'),('comfortable','Comfortable'),('wide','Fill available space')], 'Keeps long messages comfortable to read on a wide window.')
        self.show_explanations=QCheckBox('Always show why mail is listed');self.show_explanations.setAccessibleName(self.show_explanations.text())
        self.show_explanations.toggled.connect(self.emit_change);layout.addWidget(self.show_explanations)
        layout.addWidget(text('Usually, only important notices appear under mail. Use “Why is this here?” whenever you want the full explanation.','small',True))
        self.show_splash = QCheckBox('Welcome screen on startup'); self.show_splash.setAccessibleName(self.show_splash.text())
        self.show_splash.toggled.connect(self.emit_change); layout.addWidget(self.show_splash)
        layout.addWidget(text('Closes as soon as the workspace is ready.','small',True)); layout.addStretch()
        layout.addWidget(btn('Preview welcome screen',self.preview_welcome,kind='secondary'))

        library = page('My looks')
        library.addWidget(text('Keep combinations you love','section',True))
        library.addWidget(text('A look remembers your theme, colors, text, and layout. These changes are saved when you choose Save appearance.','muted',True))
        self.look_name = QLineEdit(); self.look_name.setMaxLength(48); self.look_name.setPlaceholderText('Name this look'); self.look_name.setAccessibleName('Name for a new look'); library.addWidget(self.look_name)
        library.addWidget(btn('Save this as a new look',self.add_look,kind='secondary'))
        self.look_list = QListWidget(); self.look_list.setAccessibleName('Your saved looks'); self.look_list.setMinimumHeight(140); library.addWidget(self.look_list,1)
        self.look_list.itemActivated.connect(self.use_look)
        for label, method in [('Use selected look',self.use_look),('Update selected look',self.update_look),('Rename selected look',self.rename_look),('Remove selected look',self.remove_look)]:
            library.addWidget(btn(label,method,kind='link'))
        self.look_note = text('','small',True); library.addWidget(self.look_note)
        root.addWidget(btn('Restore default appearance',lambda: self.set_value(DEFAULTS),kind='link'))
        self.refresh_looks(); self.set_value(value,emit=False); self.updating = False

    def get_value(self):
        return normalize_appearance({**{key: control.currentData() for key,control in self.controls.items()},
            'accent':self.accent,'contrast':self.contrast.isChecked(),'show_splash':self.show_splash.isChecked(),
            'show_mail_explanations':self.show_explanations.isChecked()})

    def set_value(self, value, emit=True):
        prior = self.updating; self.updating = True; value = normalize_appearance(value)
        for key,control in self.controls.items(): control.setCurrentIndex(control.findData(value[key]))
        self.accent = value['accent']; self.contrast.setChecked(value['contrast']); self.show_splash.setChecked(value['show_splash'])
        self.show_explanations.setChecked(value['show_mail_explanations'])
        self.updating = prior; self.refresh_preview()
        if emit and not self.updating: self.changed.emit(self.get_value())

    def refresh_preview(self):
        value = self.get_value(); self.value = value; self.preview.set_value(value); self.color_preview.set_value(value)
        for key,card in self.gallery.cards.items(): card.setChecked(key == value['theme'])
        color = theme_tokens(value['theme'],value)['accent']
        pix = QPixmap(24,24); pix.fill(Qt.transparent); painter = QPainter(pix); painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen); painter.setBrush(QColor(color)); painter.drawEllipse(2,2,20,20); painter.end(); self.accent_button.setIcon(QIcon(pix))
        self.accent_note.setText('Your accent is adjusted slightly to keep labels readable.' if self.accent and self.accent != color else 'Used for selections, links, and important buttons. Text stays readable in every theme.')

    def emit_change(self,*_):
        if self.updating: return
        self.refresh_preview(); self.changed.emit(self.get_value())

    def choose_accent(self):
        initial = self.accent or theme_tokens(self.theme.currentData())['accent']
        color = QColorDialog.getColor(QColor(initial),self,'Choose your accent color')
        if color.isValid(): self.accent = color.name(); self.emit_change()

    def clear_accent(self):
        self.accent = None; self.emit_change()

    def preview_welcome(self):
        from .startup import WelcomeScreen
        dialog=WelcomeScreen(self.get_value(),self,preview=True);dialog.exec()

    def selected_look(self):
        item = self.look_list.currentItem()
        return next((look for look in self.looks if item and look['id'] == item.data(Qt.UserRole)),None)

    def refresh_looks(self, selected=None):
        self.look_list.clear()
        for look in self.looks:
            item = QListWidgetItem(look['name']); item.setData(Qt.UserRole,look['id']); self.look_list.addItem(item)
            if look['id'] == selected: self.look_list.setCurrentItem(item)
        if not self.looks: self.look_note.setText('No saved looks yet. Start with a theme, then make it yours.')

    def add_look(self):
        name = self.look_name.text().strip()
        if not name: self.look_note.setText('Give this look a name first.'); self.look_name.setFocus(); return
        if len(self.looks) >= 20: self.look_note.setText('You can keep 20 looks. Remove one before adding another.'); return
        if any(x['name'].casefold() == name.casefold() for x in self.looks): self.look_note.setText('That name is already used. Choose another name, or update the selected look.'); return
        look = {'id':uuid.uuid4().hex,'name':name,'appearance':self.get_value()}; self.looks.append(look)
        self.refresh_looks(look['id']); self.look_name.clear(); self.look_note.setText('Added to your preview. Choose Save appearance to keep it.')

    def use_look(self,*_):
        look = self.selected_look()
        if look: self.set_value(look['appearance']); self.look_note.setText('Previewing '+look['name']+'. Choose Save appearance to keep these choices.')

    def update_look(self):
        look = self.selected_look()
        if look: look['appearance'] = self.get_value(); self.look_note.setText('Updated in your preview. Choose Save appearance to keep it.')

    def rename_look(self):
        look = self.selected_look()
        if not look: return
        name,ok = QInputDialog.getText(self,'Rename look','New name',text=look['name'])
        name = name.strip()
        if ok and name and len(name)<=48 and not any(x['id']!=look['id'] and x['name'].casefold()==name.casefold() for x in self.looks):
            look['name'] = name; self.refresh_looks(look['id']); self.look_note.setText('Renamed in your preview. Choose Save appearance to keep it.')
        elif ok: self.look_note.setText('Use a unique name of 1–48 characters.')

    def remove_look(self):
        look = self.selected_look()
        if look: self.looks.remove(look); self.refresh_looks(); self.look_note.setText('Removed from your preview. Cancel appearance restores it.')
