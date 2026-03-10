# pyright: reportExplicitAny=false, reportAny=false, reportPrivateUsage=false, reportUnusedCallResult=false
# pyright: reportUnannotatedClassAttribute=false
from pyrsistent import PMap, PVector, pmap, pvector

from parse_email import (
    _dedup_links,
    clean_substack_url,
    extract_links_from_email,
    find_source_url,
    normalize_text,
    unfold_header_value,
)


class TestNormalizeText:
    def test_basic_normalization(self):
        assert normalize_text("  Hello   World  ") == "hello world"

    def test_tabs_and_newlines(self):
        assert normalize_text("Hello\t\n  World") == "hello world"

    def test_already_normalized(self):
        assert normalize_text("hello world") == "hello world"

    def test_empty_string(self):
        assert not normalize_text("")

    def test_single_word(self):
        assert normalize_text("  HELLO  ") == "hello"

    def test_mixed_case_with_extra_spaces(self):
        assert normalize_text("  Read   Online  ") == "read online"


class TestUnfoldHeaderValue:
    def test_none_input(self):
        assert not unfold_header_value(None)

    def test_empty_string(self):
        assert not unfold_header_value("")

    def test_no_folding(self):
        assert unfold_header_value("Simple Header") == "Simple Header"

    def test_crlf_space_folding(self):
        assert unfold_header_value("Hello\r\n World") == "Hello World"

    def test_lf_tab_folding(self):
        assert unfold_header_value("Hello\n\tWorld") == "Hello World"

    def test_multiple_folds(self):
        assert unfold_header_value("A\r\n B\r\n C") == "A B C"

    def test_bare_newlines_removed(self):
        assert unfold_header_value("Line1\nLine2\nLine3") == "Line1 Line2 Line3"

    def test_strips_surrounding_whitespace(self):
        assert unfold_header_value("  spaced  ") == "spaced"

    def test_crlf_without_continuation(self):
        assert unfold_header_value("End\r\nStart") == "End Start"


class TestCleanSubstackUrl:
    def test_non_substack_url_unchanged(self):
        url = "https://example.com/article?foo=bar"
        assert clean_substack_url(url) == url

    def test_substack_url_no_query(self):
        url = "https://newsletter.substack.com/p/my-post"
        assert clean_substack_url(url) == url

    def test_strips_tracking_params(self):
        url = (
            "https://newsletter.substack.com/api/v1/redirect?"
            "publication_id=123&post_id=456&utm_source=email&utm_medium=link&tracking=xyz"
        )
        result = clean_substack_url(url)
        assert "publication_id=123" in result
        assert "post_id=456" in result
        assert "utm_source" not in result
        assert "tracking" not in result

    def test_missing_publication_id_returns_original(self):
        url = "https://newsletter.substack.com/api/v1/redirect?post_id=456&utm_source=email"
        assert clean_substack_url(url) == url

    def test_missing_post_id_returns_original(self):
        url = "https://newsletter.substack.com/api/v1/redirect?publication_id=123&utm_source=email"
        assert clean_substack_url(url) == url

    def test_preserves_scheme_and_netloc(self):
        url = "https://newsletter.substack.com/api/v1/redirect?publication_id=111&post_id=222&extra=junk"
        result = clean_substack_url(url)
        assert result.startswith("https://newsletter.substack.com/")


