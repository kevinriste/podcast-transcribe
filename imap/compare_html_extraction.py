"""Compare HTML extraction approaches for newsletter emails.

Connects to Gmail IMAP (read-only), fetches recent newsletter emails,
and extracts content using four approaches. Writes output to
html-comparison/ for manual review.

Usage:
    source /etc/profile.d/podcast-transcribe.sh
    cd imap && uv run python3 compare_html_extraction.py
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
from datetime import date, timedelta

from bs4 import BeautifulSoup, NavigableString, Tag
from imap_tools.mailbox import MailBox
from imap_tools.message import MailMessage
from imap_tools.query import AND
from trafilatura import extract

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

OUTPUT_DIR = pathlib.Path("../html-comparison")
MAX_EMAILS = 10
LOOKBACK_DAYS = 30

SKIP_SUBJECTS = frozenset({
    "receipt", "refund", "subscription", "ending in a week",
    "has ended", "payment",
})

SKIP_TAGS = frozenset({
    "script", "style", "nav", "footer", "header", "noscript",
    "svg", "iframe", "form", "button", "input", "select",
    "textarea", "meta", "link", "img", "head",
})

LAYOUT_CRUFT_TAGS = frozenset({
    "script", "style", "noscript", "nav", "footer", "header",
    "form", "button", "input", "select", "textarea",
    "iframe", "svg", "video", "audio", "canvas", "object", "embed",
    "meta", "link", "base", "head",
})

TRACKING_PIXEL_SELECTORS = [
    'img[width="1"]', 'img[height="1"]',
    'img[width="0"]', 'img[height="0"]',
    'img[src*="tracking"]', 'img[src*="pixel"]',
    'img[src*="beacon"]', 'img[src*="open."]',
    'img[src*="mailchimp.com/track"]',
    'img[src*="list-manage.com"]',
    'img[src*="beehiiv.com/opens"]',
]

ATTRS_TO_STRIP = frozenset({
    "class", "id", "style", "align", "valign", "bgcolor",
    "border", "cellpadding", "cellspacing", "width", "height",
    "role", "aria-hidden", "tabindex", "target", "rel", "dir", "lang",
})


# ---------------------------------------------------------------------------
# Approach A: Plain text (current baseline)
# ---------------------------------------------------------------------------


def extract_a_plain_text(msg: MailMessage) -> str:
    """Extract plain text email body (current pipeline approach)."""
    return msg.text or ""


# ---------------------------------------------------------------------------
# Approach B: Trafilatura markdown
# ---------------------------------------------------------------------------


def extract_b_trafilatura_markdown(html: str) -> str:
    """Extract content as markdown via trafilatura."""
    result = extract(
        html,
        output_format="markdown",
        include_comments=False,
        favor_recall=True,
        include_tables=True,
        include_links=False,
        include_formatting=True,
    )
    return str(result or "")


# ---------------------------------------------------------------------------
# Approach C: BeautifulSoup selective semantic extraction
# ---------------------------------------------------------------------------


def _extract_with_emphasis(element: Tag) -> str:
    """Extract text from an element, preserving bold/italic markers."""
    parts: list[str] = []
    for child in element.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            if child.name in {"strong", "b"}:
                parts.append(f"**{child.get_text()}**")
            elif child.name in {"em", "i"}:
                parts.append(f"*{child.get_text()}*")
            else:
                parts.append(child.get_text())
    return " ".join("".join(parts).split())


def _walk_element(element: Tag, output: list[str]) -> None:
    """Recursively walk DOM, emitting semantic markers."""
    for child in element.children:
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in SKIP_TAGS:
            continue

        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = name[1]
            text = child.get_text(" ", strip=True)
            if text:
                output.append(f"[H{level}] {text}")
                output.append("")
        elif name == "blockquote":
            text = child.get_text(" ", strip=True)
            if text:
                output.append(f"[QUOTE] {text}")
                output.append("")
        elif name in {"ul", "ol"}:
            for i, li in enumerate(child.find_all("li", recursive=False)):
                prefix = f"  {i + 1}." if name == "ol" else "  -"
                text = li.get_text(" ", strip=True)
                if text:
                    output.append(f"{prefix} {text}")
            output.append("")
        elif name == "p":
            text = _extract_with_emphasis(child)
            if text:
                output.append(text)
                output.append("")
        elif name in {"div", "section", "article", "main", "td", "th", "span", "table", "tr", "tbody", "html", "body"}:
            _walk_element(child, output)


def extract_c_beautifulsoup_selective(html: str) -> str:
    """Extract content with semantic markers via BeautifulSoup DOM walking."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(SKIP_TAGS):
        tag.decompose()
    for tag in soup.find_all(True, style=True):
        style = tag.get("style", "")
        if "display:none" in style.replace(" ", "") or "display: none" in style:
            tag.decompose()

    output: list[str] = []
    _walk_element(soup, output)
    return "\n".join(output).strip()


