# pyright: reportExplicitAny=false, reportAny=false, reportPrivateUsage=false, reportUnusedCallResult=false
from typing import Final

import pytest
from pyrsistent import PMap, pmap

from text_to_speech import (
    DescriptionParams,
    _build_output_filename,
    _build_title_for_tag,
    build_description,
    split_metadata,
    to_base36,
)


class TestSplitMetadata:
    def test_no_metadata_returns_empty_pmap_and_full_text(self):
        raw: Final = "Just some plain text\nwith multiple lines"
        metadata, content = split_metadata(raw)
        assert metadata == pmap({})
        assert content == raw

    def test_single_meta_field(self):
        raw: Final = "META_FROM: John Doe\n\nBody text here"
        metadata, content = split_metadata(raw)
        assert metadata == pmap({"from": "John Doe"})
        assert content == "Body text here"

    def test_multiple_meta_fields(self):
        raw: Final = (
            "META_FROM: Alice\n"
            "META_TITLE: My Article\n"
            "META_SOURCE_URL: https://example.com\n"
            "META_SOURCE_KIND: beehiiv\n"
            "\n"
            "Article body"
        )
        metadata, content = split_metadata(raw)
        assert metadata["from"] == "Alice"
        assert metadata["title"] == "My Article"
        assert metadata["source_url"] == "https://example.com"
        assert metadata["source_kind"] == "beehiiv"
        assert content == "Article body"

    def test_continuation_lines(self):
        raw: Final = "META_TITLE: A Very Long Title\n  That Continues Here\n\nContent"
        metadata, content = split_metadata(raw)
        assert metadata["title"] == "A Very Long Title That Continues Here"
        assert content == "Content"

    def test_tab_continuation_lines(self):
        raw: Final = "META_TITLE: Start\n\tContinued\n\nBody"
        metadata, content = split_metadata(raw)
        assert metadata["title"] == "Start Continued"
        assert content == "Body"

    def test_empty_body(self):
        raw: Final = "META_TITLE: Only Meta\n"
        metadata, content = split_metadata(raw)
        assert metadata["title"] == "Only Meta"
        assert not content

    def test_meta_without_colon_stops_parsing(self):
        raw: Final = "META_BROKEN\nBody text"
        metadata, content = split_metadata(raw)
        assert metadata == pmap({})
        assert content == "META_BROKEN\nBody text"

    def test_returns_pmap_type(self):
        raw: Final = "META_FROM: Test\n\nBody"
        metadata, _ = split_metadata(raw)
        assert isinstance(metadata, PMap)

    def test_meta_value_whitespace_stripped(self):
        raw: Final = "META_TITLE:   spaces around   \n\nContent"
        metadata, content = split_metadata(raw)
        assert metadata["title"] == "spaces around"
        assert content == "Content"

    def test_non_meta_line_stops_parsing(self):
        raw: Final = "META_FROM: Alice\nNot a meta line\nmore text"
        metadata, content = split_metadata(raw)
        assert metadata["from"] == "Alice"
        assert content == "Not a meta line\nmore text"


