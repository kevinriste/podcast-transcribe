"""Persist raw source HTML at intake, keyed to episode identity, for later backfill.

Paths are relative to the subproject CWD (imap/ rss/ archive/), matching the
existing pipeline convention (e.g. ../prepare-text/text-input-raw). The store is
gitignored; it holds private source content.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_STORE_ROOT = Path("../intake-html")


def slug_source(source: str) -> str:
    """Return a filesystem-safe slug for a source/sender name.

    Args:
        source: The source or sender display name/address.

    Returns:
        A lowercase, hyphen-separated slug (<=80 chars), or ``"unknown"`` when empty.

    """
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    return slug[:80] or "unknown"


def store_intake_html(
    *,
    source: str,
    episode_id: str,
    html: str,
    url: str,
    intake_type: str,
    store_root: Path = DEFAULT_STORE_ROOT,
) -> Path:
    """Write raw HTML plus a metadata sidecar for one intake item.

    No-ops (still returning the intended path) when ``html`` is empty or whitespace.
    Overwrites idempotently and creates parent directories.

    Args:
        source: Source/sender name; slugged into the directory name.
        episode_id: Episode identity stamp shared with the text/audio outputs.
        html: Raw source HTML to persist.
        url: The article's source URL, recorded in the sidecar.
        intake_type: Intake path label (email/link/rss/archive).
        store_root: Root directory of the store; defaults to ``../intake-html``.

    Returns:
        The path to the HTML file (written unless ``html`` was empty).

    """
    html_path = store_root / slug_source(source) / f"{episode_id}.html"
    if not html.strip():
        return html_path
    html_path.parent.mkdir(parents=True, exist_ok=True)
    _ = html_path.write_text(html, encoding="utf-8")
    meta = {
        "source": source,
        "episode_id": episode_id,
        "url": url,
        "intake_type": intake_type,
        "stored_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
    }
    _ = html_path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return html_path
