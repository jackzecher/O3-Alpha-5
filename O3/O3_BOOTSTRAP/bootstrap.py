"""O3 Universal Bootstrap 1.0 — GUI-independent runtime selection and supervision.

One nonproduction app implementation, NIPR fail-closed / home permanent local
setup / AI synthetic-only preview. The real Core, KM and GUI are never patched.
The native O3.exe owns the sole splash and failure diagnostic in the main folder.
"""
from __future__ import annotations
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time
import uuid

BOOTSTRAP_VERSION = '1.0.1'
BUILD_ID = '0.9.1-km12-p3-prism2'
R3_SHA256 = '56c9d68a7e1fd25a2303fcd1a578d05a2b2a7b04cc1312136bcc844455d6c644'
BOOT_DIR = Path(__file__).resolve().parent
ROOT = BOOT_DIR.parent
KIT = ROOT / 'O3_PRODUCT'
MANIFEST = BOOT_DIR / 'BOOTSTRAP_MANIFEST.json'
EVENT_PREFIX = 'O3_BOOT_EVENT='
PROBE_PREFIX = 'O3_TECHNICIAN_PROBE='
PINS = {'rdflib_version':'7.6.0', 'pyparsing_version':'3.3.3',
        'pyside6_version':'6.9.2', 'qt_version':'6.9.2', 'shiboken6_version':'6.9.2'}
PACKAGES = {'PySide6':'6.9.2', 'PySide6-Essentials':'6.9.2',
            'PySide6-Addons':'6.9.2', 'shiboken6':'6.9.2',
            'rdflib':'7.6.0', 'pyparsing':'3.3.3'}
INSTALLABLE = {'MISSING_RDFLIB','MISSING_PYPARSING','MISSING_PYSIDE6',
               'MISSING_SHIBOKEN6','NATIVE_QT_IMPORT_FAILED',
               'DEPENDENCY_VERSION_UNQUALIFIED','TIMEZONE_DATA_UNAVAILABLE'}
CLASSIFICATIONS = {'nipr':'WINDOWS_NIPR_ALPHA', 'home':'WINDOWS_NONNIPR_ALPHA'}
MODE = None
STAGE = 'BOOTSTRAP_INITIALIZING'

class Blocked(RuntimeError):
    def __init__(self, code):
        if not re.fullmatch('[A-Z0-9_]{1,80}', str(code)):
            code = 'BOOTSTRAP_FAILURE'
        self.code = code
        super().__init__(code)

def emit(kind, progress=None, message='', code=''):
    event={'kind':kind, 'mode':MODE or 'unknown', 'stage':STAGE}
    if progress is not None:event['progress']=int(progress)
    if message:event['message']=str(message)[:140]
    if code:event['code']=str(code)[:80]
    print(EVENT_PREFIX+json.dumps(event,separators=(',',':'),sort_keys=True),flush=True)

def stage(phase,percent,description):
    global STAGE
    STAGE=phase
    emit('STATUS',percent,description)

