# Punchlist & Future Architecture Items

This document tracks durable, deferred architecture improvements and ideas for the podcast transcription pipeline.

## Text-to-Speech Pause Mechanism (SSML `<break>` tags)
- **Status:** Proposed / Deferred
- **Current Behavior:** `prepare_text.py` appends periods (`.`) to the end of unpunctuated lines via `apply_end_of_line_punctuation` to induce natural phrase pauses in Cloud WaveNet and Gemini TTS engines.
- **Limitation:** Appending periods modifies the literal text content, risking collisions with regex removal/replacement patterns (e.g. `^Advertisement$`), and can sound like a hard sentence drop rather than a paragraph pause.
- **Proposed Architecture:**
  - Transition from artificial period injection to structured pause tags or SSML breaks (e.g. `<break time="750ms"/>` for paragraph transitions).
  - For WaveNet, pass SSML directly.
  - For Gemini TTS, evaluate prompt/tag support for pauses between chunked segments or structural section dividers.
