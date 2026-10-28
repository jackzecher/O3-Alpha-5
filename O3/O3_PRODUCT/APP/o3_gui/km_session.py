"""Native presentation boundary around ONE thread-owned Core/KM session.

No evaluator, rules catalog or operational persistence is duplicated here. This
module is Qt-independent so lifecycle and GUI request paths are testable headless.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from o3_core.engine import Engine, Settings
from o3_km import (KMService, initialize_workspace_v4, migrate_v3_to_v4, initialize_workspace, migrate_v2_to_v3,
                   migrate_v3_to_configurable_workspace, restore_backup, verify_backup)
from o3_km.migration import (validate_core_schema, validate_schema3,
                             LEGACY_KM10_CATALOG_FINGERPRINT, ALPHA4_CATALOG_FINGERPRINT)
from o3_km.catalog import CATALOG_FINGERPRINT
from o3_km.workspace_types import validate_v4,is_type_id
from o3_km.service import VERSION
from o3_km.upgrade_control import interruptible_sqlite
from o3_gui.logic import (summarize, validate_source, read_config, title_for,
                           subtitle_for, assert_separate, archived_preview, archive_inspection, SOURCE_FOLDERS)
from o3_gui.display_preferences import project_list_fields
from o3_gui.record_content import record_content


class SessionError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f'{code}: {message}')


def local_actor() -> str:
    """Use the OS account, not editable environment variables or source identities."""
    if os.name == 'nt':
        size = ctypes.c_ulong(257)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            raise SessionError('ACTOR_UNAVAILABLE', 'Windows account identity could not be verified')
        identity = buffer.value
    else:
        import pwd
        identity = pwd.getpwuid(os.getuid()).pw_name
    if not identity or len(identity) > 120:
        raise SessionError('ACTOR_UNAVAILABLE', 'Local account identity is unavailable')
    return 'local:' + identity


def probe_state(state: Path) -> dict:
    """Read-only startup preflight; never let Store silently migrate v1."""
    state = Path(state)
    dbpath = state / 'o3.sqlite'
    if state.is_symlink() or dbpath.is_symlink():
        raise SessionError('STATE_PATH_UNSAFE', 'Linked state is not supported')
    if not dbpath.exists():
        return {'schema': None, 'mode': 'new'}
    with closing(sqlite3.connect(dbpath.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (2, 3, 4):
                raise SessionError('STATE_SCHEMA_UNSUPPORTED', 'State requires a separately validated upgrade')
            validate_core_schema(db)
            scopes = db.execute("SELECT value FROM engine_meta WHERE key='identity_scopes'").fetchone()
            if not scopes:
                raise SessionError('STATE_IDENTITY_MISSING', 'Saved identity scopes are missing')
            result = {'schema': version, 'settings': json.loads(scopes[0]), 'mode': 'ready'}
            if version == 2:
                result['mode'] = 'migration_required'
            elif version==4:
                validate_v4(db, allow_alpha4=True)
                result['catalog'] = db.execute("SELECT value FROM engine_meta WHERE key='km_schema_contract'").fetchone()[0]
            else:
                fingerprint = db.execute("SELECT value FROM engine_meta WHERE key='km_schema_contract'").fetchone()
                if fingerprint and fingerprint[0] == LEGACY_KM10_CATALOG_FINGERPRINT:
                    result['mode'] = 'catalog_migration_required'
                else:
                    validate_schema3(db, allow_alpha4=True)
                    result['mode'] = 'workspace_type_migration_required'
            return result
        except sqlite3.DatabaseError:
            raise SessionError('STATE_INTEGRITY_FAILED', 'Cannot validate the saved database') from None


def display_row(record: dict) -> dict:
    """Shape trusted KM data for existing native columns; no inclusion logic."""
    kind = record.get('record_type', 'Organization')
    if kind == 'WorkspaceOrganization': kind = 'Organization'
    props = record.get('properties', {})
    added = record.get('additional', {})
    rid = record.get('record_id', '')
    raw = {'record_type': kind, 'record_id': rid}
    label = record.get('display_name') or record.get('name') or title_for(raw, props, added)
    overlay = record.get('effective_kind') == 'workspace_overlay'
    return {
        'id': rid, 'kind': kind, 'target_kind': 'WorkspaceOrganization' if overlay else kind,
        'label': str(label), 'secondary': ('Workspace organization' if overlay else 'User-authored Workspace record' if isinstance(kind,str) and is_type_id(kind) else subtitle_for(raw, props, added)),
        'updated': record.get('updated_at', ''), 'active': record.get('active', True),
        'fields': project_list_fields(kind, props, added, updated=record.get('updated_at', ''),
                                      active=record.get('active', True)),
        'workspace_properties': record.get('workspace_properties', {}), 'record': record,
    }


class KMSession:
    """Created and used on the engine worker only. Every mutation is KM-owned."""
    def __init__(self, state: Path, *, actor: str | None = None, startup_diagnostics=None,
                 startup_progress=None, upgrade_control=None):
        self.state = Path(state)
        self.startup_diagnostics = startup_diagnostics
        self.startup_progress = startup_progress
        self.upgrade_control = upgrade_control
        if startup_diagnostics: startup_diagnostics.phase('account_identity')
        self.actor = actor or local_actor()
        self.engine = None
        self.km = None
        self.owner = threading.get_ident()
        self.mode = 'closed'

    def _owned(self):
        if threading.get_ident() != self.owner:
            raise SessionError('WRONG_THREAD', 'KM must run on its owning engine worker')

    def open(self, *, defer_starter_conversion=False) -> dict:
        self._owned()
        if self.engine is not None:
            raise SessionError('STATE_ALREADY_OPEN', 'Session already owns a writer')
        if self.startup_diagnostics: self.startup_diagnostics.phase('state_preflight')
        check = probe_state(self.state)
        self.mode = check['mode']
        if self.mode.endswith('required'):
            return {'maintenance_required': True, 'mode': self.mode, 'schema': check['schema']}
        if defer_starter_conversion and check.get('catalog') == ALPHA4_CATALOG_FINGERPRINT:
            self.mode = 'starter_conversion_required'
            return {'maintenance_required': True, 'mode': self.mode, 'schema': check['schema']}
        if self.mode == 'new':
            if self.startup_diagnostics: self.startup_diagnostics.phase('new_workspace_initialization')
            initialize_workspace_v4(self.state, exclusive_writer_stopped=True)
            check = probe_state(self.state)
        if self.startup_diagnostics: self.startup_diagnostics.phase('engine_construction')
        engine = Engine(self.state, Settings(**check['settings']))
        try:
            if self.startup_diagnostics: self.startup_diagnostics.phase('catalog_and_saved_definition_validation')
            service = KMService(engine, actor=self.actor, allow_alpha4_catalog=True)
            if self.startup_diagnostics:
                service._startup_error = self.startup_diagnostics.failure
                self.startup_diagnostics.phase('starter_conversion_and_checkpoint')
            service._startup_phase = lambda stage:self._report_upgrade(stage)
            service._startup_progress = self._report_upgrade
            service._startup_check = self.upgrade_control.check if self.upgrade_control else None
            legacy = read_config(self.state).get("alpha3", {}).get("mail_views", [])
            with interruptible_sqlite(service.db,service._startup_check):
                converted = service.call({"contract": VERSION, "operation": "view.starters.ensure",
                    "expected_workspace_revision": service.revision(),
                    "params": {"legacy_mail_views": legacy}})
            if not converted.get("ok"):
                if self.upgrade_control:self.upgrade_control.check()
                raise SessionError(converted["error"]["code"], converted["error"]["message"])
        except Exception:
            engine.close()
            raise
        self.engine, self.km, self.mode = engine, service, 'ready'
        if self.startup_diagnostics: self.startup_diagnostics.phase('startup_summary')
        try:
            return self.summary()
        finally:
            if hasattr(service, '_startup_error'): del service._startup_error
            if hasattr(service, '_startup_phase'): del service._startup_phase
            if hasattr(service, '_startup_progress'): del service._startup_progress
            if hasattr(service, '_startup_check'): del service._startup_check

    def _report_upgrade(self, stage, counts=None):
        if self.startup_progress:
            self.startup_progress(stage,counts)
        elif self.startup_diagnostics:
            self.startup_diagnostics.progress(stage,counts)

    def close(self):
        self._owned()
        self.km = None
        if self.engine is not None:
            self.engine.close()
            self.engine = None
        self.mode = 'closed'

    def call(self, request: dict) -> dict:
        self._owned()
        if self.km is None:
            raise SessionError('STATE_NOT_READY', 'Finish the protected workspace setup first')
        return self.km.call(request)

    def query(self, operation: str, params: dict | None = None, **envelope) -> dict:
        response = self.call({'contract': VERSION, 'operation': operation, 'params': params or {}, **envelope})
        if not response['ok']:
            raise SessionError(response['error']['code'], response['error']['message'])
        return response['result']

    def summary(self) -> dict:
        self._owned()
        result = summarize(self.engine)
        capabilities = self.call({'contract': VERSION, 'operation': 'system.capabilities', 'params': {}})
        if not capabilities['ok']:
            raise SessionError(capabilities['error']['code'], capabilities['error']['message'])
        result.update({'capabilities': capabilities['result'], 'coverage': capabilities.get('coverage', {}),
                       'project_reconciliation':capabilities['result'].get('project_reconciliation'),
                       'registry': self.query('registry.views'),
                       'configuration': self.query('configuration.get'),
                       'workspace_types':self.query('workspace.types')['record_types'], 'schema': 4})
        return result

    def scan(self, source: Path) -> dict:
        self._owned()
        if not self.engine: raise SessionError('STATE_NOT_READY', 'Workspace is not ready')
        root = assert_separate(Path(source), self.state)
        # KM's existing scan owns confirmation timing and coverage. No second
        # disappearance tracker is maintained by the GUI.
        result = self.engine.scan(root, safe_mode=True)
        coverage = [(name, (root / relative).is_dir()) for name, relative in SOURCE_FOLDERS]
        result.pop('missing_paths_for_confirmation', None)
        result.update({'checked_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                       'coverage': coverage, 'summary': self.summary()})
        return result

    def listing(self, args: dict) -> dict:
        if args.get('view') in ('mail:unanswered','mail:waiting','mail:all'):
            aliases={'mail:unanswered':'view:user:starter-unanswered','mail:waiting':'view:user:starter-waiting','mail:all':'view:user:starter-all-mail'}
            args={**args,'view':aliases[args['view']]}
        params = {'page_size': 100, 'include_inactive': bool(args.get('inactive'))}
        if args.get('cursor'): params['cursor'] = args['cursor']
        if args.get('view')=='workspace:dashboard' and isinstance(args.get('kind'),str) and is_type_id(args['kind']) and not args.get('search'):
            data=self.query('workspace.dashboard',{'type_id':args['kind']})
            sections={}
            for panel in data.get('panels',[]):
                name=panel.get('label',panel['view_id'])+' · '+str(panel.get('total_count') if panel.get('total_count') is not None else panel['state'])
                sections[name]=panel['rows']
            return {'items':[],'total':sum(int(x.get('total_count') or 0) for x in data.get('panels',[])),
                    'offset':0,'next_cursor':None,'request_id':args.get('request_id'),'generation':args.get('generation'),
                    'query':{k:args.get(k) for k in ('kind','view','search','inactive')},
                    'state':'available','reason':None,'structured':{'kind':'workspace_dashboard','sections':sections,'rows':[]},
                    'labels':self.presentation_labels({'sections':sections}),'envelope':{}}
        if args.get('view') and args['view'] != 'all':
            params['view_id'] = args['view']
            params.update(args.get('view_parameters') or {})
            if params['view_id']=='view:loe:dashboard':params['bounded_sections']=True
            if params['view_id']=='view:email:conversations':params['message_preview_limit']=3
            operation = 'view.query'
        else:
            if args.get('kind'): params['record_type'] = args['kind']
            operation = 'records.query'
        if args.get('search'): params['search'] = args['search']
        if args.get('sorts'): params['sorts'] = args['sorts']
        if operation == 'records.query' and args.get('sort') in ('updated_desc','updated_asc'):
            params['sort'] = 'updated_at'
            params['sort_direction'] = 'asc' if args['sort'].endswith('_asc') else 'desc'
        response = self.call({'contract': VERSION, 'operation': operation, 'params': params})
        if not response['ok']:
            raise SessionError(response['error']['code'], response['error']['message'])
        result = response['result']
        # Dashboard sections carry evidence paths, sometimes only stable IDs.
        # Resolve their labels through the same authority before presentation.
        for entries in result.get('sections',{}).values():
            for item in entries:
                if not item.get('record_id') or item.get('properties'):continue
                rid=item['record_id'];kind=item.get('record_type')
                if not kind:
                    source=self.engine.store.row(rid)
                    kind=source['record_type'] if source else 'WorkspaceOrganization'
                resolved=self.query('records.get',{'record_id':rid,'record_type':kind})['record']
                item.update(resolved)
        rows = result.get('rows', [])
        # Conversations and dashboards are not disguised as Core records.
        records = [row for row in rows if row.get('record_id') and row.get('record_type')]
        return {
            'items': [display_row(row) for row in records], 'total': result.get('total_count', len(rows)),
            'offset': int(args.get('offset', 0)), 'next_cursor': result.get('next_cursor'),
            'request_id': args.get('request_id'), 'generation': args.get('generation'), 'query': {k: args.get(k) for k in ('kind', 'view', 'search', 'inactive')},
            'state': result.get('state', 'available'), 'reason': result.get('reason'),
            'structured': result, 'labels':self.presentation_labels(result), 'coverage': response.get('coverage', {}),
            'envelope': {k: response.get(k) for k in ('workspace_id', 'workspace_revision', 'core_epoch', 'as_of')},
        }

    def presentation_labels(self, payload):
        """Resolve only referenced identities for this bounded page, on the owner.

        The view's membership, evidence and direction are untouched. Label reads
        cannot interleave with another command on this serialized session.
        """
        wanted={};labels={};property_owners=set()
        def remember(row):
            shown=display_row(row)
            labels[shown['id']]={k:shown[k] for k in ('label','kind','target_kind')}
        def walk(value):
            if isinstance(value,list):
                for item in value:walk(item)
            elif isinstance(value,dict):
                rid=value.get('record_id')
                if rid:
                    wanted.setdefault(rid,value.get('record_type'))
                    if value.get('record_type') and (value.get('properties') or value.get('display_name') or value.get('name')):remember(value)
                    if value.get('workspace_properties') and value.get('record_type'):property_owners.add(value['record_type'])
                for name in ('source_id','target_id','effective_org_id','core_org_id','source_record'):
                    if isinstance(value.get(name),str):wanted.setdefault(value[name],value.get(name.replace('_id','_type')) if name in ('source_id','target_id') else None)
                for name in ('effective_organization_ids','person_ids'):
                    for rid in value.get(name,[]) or []:wanted.setdefault(rid,'Person' if name=='person_ids' else None)
                for key,item in value.items():
                    if key not in ('additional','properties'):walk(item)
                for ref in (value.get('properties') or {}).get('ws:poc_people',[]) or []:walk(ref)
        walk(payload)
        for rid,kind in wanted.items():
            if rid in labels:continue
            if not kind:
                row=self.engine.store.row(rid)  # Scoped identity lookup only.
                kind=row['record_type'] if row else 'WorkspaceOrganization'
            response=self.call({'contract':VERSION,'operation':'records.get','params':{'record_id':rid,'record_type':kind}})
            if response['ok']:remember(response['result']['record'])
        for owner in sorted(property_owners):
            for prop in self.query('registry.properties',{'record_type':owner})['workspace_properties']:
                labels[prop['property_id']]={'label':prop.get('label',prop.get('display_label','Workspace field'))}
        return labels

    def detail(self, rid: str, kind: str | None = None) -> dict:
        self._owned()
        if not kind and rid.startswith('wsrec_'):
            found=self.km.db.execute('SELECT type_id FROM ws_records WHERE record_id=?',(rid,)).fetchone()
            if found:kind=found[0]
        if not kind:
            row = self.engine.store.row(rid)  # Identity lookup only, not a competing semantic query.
            kind = row['record_type'] if row else 'WorkspaceOrganization'
        stamp = datetime.now(timezone.utc).isoformat()
        response = self.call({'contract': VERSION, 'operation': 'records.get',
            'params': {'record_id': rid, 'record_type': kind, 'include_detail': True}, 'as_of': stamp})
        if not response['ok']:
            raise SessionError(response['error']['code'], response['error']['message'])
        snapshot = {k: response[k] for k in ('workspace_id', 'workspace_revision', 'core_epoch')}
        snapshot['as_of'] = stamp
        record = response['result']['record']
        bundle = response['result']['detail']
        evidence, history, rules, relations = (bundle[k] for k in ('evidence','history','rules','relations'))
        display = display_row(record)
        links = []
        neighbor_labels = {key: display_row(value)['label'] for key,value in bundle['neighbors'].items()}
        # These projections are needed only for labels; do not retain the source
        # fields while constructing a potentially large relationship table.
        del bundle['neighbors']
        for edge in relations.get('edges', []):
            other = edge['target_id'] if edge['source_id'] == rid else edge['source_id']
            other_kind = edge['target_type'] if edge['source_id'] == rid else edge['source_type']
            links.append({'source': edge['source_id'], 'target': edge['target_id'],
                          'predicate': edge['predicate'], 'label':edge.get('label'), 'direction': edge['direction'],
                          'other': other, 'other_type': 'Organization' if other_kind == 'WorkspaceOrganization' else other_kind,
                          'other_target_kind': other_kind, 'other_label': neighbor_labels[other],
                          'evidence': edge.get('claims', []), 'authority': edge.get('authority', 'core'),
                          'supporting_core_edges': edge.get('supporting_core_edges', [])})
        trace = evidence.get('source_trace', {})
        raw = record
        # All five shared tabs exist for overlays too, with actual manual audit
        # and constituent evidence rather than invented source observations.
        for key in ('source_observations', 'current_derived_support', 'record_history', 'relationship_history', 'rule_evaluations'):
            trace.setdefault(key, [])
        return {'id': rid, 'kind': display['kind'], 'target_kind': kind, 'title': display['label'],
                'secondary': display['secondary'], 'active': record.get('active', True),
                'scope': record.get('scope', ''), 'basis': record.get('identity_basis', 'workspace_manual'),
                'identity_key': raw.get('identity_key', ''), 'source_sha': raw.get('source_sha', ''),
                'updated': record.get('updated_at', ''), 'properties': record.get('properties', {}),
                'added': record.get('additional', {}), 'relationships': links, 'trace': trace,
                'workspace_properties': record.get('workspace_properties', {}),
                'km_evidence': evidence, 'km_history': history, 'km_rules': rules, 'km_record': record,
                'snapshot': snapshot}

    def migrate(self, source: Path, backup: Path, *, approved: bool, confirmed: bool) -> dict:
        self._owned()
        if self.engine is not None or not confirmed:
            raise SessionError('STATE_LOCKED', 'Migration requires confirmed stopped-writer maintenance')
        root, _ = validate_source(source, self.state)
        check = probe_state(self.state)
        common = dict(independent_location_approved=approved, source_root=root,
                      actor=self.actor, exclusive_writer_stopped=True)
        if check['mode'] == 'workspace_type_migration_required':
            result=migrate_v3_to_v4(self.state,backup,**common)
        elif check['mode'] == 'migration_required':
            result = migrate_v2_to_v3(self.state, backup, **common)
        elif check['mode'] == 'catalog_migration_required':
            result = migrate_v3_to_configurable_workspace(self.state, backup, **common)
        else:
            raise SessionError('MIGRATION_BLOCKED', 'No compatible migration is required')
        return {'migration': result, 'summary': self.open()}

    def restore(self, backup: Path, source: Path, *, confirmed: bool) -> dict:
        self._owned()
        if not confirmed: raise SessionError('STATE_LOCKED', 'Restore requires explicit confirmation')
        # This entire operation is serialized after previous jobs. The stable
        # application lock stays held outside the replaced directory.
        self.close()
        try:
            result = restore_backup(backup, self.state, source_root=source,
                                    exclusive_writer_stopped=True, confirmed=True)
        except Exception:
            self.mode = 'closed'
            raise
        # Reopening waits until the UI has reacquired its legacy lock and loaded
        # the restored settings. No queued scan can start a new writer here.
        self.mode = 'restored'
        return result

    def backup(self, destination: Path, source: Path, *, approved: bool) -> dict:
        self._owned()
        result = self.query('system.backup', {'destination': str(destination), 'source_root': str(source),
                            'independent_location_approved': approved})
        return result

    def content(self, rid: str, *, offset=0, limit=20000):
        self._owned(); return record_content(self.engine, rid, offset=offset, limit=limit)

    def reading(self, rid: str, kind: str | None = None) -> dict:
        """One worker turn resolves detail and archived content at one revision."""
        detail = self.detail(rid, kind)
        detail['content'] = self.content(rid) if detail['kind'] in ('Email','Document') else None
        return detail

    def home(self, args: dict) -> dict:
        """Bounded presentation of the canonical effort dashboard and its paths."""
        self._owned()
        efforts = self.query('records.query', {'record_type':'LineOfEffort','page_size':100})
        rows = efforts.get('rows', [])
        rid = args.get('id') or (rows[0]['record_id'] if rows else None)
        if not rid: return {'state':'unconfigured','efforts':[], 'sections':{}}
        data = self.query('view.query', {'view_id':'view:loe:dashboard','loe_id':rid,
                                       'bounded_sections':True, 'page_size':100})
        data['efforts'] = [display_row(row) for row in rows]
        # No new inclusion rule: every card was returned by the canonical view.
        data['records'] = {}
        for section in data['sections'].values():
            for item in section:
                if item['record_id'] in data['records']: continue
                row = self.query('records.get', {'record_id':item['record_id'],
                    'record_type':item.get('record_type','Organization')})['record']
                data['records'][item['record_id']] = display_row(row)
        return data

    def neighborhood(self, args: dict) -> dict:
        """Stable, bounded neighborhood; edges retain their actual direction/support."""
        seed = self.detail(args['id'], args.get('kind'))
        nodes = {seed['id']: seed}; edges = []; used = set()
        order = {'LineOfEffort':0, 'Person':1, 'Document':2, 'Event':3, 'Organization':4}
        candidates = sorted(seed['relationships'], key=lambda e:(order.get(e['other_type'],5),
                            e['predicate'],e['other_label'].casefold(),e['other']))
        # One representative per kind first; then remaining neighbors. A clear
        # coverage count makes this bounded view distinct from the complete list.
        chosen=[]; kinds=set()
        for e in candidates:
            if e['other_type'] not in kinds: chosen.append(e);kinds.add(e['other_type'])
        chosen += [e for e in candidates if e not in chosen]
        for e in chosen:
            if e['other'] in nodes: continue
            if len(nodes)>=5: break
            nodes[e['other']] = self.detail(e['other'],e['other_target_kind'])
            edges.append(e);used.add((e['source'],e['predicate'],e['target']))
        # Sender's explicit affiliation adds context without an invented hub edge.
        for n in list(nodes.values()):
            if n['kind']!='Person': continue
            e=next((e for e in n['relationships'] if e['predicate']=='affiliated_with'
                    and e['direction']=='outgoing'),None)
            if e and e['other'] not in nodes and len(nodes)<6:
                nodes[e['other']]=self.detail(e['other'],e['other_target_kind']);edges.append(e)
                break
        return {'seed':seed['id'],'nodes':nodes,'edges':edges,
                'direct_total':len(seed['relationships']), 'snapshot':seed['snapshot']}

    def preview(self, rid: str, source_sha: str | None = None):
        self._owned()
        if rid.startswith('wsrec_'):
            self.query('records.get',{'record_id':rid,'record_type':self.km.db.execute('SELECT type_id FROM ws_records WHERE record_id=?',(rid,)).fetchone()[0]})
            return {'id':rid,'status':'not_applicable','source_sha':None,'text':'User-authored Workspace records have no independent source archive.'}
        if not self.engine.store.row(rid):
            # Verify this really is an existing overlay, not an arbitrary ID.
            self.query('records.get', {'record_id': rid, 'record_type': 'WorkspaceOrganization'})
            return {'id': rid, 'status': 'not_applicable', 'source_sha': None,
                    'text': 'This user-authored overlay has no independent source archive.'}
        result = archive_inspection(self.engine, rid, source_sha)
        if result['status'] == 'checksum_mismatch':
            raise SessionError('ARCHIVE_CHECKSUM_MISMATCH', 'Source preview blocked: archived evidence did not match its original checksum')
        return result

    def export(self, destination: Path, source: Path | None):
        self._owned()
        target = assert_separate(destination, self.state, source)
        return {'triples': self.engine.export_rdf(target)}
