# Recommendations

Priority levels:
- P0 Critical: risk of data loss or cross-service impact.
- P1 High: likely failure or significant reliability/perf issue.
- P2 Medium: quality, maintainability, or cost optimizations.
- P3 Low: nice-to-have improvements.

## P0 Critical

- Remove or tightly scope `docker container prune -f` and `docker volume prune -f` from the cron path; they can delete unrelated stopped containers and unused volumes on the host.

## P1 High

- Fix the AWS Dropcaster invocation path in `process-aws.sh` (currently runs `docker-compose.aws.yml` from `dropcaster-docker/audio-aws`, which will fail).
- Guard `rss/check-rss.py` against missing `diagnosis` dir and `html_content=None` before calling `bare_extraction` to avoid feed-wide crashes.
- Add explicit timeouts and retry policy for `requests` calls (Gotify, Wayback, scraper) so cron runs do not hang indefinitely.
- Use `uv sync --frozen` (or equivalent) in cron to avoid pulling new dependency versions every 20 minutes.

## P2 Medium

- Move `playwright install` out of the cron loop (provision once, or only if browsers are missing).
- Align on a newer Python version (3.11/3.12) and update `pyproject.toml` and Pipfiles to match; keep `pyenv` and `uv` consistent.
- Ensure `text-to-speech/text-input-empty-files` exists before moving empty files to avoid hard failures.
- Standardize metadata handling and ID3 tags for OpenAI/AWS TTS so RSS titles/descriptions are consistent across feeds.
- Clean or deduplicate `text-input` copies when mirroring into `text-to-speech-polly` and `text-to-speech-openai`.

## P3 Low

- Add a simple run ID to logs and rotate `/home/flog99/process-log.log`.
- Add health checks for local scraper services and Gotify before attempting requests.
- Use `docker compose run --rm` (or scheduled cleanup) for the OpenAI/AWS Dropcaster runs to avoid buildup of stopped containers.