def filehash(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def verify_package(expected_manifest):
    """Verify two independent manifests without tying the launcher to GUI hashes."""
    from pathlib import PurePosixPath
    stage('PACKAGE_INTEGRITY',16,'Checking application integrity')
    if not re.fullmatch('[0-9a-f]{64}',expected_manifest or ''):
        raise Blocked('BOOTSTRAP_MANIFEST_NOT_BOUND')
    if filehash(MANIFEST)!=expected_manifest:
        raise Blocked('BOOTSTRAP_MANIFEST_MISMATCH')
    try:
        data=json.loads(MANIFEST.read_text(encoding='utf-8'))
        boot_files=data['files']
        if (data['format_version']!=1 or data['bootstrap_version']!=BOOTSTRAP_VERSION
                or data['application_build_id']!=BUILD_ID or data['source_authority_sha256']!=R3_SHA256
                or data['classification']!='O3_UNIVERSAL_ALPHA_NONPRODUCTION'
                or type(boot_files) is not dict or len(boot_files)!=3):
            raise ValueError('bootstrap contract')
        product=json.loads((KIT/'PACKAGE_MANIFEST.json').read_text(encoding='utf-8'))
        product_files=product['files']
        if (product['format_version']!=1 or product['build_id']!=BUILD_ID
                or type(product_files) is not dict or not 40<=len(product_files)<=240):
            raise ValueError('application contract')
        all_files={}
        for name,digest in boot_files.items():
            if not name.startswith('O3_BOOTSTRAP/') or not re.fullmatch('[0-9a-f]{64}',digest):
                raise ValueError('bootstrap path')
            all_files[name]=digest
        for name,digest in product_files.items():
            rel=PurePosixPath(name)
            if (not isinstance(name,str) or name=='PACKAGE_MANIFEST.json' or rel.is_absolute()
                    or '..' in rel.parts or '\\' in name or ':' in name
                    or not re.fullmatch('[0-9a-f]{64}',digest)):
                raise ValueError('application path')
            all_files['O3_PRODUCT/'+name]=digest
        for item in ROOT.rglob('*'):
            if item.is_symlink() or (item.exists() and getattr(item.lstat(),'st_file_attributes',0)&0x400):
                raise ValueError('reparse point')
        found={}
        for path in ROOT.rglob('*'):
            if not path.is_file():continue
            name=path.relative_to(ROOT).as_posix()
            if name in ('O3.exe','O3_STARTUP_DIAGNOSTIC.txt',
                        'O3_BOOTSTRAP/BOOTSTRAP_MANIFEST.json',
                        'O3_PRODUCT/PACKAGE_MANIFEST.json'):
                continue
            if '__pycache__' in path.parts or name.endswith(('.pyc','.pyo')):
                raise ValueError('development artifact')
            found[name]=filehash(path)
        if found!=all_files:
            raise ValueError('file set or hash mismatch')
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise Blocked('PACKAGE_INTEGRITY_INVALID') from None
    return filehash(MANIFEST)

def load_supervisor():
    spec=importlib.util.spec_from_file_location('o3_universal_supervisor', KIT/'_O3_START.py')
    if not spec or not spec.loader:raise Blocked('SUPERVISOR_MODULE_MISSING')
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module

def runtime_environment(prefix, mode):
    # Process-local only; never change PATH or Python/Conda installation on disk.
    env=dict(os.environ)
    for key in ('PYTHONHOME','PYTHONPATH','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH',
                'QTWEBENGINEPROCESS_PATH','QT_QPA_PLATFORM','QT_QPA_FONTDIR'):
        env.pop(key,None)
    windows=Path(env.get('SystemRoot','C:/Windows'))
    dirs=[prefix/'Library'/'bin',prefix/'DLLs',prefix/'Scripts',prefix,
          windows/'System32',windows]
    env['PATH']=';'.join(str(p) for p in dirs)
    for plugin in (prefix/'Library'/'plugins',prefix/'Lib'/'site-packages'/'PySide6'/'plugins',
                   prefix/'Lib'/'site-packages'/'PySide6'/'Qt'/'plugins'):
        if (plugin/'platforms'/'qwindows.dll').is_file():
            env['QT_PLUGIN_PATH']=str(plugin)
            break
    env['QT_QPA_PLATFORM']='windows'
    env['PYTHONDONTWRITEBYTECODE']='1'
    env['O3_BOOTSTRAP_MODE']=mode
    if mode=='nipr':
        env['CONDA_PREFIX']=str(prefix)
        env['CONDA_DEFAULT_ENV']='focus-native'
    else:
        env.pop('CONDA_PREFIX',None)
        env.pop('CONDA_DEFAULT_ENV',None)
    return env

def verify_selected_python(mode, selected):
    stage('RUNTIME_IDENTITY',25,'Checking selected Python runtime')
    if sys.platform!='win32' or struct.calcsize('P')!=8:
        raise Blocked('WINDOWS_X64_REQUIRED')
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise Blocked('ISOLATED_INVOCATION_REQUIRED')
    if tuple(sys.version_info[:2]) != (3,12):
        raise Blocked('PYTHON_VERSION_UNQUALIFIED')
    if mode=='nipr' and tuple(sys.version_info[:3]) != (3,12,15):
        raise Blocked('NIPR_VERSION_CHANGED')
    exe=Path(sys.executable).resolve()
    if (not selected.is_absolute() or selected.is_symlink() or exe!=selected.resolve()
            or exe.name.lower()!='python.exe'):
        raise Blocked('PYTHON_IDENTITY_MISMATCH')
    prefix=Path(sys.prefix).resolve()
    if mode=='nipr':
        if (prefix.name.casefold()!='focus-native' or exe.parent!=prefix
                or not (prefix/'conda-meta'/'history').is_file()):
            raise Blocked('CONDA_IDENTITY_MISMATCH')
    else:
        local=os.environ.get('LOCALAPPDATA','')
        intended=(Path(local)/'Programs'/'Python'/'Python312').resolve() if local else None
        if not local or prefix!=intended or exe.parent!=prefix or (prefix/'conda-meta').exists():
            raise Blocked('HOME_RUNTIME_UNQUALIFIED')
    return exe,prefix

def preflight(python, env, mode):
    stage('CHECK_DEPENDENCIES',38,'Checking Qt and knowledge libraries')
    command=[str(python),'-I','-B','-u','-X','utf8',str(BOOT_DIR/'probe.py')]
    try:
        r=subprocess.run(command,cwd=KIT,env=env,text=True,encoding='utf8',errors='replace',
                         stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except (OSError,subprocess.TimeoutExpired):
        raise Blocked('RUNTIME_PREFLIGHT_FAILED') from None
    result=[]
    for line in r.stdout.splitlines():
        if line.startswith(PROBE_PREFIX) and len(line)<4096:
            try:result.append(json.loads(line[len(PROBE_PREFIX):]))
            except ValueError:raise Blocked('RUNTIME_PREFLIGHT_INVALID') from None
    failures=[v.get('code') for v in result if isinstance(v,dict) and v.get('stage')=='FAIL']
    if failures:
        raise Blocked(failures[-1])
    if r.returncode!=0 or [v.get('stage') for v in result] != ['IDENTITY_OK','DEPENDENCIES_OK','NATIVE_OK']:
        raise Blocked('NATIVE_QT_START_FAILED' if len(result)>1 else 'RUNTIME_PREFLIGHT_INVALID')
    identity, deps, native=result
    pyver=identity.get('python_version')
    if (not isinstance(pyver,str) or not re.fullmatch(r'3\.12\.\d+',pyver)
            or (mode=='nipr' and pyver!='3.12.15')
            or identity.get('mode')!=mode or identity.get('python_bits')!=64
            or identity.get('python_executable_sha256')!=filehash(python)
            or deps.get('dependencies')!=PINS or deps.get('origins_within_runtime') is not True
            or deps.get('timezone_support') is not True
            or native.get('qt_platform')!='windows'
            or native.get('qt_event_loop') is not True
            or native.get('plugin_origin_within_runtime') is not True
            or native.get('qt_library_info_within_runtime') is not True):
        raise Blocked('RUNTIME_PREFLIGHT_INVALID')
    stage('NATIVE_QT',55,'Native Windows Qt verified')
    return {'classification':CLASSIFICATIONS[mode], 'python_version':pyver,'python_bits':64,
            'dependencies':PINS, 'python_executable_sha256':identity['python_executable_sha256'],
            'prefix_sha256':identity['prefix_sha256']}

def subprocess_silent(argv,env,timeout):
    try:
        return subprocess.run(argv,cwd=KIT,env=env,text=True,encoding='utf8',errors='replace',
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except (OSError,subprocess.TimeoutExpired):return None

@contextlib.contextmanager
def setup_mutex(home):
    import msvcrt
    f=home/'bootstrap-installer.lock'
    with f.open('a+b') as fd:
        fd.seek(0,os.SEEK_END)
        if fd.tell()==0:fd.write(b'0');fd.flush()
        deadline=time.monotonic()+1500
        while True:
            try:
                fd.seek(0);msvcrt.locking(fd.fileno(),msvcrt.LK_NBLCK,1);break
            except OSError:
                if time.monotonic()>deadline:raise Blocked('AUTO_SETUP_BUSY') from None
                time.sleep(.8)
        try:yield
        finally:
            fd.seek(0);msvcrt.locking(fd.fileno(),msvcrt.LK_UNLCK,1)

def package_versions(python,env):
    script=("import importlib.metadata as m,json; "
            "names=['PySide6','PySide6-Essentials','PySide6-Addons','shiboken6','rdflib','pyparsing']; "
            "r={}; "
            "[(r.__setitem__(n,next((d.version for d in m.distributions() "
            "if d.metadata.get('Name','').lower().replace('_','-')==n.lower().replace('_','-')),None))) for n in names]; "
            "print('O3_DEP_METADATA='+json.dumps(r,sort_keys=True))")
    run=subprocess_silent([str(python),'-I','-B','-c',script],env,50)
    if run is None or run.returncode!=0:raise Blocked('AUTO_SETUP_METADATA_FAILED')
    lines=[line[len('O3_DEP_METADATA='):] for line in run.stdout.splitlines() if line.startswith('O3_DEP_METADATA=')]
    if len(lines)!=1:raise Blocked('AUTO_SETUP_METADATA_FAILED')
    try:
        data=json.loads(lines[0]);assert set(data)==set(PACKAGES)
        return data
    except (ValueError,TypeError,AssertionError):raise Blocked('AUTO_SETUP_METADATA_FAILED') from None

def install_home_packages(python,prefix,env,workspace_root,issue):
    stage('INSTALL_HOME_COMPONENTS',44,'Installing missing components permanently')
    local=os.environ.get('LOCALAPPDATA','')
    if (not local or prefix!=(Path(local)/'Programs'/'Python'/'Python312').resolve()
            or (prefix/'conda-meta').exists()):
        raise Blocked('AUTO_SETUP_RESTRICTED_RUNTIME')
    with setup_mutex(workspace_root):
        available=package_versions(python,env)
        items=[f'{name}=={pin}' for name,pin in PACKAGES.items() if available.get(name)!=pin]
        # Check timezone data independently BEFORE the first package install.
        # Otherwise a missing Qt package masks missing tzdata until the next run.
        zone_script=("from zoneinfo import ZoneInfo; "
                     "ZoneInfo('America/New_York');ZoneInfo('America/Los_Angeles')")
        zone=subprocess_silent([str(python),'-I','-B','-c',zone_script],env,25)
        if zone is None or zone.returncode!=0:
            items.append('tzdata>=2025.1,<2028')
        if not items and issue in ('NATIVE_QT_IMPORT_FAILED','MISSING_PYSIDE6','MISSING_SHIBOKEN6'):
            items=[f'{name}=={pin}' for name,pin in PACKAGES.items()]
            force=True
        else:force=False
        if not items:raise Blocked('AUTO_SETUP_NO_REPAIR_AVAILABLE')
        pip=subprocess_silent([str(python),'-I','-B','-m','pip','--version'],env,35)
        if pip is None or pip.returncode!=0:
            setup=subprocess_silent([str(python),'-I','-B','-m','ensurepip','--upgrade','--default-pip'],env,100)
            if setup is None or setup.returncode!=0:raise Blocked('AUTO_SETUP_PIP_UNAVAILABLE')
        command=[str(python),'-I','-B','-m','pip','--isolated','install','--prefix',str(prefix),
                 '--no-input','--disable-pip-version-check','--no-warn-script-location',
                 '--only-binary=:all:','--index-url','https://pypi.org/simple']
        if force:command+=['--force-reinstall']
        result=subprocess_silent(command+items,env,1500)
        if result is None or result.returncode!=0:raise Blocked('AUTO_SETUP_INSTALL_FAILED')

def restore_workspace(sup,mode):
    stage('RESTORE_WORKSPACE',62,'Restoring the saved workspace')
    local=os.environ.get('LOCALAPPDATA','')
    if not local or not Path(local).is_dir():raise Blocked('LOCALAPPDATA_UNAVAILABLE')
    base=sup.plain_path(Path(local))
    if base.is_relative_to(KIT) or KIT.is_relative_to(base):raise Blocked('WORKSPACE_LOCATION_INVALID')
    folder={'nipr':'O3_NIPR_Alpha_1','home':'O3_NONNIPR_Alpha_1'}[mode]
    root=base/folder
    root.mkdir(parents=True,exist_ok=True)
    sup.plain_path(root)
    return root

def run_supervisor(sup,mode,python,env,workspace,report_path,contract,manifest_sha,product_sha):
    stage('START_APPLICATION',72,'Starting supervised O3')
    report={'build_id':BUILD_ID,'purpose':'O3_UNIVERSAL_ALPHA_USER_WORKSPACE',
            'started_at':datetime.now(timezone.utc).isoformat(),
            'classification':CLASSIFICATIONS[mode],'manifest_code':'MANIFEST_VERIFIED',
            'manifest_sha256':product_sha,'distribution_manifest_sha256':manifest_sha,
            'runtime':contract,'runtime_selection':'EXISTING_VERIFIED_RUNTIME',
            'organizational_runtime_approval':'NOT_GRANTED',
            'dependency_distribution_approval':'NOT_GRANTED',
            'operational_source_authorization':'NOT_INFERRED',
            'workspace_mode':'PERSISTENT_ENVIRONMENT_SPECIFIC_WORKSPACE','records':[]}
    results={}
    def supervise():
        saved=dict(os.environ)
        try:
            os.environ.update(env)
            results['returncode']=sup.supervise(KIT,workspace,report_path,report,python,contract)
        except Exception:
            results['returncode']=2
            report['supervisor']='SUPERVISOR_RUNTIME_ERROR'
        finally:
            os.environ.clear();os.environ.update(saved)
    worker=threading.Thread(target=supervise,daemon=False,name='O3SupervisedSession')
    worker.start()
    seen=0;ready=False
    try:
        while worker.is_alive():
            records=report.get('records',[])
            while seen<len(records):
                record=records[seen];seen+=1
                stage_name=record.get('stage')
                if record.get('result')=='PASS':
                    mapping={'validation':(75,'Application files validated'),
                             'identity':(78,'Runtime identity confirmed'),
                             'dependencies':(80,'Libraries initialized'),
                             'native':(84,'Native window initialized'),
                             'engine':(91,'Knowledge engine ready'),
                             'ready':(99,'Opening your workspace')}
                    if stage_name in mapping:
                        pc,label=mapping[stage_name]
                        stage('APPLICATION_'+stage_name.upper(),pc,label)
            if not ready and report.get('supervisor')=='APPLICATION_READY':
                ready=True
                stage('APPLICATION_READY',100,'O3 workspace ready')
                emit('READY',100,'O3 is ready')
            time.sleep(.08)
        worker.join()
    except KeyboardInterrupt:
        # The supervising process already owns shutdown and stop command.
        worker.join(timeout=3)
        raise
    if not ready and report.get('supervisor')=='APPLICATION_READY':
        emit('READY',100,'O3 is ready');ready=True
    rc=results.get('returncode',2)
    if rc or report.get('result')!='PASS':
        reason=report.get('supervisor')
        raise Blocked(reason if isinstance(reason,str) and re.fullmatch('[A-Z0-9_]{1,80}',reason)
                      else 'APPLICATION_SESSION_INCOMPLETE')
    emit('EXIT',100,'Clean shutdown')
    return 0

def run(argv=None):
    global MODE
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['nipr','home','preview'],required=True)
    parser.add_argument('--python',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--screenshot',type=Path)
    args=parser.parse_args(argv)
    MODE=args.mode
    try:
        verify_package(args.manifest_sha256)
        if args.mode=='preview':
            # Design mode must never reach an operational workspace or auto installer.
            stage('AI_SYNTHETIC_PREVIEW',35,'Opening isolated synthetic GUI')
            import importlib.util
            spec=importlib.util.spec_from_file_location('o3_gui_preview',BOOT_DIR/'preview.py')
            mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
            return mod.run_preview(emit,args.screenshot)
        exe,prefix=verify_selected_python(args.mode,args.python)
        sup=load_supervisor()
        _,product_sha=sup.verify_kit(sup.plain_path(KIT))
        # Preserve the exact NIPR/home workspace paths used by existing Alpha.
        root=restore_workspace(sup,args.mode)
        env=runtime_environment(prefix,args.mode)
        try:contract=preflight(exe,env,args.mode)
        except Blocked as initial:
            if args.mode!='home' or initial.code not in INSTALLABLE:
                raise
            install_home_packages(exe,prefix,env,root,initial.code)
            stage('POST_INSTALL_VERIFY',50,'Checking installed components')
            contract=preflight(exe,env,args.mode)
        state_api=sup.load_module(KIT,'_O3_WORKSPACE_STATE')
        workspace=state_api.prepare_workspace(KIT,root)
        _,logs=state_api.validate_workspace(KIT,workspace)
        sup.prune_reports(logs)
        report_path=logs / ('O3_session_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                            +'-'+uuid.uuid4().hex[:8]+'.json')
        return run_supervisor(sup,args.mode,exe,env,workspace,report_path,contract,
                              args.manifest_sha256,product_sha)
    except Blocked as error:
        emit('FAIL',code=error.code,message='See O3_STARTUP_DIAGNOSTIC.txt')
        return 2
    except Exception:
        # Never include exception repr, secrets or user source paths in diagnostics.
        emit('FAIL',code='BOOTSTRAP_UNEXPECTED_ERROR',message='See O3_STARTUP_DIAGNOSTIC.txt')
        return 2

if __name__=='__main__':
    raise SystemExit(run())
