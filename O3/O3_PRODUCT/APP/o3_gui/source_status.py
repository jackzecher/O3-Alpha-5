"""Presentation of KM's recorded coverage; never inspect or certify live sources."""
FAMILIES = {'MailInbox': 'Inbox', 'MailSent': 'Sent', 'ContactsProfiles': 'Contacts',
            'CalendarICS': 'Calendars', 'DocumentCorpus': 'Documents'}
STATES = {'complete': 'available at last check', 'unavailable': 'unavailable at last check',
          'partial': 'partial at last check', 'unknown': 'not checked'}


def source_states(coverage, captured=None):
    local = dict((coverage or {}).get('local_status') or {})
    # KM calls a missing branch partial. The last explicit capture check supplies
    # the more specific directory observation, without probing live sources here.
    observed = dict((captured or {}).get('coverage') or [])
    for key, title in FAMILIES.items():
        name = 'Mail '+title if key.startswith('Mail') else title
        if local.get(key) in ('partial', 'unknown', None) and observed.get(name) is False:
            local[key] = 'unavailable'
    return local


def coverage_text(coverage, kind=None, view='all', captured=None):
    local = source_states(coverage, captured)
    if isinstance(kind, str) and kind.startswith('wsrt_'):
        return ''
    if kind == 'Email':
        keys = ['MailSent'] if view == 'view:email:sent' else ['MailInbox'] if view == 'view:email:inbox' else ['MailInbox', 'MailSent']
    else:
        keys = {'Person': ['ContactsProfiles'], 'Event': ['CalendarICS'],
                'Document': ['DocumentCorpus']}.get(kind, list(FAMILIES))
    groups = {}
    for key in keys:
        state = STATES.get(local.get(key), 'not checked')
        groups.setdefault(state, []).append(FAMILIES[key])
    return ' · '.join(', '.join(names) + ' ' + state for state, names in groups.items()) + '. Upstream freshness unknown.'
