"""Actual engine worker failures: bounded redacted evidence, unchanged semantics."""
from pathlib import Path
import json, tempfile, time
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication
from o3_gui.worker import EngineWorker
from o3_gui.startup_diagnostics import StartupDiagnostics, FILENAME
from o3_gui.km_session import KMSession
from o3_km.service import KMService
from o3_km.evaluation import KMInvalid

app=QCoreApplication([])
report={'scope':'Synthetic Linux Qt worker, real Core/KM; injected startup failures','checks':{}}
def read(worker):return json.loads(worker.startup_diagnostics.path.read_text())
def worker_at(state):
 w=EngineWorker(state);failed=[];ready=[];w.failed.connect(lambda op,error:failed.append((op,error)));w.ready.connect(ready.append)
 return w,failed,ready
with tempfile.TemporaryDirectory(prefix='o3-startup-diag-') as td:
 base=Path(td)
 # Standalone and native fresh state must remain valid with logging enabled.
 for native in (False,True):
  root=base/('native' if native else 'standalone');state=root/'state';state.mkdir(parents=True)
  if native:
   (root/'workspace-owner.json').write_text('{}');(root/'logs').mkdir()
  w,failed,ready=worker_at(state);w.initialize()
  assert ready and not failed,(native,failed)
  d=read(w);assert d['status']=='ready' and not d['errors']
  assert not (state/'logs').exists()
  w.close_engine();report['checks']['fresh_native' if native else 'fresh_standalone']=True
 # Capture the error before KM turns it into a generic response.
 state=base/'translated'/'state';state.mkdir(parents=True)
 original=KMService._dispatch
 def dispatch(service,op,*args):
  if op=='view.starters.ensure':raise RuntimeError('BACKUP_REQUIRED: private-account private-email@example.invalid C:\\private\\record.json')
  return original(service,op,*args)
 w,failed,ready=worker_at(state)
 with patch.object(KMService,'_dispatch',dispatch):w.initialize()
 assert failed and not ready
 d=read(w);assert d['status']=='failed'
 assert any(e.get('error_code')=='BACKUP_REQUIRED' for e in d['errors'])
 assert any(f['file']=='o3_km/service.py' for e in d['errors'] for f in e['frames'])
 assert d['errors'][0]['stage']=='starter_conversion_and_checkpoint'
 serialized=json.dumps(d)
 assert 'private-account' not in serialized and 'private-email' not in serialized and str(base) not in serialized
 w.close_engine();report['checks']['original_km_exception_before_translation_and_no_sensitive_strings']=True
 # Native identity/permission failures keep their actual numeric OS code.
 state=base/'identity'/'state';state.mkdir(parents=True);w,failed,ready=worker_at(state)
 with patch('o3_gui.km_session.local_actor',side_effect=PermissionError(13,'private message',str(base/'private-record'))):w.initialize()
 d=read(w);assert failed and d['errors'][0]['errno']==13 and d['stage']=='account_identity'
 assert 'private' not in json.dumps(d);w.close_engine();report['checks']['account_identity_failure_with_errno']=True
 # A report distinguishes timeout from immediate exceptions and keeps its stage.
 diag=StartupDiagnostics(base/'timeout'/'state');diag.phase('starter_conversion_and_checkpoint');diag.finish('timeout');diag.phase('startup_summary');diag.finish('ready')
 d=json.loads(diag.path.read_text());assert d['status']=='timeout' and d['completed_after_timeout']
 report['checks']['timeout_retained_even_if_worker_later_completes']=True
 # An unwritable diagnostics destination must not change engine readiness.
 state=base/'log_failure'/'state';state.mkdir(parents=True);w,failed,ready=worker_at(state)
 with patch('o3_gui.startup_diagnostics.os.replace',side_effect=PermissionError(13,'denied')):w.initialize()
 assert ready and not failed;w.close_engine();report['checks']['diagnostic_io_failure_does_not_block_engine']=True
out=Path(__file__).resolve().parents[1]/'evidence/startup_diagnostic';out.mkdir(exist_ok=True)
report['passed']=True;(out/'checks.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
