"""Text preparation pipeline: filtering, cleaning, and transformation.

Reads raw text files from text-input-raw/, applies filters and cleaning rules
from filters.yaml, and writes cleaned output to text-input-cleaned/ for TTS.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, TypedDict
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Mapping

import markdown
import yaml
from bs4 import BeautifulSoup
from podcast_shared import (
    ASIDE_MARKER,
    enable_post_in_podly,
    generate_text,
    send_gotify_notification,
    split_metadata,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

RAW_INPUT_DIR = "text-input-raw"
RAW_ARCHIVE_DIR = "text-input-raw-archive"
CLEANED_OUTPUT_DIR = "text-input-cleaned"
CLEANED_ARCHIVE_DIR = "text-input-cleaned-archive"
FILTERED_DIR = "text-input-filtered"
STATS_DIR = "stats"
CONFIG_FILE = "filters.yaml"
CHARACTER_LIMIT = 150000
STATS_RETENTION_DAYS = 365
# gpt-5.6-luna sits in OpenAI's free 10M/day group on the share key.
LLM_MODEL = os.environ.get("PREPARE_TEXT_LLM_MODEL", "gpt-5.6-luna")


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------

VALID_MATCH_FIELDS = frozenset(
    {"from", "title", "source_url", "source_kind", "source_name", "intake_type", "guid"},
)
VALID_MATCH_OPERATORS = frozenset({"contains", "not_contains"})
VALID_ACTIONS = frozenset({"skip", "notify", "podly_process"})
TERMINAL_ACTIONS = frozenset({"skip", "podly_process"})
VALID_FLAGS = frozenset({"ignorecase", "multiline", "dotall"})
CLEANING_STEPS = (
    "beehiiv_plaintext_conversion",
    "beehiiv_emphasis_removal",
    "unwrap_hard_wraps",
    "footnote_relocation",
    "roman_numeral_normalization",
    "url_removal",
    "legal_bracket_unwrap",
    "triple_dash_removal",
    "empty_bracket_removal",
    "whitespace_collapse",
    "unsubscribe_removal",
    "view_online_removal",
    "substack_refs_removal",
    "substack_boilerplate_removal",
    "standalone_at_removal",
    "end_of_line_punctuation",
)
VALID_CLEANING_KEYS = frozenset(CLEANING_STEPS)


# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------


class NotifyConfig(TypedDict):
    """Gotify notification configuration for a filter rule."""

    priority: int
    title: str


class _FilterRuleOptional(TypedDict, total=False):
    action: str  # "skip" | "notify" | "podly_process"
    llm_check: str
    notify: NotifyConfig


class FilterRule(_FilterRuleOptional):
    """A single filter rule from filters.yaml."""

    match: dict[str, dict[str, str]]  # field -> {operator: value}
    reason: str


class _TextRemovalOptional(TypedDict, total=False):
    flags: str | list[str]


class TextRemoval(_TextRemovalOptional):
    """A regex removal rule from filters.yaml."""

    pattern: str
    reason: str


class _TextReplacementOptional(TypedDict, total=False):
    flags: str | list[str]


class TextReplacement(_TextReplacementOptional):
    """A regex replacement rule from filters.yaml."""

    pattern: str
    replacement: str
    reason: str


# match: dict[str, dict[str, str]], plus dynamic cleaning step keys → bool
CleaningOverride = dict[str, object]


class GeneralCleaningConfig(TypedDict, total=False):
    """The general_cleaning section of filters.yaml."""

    beehiiv_plaintext_conversion: bool
    beehiiv_emphasis_removal: bool
    unwrap_hard_wraps: bool
    footnote_relocation: bool
    roman_numeral_normalization: bool
    url_removal: bool
    legal_bracket_unwrap: bool
    triple_dash_removal: bool
    empty_bracket_removal: bool
    whitespace_collapse: bool
    unsubscribe_removal: bool
    view_online_removal: bool
    substack_refs_removal: bool
    substack_boilerplate_removal: bool
    standalone_at_removal: bool
    end_of_line_punctuation: bool
    overrides: list[CleaningOverride]


class PipelineConfig(TypedDict, total=False):
    """Root config structure from filters.yaml."""

    filters: list[FilterRule]
    general_cleaning: GeneralCleaningConfig
    text_removals: list[TextRemoval]
    text_replacements: list[TextReplacement]


class FileStats(TypedDict):
    """Per-file processing statistics."""

    file: str
    raw_archive: str | None
    cleaned_archive: str | None
    filtered_archive: str | None
    filters_checked: list[str]
    filters_matched: list[str]
    text_removals: dict[str, dict[str, int]]
    text_replacements: dict[str, dict[str, int]]
    general_cleaning: dict[str, dict[str, int | bool]]
    outcome: Literal["filtered", "filtered_empty", "filtered_too_big", "cleaned"] | None
    chars_before: int
    chars_after: int | None


def parse_flags(flags_raw: str | list[str] | None) -> int:
    """Convert YAML flag names to a combined re flags integer.

    Returns:
        Combined regex flags.

    Raises:
        ValueError: If an invalid flag name is provided.

    """
    if flags_raw is None:
        return 0
    flag_list = [flags_raw] if isinstance(flags_raw, str) else flags_raw
    result: int = 0
    for flag_name in flag_list:
        if flag_name not in VALID_FLAGS:
            msg = f"Invalid flag: {flag_name!r} (valid: {', '.join(sorted(VALID_FLAGS))})"
            raise ValueError(msg)
        if flag_name == "ignorecase":
            result |= re.IGNORECASE
        elif flag_name == "multiline":
            result |= re.MULTILINE
        elif flag_name == "dotall":
            result |= re.DOTALL
    return result


def validate_match_block(match_block: Mapping[str, Mapping[str, str]], context: str) -> None:
    """Validate a filter's match block has valid fields and operators.

    Raises:
        ValueError: If the match block contains invalid fields or operators.

    """
    if not isinstance(match_block, dict) or not match_block:
        msg = f"{context}: 'match' must be a non-empty dict"
        raise ValueError(msg)
    for field, operators in match_block.items():
        if field not in VALID_MATCH_FIELDS:
            msg = f"{context}: unknown match field {field!r} (valid: {', '.join(sorted(VALID_MATCH_FIELDS))})"
            raise ValueError(msg)
        if not isinstance(operators, dict) or not operators:
            msg = f"{context}: match field {field!r} must be a dict with operators"
            raise ValueError(msg)
        for op in operators:
            if op not in VALID_MATCH_OPERATORS:
                valid_ops = ", ".join(sorted(VALID_MATCH_OPERATORS))
                msg = f"{context}: unknown operator {op!r} for field {field!r} (valid: {valid_ops})"
                raise ValueError(msg)


def validate_config(config: PipelineConfig) -> None:
    """Validate the full filters.yaml configuration structure.

    Raises:
        ValueError: If the config contains invalid keys, filters, or patterns.
        TypeError: If a cleaning override match block is not a dict.

    """
    valid_top_keys = frozenset(
        {"filters", "general_cleaning", "text_removals", "text_replacements"},
    )
    for key in config:
        if key not in valid_top_keys:
            msg = f"Unknown top-level key: {key!r}"
            raise ValueError(msg)

    # Validate filters
    for idx, filt in enumerate(config.get("filters") or []):
        ctx = f"filters[{idx}]"
        if "match" not in filt:
            msg = f"{ctx}: 'match' is required"
            raise ValueError(msg)
        validate_match_block(filt["match"], ctx)
        if "reason" not in filt:
            msg = f"{ctx}: 'reason' is required"
            raise ValueError(msg)
        action = filt.get("action", "skip")
        if action not in VALID_ACTIONS:
            msg = f"{ctx}: invalid action {action!r} (valid: {', '.join(sorted(VALID_ACTIONS))})"
            raise ValueError(msg)
        if action == "notify" and "notify" not in filt:
            msg = f"{ctx}: action 'notify' requires a 'notify' block"
            raise ValueError(msg)
        if "notify" in filt:
            notify = filt["notify"]
            if "priority" not in notify:
                msg = f"{ctx}: notify block requires 'priority'"
                raise ValueError(msg)
            if "title" not in notify:
                msg = f"{ctx}: notify block requires 'title'"
                raise ValueError(msg)
        valid_filter_keys = {"match", "reason", "action", "llm_check", "notify"}
        for key in filt:
            if key not in valid_filter_keys:
                msg = f"{ctx}: unknown key {key!r}"
                raise ValueError(msg)
        if "llm_check" in filt and not filt["llm_check"]:
            msg = f"{ctx}: 'llm_check' must be a non-empty string"
            raise ValueError(msg)

    # Validate general_cleaning
    gc = config.get("general_cleaning") or GeneralCleaningConfig()
    for key in gc:
        if key not in VALID_CLEANING_KEYS and key != "overrides":
            msg = f"general_cleaning: unknown key {key!r}"
            raise ValueError(msg)
    for oidx, override in enumerate(gc.get("overrides") or []):
        octx = f"general_cleaning.overrides[{oidx}]"
        if "match" not in override:
            msg = f"{octx}: 'match' is required"
            raise ValueError(msg)
        override_match: object = override["match"]
        if not isinstance(override_match, dict):
            msg = f"{octx}: 'match' must be a dict"
            raise TypeError(msg)
        validate_match_block(override_match, octx)  # pyright: ignore[reportUnknownArgumentType] — validated inside
        for key in override:
            if key != "match" and key not in VALID_CLEANING_KEYS:
                msg = f"{octx}: unknown cleaning step {key!r}"
                raise ValueError(msg)

    # Validate text_removals
    for idx, removal in enumerate(config.get("text_removals") or []):
        rctx = f"text_removals[{idx}]"
        if "pattern" not in removal:
            msg = f"{rctx}: 'pattern' is required"
            raise ValueError(msg)
        if "reason" not in removal:
            msg = f"{rctx}: 'reason' is required"
            raise ValueError(msg)
        flags = parse_flags(removal.get("flags"))
        try:
            _ = re.compile(removal["pattern"], flags)
        except re.error as exc:
            msg = f"{rctx}: invalid regex: {exc}"
            raise ValueError(msg) from exc

    # Validate text_replacements
    for idx, rep in enumerate(config.get("text_replacements") or []):
        pctx = f"text_replacements[{idx}]"
        if "pattern" not in rep:
            msg = f"{pctx}: 'pattern' is required"
            raise ValueError(msg)
        if "replacement" not in rep:
            msg = f"{pctx}: 'replacement' is required"
            raise ValueError(msg)
        if "reason" not in rep:
            msg = f"{pctx}: 'reason' is required"
            raise ValueError(msg)
        flags = parse_flags(rep.get("flags"))
        try:
            _ = re.compile(rep["pattern"], flags)
        except re.error as exc:
            msg = f"{pctx}: invalid regex: {exc}"
            raise ValueError(msg) from exc


def validate_rule_ordering(filters: list[FilterRule]) -> list[str]:
    """Check for terminal (skip / podly_process) rules that shadow later rules with overlapping match criteria.

    Returns:
        List of error messages (empty if no problems).

    """
    errors: list[str] = []
    for i, rule_a in enumerate(filters):
        action_a = rule_a.get("action", "skip")
        if action_a not in TERMINAL_ACTIONS:
            continue
        match_a = rule_a["match"]
        for j in range(i + 1, len(filters)):
            rule_b = filters[j]
            match_b = rule_b["match"]
            # Check if match_b is a subset of or identical to match_a
            # (meaning everything match_b matches, match_a also matches)
            if _match_is_subset(subset=match_b, superset=match_a):
                errors.append(
                    f"filters[{i}] ({action_a}, reason: {rule_a['reason']!r}) shadows filters[{j}] (reason: {rule_b['reason']!r}) — the later rule will never fire. Reorder or adjust match criteria.",
                )
    return errors


def _match_is_subset(subset: Mapping[str, Mapping[str, str]], superset: Mapping[str, Mapping[str, str]]) -> bool:
    """Check if everything matched by 'subset' criteria is also matched by 'superset'.

    A superset match has fewer or equal constraints — so subset must contain
    all fields from superset with compatible operators.

    Returns:
        True if subset's match criteria are a subset of superset's.

    """
    for field, operators in superset.items():
        if field not in subset:
            return False
        sub_ops = subset[field]
        for op, value in operators.items():
            if op not in sub_ops:
                return False
            if sub_ops[op].lower() != value.lower():
                return False
    return True


# ---------------------------------------------------------------------------
# Match evaluation
# ---------------------------------------------------------------------------


def evaluate_match(match_block: Mapping[str, Mapping[str, str]], metadata: dict[str, str]) -> bool:
    """Test whether a file's metadata satisfies a filter's match criteria.

    Returns:
        True if all match conditions are satisfied.

    """
    for field, operators in match_block.items():
        meta_value = metadata.get(field, "").lower()
        for op, target in operators.items():
            target_lower = target.lower()
            if op == "contains" and target_lower not in meta_value:
                return False
            if op == "not_contains" and target_lower in meta_value:
                return False
    return True


def evaluate_llm_check(prompt_template: str, metadata: dict[str, str], content: str) -> bool:
    """Run an LLM check (LLM_MODEL) and return whether the content matches.

    Returns:
        True if the LLM confirms the check, False on failure or negative result.

    """
    title = metadata.get("title", "")
    full_prompt = f"{prompt_template}\n\nTitle: {title}\n\nContent:\n{content}"
    try:
        text = generate_text(
            LLM_MODEL,
            full_prompt,
            json_schema={
                "type": "object",
                "properties": {"result": {"type": "boolean"}},
                "required": ["result"],
                "additionalProperties": False,
            },
        )
        if not text:
            logging.warning("%s returned no text for LLM check", LLM_MODEL)
            return False
        parsed: dict[str, bool] = json.loads(text)  # pyright: ignore[reportAny]
        return bool(parsed.get("result"))
    except Exception:
        logging.exception("LLM check failed")
        return False


# ---------------------------------------------------------------------------
# General cleaning functions
# ---------------------------------------------------------------------------


def clean_beehiiv_to_plaintext(text: str) -> str:
    """Convert Beehiiv markdown content to plain text via HTML.

    Returns:
        Plain text extracted from the rendered HTML.

    """
    html = markdown.markdown(text)
    soup = BeautifulSoup(html, features="html.parser")
    return soup.get_text()


def clean_beehiiv_emphasis(text: str) -> str:
    """Strip leftover Markdown emphasis markers from Beehiiv text.

    Returns:
        Text with underscored emphasis removed.

    """
    without_double = re.sub(r"__([^_]+)__", r"\1", text)
    return re.sub(r"_([^_]+)_", r"\1", without_double)


def unwrap_hard_wraps(text: str) -> str:
    """Unwrap hard-wrapped lines inside paragraphs while preserving paragraph breaks.

    Specifically, replaces single newlines (not preceded or followed by other newlines)
    with a single space, unless the paragraph looks like a list.

    Returns:
        The unwrapped text.

    """
    normalized = text.replace("\r\n", "\n")
    paragraphs = re.split(r"\n{2,}", normalized)
    unwrapped_paragraphs: list[str] = []
    for p in paragraphs:
        lines = p.split("\n")
        if len(lines) <= 1:
            unwrapped_paragraphs.append(p)
            continue

        is_list = False
        list_match_count = 0
        non_empty_lines = [line.strip() for line in lines if line.strip()]
        if not non_empty_lines:
            unwrapped_paragraphs.append(p)
            continue

        for line in non_empty_lines:
            # Matches "- foo", "* foo", "1. foo", "1) foo", etc.
            if re.match(r"^([-*•]|\d+[.)])\s", line):
                list_match_count += 1

        if list_match_count >= len(non_empty_lines) / 2:
            is_list = True

        if is_list:
            unwrapped_paragraphs.append(p)
        else:
            unwrapped_line = " ".join(non_empty_lines)
            unwrapped_paragraphs.append(unwrapped_line)

    return "\n\n".join(unwrapped_paragraphs)


_FOOTNOTE_DEF_RE = re.compile(r"^\[(\d+)\]\s+(.+)", re.DOTALL)


def _footnote_aside(number: str, note: str) -> str:
    """Render one footnote as an ``ASIDE_MARKER`` line so TTS voices it as an aside.

    Matches the structured extractor's ``Footnote {n}: ...`` phrasing (see
    ``aside_render``) so plaintext and HTML sources speak footnotes identically.

    Returns:
        A single ``❖ Footnote {n}: ...`` line, its text terminally punctuated.

    """
    text = note if note.endswith((".", "!", "?", "”")) else f"{note}."
    return f"{ASIDE_MARKER}Footnote {number}: {text}"


_SENTENCE_END_RE = re.compile(r"[.!?][\"'\u201d\u2019)\]]*(?=\s|$)|\n\s*\n")
# Text that already ends a sentence (a marker placed after the period: "done.[1]").
_ENDS_SENTENCE_RE = re.compile(r"[.!?][\"'\u201d\u2019)\]]*$")
# A period that ends an abbreviation ("et al.", "e.g.", "U.S.") rather than a sentence.
_ABBREVIATION_END_RE = re.compile(
    r"(?:\b(?:al|etc|vs|cf|e\.g|i\.e|mr|mrs|ms|dr|st|jr|sr|inc|co|no|fig|approx)|(?<![A-Za-z])(?:[A-Za-z]\.){1,}[A-Za-z])\.$",
    re.IGNORECASE,
)


def _is_abbreviation(body: str, end: int) -> bool:
    """Whether the terminal punctuation just before ``end`` closes an abbreviation.

    Returns:
        True if ``body[:end]`` (ignoring closing quotes/brackets) ends in a known abbreviation.

    """
    return bool(_ABBREVIATION_END_RE.search(body[:end].rstrip("\"'\u201d\u2019)]")))


def _sentence_end(body: str, pos: int) -> int:
    """Find where the sentence containing ``pos`` ends.

    A position right after terminal punctuation (the usual "sentence.[1]" layout) is
    already a sentence end. Periods closing abbreviations ("et al.", "U.S.") are not.

    Returns:
        The index just past the sentence's terminal punctuation (or the paragraph break /
        end of text when there is none).

    """
    if _ENDS_SENTENCE_RE.search(body, 0, pos) and not _is_abbreviation(body, pos):
        return pos
    for match in _SENTENCE_END_RE.finditer(body, pos):
        if match.group().startswith("\n"):
            return match.start()
        if not _is_abbreviation(body, match.end()):
            return match.end()
    return len(body)


def relocate_footnotes(text: str) -> tuple[str, int]:
    """Move ``[n]`` footnote definitions to an aside at their reference point.

    Footnote definitions are paragraphs whose text starts with ``[n]`` (as some
    newsletters format their footnotes). Each is spliced in at its inline ``[n]``
    reference's sentence as its own ``ASIDE_MARKER`` paragraph, so the multi-voice renderer
    reads it in the distinct aside voice (single-voice paths strip the marker and
    read it as plain narration). A marker mid-sentence defers the aside to the end of
    that sentence, so it always lands between sentences. Definitions with no inline reference are
    appended at the end, also as asides. A no-op when the text has no such footnote
    structure.

    Returns:
        A tuple of the transformed text and the number of footnotes relocated.

    """
    paragraphs = re.split(r"\n{2,}", text)
    definitions: dict[str, str] = {}
    body_paragraphs: list[str] = []
    for paragraph in paragraphs:
        match = _FOOTNOTE_DEF_RE.match(paragraph.strip())
        if match:
            definitions[match.group(1)] = " ".join(match.group(2).split())
        else:
            body_paragraphs.append(paragraph)

    if not definitions:
        return text, 0

    body = "\n\n".join(body_paragraphs)
    relocated = 0
    for number, note in definitions.items():
        marker = f"[{number}]"
        aside = _footnote_aside(number, note)
        index = body.find(marker)
        if index == -1:
            # No inline reference — keep the definition rather than lose content.
            body = body.rstrip() + f"\n\n{aside}"
            continue
        # Remove the marker, then place the aside at the end of the sentence that cited
        # it, so a mid-sentence reference ("Smith[1], who…") doesn't split the sentence.
        body = body[:index] + body[index + len(marker) :]
        end = _sentence_end(body, index)
        rest = body[end:].lstrip()
        body = body[:end].rstrip() + f"\n\n{aside}" + (f"\n\n{rest}" if rest else "")
        # Drop any further bare references to the same footnote.
        body = body.replace(marker, "")
        relocated += 1
    return body, relocated


# Canonical Roman numerals only (rejects "IIII", "VV", etc.); no anchors so it
# can be embedded with the line anchors below.
_ROMAN_CORE = r"(?=[MDCLXVI])M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
# A numeral alone on its own line (optional whitespace) followed by a period —
# i.e. a section header. The whole-line anchoring is what makes this collision-free.
_ROMAN_HEADER_RE = re.compile(rf"(?m)^[ \t]*({_ROMAN_CORE})\.[ \t]*$")

_CARDINAL_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_CARDINAL_TENS = ("", "", "twenty", "thirty", "forty")


def _roman_to_int(roman: str) -> int:
    """Convert a canonical Roman numeral to its integer value.

    Returns:
        The integer value.

    """
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for char in reversed(roman):
        current = values[char]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total


def _cardinal_words(number: int) -> str:
    """Spell a small non-negative integer (0-40) as words.

    Returns:
        The number in words (e.g. 14 -> "fourteen", 21 -> "twenty-one").

    """
    if number < 20:
        return _CARDINAL_ONES[number]
    tens, ones = divmod(number, 10)
    return _CARDINAL_TENS[tens] + (f"-{_CARDINAL_ONES[ones]}" if ones else "")


def normalize_roman_numerals(text: str) -> tuple[str, int]:
    """Spell out Roman-numeral section headers that TTS voices as letters.

    Converts a numeral alone on its own line (e.g. ``IV.``) to a spoken
    ``Section four.`` — a common section-divider pattern, which Google Wavenet
    otherwise reads as "eye-vee". Only canonical numerals in range
    1-40 convert; the whole-line anchoring makes it collision-free (it can never
    fire on ``IV`` mid-sentence, ``Mark IV`` or ``size XL``). A no-op otherwise.

    Returns:
        A tuple of the transformed text and the count of headers converted.

    """
    count = 0

    def replace_header(match: re.Match[str]) -> str:
        nonlocal count
        value = _roman_to_int(match.group(1))
        if not 1 <= value <= 40:
            return match.group(0)
        count += 1
        return f"Section {_cardinal_words(value)}."

    return _ROMAN_HEADER_RE.sub(replace_header, text), count


# Cleaning steps that only repair lossy plain-text intake; skipped for text produced by
# the structural HTML extractor (which excludes boilerplate by DOM position and needs no
# wrap/markdown/footnote repair). Whitespace, end-of-line pauses, and URL-to-context stay
# on for every profile because they are non-destructive.
STRUCTURED_ONLY_SKIP = frozenset(
    {
        "beehiiv_plaintext_conversion",
        "beehiiv_emphasis_removal",
        "unwrap_hard_wraps",
        "footnote_relocation",
        "roman_numeral_normalization",
        "legal_bracket_unwrap",
        "unsubscribe_removal",
        "view_online_removal",
        "substack_refs_removal",
        "standalone_at_removal",
    }
)

_URL_RE = re.compile(r"https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-z]{2,5}\b[-a-zA-Z0-9@:%_\+.~#?&//=]*")


def urls_to_context(text: str) -> tuple[str, int]:
    """Replace bare URLs with a spoken "a link to <host>" so context survives narration.

    Non-destructive by design: previously URLs were deleted, which could empty a footnote
    whose content was only a link. Keeping the host preserves the reference for the listener.

    Returns:
        The rewritten text and the number of URLs replaced.

    """

    def _link(match: re.Match[str]) -> str:
        host = urlparse(match.group(0)).netloc.removeprefix("www.").split(":")[0]
        return f"a link to {host}" if host else "a link"

    rewritten, count = _URL_RE.subn(_link, text)
    rewritten, bbg = re.subn(r"<?bbg://[^\s>]*>?", "a link", rewritten)
    return rewritten, count + bbg


def apply_general_cleaning(
    text: str,
    metadata: dict[str, str],
    config: PipelineConfig,
    stats: dict[str, dict[str, int | bool]],
) -> str:
    """Apply all built-in cleaning steps (URL removal, whitespace, etc.).

    Returns:
        The cleaned text.

    """

    def is_enabled(key: str) -> bool:
        return is_cleaning_step_enabled(key, metadata, config)

    def count_and_sub(pattern: str, replacement: str, text: str, key: str, flags: int = 0) -> str:
        matches = len(re.findall(pattern, text, flags=flags))
        if matches > 0:
            stats[key] = {"matches": int(stats.get(key, {}).get("matches", 0)) + matches}
        return re.sub(pattern, replacement, text, flags=flags)

    result: str = text

    # Beehiiv plaintext conversion (must be first — changes text representation)
    if is_enabled("beehiiv_plaintext_conversion") and metadata.get("source_kind") == "beehiiv":
        result = clean_beehiiv_to_plaintext(result)
        stats["beehiiv_plaintext_conversion"] = {"applied": True}

    # Beehiiv emphasis removal (right after plaintext conversion)
    if is_enabled("beehiiv_emphasis_removal") and metadata.get("source_kind") == "beehiiv":
        before_emphasis = result
        result = clean_beehiiv_emphasis(result)
        if result != before_emphasis:
            stats["beehiiv_emphasis_removal"] = {"applied": True}

    # Unwrap hard wraps (reconstruct paragraphs from line wraps)
    if is_enabled("unwrap_hard_wraps"):
        result = unwrap_hard_wraps(result)
        stats["unwrap_hard_wraps"] = {"applied": True}

    # Footnote relocation (move [n] definitions inline; before URL removal so the
    # moved footnote text is cleaned uniformly with the rest of the body)
    if is_enabled("footnote_relocation"):
        result, relocated_count = relocate_footnotes(result)
        if relocated_count:
            stats["footnote_relocation"] = {"relocated": relocated_count}

    # Roman-numeral normalization (spell out section headers + labelled numerals
    # that TTS voices as letters; before URL removal so line structure is intact)
    if is_enabled("roman_numeral_normalization"):
        result, roman_count = normalize_roman_numerals(result)
        if roman_count:
            stats["roman_numeral_normalization"] = {"converted": roman_count}

    # URL to spoken context ("a link to hyvee.com"), non-destructive (keeps the reference).
    if is_enabled("url_removal"):
        result, url_count = urls_to_context(result)
        if url_count:
            stats["url_removal"] = {"replaced": url_count}

    # Legal bracket unwrap [t]he -> the
    if is_enabled("legal_bracket_unwrap"):
        result = count_and_sub(
            r"\[([a-zA-Z])\]",
            r"\1",
            result,
            "legal_bracket_unwrap",
        )

    # Triple dash / divider removal. Only whole-line dividers are deleted (ASCII, em/en
    # dashes, asterisks, and spaced variants); inside a line, "---" is an em-dash stand-in
    # ("5---4") and is spoken as a dash, and a ***wrapper*** is unwrapped. Runs of
    # asterisks glued to a word ("f***") are censored words and are left alone.
    if is_enabled("triple_dash_removal"):
        result = count_and_sub(
            r"(?m)^[ \t]*[-*\u2014\u2013]{3,}[ \t]*$",
            "",
            result,
            "triple_dash_removal",
        )
        result = count_and_sub(r"[ \t]*-{3,}[ \t]*", " \u2014 ", result, "triple_dash_removal")
        result = count_and_sub(
            r"(?<![\w*])\*{3,}(?=\S)([^*\n]+?)(?<=\S)\*{3,}(?![\w*])",
            r"\1",
            result,
            "triple_dash_removal",
        )
        result = count_and_sub(
            r"(?m)^[ \t]*([-*\u2014\u2013][ \t]+){2,}[-*\u2014\u2013][ \t]*$",
            "",
            result,
            "triple_dash_removal",
        )

    # Empty bracket removal (including interior whitespace)
    if is_enabled("empty_bracket_removal"):
        before_brackets = result
        result = re.sub(r"\[\s*\]", "", result)
        result = re.sub(r"\(\s*\)", "", result)
        result = re.sub(r"<\s*>", "", result)
        bracket_diff = len(before_brackets) - len(result)
        if bracket_diff > 0:
            stats["empty_bracket_removal"] = {"chars_removed": bracket_diff}

    # Whitespace collapse
    if is_enabled("whitespace_collapse"):
        result = re.sub(r"[^\S\r\n]+", " ", result)
        stats["whitespace_collapse"] = {"applied": True}

    # Unsubscribe removal
    if is_enabled("unsubscribe_removal"):
        result = count_and_sub(
            r"(\r\n|\r|\n){2}Unsubscribe",
            "",
            result,
            "unsubscribe_removal",
        )

    # View online removal
    if is_enabled("view_online_removal"):
        result = count_and_sub(
            r"View this post on the web at (\r\n|\r|\n){2}",
            "",
            result,
            "view_online_removal",
        )

    # Substack refs removal
    if is_enabled("substack_refs_removal"):
        result = count_and_sub(
            r"(?im)^\s*substacks referenced above:.*\r?\n(?:\s*@\s*\r?\n)*",
            "",
            result,
            "substack_refs_removal",
        )

    # Substack UI/footer boilerplate that its HTML emails carry but the plain-text part
    # did not: standalone action-button chrome (Share/Comment/Like/Restack/etc.), the
    # free-subscriber upgrade CTA, and the copyright/address/unsubscribe trailer — anchored
    # on Substack's "548 Market Street PMB 72296" address. Gated on the Substack platform.
    if is_enabled("substack_boilerplate_removal") and metadata.get("source_kind") == "substack":
        result = count_and_sub(
            r"(?im)^(?:Share|Comment|Like|Restack|Leave a comment|Subscribe now|Read in app|Give a gift subscription|Pledge your support|Upgrade to paid|[^\n]*currently a free subscriber to [^\n]*?upgrade your subscription[^\n]*|.*©\s*\d{4}[^\n]*548 Market Street PMB 72296, San Francisco, CA 94104\s*Unsubscribe[^\n]*)\.?\s*$",
            "",
            result,
            "substack_boilerplate_removal",
        )

    # Standalone @ removal
    if is_enabled("standalone_at_removal"):
        result = count_and_sub(
            r"(?m)^\s*@\s*$\r?\n?",
            "",
            result,
            "standalone_at_removal",
        )

    return result


def is_cleaning_step_enabled(key: str, metadata: dict[str, str], config: PipelineConfig) -> bool:
    """Resolve whether a general-cleaning step runs for this file.

    Precedence: structured-extraction skip list, then the first matching per-source
    override that names the step, then the global setting, then the default (every
    step except ``unwrap_hard_wraps`` is on).

    Returns:
        True if the step should run.

    """
    # Structural-extractor output needs none of the plain-text repair steps.
    if metadata.get("extraction") == "structured" and key in STRUCTURED_ONLY_SKIP:
        return False
    gc_config = config.get("general_cleaning") or GeneralCleaningConfig()
    overrides: list[CleaningOverride] = gc_config.get("overrides") or []
    for override in overrides:
        match_val = override.get("match")
        if isinstance(match_val, dict) and evaluate_match(match_val, metadata) and key in override:  # pyright: ignore[reportUnknownArgumentType]
            return bool(override[key])
    if key in gc_config:
        return bool(gc_config[key])  # pyright: ignore[reportUnknownArgumentType]
    return key != "unwrap_hard_wraps"


def apply_end_of_line_punctuation(
    text: str,
    metadata: dict[str, str],
    config: PipelineConfig,
    stats: dict[str, dict[str, int | bool]],
) -> str:
    """Append terminal punctuation to lines ending in a word character.

    Runs after YAML text removals and replacements so anchored removal rules
    (e.g. ^Advertisement$) match against un-punctuated line endings.

    Returns:
        The text with missing line-ending periods appended.

    """
    # Future architecture item: Replace end-of-line period insertion with explicit SSML <break> tags (see PUNCHLIST.md)
    if not is_cleaning_step_enabled("end_of_line_punctuation", metadata, config):
        return text
    stats["end_of_line_punctuation"] = {"applied": True}
    return re.sub(r"(\w)\s*(\r\n|\r|\n)", r"\1.\2", text)


# ---------------------------------------------------------------------------
# YAML text removals and replacements
# ---------------------------------------------------------------------------


def apply_text_removals(text: str, config: PipelineConfig, stats: dict[str, dict[str, int]]) -> str:
    """Apply YAML-configured regex removals to text content.

    Returns:
        Text with matched patterns removed.

    """
    result: str = text
    for removal in config.get("text_removals") or []:
        pattern = removal["pattern"]
        flags = parse_flags(removal.get("flags"))
        reason = removal["reason"]
        matches = len(re.findall(pattern, result, flags=flags))
        if matches > 0:
            result = re.sub(pattern, "", result, flags=flags)
            stats[reason] = {"matches": matches}
    return result


def apply_text_replacements(text: str, config: PipelineConfig, stats: dict[str, dict[str, int]]) -> str:
    """Apply YAML-configured regex replacements to text content.

    Returns:
        Text with matched patterns replaced.

    """
    result: str = text
    for repl in config.get("text_replacements") or []:
        pattern = repl["pattern"]
        replacement = repl["replacement"]
        flags = parse_flags(repl.get("flags"))
        reason = repl["reason"]
        matches = len(re.findall(pattern, result, flags=flags))
        if matches > 0:
            result = re.sub(pattern, replacement, result, flags=flags)
            stats[reason] = {"matches": matches}
    return result


# ---------------------------------------------------------------------------
# Stats management
# ---------------------------------------------------------------------------


def load_today_stats() -> dict[str, FileStats]:
    """Load today's stats JSON file, or return an empty dict.

    Returns:
        The stats dict for today.

    """
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    stats_path = pathlib.Path(STATS_DIR) / f"{today}.json"
    if stats_path.exists():
        result: dict[str, FileStats] = json.loads(stats_path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        return result
    return {}


def save_stats(stats: dict[str, FileStats]) -> None:
    """Write the stats dict to today's JSON file."""
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    stats_path = pathlib.Path(STATS_DIR) / f"{today}.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    _ = stats_path.write_text(
        json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def rotate_stats() -> None:
    """Delete stats files older than the retention period."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=STATS_RETENTION_DAYS)
    stats_path = pathlib.Path(STATS_DIR)
    if not stats_path.exists():
        return
    for stats_file in stats_path.glob("*.json"):
        try:
            file_date = datetime.strptime(stats_file.stem, "%Y-%m-%d").replace(tzinfo=UTC)
            if file_date < cutoff:
                stats_file.unlink()
                logging.info("Rotated old stats file: %s", stats_file.name)
        except ValueError:
            logging.warning("Skipping non-date stats file: %s", stats_file.name)
            continue


# ---------------------------------------------------------------------------
# File writing helpers
# ---------------------------------------------------------------------------


def write_metadata_and_content(
    filepath: pathlib.Path,
    metadata: dict[str, str],
    content: str,
) -> None:
    """Write a metadata-prefixed text file."""
    meta_lines = [f"META_{key.upper()}: {value}" for key, value in metadata.items()]
    meta_block = "\n".join(meta_lines)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    _ = filepath.write_text(
        meta_block + "\n\n" + content,
        encoding="utf-8",
    )


def _norm_header(text: str) -> str:
    """Normalize a header or byline string for exact structural comparison.

    Returns:
        The normalized lowercase header string with punctuation stripped.

    """
    cleaned = re.sub(r"['\u2019]", "", text)
    return " ".join(re.sub(r"[^\w\s]+", " ", cleaned).split()).lower()


def body_leads_with_byline(body: str, from_name: str, title: str) -> bool:
    """Check if the body text already begins with the author byline or headline.

    Prevents double-byline at the start of articles (e.g. RSS feeds where the extracted
    body already starts with author/headline, or blog archives where line 0 is the post title).

    Returns:
        True if the leading lines match the author or headline; False otherwise.

    """
    if not body.strip():
        return False
    lines = [_norm_header(ln) for ln in body.splitlines() if ln.strip()][:2]
    if not lines:
        return False

    norm_from = _norm_header(from_name) if from_name else ""
    norm_title = _norm_header(title) if title else ""

    l0 = lines[0]
    # Check if the very first line is exactly the title or author
    if norm_title and l0 in {norm_title, f"{norm_title} by {norm_from}"}:
        return True
    if norm_from and l0 in {norm_from, f"by {norm_from}"}:
        return True
    if norm_from and norm_title and l0 == f"{norm_from} {norm_title}":
        return True
    # Check if line 0 is author and line 1 is title
    return bool(
        len(lines) > 1 and norm_from and norm_title and l0 in {norm_from, f"by {norm_from}"} and lines[1] == norm_title
    )


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def load_config() -> PipelineConfig:
    """Load and validate filters.yaml, returning an empty dict if absent.

    Returns:
        The parsed and validated config dict.

    """
    config_path = pathlib.Path(CONFIG_FILE)
    if not config_path.exists():
        logging.info("No filters.yaml found; using defaults (no filters, no removals)")
        return {}
    raw = config_path.read_text(encoding="utf-8")
    config: PipelineConfig = yaml.safe_load(raw) or {}
    validate_config(config)
    return config


def is_passthrough(metadata: dict[str, str]) -> bool:
    """Return True for generated files that must skip content-mutating cleaning.

    Comment-highlights episodes carry speaker-tagged segment markers whose
    structure is load-bearing for multi-voice synthesis, so they pass through
    raw -> cleaned untouched.

    Returns:
        True if the file must skip content-mutating cleaning.

    """
    return metadata.get("intake_type", "") == "archive-comments"


def process_file(filepath: pathlib.Path, config: PipelineConfig, all_stats: dict[str, FileStats]) -> None:
    """Filter, clean, and write a single raw text file."""
    filename = filepath.name
    logging.info("Processing: %s", filename)

    # Read and parse
    raw_text = filepath.read_text(encoding="utf-8")
    metadata: dict[str, str]
    metadata, content_raw = split_metadata(raw_text)
    timestamp = datetime.now(tz=UTC).isoformat(timespec="microseconds")

    # Initialize stats entry
    file_stats: FileStats = {
        "file": filename,
        "raw_archive": None,
        "cleaned_archive": None,
        "filtered_archive": None,
        "filters_checked": [],
        "filters_matched": [],
        "text_removals": {},
        "text_replacements": {},
        "general_cleaning": {},
        "outcome": None,
        "chars_before": len(content_raw),
        "chars_after": None,
    }

    # --- Run filters ---
    filters = config.get("filters") or []
    skip_file: bool = False
    filter_reason: str = ""

    for filt in filters:
        reason = filt["reason"]
        action = filt.get("action", "skip")

        if not evaluate_match(filt["match"], metadata):
            file_stats["filters_checked"].append(reason)
            continue

        # Match block passed — check LLM if needed
        if "llm_check" in filt:
            llm_result = evaluate_llm_check(
                filt["llm_check"],
                metadata,
                content_raw,
            )
            if not llm_result:
                file_stats["filters_checked"].append(reason)
                continue

        # Filter matched
        file_stats["filters_checked"].append(reason)
        file_stats["filters_matched"].append(reason)

        if action == "notify" and "notify" in filt:
            notify_config = filt["notify"]
            send_gotify_notification(
                title=notify_config["title"],
                message=f"{filename}\n\n{metadata.get('title', '')}",
                priority=notify_config["priority"],
            )
            continue

        if action == "podly_process":
            # Podly URL/credentials come from PODLY_URL / PODLY_USERNAME / PODLY_PASSWORD.
            logging.info("Enabling episode for processing in Podly directly: %s", filename)
            enabled = enable_post_in_podly(
                guid=metadata.get("guid"),
                download_url=metadata.get("source_url"),
                title=metadata.get("title"),
                feed_name=metadata.get("from"),
            )
            skip_file = True
            filter_reason = reason
            if not enabled:
                # The file is still filtered (TTS would be wrong for it), so say so loudly
                # instead of recording a success reason for an episode Podly never got.
                filter_reason = f"{reason} [Podly enable FAILED — enable it manually]"
                send_gotify_notification(
                    title="Podly enable failed",
                    message=f"{filename}\n\n{metadata.get('title', '')}\n\nEnable it in Podly manually.",
                    priority=8,
                )
            break

        # Remaining case is skip (notify already handled above)
        skip_file = True
        filter_reason = reason
        break

    if skip_file:
        # Write to filtered dir with reason
        filtered_metadata = {**metadata, "filtered_reason": filter_reason}
        filtered_path = pathlib.Path(FILTERED_DIR) / filename
        write_metadata_and_content(filtered_path, filtered_metadata, content_raw)

        # Archive raw
        raw_archive_path = pathlib.Path(RAW_ARCHIVE_DIR) / filename
        raw_archive_path.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(str(filepath), str(raw_archive_path))

        file_stats["filtered_archive"] = str(filtered_path)
        file_stats["raw_archive"] = str(raw_archive_path)
        file_stats["outcome"] = "filtered"
        file_stats["chars_after"] = len(content_raw)
        all_stats[timestamp] = file_stats

        # Delete raw input
        filepath.unlink()
        logging.info("Filtered: %s (reason: %s)", filename, filter_reason)
        return

    # --- Apply cleaning (skipped entirely for passthrough files) ---
    passthrough = is_passthrough(metadata)
    cleaned_text: str = content_raw
    if not passthrough:
        gc_stats: dict[str, dict[str, int | bool]] = {}
        cleaned_text = apply_general_cleaning(
            content_raw,
            metadata,
            config,
            gc_stats,
        )
        file_stats["general_cleaning"] = gc_stats

        # YAML text removals
        removal_stats: dict[str, dict[str, int]] = {}
        cleaned_text = apply_text_removals(cleaned_text, config, removal_stats)
        file_stats["text_removals"] = removal_stats

        # YAML text replacements
        replacement_stats: dict[str, dict[str, int]] = {}
        cleaned_text = apply_text_replacements(cleaned_text, config, replacement_stats)
        file_stats["text_replacements"] = replacement_stats

        # End-of-line punctuation runs after removals/replacements so anchors match clean line endings
        cleaned_text = apply_end_of_line_punctuation(cleaned_text, metadata, config, gc_stats)
        file_stats["general_cleaning"] = gc_stats

    # Check empty (before adding header/footer, which would mask empty content)
    if not cleaned_text.strip():
        empty_reason = "Content empty after cleaning"
        filtered_metadata_empty = {**metadata, "filtered_reason": empty_reason}
        filtered_path_empty = pathlib.Path(FILTERED_DIR) / filename
        write_metadata_and_content(filtered_path_empty, filtered_metadata_empty, "")

        raw_archive_path_empty = pathlib.Path(RAW_ARCHIVE_DIR) / filename
        raw_archive_path_empty.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(str(filepath), str(raw_archive_path_empty))

        file_stats["filtered_archive"] = str(filtered_path_empty)
        file_stats["raw_archive"] = str(raw_archive_path_empty)
        file_stats["outcome"] = "filtered_empty"
        file_stats["chars_after"] = 0
        all_stats[timestamp] = file_stats

        filepath.unlink()
        logging.info("Filtered (empty after cleaning): %s", filename)
        send_gotify_notification(
            "Skipping empty text-to-speech content",
            f"{filename}: empty after cleaning.",
        )
        return

    # Prepend and append author + title (skipped for passthrough files, whose
    # body is already exactly the speaker-tagged text the synthesizer expects)
    if not passthrough:
        from_name = metadata.get("from", "").strip()
        title = metadata.get("title", "").strip()
        header = (f"{from_name}.\n" if from_name else "") + (f"{title}.\n" if title else "")
        footer = "\n\n" + (f"{from_name}.\n" if from_name else "") + (f"{title}.\n" if title else "")
        if header and not body_leads_with_byline(cleaned_text, from_name, title):
            cleaned_text = header + "\n" + cleaned_text
        if from_name or title:
            cleaned_text = cleaned_text.rstrip() + footer

    # Check too-big
    if len(cleaned_text) >= CHARACTER_LIMIT:
        toobig_reason = f"Content too large: {len(cleaned_text)} chars (limit: {CHARACTER_LIMIT})"
        filtered_metadata_big = {**metadata, "filtered_reason": toobig_reason}
        filtered_path_big = pathlib.Path(FILTERED_DIR) / filename
        write_metadata_and_content(filtered_path_big, filtered_metadata_big, cleaned_text)

        raw_archive_path_big = pathlib.Path(RAW_ARCHIVE_DIR) / filename
        raw_archive_path_big.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(str(filepath), str(raw_archive_path_big))

        file_stats["filtered_archive"] = str(filtered_path_big)
        file_stats["raw_archive"] = str(raw_archive_path_big)
        file_stats["outcome"] = "filtered_too_big"
        file_stats["chars_after"] = len(cleaned_text)
        all_stats[timestamp] = file_stats

        filepath.unlink()
        logging.info("Filtered (too big): %s (%d chars)", filename, len(cleaned_text))
        send_gotify_notification(
            "Skipping large text-to-speech content",
            f"{filename}: {len(cleaned_text)} chars exceeds {CHARACTER_LIMIT} limit.",
        )
        return

    # --- Write outputs ---
    # Write cleaned output
    cleaned_path = pathlib.Path(CLEANED_OUTPUT_DIR) / filename
    write_metadata_and_content(cleaned_path, metadata, cleaned_text)

    # Archive raw
    raw_archive_final = pathlib.Path(RAW_ARCHIVE_DIR) / filename
    raw_archive_final.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy2(str(filepath), str(raw_archive_final))

    # Archive cleaned
    cleaned_archive = pathlib.Path(CLEANED_ARCHIVE_DIR) / filename
    cleaned_archive.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy2(str(cleaned_path), str(cleaned_archive))

    file_stats["raw_archive"] = str(raw_archive_final)
    file_stats["cleaned_archive"] = str(cleaned_archive)
    file_stats["outcome"] = "cleaned"
    file_stats["chars_after"] = len(cleaned_text)
    all_stats[timestamp] = file_stats

    # Delete raw input (last step — only after all writes succeeded)
    filepath.unlink()
    logging.info(
        "Cleaned: %s (%d -> %d chars)",
        filename,
        len(content_raw),
        len(cleaned_text),
    )


def process_files() -> None:
    """Process all raw text files: filter, clean, and output for TTS."""
    # Ensure directories exist
    for dir_path in (RAW_INPUT_DIR, RAW_ARCHIVE_DIR, CLEANED_OUTPUT_DIR, CLEANED_ARCHIVE_DIR, FILTERED_DIR, STATS_DIR):
        pathlib.Path(dir_path).mkdir(parents=True, exist_ok=True)

    # Rotate old stats
    rotate_stats()

    # Load config
    config = load_config()

    # Validate rule ordering
    filters = config.get("filters") or []
    ordering_errors = validate_rule_ordering(filters)
    shadowed_matches: list[dict[str, dict[str, str]]] = []

    if ordering_errors:
        error_msg = "Filter rule ordering issues:\n" + "\n".join(ordering_errors)
        logging.error(error_msg)
        send_gotify_notification(
            "prepare_text.py: filter rule ordering error",
            error_msg + "\n\nAffected files will be left in text-input-raw/ until this is fixed.",
            priority=9,
        )
        # Collect the match blocks from skip rules that shadow later rules
        for error in ordering_errors:
            # Extract the index of the skip rule from the error message
            idx_str = error.split("filters[")[1].split("]")[0]
            idx = int(idx_str)
            shadowed_matches.append(filters[idx]["match"])

    # Load today's stats (append to existing if re-run)
    all_stats = load_today_stats()

    # Process files
    txt_files = sorted(pathlib.Path(RAW_INPUT_DIR).glob("*.txt"))
    for txt_file in txt_files:
        # Check if this file matches a shadowed skip rule
        if shadowed_matches:
            raw_text_check = txt_file.read_text(encoding="utf-8")
            meta_check = split_metadata(raw_text_check)[0]
            is_shadowed = any(evaluate_match(match, meta_check) for match in shadowed_matches)
            if is_shadowed:
                logging.warning(
                    "Skipping %s due to rule ordering conflict (left in raw)",
                    txt_file.name,
                )
                continue

        try:
            process_file(txt_file, config, all_stats)
        except Exception:
            logging.exception("Error processing %s — leaving in raw for retry", txt_file.name)
            continue

    save_stats(all_stats)


if __name__ == "__main__":
    process_files()
