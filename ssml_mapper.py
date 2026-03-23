"""Deterministic SSML mapper (S-2).

Converts structured text (E-C markers or E-D cleaned HTML) to valid SSML
for Google Cloud TTS. No LLM calls — pure mechanical mapping.

Supports two input modes:
- "markers": E-C BeautifulSoup output with [H2], [QUOTE], **bold**, *italic*
- "html": E-D cleaned HTML with semantic tags preserved

Usage:
    from ssml_mapper import markers_to_ssml, html_to_ssml

    ssml = markers_to_ssml(text)   # from E-C output
    ssml = html_to_ssml(html)      # from E-D output
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup, NavigableString, Tag


def _escape_xml(text: str) -> str:
    """Escape text for safe inclusion in SSML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _wrap_speak(body: str) -> str:
    """Wrap SSML body in <speak> root element."""
    return f"<speak>{body}</speak>"


# ---------------------------------------------------------------------------
# Mode 1: E-C markers → SSML
# ---------------------------------------------------------------------------

# Patterns for marker-based input
_HEADING_RE = re.compile(r"^\[H(\d)\]\s*(.+)$", re.MULTILINE)
_QUOTE_RE = re.compile(r"^\[QUOTE\]\s*(.+)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)")
_OL_ITEM_RE = re.compile(r"^\s+(\d+)\.\s*(.+)$", re.MULTILINE)
_UL_ITEM_RE = re.compile(r"^\s+-\s*(.+)$", re.MULTILINE)


def _process_inline_markers(text: str) -> str:
    """Convert **bold** and *italic* markers to SSML emphasis."""
    escaped = _escape_xml(text)
    escaped = _BOLD_RE.sub(r'<emphasis level="moderate">\1</emphasis>', escaped)
    escaped = _ITALIC_RE.sub(r'<emphasis level="reduced">\1</emphasis>', escaped)
    return escaped


def markers_to_ssml(text: str) -> str:
    """Convert E-C marker text to SSML.

    Returns:
        Valid SSML string wrapped in <speak> tags.

    """
    lines = text.split("\n")
    ssml_parts: list[str] = []
    paragraph_buffer: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_buffer:
            sentences = " ".join(paragraph_buffer)
            processed = _process_inline_markers(sentences)
            ssml_parts.append(f"<p><s>{processed}</s></p>")
            paragraph_buffer.clear()

    for line in lines:
        stripped = line.strip()

        # Empty line = paragraph break
        if not stripped:
            flush_paragraph()
            continue

        # Heading
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            flush_paragraph()
            heading_text = _escape_xml(heading_match.group(2))
            ssml_parts.append(
                f'<break time="800ms"/>'
                f'<p><s><emphasis level="strong">{heading_text}</emphasis></s></p>'
                f'<break time="400ms"/>'
            )
            continue

        # Blockquote
        quote_match = _QUOTE_RE.match(stripped)
        if quote_match:
            flush_paragraph()
            quote_text = _process_inline_markers(quote_match.group(1))
            ssml_parts.append(
                f'<break time="300ms"/>'
                f'<p><s><prosody rate="95%">{quote_text}</prosody></s></p>'
                f'<break time="300ms"/>'
            )
            continue

        # Ordered list item
        ol_match = _OL_ITEM_RE.match(line)
        if ol_match:
            flush_paragraph()
            item_text = _process_inline_markers(ol_match.group(2))
            ssml_parts.append(f'<break time="200ms"/><s>{item_text}</s>')
            continue

        # Unordered list item
        ul_match = _UL_ITEM_RE.match(line)
        if ul_match:
            flush_paragraph()
            item_text = _process_inline_markers(ul_match.group(1))
            ssml_parts.append(f'<break time="200ms"/><s>{item_text}</s>')
            continue

        # Regular text — accumulate into paragraph
        paragraph_buffer.append(stripped)

    flush_paragraph()

    return _wrap_speak("\n".join(ssml_parts))


# ---------------------------------------------------------------------------
# Mode 2: E-D cleaned HTML → SSML
# ---------------------------------------------------------------------------

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "header", "img", "br", "hr"})
_CONTAINER_TAGS = frozenset({
    "div", "section", "article", "main", "td", "th",
    "table", "tr", "tbody", "html", "body",
})


def _inline_to_ssml(element: Tag) -> str:
    """Convert inline HTML (strong, em, text) to SSML text."""
    parts: list[str] = []
    for child in element.children:
        if isinstance(child, NavigableString):
            parts.append(_escape_xml(str(child)))
        elif isinstance(child, Tag):
            inner = _escape_xml(child.get_text())
            if child.name in {"strong", "b"}:
                parts.append(f'<emphasis level="moderate">{inner}</emphasis>')
            elif child.name in {"em", "i"}:
                parts.append(f'<emphasis level="reduced">{inner}</emphasis>')
            elif child.name == "a":
                parts.append(_escape_xml(child.get_text()))
            else:
                parts.append(_escape_xml(child.get_text()))
    return "".join(parts)


def _walk_html_to_ssml(element: Tag, output: list[str]) -> None:
    """Recursively convert HTML DOM to SSML parts."""
    for child in element.children:
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in _SKIP_TAGS:
            continue

        if name in _HEADING_TAGS:
            heading_text = _escape_xml(child.get_text(" ", strip=True))
            if heading_text:
                output.append(
                    f'<break time="800ms"/>'
                    f'<p><s><emphasis level="strong">{heading_text}</emphasis></s></p>'
                    f'<break time="400ms"/>'
                )

        elif name == "blockquote":
            quote_text = _inline_to_ssml(child) if child.find(["strong", "em", "b", "i"]) else _escape_xml(child.get_text(" ", strip=True))
            if quote_text:
                output.append(
                    f'<break time="300ms"/>'
                    f'<p><s><prosody rate="95%">{quote_text}</prosody></s></p>'
                    f'<break time="300ms"/>'
                )

        elif name in {"ul", "ol"}:
            for li in child.find_all("li", recursive=False):
                item_text = _inline_to_ssml(li)
                if item_text:
                    output.append(f'<break time="200ms"/><s>{item_text}</s>')

        elif name == "p":
            para_text = _inline_to_ssml(child)
            if para_text.strip():
                output.append(f"<p><s>{para_text}</s></p>")

        elif name in _CONTAINER_TAGS:
            _walk_html_to_ssml(child, output)


def html_to_ssml(html: str) -> str:
    """Convert E-D cleaned HTML to SSML.

    Returns:
        Valid SSML string wrapped in <speak> tags.

    """
    soup = BeautifulSoup(html, "html.parser")
    output: list[str] = []
    _walk_html_to_ssml(soup, output)
    return _wrap_speak("\n".join(output))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_ssml(ssml: str) -> bool:
    """Check that SSML is valid XML.

    Returns:
        True if valid, False otherwise.

    """
    try:
        ET.fromstring(ssml)
    except ET.ParseError:
        return False
    return True


# ---------------------------------------------------------------------------
# CLI for quick testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python ssml_mapper.py <markers|html> <input_file>")
        sys.exit(1)

    mode = sys.argv[1]
    input_text = open(sys.argv[2], encoding="utf-8").read()

    if mode == "markers":
        result = markers_to_ssml(input_text)
    elif mode == "html":
        result = html_to_ssml(input_text)
    else:
        print(f"Unknown mode: {mode} (use 'markers' or 'html')")
        sys.exit(1)

    if validate_ssml(result):
        print(result)
    else:
        print("ERROR: Generated invalid SSML", file=sys.stderr)
        print(result)
        sys.exit(1)
