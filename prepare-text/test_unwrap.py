"""Tests for unwrapping hard-wrapped text paragraphs."""

import logging
import sys

from prepare_text import unwrap_hard_wraps

logging.basicConfig(level=logging.INFO)


def run_tests() -> None:
    """Run all unwrap_hard_wraps tests."""
    # Test 1: Hard wrapped paragraph
    text_1 = (
        "Every financial services firm will tell its new employees, in their first week\n"
        'or two of work, the same thing: "Be careful with our confidential information.\n'
        "Don't tell anyone outside of the firm what you're working on."
    )

    expected_1 = (
        "Every financial services firm will tell its new employees, in their first week "
        'or two of work, the same thing: "Be careful with our confidential information. '
        "Don't tell anyone outside of the firm what you're working on."
    )

    unwrapped_1 = unwrap_hard_wraps(text_1)
    logging.info("Unwrapped 1:\n%s\n", unwrapped_1)
    if unwrapped_1 != expected_1:
        logging.error("Test 1 failed")
        sys.exit(1)

    # Test 2: Paragraph that is a bulleted list
    text_2 = "- Item one of list\n- Item two of list\n- Item three of list"

    unwrapped_2 = unwrap_hard_wraps(text_2)
    logging.info("Unwrapped 2:\n%s\n", unwrapped_2)
    if unwrapped_2 != text_2:
        logging.error("Test 2 failed")
        sys.exit(1)

    # Test 3: Paragraph that is a numbered list
    text_3 = "1. First item\n2. Second item\n3. Third item"

    unwrapped_3 = unwrap_hard_wraps(text_3)
    logging.info("Unwrapped 3:\n%s\n", unwrapped_3)
    if unwrapped_3 != text_3:
        logging.error("Test 3 failed")
        sys.exit(1)

    # Test 4: Mixed text with hard wrap and list
    text_4 = (
        "This is a normal paragraph that has been hard wrapped\n"
        "onto two lines.\n\n"
        "- Bullet 1\n"
        "- Bullet 2\n\n"
        "And another hard wrapped\n"
        "paragraph here."
    )

    expected_4 = (
        "This is a normal paragraph that has been hard wrapped onto two lines.\n\n"
        "- Bullet 1\n"
        "- Bullet 2\n\n"
        "And another hard wrapped paragraph here."
    )

    unwrapped_4 = unwrap_hard_wraps(text_4)
    logging.info("Unwrapped 4:\n%s\n", unwrapped_4)
    if unwrapped_4 != expected_4:
        logging.error("Test 4 failed")
        sys.exit(1)

    logging.info("All unwrap_hard_wraps tests passed successfully!")


if __name__ == "__main__":
    run_tests()
