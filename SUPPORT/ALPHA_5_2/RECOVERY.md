# Recovering the exact Alpha 4 workspace

The first conversion creates `O3_Recovery/alpha4-before-alpha5-<id>.zip` beside
the owned workspace. A native workspace checkpoint contains its state,
configuration, archive, logs and ownership marker. SQLite is copied consistently;
locks and SQLite sidecars are excluded. The checkpoint records file hashes and
workspace identities. The source export folder is not changed.

Keep the matching complete Alpha 4 package. Simply opening Alpha 4 against a
converted database is not a supported downgrade, even though both use schema 4.

Close all O3 processes and stop its writer. Use the Alpha 5 offline helper in the
existing approved Python environment; it needs the Alpha 5 application import
path but does not change the native launcher or install dependencies:

```python
from pathlib import Path
from o3_km.migration import verify_alpha5_checkpoint, restore_alpha5_checkpoint

backup = Path(r"<full path to the generated checkpoint ZIP>")
state = Path(r"<full path to the existing workspace state directory>")
print(verify_alpha5_checkpoint(backup))
result = restore_alpha5_checkpoint(
    backup, state, exclusive_writer_stopped=True, confirmed=True
)
print(result)
```

Those flags are assertions of an actually stopped writer and an intentional
rollback. Do not invoke this helper from a running GUI session. It checks both
ownership and KM identities, validates all checkpoint contents, and retains the
replaced Alpha 5 directory. After successful restoration, open the matching
Alpha 4 application normally. Preserve the rollback directory until satisfied.

Ordinary verified Settings > Recovery backups/restores remain available for
normal use. Do not overwrite the workspace with a live SQLite file or mix its
ownership marker with a different workspace. Tests/migration_checkpoint.py,
migration_conversion.py and continuity_upgrade.py exercise this path, including
failure midway through conversion and reopening the restored state in real Alpha 4.
