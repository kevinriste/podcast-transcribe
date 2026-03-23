# TTS Pipeline Optimization: SSML Enrichment & Voice Comparison

## Project Context

This document covers the optimization of a podcast pipeline that converts newsletter emails (Substack, Beehiiv, RSS) into spoken audio using Google Cloud Text-to-Speech. The pipeline is part of a larger self-hosted system running on an Ubuntu server with Docker.

The goal: extract maximum naturalness and meaning preservation from the TTS output by upgrading from plain text input to SSML-enriched input, and evaluating newer voice models.

---

## Current Pipeline Architecture

### Intake (runs every 20 min via cron)

Three sources write plain text files with `META_` header lines followed by a blank line and body:

1. **IMAP** (`parse_email.py`) — Fetches unseen Gmail:
   - **Default** (newsletters): Extracts plain text from Substack/Beehiiv, detects source platform, writes with `META_FROM`, `META_TITLE`, `META_SOURCE_URL`, `META_SOURCE_KIND`, `META_SOURCE_NAME`, `META_INTAKE_TYPE: email`
   - **Subject "youtube"**: Downloads audio via `yt-dlp`, generates Gemini summary, writes ID3 tags, drops MP3 directly into `dropcaster-docker/audio/` (bypasses rest of pipeline)
   - **Subject "link"**: Fetches URL via headless Chromium (Playwright) through local scraper at `localhost:3001`, extracts text with `trafilatura`, writes with `META_INTAKE_TYPE: link`

2. **RSS** (`check-rss.py`) — Polls 3 feeds (Ross Douthat, Ezra Klein via NYT RSS through Wayback Machine; Bill Simmons via Megaphone). GUIDs tracked in flat files.

### Preparation (`prepare_text.py`)

Reads `text-input-raw/*.txt`, applies in order:

1. **Filters** (YAML-configured): Match on metadata fields with `contains`/`not_contains`. Actions: `skip` or `notify` (Gotify). Optional `llm_check` calls Gemini (`gemini-3.1-flash-lite-preview`) for fuzzy matching.
2. **Empty content check**: Filters out payment receipts, subscription notices.
3. **General cleaning** (all on by default, per-source overrides in YAML):
   - URL removal, triple-dash removal, legal bracket unwrap (`[t]he` → `the`), empty bracket/paren/angle removal, whitespace collapse, unsubscribe block removal, "view online" removal, Substack refs removal, standalone `@` line removal, Beehiiv markdown→plaintext, Beehiiv emphasis marker removal, end-of-line punctuation (adds `.` before newlines for TTS pausing)
4. **Text removals** (YAML-configured regexes): Beehiiv image captions, disclaimers, social link blocks.
5. **Text replacements** (YAML-configured regexes): Pronunciation fixes (e.g. `Keynesian` → `Cainzeean`).
6. **Header/footer**: Prepends `{author}.\n{title}.\n` and appends same for TTS attribution.
7. **Character limit check**: Content over 150,000 chars filtered out.

Output: cleaned text to `text-input-cleaned/`, archives raw and cleaned permanently, daily JSON stats.

### Text-to-Speech (`text_to_speech.py`)

Reads `text-input-cleaned/*.txt`:

1. Generates 2-3 sentence summary via Gemini (`gemini-3.1-flash-lite-preview`)
2. Chunks text into 3,000–5,000 character segments (splits at whitespace/punctuation)
3. Calls Google Cloud TTS — voice `en-US-Wavenet-F`, MP3 encoding — one API call per chunk
4. Stitches MP3 chunks with pydub (`AudioSegment.from_mp3` + `reduce(add)`)
5. Writes ID3 tags via mutagen: `TIT2`, `TT3` (summary + source link), `WXXX`
6. Drops final MP3 into `dropcaster-docker/audio/`

### Publishing (`process.sh`)

1. Archives MP3s older than 8 weeks
2. Hashes `audio/` directory listing — skips if unchanged
3. Runs Dropcaster (Ruby, Docker) to generate `index.rss`

---

## Current Monthly Usage & Costs

- **Model**: WaveNet (`en-US-Wavenet-F`)
- **Monthly character volume**: ~1.0–1.2M characters
- **Current cost**: ~$2–5/month (WaveNet is $4/1M chars after 4M free tier, so well within free tier — cost may come from Neural2 or other usage)
- **Audio profile**: Not currently set (should be `headphone-class-device`)
- **SSML usage**: None — plain text only

