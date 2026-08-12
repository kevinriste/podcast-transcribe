"""extract_comments pulls (author, own-body, reply_to) and handles nesting."""

import logging

from comment_briefing import extract_comments

# A top-level comment with one nested reply; the parent must NOT absorb the child's body.
SAMPLE = """
<ol class="commentlist">
  <li class="comment"><cite class="fn">Alice Reader</cite>
    <div class="comment-body"><p>Making up numbers is inherently dubious.</p></div>
    <ul class="children">
      <li class="comment"><cite class="fn">Bob Reader</cite>
        <div class="comment-body"><p>How many QALYs can I expect to gain?</p></div></li>
    </ul>
  </li>
  <li class="comment"><cite class="fn">Empty</cite>
    <div class="comment-body"></div></li>
</ol>
"""


def check() -> None:
    """Verify own-body extraction (no child bleed) and reply-to capture.

    Raises:
        AssertionError: If extraction, nesting, or reply_to is wrong.

    """
    out = extract_comments(SAMPLE)
    expected = [
        ("Alice Reader", "Making up numbers is inherently dubious.", 0),
        ("Bob Reader", "How many QALYs can I expect to gain?", 1),
    ]
    if out != expected:
        msg = f"got {out!r}, expected {expected!r}"
        raise AssertionError(msg)


if __name__ == "__main__":
    check()
    logging.info("extract_comments tests passed.")
