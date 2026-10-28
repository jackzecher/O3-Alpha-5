"""Real Alpha 4 calendar ingestion -> timed Alpha 5 conversion.

One ICS capture can support many records. Measure full initialization without
changing evidence, timers or the recovery contract. Fixture paths are synthetic.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('phase', choices=('seed', 'measure'))
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--fixture', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--events', type=int, default=1500)
args = parser.parse_args()
sys.path[:0] = [str(ROOT / '.dev_runtime/libraries'),
                 str(args.package / 'O3/O3_PRODUCT/APP')]
from o3_gui.km_session import KMSession
from o3_gui.logic import save_config

base = args.fixture.resolve()
if args.phase == 'seed':
    source = base / 'O3Data'
    (source / 'Calendars').mkdir(parents=True)
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//O3 Synthetic//EN']
    for index in range(args.events):
        lines.extend(['BEGIN:VEVENT', f'UID:synthetic-{index}@example.test',
                      'DTSTAMP:20261001T120000Z', 'DTSTART:20261101T120000Z',
                      'DTEND:20261101T130000Z', f'SUMMARY:Synthetic event {index}',
                      'DESCRIPTION:' + 'Synthetic captured context. ' * 220, 'END:VEVENT'])
    lines.extend(['END:VCALENDAR', ''])
    capture = source / 'Calendars/events.ics'
    capture.write_text('\r\n'.join(lines), encoding='utf-8')
    state = base / 'original/state'
    session = KMSession(state, actor='synthetic:alpha4')
    session.open()
    session.scan(source)
    count = session.km.db.execute("SELECT count(*) FROM records WHERE record_type='Event'").fetchone()[0]
    assert count == args.events, count
    save_config(state, {'source_root': str(source), 'paused': True,
                        'alpha3': {'drafts': {'preserve': 'Saved synthetic draft'}}})
    session.close()
    result = {'events': count, 'capture_bytes': capture.stat().st_size,
              'seeded_with': str(args.package)}
else:
    from o3_gui.startup_diagnostics import StartupDiagnostics
    from o3_km import migration
    target = base / args.output.stem / 'state'
    shutil.copytree(base / 'original/state', target)
    phases = []
    hash_calls = [0]
    hashed_bytes = [0]
    original_hash = migration._file_hash
    def counted_hash(path):
        hash_calls[0] += 1
        hashed_bytes[0] += Path(path).stat().st_size
        return original_hash(path)
    migration._file_hash = counted_hash
    for name in ('_validate_archive', '_verify_core_provenance_readonly', '_source_roots', 'verify_alpha5_checkpoint'):
        original = getattr(migration, name)
        def timed(*a, _original=original, _name=name, **kw):
            began = time.monotonic()
            print('START ' + _name, flush=True)
            try:
                return _original(*a, **kw)
            finally:
                phases.append({'phase': _name, 'seconds': round(time.monotonic()-began, 3)})
                print(json.dumps(phases[-1]), flush=True)
        setattr(migration, name, timed)
    diag = StartupDiagnostics(target)
    session = KMSession(target, actor='synthetic:alpha5', startup_diagnostics=diag)
    started = time.monotonic()
    session.open()
    elapsed = time.monotonic() - started
    receipt = json.loads(session.km._meta('alpha5_starter_conversion_v1'))
    assert receipt['checkpoint']['verified']
    assert (target/'workspace.json').read_bytes() == (base/'original/state/workspace.json').read_bytes()
    session.close()
    diag.finish('ready')
    result = {'open_seconds': round(elapsed, 3), 'exceeds_45_second_limit': elapsed >= 45,
              'archive_hash_calls': hash_calls[0], 'archive_hash_bytes': hashed_bytes[0],
              'phases': phases, 'checkpoint_verified': True, 'saved_config_unchanged': True}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result, indent=2), flush=True)
