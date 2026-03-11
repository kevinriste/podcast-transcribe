# pyright: reportExplicitAny=false, reportAny=false, reportPrivateUsage=false, reportUnusedCallResult=false
"""Unit tests for prepare_text.py."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import requests

if TYPE_CHECKING:
    from collections.abc import Mapping

import pytest
from pyrsistent import PMap, pmap, pvector

import prepare_text
from prepare_text import (
    _match_is_subset,
    apply_general_cleaning,
    apply_text_removals,
    apply_text_replacements,
    clean_beehiiv_emphasis,
    clean_beehiiv_to_plaintext,
    evaluate_llm_check,
    evaluate_match,
    parse_flags,
    send_gotify_notification,
    split_metadata,
    validate_config,
    validate_match_block,
    validate_rule_ordering,
)

type YamlDict = PMap[str, Any]


# ---------------------------------------------------------------------------
# split_metadata
# ---------------------------------------------------------------------------


class TestSplitMetadata:
    def test_basic_metadata(self) -> None:
        raw = "META_FROM: Author Name\nMETA_TITLE: Some Title\n\nBody text here."
        metadata, content = split_metadata(raw)
        assert metadata == pmap({"from": "Author Name", "title": "Some Title"})
        assert content == "Body text here."

    def test_no_metadata(self) -> None:
        raw = "Just plain text without metadata."
        metadata, content = split_metadata(raw)
        assert metadata == pmap()
        assert content == raw

    def test_multiline_metadata_value(self) -> None:
        raw = "META_FROM: Author Name\n  continued value\nMETA_TITLE: Title\n\nBody."
        metadata, content = split_metadata(raw)
        assert metadata["from"] == "Author Name continued value"
        assert metadata["title"] == "Title"
        assert content == "Body."

    def test_empty_content(self) -> None:
        raw = "META_FROM: Author\nMETA_TITLE: Title\n\n"
        metadata, content = split_metadata(raw)
        assert metadata == pmap({"from": "Author", "title": "Title"})
        assert not content

    def test_tab_continuation(self) -> None:
        raw = "META_FROM: Author\n\tcontinued\n\nBody."
        metadata, _content = split_metadata(raw)
        assert metadata["from"] == "Author continued"

    def test_multiple_meta_fields(self) -> None:
        raw = (
            "META_FROM: Author\n"
            "META_TITLE: Title\n"
            "META_SOURCE_URL: https://example.com\n"
            "META_SOURCE_KIND: substack\n"
            "META_SOURCE_NAME: Blog\n"
            "META_INTAKE_TYPE: email\n"
            "\n"
            "Content."
        )
        metadata, content = split_metadata(raw)
        assert len(metadata) == 6
        assert metadata["source_url"] == "https://example.com"
        assert metadata["intake_type"] == "email"
        assert content == "Content."


# ---------------------------------------------------------------------------
# parse_flags
# ---------------------------------------------------------------------------


class TestParseFlags:
    def test_none_returns_zero(self) -> None:
        assert parse_flags(None) == 0

    def test_single_string(self) -> None:
        assert parse_flags("ignorecase") == re.IGNORECASE

    def test_list_of_flags(self) -> None:
        result = parse_flags(["ignorecase", "multiline", "dotall"])
        assert result == (re.IGNORECASE | re.MULTILINE | re.DOTALL)

    def test_invalid_flag_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid flag"):
            parse_flags("nonexistent")

    def test_single_dotall(self) -> None:
        assert parse_flags("dotall") == re.DOTALL


# ---------------------------------------------------------------------------
# validate_match_block
# ---------------------------------------------------------------------------


class TestValidateMatchBlock:
    def test_valid_block(self) -> None:
        validate_match_block(pmap({"from": pmap({"contains": "Author"})}), "test")

    def test_empty_block_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty dict"):
            validate_match_block(pmap(), "test")

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown match field"):
            validate_match_block(pmap({"bogus": pmap({"contains": "x"})}), "test")

    def test_unknown_operator_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown operator"):
            validate_match_block(pmap({"from": pmap({"starts_with": "x"})}), "test")

    def test_operators_must_be_dict(self) -> None:
        with pytest.raises(ValueError, match="must be a dict with operators"):
            validate_match_block(pmap({"from": "Author"}), "test")

    def test_multiple_fields(self) -> None:
        validate_match_block(
            pmap({"from": pmap({"contains": "Author"}), "title": pmap({"not_contains": "Draft"})}),
            "test",
        )


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_empty_config(self) -> None:
        validate_config(pmap())

    def test_unknown_top_level_key_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown top-level key"):
            validate_config(pmap({"bogus": True}))

    def test_filter_missing_match_raises(self) -> None:
        with pytest.raises(ValueError, match="'match' is required"):
            validate_config(pmap({"filters": pvector([pmap({"reason": "test"})])}))

    def test_filter_missing_reason_raises(self) -> None:
        with pytest.raises(ValueError, match="'reason' is required"):
            validate_config(pmap({"filters": pvector([pmap({"match": pmap({"from": pmap({"contains": "x"})})})])}))

    def test_invalid_action_raises(self) -> None:
        config: YamlDict = pmap(
            {
                "filters": pvector(
                    [
                        pmap({"match": pmap({"from": pmap({"contains": "x"})}), "reason": "test", "action": "delete"}),
                    ]
                ),
            }
        )
        with pytest.raises(ValueError, match="invalid action"):
            validate_config(config)

    def test_notify_without_notify_block_raises(self) -> None:
        config: YamlDict = pmap(
            {
                "filters": pvector(
                    [
                        pmap({"match": pmap({"from": pmap({"contains": "x"})}), "reason": "test", "action": "notify"}),
                    ]
                ),
            }
        )
        with pytest.raises(ValueError, match="requires a 'notify' block"):
            validate_config(config)

    def test_notify_block_missing_priority_raises(self) -> None:
        config: YamlDict = pmap(
            {
                "filters": pvector(
                    [
                        pmap(
                            {
                                "match": pmap({"from": pmap({"contains": "x"})}),
                                "reason": "test",
                                "action": "notify",
                                "notify": pmap({"title": "Alert"}),
                            }
                        ),
                    ]
                ),
            }
        )
        with pytest.raises(ValueError, match="requires 'priority'"):
            validate_config(config)

    def test_valid_full_config(self) -> None:
        config: YamlDict = pmap(
            {
                "filters": pvector(
                    [
                        pmap(
                            {
                                "match": pmap({"from": pmap({"contains": "Author"})}),
                                "reason": "test filter",
                                "action": "notify",
                                "notify": pmap({"title": "Alert", "priority": 5}),
                            }
                        ),
                    ]
                ),
                "general_cleaning": pmap({"url_removal": False}),
                "text_removals": pvector(
                    [
                        pmap({"pattern": "^Ad$", "flags": "multiline", "reason": "ads"}),
                    ]
                ),
                "text_replacements": pvector(
                    [
                        pmap(
                            {
                                "pattern": "Keynesian",
                                "replacement": "Cainzeean",
                                "flags": "ignorecase",
                                "reason": "pronunciation",
                            }
                        ),
                    ]
                ),
            }
        )
        validate_config(config)

    def test_invalid_regex_in_removals_raises(self) -> None:
        config: YamlDict = pmap(
            {
                "text_removals": pvector([pmap({"pattern": "[invalid", "reason": "broken"})]),
            }
        )
        with pytest.raises(ValueError, match="invalid regex"):
            validate_config(config)

    def test_invalid_regex_in_replacements_raises(self) -> None:
        config: YamlDict = pmap(
            {
                "text_replacements": pvector([pmap({"pattern": "[invalid", "replacement": "x", "reason": "broken"})]),
            }
        )
        with pytest.raises(ValueError, match="invalid regex"):
            validate_config(config)

    def test_general_cleaning_unknown_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown key"):
            validate_config(pmap({"general_cleaning": pmap({"bogus_setting": True})}))

    def test_general_cleaning_non_bool_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a boolean"):
            validate_config(pmap({"general_cleaning": pmap({"url_removal": "yes"})}))


# ---------------------------------------------------------------------------
# validate_rule_ordering
# ---------------------------------------------------------------------------


class TestValidateRuleOrdering:
    def test_no_shadowing(self) -> None:
        filters = pvector(
            [
                pmap({"match": pmap({"from": pmap({"contains": "Author A"})}), "action": "skip", "reason": "skip A"}),
                pmap({"match": pmap({"from": pmap({"contains": "Author B"})}), "action": "skip", "reason": "skip B"}),
            ]
        )
        assert validate_rule_ordering(filters) == ()

    def test_shadow_detected(self) -> None:
        filters = pvector(
            [
                pmap(
                    {
                        "match": pmap({"source_url": pmap({"contains": "example"})}),
                        "action": "skip",
                        "reason": "skip all example",
                    }
                ),
                pmap(
                    {
                        "match": pmap(
                            {
                                "source_url": pmap({"contains": "example"}),
                                "title": pmap({"contains": "special"}),
                            }
                        ),
                        "action": "notify",
                        "reason": "notify special example",
                    }
                ),
            ]
        )
        errors = validate_rule_ordering(filters)
        assert len(errors) == 1
        assert "shadows" in errors[0]

    def test_notify_before_skip_no_shadow(self) -> None:
        filters = pvector(
            [
                pmap(
                    {
                        "match": pmap({"source_url": pmap({"contains": "example"})}),
                        "action": "notify",
                        "reason": "notify example",
                    }
                ),
                pmap(
                    {
                        "match": pmap({"source_url": pmap({"contains": "example"})}),
                        "action": "skip",
                        "reason": "skip example",
                    }
                ),
            ]
        )
        assert validate_rule_ordering(filters) == ()


# ---------------------------------------------------------------------------
# _match_is_subset
# ---------------------------------------------------------------------------


class TestMatchIsSubset:
    def test_identical_matches(self) -> None:
        match: Mapping[str, Any] = pmap({"from": pmap({"contains": "Author"})})
        assert _match_is_subset(subset=match, superset=match) is True

    def test_subset_has_extra_constraint(self) -> None:
        superset: Mapping[str, Any] = pmap({"from": pmap({"contains": "Author"})})
        subset: Mapping[str, Any] = pmap({"from": pmap({"contains": "Author"}), "title": pmap({"contains": "News"})})
        assert _match_is_subset(subset=subset, superset=superset) is True

    def test_not_subset_different_value(self) -> None:
        superset: Mapping[str, Any] = pmap({"from": pmap({"contains": "Author A"})})
        subset: Mapping[str, Any] = pmap({"from": pmap({"contains": "Author B"})})
        assert _match_is_subset(subset=subset, superset=superset) is False

    def test_not_subset_missing_field(self) -> None:
        superset: Mapping[str, Any] = pmap(
            {
                "from": pmap({"contains": "Author"}),
                "title": pmap({"contains": "News"}),
            }
        )
        subset: Mapping[str, Any] = pmap({"from": pmap({"contains": "Author"})})
        assert _match_is_subset(subset=subset, superset=superset) is False

    def test_case_insensitive_comparison(self) -> None:
        superset: Mapping[str, Any] = pmap({"from": pmap({"contains": "AUTHOR"})})
        subset: Mapping[str, Any] = pmap({"from": pmap({"contains": "author"})})
        assert _match_is_subset(subset=subset, superset=superset) is True


# ---------------------------------------------------------------------------
# evaluate_match
# ---------------------------------------------------------------------------


class TestEvaluateMatch:
    def test_contains_match(self) -> None:
        match: Mapping[str, Any] = pmap({"from": pmap({"contains": "author"})})
        metadata: Mapping[str, str] = pmap({"from": "The Author Name", "title": "Article"})
        assert evaluate_match(match, metadata) is True

    def test_contains_no_match(self) -> None:
        match: Mapping[str, Any] = pmap({"from": pmap({"contains": "nobody"})})
        metadata: Mapping[str, str] = pmap({"from": "The Author Name"})
        assert evaluate_match(match, metadata) is False

    def test_not_contains_match(self) -> None:
        match: Mapping[str, Any] = pmap({"title": pmap({"not_contains": "draft"})})
        metadata: Mapping[str, str] = pmap({"title": "Published Article"})
        assert evaluate_match(match, metadata) is True

    def test_not_contains_no_match(self) -> None:
        match: Mapping[str, Any] = pmap({"title": pmap({"not_contains": "draft"})})
        metadata: Mapping[str, str] = pmap({"title": "Draft Article"})
        assert evaluate_match(match, metadata) is False

    def test_multiple_conditions_all_must_match(self) -> None:
        match: Mapping[str, Any] = pmap({"from": pmap({"contains": "author"}), "title": pmap({"contains": "news"})})
        metadata: Mapping[str, str] = pmap({"from": "Author Name", "title": "Breaking News"})
        assert evaluate_match(match, metadata) is True

    def test_multiple_conditions_one_fails(self) -> None:
        match: Mapping[str, Any] = pmap({"from": pmap({"contains": "author"}), "title": pmap({"contains": "sports"})})
        metadata: Mapping[str, str] = pmap({"from": "Author Name", "title": "Breaking News"})
        assert evaluate_match(match, metadata) is False

    def test_missing_metadata_field(self) -> None:
        match: Mapping[str, Any] = pmap({"source_url": pmap({"contains": "example.com"})})
        metadata: Mapping[str, str] = pmap({"from": "Author"})
        assert evaluate_match(match, metadata) is False

    def test_case_insensitive(self) -> None:
        match: Mapping[str, Any] = pmap({"from": pmap({"contains": "AUTHOR"})})
        metadata: Mapping[str, str] = pmap({"from": "author name"})
        assert evaluate_match(match, metadata) is True


# ---------------------------------------------------------------------------
# clean_beehiiv_to_plaintext
# ---------------------------------------------------------------------------


class TestCleanBeehiivToPlaintext:
    def test_basic_markdown(self) -> None:
        result = clean_beehiiv_to_plaintext("**bold** and _italic_")
        assert "bold" in result
        assert "italic" in result
        assert "**" not in result
        assert "_" not in result or result.count("_") == 0

    def test_plain_text_passthrough(self) -> None:
        result = clean_beehiiv_to_plaintext("Just plain text.")
        assert result.strip() == "Just plain text."


# ---------------------------------------------------------------------------
# clean_beehiiv_emphasis
# ---------------------------------------------------------------------------


class TestCleanBeehiivEmphasis:
    def test_double_underscore(self) -> None:
        assert clean_beehiiv_emphasis("__bold text__") == "bold text"

    def test_single_underscore(self) -> None:
        assert clean_beehiiv_emphasis("_italic text_") == "italic text"

    def test_mixed(self) -> None:
        result = clean_beehiiv_emphasis("__bold__ and _italic_")
        assert result == "bold and italic"

    def test_no_emphasis(self) -> None:
        assert clean_beehiiv_emphasis("no emphasis here") == "no emphasis here"


# ---------------------------------------------------------------------------
# apply_general_cleaning
# ---------------------------------------------------------------------------


class TestApplyGeneralCleaning:
    def test_url_removal(self) -> None:
        text = "Visit https://example.com/page for more info."
        result, stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert "https://example.com" not in result
        assert "url_removal" in stats

    def test_whitespace_collapse(self) -> None:
        text = "too    many    spaces"
        result, stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert "too many spaces" in result
        assert "whitespace_collapse" in stats

    def test_unsubscribe_removal(self) -> None:
        text = "Article content.\n\nUnsubscribe from this list."
        result, _stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert "Unsubscribe" not in result

    def test_legal_bracket_unwrap(self) -> None:
        text = "[t]he court ruled that [T]he defendant"
        result, _stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert "[t]" not in result
        assert "[T]" not in result
        assert "the court" in result

    def test_disabled_cleaning(self) -> None:
        text = "Visit https://example.com for info."
        config: Mapping[str, Any] = pmap({"general_cleaning": pmap({"url_removal": False})})
        result, _stats = apply_general_cleaning(text, pmap(), config, pmap())
        assert "https://example.com" in result

    def test_end_of_line_punctuation(self) -> None:
        text = "Line without period\nNext line"
        result, _stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert "period.\n" in result

    def test_triple_dash_removal(self) -> None:
        text = "Before --- After"
        result, _stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert "---" not in result

    def test_empty_bracket_removal(self) -> None:
        text = "Text with [] and () and <> markers."
        result, _stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert "[]" not in result
        assert "()" not in result
        assert "<>" not in result

    def test_view_online_removal(self) -> None:
        text = "View this post on the web at \n\nArticle starts here."
        result, _stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert "View this post on the web" not in result

    def test_standalone_at_removal(self) -> None:
        text = "Before\n @ \nAfter"
        result, _stats = apply_general_cleaning(text, pmap(), pmap(), pmap())
        assert " @ " not in result

    def test_override_for_specific_source(self) -> None:
        text = "Visit https://example.com for info."
        config: Mapping[str, Any] = pmap(
            {
                "general_cleaning": pmap(
                    {
                        "overrides": pvector(
                            [
                                pmap({"match": pmap({"from": pmap({"contains": "author"})}), "url_removal": False}),
                            ]
                        ),
                    }
                ),
            }
        )
        result, _stats = apply_general_cleaning(text, pmap({"from": "Author Name"}), config, pmap())
        assert "https://example.com" in result


# ---------------------------------------------------------------------------
# apply_text_removals
# ---------------------------------------------------------------------------


class TestApplyTextRemovals:
    def test_basic_removal(self) -> None:
        config: Mapping[str, Any] = pmap(
            {
                "text_removals": pvector([pmap({"pattern": "^Advertisement$", "flags": "multiline", "reason": "ads"})]),
            }
        )
        text = "Content\nAdvertisement\nMore content"
        result, stats = apply_text_removals(text, config, pmap())
        assert "Advertisement" not in result
        assert "Content" in result
        assert stats["ads"]["matches"] == 1

    def test_no_match(self) -> None:
        config: Mapping[str, Any] = pmap(
            {
                "text_removals": pvector([pmap({"pattern": "NONEXISTENT", "reason": "missing"})]),
            }
        )
        text = "Normal content."
        result, stats = apply_text_removals(text, config, pmap())
        assert result == text
        assert len(stats) == 0

    def test_multiple_removals(self) -> None:
        config: Mapping[str, Any] = pmap(
            {
                "text_removals": pvector(
                    [
                        pmap({"pattern": "^Ad$", "flags": "multiline", "reason": "ads"}),
                        pmap({"pattern": "^Sponsored$", "flags": "multiline", "reason": "sponsored"}),
                    ]
                ),
            }
        )
        text = "Content\nAd\nMore\nSponsored\nEnd"
        result, stats = apply_text_removals(text, config, pmap())
        assert "Ad" not in result.split("\n")
        assert "Sponsored" not in result.split("\n")
        assert stats["ads"]["matches"] == 1
        assert stats["sponsored"]["matches"] == 1

    def test_dotall_flag(self) -> None:
        config: Mapping[str, Any] = pmap(
            {
                "text_removals": pvector([pmap({"pattern": "START.*END", "flags": "dotall", "reason": "block"})]),
            }
        )
        text = "Before START\nmiddle\nEND After"
        result, _stats = apply_text_removals(text, config, pmap())
        assert "middle" not in result
        assert "Before" in result
        assert "After" in result

    def test_empty_removals_list(self) -> None:
        config: Mapping[str, Any] = pmap()
        text = "Content."
        result, _stats = apply_text_removals(text, config, pmap())
        assert result == text


# ---------------------------------------------------------------------------
# apply_text_replacements
# ---------------------------------------------------------------------------


class TestApplyTextReplacements:
    def test_basic_replacement(self) -> None:
        config: Mapping[str, Any] = pmap(
            {
                "text_replacements": pvector(
                    [
                        pmap(
                            {
                                "pattern": "Keynesian",
                                "replacement": "Cainzeean",
                                "flags": "ignorecase",
                                "reason": "pronunciation",
                            }
                        ),
                    ]
                ),
            }
        )
        text = "The Keynesian school of thought"
        result, stats = apply_text_replacements(text, config, pmap())
        assert "Cainzeean" in result
        assert "Keynesian" not in result
        assert stats["pronunciation"]["matches"] == 1

    def test_no_match(self) -> None:
        config: Mapping[str, Any] = pmap(
            {
                "text_replacements": pvector(
                    [
                        pmap({"pattern": "NONEXISTENT", "replacement": "X", "reason": "test"}),
                    ]
                ),
            }
        )
        text = "Normal content."
        result, stats = apply_text_replacements(text, config, pmap())
        assert result == text
        assert len(stats) == 0

    def test_multiple_occurrences(self) -> None:
        config: Mapping[str, Any] = pmap(
            {
                "text_replacements": pvector(
                    [
                        pmap({"pattern": "colour", "replacement": "color", "reason": "spelling"}),
                    ]
                ),
            }
        )
        text = "The colour of the colour wheel"
        result, stats = apply_text_replacements(text, config, pmap())
        assert result == "The color of the color wheel"
        assert stats["spelling"]["matches"] == 2

    def test_empty_replacements_list(self) -> None:
        config: Mapping[str, Any] = pmap()
        text = "Content."
        result, _stats = apply_text_replacements(text, config, pmap())
        assert result == text


# ---------------------------------------------------------------------------
# evaluate_llm_check
# ---------------------------------------------------------------------------


class TestEvaluateLlmCheck:
    def test_returns_true_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail-closed: if the LLM call throws, treat the filter as matched."""

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            msg = "API down"
            raise ConnectionError(msg)

        monkeypatch.setattr(prepare_text, "get_gemini_client", _boom)
        result = evaluate_llm_check("Is this about sports?", pmap({"title": "Test"}), "content")
        assert result is True

    def test_returns_true_when_result_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail-closed: if LLM returns JSON without 'result' key, treat as matched."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"answer": true}'  # No "result" key
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(prepare_text, "get_gemini_client", lambda: mock_client)

        result = evaluate_llm_check("prompt", pmap({"title": "Test"}), "content")
        assert result is True

    def test_returns_true_when_llm_says_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"result": true}'
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(prepare_text, "get_gemini_client", lambda: mock_client)

        result = evaluate_llm_check("prompt", pmap({"title": "Test"}), "content")
        assert result is True

    def test_returns_false_when_llm_says_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"result": false}'
        mock_client.models.generate_content.return_value = mock_response
        monkeypatch.setattr(prepare_text, "get_gemini_client", lambda: mock_client)

        result = evaluate_llm_check("prompt", pmap({"title": "Test"}), "content")
        assert result is False