---

## Optimization Strategies

### Strategy 1: WaveNet + Audio Profile (immediate, free)

Add `effects_profile_id=["headphone-class-device"]` to `AudioConfig`. One-line change. Optimizes the synthesized audio for headphone/earbud playback, which is the primary consumption mode for podcasts.

```python
audio_config = texttospeech.AudioConfig(
    audio_encoding=texttospeech.AudioEncoding.MP3,
    effects_profile_id=["headphone-class-device"],
)
```

**Available audio profiles**: `wearable-class-device`, `handset-class-device`, `headphone-class-device`, `small-bluetooth-speaker-class-device`, `medium-bluetooth-speaker-class-device`, `large-home-entertainment-class-device`, `large-automotive-class-device`, `telephony-class-application`

**Docs**: https://cloud.google.com/text-to-speech/docs/audio-profiles

### Strategy 2: WaveNet + Full SSML Enrichment (free at current volume)

Use Gemini (`gemini-2.0-flash-lite` or `gemini-3.1-flash-lite-preview`) to convert cleaned plain text into SSML before sending to Cloud TTS. At ~1.1M chars/month, even with ~40% SSML overhead (~1.54M chars), this stays well under the 4M free tier.

#### SSML Tags to Use (WaveNet-compatible whitelist)

| Tag | Purpose | Example | Character cost |
|-----|---------|---------|----------------|
| `<speak>` | Root element (required) | `<speak>...</speak>` | 15 chars total |
| `<p>` | Paragraph boundaries | `<p>...</p>` | 7 chars per paragraph |
| `<s>` | Sentence boundaries | `<s>...</s>` | 7 chars per sentence |
| `<break time="Xms"/>` | Pauses between sections | `<break time="800ms"/>` | 22 chars each |
| `<sub alias="X">Y</sub>` | Pronunciation override | `<sub alias="Cainzeean">Keynesian</sub>` | 20+ chars each |
| `<say-as interpret-as="TYPE">` | Number/date/abbreviation reading | `<say-as interpret-as="ordinal">3rd</say-as>` | 40+ chars each |
| `<emphasis level="moderate">` | Word stress | `<emphasis level="moderate">critical</emphasis>` | 40 chars each |

#### `say-as` interpret-as values supported by Google Cloud TTS

- `cardinal` — reads number as cardinal ("10" → "ten")
- `ordinal` — reads number as ordinal ("1st" → "first")
- `characters` — spells out letter by letter ("SSML" → "S S M L")
- `date` — reads as date (use `format` attribute: `mdy`, `dmy`, `ymd`, `md`, `dm`, `ym`, `my`, `y`, `m`, `d`)
- `time` — reads as time ("2:30pm" → "two thirty PM")
- `unit` — reads as unit measurement
- `fraction` — reads as fraction ("3/4" → "three quarters")

#### SSML nesting rules (Google Cloud TTS specific)

- `<s>` goes inside `<p>`
- `<sub>`, `<say-as>`, `<emphasis>`, `<break/>` go inside `<s>`
- Do NOT nest `<say-as>` inside `<emphasis>` or vice versa
- Do NOT nest `<sub>` inside `<say-as>` or vice versa
- Keep nesting flat — deeply nested SSML causes unpredictable behavior on WaveNet

#### XML escaping requirements

- `&` → `&amp;` (CRITICAL — newsletters are full of ampersands)
- `<` → `&lt;` (in text content only, not tags)
- `>` → `&gt;`
- `"` → `&quot;` (inside attribute values)

#### WaveNet SSML limitations / gotchas

- `<prosody>` supports `rate`, `pitch`, `volume` but `contour` is inconsistent
- `<emphasis>` support is more limited than Azure/Polly — `level="moderate"` is safest
- `<phoneme>` works with IPA but is very character-expensive vs `<sub>` for similar results
- Studio voices have different SSML support than WaveNet — don't mix docs
- Malformed SSML causes the entire request to fail — always validate with `xml.etree.ElementTree.fromstring()`

**Docs**: https://cloud.google.com/text-to-speech/docs/ssml

