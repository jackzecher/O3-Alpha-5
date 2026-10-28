O3 ALPHA 5 — OFFLINE AI LAUNCH AND NATIVE QT CAPTURE

Alpha 5 preserves the protected Windows startup and the separate offline AI
runtime beside O3/.
This runner launches the CURRENT application, including the real Core/KM worker,
against a fresh disposable synthetic workspace. It does not use the unsupported
legacy bootstrap preview or an imitation of the application UI.

USAGE (from the extracted package root on a supported Linux host)

Verify the package and bundled dependencies without extracting the runtime:
    python3 -I -S AI_RUNTIME/run.py --check-only

Create seven actual Qt screenshots and a machine-readable execution report:
    python3 -I -S AI_RUNTIME/run.py --capture-all ./captures

Capture a specific view, including larger-text/smaller-window checks:
    python3 -I -S AI_RUNTIME/run.py --screenshot ./reader.png --view reader --width 1060 --height 800 --text-scale 1.25

Interactive use on a Linux desktop with a graphical display:
    python3 -I -S AI_RUNTIME/run.py

Views: mail (default), workspace, records, reader, connections, settings, sources.
Text scale: 1.0, 1.25, 1.5. Minimum window size: 860 x 680.
Capture paths must be outside O3/ and AI_RUNTIME/.

RUNTIME AND ISOLATION
- Bundled PySide6 Essentials/Qt/shiboken6 6.9.2, RDFLib 7.6.0, pyparsing 3.3.3.
- No network, pip, downloads, or permanent dependency installation is required
  when RUNNING this package. Dependencies are verified and extracted temporarily.
- Requires host CPython 3.11-3.13, Linux x86_64, glibc >= 2.28 and Qt system shared
  libraries such as libGL. The bundle is not an OS or a Python interpreter.
- -I -S is required to exclude host Python packages and startup hooks.
- Every run creates its own synthetic source tree and workspace. No personal or
  operational Workspace is opened; no automatic source discovery is performed.
- External source selection, restore and migration are disabled by the runner's
  small development-only window subclass. Normal reading, navigation, editing of
  synthetic knowledge, relationship exploration and settings use the actual GUI.
- Temporary libraries, synthetic data and caches are removed after normal exit.
  PNGs and the requested capture report remain at the selected output location.
- Synthetic edits are disposable. Close the main window to end interactive use.
- This workflow does not test O3.exe, the Windows Qt platform, or NIPR deployment.
  Do not run AI_RUNTIME on NIPR. Use the protected Windows launcher normally.

FIXTURES
The fiction-only Harbor Readiness examples were recovered from the Step 2
source/evidence engineering seed. They include correspondence, profiles, calendar
and document captures, an authored effort and evidence-linked relationships.
Inbox and Sent include fictional reply and waiting examples. One document
intentionally has partial extraction. These are useful honest-state examples, not operational records.
Unlike the Alpha 2 static preview, this runner opens a disposable SQLite database,
uses the real worker/service and saves preferences ONLY within that temporary tree.

REPOSITORY HANDOFF
Import AI_RUNTIME/ as development support beside the Windows product. Keep its
runtime dependencies, licensing notices, fixture generator and runner available
for AI and human development. Keep generated captures/workspaces out of O3/.
AI_RUNTIME_MANIFEST.json binds this exact Alpha 5 and all support files.
If development changes any bound file, regenerate the development manifest from
reviewed source as part of packaging; never bypass its checks or change Windows
startup policy to make this runner work. The original Windows integrity verifiers
continue to apply independently to O3/.

DEPENDENCY ORIGIN
Qt wheels were reused byte-for-byte from the supplied Alpha 2 AI runtime.
The pure-Python bundle was updated from published RDFLib 7.6.0 and pyparsing 3.3.3
wheels to match O3's dependency pins. Upstream metadata and licenses are retained.
See THIRD_PARTY_NOTICES.txt, licenses/ and package metadata in the dependency ZIP.
