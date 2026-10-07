"""Convert HTML raw files to body text, holding back files whose image descriptions fail.

A raw file marked ``META_BODY_FORMAT: html`` carries the article's HTML as its body. Before
filters and cleaning see it, prepare-text runs it through the shared HTML stage
(``podcast_shared.html_body``). If its images can't be described yet, the file stays in
``text-input-raw/`` and is retried on later runs (``podcast_shared.vision_deferral``), and
after the deferral limit it publishes with caption/alt text.
"""

from __future__ import annotations

import logging
import pathlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from podcast_shared import (
    VisionUnavailableError,
    describe_image,
    parse_html_body,
    render_html_body,
    send_gotify_notification,
)
from podcast_shared.vision_deferral import (
    DEFER_LIMIT,
    DeferralStore,
    VisionCircuit,
    VisionSkippedError,
    make_describer,
)

if TYPE_CHECKING:
    from podcast_shared.describe import Describer

VISION_DEFERRALS_PATH = pathlib.Path("vision-deferrals.json")

# Headers only the HTML stage reads; they are dropped once the body is text.
_STAGE_KEYS = frozenset({"body_format", "content_selector", "preface"})


class HtmlStage:
    """The HTML stage for one prepare-text run: deferral state plus the per-run vision circuit."""

    def __init__(self, state_path: pathlib.Path | None = None, describe: Describer | None = None) -> None:
        """Load the deferral state (default ``VISION_DEFERRALS_PATH``); describe images with ``describe_image``."""
        self.deferrals: DeferralStore = DeferralStore(state_path or VISION_DEFERRALS_PATH)
        self.circuit: VisionCircuit = VisionCircuit()
        self.describe: Describer = describe or describe_image

    def convert(self, name: str, metadata: dict[str, str], html: str) -> tuple[dict[str, str], str] | None:
        """Turn one HTML raw file into body text.

        Reads ``content_selector`` (the article element; empty means detect it),
        ``source_url`` (for relative image URLs) and ``preface`` (a line spoken before the
        article) from ``metadata``.

        Returns:
            ``(metadata, body)`` with ``extraction`` set and the stage headers dropped, or
            None when the file is held back for a later run.

        """
        now = datetime.now(tz=UTC)
        plan = self.deferrals.plan(name, now)
        if plan.action == "wait":
            logging.info("Vision deferred; not retrying %s yet", name)
            return None
        body = parse_html_body(
            html,
            content_selector=metadata.get("content_selector", ""),
            base_url=metadata.get("source_url", ""),
        )
        failures: list[str] = []
        describer = make_describer(
            self.describe, plan.cache, self.circuit, allow_undescribed=plan.allow_undescribed, failures=failures
        )
        title = metadata.get("title") or name
        try:
            text = render_html_body(body, describer)
        except VisionSkippedError:
            # Vision is down (or over budget) for this run: not a real attempt, so no alert
            # and no change to the retry schedule. Keep any descriptions obtained.
            if self.deferrals.stash(name, plan.cache, now):
                self.deferrals.save()
            logging.info("Skipping %s this run: vision unavailable", name)
            return None
        except VisionUnavailableError as exc:
            newly_deferred = self.deferrals.record_failure(name, plan.cache, now)
            self.deferrals.save()
            logging.warning("Deferring %s to a later run: %s", name, exc)
            if newly_deferred:
                send_gotify_notification(
                    "Episode deferred: image descriptions unavailable",
                    f"{title}\n\n{exc}\n\nRetrying hourly; publishes with caption/alt text after {DEFER_LIMIT}.",
                )
            return None
        if self.deferrals.clear(name):
            self.deferrals.save()
        if failures:
            send_gotify_notification(
                "Image descriptions unavailable",
                f"{title}: {len(failures)} image(s) published with caption/alt text.",
            )
        preface = metadata.get("preface", "")
        if preface:
            text = f"{preface}\n\n{text}"
        converted = {key: value for key, value in metadata.items() if key not in _STAGE_KEYS}
        converted["extraction"] = "structured" if body.structural else "plaintext"
        return converted, text

    def prune(self, waiting: set[str]) -> None:
        """Forget deferrals for files no longer waiting in ``text-input-raw/``."""
        if self.deferrals.prune(waiting):
            self.deferrals.save()
