# Archive Intake

Scheduled intake script that walks a blog/archive one post per day.

## How it works

1.  `posts.json` (gitignored) contains a chronological list of the source's posts, each `{"title": ..., "url": ...}`.
2.  `source.yaml` (gitignored; copy from `source.example.yaml`) sets the source display name written to `META_FROM`.
3.  `check-archive.py` runs once per day (triggered by `process.sh`).
4.  It tracks progress in `state.json` (`last_processed_index` and `last_processed_date`).
5.  Each day it fetches the next post in the list, extracts the content, and writes it to `../prepare-text/text-input-raw/` with standard metadata headers.
6.  When a post has enough comments, it also writes a multi-voice "Highlights From The Comments" episode (see `comment_briefing.py`).

## Setup

```bash
cp source.example.yaml source.yaml   # then edit source_name
uv sync
uv run python3 check-archive.py
```