# ---------------------------------------------------------------------------
# send_gotify_notification
# ---------------------------------------------------------------------------


class TestSendGotifyNotification:
    def test_does_not_raise_when_post_throws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOTIFY_SERVER", "https://gotify.example.com")
        monkeypatch.setenv("GOTIFY_TOKEN", "test-token")

        mock_post = MagicMock(side_effect=requests.ConnectionError("network down"))
        monkeypatch.setattr("prepare_text.requests.post", mock_post)

        # Should not raise
        send_gotify_notification("Title", "Message")

    def test_skips_when_env_vars_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOTIFY_SERVER", raising=False)
        monkeypatch.delenv("GOTIFY_TOKEN", raising=False)

        mock_post = MagicMock()
        monkeypatch.setattr("prepare_text.requests.post", mock_post)

        send_gotify_notification("Title", "Message")
        mock_post.assert_not_called()

    def test_sends_with_correct_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOTIFY_SERVER", "https://gotify.example.com")
        monkeypatch.setenv("GOTIFY_TOKEN", "test-token-123")

        mock_post = MagicMock()
        monkeypatch.setattr("prepare_text.requests.post", mock_post)

        send_gotify_notification("Test Title", "Test Message", priority=8)

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://gotify.example.com/message?token=test-token-123"
        assert call_args[1]["data"]["title"] == "Test Title"
        assert call_args[1]["data"]["message"] == "Test Message"
        assert call_args[1]["data"]["priority"] == 8