class TestBuildDescription:
    def test_minimal_params(self):
        params: Final = DescriptionParams(summary="A summary", title="Title", source_url="", source_kind="")
        result: Final = build_description(params)
        assert result == "A summary<br/><br/>Title: Title"

    def test_no_summary_shows_unavailable(self):
        params: Final = DescriptionParams(summary="", title="Title", source_url="", source_kind="")
        result: Final = build_description(params)
        assert "Summary unavailable." in result

    def test_no_title_shows_untitled(self):
        params: Final = DescriptionParams(summary="A summary", title="", source_url="", source_kind="")
        result: Final = build_description(params)
        assert "Title: Untitled" in result

    def test_with_source_url(self):
        params: Final = DescriptionParams(
            summary="Sum", title="T", source_url="https://example.com", source_kind="substack"
        )
        result: Final = build_description(params)
        assert '<a href="https://example.com">https://example.com</a>' in result

    def test_beehiiv_source_uses_name(self):
        params: Final = DescriptionParams(
            summary="Sum",
            title="T",
            source_url="https://example.com",
            source_kind="beehiiv",
            source_name="My Newsletter",
        )
        result: Final = build_description(params)
        assert '<a href="https://example.com">My Newsletter</a>' in result

    def test_beehiiv_without_name_uses_url(self):
        params: Final = DescriptionParams(
            summary="Sum", title="T", source_url="https://example.com", source_kind="beehiiv", source_name=""
        )
        result: Final = build_description(params)
        assert '<a href="https://example.com">https://example.com</a>' in result

    def test_intake_type_email(self):
        params: Final = DescriptionParams(summary="Sum", title="T", source_url="", source_kind="", intake_type="email")
        result: Final = build_description(params)
        assert "Via: Email" in result

    def test_intake_type_rss(self):
        params: Final = DescriptionParams(summary="Sum", title="T", source_url="", source_kind="", intake_type="rss")
        result: Final = build_description(params)
        assert "Via: RSS" in result

    def test_intake_type_unknown_passes_through(self):
        params: Final = DescriptionParams(
            summary="Sum", title="T", source_url="", source_kind="", intake_type="carrier_pigeon"
        )
        result: Final = build_description(params)
        assert "Via: carrier_pigeon" in result

    def test_all_parts_joined_with_br(self):
        params: Final = DescriptionParams(
            summary="Sum",
            title="T",
            source_url="https://example.com",
            source_kind="substack",
            intake_type="email",
        )
        result: Final = build_description(params)
        parts = result.split("<br/><br/>")
        assert len(parts) == 4
        assert parts[0] == "Sum"
        assert parts[1] == "Title: T"
        assert parts[2] == "Via: Email"
        assert "Source:" in parts[3]


class TestToBase36:
    def test_zero(self):
        assert to_base36(0) == "0"

    def test_small_numbers(self):
        assert to_base36(1) == "1"
        assert to_base36(10) == "a"
        assert to_base36(35) == "z"

    def test_thirty_six(self):
        assert to_base36(36) == "10"

    def test_large_number(self):
        assert to_base36(1000) == "rs"

    def test_unix_timestamp_range(self):
        result: Final = to_base36(1700000000)
        assert len(result) == 6
        assert all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in result)

    def test_roundtrip(self):
        values: Final = (0, 1, 35, 36, 100, 999999, 1700000000)
        for val in values:
            result = to_base36(val)
            reconstructed = int(result, 36)
            assert reconstructed == val


class TestBuildOutputFilename:
    def test_with_dash_in_name(self):
        name: Final = "20260310-123456-author-article title here"
        result: Final = _build_output_filename(name)
        assert result.startswith("../dropcaster-docker/audio/")
        assert result.endswith(".mp3")
        assert "author-" in result
        assert "20260310-123456-" in result

    def test_without_dash_in_name(self):
        name: Final = "20260310-123456-articleonly"
        result: Final = _build_output_filename(name)
        assert result.startswith("../dropcaster-docker/audio/")
        assert result.endswith(".mp3")
        assert "articleonly" in result


class TestBuildTitleForTag:
    def test_with_from_and_title(self):
        metadata: Final[PMap[str, str]] = pmap({"from": "Author", "title": "Article Title"})
        result: Final = _build_title_for_tag(metadata, "fallback")
        assert result.startswith("Author- ")
        assert result.endswith("- Article Title")

    def test_without_from_uses_meta_title(self):
        metadata: Final[PMap[str, str]] = pmap({"title": "Article Title"})
        result: Final = _build_title_for_tag(metadata, "fallback")
        assert result == "Article Title"

    def test_without_from_or_title_uses_file_title(self):
        metadata: Final[PMap[str, str]] = pmap({})
        result: Final = _build_title_for_tag(metadata, "fallback")
        assert result == "fallback"

    def test_base36_in_middle(self):
        metadata: Final[PMap[str, str]] = pmap({"from": "Author", "title": "Title"})
        result: Final = _build_title_for_tag(metadata, "fallback")
        parts = result.split("- ")
        assert len(parts) == 3
        base36_part: Final = parts[1].strip()
        int(base36_part, 36)


class TestDescriptionParams:
    def test_frozen(self):
        params: Final = DescriptionParams(summary="s", title="t", source_url="u", source_kind="k")
        with pytest.raises(AttributeError):
            params.summary = "new"  # pyright: ignore[reportAttributeAccessIssue]

    def test_defaults(self):
        params: Final = DescriptionParams(summary="s", title="t", source_url="u", source_kind="k")
        assert not params.source_name
        assert not params.intake_type

    def test_slots(self):
        params: Final = DescriptionParams(summary="s", title="t", source_url="u", source_kind="k")
        assert hasattr(params, "__slots__")
