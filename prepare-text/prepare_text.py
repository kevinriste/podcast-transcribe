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
from datetime import datetime, timedelta
from typing import Final

import markdown
import requests
import yaml
from bs4 import BeautifulSoup
from google import genai

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

RAW_INPUT_DIR: Final = "text-input-raw"
RAW_ARCHIVE_DIR: Final = "text-input-raw-archive"
CLEANED_OUTPUT_DIR: Final = "text-input-cleaned"
CLEANED_ARCHIVE_DIR: Final = "text-input-cleaned-archive"
FILTERED_DIR: Final = "text-input-filtered"
STATS_DIR: Final = "stats"
CONFIG_FILE: Final = "filters.yaml"
CHARACTER_LIMIT: Final = 150000
STATS_RETENTION_DAYS: Final = 365
LLM_MODEL: Final = "gemini-3.1-flash-lite-preview"

_gemini_client: genai.Client | None = None


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------


def get_gemini_client() -> genai.Client:
    global _gemini_client  # noqa: PLW0603
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


# ---------------------------------------------------------------------------
# Gotify notifications
# ---------------------------------------------------------------------------


def send_gotify_notification(title: str, message: str, priority: int = 6) -> None:
    final_server: Final = os.environ.get("GOTIFY_SERVER")
    final_token: Final = os.environ.get("GOTIFY_TOKEN")
    if not final_server or not final_token:
        logging.warning("Gotify env vars not set; skipping notification.")
        return
    final_url: Final = f"{final_server}/message?token={final_token}"
    final_data: Final = {"title": title, "message": message, "priority": priority}
    requests.post(final_url, data=final_data, timeout=30)


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------


def split_metadata(raw_text: str) -> tuple[dict[str, str], str]:
    if not raw_text.startswith("META_"):
        return {}, raw_text
    final_lines: Final = raw_text.splitlines()
    metadata: Final[dict[str, str]] = {}
    current_key: str | None = None
    content_start: int = len(final_lines)
    for idx, line in enumerate(final_lines):
        if line.startswith("META_"):
            if ":" not in line:
                content_start = idx
                break
            key_raw = line.split(":", 1)[0]
            value_raw = line.split(":", 1)[1]
            current_key = key_raw.replace("META_", "").lower()
            metadata[current_key] = value_raw.strip()
            continue
        if line.startswith((" ", "\t")) and current_key:
            metadata[current_key] = (
                f"{metadata.get(current_key, '')} {line.strip()}".strip()
            )
            continue
        if not line.strip():
            content_start = idx + 1
            break
        content_start = idx
        break
    final_content: Final = (
        "\n".join(final_lines[content_start:]) if content_start < len(final_lines) else ""
    )
    return metadata, final_content


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------

VALID_MATCH_FIELDS: Final = frozenset(
    {"from", "title", "source_url", "source_kind", "source_name", "intake_type"},
)
VALID_MATCH_OPERATORS: Final = frozenset({"contains", "not_contains"})
VALID_ACTIONS: Final = frozenset({"skip", "notify"})
VALID_FLAGS: Final = frozenset({"ignorecase", "multiline", "dotall"})
VALID_CLEANING_KEYS: Final = frozenset(
    {
        "url_removal",
        "triple_dash_removal",
        "legal_bracket_unwrap",
        "empty_bracket_removal",
        "whitespace_collapse",
        "unsubscribe_removal",
        "view_online_removal",
        "substack_refs_removal",
        "standalone_at_removal",
        "beehiiv_plaintext_conversion",
        "beehiiv_emphasis_removal",
        "end_of_line_punctuation",
    },
)


def parse_flags(flags_raw: str | list[str] | None) -> int:
    if flags_raw is None:
        return 0
    final_flag_list: Final = [flags_raw] if isinstance(flags_raw, str) else flags_raw
    result: int = 0
    for flag_name in final_flag_list:
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


def validate_match_block(match_block: dict, context: str) -> None:
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


