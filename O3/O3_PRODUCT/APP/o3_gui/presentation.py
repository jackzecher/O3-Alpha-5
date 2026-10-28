"""Shared, accessible native presentation. Theme names are persisted UI choices."""
import sys
from pathlib import Path
from PySide6.QtGui import QPalette, QColor, QIcon
from .ui import stylesheet, icon, INK, MUTED, BLUE, PALE, LINE
from .appearance import (THEMES, THEME_CHOICES, normalize_appearance, derived_theme,
                         readable, contrast, mix, luminance)

FONT = 'Segoe UI' if sys.platform == 'win32' else 'Nimbus Sans [UKWN]'
_THEMES = {
    'harbor': dict(canvas='#ffffff', rail='#f3f6fa', surface='#f6f8fb', body='#243247',
        muted='#57677b', accent='#285d9a', accent_hover='#1d4a80', selection='#e5eef9',
        border='#cbd5e2', border_soft='#e3e9f0', hover='#edf2f8', control='#f4f7fb',
        success='#256348', warn='#7a570d', error='#a23636', error_surface='#fff1ef',
        focus='#285d9a', edge='#5d718b', scrollbar='#91a1b4', disabled='#69788a'),
    'warm-paper': dict(canvas='#fffdf8', rail='#f4efe5', surface='#f8f4eb', body='#3a332b',
        muted='#706252', accent='#855224', accent_hover='#6b3e19', selection='#f2e6d5',
        border='#d4c6b3', border_soft='#e8e0d4', hover='#f2ebdf', control='#f8f3e9',
        success='#356342', warn='#78530e', error='#a03631', error_surface='#fff0e9',
        focus='#855224', edge='#80684c', scrollbar='#b6a48e', disabled='#7c6e60'),
    'sage': dict(canvas='#fcfdfa', rail='#edf3ed', surface='#f2f6f0', body='#293a32',
        muted='#546b5d', accent='#2e6b52', accent_hover='#21513e', selection='#dfede3',
        border='#bdcebf', border_soft='#dce6dd', hover='#e8f0e8', control='#f0f5ef',
        success='#28623f', warn='#78570f', error='#a23636', error_surface='#fff0ec',
        focus='#2e6b52', edge='#587560', scrollbar='#92a797', disabled='#6d7c70'),
    'slate': dict(canvas='#fbfcfe', rail='#eef0f5', surface='#f2f4f8', body='#303549',
        muted='#60657c', accent='#5b518f', accent_hover='#463d73', selection='#e9e5f6',
        border='#c8cbdb', border_soft='#e2e4ee', hover='#ececf5', control='#f0f2f8',
        success='#2f6750', warn='#7b580e', error='#a23636', error_surface='#fff0f0',
        focus='#5b518f', edge='#6b6a86', scrollbar='#a0a3ba', disabled='#74798c'),
    'night': dict(canvas='#18212d', rail='#141c27', surface='#202d3d', body='#edf2f9',
        muted='#b0bed0', accent='#9bc3fa', accent_hover='#bedaff', selection='#2b425e',
        border='#526278', border_soft='#344356', hover='#29394e', control='#233144',
        success='#9bd6b5', warn='#eac67c', error='#ffb2ab', error_surface='#452c33',
        focus='#a8cdff', edge='#94a9c5', scrollbar='#62748e', disabled='#9ba9bb'),
}
for _key in THEMES:
    if _key not in _THEMES:
        _THEMES[_key] = derived_theme(_key)

def theme_tokens(theme='harbor', appearance=None):
    """Return independent tokens; unknown historic preferences safely use Harbor."""
    c = dict(_THEMES.get(theme, _THEMES['harbor']))
    prefs = normalize_appearance(appearance)
    if prefs['accent']:
        c['accent'] = prefs['accent']
        c['selection'] = mix(c['canvas'], c['accent'], .18 if luminance(c['canvas']) < .2 else .12)
    surfaces = [c[k] for k in ('canvas', 'rail', 'surface', 'control', 'selection', 'hover')]
    minimum = 7.0 if prefs['contrast'] else 4.5
    for key in ('body', 'muted', 'accent'):
        c[key] = readable(c[key], surfaces, minimum)
    c['accent_hover'] = readable(mix(c['accent'], c['body'], .15), surfaces, minimum)
    c['focus'] = c['accent']
    if prefs['contrast']:
        c['border'] = readable(c['border'], [c['canvas'], c['control']], 3.0)
        c['border_soft'] = c['border']
        c['disabled'] = readable(c['disabled'], surfaces, 4.5)
    foreground = max(('#ffffff', '#172437'), key=lambda v: min(contrast(v, c[k]) for k in ('accent', 'accent_hover')))
    c.update(panel=c['canvas'], panel_light=c['surface'], selection_text=c['body'],
        selected_side=c['selection'], white=c['body'], dim=c['muted'], link=c['accent'],
        accent_hot=c['accent'], control_hover=c['hover'], control_border=c['border'],
        on_accent=foreground, tooltip='#1b2839',
        tooltip_text='#ffffff')
    return c

