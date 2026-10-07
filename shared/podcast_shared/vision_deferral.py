"""Hold back episodes whose image descriptions failed, and retry them on later runs.

An item (a raw file in prepare-text) whose vision call fails is left where it is and
recorded here under its key (the file name). Descriptions already obtained are cached so a
retry only asks for the missing ones. Retries are spaced out, and after ``DEFER_LIMIT`` the
item publishes with caption/alt text instead.

Within one run, a ``VisionCircuit`` stops vision calls once the service looks down (or the
run's vision time budget is spent). Items reached after that are skipped for this run
without counting as a failed attempt or alerting, so one outage produces one alert.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, TypedDict

from podcast_shared.describe import VisionRejectedError, VisionUnavailableError
from podcast_shared.json_narrow import is_json_object

if TYPE_CHECKING:
    from podcast_shared.describe import Describer

# How long to keep retrying before publishing with caption/alt text.
DEFER_LIMIT = timedelta(hours=24)
# Minimum gap between retries of one deferred item (runs are every 20 minutes).
RETRY_INTERVAL = timedelta(hours=1)
# Total seconds of vision calls per run; past this, remaining items wait for the next run.
RUN_BUDGET_SECONDS = 300.0


class Deferral(TypedDict):
    """Retry state for one deferred item."""

    first_deferred: str  # ISO-8601, timezone-aware
    last_attempt: str  # ISO-8601, timezone-aware
    descriptions: dict[str, str]  # image src -> description already obtained
    alerted: bool  # whether the "deferred" alert has been sent for this item


@dataclass(slots=True)
class VisionPlan:
    """What to do with one item this run."""

    action: Literal["process", "wait"]
    cache: dict[str, str] = field(default_factory=dict[str, str])
    allow_undescribed: bool = False
    previously_deferred: bool = False


@dataclass(slots=True)
class VisionCircuit:
    """Per-run breaker: once vision is down (or the time budget is spent), stop calling it."""

    open: bool = False
    spent: float = 0.0
    budget: float = RUN_BUDGET_SECONDS

    def tripped(self) -> bool:
        """Whether vision should not be called again this run.

        Returns:
            True after an outage-class failure or once ``budget`` seconds have been spent.

        """
        return self.open or self.spent >= self.budget


class VisionSkippedError(VisionUnavailableError):
    """Vision was not attempted for this item because the circuit is tripped for this run."""


def _aware(value: object) -> datetime | None:
    """Parse a timezone-aware ISO timestamp.

    Returns:
        The datetime, or None if ``value`` is not an aware ISO-8601 string.

    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class DeferralStore:
    """The on-disk set of deferred items."""

    def __init__(self, path: pathlib.Path) -> None:
        """Load state from ``path``; a missing or malformed file (or entry) is ignored."""
        self.path: pathlib.Path = path
        self.entries: dict[str, Deferral] = {}
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  (JSON boundary; narrowed below)
        except (OSError, ValueError):
            return
        if not is_json_object(raw):
            return
        for key, entry in raw.items():
            if not is_json_object(entry):
                continue
            first = entry.get("first_deferred")
            last = entry.get("last_attempt")
            descs = entry.get("descriptions")
            if _aware(first) is None or _aware(last) is None:
                continue
            if not isinstance(first, str) or not isinstance(last, str):
                continue
            cache = {k: v for k, v in descs.items() if isinstance(v, str) and v} if is_json_object(descs) else {}
            alerted = entry.get("alerted")
            self.entries[key] = {
                "first_deferred": first,
                "last_attempt": last,
                "descriptions": cache,
                "alerted": alerted if isinstance(alerted, bool) else True,
            }

    def save(self) -> None:
        """Write state atomically (temp file + rename), so a crash never leaves a torn file."""
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.entries, fh, indent=2)
            _ = pathlib.Path(tmp).replace(self.path)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise

    def plan(self, key: str, now: datetime) -> VisionPlan:
        """Decide whether to process an item now, and with what cached descriptions.

        Returns:
            ``wait`` for a deferred item whose next retry is not due; otherwise ``process``,
            with ``allow_undescribed`` once the deferral limit has passed.

        """
        entry = self.entries.get(key)
        if entry is None:
            return VisionPlan(action="process")
        first = _aware(entry["first_deferred"]) or now
        last = _aware(entry["last_attempt"]) or first
        expired = now - first >= DEFER_LIMIT
        if not expired and now - last < RETRY_INTERVAL:
            return VisionPlan(action="wait", previously_deferred=True)
        return VisionPlan(
            action="process",
            cache=dict(entry["descriptions"]),
            allow_undescribed=expired,
            previously_deferred=True,
        )

    def record_failure(self, key: str, cache: dict[str, str], now: datetime) -> bool:
        """Record (or extend) a deferral after vision failed.

        Returns:
            True if this item was not already deferred (the caller should alert once).

        """
        entry = self.entries.get(key)
        self.entries[key] = {
            "first_deferred": entry["first_deferred"] if entry else now.isoformat(),
            "last_attempt": now.isoformat(),
            "descriptions": {k: v for k, v in cache.items() if v},
            "alerted": True,
        }
        return entry is None or not entry["alerted"]

    def stash(self, key: str, cache: dict[str, str], now: datetime) -> bool:
        """Record a run-level skip without counting it as an attempt.

        An existing entry keeps its timing and gains any new descriptions. A new entry is
        created already due, so the next run tries again, and its ``DEFER_LIMIT`` clock starts
        now; otherwise a long outage would keep skipping it without the clock ever starting.
        No alert is implied.

        Returns:
            True if the state changed (the caller should save).

        """
        descriptions = {k: v for k, v in cache.items() if v}
        entry = self.entries.get(key)
        if entry is not None:
            if descriptions == entry["descriptions"]:
                return False
            entry["descriptions"] = descriptions
            return True
        self.entries[key] = {
            "first_deferred": now.isoformat(),
            "last_attempt": (now - RETRY_INTERVAL).isoformat(),
            "descriptions": descriptions,
            "alerted": False,
        }
        return True

    def clear(self, key: str) -> bool:
        """Forget an item once it has been published.

        Returns:
            True if an entry was removed.

        """
        return self.entries.pop(key, None) is not None

    def prune(self, live_keys: set[str]) -> bool:
        """Drop entries for items that are gone (published some other way, or deleted by hand).

        Returns:
            True if anything was removed.

        """
        stale = [key for key in self.entries if key not in live_keys]
        for key in stale:
            logging.info("Dropping vision deferral for %s (no longer waiting)", key)
            del self.entries[key]
        return bool(stale)