def validate_config(config: dict) -> None:
    final_valid_top_keys: Final = frozenset(
        {"filters", "general_cleaning", "text_removals", "text_replacements"},
    )
    for key in config:
        if key not in final_valid_top_keys:
            msg = f"Unknown top-level key: {key!r}"
            raise ValueError(msg)

    # Validate filters
    for idx, filt in enumerate(config.get("filters") or []):
        final_ctx = f"filters[{idx}]"
        if "match" not in filt:
            msg = f"{final_ctx}: 'match' is required"
            raise ValueError(msg)
        validate_match_block(filt["match"], final_ctx)
        if "reason" not in filt:
            msg = f"{final_ctx}: 'reason' is required"
            raise ValueError(msg)
        final_action = filt.get("action", "skip")
        if final_action not in VALID_ACTIONS:
            msg = f"{final_ctx}: invalid action {final_action!r} (valid: {', '.join(sorted(VALID_ACTIONS))})"
            raise ValueError(msg)
        if final_action == "notify" and "notify" not in filt:
            msg = f"{final_ctx}: action 'notify' requires a 'notify' block"
            raise ValueError(msg)
        if "notify" in filt:
            final_notify = filt["notify"]
            if "priority" not in final_notify:
                msg = f"{final_ctx}: notify block requires 'priority'"
                raise ValueError(msg)
            if "title" not in final_notify:
                msg = f"{final_ctx}: notify block requires 'title'"
                raise ValueError(msg)
        if "llm_check" in filt and not isinstance(filt["llm_check"], str):
            msg = f"{final_ctx}: 'llm_check' must be a string"
            raise ValueError(msg)

    # Validate general_cleaning
    final_gc: Final = config.get("general_cleaning") or {}
    for key, value in final_gc.items():
        if key == "overrides":
            if not isinstance(value, list):
                msg = "general_cleaning.overrides must be a list"
                raise ValueError(msg)
            for oidx, override in enumerate(value):
                final_octx = f"general_cleaning.overrides[{oidx}]"
                if "match" not in override:
                    msg = f"{final_octx}: 'match' is required"
                    raise ValueError(msg)
                validate_match_block(override["match"], final_octx)
                for okey in override:
                    if okey != "match" and okey not in VALID_CLEANING_KEYS:
                        msg = f"{final_octx}: unknown key {okey!r}"
                        raise ValueError(msg)
        elif key not in VALID_CLEANING_KEYS:
            msg = f"general_cleaning: unknown key {key!r}"
            raise ValueError(msg)
        elif not isinstance(value, bool):
            msg = f"general_cleaning.{key}: must be a boolean"
            raise ValueError(msg)

    # Validate text_removals
    for idx, removal in enumerate(config.get("text_removals") or []):
        final_rctx = f"text_removals[{idx}]"
        if "pattern" not in removal:
            msg = f"{final_rctx}: 'pattern' is required"
            raise ValueError(msg)
        if "reason" not in removal:
            msg = f"{final_rctx}: 'reason' is required"
            raise ValueError(msg)
        final_rflags = parse_flags(removal.get("flags"))
        try:
            re.compile(removal["pattern"], final_rflags)
        except re.error as exc:
            msg = f"{final_rctx}: invalid regex: {exc}"
            raise ValueError(msg) from exc

    # Validate text_replacements
    for idx, repl in enumerate(config.get("text_replacements") or []):
        final_pctx = f"text_replacements[{idx}]"
        if "pattern" not in repl:
            msg = f"{final_pctx}: 'pattern' is required"
            raise ValueError(msg)
        if "replacement" not in repl:
            msg = f"{final_pctx}: 'replacement' is required"
            raise ValueError(msg)
        if "reason" not in repl:
            msg = f"{final_pctx}: 'reason' is required"
            raise ValueError(msg)
        final_pflags = parse_flags(repl.get("flags"))
        try:
            re.compile(repl["pattern"], final_pflags)
        except re.error as exc:
            msg = f"{final_pctx}: invalid regex: {exc}"
            raise ValueError(msg) from exc