COLORS = theme_tokens()
DENSITIES = {
    'compact': {'spacing':6,'margin':12,'control_height':29,'row_height':35,'context_row_height':36,'tab_height':36,'toolbar_height':40,'cell_padding':2,'control_padding':3,'tab_padding':6},
    'balanced': {'spacing':8,'margin':16,'control_height':32,'row_height':48,'context_row_height':40,'tab_height':38,'toolbar_height':44,'cell_padding':6,'control_padding':5,'tab_padding':7},
    'comfortable': {'spacing':10,'margin':20,'control_height':38,'row_height':60,'context_row_height':44,'tab_height':42,'toolbar_height':50,'cell_padding':8,'control_padding':8,'tab_padding':10},
}

def density_metrics(name='compact'):
    return dict(DENSITIES.get(name, DENSITIES['compact']))

def native_icon(name, color=None, size=20):
    aliases = {'Email':'mail', 'Mail':'mail', 'Event':'calendar', 'Person':'person',
               'Organization':'building', 'Document':'doc', 'LineOfEffort':'effort',
               'settings':'sliders', 'health':'sources', 'overview':'grid'}
    return icon(aliases.get(name, name), color, size)

def application_icon():
    """Window icon only; native launcher resources require the missing build source."""
    return QIcon(str(Path(__file__).with_name('assets') / 'o3.ico'))

