"""Canonical reply evidence over existing records and Core reply relationships.

This is an evaluator input index, not a persisted classification or task model.
Legacy direct-edge policies and verified ancestry share this one graph reader.
"""
from collections import defaultdict
from datetime import datetime, timezone
import json

from .catalog import email_address

MAX_CHAIN = 256


def address(value):
    if isinstance(value, dict): value = value.get('address') or value.get('email')
    if not isinstance(value, str): return None
    try: return email_address(value[7:] if value[:7].casefold() == 'mailto:' else value)
    except (ValueError, TypeError): return None


def recipients(value):
    if not isinstance(value, list): return set(), False
    parsed = [address(item) for item in value]
    return {item for item in parsed if item}, all(item is not None for item in parsed)


def source_time(properties, additional):
    field = 'sent_at_utc' if additional.get('direction') == 'Outgoing' else 'received_at_utc'
    value = properties.get(field)
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else None
    except (ValueError, TypeError, AttributeError): return None


REASONS = {
    'reply_found': 'A reply is supported by the saved message links.',
    'no_reply_found': 'No reply was found in the available saved mail.',
    'identity_ambiguous': 'Confirm your email addresses to identify replies.',
    'sender_unknown': 'The saved sender is missing or ambiguous.',
    'direction_unknown': 'The saved message direction is unavailable.',
    'direction_conflict': 'The saved sender and mail folder disagree.',
    'mailbox_unknown': 'The saved message does not identify its mailbox.',
    'thread_gap': 'Some reply links are missing, ambiguous or cyclic.',
    'thread_limit': 'The reply chain exceeds the supported evidence bound.',
    'sent_unavailable': 'Saved Sent mail coverage is incomplete.',
    'inbox_unavailable': 'Saved Inbox mail coverage is incomplete.',
    'mailbox_coverage_unknown': 'Folder coverage does not establish coverage for each mailbox.',
}