### Strategy 3: Chirp 3 HD (plain or SSML)

Newer model with more natural voice quality. $30/1M chars after 1M free.

- At 1.1M chars plain: ~$3/month
- At 1.54M chars with SSML: ~$16/month

Voice names follow format like `en-US-Chirp3-HD-Achernar`. Check available voices in GCP console or via `client.list_voices()`.

Chirp 3 HD supports a subset of SSML: `<phoneme>`, `<p>`, `<s>`, `<sub>`, `<say-as>`. Notably does NOT support `<emphasis>` or `<prosody>` yet.

**Docs**: https://cloud.google.com/text-to-speech/docs/chirp3-hd

### Strategy 4: Gemini 2.5 Flash TTS

LLM-native TTS — no SSML needed. Instead, you provide a natural-language style prompt describing how the voice should sound, and the model handles pacing, emphasis, and tone contextually.

**Pricing**: Input $0.50/1M tokens, Output $10.00/1M audio tokens (25 tokens/second of audio). At ~1.1M chars this works out to roughly **$25–30/month** — significantly more expensive.

**Key constraints**:
- Text field and prompt field each max 4,000 bytes (8,000 bytes combined)
- Output audio max ~655 seconds per request
- Context window limit of 32k tokens
- Returns raw PCM 16-bit 24kHz audio (no WAV headers) — needs wrapping
- No free tier

**Available voices** (30 total): Acherner, Aoede, Charon, Fenrir, Kore, Leda, Orus, Puck, Schedar, Zephyr, and more. Preview at https://ai.google.dev/gemini-api/docs/speech-generation

**Style prompt example for podcast narration**:
```
Narrate in a calm, engaging podcast host voice.
Read naturally as if telling someone about an interesting article.
Pause briefly between major sections.
Vary pacing slightly — slow down for important points,
maintain a conversational rhythm throughout.
```

The December 2025 model updates (Gemini 2.5 Flash TTS and Pro TTS) significantly improved:
- Style prompt adherence (actually follows tone instructions now)
- Context-aware pacing (slows for emphasis, speeds for excitement)
- Character voice consistency in multi-speaker mode

**Docs**:
- https://cloud.google.com/text-to-speech/docs/gemini-tts
- https://ai.google.dev/gemini-api/docs/speech-generation
- https://blog.google/innovation-and-ai/technology/developers-tools/gemini-2-5-text-to-speech/

---

## SSML Enrichment Implementation

### Architecture

New step inserted between `prepare_text.py` (cleaning) and `text_to_speech.py` (synthesis):

```
text-input-cleaned/*.txt
        │
        ▼
  SSML Enrichment (Gemini Flash Lite)
        │
        ▼
  text-input-ssml/*.xml (or *.ssml)
        │
        ▼
  text_to_speech.py (modified to accept SSML input)
```

### Gemini System Prompt for SSML Generation

```
You are an SSML preprocessor for Google Cloud Text-to-Speech (WaveNet voices).
Your job is to convert plain article text into well-formed SSML that improves
the naturalness and clarity of the spoken output.

## Rules

1. Wrap the entire output in a single <speak> root element.
2. Wrap each paragraph in <p> tags.
3. Wrap each sentence within a paragraph in <s> tags.
4. Use ONLY these SSML tags (no others):
   - <speak> (root)
   - <p> (paragraph)
   - <s> (sentence)
   - <break time="Xms"/> (pauses — 600-1000ms for major section breaks,
     300-400ms for minor transitions)
   - <sub alias="spoken form">written form</sub> (pronunciation overrides)
   - <say-as interpret-as="TYPE">text</say-as> where TYPE is one of:
     cardinal, ordinal, characters, date, time, unit, fraction
   - <emphasis level="moderate">text</emphasis> (use sparingly)

5. XML escaping is critical:
   - & must be &amp;
   - < must be &lt; (in text content, not tags)
   - > must be &gt; (in text content, not tags)
   - " in text must be &quot;

6. Keep nesting FLAT:
   - Do NOT put <say-as> inside <emphasis> or vice versa
   - <sub>, <say-as>, <emphasis>, and <break/> go directly inside <s> tags
   - <s> tags go inside <p> tags
   - <p> tags go inside <speak>

7. For the author/title header lines at the start and end, add a
   <break time="800ms"/> after them.

8. Do NOT add any text that wasn't in the original. Do NOT summarize or
   rephrase. Output must contain ALL original text, wrapped in SSML tags.

9. Common substitutions:
   - Standalone abbreviations/acronyms: <say-as interpret-as="characters">
   - Dollar amounts: keep as-is (WaveNet handles "$X" well)
   - Percentages: keep as-is (WaveNet handles "X%" well)
   - Years in date context: keep as-is
   - Ordinals like "1st", "2nd": <say-as interpret-as="ordinal">
   - Large numbers: <say-as interpret-as="cardinal"> only if ambiguous

10. Output ONLY the SSML. No markdown fences, no explanation, no preamble.
```

