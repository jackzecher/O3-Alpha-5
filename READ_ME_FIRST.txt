O3 ALPHA 5.3 - STARTUP AND MAIL PERFORMANCE REPAIR

1. Close O3. Extract this complete ZIP into a NEW local folder.
2. Keep the O3 folder intact. Open O3/O3.exe and select NIPR as usual.
3. Use the existing focus-native environment and existing workspace.
   Keep the workspace and recovery files. No Conda package changes are needed.

WHAT WAS FIXED
Alpha 5.2 repeated whole-database integrity checks inside schema validation.
It also kept every full message body in its reply-evidence index, although
reply detection only uses metadata. Mail filtering and sorting decoded all
those bodies again. Alpha 5.3 removes this redundant work. Full integrity and
foreign-key checks, full message content, body search and reply semantics remain.

The synthetic comparison used 10,000 actual ingested records with long content,
including 6,000 messages. Repeat opening and mail navigation improved. Exact
query-result comparisons, integrity failures, real Qt interactions and clean
shutdown were checked. See SUPPORT/ALPHA_5_3 for results and limitations.
Linux measurements are not a promise of Windows/NIPR timings.

EXISTING WORKSPACE
No new schema or catalog change is introduced. An already converted Alpha 5
workspace opens directly. If the workspace still has the supported Alpha 4
catalog, the Alpha 5.2 protected upgrade, verified checkpoint and cancellation
behavior remain. The native launcher, environment checks and dependency pins
are unchanged. Do not open a converted workspace with Alpha 4 without using
the supported verified rollback procedure.

VALIDATION STATUS
This exact candidate still needs Windows/NIPR host acceptance. If startup fails,
return O3_ENGINE_STARTUP_DIAGNOSTIC.json and the native session report. The
diagnostic build should be alpha5.3 and successful startup should report ready.
Existing log location: %LOCALAPPDATA%\O3_NIPR_Alpha_1\O3_Workspace\logs

ALPHA_5_3_VERIFICATION.json and SUPPORT/ALPHA_5_3 describe this release.
BASE_* files and other SUPPORT directories describe earlier releases only.
AI_RUNTIME is separate offline Linux support, not the NIPR entry point.