def validate_rule_ordering(filters: list[dict]) -> list[str]:
    """Check for skip rules that shadow later rules with overlapping match criteria.

    Returns list of error messages (empty if no problems).
    """
    errors: Final[list[str]] = []
    for i, rule_a in enumerate(filters):
        if rule_a.get("action", "skip") != "skip":
            continue
        final_match_a = rule_a["match"]
        for j in range(i + 1, len(filters)):
            final_rule_b = filters[j]
            final_match_b = final_rule_b["match"]
            # Check if match_b is a subset of or identical to match_a
            # (meaning everything match_b matches, match_a also matches)
            if _match_is_subset(subset=final_match_b, superset=final_match_a):
                errors.append(
                    f"filters[{i}] (skip, reason: {rule_a['reason']!r}) shadows "
                    f"filters[{j}] (reason: {final_rule_b['reason']!r}) — "
                    f"the later rule will never fire. Reorder or adjust match criteria.",
                )
    return errors


def _match_is_subset(subset: dict, superset: dict) -> bool:
    """Check if everything matched by 'subset' criteria is also matched by 'superset'.

    A superset match has fewer or equal constraints — so subset must contain
    all fields from superset with compatible operators.
    """
    for field, operators in superset.items():
        if field not in subset:
            return False
        final_sub_ops = subset[field]
        for op, value in operators.items():
            if op not in final_sub_ops:
                return False
            if final_sub_ops[op].lower() != value.lower():
                return False
    return True


# ---------------------------------------------------------------------------
# Match evaluation
# ---------------------------------------------------------------------------


def evaluate_match(match_block: dict, metadata: dict[str, str]) -> bool:
    for field, operators in match_block.items():
        final_meta_value = metadata.get(field, "").lower()
        for op, target in operators.items():
            final_target_lower = target.lower()
            if op == "contains" and final_target_lower not in final_meta_value:
                return False
            if op == "not_contains" and final_target_lower in final_meta_value:
                return False
    return True