class ReplyEvidence:
    def __init__(self, service):
        self.service = service
        self.selves = set(service.settings()['self_addresses'])
        self.coverage = service.coverage()
        self.records = {}
        self.parents = defaultdict(set)
        self.incoming = defaultdict(list)
        self.claims = defaultdict(list)
        self.responses = defaultdict(dict)
        self.groups = {}
        self.gaps = set()
        self.limits = set()
        self.mailboxes = set()
        # Read only evidence fields; large body text never enters this index.
        names = ('mailbox_id', 'conversation_id', 'in_reply_to', 'from', 'to', 'cc',
                 'sent_at_utc', 'received_at_utc')
        for raw in service.db.execute("SELECT record_id,properties_json,json_remove(added_json,'$.body_text') AS added_json FROM records WHERE record_type='Email' AND active=1 ORDER BY record_id"):
            props = json.loads(raw['properties_json'])
            props = {key: props[key] for key in names if key in props}
            added = json.loads(raw['added_json'])
            rid = raw['record_id']
            self.records[rid] = (props, added)
            mailbox = str(props.get('mailbox_id') or '').strip()
            conversation = str(props.get('conversation_id') or '').strip()
            if mailbox: self.mailboxes.add(mailbox)
            self.groups[rid] = (mailbox, conversation, '' if mailbox and conversation else rid)
        for edge in service.db.execute("SELECT source_id,target_id,owner,evidence_json FROM relationships WHERE predicate='replies_to' ORDER BY source_id,target_id,owner"):
            child, parent = edge['source_id'], edge['target_id']
            if child not in self.records or parent not in self.records: continue
            claim = {'owner': edge['owner'], 'evidence': json.loads(edge['evidence_json'])}
            self.claims[(child, parent)].append(claim)
            self.incoming[parent].append(child)
            cm, pm = self.groups[child][0], self.groups[parent][0]
            if not cm or cm.casefold() != pm.casefold():
                self.gaps.add(child)
                continue
            self.parents[child].add(parent)
        for rid, (props, _) in self.records.items():
            if (props.get('in_reply_to') and not self.parents[rid]) or len(self.parents[rid]) > 1:
                self.gaps.add(rid)
        for child in self.records:
            actor, _ = self.actor(child)
            seen = {child}; node = child; ancestors = []; valid = True
            for _ in range(MAX_CHAIN):
                parents = self.parents[node]
                if not parents: break
                if len(parents) != 1:
                    self.gaps.update(seen); valid = False; break
                parent = next(iter(parents))
                if parent in seen:
                    self.gaps.update(seen); valid = False; break
                seen.add(parent); ancestors.append(parent); node = parent
            else:
                self.limits.update(seen); valid = False
            if valid and actor in ('self', 'external'):
                for depth, parent in enumerate(ancestors):
                    self.responses[parent].setdefault(actor, (child, depth + 1))
        self.group_gaps = {self.groups[rid] for rid in self.gaps}
        self.group_limits = {self.groups[rid] for rid in self.limits}

    def actor(self, rid):
        if not self.selves: return None, 'identity_ambiguous'
        props, added = self.records[rid]
        sender = address(props.get('from')); direction = added.get('direction')
        if not sender: return None, 'sender_unknown'
        if direction not in ('Incoming', 'Outgoing'): return None, 'direction_unknown'
        own = sender in self.selves
        if (direction == 'Incoming' and own) or (direction == 'Outgoing' and not own):
            return None, 'direction_conflict'
        return ('self' if own else 'external'), None

    def edge(self, child, parent):
        return {'source_id': child, 'target_id': parent, 'predicate': 'replies_to',
                'authority': 'core', 'claims': self.claims[(child, parent)]}

    def observed(self, rid, *, policy='verified_ancestry'):
        if rid not in self.records:
            return {'state': 'not_evaluable', 'value': None, 'status': 'unknown',
                    'reason': 'This message is not current.', 'reason_code': 'inactive', 'evidence': []}
        props, added = self.records[rid]
        direction = added.get('direction')
        if policy in ('legacy_outgoing_direct', 'legacy_incoming_direct'):
            expected = 'Outgoing' if policy == 'legacy_outgoing_direct' else 'Incoming'
            found = [self.edge(child, rid) for child in dict.fromkeys(self.incoming[rid])
                     if str(self.records[child][1].get('direction', '')).casefold() == expected.casefold()]
            return {'state': 'evaluated', 'value': bool(found),
                    'status': 'replied' if found else 'no_reply_found', 'evidence': found,
                    'reason': REASONS['reply_found' if found else 'no_reply_found'],
                    'policy': policy}
        actor, failure = self.actor(rid)
        expected_actor = 'self' if actor == 'external' else 'external'
        response = self.responses[rid].get(expected_actor) if not failure else None
        evidence = []
        if response:
            child, depth = response
            for _ in range(depth):
                parent = next(iter(self.parents[child])); evidence.append(self.edge(child, parent)); child = parent
            return {'state': 'evaluated', 'value': True, 'status': 'replied',
                    'reason_code': 'reply_found', 'reason': REASONS['reply_found'],
                    'evidence': evidence, 'policy': policy}
        group = self.groups[rid]
        if not failure:
            if not group[0]: failure = 'mailbox_unknown'
            elif group in self.group_gaps: failure = 'thread_gap'
            elif group in self.group_limits: failure = 'thread_limit'
            elif self.coverage.get('MailSent' if direction == 'Incoming' else 'MailInbox') != 'complete':
                failure = 'sent_unavailable' if direction == 'Incoming' else 'inbox_unavailable'
            elif len(self.mailboxes) > 1: failure = 'mailbox_coverage_unknown'
        code = failure or 'no_reply_found'
        return {'state': 'not_evaluable' if failure else 'evaluated',
                'value': None if failure else False, 'status': 'unknown' if failure else 'no_reply_found',
                'reason_code': code, 'reason': REASONS[code], 'evidence': evidence,
                'policy': policy, 'coverage_scope': 'Available local captures; remote freshness is unknown.'}


def context(service):
    """One bounded graph index for the current canonical knowledge snapshot."""
    settings=service.settings()
    key = (service.epoch(),tuple(settings['self_addresses']),tuple(settings['group_addresses']),
           json.dumps(service.coverage(), sort_keys=True))
    cached = getattr(service, '_reply_evidence_context', None)
    if cached is None or cached[0] != key:
        cached = (key, ReplyEvidence(service)); service._reply_evidence_context = cached
    return cached[1]
