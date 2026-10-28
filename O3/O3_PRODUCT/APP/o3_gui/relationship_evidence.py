"""Explain the supplied relationship without exposing implementation machinery."""
from html import escape
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QTextBrowser
from .ui import text, btn

PREDICATES = {'affiliated_with':'works at', 'member_of':'belongs to', 'part_of':'is part of',
    'sent_by':'was sent by', 'sent_to':'was sent to', 'copied_to':'was copied to',
    'authored_by':'was written by', 'attached_to':'is attached to',
    'associated_with':'mentions project', 'organized_by':'is organized by',
    'invites':'invites', 'replies_to':'replies to', 'ws:parent_command':'belongs to'}

def predicate_label(value):
    return PREDICATES.get(value, 'is connected to')

def authority_label(value):
    if value in {'workspace_manual', 'workspace_overlay', 'user'}: return 'Added by you'
    if value in {'core', 'core_bounded_traversal', 'core_traversal'}: return 'Found in your saved material'
    if value == 'source_scoped_group': return 'Messages in the same saved conversation'
    return 'Saved information'

def context_summary(data, labels):
    def name(rid): return labels.get(rid, {}).get('label') or 'An item whose name is unavailable'
    if not isinstance(data, dict): return [('About this connection', 'No additional details were included.')]
    source = data.get('source_id') or data.get('source')
    target = data.get('target_id') or data.get('target')
    rows = []
    if source and target:
        rows.append(('Connection', name(source) + ' → ' + predicate_label(data.get('predicate')) + ' → ' + name(target)))
        rows.append(('Where it came from', authority_label(data.get('authority'))))
        claims = data.get('claims') or data.get('evidence') or []
        if isinstance(claims, dict): claims = [claims]
        if not isinstance(claims, list): claims = []
        rows.append(('Supporting details', (str(len(claims)) + (' detail was included.' if len(claims) == 1 else ' details were included.')) if claims else 'No additional source details were included.'))
        fields = {'Person.companyName':'Organization named in the person profile',
                  'Person.companyName+department':'Organization and department named in the person profile',
                  'sender':'Sender of the message', 'from':'Sender of the message',
                  'to':'Recipient of the message', 'cc':'Person copied on the message'}
        for claim in claims[:8]:
            if not isinstance(claim, dict): continue
            evidence = claim.get('evidence', claim)
            if isinstance(evidence, dict):
                field, rid = evidence.get('field'), evidence.get('source_record')
                if field: rows.append(('In the original item', fields.get(field, 'A detail saved with the original item')))
                if rid: rows.append(('Supporting item', name(rid)))
        if data.get('authority') in {'workspace_manual', 'workspace_overlay', 'user'}:
            rows.append(('Your connection', 'This connection was added in O3. It does not change the original material.'))
    elif 'messages' in data:
        rows = [('Conversation', 'These saved messages belong to the same conversation in one mailbox.'),
                ('Saved messages', str(data.get('count', len(data['messages'])))),
                ('What O3 can tell', 'Only messages saved in O3 are included. Other messages may be missing.')]
    elif data.get('grouping'):
        rows = [('Why this appears', 'These items are shown together by the choices in this view.'),
                ('What it means', 'Showing items together does not create a connection between them.')]
    elif data.get('record_id'):
        rows = [('Item', name(data['record_id'])), ('Where it came from', authority_label(data.get('authority'))),
                ('About this item', 'This item is saved in O3 and included in the selected view. Open the item to see its original material.')]
        if data.get('paths') or data.get('support_paths'):
            rows.append(('Related items', 'Saved connections were used to find this item.'))
    else:
        rows = [('About this connection', 'No additional explanation was included with this result.')]
    return rows

class RelationshipEvidence(QDialog):
    def __init__(self, title, data, labels, parent=None):
        super().__init__(parent)
        self.evidence = data  # Original evidence remains intact for diagnostics/export.
        self.setWindowTitle('Why this connection?'); self.resize(740, 530)
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 22, 24, 20); layout.setSpacing(12)
        layout.addWidget(text(title, 'section', True))
        self.summary = QTextBrowser(); self.summary.setOpenLinks(False)
        self.summary.setAccessibleName('Connection and supporting details')
        self.summary.setHtml(''.join('<p><b>' + escape(key) + '</b><br>' + escape(str(value)) + '</p>' for key, value in context_summary(data, labels)))
        layout.addWidget(self.summary, 1)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(btn('Close', self.accept)); layout.addLayout(row)

