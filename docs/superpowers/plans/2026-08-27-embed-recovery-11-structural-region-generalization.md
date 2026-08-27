# Generalize Structural Extraction to RSS + Beehiiv (Plan 11) — RECORDED, NOT YET APPROVED TO BUILD

> Status: **parked**. Recorded at the maintainer's request as the agreed direction (#3).
> Do NOT implement until explicitly greenlit. Depends on Plan 10 (intake-aware cleaning)
> shipping first, so newly-structured sources inherit the `structured` cleaning profile.

## Why

Today only Substack email flows through the structural extractor (`extract_blocks` →
`serialize_flat`), because `find_content_region` recognizes only Substack's
`div.body.markup` (else `<article>`, else whole document). Every other HTML source is
either plain-text (`msg.text` for Beehiiv + non-HTML custom sources) or ad-hoc
BeautifulSoup (RSS `content` mode). That means:

- **Beehiiv** (e.g. garbageday) ships real HTML but we read its lossy plain-text part,
  purely because region detection isn't Beehiiv-aware.
- **RSS `content` mode** hand-rolls BeautifulSoup on `entry.content`, so it gets none of
  the tweet/image/quote/footnote/card recovery, aside voicing, or vision descriptions.

Structure-aware extraction is strictly more robust than regex-on-flattened-text
(boilerplate is excluded by DOM position, not guessed). Unifying sources onto the
extractor shrinks the legacy cleaning surface toward whitespace-only for everyone.

## Scope

1. **Generalize `find_content_region`** beyond Substack:
   - Add Beehiiv content-container detection (inspect real Beehiiv/garbageday DOM — likely
     a table-based content cell or a known wrapper class; verify against `email-corpus`).
   - Keep the Substack → Beehiiv → `<article>` → whole-doc precedence; add per-platform
     structural probes rather than source-name coupling (stay generic/portable).
   - Regression-test each platform's region on real corpus files (block counts, no
     nav/footer leakage).

2. **Route Beehiiv email through `extract_body_from_html`** in imap (drop the `msg.text`
   path for Beehiiv once its region is reliable). Beehiiv emphasis/plaintext cleaning
   steps become dead for Beehiiv — leave them for any residual plain-text fallback.

3. **Migrate RSS `content` mode** to `extract_blocks`/`serialize_flat`:
   - `entry.content` HTML → `find_content_region` → `extract_blocks` → `serialize_flat`.
   - `description` and `full_scraper` modes: evaluate separately (full_scraper already
     fetches article HTML — a strong candidate for the same treatment).
   - Stamp `META_EXTRACTION: structured` so RSS inherits the structured cleaning profile.

4. **Consider archive intake** similarly if its `posts.json` carries HTML.

## Explicitly out of scope
- Vision on RSS/Beehiiv images is automatic once they extract structurally (same
  `enrich_images` path) — no extra work, but watch OpenAI cost/volume and the
  `EMBED_VISION`/`EMBED_DROP_TYPES` toggles apply.
- Non-HTML custom sources without a recognizable region stay plain-text.

## Verification approach
Same as Plans 7–10: build bodies from `email-corpus` fixtures, diff block counts and
serialized output, then render one real episode per newly-migrated platform and confirm
no boilerplate leakage and correct aside voicing.

## Open questions for the maintainer
- Beehiiv region: is there a stable structural anchor across Beehiiv senders, or does it
  vary per publication (needing a small per-platform probe list)?
- RSS `description`-mode feeds (podcasts): leave as-is (summaries), or also structure?