class TestFindSourceUrl:
    def test_beehiiv_read_online(self):
        links = pvector(
            [
                pmap({"href": "https://example.com/other", "text": "Other link"}),
                pmap({"href": "https://beehiiv.com/article", "text": "Read Online"}),
            ]
        )
        result = find_source_url(links, "beehiiv", "My Newsletter")
        assert result == "https://beehiiv.com/article"

    def test_beehiiv_case_insensitive(self):
        links = pvector(
            [
                pmap({"href": "https://beehiiv.com/article", "text": "  READ   ONLINE  "}),
            ]
        )
        result = find_source_url(links, "beehiiv", "Newsletter")
        assert result == "https://beehiiv.com/article"

    def test_beehiiv_no_read_online(self):
        links = pvector(
            [
                pmap({"href": "https://beehiiv.com/article", "text": "Click here"}),
            ]
        )
        result = find_source_url(links, "beehiiv", "Newsletter")
        assert not result

    def test_substack_app_link_by_title(self):
        links = pvector(
            [
                pmap({"href": "https://substack.com/app-link/post?pub=1&post=2", "text": "My Post Title"}),
            ]
        )
        result = find_source_url(links, "substack", "My Post Title")
        assert "substack.com" in result

    def test_substack_open_link_by_title(self):
        links = pvector(
            [
                pmap({"href": "https://open.substack.com/pub/author/p/my-post", "text": "My Post"}),
            ]
        )
        result = find_source_url(links, "substack", "My Post")
        assert result == "https://open.substack.com/pub/author/p/my-post"

    def test_substack_app_link_fallback_no_title_match(self):
        links = pvector(
            [
                pmap({"href": "https://substack.com/app-link/post?pub=1&post=2", "text": "Different Text"}),
            ]
        )
        result = find_source_url(links, "substack", "My Post Title")
        assert "substack.com/app-link/post" in result

    def test_substack_open_link_fallback_no_title_match(self):
        links = pvector(
            [
                pmap({"href": "https://open.substack.com/pub/author/p/my-post", "text": "Something Else"}),
            ]
        )
        result = find_source_url(links, "substack", "My Post Title")
        assert "open.substack.com" in result

    def test_substack_generic_fallback(self):
        links = pvector(
            [
                pmap({"href": "https://newsletter.substack.com/p/my-article?pub=1&post=2", "text": "Read more"}),
            ]
        )
        result = find_source_url(links, "substack", "Totally Different")
        assert "substack.com" in result

    def test_unknown_source_kind(self):
        links = pvector(
            [
                pmap({"href": "https://example.com", "text": "Link"}),
            ]
        )
        result = find_source_url(links, "unknown", "Subject")
        assert not result

    def test_empty_links(self):
        empty: PVector[PMap[str, str]] = pvector()
        result = find_source_url(empty, "substack", "Subject")
        assert not result

    def test_accepts_plain_dicts(self):
        """find_source_url accepts Sequence[Mapping] so plain dicts work too."""
        links = [
            {"href": "https://beehiiv.com/article", "text": "Read Online"},
        ]
        result = find_source_url(links, "beehiiv", "Newsletter")
        assert result == "https://beehiiv.com/article"


class TestDedupLinks:
    def test_removes_duplicates(self):
        links = [
            pmap({"href": "https://a.com", "text": "A"}),
            pmap({"href": "https://b.com", "text": "B"}),
            pmap({"href": "https://a.com", "text": "A duplicate"}),
        ]
        result = _dedup_links(links)
        assert len(result) == 2
        assert result[0]["href"] == "https://a.com"
        assert result[1]["href"] == "https://b.com"

    def test_preserves_first_occurrence(self):
        links = [
            pmap({"href": "https://a.com", "text": "First"}),
            pmap({"href": "https://a.com", "text": "Second"}),
        ]
        result = _dedup_links(links)
        assert len(result) == 1
        assert result[0]["text"] == "First"

    def test_empty_input(self):
        result = _dedup_links([])
        assert len(result) == 0

    def test_returns_pvector(self):
        links = [pmap({"href": "https://a.com", "text": "A"})]
        result = _dedup_links(links)
        assert isinstance(result, PVector)


class TestExtractLinksFromEmail:
    def test_extracts_html_links(self):
        class FakeMsg:
            html: str | None = '<html><body><a href="https://example.com">Example</a></body></html>'
            text: str | None = None

        result = extract_links_from_email(FakeMsg())
        assert len(result) == 1
        assert result[0]["href"] == "https://example.com"
        assert result[0]["text"] == "Example"

    def test_extracts_text_links(self):
        class FakeMsg:
            html: str | None = None
            text: str | None = "Check out https://example.com and https://other.com for more."

        result = extract_links_from_email(FakeMsg())
        assert len(result) == 2
        assert result[0]["href"] == "https://example.com"
        assert result[1]["href"] == "https://other.com"

    def test_deduplicates_across_html_and_text(self):
        class FakeMsg:
            html: str | None = '<html><body><a href="https://example.com">Link</a></body></html>'
            text: str | None = "Visit https://example.com for more."

        result = extract_links_from_email(FakeMsg())
        assert len(result) == 1

    def test_no_links(self):
        class FakeMsg:
            html: str | None = "<html><body>No links here</body></html>"
            text: str | None = "Also no links"

        result = extract_links_from_email(FakeMsg())
        assert len(result) == 0

    def test_none_html_and_text(self):
        class FakeMsg:
            html: str | None = None
            text: str | None = None

        result = extract_links_from_email(FakeMsg())
        assert len(result) == 0

    def test_returns_pvector_of_pmaps(self):
        class FakeMsg:
            html: str | None = '<html><body><a href="https://example.com">Ex</a></body></html>'
            text: str | None = None

        result = extract_links_from_email(FakeMsg())
        assert isinstance(result, PVector)
        assert isinstance(result[0], PMap)
