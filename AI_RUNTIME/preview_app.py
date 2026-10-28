"""Run the shipped Qt application against disposable synthetic Core/KM state."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback


def launch(args, temporary, manifest, versions):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication
    import o3_gui.app as ui
    from o3_gui.km_client import WorkspaceLock
    from o3_gui.logic import read_config
    from o3_gui.panels import SettingsPanel, SourcesPanel
    from seed import seed

    # Every run owns a NEW workspace. There is no option to open an existing one.
    base = temporary / 'synthetic'
    state = seed(base)
    source = (base / 'O3Data').resolve()
    ui.discover_sources = lambda: []

    class SyntheticWindow(ui.O3Window):
        def fixture_only(self):
            self.show_information('Synthetic AI preview',
                'This session uses disposable fictional records. External sources and '
                'workspace restores are disabled in this development runner.')
        def choose_source(self):
            self.fixture_only()
        def do_restore(self):
            self.fixture_only()
        def begin_migration(self):
            self.fixture_only()
        def scan_sources(self):
            if Path(self.config.get('source_root', '')).resolve() != source:
                raise RuntimeError('Synthetic source binding changed')
            return super().scan_sources()

    # Offscreen Qt otherwise supplies a screen smaller than the requested
    # desktop window, which can leave partially repainted capture regions.
    if args.capture_all or args.screenshot:
        screen = temporary / 'virtual_screen.json'
        screen.write_text(json.dumps({'screens': [{'width': max(1600, args.width + 80),
            'height': max(1100, args.height + 80), 'logicalDpi': 96,
            'logicalBaseDpi': 96}]}))
        os.environ['QT_QPA_PLATFORM'] = 'offscreen:configfile=' + str(screen)
    app = QApplication(['O3 synthetic AI preview'])
    app.setApplicationName('O3 synthetic AI preview')
    app.setOrganizationName('O3 development')
    app.setStyle('Fusion')
    errors = []
    previous_hook = sys.excepthook
    def on_exception(typ, value, tb):
        errors.append(''.join(traceback.format_exception(typ, value, tb)))
        print(errors[-1], file=sys.stderr, flush=True)
        if window is not None:
            QTimer.singleShot(0, window.close)
    sys.excepthook = on_exception
    lock = WorkspaceLock(state)
    if not lock.tryLock(0):
        raise RuntimeError('Synthetic workspace is already locked')
    window = None
    dialogs = []
    captures = []
    clean = False
    exit_code = 0
    summary = {'scope': 'synthetic Linux Qt execution; NOT Windows/NIPR qualification',
               'application_build_id': manifest['application_build_id'],
               'runtime': versions, 'captures': captures, 'fixture_only': True,
               'temporary_workspace': True, 'network_required': False}

    def wait(predicate, timeout=20, check_errors=True):
        deadline = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            if check_errors and errors:
                raise RuntimeError('Qt callback failed: ' + errors[-1])
            if check_errors and window is not None and window.last_error:
                raise RuntimeError('Application operation failed: ' + str(window.last_error))
            if time.monotonic() >= deadline:
                raise RuntimeError('Qt operation timed out')
            time.sleep(.01)
        for _ in range(5):
            app.processEvents()
        if check_errors and errors:
            raise RuntimeError('Qt callback failed: ' + errors[-1])

    def settle():
        until = time.monotonic() + .15
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(.01)

    try:
        window = SyntheticWindow(state, read_config(state), lock)
        window.mode_chip.setText('SYNTHETIC AI PREVIEW')
        window.setWindowTitle('O3 ' + manifest['application_build_id'] + ' | Synthetic AI preview')
        window.apply_scale(args.text_scale, persist=False)
        window.resize(args.width, args.height)
        window.show()
        wait(lambda: window.initialized)
        window.go_home()
        wait(lambda: window.page == 'overview' and bool(window.home_data))
        summary['engine_ready'] = window.initialized
        summary['qt_platform'] = app.platformName()
        summary['correspondence_rows'] = len(window.home_surface.projected)
        if not window.home_surface.projected:
            raise RuntimeError('Synthetic correspondence did not load')
        selected=next((rid for rid,row in window.home_surface.projected.items() if 'presentation requested' in row['label']),next(iter(window.home_surface.projected)))

        def activate(view):
            for dialog in dialogs:
                dialog.close()
            dialogs.clear()
            if view == 'mail':
                window.open_mail()
                wait(lambda: window.mail_workspace.ready and window.page == 'mail')
            elif view == 'workspace':
                window.go_home()
                wait(lambda: window.page == 'overview' and window.overview.isEnabled() and bool(window.home_data))
            elif view == 'records':
                window.open_collection()
                wait(lambda: window.collection_ready and bool(window.record_surface.projected))
            elif view == 'reader':
                window.open_record(selected)
                wait(lambda: window.current_detail and window.current_detail['id'] == selected
                     and window.page == 'reader' and bool(window.reader_text.toPlainText()))
            elif view == 'connections':
                window.open_spatial(selected)
                wait(lambda: window.spatial.seed == selected and window.spatial.canvas.isEnabled())
                if not window.spatial.nodes or not window.spatial.edges:
                    raise RuntimeError('Synthetic relationship graph did not load')
                summary['spatial_nodes'] = len(window.spatial.nodes)
                summary['spatial_edges'] = len(window.spatial.edges)
            elif view in ('settings', 'sources'):
                dialog = (SettingsPanel if view == 'settings' else SourcesPanel)(window)
                dialogs.append(dialog)
                dialog.show()
                settle()
                return dialog
            settle()
            return window

        def capture(view, path):
            widget = activate(view)
            if not widget.grab().save(str(path), 'PNG'):
                raise RuntimeError('Failed to save native Qt PNG: ' + str(path))
            captures.append({'view': view, 'file': path.name,
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                             'width': widget.width(), 'height': widget.height()})
            print('NATIVE_QT_PNG:', path, flush=True)

        if args.capture_all:
            output = Path(args.capture_all)
            for view in ('mail', 'workspace', 'records', 'reader', 'connections', 'settings', 'sources'):
                capture(view, output / ('O3_Alpha5_' + view + '.png'))
        elif args.screenshot:
            capture(args.view, Path(args.screenshot))
        else:
            activate(args.view)
            print('SYNTHETIC_WINDOW_READY: close the main window to end this disposable session', flush=True)
            exit_code = app.exec()
        if errors:
            raise RuntimeError('Qt callback errors occurred')
    finally:
        for dialog in dialogs:
            dialog.close()
        if window is not None:
            if not window.allow_close:
                window.close()
            wait(lambda: window.allow_close and not window.thread.isRunning(), check_errors=False)
            clean = not lock.isLocked() and not window.isVisible()
        else:
            lock.unlock()
        sys.excepthook = previous_hook
    if not clean or exit_code:
        raise RuntimeError('Synthetic session did not shut down cleanly')
    with sqlite3.connect(state / 'o3.sqlite') as database:
        integrity = database.execute('PRAGMA integrity_check').fetchall()
    if integrity != [('ok',)]:
        raise RuntimeError('Synthetic workspace integrity check failed')
    summary.update(result='PASS_SYNTHETIC_QT', shutdown_clean=True, sqlite_integrity='ok', errors=errors)
    if args.capture_all:
        (Path(args.capture_all) / 'O3_AI_preview_result.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2), flush=True)
    return 0
