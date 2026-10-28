"""Bounded appearance choices. Preferences are local to the existing workspace."""
from copy import deepcopy
import re

# Name, light/dark, canvas, rail, ink, accent. All other colors are derived.
THEMES = {
    'harbor': ('Harbor', 'light', '#ffffff', '#f3f6fa', '#243247', '#285d9a'),
    'warm-paper': ('Warm Paper', 'light', '#fffdf8', '#f4efe5', '#3a332b', '#855224'),
    'sage': ('Sage', 'light', '#fcfdfa', '#edf3ed', '#293a32', '#2e6b52'),
    'slate': ('Slate', 'light', '#fbfcfe', '#eef0f5', '#303549', '#5b518f'),
    'porcelain': ('Porcelain', 'light', '#faf9f6', '#efeeea', '#343838', '#486666'),
    'rosewater': ('Rosewater', 'light', '#fff8fa', '#f5e8ee', '#46303d', '#9a456c'),
    'sea-glass': ('Sea Glass', 'light', '#f7fcfb', '#e6f3f1', '#214249', '#187c80'),
    'apricot': ('Apricot', 'light', '#fff9f4', '#faeadd', '#51382f', '#ad542d'),
    'lavender': ('Lavender', 'light', '#fbf9ff', '#eee8fa', '#3c3552', '#7850ad'),
    'olive-linen': ('Olive Linen', 'light', '#fafbf3', '#eef0db', '#3b422c', '#64752b'),
    'glacier': ('Glacier', 'light', '#f6fbff', '#e4f1fc', '#263e53', '#2478b0'),
    'sandstone': ('Sandstone', 'light', '#fbf7ef', '#eee3d0', '#493f33', '#927027'),
    'night': ('Night', 'dark', '#18212d', '#141c27', '#edf2f9', '#9bc3fa'),
    'midnight': ('Midnight', 'dark', '#101426', '#0d1020', '#e6eaff', '#a9b5ff'),
    'aubergine': ('Aubergine', 'dark', '#291e31', '#211728', '#f3e7f5', '#e4ba83'),
    'forest': ('Forest', 'dark', '#142820', '#101f19', '#e4f1e8', '#95d8ad'),
    'charcoal': ('Charcoal', 'dark', '#202323', '#191c1c', '#eef2ef', '#b8d5c3'),
    'deep-ocean': ('Deep Ocean', 'dark', '#102a34', '#0d222b', '#e3f2f5', '#76d4dc'),
    'ember': ('Ember', 'dark', '#2b211f', '#231a19', '#f8eae3', '#f4af83'),
    'plum': ('Plum', 'dark', '#2d1d2a', '#241722', '#f6e6f0', '#e9a6ce'),
    'espresso': ('Espresso', 'dark', '#29231d', '#201b16', '#f1e9d9', '#d6be8d'),
    'indigo': ('Indigo', 'dark', '#202039', '#19192f', '#eeeafb', '#c4b1f4'),
    'moss': ('Moss', 'dark', '#252a1c', '#1d2216', '#edf0df', '#c4d48e'),
    'graphite': ('Graphite', 'dark', '#24262b', '#1c1e23', '#eceef2', '#e5b8a0'),
}
THEME_CHOICES = tuple((key, value[0]) for key, value in THEMES.items())
TEXT_SCALES = (1.0, 1.1, 1.25, 1.4, 1.5)
FONT_CHOICES = (('default', 'Clean'), ('open', 'Open & clear'), ('soft', 'Soft & rounded'))
READER_FONTS = (('match', 'Match O3'), ('book', 'Book'), ('plain', 'Plain text'))
DEFAULTS = {
    'theme': 'harbor', 'text_scale': 1.0, 'accent': None, 'font': 'default',
    'reader_font': 'match', 'reader_scale': 1.0, 'line_spacing': 1.45,
    'density': 'balanced', 'navigation': 'automatic', 'reading_width': 'wide',
    'contrast': False, 'show_splash': True, 'show_mail_explanations': False,
}


