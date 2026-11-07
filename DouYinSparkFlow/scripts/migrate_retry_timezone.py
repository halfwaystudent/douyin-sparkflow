"""Explicit, offline migration for retry reservations missing timezone provenance."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filelock import FileLock
from core.browser import BROWSER_ACCOUNT_LOCK_DIR, browser_account_lock_name
from core.retry_quota_migration import BackfillPlan, plan_backfill
from core.tasks import task_run_lock
from utils.config import update_user_data
from utils.secure_storage import read_json


USER_DATA_FILE = "usersData.json"


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_600(path: Path, now: datetime) -> Path:
    """Copy the exact storage payload before a write without changing its mode."""
    backup_dir = path.parent / ".data-backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    name = f"{now.astimezone(timezone.utc):%Y%m%d-%H%M%S-%f}-retry-timezone-{path.name}"
    destination = backup_dir / name
    with path.open("rb") as source, os.fdopen(
        os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as output:
        shutil.copyfileobj(source, output)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(destination, 0o600)
    return destination


def _account_locks(data_dir: Path, rows: list[dict], account_refs: Sequence[str]) -> list[Path]:
    selected = {ref: next(row for row in rows if row.get("account_ref") == ref) for ref in account_refs}
    lock_dir = data_dir / BROWSER_ACCOUNT_LOCK_DIR
    return sorted(
        {
            lock_dir / f"{browser_account_lock_name(row)}.lock.guard"
            for row in selected.values()
        },
        key=str,
    )


def _result(plan: BackfillPlan, *, mode: str, backup: Path | None = None) -> dict:
    result = {
        "mode": mode,
        "planned_updates": len(plan.changes),
        "updated_reservations": len(plan.changes) if mode == "applied" else 0,
        "account_generations": dict(plan.generations),
    }
    if backup is not None:
        result["backup"] = str(backup)
    return result


def migrate(
    data_dir: Path | str,
    *,
    account_refs: Sequence[str],
    legacy_timezone: str,
    writer_cutoff: datetime,
    now: datetime | None = None,
    apply: bool = False,
) -> dict:
    """Plan by default; apply only under the runtime task and store locks."""
    data_dir = Path(data_dir)
    target = data_dir / USER_DATA_FILE
    now = now or datetime.now(timezone.utc)
    if apply and data_dir.resolve() != Path.cwd().resolve():
        raise ValueError("data_dir must equal current runtime CWD for apply")
    if not target.exists():
        raise FileNotFoundError(target)

    def snapshot():
        with FileLock(f"{target}.lock", timeout=0):
            original = read_json(target)
            snapshot_sha256 = _raw_sha256(target)
            plan = plan_backfill(
                original,
                account_refs=account_refs,
                legacy_timezone=legacy_timezone,
                writer_cutoff=writer_cutoff,
                now=now,
            )
        return original, snapshot_sha256, plan

    if not apply:
        original, snapshot_sha256, plan = snapshot()
        return _result(plan, mode="dry-run")

    # The runtime task lock is the same lock used by scheduled task runs. Keep
    # it across planning, sender guards, canonical update, and readback.
    with task_run_lock():
        original, snapshot_sha256, plan = snapshot()
        if not plan.changes:
            return _result(plan, mode="dry-run")

        # The browser sender holds these exact .guard locks for physical accounts.
        with ExitStack() as locks:
            for lock_path in _account_locks(data_dir, original, account_refs):
                locks.enter_context(FileLock(str(lock_path), timeout=0))
            if _raw_sha256(target) != snapshot_sha256:
                raise ValueError("account storage changed after migration snapshot")
            backup = _backup_600(target, now)

            def mutate(current: list[dict]):
                if _raw_sha256(target) != snapshot_sha256:
                    raise ValueError("account storage changed after migration snapshot")
                confirmed = plan_backfill(
                    current,
                    account_refs=account_refs,
                    legacy_timezone=legacy_timezone,
                    writer_cutoff=writer_cutoff,
                    now=now,
                    expected_generations=plan.generations,
                )
                if confirmed.changes != plan.changes or confirmed.rows != plan.rows:
                    raise ValueError("account storage changed after migration snapshot")
                current[:] = confirmed.rows
                return {"updated_reservations": len(confirmed.changes)}, True

            # Canonical update_user_data takes the actual global storage FileLock.
            outcome = update_user_data(mutate, path=target, force_reload=True)
            after = read_json(target)
            if after != plan.rows:
                raise RuntimeError("independent migration readback differs")
            # Restoring exactly the changed timezone fields must recover the old
            # object, including all credentials and unknown application fields.
            restored = json.loads(json.dumps(after))
            for account_ref, target_ref in plan.changes:
                before_row = next(row for row in original if row.get("account_ref") == account_ref)
                after_row = next(row for row in restored if row.get("account_ref") == account_ref)
                before_value = before_row["refusal_retry_reservations"][target_ref]
                after_value = after_row["refusal_retry_reservations"][target_ref]
                if "timezone" in before_value:
                    after_value["timezone"] = before_value["timezone"]
                else:
                    after_value.pop("timezone", None)
            if restored != original or outcome.get("updated_reservations") != len(plan.changes):
                raise RuntimeError("migration changed fields other than timezone")
            return _result(plan, mode="applied", backup=backup)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601 with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601 with timezone")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--account-ref", action="append", required=True, help="repeat for each legacy account")
    parser.add_argument("--legacy-timezone", required=True, help="proven timezone used by the old writer")
    parser.add_argument("--writer-cutoff", required=True, type=_parse_timestamp, help="exclusive ISO-8601 legacy writer cutoff")
    parser.add_argument("--apply", action="store_true", help="write the validated plan (default is dry-run)")
    args = parser.parse_args(argv)
    result = migrate(
        args.data_dir,
        account_refs=args.account_ref,
        legacy_timezone=args.legacy_timezone,
        writer_cutoff=args.writer_cutoff,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
