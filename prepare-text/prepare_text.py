"""Text preparation pipeline: filtering, cleaning, and transformation.

Reads raw text files from text-input-raw/, applies filters and cleaning rules
from filters.yaml, and writes cleaned output to text-input-cleaned/ for TTS.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import shutil
from datetime import UTC, datetime, timedelta

import markdown
import yaml
from bs4 import BeautifulSoup
from podcast_shared import get_gemini_client, send_gotify_notification, split_metadata

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
LLM_MODEL = "gemini-3.1-flash-lite-preview"


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------

VALID_MATCH_FIELDS = frozenset(
    {"from", "title", "source_url", "source_kind", "source_name", "intake_type"},
)
VALID_MATCH_OPERATORS = frozenset({"contains", "not_contains"})
VALID_ACTIONS = frozenset({"skip", "notify"})
VALID_FLAGS = frozenset({"ignorecase", "multiline", "dotall"})
CLEANING_STEPS = (
    "beehiiv_plaintext_conversion",
    "beehiiv_emphasis_removal",
    "url_removal",
    "legal_bracket_unwrap",
    "triple_dash_removal",
    "empty_bracket_removal",
    "whitespace_collapse",
    "unsubscribe_removal",
    "view_online_removal",
    "substack_refs_removal",
    "standalone_at_removal",
    "end_of_line_punctuation",
)
VALID_CLEANING_KEYS = frozenset(CLEANING_STEPS)


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


def validate_match_block(match_block: dict, context: str) -> None:
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


def validate_config(config: dict) -> None:
    """Validate the full filters.yaml configuration structure.

    Raises:
        ValueError: If the config contains invalid keys, filters, or patterns.

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
        if "llm_check" in filt and not isinstance(filt["llm_check"], str):
            msg = f"{ctx}: 'llm_check' must be a string"
            raise ValueError(msg)

    # Validate general_cleaning
    gc = config.get("general_cleaning") or {}
    for key, value in gc.items():
        if key == "overrides":
            if not isinstance(value, list):
                msg = "general_cleaning.overrides must be a list"
                raise ValueError(msg)
            for oidx, override in enumerate(value):
                octx = f"general_cleaning.overrides[{oidx}]"
                if "match" not in override:
                    msg = f"{octx}: 'match' is required"
                    raise ValueError(msg)
                validate_match_block(override["match"], octx)
                for okey in override:
                    if okey != "match" and okey not in VALID_CLEANING_KEYS:
                        msg = f"{octx}: unknown key {okey!r}"
                        raise ValueError(msg)
        elif key not in VALID_CLEANING_KEYS:
            msg = f"general_cleaning: unknown key {key!r}"
            raise ValueError(msg)
        elif not isinstance(value, bool):
            msg = f"general_cleaning.{key}: must be a boolean"
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
        rflags = parse_flags(removal.get("flags"))
        try:
            re.compile(removal["pattern"], rflags)
        except re.error as exc:
            msg = f"{rctx}: invalid regex: {exc}"
            raise ValueError(msg) from exc

    # Validate text_replacements
    for idx, repl in enumerate(config.get("text_replacements") or []):
        pctx = f"text_replacements[{idx}]"
        if "pattern" not in repl:
            msg = f"{pctx}: 'pattern' is required"
            raise ValueError(msg)
        if "replacement" not in repl:
            msg = f"{pctx}: 'replacement' is required"
            raise ValueError(msg)
        if "reason" not in repl:
            msg = f"{pctx}: 'reason' is required"
            raise ValueError(msg)
        pflags = parse_flags(repl.get("flags"))
        try:
            re.compile(repl["pattern"], pflags)
        except re.error as exc:
            msg = f"{pctx}: invalid regex: {exc}"
            raise ValueError(msg) from exc


def validate_rule_ordering(filters: list[dict]) -> list[str]:
    """Check for skip rules that shadow later rules with overlapping match criteria.

    Returns:
        List of error messages (empty if no problems).

    """
    errors: list[str] = []
    for i, rule_a in enumerate(filters):
        if rule_a.get("action", "skip") != "skip":
            continue
        match_a = rule_a["match"]
        for j in range(i + 1, len(filters)):
            rule_b = filters[j]
            match_b = rule_b["match"]
            # Check if match_b is a subset of or identical to match_a
            # (meaning everything match_b matches, match_a also matches)
            if _match_is_subset(subset=match_b, superset=match_a):
                errors.append(
                    f"filters[{i}] (skip, reason: {rule_a['reason']!r}) shadows "
                    f"filters[{j}] (reason: {rule_b['reason']!r}) — "
                    f"the later rule will never fire. Reorder or adjust match criteria.",
                )
    return errors


def _match_is_subset(subset: dict, superset: dict) -> bool:
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


def evaluate_match(match_block: dict, metadata: dict[str, str]) -> bool:
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
    """Run a Gemini LLM check and return whether the content matches.

    Returns:
        True if the LLM confirms the check, False on failure or negative result.

    """
    title = metadata.get("title", "")
    full_prompt = f"{prompt_template}\n\nTitle: {title}\n\nContent:\n{content}"
    try:
        client = get_gemini_client()
        response = client.models.generate_content(
            model=LLM_MODEL,
            contents=full_prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "object",
                    "properties": {"result": {"type": "boolean"}},
                    "required": ["result"],
                },
            },
        )
        if response.text is None:
            logging.warning("Gemini returned no text for LLM check")
            return False
        parsed = json.loads(response.text)
        return bool(parsed.get("result", False))
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


