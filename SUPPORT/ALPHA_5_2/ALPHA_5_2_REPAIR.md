# Alpha 5.2 checkpoint and protected upgrade repair

The target-host evidence identifies a concrete redundant content-verification
pass. In the exact Alpha 5.1 verifier, `_validate_archive` took 66.98 seconds
with repeated samples in migration.py's hash read loop (87/88 -> 217 -> 878).
The following complete Core provenance audit took 6.05 seconds. Verification
succeeded in 81.01 seconds. Prior standalone extraction, CRC, SQLite and cleanup
checks also passed. This establishes an expensive code path and a startup
lifecycle mismatch; it does not establish why those reads cost more on Windows
or demonstrate any particular endpoint-security interference.

## Resulting behavior

The migration path checks all distinct archive references for safe names,
containment, file existence and symlinks. It then calls the existing Core audit,
which hashes the actual bytes for each distinct (path, expected hash), validates
both reference tables, and checks retained provenance/history. The redundant
migration content read immediately before this audit is removed. Standalone
`_validate_archive` callers retain their full hashing behavior. ZIP extraction
still independently verifies each member's manifest SHA-256. Source validation
before snapshot and validation of the recovered snapshot both remain.

The read-only startup preflight recognizes the exact Alpha 4 catalog and emits
maintenance before constructing an Engine or writer. The unchanged native
wrapper reports WORKSPACE_UPGRADE_REQUIRED. Its existing supervisor recognizes
that state while the native window is visible. The same owning worker performs
the checkpoint and conversion; readiness follows actual session completion.
No schema, workspace root, runtime policy, launcher, dependency pin, protocol
identifier or authoritative data model changed.

The upgrade dialog reports real stages and available file counts. Close uses a
thread-safe stop event, checked between files/chunks and by SQLite's progress
handler. Failed work closes the writer and permits retry. The five-minute
cooperative upgrade recovery limit is separate from the unchanged ordinary
45-second initialization watchdog. Neither cancellation nor that limit can
interrupt an individual OS call blocked below Python. Accepted conversion
writes remain one atomic transaction; a committed conversion is not presented
as having been undone when Close is requested.

## Contract mapping and evidence

The reference is Managed_NIPR_Windows_Python_Tool_Build_Contract(1).md,
SHA-256 048a8a2b009c38c33bc840029a0181b9b669972adec3328cc1e4cb8dcf62823b.
Earlier working Alpha 2 and Alpha 4 startup code was compared: neither performed
the new Alpha 5 conversion checkpoint inside ordinary KMSession.open.

| Requirement | Repair/evidence | Scope |
|---|---|---|
| Section 7 supervised state and actual readiness | Maintenance before writer; no ready while verification is held; ready after conversion | Actual Linux Qt and adapted frozen wrapper |
| Section 7 ordinary responsive startup | Heavy catalog upgrade uses existing protected setup state; current catalog remains ordinary startup | Synthetic fresh/current/old catalog |
| Section 9 consistent backup before migration | SQLite backup API, full tree inventory, content checks, schema/identity/owner and Core audit before atomic conversion | Synthetic Alpha 4 conversion and rollback |
| Section 11 observable bounded work and recovery | Progress, cooperative stop, recoverable timeout, same-window retry | Actual Qt with injected delay/timeout |
| Frozen native/runtime contract | Nine protected files compared to original Alpha 2; native manifest verifiers; AI runtime payload preserved | Static/integrity checks, not Windows execution |
| Target executable acceptance | Must launch this exact ZIP's O3.exe and collect ready/clean shutdown | Pending on Windows/NIPR |

Focused evidence includes checkpoint_single_pass.json (actual tamper and expected
hash conflicts), migration_checkpoint.json, migration_conversion.json,
upgrade_lifecycle_qt/checks.json, startup_checkpoint_qt/checks.json,
verifier_diagnostics*.json and alpha52_wrapper/*.json. Reports describe their
own scope. Earlier baseline reports are retained as historical records only.

## Reproduce fixture-based tests

Copy the matching complete Alpha 4 release alongside this source and retain the
included offline AI runtime from this release. Prepare the development runtime
with tools/prepare_test_runtime.py. Generate the synthetic fixture using:

```sh
python3 -B tests/startup_checkpoint_scale.py seed --package ../alpha4_restore/package --fixture .test_work/checkpoint_scale --output evidence/alpha4_scale_seed.json
python3 -I -S tools/dev_qt.py tests/checkpoint_single_pass.py
python3 -I -S tools/dev_qt.py tests/upgrade_lifecycle_qt.py
python3 -I -S tools/dev_qt.py tests/verifier_diagnostics.py
python3 -I -S tools/dev_qt.py tests/verifier_diagnostics_qt.py
python3 -I -S tools/dev_qt.py tests/startup_checkpoint_qt.py --fixture .test_work/checkpoint_scale
python3 -B tests/continuity_wrapper.py alpha4_upgrade --strict-manifests --output evidence/alpha52_wrapper
python3 -B tests/continuity_wrapper.py alpha4_cancel --strict-manifests --output evidence/alpha52_wrapper
```

The >45-second test uses a controlled delay. Its purpose is lifecycle acceptance,
not a claim about actual Windows launch speed. No user data is used in tests.