def evaluate_llm_check(prompt_template: str, metadata: dict[str, str], content: str) -> bool:
    final_title: Final = metadata.get("title", "")
    final_full_prompt: Final = f"{prompt_template}\n\nTitle: {final_title}\n\nContent:\n{content}"
    try:
        final_client: Final = get_gemini_client()
        final_response: Final = final_client.models.generate_content(
            model=LLM_MODEL,
            contents=final_full_prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "object",
                    "properties": {"result": {"type": "boolean"}},
                    "required": ["result"],
                },
            },
        )
        final_parsed: Final = json.loads(final_response.text)
        return bool(final_parsed.get("result", False))
    except Exception as exc:
        logging.exception("LLM check failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# General cleaning functions
# ---------------------------------------------------------------------------


def clean_beehiiv_to_plaintext(text: str) -> str:
    final_html: Final = markdown.markdown(text)
    final_soup: Final = BeautifulSoup(final_html, features="html.parser")
    return final_soup.get_text()


def clean_beehiiv_emphasis(text: str) -> str:
    final_without_double: Final = re.sub(r"__([^_]+)__", r"\1", text)
    return re.sub(r"_([^_]+)_", r"\1", final_without_double)


def apply_general_cleaning(
    text: str,
    metadata: dict[str, str],
    config: dict,
    stats: dict[str, dict],
) -> str:
    final_gc_config: Final = config.get("general_cleaning") or {}
    final_overrides: Final = final_gc_config.get("overrides") or []

    def is_enabled(key: str) -> bool:
        # Check per-source overrides first
        for override in final_overrides:
            if evaluate_match(override["match"], metadata) and key in override:
                return bool(override[key])
        # Then global config
        if key in final_gc_config:
            return bool(final_gc_config[key])
        # Default: enabled
        return True

    def count_and_sub(pattern: str, replacement: str, text: str, key: str, flags: int = 0) -> str:
        final_matches: Final = len(re.findall(pattern, text, flags=flags))
        if final_matches > 0:
            stats[key] = {"matches": final_matches}
        return re.sub(pattern, replacement, text, flags=flags)

    result: str = text

    # Beehiiv plaintext conversion (must be first — changes text representation)
    if is_enabled("beehiiv_plaintext_conversion") and metadata.get("source_kind") == "beehiiv":
        result = clean_beehiiv_to_plaintext(result)
        stats["beehiiv_plaintext_conversion"] = {"applied": True}

    # Beehiiv emphasis removal (right after plaintext conversion)
    if is_enabled("beehiiv_emphasis_removal") and metadata.get("source_kind") == "beehiiv":
        final_before_emphasis: Final = result
        result = clean_beehiiv_emphasis(result)
        if result != final_before_emphasis:
            stats["beehiiv_emphasis_removal"] = {"applied": True}

    # URL removal
    if is_enabled("url_removal"):
        result = count_and_sub(
            r"https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-z]{2,5}\b([-a-zA-Z0-9@:%_\+.~#?&//=]*)",
            "",
            result,
            "url_removal",
        )

    # Legal bracket unwrap [t]he -> the
    if is_enabled("legal_bracket_unwrap"):
        result = count_and_sub(
            r"\[([a-zA-Z])\]",
            r"\1",
            result,
            "legal_bracket_unwrap",
        )

    # Triple dash removal
    if is_enabled("triple_dash_removal"):
        result = count_and_sub(r"---+", "", result, "triple_dash_removal")

    # Empty bracket removal
    if is_enabled("empty_bracket_removal"):
        final_before_brackets: Final = result
        result = re.sub(r"\[\]", "", result)
        result = re.sub(r"\(\)", "", result)
        result = result.replace("<>", "")
        final_bracket_diff: Final = len(final_before_brackets) - len(result)
        if final_bracket_diff > 0:
            stats["empty_bracket_removal"] = {"chars_removed": final_bracket_diff}

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

    # Standalone @ removal
    if is_enabled("standalone_at_removal"):
        result = count_and_sub(
            r"(?m)^\s*@\s*$\r?\n?",
            "",
            result,
            "standalone_at_removal",
        )

    # End-of-line punctuation (must be last)
    if is_enabled("end_of_line_punctuation"):
        result = re.sub(r"(\w)\s*(\r\n|\r|\n)", r"\1.\2", result)
        stats["end_of_line_punctuation"] = {"applied": True}

    return result


# ---------------------------------------------------------------------------
# YAML text removals and replacements
# ---------------------------------------------------------------------------


def apply_text_removals(text: str, config: dict, stats: dict[str, dict]) -> str:
    result: str = text
    for removal in config.get("text_removals") or []:
        final_pattern = removal["pattern"]
        final_flags = parse_flags(removal.get("flags"))
        final_reason = removal["reason"]
        final_matches = len(re.findall(final_pattern, result, flags=final_flags))
        if final_matches > 0:
            result = re.sub(final_pattern, "", result, flags=final_flags)
            stats[final_reason] = {"matches": final_matches}
    return result


def apply_text_replacements(text: str, config: dict, stats: dict[str, dict]) -> str:
    result: str = text
    for repl in config.get("text_replacements") or []:
        final_pattern = repl["pattern"]
        final_replacement = repl["replacement"]
        final_flags = parse_flags(repl.get("flags"))
        final_reason = repl["reason"]
        final_matches = len(re.findall(final_pattern, result, flags=final_flags))
        if final_matches > 0:
            result = re.sub(final_pattern, final_replacement, result, flags=final_flags)
            stats[final_reason] = {"matches": final_matches}
    return result


# ---------------------------------------------------------------------------
# Stats management
# ---------------------------------------------------------------------------


def load_today_stats() -> dict:
    final_today: Final = datetime.now().strftime("%Y-%m-%d")
    final_stats_path: Final = pathlib.Path(STATS_DIR) / f"{final_today}.json"
    if final_stats_path.exists():
        return json.loads(final_stats_path.read_text(encoding="utf-8"))
    return {}


def save_stats(stats: dict) -> None:
    final_today: Final = datetime.now().strftime("%Y-%m-%d")
    final_stats_path: Final = pathlib.Path(STATS_DIR) / f"{final_today}.json"
    final_stats_path.parent.mkdir(parents=True, exist_ok=True)
    final_stats_path.write_text(
        json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def rotate_stats() -> None:
    final_cutoff: Final = datetime.now() - timedelta(days=STATS_RETENTION_DAYS)
    final_stats_path: Final = pathlib.Path(STATS_DIR)
    if not final_stats_path.exists():
        return
    for stats_file in final_stats_path.glob("*.json"):
        try:
            final_file_date = datetime.strptime(stats_file.stem, "%Y-%m-%d")
            if final_file_date < final_cutoff:
                stats_file.unlink()
                logging.info("Rotated old stats file: %s", stats_file.name)
        except ValueError:
            continue


# ---------------------------------------------------------------------------
# File writing helpers
# ---------------------------------------------------------------------------


def write_metadata_and_content(
    filepath: pathlib.Path,
    metadata: dict[str, str],
    content: str,
) -> None:
    final_meta_lines: Final = [
        f"META_{key.upper()}: {value}" for key, value in metadata.items()
    ]
    final_meta_block: Final = "\n".join(final_meta_lines)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(
        final_meta_block + "\n\n" + content,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def load_config() -> dict:
    final_config_path: Final = pathlib.Path(CONFIG_FILE)
    if not final_config_path.exists():
        logging.info("No filters.yaml found; using defaults (no filters, no removals)")
        return {}
    final_raw: Final = final_config_path.read_text(encoding="utf-8")
    final_config: Final = yaml.safe_load(final_raw) or {}
    validate_config(final_config)
    return final_config


def process_file(filepath: pathlib.Path, config: dict, all_stats: dict) -> None:
    final_filename: Final = filepath.name
    logging.info("Processing: %s", final_filename)

    # Read and parse
    final_raw_text: Final = filepath.read_text(encoding="utf-8")
    final_metadata: dict[str, str]
    final_metadata, final_content_raw = split_metadata(final_raw_text)
    final_timestamp: Final = datetime.now().isoformat(timespec="seconds")

    # Initialize stats entry
    final_file_stats: Final[dict] = {
        "file": final_filename,
        "raw_archive": None,
        "cleaned_archive": None,
        "filtered_archive": None,
        "filters_checked": [],
        "filters_matched": [],
        "text_removals": {},
        "text_replacements": {},
        "general_cleaning": {},
        "outcome": None,
        "chars_before": len(final_content_raw),
        "chars_after": None,
    }

    # --- Run filters ---
    final_filters: Final = config.get("filters") or []
    skip_file: bool = False
    final_filter_reason: str = ""

    for filt in final_filters:
        final_reason = filt["reason"]
        final_action = filt.get("action", "skip")

        if not evaluate_match(filt["match"], final_metadata):
            final_file_stats["filters_checked"].append(final_reason)
            continue

        # Match block passed — check LLM if needed
        if "llm_check" in filt:
            final_llm_result = evaluate_llm_check(
                filt["llm_check"],
                final_metadata,
                final_content_raw,
            )
            if not final_llm_result:
                final_file_stats["filters_checked"].append(final_reason)
                continue

        # Filter matched
        final_file_stats["filters_checked"].append(final_reason)
        final_file_stats["filters_matched"].append(final_reason)

        if final_action == "notify":
            final_notify_config = filt["notify"]
            send_gotify_notification(
                title=final_notify_config["title"],
                message=f"{final_filename}\n\n{final_metadata.get('title', '')}",
                priority=final_notify_config["priority"],
            )
            continue

        # action == skip
        skip_file = True
        final_filter_reason = final_reason
        break

    if skip_file:
        # Write to filtered dir with reason
        final_filtered_metadata: Final = {**final_metadata, "filtered_reason": final_filter_reason}
        final_filtered_path: Final = pathlib.Path(FILTERED_DIR) / final_filename
        write_metadata_and_content(final_filtered_path, final_filtered_metadata, final_content_raw)

        # Archive raw
        final_raw_archive_path: Final = pathlib.Path(RAW_ARCHIVE_DIR) / final_filename
        final_raw_archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(filepath), str(final_raw_archive_path))

        final_file_stats["filtered_archive"] = str(final_filtered_path)
        final_file_stats["raw_archive"] = str(final_raw_archive_path)
        final_file_stats["outcome"] = "filtered"
        final_file_stats["chars_after"] = len(final_content_raw)
        all_stats[final_timestamp] = final_file_stats

        # Delete raw input
        filepath.unlink()
        logging.info("Filtered: %s (reason: %s)", final_filename, final_filter_reason)
        return

    # --- Apply cleaning ---
    final_gc_stats: Final[dict[str, dict]] = {}
    cleaned_text: str = apply_general_cleaning(
        final_content_raw,
        final_metadata,
        config,
        final_gc_stats,
    )
    final_file_stats["general_cleaning"] = final_gc_stats

    # YAML text removals
    final_removal_stats: Final[dict[str, dict]] = {}
    cleaned_text = apply_text_removals(cleaned_text, config, final_removal_stats)
    final_file_stats["text_removals"] = final_removal_stats

    # YAML text replacements
    final_replacement_stats: Final[dict[str, dict]] = {}
    cleaned_text = apply_text_replacements(cleaned_text, config, final_replacement_stats)
    final_file_stats["text_replacements"] = final_replacement_stats

    # Prepend and append author + title
    final_from_name: Final = final_metadata.get("from", "").strip()
    final_title: Final = final_metadata.get("title", "").strip()
    final_header: Final = (
        (f"{final_from_name}.\n" if final_from_name else "")
        + (f"{final_title}.\n" if final_title else "")
    )
    final_footer: Final = (
        "\n\n"
        + (f"{final_from_name}.\n" if final_from_name else "")
        + (f"{final_title}.\n" if final_title else "")
    )
    if final_header:
        cleaned_text = final_header + "\n" + cleaned_text
    if final_from_name or final_title:
        cleaned_text = cleaned_text.rstrip() + final_footer

    # Check too-big
    if len(cleaned_text) >= CHARACTER_LIMIT:
        final_toobig_reason: Final = f"Content too large: {len(cleaned_text)} chars (limit: {CHARACTER_LIMIT})"
        final_filtered_metadata_big: Final = {**final_metadata, "filtered_reason": final_toobig_reason}
        final_filtered_path_big: Final = pathlib.Path(FILTERED_DIR) / final_filename
        write_metadata_and_content(final_filtered_path_big, final_filtered_metadata_big, cleaned_text)

        final_raw_archive_path_big: Final = pathlib.Path(RAW_ARCHIVE_DIR) / final_filename
        final_raw_archive_path_big.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(filepath), str(final_raw_archive_path_big))

        final_file_stats["filtered_archive"] = str(final_filtered_path_big)
        final_file_stats["raw_archive"] = str(final_raw_archive_path_big)
        final_file_stats["outcome"] = "filtered_too_big"
        final_file_stats["chars_after"] = len(cleaned_text)
        all_stats[final_timestamp] = final_file_stats

        filepath.unlink()
        logging.info("Filtered (too big): %s (%d chars)", final_filename, len(cleaned_text))
        send_gotify_notification(
            "Skipping large text-to-speech content",
            f"{final_filename}: {len(cleaned_text)} chars exceeds {CHARACTER_LIMIT} limit.",
        )
        return

    # Check empty
    if not cleaned_text.strip():
        final_empty_reason: Final = "Content empty after cleaning"
        final_filtered_metadata_empty: Final = {**final_metadata, "filtered_reason": final_empty_reason}
        final_filtered_path_empty: Final = pathlib.Path(FILTERED_DIR) / final_filename
        write_metadata_and_content(final_filtered_path_empty, final_filtered_metadata_empty, "")

        final_raw_archive_path_empty: Final = pathlib.Path(RAW_ARCHIVE_DIR) / final_filename
        final_raw_archive_path_empty.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(filepath), str(final_raw_archive_path_empty))

        final_file_stats["filtered_archive"] = str(final_filtered_path_empty)
        final_file_stats["raw_archive"] = str(final_raw_archive_path_empty)
        final_file_stats["outcome"] = "filtered_empty"
        final_file_stats["chars_after"] = 0
        all_stats[final_timestamp] = final_file_stats

        filepath.unlink()
        logging.info("Filtered (empty after cleaning): %s", final_filename)
        send_gotify_notification(
            "Skipping empty text-to-speech content",
            f"{final_filename}: empty after cleaning.",
        )
        return

    # --- Write outputs ---
    # Write cleaned output
    final_cleaned_path: Final = pathlib.Path(CLEANED_OUTPUT_DIR) / final_filename
    write_metadata_and_content(final_cleaned_path, final_metadata, cleaned_text)

    # Archive raw
    final_raw_archive_final: Final = pathlib.Path(RAW_ARCHIVE_DIR) / final_filename
    final_raw_archive_final.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(filepath), str(final_raw_archive_final))

    # Archive cleaned
    final_cleaned_archive: Final = pathlib.Path(CLEANED_ARCHIVE_DIR) / final_filename
    final_cleaned_archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(final_cleaned_path), str(final_cleaned_archive))

    final_file_stats["raw_archive"] = str(final_raw_archive_final)
    final_file_stats["cleaned_archive"] = str(final_cleaned_archive)
    final_file_stats["outcome"] = "cleaned"
    final_file_stats["chars_after"] = len(cleaned_text)
    all_stats[final_timestamp] = final_file_stats

    # Delete raw input (last step — only after all writes succeeded)
    filepath.unlink()
    logging.info(
        "Cleaned: %s (%d -> %d chars)",
        final_filename,
        len(final_content_raw),
        len(cleaned_text),
    )


