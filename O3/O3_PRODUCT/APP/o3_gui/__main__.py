"""Launch O3 as one native Qt Widgets workbench without a custom splash."""
from __future__ import annotations
import sys
import traceback


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
        import rdflib  # Unified Core runtime dependency; home preflight verifies version.
        from o3_gui.presentation import CSS
        from o3_gui.app import run
        app=QApplication(sys.argv)
        app.setApplicationName('O3 Operational Intelligence')
        app.setOrganizationName('O3')
        app.setStyle('Fusion')  # Reproducible stock Qt Widget base; no theme add-on.
        app.setStyleSheet(CSS)
        return run()
    except Exception as exc:
        from o3_core.engine import default_state
        from datetime import datetime, timezone
        path=default_state()/'logs'
        path.mkdir(parents=True,exist_ok=True)
        trace=path/'startup_failure.txt'
        previous=trace.read_text(encoding='utf-8',errors='replace')[-35000:] if trace.exists() else ''
        with trace.open('w',encoding='utf-8') as file:
            file.write(previous)
            file.write(datetime.now(timezone.utc).isoformat()+' GUI startup exception\n')
            file.write(traceback.format_exc()[-10000:]+'\n')
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0,
                'O3 could not start. A local technician diagnostic is available under the O3 application logs directory.\n'+
                str(type(exc).__name__), 'O3 startup failure',0x10)
        except Exception:
            print('O3 GUI startup failed:',type(exc).__name__,file=sys.stderr)
        return 2


if __name__=='__main__':sys.exit(main())