# ---------------------------------------------------------------------------
# Approach D: Cleaned HTML (strip layout, keep semantic tags)
# ---------------------------------------------------------------------------


def extract_d_cleaned_html(html: str) -> str:
    """Strip layout cruft from HTML, keeping semantic structure."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove layout cruft tags
    for tag in soup.find_all(LAYOUT_CRUFT_TAGS):
        tag.decompose()
    # Remove hidden elements (email preview text, tracking, etc.)
    for tag in soup.find_all(True, style=True):
        style = tag.get("style", "")
        if "display:none" in style.replace(" ", "") or "display: none" in style:
            tag.decompose()

    # Remove tracking pixels
    for selector in TRACKING_PIXEL_SELECTORS:
        for img in soup.select(selector):
            img.decompose()

    # Remove decorative images (no meaningful alt text)
    for img in soup.find_all("img"):
        alt = img.get("alt", "")
        if not alt or len(str(alt)) < 3:
            img.decompose()

    # Strip layout attributes
    for tag in soup.find_all(True):
        for attr_name in list(tag.attrs.keys()):
            if attr_name in ATTRS_TO_STRIP or attr_name.startswith(("data-", "on")):
                del tag[attr_name]

    # Unwrap pure-layout wrapper tags
    for tag in soup.find_all(["span", "font", "center"]):
        tag.unwrap()

    # Unwrap single-column layout tables
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if rows:
            max_cols = max(len(row.find_all(["td", "th"])) for row in rows)
            if max_cols <= 1:
                table.unwrap()

    # Collapse empty tags
    for tag in soup.find_all(True):
        if not tag.get_text(strip=True) and tag.name not in {"br", "hr"}:
            tag.decompose()

    result = str(soup)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


# ---------------------------------------------------------------------------
# Semantic signal counting
# ---------------------------------------------------------------------------


def count_signals(text: str, mode: str) -> dict[str, int]:
    """Count semantic signals preserved in extracted text."""
    if mode == "A":
        return {"headings": 0, "bold": 0, "italic": 0, "list_items": 0, "blockquotes": 0}
    if mode == "B":
        return {
            "headings": len(re.findall(r"^#{1,6} ", text, re.MULTILINE)),
            "bold": len(re.findall(r"\*\*[^*]+\*\*", text)),
            "italic": len(re.findall(r"(?<!\*)\*(?!\*)[^*]+\*(?!\*)", text)),
            "list_items": len(re.findall(r"^- ", text, re.MULTILINE)),
            "blockquotes": len(re.findall(r"^> ", text, re.MULTILINE)),
        }
    if mode == "C":
        return {
            "headings": len(re.findall(r"^\[H\d\]", text, re.MULTILINE)),
            "bold": len(re.findall(r"\*\*[^*]+\*\*", text)),
            "italic": len(re.findall(r"(?<!\*)\*(?!\*)[^*]+\*(?!\*)", text)),
            "list_items": len(re.findall(r"^\s+[-\d]+\.?\s", text, re.MULTILINE)),
            "blockquotes": len(re.findall(r"^\[QUOTE\]", text, re.MULTILINE)),
        }
    # mode == "D"
    return {
        "headings": len(re.findall(r"<h[1-6][ >]", text, re.IGNORECASE)),
        "bold": len(re.findall(r"<(?:strong|b)[ >]", text, re.IGNORECASE)),
        "italic": len(re.findall(r"<(?:em|i)[ >]", text, re.IGNORECASE)),
        "list_items": len(re.findall(r"<li[ >]", text, re.IGNORECASE)),
        "blockquotes": len(re.findall(r"<blockquote[ >]", text, re.IGNORECASE)),
    }


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def print_summary(results: list[dict[str, object]]) -> None:
    """Print comparison summary table."""
    print("\n" + "=" * 100)
    print("CHARACTER COUNTS")
    print(f"{'Email':<45s} {'A:plain':>8s} {'B:trafil':>9s} {'C:bs4':>8s} {'D:html':>8s} {'Beehiiv':>8s}")
    print("-" * 100)
    for r in results:
        name = str(r["name"])[:44]
        beehiiv = "Yes" if r["beehiiv"] else "No"
        print(f"{name:<45s} {r['a']:>8,d} {r['b']:>9,d} {r['c']:>8,d} {r['d']:>8,d} {beehiiv:>8s}")

    print("\n" + "=" * 100)
    print("SEMANTIC SIGNALS DETECTED")
    print(f"{'Email':<30s} {'Mode':<6s} {'Heads':>6s} {'Bold':>6s} {'Italic':>6s} {'Lists':>6s} {'Quotes':>6s}")
    print("-" * 100)
    for r in results:
        name = str(r["name"])[:29]
        signals = r.get("signals", {})
        if not signals:
            continue
        for mode in ("A", "B", "C", "D"):
            s = signals.get(mode, {})
            if not s:
                continue
            print(
                f"{name if mode == 'A' else '':<30s} {mode:<6s} "
                f"{s.get('headings', 0):>6d} {s.get('bold', 0):>6d} "
                f"{s.get('italic', 0):>6d} {s.get('list_items', 0):>6d} "
                f"{s.get('blockquotes', 0):>6d}"
            )
        print()
    print("=" * 100)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Fetch recent emails and compare HTML extraction approaches."""
    gmail_user = os.getenv("GMAIL_PODCAST_ACCOUNT")
    gmail_password = os.getenv("GMAIL_PODCAST_ACCOUNT_APP_PASSWORD")
    if not gmail_user or not gmail_password:
        logging.error("Gmail credentials not set — source /etc/profile.d/podcast-transcribe.sh")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with MailBox("imap.gmail.com").login(gmail_user, gmail_password) as mailbox:
        query = AND(seen=True, date_gte=date.today() - timedelta(days=LOOKBACK_DAYS))
        msgs = mailbox.fetch(query, mark_seen=False, limit=MAX_EMAILS * 5, reverse=True)

        results: list[dict[str, object]] = []
        count = 0

        for msg in msgs:
            if count >= MAX_EMAILS:
                break

            subject_raw = (msg.subject or "").replace("Fwd: ", "")
            subject_clean = re.sub(r"[^A-Za-z0-9 ]+", "", subject_raw)
            subject_lower = subject_clean.strip().lower()
            if subject_lower in {"link", "youtube"}:
                continue
            if any(skip in subject_lower for skip in SKIP_SUBJECTS):
                logging.info("Skipping junk email: %s", subject_raw[:50])
                continue

            from_values = msg.from_values
            from_name = from_values.name if from_values else ""
            from_name_clean = re.sub(r"[^A-Za-z0-9 ]+", "", from_name)
            from_email = from_values.email if from_values else ""

            has_beehiiv = bool(msg.headers.get("x-beehiiv-ids"))

            count += 1
            logging.info("Processing email %d: %s — %s", count, from_name, subject_raw[:50])
            dir_name = f"email-{count}-{from_name_clean}-{subject_clean}"[:80]
            email_dir = OUTPUT_DIR / dir_name
            email_dir.mkdir(parents=True, exist_ok=True)

            # Metadata
            _ = (email_dir / "metadata.txt").write_text(
                f"From: {from_name} <{from_email}>\n"
                f"Subject: {subject_raw}\n"
                f"Date: {msg.date}\n"
                f"Beehiiv: {has_beehiiv}\n",
                encoding="utf-8",
            )

            # A: Plain text
            text_a = extract_a_plain_text(msg)
            _ = (email_dir / "a_plain_text.txt").write_text(text_a, encoding="utf-8")

            if not msg.html:
                logging.info("  No HTML part, skipping B/C/D")
                results.append({
                    "name": f"{from_name_clean} - {subject_clean}"[:50],
                    "a": len(text_a), "b": 0, "c": 0, "d": 0,
                    "beehiiv": has_beehiiv, "signals": {},
                })
                continue

            # Save source HTML
            _ = (email_dir / "source.html").write_text(msg.html, encoding="utf-8")

            # B: Trafilatura markdown
            text_b = extract_b_trafilatura_markdown(msg.html)
            _ = (email_dir / "b_trafilatura_markdown.md").write_text(text_b, encoding="utf-8")

            # C: BeautifulSoup selective
            text_c = extract_c_beautifulsoup_selective(msg.html)
            _ = (email_dir / "c_beautifulsoup_selective.txt").write_text(text_c, encoding="utf-8")

            # D: Cleaned HTML
            text_d = extract_d_cleaned_html(msg.html)
            _ = (email_dir / "d_cleaned_html.html").write_text(text_d, encoding="utf-8")

            signals = {
                "A": count_signals(text_a, "A"),
                "B": count_signals(text_b, "B"),
                "C": count_signals(text_c, "C"),
                "D": count_signals(text_d, "D"),
            }
            results.append({
                "name": f"{from_name_clean} - {subject_clean}"[:50],
                "a": len(text_a), "b": len(text_b),
                "c": len(text_c), "d": len(text_d),
                "beehiiv": has_beehiiv, "signals": signals,
            })

        print_summary(results)
        print(f"\nOutput written to {OUTPUT_DIR.resolve()}/")


if __name__ == "__main__":
    main()
