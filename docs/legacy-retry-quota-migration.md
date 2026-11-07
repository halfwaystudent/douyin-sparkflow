# Legacy retry-quota timezone migration

`migrate_retry_timezone.py` is an offline repair for retry reservations that
were written before timezone provenance was stored. It never starts a browser,
sends a message, changes schedules, or changes retry policy. The runtime reader
continues to treat missing or invalid timezone provenance conservatively.

## Preconditions

- For `--apply`, change directory to the actual application data root and pass
  that same directory as `--data-dir` (or omit it). The apply path rejects a
  different runtime CWD before creating a backup or writing. Dry runs may read
  an arbitrary `--data-dir`.
- Use only an operator-proven legacy timezone and the exclusive cutoff of the
  old writer. Do not infer either value from the current host timezone.
- List every intended `account_ref` explicitly. The option is repeatable.
- Make the configured secure-storage key available when `usersData.json` is
  encrypted. The tool reads and writes through the existing storage helpers;
  it does not change encryption mode.

## Dry run first

The default is read-only. It validates the account-reference uniqueness and
login-generation snapshot, plus every eligible reservation's ISO day, aware
past timestamp, old-writer cutoff, timezone/day agreement, run ID, and identity
references.

```bash
python scripts/migrate_retry_timezone.py \
  --data-dir /path/to/application-data \
  --account-ref ACCOUNT_REF \
  --account-ref ANOTHER_ACCOUNT_REF \
  --legacy-timezone Region/City \
  --writer-cutoff <exclusive-legacy-writer-cutoff-ISO-8601>
```

Rows that already contain a timezone are left unchanged. Any unreadable or
unproven selected legacy row stops the operation rather than widening a quota.

## Apply

For apply, run from the actual data root and point `--data-dir` at that same
absolute directory (or omit `--data-dir` when the script's default is that
root):

```bash
cd /path/to/application-data
python /path/to/DouYinSparkFlow/scripts/migrate_retry_timezone.py \
  --data-dir "$PWD" \
  --account-ref ACCOUNT_REF \
  --legacy-timezone Region/City \
  --writer-cutoff <exclusive-legacy-writer-cutoff-ISO-8601> \
  --apply
```

After reviewing the dry-run JSON, repeat the exact command with `--apply`.
The write path:

1. acquires the runtime `core.tasks.task_run_lock()` plus the normal global
   account-store lock and the browser sender's per-physical-account lock guards;
2. creates an exact pre-write backup in `.data-backups/` with mode `0600`;
3. rechecks the file hash, account generations, and planned result inside
   canonical `update_user_data` (CAS);
4. independently reads storage back and verifies that removing only the newly
   added `timezone` fields restores the original object.

A concurrent login or credential update causes the CAS to fail without
replacing those newer credentials. A failed run may leave its pre-write backup;
it never attempts a rollback over concurrent data.
