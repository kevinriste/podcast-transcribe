# Recommendations

Priority levels:
- P0 Critical: risk of data loss or cross-service impact.
- P1 High: likely failure or significant reliability/perf issue.
- P2 Medium: quality, maintainability, or cost optimizations.
- P3 Low: nice-to-have improvements.

## P0 Critical

- N/A (no current P0 items)

## P1 High

- Guard `rss/check-rss.py` against missing `diagnosis` dir and `html_content=None` before calling `bare_extraction` to avoid feed-wide crashes.
- Ensure `text-to-speech/text-input-empty-files` exists before moving empty files to avoid hard failures in `process.sh`.
- Add explicit timeouts and retry policy for `requests` calls (Gotify, Wayback, scraper) so cron runs do not hang indefinitely.
- Use `uv sync --frozen` (or equivalent) in cron to avoid pulling new dependency versions every 20 minutes.

## P2 Medium

- Move `playwright install` out of the cron loop (provision once, or only if browsers are missing).
- Align on a newer Python version (3.11/3.12) and update `pyproject.toml` and Pipfiles to match; keep `pyenv` and `uv` consistent.
- Clean or deduplicate archived inputs in `text-to-speech/input-text-archive` as needed.
- Guard Beehiiv parsing when `msg.text` is missing by falling back to `msg.html` or an empty string to avoid IMAP crashes.
- Handle RSS entries without `content[0].value` by falling back to `summary`/`description`.
- Consider splitting the monolithic cron flow into per-stage steps with isolated retries (IMAP, RSS, TTS, Dropcaster) to reduce “one failure blocks all” behavior.
- Replace shell orchestration with a small Python runner (or Makefile + Python) to improve error handling and portability without changing behavior.
- Parameterize hardcoded paths and service URLs via env vars to improve portability (paths, local scraper endpoints, log locations).

## P3 Low

- Add a simple run ID to logs and rotate `/home/flog99/process-log.log`.
- Add health checks for local scraper services and Gotify before attempting requests.
- Consider containerizing the pipeline (not just Dropcaster) to reduce host-specific dependencies (pyenv/uv/playwright/cron).