def apply_general_cleaning(
    text: str,
    metadata: dict[str, str],
    config: dict,
    stats: dict[str, dict],
) -> str:
    """Apply all built-in cleaning steps (URL removal, whitespace, etc.).

    Returns:
        The cleaned text.

    """
    gc_config = config.get("general_cleaning") or {}
    overrides = gc_config.get("overrides") or []

    def is_enabled(key: str) -> bool:
        # Check per-source overrides first
        for override in overrides:
            if evaluate_match(override["match"], metadata) and key in override:
                return bool(override[key])
        # Then global config
        if key in gc_config:
            return bool(gc_config[key])
        # All cleaning steps are enabled by default
        return True

    def count_and_sub(pattern: str, replacement: str, text: str, key: str, flags: int = 0) -> str:
        matches = len(re.findall(pattern, text, flags=flags))
        if matches > 0:
            stats[key] = {"matches": matches}
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
        before_brackets = result
        result = re.sub(r"\[\]", "", result)
        result = re.sub(r"\(\)", "", result)
        result = result.replace("<>", "")
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


def apply_text_replacements(text: str, config: dict, stats: dict[str, dict]) -> str:
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


def load_today_stats() -> dict:
    """Load today's stats JSON file, or return an empty dict.

    Returns:
        The stats dict for today.

    """
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    stats_path = pathlib.Path(STATS_DIR) / f"{today}.json"
    if stats_path.exists():
        return json.loads(stats_path.read_text(encoding="utf-8"))
    return {}


def save_stats(stats: dict) -> None:
    """Write the stats dict to today's JSON file."""
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    stats_path = pathlib.Path(STATS_DIR) / f"{today}.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
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
    filepath.write_text(
        meta_block + "\n\n" + content,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load and validate filters.yaml, returning an empty dict if absent.

    Returns:
        The parsed and validated config dict.

    """
    config_path = pathlib.Path(CONFIG_FILE)
    if not config_path.exists():
        logging.info("No filters.yaml found; using defaults (no filters, no removals)")
        return {}
    raw = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(raw) or {}
    validate_config(config)
    return config


def process_file(filepath: pathlib.Path, config: dict, all_stats: dict) -> None:
    """Filter, clean, and write a single raw text file."""
    filename = filepath.name
    logging.info("Processing: %s", filename)

    # Read and parse
    raw_text = filepath.read_text(encoding="utf-8")
    metadata: dict[str, str]
    metadata, content_raw = split_metadata(raw_text)
    timestamp = datetime.now(tz=UTC).isoformat(timespec="seconds")

    # Initialize stats entry
    file_stats: dict = {
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

        if action == "notify":
            notify_config = filt["notify"]
            send_gotify_notification(
                title=notify_config["title"],
                message=f"{filename}\n\n{metadata.get('title', '')}",
                priority=notify_config["priority"],
            )
            continue

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
        shutil.copy2(str(filepath), str(raw_archive_path))

        file_stats["filtered_archive"] = str(filtered_path)
        file_stats["raw_archive"] = str(raw_archive_path)
        file_stats["outcome"] = "filtered"
        file_stats["chars_after"] = len(content_raw)
        all_stats[timestamp] = file_stats

        # Delete raw input
        filepath.unlink()
        logging.info("Filtered: %s (reason: %s)", filename, filter_reason)
        return

    # --- Apply cleaning ---
    gc_stats: dict[str, dict] = {}
    cleaned_text: str = apply_general_cleaning(
        content_raw,
        metadata,
        config,
        gc_stats,
    )
    file_stats["general_cleaning"] = gc_stats

    # YAML text removals
    removal_stats: dict[str, dict] = {}
    cleaned_text = apply_text_removals(cleaned_text, config, removal_stats)
    file_stats["text_removals"] = removal_stats

    # YAML text replacements
    replacement_stats: dict[str, dict] = {}
    cleaned_text = apply_text_replacements(cleaned_text, config, replacement_stats)
    file_stats["text_replacements"] = replacement_stats

    # Check empty (before adding header/footer, which would mask empty content)
    if not cleaned_text.strip():
        empty_reason = "Content empty after cleaning"
        filtered_metadata_empty = {**metadata, "filtered_reason": empty_reason}
        filtered_path_empty = pathlib.Path(FILTERED_DIR) / filename
        write_metadata_and_content(filtered_path_empty, filtered_metadata_empty, "")

        raw_archive_path_empty = pathlib.Path(RAW_ARCHIVE_DIR) / filename
        raw_archive_path_empty.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(filepath), str(raw_archive_path_empty))

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

    # Prepend and append author + title
    from_name = metadata.get("from", "").strip()
    title = metadata.get("title", "").strip()
    header = (f"{from_name}.\n" if from_name else "") + (f"{title}.\n" if title else "")
    footer = "\n\n" + (f"{from_name}.\n" if from_name else "") + (f"{title}.\n" if title else "")
    if header:
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
        shutil.copy2(str(filepath), str(raw_archive_path_big))

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
    shutil.copy2(str(filepath), str(raw_archive_final))

    # Archive cleaned
    cleaned_archive = pathlib.Path(CLEANED_ARCHIVE_DIR) / filename
    cleaned_archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(cleaned_path), str(cleaned_archive))

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
    shadowed_matches: list[dict] = []

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
