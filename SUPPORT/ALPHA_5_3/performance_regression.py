"""Content/integrity guards and exact public-result comparison for Alpha 5.3."""
from pathlib import Path
import argparse, hashlib, json, shutil, sqlite3, sys, tempfile
from continuity_support import ROOT, runtime, tree_hashes, write_report


def rejected(fn, text):
    try:
        fn()
    except RuntimeError as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError('Invalid database accepted')


def guards(package):
    report = {'scope': 'Synthetic integrity and full-content regression', 'checks': {}}
    with tempfile.TemporaryDirectory(prefix='o3-performance-guards-') as temporary:
        base = Path(temporary)
        runtime(package, base)
        from o3_gui.km_session import KMSession
        from o3_km.workspace_types import validate_v4
        from o3_km.reply_evidence import context
        from acceptance_shared import write_mail
        state = base/'state'; source = base/'O3Data'
        for folder in ('Mail/Inbox', 'Mail/Sent', 'Contacts/Profiles', 'Documents', 'Calendars'):
            (source/folder).mkdir(parents=True, exist_ok=True)
        write_mail(source, 'long', 'Keep this message')
        write_mail(source, 'excluded', 'Exclude this message')
        for path in source.glob('Mail/*/*.json'):
            data = json.loads(path.read_text())
            data['body_html'] = '<p>body-only-needle ' + 'Long retained source context. '*2000 + '</p>'
            path.write_text(json.dumps(data))
        before = tree_hashes(source)
        s = KMSession(state, actor='synthetic:performance'); s.open(); s.scan(source)
        db = s.km.db; traced = []
        db.set_trace_callback(traced.append)
        validate_v4(db)
        db.set_trace_callback(None)
        checks = [sql.strip().casefold() for sql in traced]
        assert checks.count('pragma quick_check') == 1, checks
        assert checks.count('pragma foreign_key_check') == 1, checks
        report['checks']['one_complete_integrity_pass_per_validation'] = True

        class FailedQuickCheck:
            def execute(self, sql, *args):
                if sql.casefold() == 'pragma quick_check':
                    return self
                return db.execute(sql, *args)
            def fetchone(self):
                return ('synthetic corrupt page',)
        rejected(lambda: validate_v4(FailedQuickCheck()), 'INTEGRITY_FAILED')
        report['checks']['failed_quick_check_rejected'] = True
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute("INSERT INTO ws_records VALUES ('wsrec_missing','missing','Invalid',1,'2026-01-01','2026-01-01',1)")
        db.commit()
        rejected(lambda: validate_v4(db), 'foreign key')
        db.execute("DELETE FROM ws_records WHERE record_id='wsrec_missing'"); db.commit()
        db.execute('PRAGMA foreign_keys=ON')
        validate_v4(db)
        report['checks']['dangling_workspace_reference_rejected'] = True

        spec = {'label': 'Body preservation', 'record_type': 'Email',
                'filter_expression': {'field': 'subject', 'operator': 'contains', 'value': 'Keep'},
                'sorts': [{'field': 'source_time', 'direction': 'desc'}]}
        result = s.query('view.preview', {'specification': spec, 'page_size': 100})
        assert len(result['rows']) == 1
        assert 'body-only-needle' in result['rows'][0]['additional']['body_text']
        assert len(result['excluded_examples']) == 1
        assert 'body-only-needle' in result['excluded_examples'][0]['record']['additional']['body_text']
        for params in ({'search': 'body-only-needle'}, {}):
            searching = dict(spec)
            if not params: searching['search'] = 'body-only-needle'
            found = s.query('view.preview', {'specification': searching, **params})
            assert [r['record_id'] for r in found['rows']] == [r['record_id'] for r in result['rows']]
        content = s.content(result['rows'][0]['record_id'])
        assert 'body-only-needle' in json.dumps(content)
        assert all('body_text' not in added for _, added in context(s.km).records.values())
        assert tree_hashes(source) == before
        assert s.engine.audit_provenance()['ok'] and s.km.audit_workspace()['ok']
        report['checks'].update(full_output_and_excluded_body_preserved=True,
            body_only_saved_and_current_search_preserved=True,
            reader_and_sources_preserved=True, reply_index_retains_no_bodies=True)
        s.close()
    report['passed'] = True
    return report


def snapshot(package, state):
    """Run each package on a copy of the exact same already-converted state."""
    report = {'scope': 'Exact public KM outputs on a shared synthetic state', 'results': {}}
    with tempfile.TemporaryDirectory(prefix='o3-performance-diff-') as temporary:
        base = Path(temporary); runtime(package, base)
        from o3_gui.km_session import KMSession
        from o3_km.reply_evidence import context
        target = base/'state'; shutil.copytree(state, target)
        s = KMSession(target, actor='synthetic:performance-diff'); s.open()
        as_of = '2026-10-27T23:00:00Z'
        def query(label, operation, params):
            result = s.query(operation, params, as_of=as_of)
            raw = json.dumps(result, sort_keys=True, separators=(',', ':')).encode()
            report['results'][label] = {'sha256': hashlib.sha256(raw).hexdigest(),
                'total_count': result.get('total_count'), 'rows': len(result.get('rows', []))}
            return result
        for task in ('all', 'unanswered', 'waiting'):
            params = {'task': task, 'page_size': 30}
            first = query(task, 'mail.tasks.query', params)
            if first.get('next_cursor'):
                query(task+'_page2', 'mail.tasks.query', {**params, 'cursor': first['next_cursor']})
        for field in ('subject', 'source_time', 'reply_status', 'sender_identity'):
            for direction in ('asc', 'desc'):
                spec = {'label': 'Exact differential', 'record_type': 'Email',
                        'sorts': [{'field': field, 'direction': direction}]}
                query(field+'_'+direction, 'view.preview', {'specification': spec, 'page_size': 30})
        spec = {'label': 'Exact grouped differential', 'record_type': 'Email',
                'filter_expression': {'field': 'subject', 'operator': 'contains', 'value': '1'},
                'group_by_fields': ['mailbox_id', 'conversation_id'],
                'sorts': [{'field': 'source_time', 'direction': 'desc'}]}
        query('grouped_filter', 'view.preview', {'specification': spec})
        query('body_search', 'view.preview', {'specification': spec, 'search': 'retained'})
        spec['search'] = 'Synthetic long saved context'
        query('saved_body_search', 'view.preview', {'specification': spec})
        index = context(s.km)
        report['reply_index'] = {'records': len(index.records),
            'retained_body_characters': sum(len(added.get('body_text', '')) for _, added in index.records.values())}
        s.close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('guards', 'snapshot'))
    parser.add_argument('--package', type=Path, default=ROOT/'package')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = guards(args.package.resolve()) if args.mode == 'guards' else snapshot(args.package.resolve(), args.state.resolve())
    write_report(args.output, result)
    print(json.dumps(result, indent=2))