def process_files() -> None:
    # Ensure directories exist
    for dir_path in (RAW_INPUT_DIR, RAW_ARCHIVE_DIR, CLEANED_OUTPUT_DIR, CLEANED_ARCHIVE_DIR, FILTERED_DIR, STATS_DIR):
        pathlib.Path(dir_path).mkdir(parents=True, exist_ok=True)

    # Rotate old stats
    rotate_stats()

    # Load config
    final_config: Final = load_config()

    # Validate rule ordering
    final_filters: Final = final_config.get("filters") or []
    final_ordering_errors: Final = validate_rule_ordering(final_filters)
    final_shadowed_matches: Final[list[dict]] = []

    if final_ordering_errors:
        final_error_msg: Final = "Filter rule ordering issues:\n" + "\n".join(final_ordering_errors)
        logging.error(final_error_msg)
        send_gotify_notification(
            "prepare_text.py: filter rule ordering error",
            final_error_msg + "\n\nAffected files will be left in text-input-raw/ until this is fixed.",
            priority=9,
        )
        # Collect the match blocks from skip rules that shadow later rules
        for error in final_ordering_errors:
            # Extract the index of the skip rule from the error message
            final_idx_str = error.split("filters[")[1].split("]")[0]
            final_idx = int(final_idx_str)
            final_shadowed_matches.append(final_filters[final_idx]["match"])

    # Load today's stats (append to existing if re-run)
    final_all_stats: Final = load_today_stats()

    # Process files
    final_txt_files: Final = sorted(pathlib.Path(RAW_INPUT_DIR).glob("*.txt"))
    for txt_file in final_txt_files:
        # Check if this file matches a shadowed skip rule
        if final_shadowed_matches:
            final_raw_text_check = txt_file.read_text(encoding="utf-8")
            final_meta_check = split_metadata(final_raw_text_check)[0]
            final_is_shadowed = any(
                evaluate_match(match, final_meta_check) for match in final_shadowed_matches
            )
            if final_is_shadowed:
                logging.warning(
                    "Skipping %s due to rule ordering conflict (left in raw)",
                    txt_file.name,
                )
                continue

        try:
            process_file(txt_file, final_config, final_all_stats)
        except Exception:
            logging.exception("Error processing %s — leaving in raw for retry", txt_file.name)
            continue

    save_stats(final_all_stats)


if __name__ == "__main__":
    process_files()
