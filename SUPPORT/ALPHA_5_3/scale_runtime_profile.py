"""Real-ingestion large-content comparison; synthetic Linux measurements only."""
from pathlib import Path
import argparse,cProfile,hashlib,io,json,pstats,shutil,sys,time
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('phase',choices=('seed','measure'));p.add_argument('--package',type=Path,required=True);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--label',default='sample');p.add_argument('--state-from',type=Path);a=p.parse_args()
sys.dont_write_bytecode=True
sys.path[:0]=[str(ROOT/'.dev_runtime/libraries'),str(a.package.resolve()/'O3/O3_PRODUCT/APP')]
from o3_gui.km_session import KMSession
from o3_gui.logic import save_config
from continuity_support import command
base=a.fixture.resolve();base.mkdir(parents=True,exist_ok=True)
if a.phase=='seed':
 from benchmark_central import fixture
 source=base/'O3Data';counts=fixture(source,10000)
 for file in source.glob('Mail/*/*.json'):
  item=json.loads(file.read_text());item['body_html']='<p>Synthetic long saved context. Review evidence and retain the previous discussion.</p>'*430
  file.write_text(json.dumps(item,separators=(',',':')))
 for file in source.glob('Documents/*.json'):
  item=json.loads(file.read_text());item['content']['units'][0]['elements'][0]['text']='Synthetic complete document paragraph with retained source context. '*1100
  file.write_text(json.dumps(item,separators=(',',':')))
 print('SOURCE_MB',round(sum(f.stat().st_size for f in source.rglob('*') if f.is_file())/1048576,2),flush=True)
 s=KMSession(base/'original/state',actor='synthetic:scale');s.open();command(s,'configuration.save',{'self_addresses':['self@example.test']})
 t=time.perf_counter();r=s.scan(source);assert r['complete'];save_config(base/'original/state',{'source_root':str(source),'paused':True})
 assert all(dict(s.km.db.execute('SELECT record_type,COUNT(*) FROM records GROUP BY record_type')).get(k)==v for k,v in counts.items())
 print('SEED_SECONDS',round(time.perf_counter()-t,3),flush=True);s.close()
else:
 target=base/a.label/'state';shutil.copytree(a.state_from or base/'original/state',target)
 out=ROOT/'evidence/performance_large';out.mkdir(parents=True,exist_ok=True)
 report={'label':a.label,'scope':'Linux synthetic large-content comparison; no target-host claim','runs':[]}
 def run(name,fn,profile=False):
  profiler=cProfile.Profile() if profile else None
  if profiler:profiler.enable()
  start=time.perf_counter();v=fn();elapsed=time.perf_counter()-start
  if profiler:
   profiler.disable();stream=io.StringIO();pstats.Stats(profiler,stream=stream).strip_dirs().sort_stats('cumtime').print_stats(22)
   (out/(a.label+'_'+name+'.txt')).write_text(stream.getvalue())
  row={'name':name,'seconds':round(elapsed,4)}
  if isinstance(v,dict):row.update(total_count=v.get('total_count'),rows=len(v.get('rows',[])))
  report['runs'].append(row);print(json.dumps(row),flush=True);return v
 s=KMSession(target,actor='synthetic:scale');run('initial_open',s.open,True);s.close()
 for i in range(2):
  s=KMSession(target,actor='synthetic:scale');run('reopen_'+str(i),s.open,True)
  run('summary_'+str(i),s.summary)
  first=run('all_mail_'+str(i),lambda:s.query('mail.tasks.query',{'task':'all','page_size':100}),True)
  run('unanswered_'+str(i),lambda:s.query('mail.tasks.query',{'task':'unanswered','page_size':100}),True)
  run('waiting_'+str(i),lambda:s.query('mail.tasks.query',{'task':'waiting','page_size':100}))
  run('all_mail_repeat_'+str(i),lambda:s.query('mail.tasks.query',{'task':'all','page_size':100}),True)
  record=first['rows'][0];rid=record.get('record_id') or record.get('display_record_id') or record['record']['record_id']
  run('content_'+str(i),lambda:s.content(rid),True)
  run('records_page_'+str(i),lambda:s.query('records.query',{'record_type':'Email','page_size':100}))
  if first.get('next_cursor'):
   run('mail_page_two_'+str(i),lambda:s.query('mail.tasks.query',{'task':'all','page_size':100,'cursor':first['next_cursor']},as_of=first['snapshot']['as_of']))
  s.close()
 (out/(a.label+'.json')).write_text(json.dumps(report,indent=2)+'\n')
