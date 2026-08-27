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
   - **Beehiiv container is `id="content-blocks"`** — ANALYZED (garbageday corpus, 100/100
     files). It's Beehiiv's analog to Substack's `div.body.markup`; masthead + footer
     (unsubscribe/©/address) sit OUTSIDE it (verified), so scoping to it excludes all
     chrome structurally. Scoping cut a sample from 68 blocks (with masthead+footer+junk
     images) to 53 clean blocks; junk images 17→9. Selector: `soup.select_one("#content-blocks")`.
   - Precedence: Substack (`div.body.markup`) → Beehiiv (`#content-blocks`) → `<article>`
     → whole-doc. Structural probes only, no source-name coupling (stay generic/portable).
   - Block handlers need NO Beehiiv additions: garbageday uses h2/p/img/inline-a (all
     handled); embeds are bare links (kept as text); no footnotes/cards. Only possible
     tweak: add any Beehiiv decorative-image class to `_is_decorative` (spot-check the ~9
     in-body images for section dividers).
   - VALIDATION GAP: garbageday is the only Beehiiv publisher in the corpus. `content-blocks`
     is a Beehiiv *platform* id (should generalize) but confirm against a 2nd Beehiiv sender.
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
- Beehiiv region: `#content-blocks` confirmed on garbageday (100/100). Stable across other
  Beehiiv senders? (platform id, expected yes — confirm when a 2nd sender appears.)
- RSS `description`-mode feeds (podcasts): leave as-is (summaries), or also structure?
