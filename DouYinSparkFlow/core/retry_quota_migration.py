"""Pure, fail-closed helpers for one-time legacy retry timezone backfills.

This module never reads storage, takes locks, or starts application services.
The CLI owns those effects; callers receive a copied candidate and an explicit
account-generation snapshot for compare-and-swap application.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RETRY_QUOTA_KEY = "refusal_retry_reservations"


@dataclass(frozen=True)
class BackfillPlan:
    """A validated candidate write and the snapshot required to apply it."""

    rows: list[dict]
    generations: dict[str, str]
    changes: list[tuple[str, str]]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _aware(value: datetime, label: str) -> None:
    _require(
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None,
        f"{label} must be timezone-aware",
    )


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _requested_refs(account_refs: Sequence[str]) -> list[str]:
    _require(isinstance(account_refs, (list, tuple, set)), "account refs must be explicit")
    refs = list(account_refs)
    _require(bool(refs) and all(_nonempty_text(ref) for ref in refs), "account refs must be non-empty strings")
    _require(len(set(refs)) == len(refs), "account refs must be unique")
    return refs


def _parse_legacy_reservation(value: object, target_ref: object, zone: ZoneInfo, cutoff: datetime, now: datetime) -> None:
    _require(_nonempty_text(target_ref), "reservation ref must be a non-empty string")
    _require(isinstance(value, dict), "reservation must be an object")
    _require(_nonempty_text(value.get("day")) and _nonempty_text(value.get("at")), "reservation day and timestamp are required")
    try:
        day = datetime.fromisoformat(f"{value['day']}T00:00:00").date()
        at = datetime.fromisoformat(value["at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("reservation day or timestamp is invalid") from exc
    _require(day.isoformat() == value["day"], "reservation day must be ISO calendar date")
    _aware(at, "reservation timestamp")
    _require(at <= now, "reservation timestamp is in the future")
    _require(at < cutoff, "reservation lacks legacy-writer provenance")
    _require(at.astimezone(zone).date() == day, "legacy timezone does not match stored day")
    _require(_nonempty_text(value.get("runId")), "reservation runId must be a non-empty string")
    identity_refs = value.get("identityRefs")
    _require(
        isinstance(identity_refs, list)
        and bool(identity_refs)
        and all(_nonempty_text(ref) for ref in identity_refs),
        "reservation identityRefs must be non-empty strings",
    )
    _require(len(set(identity_refs)) == len(identity_refs), "reservation identityRefs must be unique")


def plan_backfill(
    rows: object,
    *,
    account_refs: Sequence[str],
    legacy_timezone: str,
    writer_cutoff: datetime,
    now: datetime,
    expected_generations: Mapping[str, str] | None = None,
) -> BackfillPlan:
    """Return a copied candidate with proven legacy rows assigned ``legacy_timezone``.

    Every requested account must occur exactly once and provide a non-empty
    ``login_generation``. If ``expected_generations`` is supplied, it is a CAS
    snapshot from a prior dry run and must still match before a candidate can be
    created. Records with an existing timezone are left byte-for-value intact.
    """

    _require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "account storage must be a list of objects")
    refs = _requested_refs(account_refs)
    _require(_nonempty_text(legacy_timezone), "legacy timezone is required")
    try:
        zone = ZoneInfo(legacy_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("legacy timezone is unknown") from exc
    _aware(writer_cutoff, "writer cutoff")
    _aware(now, "current time")
    _require(writer_cutoff <= now, "writer cutoff is in the future")
    if expected_generations is not None:
        _require(isinstance(expected_generations, Mapping), "generation snapshot is invalid")
        _require(set(expected_generations) == set(refs), "generation snapshot does not match requested accounts")
        _require(all(_nonempty_text(value) for value in expected_generations.values()), "generation snapshot is invalid")

    result = deepcopy(rows)
    generations: dict[str, str] = {}
    changes: list[tuple[str, str]] = []
    for account_ref in refs:
        matches = [row for row in rows if row.get("account_ref") == account_ref]
        _require(len(matches) == 1, "account ref is missing or ambiguous")
        account = matches[0]
        generation = account.get("login_generation")
        _require(_nonempty_text(generation), "account login generation is required")
        if expected_generations is not None:
            _require(generation == expected_generations[account_ref], "account generation changed")
        generations[account_ref] = generation
        bucket = account.get(RETRY_QUOTA_KEY, {})
        _require(isinstance(bucket, dict), "retry quota bucket is unreadable")
        for target_ref, reservation in bucket.items():
            if isinstance(reservation, dict) and reservation.get("timezone") not in (None, ""):
                continue
            _parse_legacy_reservation(reservation, target_ref, zone, writer_cutoff, now)
            result_row = next(row for row in result if row.get("account_ref") == account_ref)
            result_row[RETRY_QUOTA_KEY][target_ref]["timezone"] = zone.key
            changes.append((account_ref, target_ref))
    return BackfillPlan(rows=result, generations=generations, changes=changes)
