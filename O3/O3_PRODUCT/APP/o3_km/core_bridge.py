"""Narrow KM 3 Core extensions; original 56 registered rule bodies stay untouched."""
from __future__ import annotations
import json
from collections import defaultdict
from .catalog import CATALOG_FINGERPRINT
from o3_core.store import digest,encode,now
from o3_core.rules import organization,loe


def calendar_scope(engine,path):
    """One permanent per-calendar identity; never infer rename identity by bytes."""
    from pathlib import Path
    import uuid
    path=Path(path)
    if path.parent.name.casefold()!='calendars':raise ValueError('CALENDAR_SCOPE_CONFLICT: calendar must be top-level')
    name=path.name
    if name.casefold()=='primary.ics':scope='primary'
    else:scope=None
    db=engine.store.db
    entry=db.execute("SELECT value FROM engine_meta WHERE key='calendar_sources_v1'").fetchone()
    if not entry:return engine.settings.calendar_scope
    registry=json.loads(entry[0]); found=[x for x in registry.values() if x['path'].casefold()==name.casefold()]
    if found:
        if len(found)!=1:raise ValueError('CALENDAR_SCOPE_CONFLICT: duplicate path')
        return found[0]['scope']
    if scope=='primary' and 'primary' in registry:
        if registry['primary']['path'].casefold()!=name.casefold():
            raise ValueError('CALENDAR_SCOPE_CONFLICT: Primary already registered')
        return scope
    scope='cal_'+uuid.uuid4().hex
    registry[scope]={'scope':scope,'path':name,'display_name':path.stem,'state':'active','revision':1}
    # Registration is a Core source identity mutation; persist atomically before records.
    with db:
        db.execute('UPDATE engine_meta SET value=? WHERE key=?',(encode(registry),'calendar_sources_v1'))
        db.execute("UPDATE engine_meta SET value=CAST(value AS INTEGER)+1 WHERE key='km_core_epoch'")
    return scope


def organizations(engine,*,force=False):
    """Person-derived claims + explicit Document issuer claims, same Core Org identity."""
    db=engine.store.db
    existing={(r['producer'],r['source_id']):(r['fingerprint'],json.loads(r['result_json']))
              for r in db.execute("SELECT * FROM derived_evidence WHERE producer IN ('Organization','DocumentIssuer')")}
    updates={};current=set();
    for row in engine.store.records('Person'):
        key=('Organization',row['record_id']);current.add(key)
        profile=json.loads(row['properties_json'])
        fp=organization.person_fingerprint(row['scope'],row['record_id'],profile)
        if key in existing and existing[key][0]==fp and not force:continue
        ctx=organization.evaluate(row['record_id'],row['scope'],row['source_sha'],profile)
        updates[key]=(fp,{'claims':list(ctx.candidates.values()),
            'links':[{'source':s,'predicate':p,'target':t,'evidence':e} for (s,p,t),e in sorted(ctx.links.items())],
            'rules':ctx.outcomes,'warnings':ctx.warnings,'source_sha256':row['source_sha']})
    for row in engine.store.records('Document'):
        key=('DocumentIssuer',row['record_id']);current.add(key)
        props=json.loads(row['properties_json']);metadata=props.get('metadata') or {}
        name=metadata.get('issuing_organization') if isinstance(metadata,dict) else None
        fp=digest(encode({'version':'issuer-v1','scope':row['scope'],'record':row['record_id'], 'field':name}).encode())
        if key in existing and existing[key][0]==fp and not force:continue
        normalized=organization.display_label(name) if name is None or isinstance(name,str) else ''
        claims=[];warnings=[]
        if isinstance(name,str) and normalized and len(normalized)<=160:
            oid=organization.organization_id(row['scope'],'organization',normalized)
            claims=[{'record_id':oid,'scope':row['scope'],'kind':'organization','name':normalized,
                     'parent_id':None,'identity_basis':'scoped_source_label'}]
        elif name is not None:
            warnings.append('metadata.issuing_organization is not a supported explicit label')
        updates[key]=(fp,{'claims':claims,'links':[],'rules':[{'rule_id':'KM-DOC-ISSUER-001',
            'result':'applied' if claims else 'no_change'}], 'warnings':warnings,
            'field_path':'metadata.issuing_organization','source_sha256':row['source_sha']})
    removed=set(existing)-current
    if not updates and not removed:return 0
    staged={key:output for key,(_,output) in existing.items() if key in current}
    staged.update({key:r for key,(_,r) in updates.items()})
    claims=defaultdict(list)
    for output in staged.values():
        for claim in output['claims']:claims[claim['record_id']].append(claim)
    resolved={}
    for rid,vs in claims.items():
        if len({(x['scope'],x['kind'],x['parent_id']) for x in vs})!=1:
            raise ValueError('Conflicting Organization identity evidence')
        x=sorted(vs,key=lambda v:(v['name'].casefold(),v['name']))[0]
        resolved[rid]={k:x[k] for k in ('record_id','scope','kind','name','parent_id','identity_basis')}
    with db:
        for (producer,rid),(fp,out) in updates.items():
            db.execute('''INSERT INTO derived_evidence VALUES(?,?,?,?)
                ON CONFLICT(producer,source_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                result_json=excluded.result_json''',(producer,rid,fp,encode(out)))
        db.executemany('DELETE FROM derived_evidence WHERE producer=? AND source_id=?',list(removed))
        for rid,p in sorted(resolved.items()):
            engine.store.upsert('Organization',rid,p['scope'],p['identity_basis'],
                               p['kind']+'|'+p['name'],digest(encode(p).encode()),'',p,{},None,None)
        for row in engine.store.records('Organization'):
            if row['record_id'] not in resolved:db.execute('UPDATE records SET active=0, updated_at=? WHERE record_id=?',(now(),row['record_id']))
        for (producer,rid),out in staged.items():
            if producer!='Organization':continue
            links={(x['source'],x['predicate'],x['target']):{'field':x['evidence'],'source_record':rid} for x in out['links']}
            engine.store.replace_claims('Organization:'+rid,links,allowed=engine.ontology.allowed)
        for producer,rid in removed:
            if producer=='Organization':engine.store.replace_claims('Organization:'+rid,{},allowed=engine.ontology.allowed)
    return len(updates)