### Validation

Always validate SSML output before sending to Cloud TTS:

```python
import xml.etree.ElementTree as ET

def validate_ssml(ssml: str) -> bool:
    try:
        ET.fromstring(ssml)
        return True
    except ET.ParseError:
        return False
```

Fallback on validation failure: wrap the plain text in minimal `<speak><p>...</p></speak>` with XML escaping.

### Chunking for SSML

When chunking SSML for Cloud TTS (max ~5000 bytes per request), split on `</p>` boundaries rather than arbitrary character positions. Each chunk must be re-wrapped in `<speak>...</speak>`. This ensures:
- Each chunk is a complete paragraph (proper sentence-final intonation)
- No unclosed tags
- No mid-sentence chunk boundaries creating audible seams

### Replacing existing pronunciation hacks

The current `prepare_text.py` text replacements for pronunciation (e.g. `Keynesian` → `Cainzeean`) should be migrated to `<sub>` tags in the SSML enrichment step. This keeps the cleaned text human-readable while still controlling pronunciation. The YAML-configured regex replacements can be converted to a pronunciation dictionary that the SSML enrichment prompt references, or handled as a post-processing step that inserts `<sub>` tags for known words.

### Preserving HTML semantics

Currently, HTML formatting is stripped early in `prepare_text.py`. For better SSML enrichment, consider extracting semantic signals from the source HTML before flattening:
- **Bold/italic text** → candidates for `<emphasis>`
- **`<h2>`/`<h3>` tags** → longer `<break>` pauses before/after
- **Block quotes** → could use `<prosody rate="95%">` for slightly slower reading
- **Lists** → enumeration pauses between items

This would require changes to the intake step to pass through a semantic annotation layer rather than stripping to plain text immediately.

---

## Pricing Summary (at ~1.1M chars/month)

| Strategy | Monthly chars | Monthly cost |
|----------|-------------|-------------|
| WaveNet plain text (current) | 1.1M | **~$0** (under 4M free) |
| WaveNet + audio profile only | 1.1M | **~$0** |
| WaveNet + full SSML | ~1.5M | **~$0** (under 4M free) |
| Chirp 3 HD plain text | 1.1M | **~$3** |
| Chirp 3 HD + SSML | ~1.5M | **~$15** |
| Gemini 2.5 Flash TTS | token-based | **~$25–30** |
| Gemini 2.5 Pro TTS | token-based | **~$55** |

SSML enrichment via Gemini adds negligible cost (fractions of a cent per article at Flash Lite pricing).

---

## Comparison Script

A comparison script is provided at `tts_comparison.py`. It takes a cleaned article and generates audio through all 5 strategies for A/B listening tests.

### Usage

```bash
# Install dependencies
pip install google-cloud-texttospeech google-genai

# Set environment variables
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_API_KEY=your-gemini-api-key
# Also ensure GOOGLE_APPLICATION_CREDENTIALS is set for Cloud TTS

# Quick test (first 2000 chars)
python tts_comparison.py text-input-cleaned/some_article.txt --max-chars 2000

# Specific strategies only
python tts_comparison.py text-input-cleaned/some_article.txt \
  --strategies wavenet-plain wavenet-ssml chirp3-plain

# All strategies, full article
python tts_comparison.py text-input-cleaned/some_article.txt
```

### Output

```
comparison_output/
├── 01_wavenet_plain.mp3      # Current baseline
├── 02_wavenet_ssml.mp3       # WaveNet + SSML enrichment
├── 03_chirp3_plain.mp3       # Chirp 3 HD voice
├── 04_chirp3_ssml.mp3        # Chirp 3 HD + SSML enrichment
├── 05_gemini_tts.wav         # Gemini Flash TTS (raw PCM wrapped in WAV)
└── enriched.ssml.xml         # The SSML that Gemini generated (for inspection)
```

