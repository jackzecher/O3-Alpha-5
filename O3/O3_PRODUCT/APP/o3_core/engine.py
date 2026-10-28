"""One-process, six-type O3 deterministic processing engine (no AI or network).

The six rule families are preserved from the original tested robot code.
All persistence, archive, checkpoint, identity lookup and relationship claims
are performed by ONE shared SQLite repository. RDF is an on-demand view.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Callable
from .store import Store, digest, encode, now
from .ontology import Ontology
from .rules import email, event, person, document, organization, loe

RULE_SETS = {
    'Email': email.RULES, 'Event': event.RULES, 'Person': person.RULES,
    'Document': document.RULES, 'Organization': organization.RULES,
    'LineOfEffort': loe.RULES
}
assert sum(map(len,RULE_SETS.values())) == 56
assert len({r.rule_id for rules in RULE_SETS.values() for r in rules}) == 56


class InputError(ValueError):
    """Bad or conflicting source input; preserve previous known-good state."""


class MailLookup:
    """Disposable indexed lookup of the single authoritative SQLite Email records."""
    def __init__(self, store: Store):
        self.store=store
        self._by_provider=None
        self._by_internet=None

    def invalidate(self):
        self._by_provider=None
        self._by_internet=None

    def _ensure(self):
        if self._by_provider is not None:return
        by_provider=defaultdict(set);by_internet=defaultdict(set)
        for row in self.store.records('Email'):
            p=json.loads(row['properties_json'])
            mailbox=str(p.get('mailbox_id') or '').strip().casefold()
            provider=str(p.get('message_id') or '').strip()
            internet=str(p.get('internet_message_id') or '').strip()
            if mailbox and provider:by_provider[(mailbox,provider)].add(row['record_id'])
            if mailbox and internet:by_internet[(mailbox,internet)].add(row['record_id'])
        self._by_provider=by_provider;self._by_internet=by_internet

    def resolve(self, mailbox_id: str, message_id: str) -> str | None:
        self._ensure()
        matches=self._by_provider.get((mailbox_id.strip().casefold(),message_id.strip()),set())
        return next(iter(matches)) if len(matches)==1 else None

    def resolve_internet(self, mailbox: str, internet_id: str, *, except_id: str | None = None) -> str | None:
        self._ensure()
        matches=self._by_internet.get((mailbox.strip().casefold(),internet_id.strip()),set())-{except_id}
        return next(iter(matches)) if len(matches)==1 else None


@dataclass(frozen=True)
class Settings:
    """Explicit identity namespaces, not paths or mutable filenames."""
    directory_scope: str='o3data'
    calendar_scope: str='primary'
    document_scope: str='o3data'
    loe_namespace: str='o3data'

    def __post_init__(self):
        if any(not isinstance(v,str) or not v.strip() for v in vars(self).values()):
            raise ValueError('Identity namespace cannot be empty')


class Engine:
    def __init__(self, state: Path, settings: Settings = Settings(), ontology: Path | None = None):
        self.store=Store(state)
        self.settings=settings
        settings_json=encode(vars(settings))
        prior=self.store.db.execute("SELECT value FROM engine_meta WHERE key='identity_scopes'").fetchone()
        if prior and prior[0]!=settings_json:
            self.store.close()
            raise ValueError('Identity namespaces differ from existing O3 state; explicit migration required')
        if not prior:
            with self.store.db:
                self.store.db.execute("INSERT INTO engine_meta VALUES('identity_scopes',?)",(settings_json,))
        if ontology is None:
            ontology=Path(__file__).resolve().parent.parent/'ontology'/'o3_core_v0_1.ttl'
        self.ontology=Ontology(ontology)
        self.mail=MailLookup(self.store)
        self._person_cache=None
        self._person_indexes={}
        self.metrics:dict[str,int]={}

    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
    def close(self):self.store.close()

    def rules(self) -> list[dict]:
        return [dict(record_type=kind,id=r.rule_id,group=r.group,inputs=r.inputs,
                     description=r.description) for kind,entries in RULE_SETS.items() for r in entries]

    def people(self) -> list[dict]:
        return [{'record_id':r['record_id'], 'addresses':json.loads(r['added_json']).get('addresses',[])}
                for r in self.store.records('Person')]

    def person_index(self, module):
        if self._person_cache is None:
            self._person_cache=self.people()
            self._person_indexes={}
        if module not in self._person_indexes:
            self._person_indexes[module]=module.PersonIndex.from_list(self._person_cache)
        return self._person_indexes[module]

    def org_index(self):
        items=[]
        for r in self.store.records('Organization'):
            p=json.loads(r['properties_json'])
            if p['kind']=='organization':
                items.append({'record_id':r['record_id'],'kind':'company','name':p['name']})
            elif p['kind']=='department' and p.get('parent_id'):
                parent=self.store.row(p['parent_id'])
                if parent and parent['active']:
                    items.append({'record_id':r['record_id'],'kind':'department',
                                  'name':p['name'],'company':json.loads(parent['properties_json'])['name']})
        return person.OrganizationIndex.from_list(items)

    def _snapshot(self, path: Path, kind: str) -> tuple[str,bytes,os.stat_result]:
        path=Path(path)
        if path.is_symlink() or not path.is_file(): raise InputError('Source file absent or symlink')
        before=path.stat()
        max_size={'Email':email.MAX_FILE_BYTES,'Person':person.MAX_BYTES,
                  'Document':document.MAX_CORPUS_BYTES,'Event':event.MAX_BYTES}[kind]
        if before.st_size > max_size: raise InputError('Source exceeds maximum size')
        raw=path.read_bytes()
        after=path.stat()
        if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):
            raise InputError('Source changed during read; retry later')
        return str(path.resolve()),raw,after

    def _skip(self, path: str, stat: os.stat_result, source_sha: str, *, force: bool):
        if force: return False
        row=self.store.db.execute('SELECT * FROM observed_files WHERE path=?',(path,)).fetchone()
        if not row: return False
        # _snapshot has already read the bytes. Verify their checksum; relying
        # on size/mtime alone misses changes from sync clients retaining times.
        if (row['size_bytes'],row['mtime_ns'],row['sha256']) != (stat.st_size,stat.st_mtime_ns,source_sha):
            return False
        members=self.store.db.execute('SELECT record_id FROM file_members WHERE path=?',(path,)).fetchall()
        return all(self.store.row(x['record_id'])['active'] for x in members)

    @staticmethod
    def _email_record_material(payload: dict) -> dict:
        """Capture metadata belongs to evidence, not the email's semantic content."""
        return {k: v for k, v in payload.items() if k not in ('capture_id', 'captured_at_utc')}

    def _ensure_no_conflict(self, path: str, rid: str, fingerprint: str):
        for row in self.store.db.execute('''SELECT m.path,m.fingerprint FROM file_members m
                                          JOIN observed_files f ON f.path=m.path
                                          WHERE m.record_id=? AND m.path<>?''',(rid,path)):
            other=Path(row['path'])
            # A pending source removal/update is not current competing evidence.
            if not other.is_file() or other.is_symlink(): continue
            if row['fingerprint'] != fingerprint:
                raise InputError('Conflicting simultaneously present source versions for Record ID')

    def _persist_one(self, kind: str, path: str, raw: bytes, stat: os.stat_result,
                     rid: str, scope: str, basis: str, key: str, fingerprint: str,
                     props: dict, added: dict, ctx, *, ext: str) -> str:
        self._ensure_no_conflict(path,rid,fingerprint)
        raw_sha=digest(raw)
        archive=self.store.archive(raw,ext)
        detail={'rules':ctx.outcomes,'modifications':ctx.modifications,'warnings':ctx.warnings,
                'additional':added, 'source_sha256':raw_sha}
        with self.store.db:
            result=self.store.upsert(kind,rid,scope,basis,key,fingerprint,raw_sha,
                                     props,added,archive,detail)
            self.store.refresh_observation(path,kind,scope,stat,raw_sha,{rid:fingerprint})
        return result

    def ingest_json(self, path: Path, kind: str, *, force: bool=False) -> str:
        if kind not in ('Email','Person','Document'):raise ValueError('Unsupported JSON Record Type')
        path,raw,stat=self._snapshot(path,kind)
        sha=digest(raw)
        if self._skip(path,stat,sha,force=force):return 'unchanged'
        try:
            payload=json.loads(raw.decode('utf-8-sig'))
        except (UnicodeDecodeError,json.JSONDecodeError) as e:
            raise InputError('Incomplete or invalid JSON') from e
        if kind=='Person':
            profile=person.source_profile(payload)
            scope=self.settings.directory_scope
            directory_id=person.directory_id(profile['id'])
            rid=person.person_id(scope,directory_id)
            ctx=person.evaluate(profile,rid,scope,self.org_index())
            props,added=ctx.normalized,ctx.additional
            basis='directory_id'; key=directory_id
        elif kind=='Email':
            email._clean_json_shape(payload)
            scope=str(payload.get('mailbox_id') or '').strip().casefold() or 'provisional'
            rid,basis=email.identity(payload,sha)
            # Preserve the existing Email Robot collision guard: a provider-scoped
            # record must not silently acquire a different Internet Message-ID.
            existing=self.store.row(rid)
            if existing is not None:
                previous=json.loads(existing['properties_json']).get('internet_message_id')
                incoming=payload.get('internet_message_id')
                if isinstance(previous,str) and previous.strip() and isinstance(incoming,str) and incoming.strip() and previous.strip()!=incoming.strip():
                    raise InputError('Conflicting Internet Message-IDs for the same scoped Email identity')
            ctx=email.evaluate(payload,rid,basis,people=self.person_index(email),
                               mail=None,file_folder=email._folder(Path(path).parent.name))
            props,added=ctx.normalized.copy(),ctx.additional
            # The source archive holds original HTML. Keep body_text as the
            # indexed derivative, not a second full copy of body_html.
            props.pop('body_html',None)
            # Recapture identifiers/timestamps can differ across independent
            # JSON files describing the same Email. Archive both captures and
            # index both observations, but do not fabricate a new record version
            # or reject one solely because its capture metadata differs.
            # A changed substantive property remains an explicit conflict when
            # two simultaneously present files claim the same record identity.
            if existing is not None:
                old=json.loads(self._current_source(existing).decode('utf-8-sig'))
                if (self._email_record_material(old)==self._email_record_material(payload)
                        and json.loads(existing['added_json']).get('direction')==added.get('direction')):
                    sha_for_record=existing['fingerprint']
                else:
                    sha_for_record=sha
            else:
                sha_for_record=sha
            key='|'.join([str(payload.get('mailbox_id') or ''),str(payload.get('message_id') or ''),
                          str(payload.get('internet_message_id') or '')])
        else:
            document.validate_corpus(payload)
            scope=self.settings.document_scope
            rid,basis,key=document.document_identity(payload,scope)
            ctx=document.evaluate(payload,rid,basis,people=self.person_index(document),emails=self.mail)
            props,added=ctx.normalized.copy(),ctx.additional
            # Full corpus remains byte-for-byte in the immutable archive.
            # Only headings relevant to deterministic LOE matching are indexed.
            props['content']={'units':[
                {**{k:v for k,v in unit.items() if k!='elements'},
                 'elements':[e for e in unit['elements'] if e['type'].casefold()
                             in ('heading','title','section_heading')]}
                for unit in ctx.normalized['content']['units']]}
        result=self._persist_one(kind,path,raw,stat,rid,scope,basis,key,
                                 sha_for_record if kind=='Email' else sha,
                                 props,added,ctx,ext='json')
        if result != 'unchanged':
            if kind=='Person': self._person_cache=None
            if kind=='Email': self.mail.invalidate()
        return result

    def _event_master_index(self, incoming: list[event.Event]|None=None, *, scope: str|None=None) -> dict[str,str]:
        scope=scope or self.settings.calendar_scope
        uid_to_ids=defaultdict(set)
        for row in self.store.records('Event'):
            data=json.loads(row['properties_json'])
            if row['scope']==scope and json.loads(row['added_json']).get('recurrence_key')=='master':
                uid_to_ids[data['uid']].add(row['record_id'])
        for e in incoming or []:
            rid,uid,occ=event.identity(scope,e)
            if occ=='master':uid_to_ids[uid].add(rid)
        return {uid:next(iter(ids)) for uid,ids in uid_to_ids.items() if len(ids)==1}

    def ingest_calendar(self, path: Path, *, force: bool=False) -> dict:
        path,raw,stat=self._snapshot(path,'Event')
        sha=digest(raw)
        if self._skip(path,stat,sha,force=force):return {'result':'unchanged','events':0}
        header,events=event.parse_calendar(raw)
        from o3_km.core_bridge import calendar_scope
        scope=calendar_scope(self,path) if self.store.db.execute('PRAGMA user_version').fetchone()[0]>=3 else self.settings.calendar_scope
        mapping={}; candidates=[]
        for item in events:
            rid,uid,occ=event.identity(scope,item)
            if rid in mapping:raise InputError('Duplicate VEVENT identity in a single calendar file')
            fp=item.fingerprint()
            self._ensure_no_conflict(path,rid,fp)
            mapping[rid]=fp
            candidates.append((rid,item,uid,occ,fp))
        master=self._event_master_index(events,scope=scope)
        pi=self.person_index(event)
        evaluated=[]
        # All input events validated before ANY state mutation.
        for rid,item,uid,occ,fp in candidates:
            previous=self.store.row(rid)
            if not force and previous and previous['fingerprint']==fp and previous['active']:
                evaluated.append((rid,uid,occ,fp,None))
                continue
            ctx=event.evaluate(item,scope,pi,master)
            ctx.additions['recurrence_key']=occ
            evaluated.append((rid,uid,occ,fp,ctx))
        archive=self.store.archive(raw,'ics')
        updated=0; created=0
        with self.store.db:
            for rid,uid,occ,fp,ctx in evaluated:
                if ctx is None: continue
                outcome={'rules':ctx.outcomes,'modifications':ctx.modifications,
                         'warnings':ctx.warnings,'additional':ctx.additions,
                         'source_sha256':sha}
                result=self.store.upsert('Event',rid,scope,'uid+recurrence',
                                         uid+'|'+occ,fp,sha,ctx.normalized,ctx.additions,archive,outcome)
                created+=result=='created'
                updated+=result!='unchanged'
            self.store.refresh_observation(path,'Event',scope,stat,sha,mapping)
        return {'result':'created' if created else 'updated' if updated else 'unchanged',
                'events':len(events),'updated':updated,'created':created}

    def _current_source(self, row) -> bytes:
        record=self.store.db.execute('''SELECT archive_relpath FROM record_versions
             WHERE record_id=? AND fingerprint=?''',(row['record_id'],row['fingerprint'])).fetchone()
        if not record:raise RuntimeError('Missing immutable source version for current record')
        path=self.store.root/record[0]
        data=path.read_bytes()
        if digest(data)!=row['source_sha']:
            raise RuntimeError('Original source evidence checksum mismatch')
        return data

    def _relink(self, kind: str):
        people=self.person_index({'Email':email,'Event':event,'Document':document}[kind])
        calendar_cache={};master=self._event_master_index()
        count=0
        for row in self.store.records(kind):
            rid=row['record_id']
            if kind=='Email':
                data=email._clean_json_shape(json.loads(self._current_source(row).decode('utf-8-sig')))
                ctx=email.evaluate(data,rid,row['identity_basis'],people=people,mail=self.mail)
                links=ctx.links
            elif kind=='Document':
                data=document.validate_corpus(json.loads(self._current_source(row).decode('utf-8-sig')))
                ctx=document.evaluate(data,rid,row['identity_basis'],people=people,emails=self.mail)
                links=ctx.links
            else:
                raw=self._current_source(row)
                sha=digest(raw)
                if sha not in calendar_cache:
                    calendar_cache[sha]=event.parse_calendar(raw)[1]
                item=next((item for item in calendar_cache[sha]
                           if event.identity(row['scope'],item)[0]==rid),None)
                if item is None:raise RuntimeError('Archived VEVENT not found')
                ctx=event.evaluate(item,row['scope'],people,self._event_master_index(scope=row['scope']))
                links=ctx.links
            with self.store.db:
                count+=self.store.replace_claims(kind+':'+rid,links,allowed=self.ontology.allowed)
        return count

    def _derive_organizations(self, *, force: bool = False) -> int:
        """Apply Person evidence, Organizations and supported links in one transaction."""
        if self.store.db.execute('PRAGMA user_version').fetchone()[0]>=3:
            from o3_km.core_bridge import organizations
            return organizations(self,force=force)
        existing={r['source_id']:(r['fingerprint'],json.loads(r['result_json']))
                  for r in self.store.db.execute("SELECT * FROM derived_evidence WHERE producer='Organization'")}
        updates={}; current=set()
        for row in self.store.records('Person'):
            rid=row['record_id'];current.add(rid)
            profile=json.loads(row['properties_json'])
            fp=organization.person_fingerprint(row['scope'],rid,profile)
            if rid in existing and existing[rid][0]==fp and not force:continue
            ctx=organization.evaluate(rid,row['scope'],row['source_sha'],profile)
            result={'claims':list(ctx.candidates.values()),
                    'links':[{'source':s,'predicate':p,'target':t,'evidence':e}
                             for (s,p,t),e in sorted(ctx.links.items())],
                    'rules':ctx.outcomes,'warnings':ctx.warnings,
                    'source_sha256':row['source_sha']}
            updates[rid]=(fp,result)
        removed=set(existing)-current
        if not updates and not removed:return 0
        staged={rid: result for rid,(_,result) in existing.items() if rid in current}
        staged.update({rid:entry[1] for rid,entry in updates.items()})
        claims=defaultdict(list)
        for pid,output in staged.items():
            for claim in output['claims']:claims[claim['record_id']].append(claim)
        resolved={}
        for rid,variants in sorted(claims.items()):
            if len({(v['scope'],v['kind'],v['parent_id']) for v in variants})!=1:
                raise ValueError('Conflicting Organization identity evidence')
            candidate=sorted(variants,key=lambda v:(v['name'].casefold(),v['name']))[0]
            resolved[rid]={k:candidate[k] for k in ('record_id','scope','kind','name','parent_id','identity_basis')}
        with self.store.db:
            for pid,(fp,result) in updates.items():
                self.store.db.execute("""INSERT INTO derived_evidence VALUES(?,?,?,?)
                    ON CONFLICT(producer,source_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                    result_json=excluded.result_json""",('Organization',pid,fp,encode(result)))
            self.store.db.executemany('DELETE FROM derived_evidence WHERE producer=? AND source_id=?',
                                      [('Organization',pid) for pid in removed])
            for rid,props in resolved.items():
                self.store.upsert('Organization',rid,props['scope'],props['identity_basis'],
                                  props['kind']+'|'+props['name'],digest(encode(props).encode()),
                                  '',props,{},None,None)
            for row in self.store.records('Organization'):
                if row['record_id'] not in resolved:
                    self.store.db.execute('UPDATE records SET active=0 WHERE record_id=?',(row['record_id'],))
            for pid,output in staged.items():
                links={(x['source'],x['predicate'],x['target']):
                        {'field':x['evidence'],'source_record':pid} for x in output['links']}
                self.store.replace_claims('Organization:'+pid,links,allowed=self.ontology.allowed)
            for pid in removed:
                self.store.replace_claims('Organization:'+pid,{},allowed=self.ontology.allowed)
        return len(updates)

    def _derive_loes(self, *, force: bool = False) -> int:
        """Derive only explicit LOE references; conflicting identities are withheld."""
        if self.store.db.execute('PRAGMA user_version').fetchone()[0]>=3:
            from o3_km.core_bridge import loes
            return loes(self,force=force)
        existing={r['source_id']:(r['fingerprint'],json.loads(r['result_json']))
                  for r in self.store.db.execute("SELECT * FROM derived_evidence WHERE producer='LOE'")}
        updates={}; current=set()
        for kind in ('Email','Event','Document'):
            for row in self.store.records(kind):
                rid=row['record_id'];current.add(rid)
                props=json.loads(row['properties_json']); added=json.loads(row['added_json'])
                evidence=loe.Evidence(kind,rid,row['source_sha'],loe.relevant_lines(kind,props,added))
                fp=loe.input_fingerprint(evidence,self.settings.loe_namespace)
                if rid in existing and existing[rid][0]==fp and not force:continue
                ctx=loe.evaluate(evidence,namespace=self.settings.loe_namespace)
                result={'claims':list(ctx.claims.values()),
                        'links':[{'source':s,'predicate':p,'target':t,'fields':sorted(fields)}
                                 for (s,p,t),fields in sorted(ctx.links.items())],
                        'rules':ctx.outcomes,'warnings':ctx.warnings,
                        'source_sha256':row['source_sha']}
                updates[rid]=(fp,result)
        removed=set(existing)-current
        if not updates and not removed:return 0
        staged={rid:result for rid,(_,result) in existing.items() if rid in current}
        staged.update({rid:x[1] for rid,x in updates.items()})
        claims=defaultdict(list)
        for result in staged.values():
            for claim in result['claims']:claims[claim['record_id']].append(claim)
        resolved={}
        for rid,variants in sorted(claims.items()):
            same=[{v['kind'] for v in variants},
                  {loe.norm_key(v['name']) for v in variants},
                  {v['reference_code'] for v in variants},
                  {v['namespace'] for v in variants},
                  {v['identity_basis'] for v in variants}]
            if any(v.get('conflict') for v in variants) or any(len(x)!=1 for x in same):
                continue
            rec=sorted(variants,key=lambda v:(v['name'].casefold(),v['name']))[0].copy()
            rec.pop('conflict',None)
            resolved[rid]=rec
        with self.store.db:
            for rid,(fp,result) in updates.items():
                self.store.db.execute("""INSERT INTO derived_evidence VALUES(?,?,?,?)
                    ON CONFLICT(producer,source_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                    result_json=excluded.result_json""",('LOE',rid,fp,encode(result)))
            self.store.db.executemany('DELETE FROM derived_evidence WHERE producer=? AND source_id=?',
                                      [('LOE',rid) for rid in removed])
            for rid,rec in resolved.items():
                self.store.upsert('LineOfEffort',rid,rec['namespace'],rec['identity_basis'],
                                  rec['reference_code'] or rec['kind']+'|'+rec['name'],
                                  digest(encode(rec).encode()),'',rec,{},None,None)
            for row in self.store.records('LineOfEffort'):
                if row['record_id'] not in resolved:
                    self.store.db.execute('UPDATE records SET active=0 WHERE record_id=?',(row['record_id'],))
            for src,output in staged.items():
                links={(x['source'],x['predicate'],x['target']):
                       {'fields':x['fields'],'source_record':src}
                       for x in output['links'] if x['target'] in resolved}
                self.store.replace_claims('LOE:'+src,links,allowed=self.ontology.allowed)
            for src in removed:
                self.store.replace_claims('LOE:'+src,{},allowed=self.ontology.allowed)
        return len(updates)

    def _gather(self, data_root: Path, kind: str, *, allow_missing: bool=False,
                missing: list[str] | None=None):
        root=Path(data_root)
        folders={'Person':[root/'Contacts'/'Profiles'],
                 'Email':[root/'Mail'/'Inbox',root/'Mail'/'Sent'],
                 'Event':[root/'Calendars'], 'Document':[root/'Documents']}[kind]
        suffix='.ics' if kind=='Event' else '.json'
        files=[]
        for folder in folders:
            segment=folder
            while segment!=root:
                if segment.is_symlink():raise InputError('Symlink in source directory path is unsupported')
                segment=segment.parent
            if not folder.is_dir():
                if allow_missing and not folder.exists():
                    if missing is not None: missing.append(str(folder.relative_to(root)))
                    continue
                raise InputError('Missing source folder: '+str(folder))
            def walk_failed(error):
                raise InputError('Incomplete source enumeration: '+str(error))
            for parent,dirs,names in os.walk(folder,followlinks=False,onerror=walk_failed):
                if allow_missing and any((Path(parent)/d).is_symlink()
                                         for d in dirs if d.casefold()!='flowrequests'):
                    raise InputError('Source symlink subtree is not safe to reconcile')
                dirs[:]=sorted(d for d in dirs if d.casefold()!='flowrequests' and not (Path(parent)/d).is_symlink())
                if kind=='Event' and self.store.db.execute('PRAGMA user_version').fetchone()[0]>=3:
                    # The KM 1.0 contract registers only top-level Calendars/*.ics.
                    # Do not mint calendar identities from unrelated nested folders.
                    dirs.clear()
                for name in sorted(names):
                    p=Path(parent)/name
                    if p.suffix.lower()==suffix:
                        if p.is_symlink():
                            if allow_missing: raise InputError('Source symlink file is not safe to reconcile')
                            continue
                        files.append(p)
        return files

    @staticmethod
    def _source_family(root, path, kind):
        """Map source observations to the five existing input families."""
        if kind=='Email':
            try:parts=Path(path).relative_to(root).parts
            except ValueError:parts=Path(path).parts
            return 'MailSent' if 'Sent' in parts[:3] else 'MailInbox'
        return {'Person':'ContactsProfiles','Event':'CalendarICS',
                'Document':'DocumentCorpus'}[kind]

    @staticmethod
    def _missing_source_family(folder):
        """_gather returns native separators; coverage is platform-independent."""
        relative=str(folder).replace('\\','/').strip('/')
        families={'Mail/Inbox':'MailInbox','Mail/Sent':'MailSent',
                  'Contacts/Profiles':'ContactsProfiles','Calendars':'CalendarICS',
                  'Documents':'DocumentCorpus'}
        if relative not in families:raise InputError('Unknown missing source folder')
        return families[relative]

    def _record_km_coverage(self,statuses,*,changed=False):
        if self.store.db.execute('PRAGMA user_version').fetchone()[0]<3:return
        coverage=encode(statuses)
        prior=self.store.db.execute("SELECT value FROM engine_meta WHERE key='km_source_coverage_v1'").fetchone()
        with self.store.db:
            if prior is None or prior[0]!=coverage:
                changed=True
                self.store.db.execute("INSERT INTO engine_meta(key,value) VALUES('km_source_coverage_v1',?) "
                                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",(coverage,))
            if changed:
                self.store.db.execute("UPDATE engine_meta SET value=CAST(value AS INTEGER)+1 WHERE key='km_core_epoch'")

    def scan(self, data_root: Path, *, force:bool=False, safe_mode:bool=False,
             confirmed_missing: frozenset[str] | None=None) -> dict:
        """Scan sources. safe_mode is GUI-only conservative removal policy.

        The original Core v2 CLI behavior is preserved. KM v3 always uses
        conservative deletion: two healthy complete observations separated by
        at least one normal check cycle, never a single transient absence.
        """
        root=Path(data_root)
        # v3 always uses safe source-removal semantics, including the technician CLI.
        # v2 retains the original Core behavior and regression compatibility.
        km_v3=self.store.db.execute('PRAGMA user_version').fetchone()[0]>=3
        safe_mode=safe_mode or km_v3
        if km_v3:
            # The source-root identity belongs to this state. A different folder
            # must not be interpreted as deletion of all previously captured facts.
            selected=str(root.resolve())
            prior=self.store.db.execute(
                "SELECT value FROM engine_meta WHERE key='km_bound_source_root_v1'").fetchone()
            if (prior and prior[0]!=selected) or any(
                not Path(r['path']).is_relative_to(root.resolve())
                for r in self.store.db.execute('SELECT path FROM observed_files')):
                raise InputError('SOURCE_ROOT_CONFLICT: explicit verified relocation required')
        families=('MailInbox','MailSent','ContactsProfiles','CalendarICS','DocumentCorpus')
        if root.is_symlink() or not root.is_dir():
            self._record_km_coverage({key:'unavailable' for key in families})
            raise InputError('O3Data root must exist and not be symlink')
        if self.store.root == root.resolve() or self.store.root.is_relative_to(root.resolve()):
            raise InputError('O3 application state must be outside O3Data source files')
        missing_folders=[]
        try:
            files={kind:self._gather(root,kind,allow_missing=safe_mode,
                                     missing=missing_folders)
                   for kind in ('Person','Email','Event','Document')}
        except (InputError,OSError):
            # An incomplete directory listing is unknown evidence, never a
            # fresh negative or an authorization to deactivate existing facts.
            self._record_km_coverage({key:'partial' for key in families})
            raise
        counters={k:{'created':0,'updated':0,'unchanged':0,'errors':0} for k in files}
        errors=[]; dirty=set()
        for kind,items in files.items():
            for p in items:
                try:
                    result=self.ingest_calendar(p,force=force)['result'] if kind=='Event' else self.ingest_json(p,kind,force=force)
                    counters[kind][result]+=1
                    if result!='unchanged':dirty.add(kind)
                except (InputError,ValueError,OSError,sqlite3.Error,KeyError,TypeError) as e:
                    counters[kind]['errors']+=1
                    errors.append({'record_type':kind,'source_family':self._source_family(root,p,kind),
                                   'file':p.name,'error':str(e)})
        # Only a full successful scan of every directory can remove a previously observed file.
        # Missing branches, failed files and incomplete directory traversal
        # are unknown coverage and cannot imply source removals.
        complete=not errors and not missing_folders
        live={str(p.resolve()) for items in files.values() for p in items}
        missing_now=frozenset(r['path'] for r in self.store.db.execute('SELECT path FROM observed_files')
                              if r['path'] not in live)
        # Absence is never enough on one healthy listing. Persist only the
        # pending path set and first observation time in existing engine_meta;
        # no watcher, second database, or alternative reconciliation mechanism.
        prior_pending={}
        if km_v3:
            receipt=self.store.db.execute("SELECT value FROM engine_meta WHERE key='km_pending_absence_v1'").fetchone()
            if receipt:
                try:prior_pending=json.loads(receipt[0])
                except (TypeError,ValueError):prior_pending={}
        pending_paths=sorted(missing_now)
        root_id=str(root.resolve())
        same_pending=(prior_pending.get('source_root')==root_id and
                      prior_pending.get('paths')==pending_paths)
        elapsed=time.time()-prior_pending.get('first_seen',time.time()) if same_pending else 0
        two_cycles=(same_pending and elapsed>=60 and
                    (confirmed_missing is None or missing_now==confirmed_missing))
        prune=complete and (not safe_mode or not missing_now or two_cycles)
        if km_v3:
            # Any interrupted/incomplete scan breaks the healthy-observation
            # sequence. A different missing set starts its own first cycle.
            checkpoint=({'source_root':root_id,'paths':pending_paths,
                         'first_seen':prior_pending['first_seen'] if same_pending else time.time()}
                        if complete and pending_paths and not prune else {})
            if checkpoint!=prior_pending:
                with self.store.db:
                    self.store.db.execute("INSERT INTO engine_meta(key,value) VALUES('km_pending_absence_v1',?) "
                                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                          (encode(checkpoint),))
        if prune:
            to_remove=sorted(missing_now)
            if to_remove:
                with self.store.db:
                    self.store.mark_pending()
                    self.store.db.executemany('DELETE FROM observed_files WHERE path=?',[(p,) for p in to_remove])
                dirty.update(('Person','Email','Event','Document'))
            with self.store.db:
                inactive=self.store.deactivate_orphans()
            if inactive:
                dirty.update(('Person','Email','Event','Document'))
                self._person_cache=None
                self.mail.invalidate()
        else:
            inactive=0
        # A malformed file does not block valid records or relationship repair.
        # A crash, failed scan or restart must not lose pending link repairs.
        # Input changes set this marker in the SAME transaction as records.
        pending=self.store.reconciliation_needed()
        repair_only=pending and not dirty
        if repair_only:
            dirty.update(('Person','Email','Event','Document'))
        # Derived Org and LOE evidence is incrementally fingerprinted; source
        # records which disappeared are retracted, never physically erased.
        org_evaluated=self._derive_organizations(force=force or repair_only)
        loe_evaluated=self._derive_loes(force=force or repair_only)
        # Re-run relationship rules only for dependency changes. This permits
        # parents or known Persons to arrive after their referencing records.
        refreshed={}
        if dirty & {'Person','Email'} or force:
            refreshed['Email']=self._relink('Email')
        if dirty & {'Person','Event'} or force:
            refreshed['Event']=self._relink('Event')
        if dirty & {'Person','Email','Document'} or force:
            refreshed['Document']=self._relink('Document')
        with self.store.db:
            # Deactivated sources/targets do not remain authoritative.
            self.store.db.execute('''DELETE FROM relationships WHERE
                source_id IN (SELECT record_id FROM records WHERE active=0) OR
                target_id IN (SELECT record_id FROM records WHERE active=0)''')
            self.store.clear_pending()
        if self.store.db.execute('PRAGMA user_version').fetchone()[0]>=3:
            # Independent local source-family coverage; never confuse a missing
            # document with an incomplete Sent mailbox or remote cloud freshness.
            statuses={key:'complete' for key in families}
            for folder in missing_folders:
                statuses[self._missing_source_family(folder)]='partial'
            for error in errors:statuses[error['source_family']]='partial'
            if complete and not prune:
                for row in self.store.db.execute('SELECT path,kind FROM observed_files'):
                    if row['path'] in missing_now:
                        statuses[self._source_family(root,row['path'],row['kind'])]='partial'
            self._record_km_coverage(statuses,changed=bool(dirty or org_evaluated or loe_evaluated or inactive or
                        sum(v['created']+v['updated'] for v in counters.values())))
        return {'sources':counters,'missing_folders':missing_folders,
                'complete':complete, 'pending_removals':len(missing_now) if complete and not prune else 0,
                'missing_paths_for_confirmation':sorted(missing_now) if complete and not prune else [],
                'organizations_evaluated':org_evaluated,'inactive':inactive,
                'loe_sources_evaluated':loe_evaluated,'relationships_refreshed':refreshed,
                'errors':errors,'state':self.store.summary()}

    def graph(self):return self.ontology.graph(self.store)
    def audit_rdf(self)->dict:return self.ontology.audit(self.store)
    def audit_provenance(self)->dict:return self.store.audit_provenance()
    def trace_record(self,record_id:str)->dict:return self.store.trace(record_id)
    def export_rdf(self,path:Path)->int:return self.ontology.export(self.store,path)


def default_state() -> Path:
    """Usual per-user OS application-data location, subject to workstation policy."""
    if os.name=='nt':
        return Path(os.environ.get('LOCALAPPDATA') or (Path.home()/'AppData'/'Local'))/'O3'
    return Path(os.environ.get('XDG_DATA_HOME') or (Path.home()/'.local'/'share'))/'O3'


def cli(argv=None) -> int:
    parser=argparse.ArgumentParser(description='O3 Core: read-only O3Data ingestion and RDF query')
    parser.add_argument('command',choices=('scan','stats','rules','export','audit-rdf','audit-provenance','trace'))
    parser.add_argument('--source',type=Path,help='O3Data folder (scan only)')
    parser.add_argument('--state',type=Path,default=default_state(),help='O3-managed persistent state folder (automatic default)')
    parser.add_argument('--directory-scope',default='o3data')
    parser.add_argument('--calendar-scope',default='primary')
    parser.add_argument('--document-scope',default='o3data')
    parser.add_argument('--loe-namespace',default='o3data')
    parser.add_argument('--output',type=Path,help='Output Turtle file for export command')
    parser.add_argument('--record-id',type=str,help='Record ID to inspect with trace')
    parser.add_argument('--force',action='store_true')
    args=parser.parse_args(argv)
    cfg=Settings(args.directory_scope,args.calendar_scope,args.document_scope,args.loe_namespace)
    if args.command=='scan' and args.source:
        source=args.source.resolve()
        state=args.state.resolve()
        if state==source or state.is_relative_to(source):
            parser.error('O3 state must not be stored inside the read-only O3Data tree')
    with Engine(args.state,cfg) as eng:
        if args.command=='scan':
            if not args.source:parser.error('--source required for scan')
            result=eng.scan(args.source,force=args.force)
            print(json.dumps(result,indent=2,ensure_ascii=False))
            return 2 if result['errors'] else 0
        if args.command=='stats': print(json.dumps(eng.store.summary(),indent=2))
        elif args.command=='rules':print(json.dumps(eng.rules(),indent=2))
        elif args.command=='audit-provenance':
            result=eng.audit_provenance()
            print(json.dumps(result,indent=2,ensure_ascii=False))
            return 0 if result['ok'] else 2
        elif args.command=='trace':
            if not args.record_id:parser.error('--record-id required for trace')
            print(json.dumps(eng.trace_record(args.record_id),indent=2,ensure_ascii=False))
        elif args.command=='audit-rdf':
            audit=eng.audit_rdf()
            print(json.dumps(audit,indent=2))
            return 0 if audit['ok'] else 2
        else:
            if not args.output:parser.error('--output required for export')
            print(json.dumps({'triples':eng.export_rdf(args.output),'output':str(args.output)}))
    return 0

if __name__=='__main__': raise SystemExit(cli())