def make_describer(
    describe: Describer,
    cache: dict[str, str],
    circuit: VisionCircuit,
    *,
    allow_undescribed: bool,
    failures: list[str],
) -> Describer:
    """Wrap ``describe`` with the per-item cache, the per-run circuit, and the expiry fallback.

    Descriptions are read from and written to ``cache``. An image the API rejects outright
    falls back to "" (caption/alt) and is listed in ``failures``. Otherwise, without
    ``allow_undescribed``, a failure raises ``VisionUnavailableError`` (deferring the item),
    and a tripped circuit raises ``VisionSkippedError`` (skipping it for this run). With
    ``allow_undescribed``, each failing image falls back to "" and is listed in ``failures``,
    so later images in the same item are still described.

    Returns:
        The wrapped describer.

    """

    def fallback(src: str) -> str:
        failures.append(src)
        return ""

    def wrapped(src: str, alt: str, caption: str) -> str:
        if src in cache:
            return cache[src]
        if circuit.tripped():
            if allow_undescribed:
                return fallback(src)
            msg = "vision is unavailable for the rest of this run"
            raise VisionSkippedError(msg)
        start = time.monotonic()
        try:
            description = describe(src, alt, caption)
        except VisionRejectedError as exc:
            logging.warning("%s; using caption/alt for this image", exc)
            return fallback(src)
        except VisionUnavailableError as exc:
            if exc.outage:
                circuit.open = True
            if not allow_undescribed:
                raise
            return fallback(src)
        finally:
            circuit.spent += time.monotonic() - start
        cache[src] = description
        return description

    return wrapped