### Configuration constants to review

- `WAVENET_VOICE` — currently `en-US-Wavenet-F` (matches existing pipeline)
- `CHIRP3_VOICE` — set to `en-US-Chirp3-HD-Achernar` as placeholder; verify available voices via `client.list_voices()` or GCP console
- `GEMINI_TTS_VOICE` — set to `Kore`; alternatives include `Aoede`, `Charon`, `Puck`, `Fenrir`. Preview at https://ai.google.dev/gemini-api/docs/speech-generation
- `SSML_ENRICHMENT_MODEL` — set to `gemini-2.0-flash-lite`; can also use `gemini-3.1-flash-lite-preview` or a more capable model
- `AUDIO_EFFECTS_PROFILE` — set to `headphone-class-device`
- `CHUNK_SIZE` — 4500 chars (Cloud TTS limit is ~5000 bytes per request)
- `GEMINI_STYLE_PROMPT` — natural language narration style instruction for Gemini TTS

---

## Implementation Plan

### Phase 1: Quick wins (no code changes to production pipeline)
1. Add `effects_profile_id=["headphone-class-device"]` to `text_to_speech.py` AudioConfig
2. Run comparison script on a few articles, listen to results

### Phase 2: SSML enrichment
1. Add SSML enrichment step between `prepare_text.py` and `text_to_speech.py`
2. Modify `text_to_speech.py` to detect SSML input and use `SynthesisInput(ssml=...)` instead of `SynthesisInput(text=...)`
3. Add XML validation with plain-text fallback
4. Modify chunking to split on `</p>` boundaries for SSML input
5. Migrate YAML pronunciation replacements from plain-text regex to `<sub>` tags

### Phase 3: Voice model decision
1. Based on listening comparison, decide whether to:
   - Stay on WaveNet + SSML (free)
   - Upgrade to Chirp 3 HD plain (cheap) or Chirp 3 HD + SSML (moderate)
   - Switch to Gemini TTS (expensive but most natural)

### Phase 4: HTML semantic preservation (optional, higher effort)
1. Modify intake to extract bold/italic/heading signals from source HTML
2. Pass semantic annotations through to SSML enrichment
3. Let Gemini use those signals to place `<emphasis>` and `<break>` more accurately

---

## Key Documentation Links

| Resource | URL |
|----------|-----|
| Cloud TTS overview | https://cloud.google.com/text-to-speech/docs/basics |
| SSML reference (Cloud TTS) | https://cloud.google.com/text-to-speech/docs/ssml |
| SSML tutorial | https://cloud.google.com/text-to-speech/docs/ssml-tutorial |
| Audio profiles | https://cloud.google.com/text-to-speech/docs/audio-profiles |
| Chirp 3 HD | https://cloud.google.com/text-to-speech/docs/chirp3-hd |
| Gemini TTS (Cloud API) | https://cloud.google.com/text-to-speech/docs/gemini-tts |
| Gemini TTS (AI Studio / Gemini API) | https://ai.google.dev/gemini-api/docs/speech-generation |
| Gemini TTS improvements blog post | https://blog.google/innovation-and-ai/technology/developers-tools/gemini-2-5-text-to-speech/ |
| Cloud TTS pricing | https://cloud.google.com/text-to-speech/pricing |
| Cloud TTS release notes | https://cloud.google.com/text-to-speech/docs/release-notes |
| AudioConfig reference | https://cloud.google.com/text-to-speech/docs/reference/rest/v1/AudioConfig |
| W3C SSML spec | https://www.w3.org/TR/speech-synthesis11/ |
| Python client library | https://cloud.google.com/python/docs/reference/texttospeech/latest |
| Gemini Python SDK (`google-genai`) | https://ai.google.dev/gemini-api/docs/quickstart |

---

## Dependencies

```
google-cloud-texttospeech  # Cloud TTS API client
google-genai               # Gemini API client (for SSML enrichment + Gemini TTS)
pydub                      # Audio stitching (already in pipeline)
mutagen                    # ID3 tagging (already in pipeline)
```

