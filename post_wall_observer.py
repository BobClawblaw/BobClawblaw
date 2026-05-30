#!/usr/bin/env python3
"""
Post arbitrary text to Bitcointalk with cookie persistence.

Unified posting pipeline: subject + message → login check → token fetch → POST.

Usage:
    post_wall_observer.py <subject> <message_or_file> [--topic TOPIC_ID]
"""

import os
import sys
import json
import re
import urllib.parse
import subprocess

from posting_util import (
    post_with_login,
    load_credentials,
    is_logged_in,
)

CREDS_PATH = "/root/.hermes/bobclawblaw/profile/credentials.json"
COOKIE_PATH = "/root/.hermes/bobclawblaw/profile/bt_cookies.txt"
WALL_OBSERVER_TOPIC = "178336"


def clean_subject(subject):
    """Escape messy Unicode in the subject to clean ASCII."""
    replacements = {
        "\u2013": "-",      # en-dash
        "\u2014": "-",      # em-dash
        "\u2011": "-",      # non-breaking hyphen
        "\u00ad": "-",      # soft hyphen
        "\u202f": " ",      # narrow no-break space
        "\u2018": "'",      # left single
        "\u2019": "'",      # right single
        "\u201c": '"',      # left double
        "\u201d": '"',      # right double
        "\u2026": "...",    # ellipsis
        "\u00b7": " ",
        "\u2039": "<",      # single left angle
        "\u203a": ">",      # single right angle
    }
    # Also handle mojibake
    mojibake = {
        "â€'"   : "-",
        "Ã‚Â·"  : " ",
        "â€"    : '"',
    }
    for old, new in list(replacements.items()) + list(mojibake.items()):
        subject = subject.replace(old, new)
    return subject


def clean_message(message):
    """Remove non-printable, non-ASCII from the message body (BB tags only need ASCII)."""
    return ''.join(c for c in message
                   if 32 <= ord(c) <= 126
                   or c in ('\n', '\r', '\t'))


def main(argv=None):
    # Parse argv
    args = argv or sys.argv[1:]
    if len(args) < 2:
        print("Usage: post_wall_observer.py <subject> <message_or_file> [--topic TOPIC_ID]")
        sys.exit(1)

    subject = clean_subject(args[0])

    topic_id = WALL_OBSERVER_TOPIC
    for i in range(2, len(args)):
        if args[i] == "--topic" and i + 1 < len(args):
            topic_id = args[i + 1]
            break

    # Read message from file or use string directly
    text_arg = args[1]
    if os.path.isfile(text_arg):
        with open(text_arg, "r", encoding="utf-8", errors="replace") as f:
            message = f.read().strip()
            message = clean_message(message)
    else:
        message = clean_message(text_arg)

    if post_with_login(topic_id, subject, message, board="57"):
        print("Done.")
    else:
        print("Posting failed. Use browser tools for manual injection.")
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
