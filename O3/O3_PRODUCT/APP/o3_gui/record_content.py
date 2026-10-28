"""Read-only presentation of accepted records and archived document text.

This module describes existing values. It does not evaluate rules, create
relationships, normalize identity, write SQLite, or read live source folders.
The engine-owning worker alone calls record_content().
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from o3_gui.logic import MAX_PREVIEW
from o3_core.rules.document import MAX_CORPUS_BYTES

MISSING = object()


def display_value(value=MISSING) -> str:
    """Keep missing, null, empty, false and zero visibly distinct."""
    if value is MISSING or value is None:
        return 'Not supplied'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, str):
        return value if value.strip() else 'Empty'
    if isinstance(value, (list, tuple)):
        return '\n'.join(display_value(v) for v in value) if value else 'No values supplied'
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False) if value else 'Empty'
    return str(value)


def address_value(value=MISSING) -> str:
    if isinstance(value, list):
        return '\n'.join(address_value(v) for v in value) if value else 'No recipients supplied'
    if not isinstance(value, dict):
        return display_value(value)
    params = value.get('params') if isinstance(value.get('params'), dict) else {}
    name = value.get('name') or params.get('CN')
    address = value.get('address') or value.get('raw')
    if name and address and name != address:
        return f'{name} <{address}>'
    return display_value(address or name)


def source_time(value=MISSING) -> str:
    """Format an existing timestamp without assigning a missing timezone."""
    if not isinstance(value, str) or not value.strip():
        return display_value(value)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return value
    if 'T' not in value and ' ' not in value:
        return value
    text = parsed.strftime('%Y-%m-%d  %H:%M:%S')
    offset = parsed.utcoffset()
    if offset is None:
        return text + ' · timezone not supplied'
    if offset.total_seconds() == 0:
        return text + ' UTC'
    return text + ' ' + parsed.strftime('%z')


def event_time(value=MISSING) -> str:
    if not isinstance(value, dict):
        return display_value(value)
    local = value.get('local', MISSING)
    if value.get('kind') == 'date':
        return display_value(local) + ' · date only'
    zone = value.get('timezone')
    if zone == 'UTC':
        return source_time(value.get('utc') or local)
    text = display_value(local).replace('T', '  ')
    if zone == 'floating':
        return text + ' · floating local time; UTC not assigned'
    if zone:
        text += ' · ' + str(zone)
        if not value.get('utc'):
            text += '; UTC not assigned'
    else:
        text += ' · timezone not supplied'
    return text


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def connection_caption(kind: str, edge: dict) -> str:
    """Readable captions for stored predicates; direction is never inferred."""
    if edge.get('authority')!='core' and edge.get('label'):return edge['label']
    key = (kind, edge.get('predicate'), edge.get('direction'))
    labels = {
        ('Organization', 'part_of', 'outgoing'): 'Parent organization',
        ('Organization', 'part_of', 'incoming'): 'Child organization',
        ('Organization', 'affiliated_with', 'incoming'): 'Affiliated person',
        ('Organization', 'member_of', 'incoming'): 'Member',
        ('Person', 'affiliated_with', 'outgoing'): 'Organization',
        ('Person', 'member_of', 'outgoing'): 'Department',
        ('Email', 'sent_by', 'outgoing'): 'Sender',
        ('Email', 'sent_to', 'outgoing'): 'Recipient',
        ('Email', 'copied_to', 'outgoing'): 'Cc recipient',
        ('Email', 'attached_to', 'incoming'): 'Attachment',
        ('Email', 'replies_to', 'incoming'): 'Replies to this email',
        ('Email', 'replies_to', 'outgoing'): 'Replies to',
        ('Event', 'organized_by', 'outgoing'): 'Organizer',
        ('Event', 'invites', 'outgoing'): 'Invited person',
        ('Event', 'exception_of', 'outgoing'): 'Recurring series',
        ('Event', 'exception_of', 'incoming'): 'Stored exception',
        ('Document', 'attached_to', 'outgoing'): 'Source email',
        ('Document', 'authored_by', 'outgoing'): 'Author',
        ('LineOfEffort', 'associated_with', 'incoming'): 'Supporting record',
    }
    if key in labels:
        return labels[key]
    if edge.get('predicate') == 'associated_with' and edge.get('direction') == 'outgoing':
        return 'Referenced Line of Effort'
    return str(edge.get('direction', '')).capitalize() + ' · ' + str(edge.get('predicate', '')).replace('_', ' ')


def overview_data(detail: dict) -> dict:
    """One summary contract shared by all six native record presentations."""
    p = _mapping(detail.get('properties'))
    a = _mapping(detail.get('added'))
    kind = detail.get('kind')
    result = {'sections': [], 'after_content': [], 'notices': [], 'content_title': '',
              'content_text': '', 'content_note': '', 'needs_content': False}
    sections = result['sections']
    if detail.get('active') is False:
        result['notices'].append('Retained inactive record. Its last stored information remains available.')
    if kind == 'Email':
        sections.append(('Correspondence', [
            ('From', address_value(p.get('from', MISSING))),
            ('To', address_value(p.get('to', MISSING))),
            ('Cc', address_value(p.get('cc', MISSING))),
            ('Folder', display_value(p.get('source_folder', MISSING))),
            ('Direction', display_value(a.get('direction', MISSING))),
            ('Sent', source_time(p.get('sent_at_utc', MISSING))),
            ('Received', source_time(p.get('received_at_utc', MISSING))),
            ('Importance', display_value(p.get('importance', MISSING))),
        ]))
        attachments = p.get('attachments', MISSING)
        rows = []
        if isinstance(attachments, list):
            for index, attachment in enumerate(attachments):
                attachment = _mapping(attachment)
                parts = [display_value(attachment.get('name', MISSING))]
                if attachment.get('content_type'):
                    parts.append(str(attachment['content_type']))
                if attachment.get('size_bytes') is not None:
                    parts.append(f"{display_value(attachment['size_bytes'])} bytes")
                rows.append((f'Attachment {index + 1}', '\n'.join(parts)))
            if not attachments:
                rows.append(('Metadata', 'No attachment entries supplied'))
        else:
            rows.append(('Metadata', display_value(attachments)))
        if p.get('has_attachments') is True and (attachments is MISSING or not attachments):
            result['notices'].append('The source reports attachments; attachment metadata was not supplied.')
        if rows and (attachments is not MISSING or p.get('has_attachments') is not None):
            result['after_content'].append(('Attachments', rows))
        result.update(content_title='Message', content_text=display_value(a.get('body_text', MISSING)),
                      content_note='Stored plain text', needs_content=True)
    elif kind == 'Event':
        sections.append(('Schedule', [
            ('Starts', event_time(p.get('dtstart', MISSING))),
            ('Ends (exclusive)' if a.get('all_day') is True else 'Ends',
             event_time(p.get('dtend', MISSING)) if 'dtend' in p else source_time(a.get('end_utc', MISSING))),
            ('All day', display_value(a.get('all_day', MISSING))),
            ('Location', display_value(p.get('location', MISSING))),
            ('Status', display_value(p.get('status', MISSING))),
            ('Recurrence', display_value(a.get('recurrence_role', MISSING))),
        ]))
        sections.append(('Participants', [
            ('Organizer', address_value(p.get('organizer', MISSING))),
            ('Attendees', address_value(p.get('attendee', MISSING))),
        ]))
        for key in ('dtstart', 'dtend'):
            value = _mapping(p.get(key))
            if value.get('kind') != 'date' and value.get('local') and not value.get('utc'):
                note = 'Source local time is retained; the stored record has no resolved UTC value.'
                if note not in result['notices']:
                    result['notices'].append(note)
        result.update(content_title='Description', content_text=display_value(p.get('description', MISSING)))
    elif kind == 'Person':
        sections.append(('Contact', [
            ('Known addresses', display_value(a.get('addresses', MISSING))),
            ('Mail', display_value(p.get('mail', MISSING))),
            ('Account address', display_value(p.get('userPrincipalName', MISSING))),
            ('Job title', display_value(p.get('jobTitle', MISSING))),
            ('Organization', display_value(p.get('companyName', MISSING))),
            ('Department', display_value(p.get('department', MISSING))),
            ('Office', display_value(p.get('officeLocation', MISSING))),
            ('Business phone', display_value(p.get('businessPhones', MISSING))),
            ('Mobile phone', display_value(p.get('mobilePhone', MISSING))),
        ]))
        location = [(label, display_value(p[key])) for key, label in (
            ('streetAddress', 'Street'), ('city', 'City'), ('state', 'State / region'),
            ('postalCode', 'Postal code'), ('country', 'Country')) if key in p]
        if location:
            sections.append(('Location', location))
    elif kind == 'Document':
        src = _mapping(p.get('source')); meta = _mapping(p.get('metadata'))
        extraction = _mapping(p.get('extraction')); origin = _mapping(p.get('origin'))
        sections.append(('Document', [
            ('Filename', display_value(src.get('filename', MISSING))),
            ('File type', display_value(src.get('file_type', MISSING))),
            ('Origin', display_value(origin.get('type', MISSING)).replace('_', ' ')),
            ('Authors', display_value(meta.get('authors', MISSING))),
            ('Extraction', display_value(extraction.get('status', MISSING))),
            ('Source modified', source_time(src.get('modified_utc', MISSING))),
        ]))
        if extraction.get('status') in ('partial', 'failed'):
            result['notices'].append('Source extraction is ' + extraction['status'] + '; available content may be incomplete.')
        if extraction.get('warnings'):
            sections.append(('Extraction notes', [('Warnings', display_value(extraction['warnings']))]))
        result.update(content_title='Extracted content', content_text='Loading verified archived text…',
                      content_note='The original file is represented by its supplied document corpus.', needs_content=True)
    elif kind == 'Organization':
        if detail.get('target_kind')=='WorkspaceOrganization':
            raw=detail.get('km_record',{})
            sections.append(('Workspace identity',[('Display name',display_value(raw.get('display_name',detail.get('title',MISSING)))),
                ('Explicit Core members',display_value(raw.get('core_org_ids',MISSING)))]))
            if p:sections.append(('Manual Workspace fields',[(str(key),display_value(value)) for key,value in p.items()]))
        else:sections.append(('Organization', [('Kind', display_value(p.get('kind', MISSING)))]))
        result['notices'].append('User-authored effective identity. Raw Core members and evidence remain unchanged.' if detail.get('target_kind')=='WorkspaceOrganization' else 'Derived from supported Person fields or explicit Document issuer evidence.')
    elif kind == 'LineOfEffort':
        basis = p.get('identity_basis') or detail.get('basis', MISSING)
        basis_text = {'scoped_reference_code': 'Explicit reference code',
                      'scoped_explicit_label': 'Explicit label · provisional identity'}.get(basis, display_value(basis))
        sections.append(('Line of Effort', [
            ('Kind', display_value(p.get('kind', MISSING))),
            ('Reference', display_value(p.get('reference_code', MISSING))),
            ('Identity', basis_text),
        ]))
        if basis in ('user_created','user_adopted'):
            sections.append(('User-authored objective', [('Objective', display_value(p.get('objective', MISSING)))]))
            result['notices'].append('User-authored Core LOE. Source references support connections, not ownership.')
        else:
            result['notices'].append('Retained legacy-derived LOE with historical source evidence.')
    elif isinstance(kind,str) and kind.startswith('wsrt_'):
        sections.append(('Workspace record', [('Title',display_value(p.get('title',detail.get('title',MISSING)))),
                           ('Created',source_time(p.get('created_at_utc',MISSING))),
                           ('Updated',source_time(p.get('updated_at_utc',MISSING)))]))
        values=detail.get('workspace_properties',{})
        if values:
            sections.append(('Configured properties',[(str(key),display_value(value.get('value')))
                for key,value in values.items() if isinstance(value,dict)]))
        result['notices'].append('User-authored Workspace record. References do not assert new Core relationships.')
    else:
        result['notices'].append('A readable summary is unavailable for this record type. Full properties remain in Technical fields.')
    return result


def _element_text(element: dict) -> str | None:
    """Project supplied text, including simple corpus table rows; no conversion."""
    if isinstance(element.get('text'), str):
        return element['text']
    rows = element.get('rows')
    if isinstance(rows, list):
        rendered = []
        for row in rows:
            cells = row.get('cells') if isinstance(row, dict) else row
            if not isinstance(cells, list):
                return None
            values = []
            for cell in cells:
                if isinstance(cell, dict):
                    cell = cell.get('text', cell.get('value', MISSING))
                if isinstance(cell, (str, int, float, bool)) or cell is None:
                    values.append('' if cell is None else str(cell))
                else:
                    return None
            rendered.append(' | '.join(values))
        return '\n'.join(rendered)
    return None


def _page_segments(segments, offset, limit):
    """Slice a text stream without constructing a second copy of a large corpus."""
    pieces = []; position = 0; end = offset + limit; first = True
    for segment in segments:
        if not first:
            separator_end = position + 2
            if position < end and separator_end > offset:
                pieces.append('\n\n'[max(0, offset-position):min(2, end-position)])
            position = separator_end
        first = False
        segment_end = position + len(segment)
        if position < end and segment_end > offset:
            pieces.append(segment[max(0, offset-position):min(len(segment), end-position)])
        position = segment_end
    next_offset = end if end < position else None
    return ''.join(pieces), position, next_offset


def record_content(engine, rid: str, *, offset=0, limit=MAX_PREVIEW) -> dict:
    """A bounded page of full accepted content, with correlated record identity.

    Consumers may append the next page only while both record ID and source_sha
    match the first response. Content stays in the engine-owned worker.
    """
    try:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200000:
            return {'id': rid, 'text': '', 'status': 'error', 'truncated': False,
                    'notice': 'The requested content range is invalid.'}
        return _record_content(engine, rid, offset=offset, limit=limit)
    except Exception as exc:
        return {'id': rid, 'text': '', 'status': 'error', 'truncated': False,
                'notice': f'Readable content could not be loaded ({type(exc).__name__}).'}


def _record_content(engine, rid: str, *, offset=0, limit=MAX_PREVIEW) -> dict:
    """Bounded pages from authoritative Email values or a verified archive.

    Every accepted text character remains reachable by next_offset. Errors never
    substitute a different source, a live file, or an unrelated selected record.
    """
    row = engine.store.row(rid)
    base = {'id': rid, 'text': '', 'status': 'unavailable', 'notice': '', 'truncated': False,
            'offset': offset, 'next_offset': None, 'total_characters': 0, 'has_more': False}
    if row is None:
        return {**base, 'status': 'error', 'notice': 'Record is no longer available.'}
    base['source_sha'] = row['source_sha']
    if row['record_type'] == 'Email':
        added = json.loads(row['added_json'])
        text = added.get('body_text')
        if text is None:
            return {**base, 'status': 'not_supplied', 'notice': 'Message body text was not supplied.'}
        if not isinstance(text, str):
            return {**base, 'status': 'error', 'notice': 'Stored body text has an unsupported value type.'}
        shown, total, next_offset = _page_segments((text,), offset, limit)
        more = next_offset is not None
        note = (f'Stored plain text · characters {offset+1:,}–{offset+len(shown):,} of {total:,}. Load more to continue.'
                if more else 'Stored plain text' if text else 'The supplied message body is empty.')
        return {**base, 'text': shown, 'truncated': more, 'has_more': more,
                'next_offset': next_offset, 'total_characters': total,
                'status': 'paged' if more else 'available' if text else 'empty', 'notice': note}
    if row['record_type'] != 'Document':
        return {**base, 'status': 'not_applicable', 'notice': 'This record has no document-text preview.'}
    version = engine.store.db.execute(
        'SELECT archive_relpath,source_sha FROM record_versions WHERE record_id=? AND fingerprint=?',
        (rid, row['fingerprint'])).fetchone()
    if version is None:
        return {**base, 'notice': 'The matching archived source version is unavailable.'}
    archive = (engine.store.root / version['archive_relpath']).resolve()
    allowed = (engine.store.root / 'archive').resolve()
    if not archive.is_relative_to(allowed) or not archive.is_file():
        return {**base, 'notice': 'Archived source is unavailable or outside the authorized archive.'}
    try:
        if archive.stat().st_size > MAX_CORPUS_BYTES:
            return {**base, 'notice': 'Archived corpus exceeds the supported preview size.'}
        raw = archive.read_bytes()
        if hashlib.sha256(raw).hexdigest() != version['source_sha']:
            return {**base, 'status': 'error', 'notice': 'Archive checksum verification failed; content is blocked.'}
        corpus = json.loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return {**base, 'status': 'error', 'notice': 'Archived document text could not be read.'}
    if not isinstance(corpus, dict):
        return {**base, 'status': 'error', 'notice': 'Archived corpus has an unsupported structure.'}
    content = _mapping(corpus.get('content')); units = content.get('units')
    if not isinstance(units, list):
        return {**base, 'status': 'not_supplied', 'notice': 'Structured content was not supplied in the corpus.'}
    unsupported = [0]
    def segments():
        for unit in units:
            if not isinstance(unit, dict) or not isinstance(unit.get('elements'), list):
                unsupported[0] += 1; continue
            name = unit.get('name') or unit.get('title')
            if name: yield str(name)
            for element in unit['elements']:
                if not isinstance(element, dict):
                    unsupported[0] += 1; continue
                text = _element_text(element)
                if text is None:
                    unsupported[0] += 1; continue
                yield text
    text, total, next_offset = _page_segments(segments(), offset, limit)
    more = next_offset is not None
    extraction_info = _mapping(corpus.get('extraction')); extraction = extraction_info.get('status')
    warnings = extraction_info.get('warnings') or []
    if not isinstance(warnings, list): warnings = [warnings]
    notes = ['Archived corpus verified']
    if extraction: notes.append('source extraction: ' + str(extraction))
    if more: notes.append(f'characters {offset+1:,}–{offset+len(text):,} of {total:,}; load more to continue')
    if unsupported[0]: notes.append('some structured elements have no text representation; inspect the archived source')
    if not total: notes.append('no readable text supplied')
    return {**base, 'text': text, 'truncated': more, 'has_more': more,
            'next_offset': next_offset, 'total_characters': total, 'extraction_status': extraction,
            'extraction_warnings': warnings,
            'status': 'paged' if more else 'partial' if unsupported[0] else 'available' if total else 'empty',
            'notice': ' · '.join(notes) + '.'}