def normalize_appearance(raw=None):
    raw = raw if isinstance(raw, dict) else {}
    result = dict(DEFAULTS)
    options = {
        'theme': tuple(THEMES), 'text_scale': TEXT_SCALES,
        'font': tuple(k for k, _ in FONT_CHOICES), 'reader_font': tuple(k for k, _ in READER_FONTS),
        'reader_scale': TEXT_SCALES, 'line_spacing': (1.25, 1.45, 1.7, 2.0),
        'density': ('compact', 'balanced', 'comfortable'),
        'navigation': ('automatic', 'focus'), 'reading_width': ('narrow', 'comfortable', 'wide'),
    }
    for key, allowed in options.items():
        value = raw.get(key)
        if type(value) in (str, int, float) and value in allowed:
            result[key] = value
    color = raw.get('accent')
    if isinstance(color, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        result['accent'] = color.lower()
    for key in ('contrast', 'show_splash', 'show_mail_explanations'):
        if type(raw.get(key)) is bool:
            result[key] = raw[key]
    return result


def appearance_from_config(config):
    alpha = config.get('alpha3', {}) if isinstance(config, dict) else {}
    alpha = alpha if isinstance(alpha, dict) else {}
    raw = alpha.get('appearance', {})
    raw = dict(raw) if isinstance(raw, dict) else {}
    for key in ('theme', 'text_scale'):
        if key in alpha:
            raw[key] = alpha[key]
    return normalize_appearance(raw)


def store_appearance(config, value):
    value = normalize_appearance(value)
    if not isinstance(config.get('alpha3'), dict):
        config['alpha3'] = {}
    alpha = config['alpha3']
    extras = alpha.get('appearance', {})
    extras = deepcopy(extras) if isinstance(extras, dict) else {}
    extras.update({key: val for key, val in value.items() if key not in ('theme', 'text_scale')})
    alpha.update(theme=value['theme'], text_scale=value['text_scale'], appearance=extras)


def saved_looks(raw):
    result, ids = [], set()
    for item in (raw[:20] if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        key, name = item.get('id'), item.get('name')
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key) or key in ids:
            continue
        if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 for c in name):
            continue
        ids.add(key)
        result.append({'id': key, 'name': name.strip()[:48], 'appearance': normalize_appearance(item.get('appearance'))})
    return result


def rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, amount):
    return '#' + ''.join(f'{round(x * (1 - amount) + y * amount):02x}' for x, y in zip(rgb(a), rgb(b)))


def luminance(color):
    values = [value / 255 for value in rgb(color)]
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    return sum(a * b for a, b in zip(values, (.2126, .7152, .0722)))


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + .05) / (lo + .05)


def readable(color, backgrounds, minimum=4.5):
    """Smallest black/white blend that meets contrast on every supplied surface."""
    if all(contrast(color, bg) >= minimum for bg in backgrounds):
        return color
    for step in range(1, 101):
        candidates = [mix(color, target, step / 100) for target in ('#000000', '#ffffff')]
        for candidate in candidates:
            if all(contrast(candidate, bg) >= minimum for bg in backgrounds):
                return candidate
    return max(('#000000', '#ffffff'), key=lambda c: min(contrast(c, bg) for bg in backgrounds))


def derived_theme(key):
    _, mode, canvas, rail, body, accent = THEMES.get(key, THEMES['harbor'])
    dark = mode == 'dark'
    surface = mix(canvas, body, .045 if dark else .025)
    return dict(canvas=canvas, rail=rail, surface=surface, body=body,
        muted=mix(body, canvas, .25), accent=accent, accent_hover=mix(accent, body, .18),
        selection=mix(canvas, accent, .18 if dark else .12), border=mix(canvas, body, .28),
        border_soft=mix(canvas, body, .13), hover=mix(canvas, body, .075), control=surface,
        success='#9bd6b5' if dark else '#256348', warn='#eac67c' if dark else '#7a570d',
        error='#ffb2ab' if dark else '#a23636', error_surface=mix(canvas, '#ef6d78', .12),
        focus=accent, edge=mix(body, canvas, .30), scrollbar=mix(body, canvas, .45), disabled=mix(body, canvas, .25))
