"""Thin read-only projection, validated paths and small GUI preferences.

Uses the ONE existing SQLite database; never persists a display model or
changes deterministic identities, rules, RDF ontology or O3Data inputs.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import hashlib
import re
from o3_gui.navigation import preferences as navigation_preferences

from o3_core.store import KINDS, digest
from o3_gui.display_preferences import validate_display_preferences, project_list_fields

TYPE_NAMES = {
    'Email':'Mail', 'Event':'Calendar', 'Person':'People',
    'Document':'Documents', 'Organization':'Organizations', 'LineOfEffort':'Projects',
}
SOURCE_FOLDERS = (
    ('Mail Inbox','Mail/Inbox'), ('Mail Sent','Mail/Sent'),
    ('Contacts','Contacts/Profiles'), ('Calendars','Calendars'),
    ('Documents','Documents'),
)
LIMIT = 200
MAX_PREVIEW = 20000

def short_json(value, maximum=MAX_PREVIEW) -> str:
    """Readable, bounded source preview; never execute or render source HTML."""
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return text if len(text) <= maximum else text[:maximum] + f'\n[Preview truncated at {maximum:,} characters.]'

def is_relative(child: Path, parent: Path) -> bool:
    return child == parent or child.is_relative_to(parent)


def assert_separate(path: Path, state: Path, source: Path | None=None) -> Path:
    """Reject overlap in BOTH directions; also reject paths under an input root."""
    p=Path(path).expanduser().resolve()
    s=Path(state).expanduser().resolve()
    if is_relative(p,s) or is_relative(s,p):
        raise ValueError('This location overlaps the O3 managed state directory')
    if source is not None:
        r=Path(source).expanduser().resolve()
        if is_relative(p,r) or is_relative(r,p):
            raise ValueError('This location overlaps read-only O3Data sources')
    return p


def validate_source(root: Path, state: Path) -> tuple[Path,list[tuple[str,bool]]]:
    given=Path(root).expanduser()
    if not given.is_dir() or given.is_symlink():
        raise ValueError('Select an existing, locally accessible O3Data folder (not a cloud URL or link)')
    source=assert_separate(given, state)
    # Check approved subpaths without traversing symlinks or changing the input.
    coverage=[]
    for label,sub in SOURCE_FOLDERS:
        part=source
        for component in Path(sub).parts:
            part=part/component
            if part.is_symlink():
                raise ValueError('Source symlink or cloud placeholder is unsupported: '+label)
        if part.exists() and not part.is_dir():
            raise ValueError('Source path is not a directory: '+label)
        if part.is_dir():
            try:
                with os.scandir(part) as it: next(it,None)
            except OSError as exc:
                raise ValueError('Source folder is not readable: '+label) from exc
        coverage.append((label,part.is_dir()))
    if not any(available for _,available in coverage):
        raise ValueError('Selected folder has none of the supported O3Data source folders')
    return source,coverage


def discover_sources() -> list[Path]:
    """Bounded, permission-respecting discovery. Never choose automatically."""
    bases=[]
    for name in ('OneDriveCommercial','OneDrive','USERPROFILE'):
        value=os.environ.get(name)
        if value: bases.append(Path(value))
    seen=set(); result=[]
    for base in bases:
        for p in (base/'O3Data',base/'Documents'/'O3Data'):
            if str(p).casefold() in seen: continue
            seen.add(str(p).casefold())
            if p.is_dir() and not p.is_symlink():result.append(p)
    return result


def retained_display_preferences(raw):
    """Validate supported preferences while preserving unrecognized saved fields."""
    checked=validate_display_preferences(raw)
    def merge(old,new):
        result=dict(old) if isinstance(old,dict) else {}
        for key,value in new.items():
            result[key]=merge(result.get(key),value) if isinstance(value,dict) else value
        return result
    return merge(raw,checked)


def retained_navigation_preferences(raw, previous=None):
    """Retain passive extension fields without replaying unchecked navigation."""
    old=dict(previous) if isinstance(previous,dict) else {}
    supplied=dict(raw) if isinstance(raw,dict) else {}
    combined={**old,**supplied}
    prior_last=old.get('last') if isinstance(old.get('last'),dict) else {}
    supplied_last=supplied.get('last') if isinstance(supplied.get('last'),dict) else {}
    original_last={**prior_last,**supplied_last}
    combined['last']=original_last
    checked=navigation_preferences(combined)
    result={**combined,**checked}
    result['last']={**original_last,**checked['last']}
    # These tokens are transient even when a newer producer supplied them.
    for key in ('cursor','next_cursor','as_of','snapshot'):
        result.pop(key,None);result['last'].pop(key,None)
    return result


def read_config(state: Path) -> dict:
    p=Path(state)/'workspace.json'
    if not p.exists():return {}
    obj=json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(obj,dict):raise ValueError('Invalid workspace configuration')
    # Preserve extension and older presentation preferences losslessly. Validators
    # below own only their known keys; an unfamiliar setting is not disposable.
    config = dict(obj)
    config['display'] = retained_display_preferences(obj.get('display'))
    config['navigation'] = retained_navigation_preferences(obj.get('navigation'))
    return config


def save_config(state: Path, config: dict) -> None:
    root=Path(state)
    root.mkdir(parents=True, exist_ok=True)
    previous = root/'workspace.json'
    data = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else {}
    if not isinstance(data,dict): raise ValueError('Invalid workspace configuration')
    previous_navigation=data.get('navigation')
    data.update(config)
    data['display'] = retained_display_preferences(config.get('display'))
    data['navigation'] = retained_navigation_preferences(config.get('navigation'),previous_navigation)
    tmp=None
    try:
        with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=root,
                                         prefix='.o3-preferences-',suffix='.tmp',delete=False) as f:
            tmp=Path(f.name)
            json.dump(data,f,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,root/'workspace.json')
    finally:
        if tmp is not None:tmp.unlink(missing_ok=True)


def title_for(row: dict, properties=None, added=None) -> str:
    p=json.loads(row['properties_json']) if properties is None else properties
    a=json.loads(row['added_json']) if added is None else added
    kind=row['record_type']
    raw={
        'Email': p.get('subject'), 'Event': p.get('summary'),
        'Person': a.get('display_label') or p.get('displayName'),
        'Document':a.get('display_title') or p.get('metadata',{}).get('title') or p.get('source',{}).get('filename'),
        'Organization':p.get('name'), 'LineOfEffort':p.get('name')
    }.get(kind)
    return str(raw).strip() if raw else str(row['record_id'])


def subtitle_for(row: dict, properties=None, added=None) -> str:
    p=json.loads(row['properties_json']) if properties is None else properties
    a=json.loads(row['added_json']) if added is None else added
    kind=row['record_type']
    if kind=='Email':
        sender=p.get('from') or {}
        return str((sender.get('address') if isinstance(sender,dict) else sender) or a.get('direction') or 'Sender not supplied')
    if kind=='Event':return str(a.get('start_utc') or p.get('dtstart') or p.get('start') or 'Date not supplied')
    if kind=='Person':return str(p.get('jobTitle') or p.get('companyName') or 'Profile')
    if kind=='Document':return str((p.get('source') or {}).get('file_type') or (p.get('extraction') or {}).get('status') or 'Document')
    if kind=='Organization':return str(p.get('kind') or 'Organization')
    if kind=='LineOfEffort':return str(p.get('reference_code') or p.get('kind') or 'LOE')
    return ''


def summarize(engine) -> dict:
    counts={kind:0 for kind in KINDS}
    for kind,count in engine.store.db.execute('SELECT record_type,COUNT(*) FROM records WHERE active=1 GROUP BY record_type'):
        counts[kind]=count
    return {'counts':counts,'relationships':len(engine.store.active_relations()),
            'versions':engine.store.db.execute('SELECT COUNT(*) FROM record_versions').fetchone()[0],
            'last_scan':None}


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag.casefold() in ('script','style','head','svg','iframe'):self.skip+=1
        if tag in ('p','br','div','li','tr'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag.casefold() in ('script','style','head','svg','iframe') and self.skip:self.skip-=1
        if tag in ('p','div','li','tr'):self.parts.append('\n')
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)


def archive_inspection(engine, rid: str, source_sha: str | None = None) -> dict:
    """Inspect an exact, record-owned archive observation. Never accept a UI path.

    Hash the same bytes used for display, with bounded memory. A verified blob
    is not a claim that its source is current or its entire record is valid.
    """
    if source_sha is not None and (not isinstance(source_sha, str) or re.fullmatch(r'[0-9a-f]{64}', source_sha) is None):
        raise ValueError('Invalid archived source checksum')
    where = 'record_id=?' + (' AND source_sha=?' if source_sha is not None else '')
    args = (rid, source_sha) if source_sha is not None else (rid,)
    row = engine.store.db.execute('SELECT archive_relpath,source_sha FROM record_versions WHERE '+where+
                                  ' ORDER BY seen_at DESC,source_sha LIMIT 1', args).fetchone()
    if row is None:
        return {'id': rid, 'status': 'unavailable', 'source_sha': source_sha,
                'text': 'No archived source version is associated with this record and checksum.'}
    given = engine.store.root / row['archive_relpath']
    allowed = engine.store.root / 'archive'
    try:
        archive = given.resolve(strict=True)
        if (not archive.is_relative_to(allowed.resolve()) or not archive.is_file()
                or any(item.is_symlink() for item in (given, *given.parents))):
            raise ValueError('Archive path is unavailable or unsafe')
        hasher = hashlib.sha256(); retained = bytearray(); count = 0; budget = 4*1024*1024
        with archive.open('rb') as stream:
            for chunk in iter(lambda: stream.read(65536), b''):
                hasher.update(chunk); count += len(chunk)
                if len(retained) < budget: retained.extend(chunk[:budget-len(retained)])
    except (OSError, ValueError):
        return {'id': rid, 'status': 'unavailable', 'source_sha': row['source_sha'],
                'text': 'Archived source is unavailable or outside the authorized archive.'}
    if hasher.hexdigest() != row['source_sha']:
        return {'id': rid, 'status': 'checksum_mismatch', 'source_sha': row['source_sha'],
                'text': 'ERROR: archive checksum verification failed; source preview blocked.'}
    entry = engine.store.row(rid)
    text = retained.decode('utf-8-sig', 'replace')
    if count > budget:
        text = text[:MAX_PREVIEW] + '\n[Verified full checksum; bounded plain-text source excerpt only.]'
    elif entry['record_type'] != 'Event':
        try: doc = json.loads(text)
        except (ValueError, RecursionError):
            return {'id': rid, 'status': 'decode_failed', 'source_sha': row['source_sha'],
                    'text': 'Archive checksum verified, but the JSON cannot be decoded.'}
        if entry['record_type'] == 'Email' and isinstance(doc, dict):
            raw_html = doc.pop('body_html', '')
            if isinstance(raw_html, str):
                parser = _TextOnly()
                try: parser.feed(raw_html)
                except ValueError: pass
                doc['body_text_only'] = ''.join(parser.parts)[:MAX_PREVIEW]
        text = short_json(doc, MAX_PREVIEW)
    if len(text) > MAX_PREVIEW:
        text = text[:MAX_PREVIEW] + f'\n[Preview truncated at {MAX_PREVIEW:,} characters.]'
    return {'id': rid, 'status': 'verified', 'source_sha': row['source_sha'], 'text': text}


def archived_preview(engine, rid: str) -> str:
    """Compatibility entry; the live evidence lens uses the structured status."""
    return archive_inspection(engine, rid)['text']
