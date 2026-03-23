# TTS Quality Test Plan

## Cost Analysis

At ~1.1M chars/month:

| Strategy | Chars after SSML | Monthly cost |
|---|---|---|
| WaveNet plain (today) | 1.1M | **Free** (4M free tier) |
| WaveNet + full SSML | ~1.5M | **Free** (still under 4M) |
| Chirp 3 HD plain | 1.1M | **~$3** (1M free) |
| Chirp 3 HD + full SSML | ~1.5M | **~$15** |
| Gemini Flash TTS | token-based | **~$27** |
| Gemini Pro TTS | token-based | **~$50+** (estimate) |

**Key insight**: WaveNet + full SSML enrichment is completely free at this volume. No reason to hold back on SSML tags — go wild with every tag.

## Stage 1: Input Extraction

How content enters the pipeline from email HTML:

| ID | Approach | Input | Output format |
|----|----------|-------|---------------|
| **E-A** | Plain text | `msg.text` | Flat text (current) |
| **E-B** | Trafilatura | `msg.html` | Markdown (headings, bold, italic, lists, blockquotes) |
| **E-C** | BeautifulSoup selective | `msg.html` | Custom markers (`[H2]`, `[QUOTE]`, `**bold**`) |
| **E-D** | Cleaned HTML | `msg.html` | Stripped HTML (semantic tags preserved) |

### Extraction comparison results (Arnold Kling "Reactions to AI")

| Approach | Chars | Headings | Bold | Italic | Blockquotes |
|----------|------:|------:|-----:|-------:|------------:|
| A: Plain text | 5,220 | 0 | 0 | 0 | 0 |
| B: Trafilatura | 4,151 | 0 | 0 | 2 | 0 |
| C: BeautifulSoup | 3,905 | 2 | 0 | 0 | 5 |
| D: Cleaned HTML | 15,890 | 2 | 0 | 2 | 5 |

- **A** preserves zero semantic signals
- **B** found italic but missed headings/blockquotes (boilerplate filter too aggressive for email HTML)
- **C** found headings and blockquotes but missed italic (emphasis only extracted from `<p>` children)
- **D** captured everything but at 3x character count (HTML markup overhead)

## Stage 2: SSML Enrichment

| ID | Approach | Notes |
|----|----------|-------|
| **S-0** | None | No SSML — plain text to TTS (current) |
| **S-1** | LLM (`gemini-3.1-flash-lite-preview`) | Smart, nondeterministic, handles flat text and structured input |
| **S-2** | Deterministic mapper | Mechanical HTML/markers → SSML. Fast, free, predictable, always valid XML |

### S-1 vs S-2 tradeoffs

| | S-1 (LLM) | S-2 (Deterministic) |
|---|---|---|
| Cost | ~$0.01/article | Free |
| Latency | 1-3s per chunk | Instant |
| Predictability | Varies per run | Identical every time |
| XML validity | Can fail (needs fallback) | Guaranteed |
| Flat text input (E-A) | Works — guesses structure | Useless — nothing to map |
| Structured input (E-C/E-D) | Works — reads markers | Works — maps markers mechanically |
| Nuance | Can add `<emphasis>` contextually | Only maps explicit structural signals |
| Pronunciation | Can add `<sub>` for tricky words | Needs a separate dictionary |

### S-2 mapping rules

From E-C (BeautifulSoup markers):
```
[H1-6] text          →  <break time="800ms"/><emphasis>text</emphasis><break time="400ms"/>
[QUOTE] text          →  <break time="300ms"/><prosody rate="95%">text</prosody><break time="300ms"/>
**bold text**         →  <emphasis level="moderate">bold text</emphasis>
*italic text*         →  <emphasis level="reduced">italic text</emphasis>
paragraph break       →  </s></p><p><s>
  - list item         →  <break time="200ms"/>list item
  N. list item        →  <break time="200ms"/>list item
```

From E-D (cleaned HTML):
```
<h1-6>text</h*>       →  <break time="800ms"/><emphasis>text</emphasis><break time="400ms"/>
<blockquote>text</bq>  →  <break time="300ms"/><prosody rate="95%">text</prosody><break time="300ms"/>
<strong>text</strong>  →  <emphasis level="moderate">text</emphasis>
<em>text</em>          →  <emphasis level="reduced">text</emphasis>
<p>text</p>            →  <p><s>text</s></p>
<li>text</li>          →  <break time="200ms"/><s>text</s>
```

## Stage 3: TTS Engine

| ID | Engine | Input format | Notes |
|----|--------|-------------|-------|
| **T-1** | Google Cloud TTS `en-US-Wavenet-F` | Plain text or SSML | Current production |
| **T-2** | Google Cloud TTS `en-US-Wavenet-F` | SSML | Same engine, SSML-enriched input |
| **T-3** | Gemini Flash TTS `gemini-2.5-flash-preview-tts` | Text + style prompt | Fast, cheaper |
| **T-4** | Google Cloud TTS Chirp 3 HD | Text or SSML | Studio-quality, multi-speaker capable |
| **T-5** | Gemini Pro TTS `gemini-2.5-pro-preview-tts` | Text + style prompt | Higher quality, slower |