def current_tokens():
    """Painting uses real tokens, not Qt's style-generated translucent roles."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app:
        cached = app.property('o3_tokens')
        if isinstance(cached, dict): return cached
        name = app.property('o3_theme')
        if name in _THEMES: return theme_tokens(name)
        background = app.palette().color(QPalette.Window).name()
        for key, values in _THEMES.items():
            if values['canvas'] == background: return theme_tokens(key)
    return theme_tokens()

def current_appearance():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    return normalize_appearance(app.property('o3_appearance') if app else None)


def font_family(appearance=None, reader=False):
    prefs = normalize_appearance(appearance)
    family = {'default': FONT, 'open': 'Verdana', 'soft': 'Trebuchet MS'}[prefs['font']]
    if reader:
        return {'match': family, 'book': 'Georgia', 'plain': 'Consolas'}[prefs['reader_font']]
    return family


def native_palette(theme='harbor', appearance=None):
    c = theme_tokens(theme, appearance)
    p = QPalette()
    for role, token in {'Window':'canvas', 'WindowText':'body', 'Base':'canvas',
        'AlternateBase':'surface', 'Text':'body', 'Button':'control', 'ButtonText':'body',
        'Highlight':'selection', 'HighlightedText':'body', 'Link':'accent',
        'LinkVisited':'accent', 'PlaceholderText':'muted', 'ToolTipBase':'tooltip',
        'ToolTipText':'tooltip_text', 'Mid':'edge', 'Dark':'border', 'Light':'surface',
        'BrightText':'on_accent', 'Accent':'accent'}.items():
        if hasattr(QPalette.ColorRole, role):
            p.setColor(getattr(QPalette.ColorRole, role), QColor(c[token]))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(c['disabled']))
    return p

def stylesheet_for(density='compact', scale=1.0, theme='harbor', appearance=None):
    prefs = normalize_appearance(appearance)
    c = theme_tokens(theme, prefs)
    density = prefs['density'] if appearance is not None else density
    padding = {'compact': 4, 'balanced': 6, 'comfortable': 9}[density]
    asset_dir = Path(__file__).with_name('assets').as_posix()
    arrow = 'chevron-light.svg' if luminance(c['canvas']) < .2 else 'chevron-dark.svg'
    checkmark = 'check-dark.svg' if luminance(c['on_accent']) < .2 else 'check-light.svg'
    return stylesheet(scale, font_family(prefs), c) + f'''
    QDialog {{ background:{c['canvas']}; }}
    QListWidget#settingsNav::item {{ padding:10px 12px; margin:2px 0; border-radius:7px; }}
    QListWidget#settingsNav::item:selected {{ background:{c['selection']}; color:{c['body']}; font-weight:600; }}
    QListWidget#commandResults::item, QListWidget#savedPerspectives::item {{ padding:9px 12px; margin:2px 0; border-radius:6px; }}
    QListWidget#commandResults::item:selected, QListWidget#savedPerspectives::item:selected {{ background:{c['selection']}; color:{c['body']}; }}
    QListWidget#commandResults::item:disabled {{ color:{c['disabled']}; }}
    QComboBox::drop-down {{ border:0; width:28px; }}
    QComboBox::down-arrow {{ image:url("{asset_dir}/{arrow}"); width:12px; height:12px; }}
    QCheckBox::indicator {{ width:{round(17*scale)}px; height:{round(17*scale)}px; border:1px solid {c['muted']}; border-radius:4px; background:{c['canvas']}; }}
    QCheckBox::indicator:checked {{ background:{c['accent']}; border-color:{c['accent']}; image:url("{asset_dir}/{checkmark}"); }}
    QCheckBox::indicator:hover {{ border:2px solid {c['focus']}; }}
    QCheckBox::indicator:disabled {{ border-color:{c['disabled']}; background:{c['surface']}; }}
    QDialog QPushButton {{ padding:8px 12px; }}
    QPushButton#primary {{ background:{c['accent']}; color:{c['on_accent']}; text-align:center; font-weight:600; }}
    QToolButton {{ border:1px solid {c['border']}; padding:6px 10px; border-radius:6px; background:{c['control']}; }}
    QToolButton:checked, QPushButton:checked {{ background:{c['selection']}; color:{c['body']}; }}
    QTableView, QTreeView, QPlainTextEdit {{ background:{c['canvas']}; border:1px solid {c['border']}; border-radius:6px; selection-background-color:{c['selection']}; selection-color:{c['body']}; }}
    QTreeView::item, QTableView::item {{ padding:{padding}px; border-bottom:1px solid {c['border_soft']}; }}
    QTreeView::item:selected, QTableView::item:selected {{ background:{c['selection']}; color:{c['body']}; }}
    QHeaderView::section {{ background:{c['surface']}; color:{c['muted']}; border:0; border-bottom:1px solid {c['border']}; padding:9px; text-align:left; }}
    QTabWidget::pane {{ border:0; border-top:1px solid {c['border']}; }}
    QTabBar::tab {{ padding:9px 16px; color:{c['muted']}; background:transparent; border-bottom:2px solid transparent; }}
    QTabBar::tab:selected {{ border-bottom-color:{c['accent']}; color:{c['accent']}; }}
    QSpinBox, QDoubleSpinBox, QDateEdit, QDateTimeEdit {{ background:{c['control']}; border:1px solid {c['border']}; border-radius:5px; padding:6px; }}
    QLabel#dialog_status {{ color:{c['error']}; background:{c['error_surface']}; padding:10px; border-radius:6px; }}
    QLabel#muted, QLabel#sidebar_hint, QLabel#eyebrow {{ color:{c['muted']}; }}
    QLabel#section, QLabel#dialog_title {{ font-size:{round(19*scale)}px; font-weight:600; }}
    QSplitter::handle {{ background:{c['border_soft']}; }}
    QProgressBar {{ border:1px solid {c['border']}; border-radius:4px; background:{c['surface']}; color:{c['body']}; text-align:center; }}
    QProgressBar::chunk {{ background:{c['accent']}; border-radius:3px; }}
    QPushButton[kind="nav"] {{ padding:{padding+3}px 11px; }}
    QFrame#appearancePreview {{ border:1px solid {c['border']}; border-radius:12px; background:{c['surface']}; }}
    QLabel#appearanceHint {{ color:{c['muted']}; }}
    QSlider::groove:horizontal {{ height:5px; background:{c['border']}; border-radius:2px; }}
    QSlider::handle:horizontal {{ width:18px; margin:-7px 0; border-radius:9px; background:{c['accent']}; }}
    '''

CSS = stylesheet_for()

