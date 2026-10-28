# Alpha 5.3 startup and mail performance repair

Alpha 5.2's checkpoint repair did not resolve repeat startup or mail navigation
cost. A new 10,000-record fixture ingested through the actual Alpha 4 application
contains 6,000 long emails, 2,000 contacts, 1,500 long documents and 500 events.
Its original sources total 327.86 MiB. The former short-content benchmark did
not expose the overhead as clearly.

## Measured defects and changes

1. `validate_v4` called `validate_workspace_schema`, which already ran SQLite
   quick-check and foreign-key-check, and then repeated both checks. Preflight
   and service construction each use this validator. Alpha 5.3 removes only
   the duplicate pair within the validator. Inherited and extension schemas,
   catalog identity, Core provenance triggers and complete integrity checks
   remain mandatory. Preflight and writer construction still validate separately.
2. `ReplyEvidence` decoded and retained every message body, despite its comment
   saying it kept only evidence fields. On the shared fixture, it retained
   211,554,000 body characters. Alpha 5.3 projects out `body_text` before Python
   decoding. The resulting index retains zero body characters; every other
   evidence field and the existing graph/uncertainty behavior are preserved.
3. Configured mail views decoded all full bodies while evaluating metadata
   fields and global ordering. Alpha 5.3 omits bodies from candidate rows only
   when neither saved nor current text search is active and body text is not
   an exposed typed field. Selected pages and excluded examples fetch full
   records. Search still uses complete content. No database contents change.

## Comparison

These are synthetic Linux measurements, not Windows/NIPR results. Core profile
runs use identical cProfile instrumentation; each value below is the mean of
two measured runs after opening once. They exclude the one-time Alpha 4 catalog
conversion. Cold-cache/open-after-copy timings vary and are retained in the raw
reports rather than presented as a warmed-start guarantee.

| Operation | Alpha 4 | Alpha 5.2 | Alpha 5.3 candidate |
|---|---:|---:|---:|
| Repeat session open | 0.550 s | 1.080 s | 0.683 s |
| First All mail query | 1.361 s | 1.990 s | 0.829 s |
| Unanswered query | 1.142 s | 1.348 s | 0.795 s |
| Waiting query | 0.966 s | 1.049 s | 0.389 s |
| All mail page two | 1.174 s | 0.893 s | 0.277 s |

A separate actual Qt run on copies of the same converted state measured startup
to ready at 1.271 s versus 0.644 s, All mail at 2.738 s versus 0.950 s,
Unanswered at 0.949 s versus 0.506 s, and Waiting at 1.079 s versus 0.506 s
(Alpha 5.2, then candidate). These are single samples, not statistical bounds.
Other views are not uniformly faster; the built-in conversation collection, for
example, measured 1.743 s versus 2.101 s. This repair targets the identified
duplicate startup and configured-mail work, not every possible latency source.
Both Qt runs completed reader, inline expansion, navigation and clean shutdown
without executing session queries on the GUI thread. Raw timings include event
loop intervals; remaining UI pauses are not claimed eliminated.

## Correctness and scope

Seventeen public query outputs matched the released Alpha 5.2 byte-for-byte
after canonical JSON encoding, using identical state and a fixed as-of time.
The cases cover reply views, paging, ascending/descending metadata and reply
sorts, grouped filtering and search. Additional guards verify body-only saved
and current searches, complete selected/excluded/reader bodies, unchanged source
files, rejection of failed quick-check and dangling foreign keys, and exactly
one full pair of integrity checks within each validator call. Existing reply,
sorting, saved-rule, checkpoint, conversion and Qt interaction suites also run.

The Windows/NIPR contract is unchanged. No timeout is lengthened, no schema or
catalog is converted for an already converted Alpha 5 workspace, and no runtime
policy or launcher is altered. All nine protected launch files must match the
original baseline. Alpha 5.2's verified recovery and upgrade lifecycle repair
is retained. Native Windows/NIPR execution of this exact release is pending.

## Reproduce

Use the complete matching Alpha 4 and Alpha 5.2 packages alongside the source,
and prepare the bundled offline development runtime. The fixture contains only
fictional records and must stay outside product directories.

```sh
python3 -B tests/scale_runtime_profile.py seed --package ../alpha4_restore/package --fixture .test_work/large_content
python3 -B tests/scale_runtime_profile.py measure --package ../alpha4_restore/package --fixture .test_work/large_content --label alpha4
python3 -B tests/scale_runtime_profile.py measure --package ../alpha5_2_repair/package --fixture .test_work/large_content --label alpha52
python3 -B tests/scale_runtime_profile.py measure --package package --fixture .test_work/large_content --label candidate --state-from .test_work/large_content/alpha52/state
python3 -B tests/performance_regression.py guards --output evidence/performance_guards.json
python3 -B tests/performance_regression.py snapshot --package ../alpha5_2_repair/package --state .test_work/large_content/alpha52/state --output evidence/performance_large/alpha52_semantics.json
python3 -B tests/performance_regression.py snapshot --state .test_work/large_content/alpha52/state --output evidence/performance_large/candidate_semantics.json
python3 -B tests/scale_runtime_ui.py --package package --state .test_work/large_content/alpha52/state --output evidence/performance_large/candidate_ui
```

Raw reports are in `evidence/performance_large` and the release support directory.