Available Chirp 3 HD voices: 33 voices (star/moon names). Default: `en-US-Chirp3-HD-Achernar`.
Gemini TTS voices (shared names across Flash/Pro): Aoede, Charon, Fenrir, Kore, Leda, Orus, Puck, Zephyr.
CLI flags `--chirp-voice` and `--gemini-voice` to swap without editing code.

## Interactions and Dependencies

```
Stage 1 (extraction)  →  Stage 2 (SSML)  →  Stage 3 (TTS)
─────────────────────────────────────────────────────────────

E-A (plain text)  ──→  S-0 (none)    ──→  T-1 (Wavenet)        ← CURRENT PRODUCTION
                  ──→  S-1 (LLM)     ──→  T-2 (Wavenet+SSML)   ← FREE, smart SSML
                  ──→  S-0 (none)    ──→  T-3 (Gemini Flash)
                  ──→  S-0 (none)    ──→  T-4 (Chirp HD)
                  ──→  S-1 (LLM)     ──→  T-4 (Chirp HD+SSML)
                  ──→  S-0 (none)    ──→  T-5 (Gemini Pro)
                  ╌╌╌  S-2 CANNOT WORK WITH E-A (no structure to map)

E-B (trafilatura) ──→  S-1 (LLM)     ──→  T-2 (Wavenet+SSML)
                  ──→  S-0 (none)    ──→  T-3 (Gemini Flash)
                  ──→  S-0 (none)    ──→  T-4 (Chirp HD)
                  ──→  S-0 (none)    ──→  T-5 (Gemini Pro)

E-C (bs4 markers) ──→  S-1 (LLM)     ──→  T-2 (Wavenet+SSML)
                  ──→  S-2 (determ)  ──→  T-2 (Wavenet+SSML)   ← FREE end-to-end
                  ──→  S-0 (none)    ──→  T-3 (Gemini Flash)
                  ──→  S-0 (none)    ──→  T-5 (Gemini Pro)

E-D (clean HTML)  ──→  S-1 (LLM)     ──→  T-2 (Wavenet+SSML)
                  ──→  S-2 (determ)  ──→  T-2 (Wavenet+SSML)   ← FREE end-to-end
                  ──→  S-0 (none)    ──→  T-3 (Gemini Flash)
                  ──→  S-0 (none)    ──→  T-5 (Gemini Pro)
```

## Test Rounds

### Round 1 — Voice/engine comparison (flat text input, E-A only)

Hold input constant (current plain text), vary the TTS:

| # | Pipeline | What it tests |
|---|----------|---------------|
| 1 | `E-A → S-0 → T-1` | Current production (control) |
| 2 | `E-A → S-1 → T-2` | Does LLM SSML enrichment help Wavenet? (free) |
| 3 | `E-A → S-0 → T-3` | Gemini Flash TTS on flat text |
| 4 | `E-A → S-0 → T-4` | Chirp 3 HD on flat text |
| 5 | `E-A → S-1 → T-4` | Chirp 3 HD with LLM SSML |
| 6 | `E-A → S-0 → T-5` | Gemini Pro TTS on flat text |

### Round 2 — Input quality + deterministic SSML (best engine from Round 1)

| # | Pipeline | What it tests |
|---|----------|---------------|
| 7 | `E-C → S-2 → T-2` | BS4 markers + deterministic SSML + Wavenet (fully free) |
| 8 | `E-D → S-2 → T-2` | Clean HTML + deterministic SSML + Wavenet (fully free) |
| 9 | `E-C → S-1 → T-2` | BS4 markers + LLM SSML + Wavenet (S-1 vs S-2 comparison) |
| 10 | `E-D → S-1 → T-2` | Clean HTML + LLM SSML + Wavenet (S-1 vs S-2 comparison) |

### Round 3 — Interaction effects (if needed)

| # | Pipeline | What it tests |
|---|----------|---------------|
| 11 | `E-D → S-0 → T-3` | Gemini Flash reading HTML directly (no SSML middleman) |
| 12 | `E-B → S-0 → T-3` | Gemini Flash reading markdown (native LLM format) |
| 13 | `E-D → S-0 → T-5` | Gemini Pro reading HTML directly |
| 14 | `E-C → S-2 → T-4` | BS4 + deterministic SSML + Chirp HD (best structure, best voice?) |

## Execution

- All tests on the **same article** (Arnold Kling "Reactions to AI" — has headings, blockquotes, italic)
- Output MP3s dropped into `dropcaster-docker/audio/` with `[TEST-R1-1]` etc. title prefixes
- ID3 tags set so they show up in podcast app for listening comparison
- Dropcaster regenerates feed on next cron run
- `--max-chars 2000` for quick iteration, full article for final comparison

## Scripts

- `comparison.py` — TTS comparison (all strategies, dropcaster output)
- `ssml_mapper.py` — S-2 deterministic SSML mapper (markers or HTML → SSML)
- `imap/compare_html_extraction.py` — Stage 1 extraction comparison
- Output: `dropcaster-docker/audio/` (test MP3s), `comparison_output/` (SSML), `html-comparison/` (extraction text)