def loes(engine,*,force=False):
    """Reconcile only against existing known LOEs; never auto-mint unknown source references."""
    db=engine.store.db
    candidates=[r for r in db.execute("SELECT * FROM records WHERE record_type='LineOfEffort'")
                if r['identity_basis'] in ('user_created','user_adopted','scoped_reference_code','scoped_explicit_label')]
    bycode=defaultdict(set);bylabel=defaultdict(set)
    for row in candidates:
        props=json.loads(row['properties_json']);add=json.loads(row['added_json'])
        if add.get('km_ownership')=='legacy_retired':continue
        namespace=props.get('namespace',row['scope']);code=props.get('reference_code')
        if isinstance(code,str) and code.strip():bycode[(namespace,code.casefold())].add(row['record_id'])
        for alt in props.get('aliases',[]):
            if isinstance(alt,str) and alt.strip():bylabel[(namespace,loe.norm_key(alt))].add(row['record_id'])
        if isinstance(props.get('name'),str):bylabel[(namespace,loe.norm_key(props['name']))].add(row['record_id'])
    existing={r['source_id']:(r['fingerprint'],json.loads(r['result_json']))
              for r in db.execute("SELECT * FROM derived_evidence WHERE producer='LOE'")}
    updated={};current=set();all_targets={r['record_id'] for r in candidates}
    for kind in ('Email','Event','Document'):
        for row in engine.store.records(kind):
            rid=row['record_id'];current.add(rid)
            props=json.loads(row['properties_json']);added=json.loads(row['added_json'])
            evidence=loe.Evidence(kind,rid,row['source_sha'],loe.relevant_lines(kind,props,added))
            fp=loe.input_fingerprint(evidence,engine.settings.loe_namespace)
            # Still refresh when target registry changed so newly added LOEs get links.
            ctx=loe.evaluate(evidence,namespace=engine.settings.loe_namespace)
            links=[];claims=[];warnings=list(ctx.warnings)
            for mention in ctx.mentions:
                key=(engine.settings.loe_namespace,mention['code'].casefold()) if mention['code'] else None
                match=set(bycode.get(key,())) if key else set(bylabel.get((engine.settings.loe_namespace,loe.norm_key(mention['name'])),()))
                if len(match)==1:
                    target=next(iter(match));claims.append({'record_id':target, 'kind':mention['kind'],
                                               'name':mention['name'],'reference_code':mention['code']})
                    links.append({'source':rid,'predicate':'associated_with','target':target,'fields':[mention['field']]})
                elif len(match)>1:warnings.append('LOE_REFERENCE_AMBIGUOUS: multiple known matching LOEs')
                else:warnings.append('LOE_REFERENCE_UNRESOLVED: explicit source reference has no registered LOE')
            result={'claims':claims,'links':links,'rules':ctx.outcomes,'warnings':warnings,'source_sha256':row['source_sha']}
            if rid not in existing or existing[rid]!=(fp,result) or force:
                updated[rid]=(fp,result)
    removed=set(existing)-current
    if not updated and not removed:return 0
    staged={rid:res for rid,(_,res) in existing.items() if rid in current}
    staged.update({rid:res for rid,(_,res) in updated.items()})
    supported={c['record_id'] for r in staged.values() for c in r['claims']}
    with db:
        for rid,(fp,out) in updated.items():
            db.execute('''INSERT INTO derived_evidence VALUES(?,?,?,?)
                ON CONFLICT(producer,source_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                result_json=excluded.result_json''',('LOE',rid,fp,encode(out)))
        for rid in removed:db.execute('DELETE FROM derived_evidence WHERE producer=? AND source_id=?',('LOE',rid))
        for row in candidates:
            if row['identity_basis'] in ('user_created','user_adopted'):continue
            if row['record_id'] not in supported and row['active']:
                db.execute('UPDATE records SET active=0,updated_at=? WHERE record_id=?',(now(),row['record_id']))
            elif row['record_id'] in supported and not row['active']:
                db.execute('UPDATE records SET active=1,updated_at=? WHERE record_id=?',(now(),row['record_id']))
        for rid,out in staged.items():
            links={(x['source'],x['predicate'],x['target']):{'fields':x['fields'],'source_record':rid}
                   for x in out['links'] if x['target'] in all_targets}
            engine.store.replace_claims('LOE:'+rid,links,allowed=engine.ontology.allowed)
        for rid in removed:engine.store.replace_claims('LOE:'+rid,{},allowed=engine.ontology.allowed)
    return len(updated)
