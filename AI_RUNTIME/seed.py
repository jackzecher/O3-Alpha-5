"""Create disposable synthetic O3Data with the protected source formats."""
import json, html, sys, uuid
from pathlib import Path
from o3_gui.km_session import KMSession
from o3_gui.logic import save_config
from o3_km.service import VERSION

def command(session, operation, params):
    cap = session.query('system.capabilities')
    result = session.call({'contract': VERSION, 'operation': operation, 'params': params,
                          'workspace_id': cap['workspace_id'],
                          'expected_workspace_revision': cap['workspace_revision']})
    if not result['ok']: raise RuntimeError((operation, result))
    return result['result']

def seed(base, fixtures=None):
    base = Path(base).resolve(); source = base/'O3Data'; state=base/'Workspace'
    if state.exists(): raise ValueError('Seed requires a new disposable directory')
    for name in ('Mail/Inbox','Mail/Sent','Contacts/Profiles','Calendars','Documents'):
        (source/name).mkdir(parents=True,exist_ok=True)
    if fixtures is None: fixtures=Path(__file__).with_name('synthetic_examples.json')
    records=json.loads(Path(fixtures).read_text())['records']
    def write(path,obj): (source/path).write_text(json.dumps(obj,indent=2),encoding='utf-8')
    people={'Maya Chen': 'maya.chen@example.test','Jordan Ellis':'jordan.ellis@example.test','Alex Morgan':'alex.morgan@example.test'}
    for i,(name,address) in enumerate(people.items()):
        write(f'Contacts/Profiles/person-{i}.json',{'id':str(uuid.uuid5(uuid.NAMESPACE_DNS,address)),
            'displayName':name,'mail':address,'userPrincipalName':address,'jobTitle':'Planning lead' if i==0 else 'Program analyst',
            'companyName':'Coastal Programs','department':'Readiness','officeLocation':'Synthetic office'})
    mails=[r for r in records if r['kind']=='Email']
    for i,r in enumerate(mails):
        name=r.get('person','Maya Chen');address=people.get(name,'maya.chen@example.test')
        stamp=f'2026-10-{26-i//4:02d}T{12-i%4:02d}:42:00Z'
        write(f'Mail/Inbox/{r["id"]}.json',{'schema_version':'em-raw-v1',
            'capture_id':r['id'],'mailbox_id':'synthetic-alex','source_folder':'Inbox',
            'message_id':r['id'],'internet_message_id':'<'+r['id']+'@example.test>',
            'conversation_id':'harbor-review' if i<4 else r['id'],
            'subject':r['title'].replace('Harbor review','Harbor Readiness review'),
            'from':{'name':name,'address':address},'to':[{'name':'Alex Morgan','address':people['Alex Morgan']}],'cc':[],
            'received_at_utc':stamp,'sent_at_utc':stamp,'captured_at_utc':stamp,
            'body_html':'<p>Project: Harbor Readiness</p><p>'+html.escape(r['body']).replace('\n','<br>')+'</p><p>Harbor Readiness</p>',
            'has_attachments':i==0,'attachments':[{'attachment_id':'planning-note','filename':'Planning note.pdf','size_bytes':2048}] if i==0 else []})
    # Two ordinary synthetic Sent captures exercise observed replies and waiting.
    for mid,subject,parent,conversation in [('sent-confirmed','Re: Site visit · observations captured','mail-006','mail-006'),('sent-question','Harbor Readiness · confirm the review pack',None,'waiting-review-pack')]:
        message={'schema_version':'em-raw-v1','capture_id':mid,'mailbox_id':'synthetic-alex','source_folder':'Sent',
            'message_id':mid,'internet_message_id':'<'+mid+'@example.test>','conversation_id':conversation,
            'subject':subject,'from':{'name':'Alex Morgan','address':people['Alex Morgan']},
            'to':[{'name':'Maya Chen','address':people['Maya Chen']}],'cc':[],
            'sent_at_utc':'2026-10-26T14:15:00Z','captured_at_utc':'2026-10-26T14:16:00Z',
            'body_text':'Please confirm which figures are ready for the Harbor Readiness review. Thank you.'}
        if parent:message['in_reply_to']='<'+parent+'@example.test>'
        write('Mail/Sent/'+mid+'.json',message)
    for i,r in enumerate(r for r in records if r['kind']=='Document'):
        origin={'type':'email_attachment','mailbox_id':'synthetic-alex','message_id':'mail-001','attachment_id':'planning-note'} if i==0 else {'type':'file'}
        write(f'Documents/{r["id"]}.json',{'schema_version':'o3-document-v1',
            'source':{'filename':r['title']+'.pdf','file_type':'pdf','source_id':r['id'],'modified_utc':'2026-10-24T16:00:00Z'},
            'origin':origin,'metadata':{'title':r['title'],'authors':['Maya Chen'],'issuing_organization':'Coastal Programs'},
            'content':{'units':[{'type':'page','elements':[{'type':'heading','text':'Project: Harbor Readiness'},{'type':'paragraph','text':r['body']}]}]},
            'extraction':{'status':'partial' if i==0 else 'complete','warnings':['Two annexes were not supplied by the converter.'] if i==0 else []}})
    (source/'Calendars/readiness.ics').write_text('BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//O3 Synthetic//EN\nBEGIN:VEVENT\nUID:harbor-review@example.test\nDTSTAMP:20261025T140000Z\nDTSTART:20261029T140000Z\nDTEND:20261029T150000Z\nSUMMARY:Harbor Readiness review\nDESCRIPTION:Project: Harbor Readiness\\nReview recorded evidence and open dependencies.\nORGANIZER;CN=Maya Chen:mailto:maya.chen@example.test\nATTENDEE;CN=Alex Morgan;PARTSTAT=NEEDS-ACTION:mailto:alex.morgan@example.test\nEND:VEVENT\nEND:VCALENDAR\n',encoding='utf-8')
    session=KMSession(state,actor='synthetic:ai-preview');session.open()
    effort=command(session,'loe.create',{'fields':{'name':'Harbor Readiness','kind':'project','objective':'Readiness evidence, dependencies and decisions.','reference_code':None,'aliases':[]}})
    command(session,'configuration.save',{'self_addresses':[people['Alex Morgan']]})
    result=session.scan(source)
    ids={}
    for row in session.query('records.query',{'page_size':100})['rows']:
        ids.setdefault(row['record_type'],[]).append(row['record_id'])
    selected=next(row['record_id'] for row in session.query('records.query',{'record_type':'Email'})['rows'] if 'presentation requested' in row['properties']['subject'])
    command(session,'property.create',{'record_type':'Email','definition_id':'ws:user:review-event',
        'specification':{'label':'Review event','mode':'manual','value_type':'record_ref','ref_type':'Event'}})
    command(session,'property.set',{'record_type':'Email','record_id':selected,'property_id':'ws:user:review-event',
        'value':{'record_type':'Event','record_id':ids['Event'][0]}})
    save_config(state,{'source_root':str(source),'paused':True,'last_capture_check':result['checked_utc'],'last_capture_result':{k:result.get(k) for k in ('complete','coverage','errors','pending_removals')},'alpha3':{'version':1,'home_effort':ids['LineOfEffort'][0]},'synthetic_qualification':True})
    (base/'seed-result.json').write_text(json.dumps({'effort':effort,'scan':result,'ids':ids},indent=2,default=str))
    (base/'schema.json').write_text(json.dumps(session.query('builder.schema',{'record_type':'Email'}),indent=2,default=str))
    session.close()
    return state

if __name__=='__main__': print(seed(sys.argv[1]))
