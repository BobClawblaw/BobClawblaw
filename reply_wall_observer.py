#!/usr/bin/env python3
"""
Reply to a specific Wall Observer post on Bitcointalk (topic 178336).

This script uses the proper Bitcointalk "Quote" button path:
  1. Load the topic page to get a valid sesc token
  2. Hit action=post;quote=<msg_id> to get the form with pre-filled quote
  3. Parse the pre-filled quote from the textarea
  4. Append the reply body and post to post2

Unlike post_wall_observer.py which creates new topics, this script replies
to a specific message with a properly formatted quote block.

Usage:
    reply_wall_observer.py <msg_id> <subject> <message_or_file> [--topic TOPIC_ID]
"""

import os
import re
import sqlite3
import sys
import time
import json
import subprocess
import urllib.parse

DB_PATH = "/root/.hermes/bobclawblaw/wall_posts.db"
COOKIE_PATH = "/root/.hermes/bobclawblaw/profile/bt_cookies.txt"
CREDS_PATH = "/root/.hermes/bobclawblaw/profile/credentials.json"
WALL_OBSERVER_TOPIC = "178336"
WALL_OBSERVER_BOARD = "57"


def _run_cmd(cmd):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=30
        )
        return result.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"CMD ERROR: {e} | CMD: {cmd}")
        return ""


def load_credentials():
    with open(CREDS_PATH, "r") as f:
        data = json.load(f)
    bits = data["bitcointalk"]
    return {
        "username": bits["username"],
        "password": bits["password"],
        "ccode": bits["captcha_bypass_url"],
    }


def is_logged_in():
    if not os.path.exists(COOKIE_PATH):
        return False
    out = _run_cmd(
        f'curl -s -b {COOKIE_PATH} "https://bitcointalk.org/index.php?action=profile"'
    )
    return "Logout" in out or "Your profile" in out


def perform_login(creds):
    import hashlib

    ccode_val = creds["ccode"]
    if isinstance(ccode_val, str) and "action=login" in ccode_val:
        login_url = ccode_val
    else:
        login_url = f"https://bitcointalk.org/index.php?action=login;ccode={ccode_val}"

    login2_url = re.sub(r"action=login;", "action=login2;", login_url)

    _run_cmd(f'curl -s -c {COOKIE_PATH} "{login_url}"')
    page = _run_cmd(f'curl -s -b {COOKIE_PATH} "{login_url}"')

    cookielength_m = re.search(r'name="cookielength" value="([^"]+)"', page)
    cookielength = cookielength_m.group(1) if cookielength_m else "60"

    try:
        cookie_text = open(COOKIE_PATH, "r", encoding="utf-8", errors="ignore").read()
    except Exception as e:
        print(f"FAILED: Could not read cookie jar for sessionid: {e}")
        return False

    sess_m = re.search(r"\tsessionid\t([^\t\r\n]+)", cookie_text)
    sessionid = sess_m.group(1) if sess_m else None
    if not sessionid:
        print("FAILED: Could not find sessionid cookie")
        return False

    inner = hashlib.sha1(
        (creds["username"].lower() + creds["password"]).encode("utf-8")
    ).hexdigest()
    hash_passwrd = hashlib.sha1((inner + sessionid).encode("utf-8")).hexdigest()

    post_data = (
        f"user={urllib.parse.quote_plus(creds['username'])}"
        f"&passwrd={urllib.parse.quote_plus(creds['password'])}"
        f"&hash_passwrd={hash_passwrd}"
        f"&totp_value="
        f"&cookielength={cookielength}"
    )

    _run_cmd(
        f'curl -s -b {COOKIE_PATH} -c {COOKIE_PATH} -d "{post_data}" "{login2_url}"'
    )

    profile = _run_cmd(f'curl -s -b {COOKIE_PATH} "https://bitcointalk.org/index.php?action=profile"')
    if "Logout" in profile or "Welcome" in profile:
        print("Login successful. Cookies saved.")
        return True

    print("Login failed.")
    return False


def clean_subject(subject):
    """Escape messy Unicode in the subject to clean ASCII."""
    replacements = {
        "\u2013": "-",
        "\u2014": "-",
        "\u2011": "-",
        "\u00ad": "-",
        "\u202f": " ",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u00b7": " ",
        "\u2039": "<",
        "\u203a": ">",
    }
    for old, new in replacements.items():
        subject = subject.replace(old, new)
    return subject


def clean_message(message):
    """Remove non-printable, non-ASCII from the message body."""
    message = message.replace("\u20bf", "B")
    message = message.replace("\u09f9", "B")
    return "".join(
        c
        for c in message
        if 32 <= ord(c) <= 126 or c in ("\n", "\r", "\t")
    )


def lookup_post(msg_id):
    """Look up a post by msg_id in wall_posts.db."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT msg_id, author, author_uid, date, is_chart, subject, body "
            "FROM posts WHERE msg_id = ?",
            (str(msg_id),),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        return dict(row)
    except Exception as e:
        print(f"DB ERROR: {e}")
        return None


def get_sesc_token(topic_id):
    """Load the topic page to get a valid sesc token for quote links."""
    url = f"https://bitcointalk.org/index.php?topic={topic_id};start=0"
    page = _run_cmd(f'curl -s -b {COOKIE_PATH} "{url}"')
    m = re.search(r'sesc=([a-f0-9]+)', page)
    if m:
        return m.group(1)
    return None


def fetch_quote_form(msg_id, topic_id, sesc):
    """
    Hit the action=post;quote=<msg_id> endpoint to get the post form
    with the quote pre-filled in the textarea.

    Returns (textarea_content, sc, seqnum) or (None, None, None) on failure.
    """
    quote_url = (
        f"https://bitcointalk.org/index.php"
        f"?action=post;quote={msg_id};topic={topic_id}.0;sesc={sesc}"
    )
    post_form = _run_cmd(f'curl -s -b {COOKIE_PATH} "{quote_url}"')

    # Check for error page
    if "An Error Has Occurred" in post_form or "Session verification failed" in post_form:
        print(f"ERROR: Session verification failed when fetching quote form.")
        print("The sesc token may have expired. Try again.")
        return None, None, None

    # Extract the pre-filled textarea content
    textarea_m = re.search(
        r'<textarea class="editor"[^>]*name="message"[^>]*>(.*?)</textarea>',
        post_form,
        re.DOTALL,
    )
    textarea_content = textarea_m.group(1) if textarea_m else None

    # Extract sc and seqnum
    sc_m = re.search(r'name="sc" value="([^"]+)"', post_form)
    seq_m = re.search(r'name="seqnum" value="([^"]+)"', post_form)
    topic_m = re.search(r'name="topic" value="([^"]+)"', post_form)
    board_m = re.search(r'name="board" value="([^"]+)"', post_form)

    sc = sc_m.group(1) if sc_m else None
    seqnum = seq_m.group(1) if seq_m else None
    form_topic = topic_m.group(1) if topic_m else None
    form_board = board_m.group(1) if board_m else "57"

    return textarea_content, sc, seqnum, form_topic, form_board


def post_reply(form_topic, form_board, sc, seqnum, subject, message, target_msg_id):
    """POST the reply to post2 endpoint."""
    post_url = f"https://bitcointalk.org/index.php?action=post2;start=0;board={form_board}"
    payload = {
        "topic": form_topic,
        "subject": subject,
        "message": message,
        "post": "Post",
        "notify": "0",
        "do_watch": "0",
        "goback": "1",
        "additional_options": "0",
        "sc": sc,
        "seqnum": seqnum,
        "reply_to_msg": target_msg_id,
    }
    encoded = urllib.parse.urlencode(payload)
    out = subprocess.run(
        f'curl -i -s -b {COOKIE_PATH} -d "{encoded}" "{post_url}"',
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    out_text = out.stdout

    loc_match = re.search(r"(?i)location:\s*([^\r\n]+)", out_text)
    if loc_match:
        loc = loc_match.group(1)
        if f"topic={form_topic}" in loc:
            print("SUCCESS: Reply posted successfully!")
            return True
        elif "#new" in loc:
            print("SUCCESS: Reply redirected to #new (standard SMF behavior).")
            return True
        print(f"REPLY FAILED: Redirected to unexpected URL: {loc}")
        return False

    if "90 seconds" in out_text:
        print("REPLY FAILED: Cooldown limit (90 seconds) triggered.")
    else:
        print("REPLY FAILED: Response did not indicate success.")
    return False


def main(argv=None):
    args = argv or sys.argv[1:]
    if len(args) < 3:
        print("Usage: reply_wall_observer.py <msg_id> <subject> <message_or_file> [--topic TOPIC_ID]")
        sys.exit(1)

    target_msg_id = args[0]
    subject = clean_subject(args[1])

    topic_id = WALL_OBSERVER_TOPIC
    for i in range(2, len(args)):
        if args[i] == "--topic" and i + 1 < len(args):
            topic_id = args[i + 1]
            break

    # Read message body from file or string
    text_arg = args[2]
    if os.path.isfile(text_arg):
        with open(text_arg, "r", encoding="utf-8", errors="replace") as f:
            message_body = f.read().strip()
            message_body = clean_message(message_body)
    else:
        message_body = clean_message(text_arg)

    # Step 1: Login
    if not is_logged_in():
        creds = load_credentials()
        if not perform_login(creds):
            print("CRITICAL: Login failed.")
            sys.exit(1)

    # Step 2: Get sesc token from topic page
    sesc = get_sesc_token(topic_id)
    if not sesc:
        print("ERROR: Could not get sesc token from topic page.")
        print("The topic page may not be accessible with current cookies.")
        sys.exit(1)
    print(f"Got sesc token: {sesc[:12]}...")

    # Step 3: Fetch the post form with the quote pre-filled
    print(f"Fetching quote for msg {target_msg_id}...")
    result = fetch_quote_form(target_msg_id, topic_id, sesc)
    if result[0] is None:
        print("ERROR: Could not fetch quote form.")
        sys.exit(1)

    textarea_content, sc, seqnum, form_topic, form_board = result

    print(f"Form topic: {form_topic}, Form board: {form_board}")
    print(f"sc: {sc[:12]}..., seqnum: {seqnum}")

    # Step 4: Combine the pre-filled quote with the reply body
    if textarea_content:
        # The textarea already has [quote=...][/quote] pre-filled
        full_message = textarea_content + "\n\n" + message_body
        print(f"Pre-filled quote found in form ({len(textarea_content)} chars).")
    else:
        # Fallback: build the quote from DB data
        post = lookup_post(target_msg_id)
        if post:
            post_link = f"https://bitcointalk.org/index.php?msg={post['msg_id']}"
            post_date = post["date"] if post["date"] else "unknown date"
            full_message = (
                f"[quote={post['author']}]\n"
                f"{clean_message(post['body'])}\n"
                f"--Posted: {post_date} (Msg #{post['msg_id']})\n"
                f"[/quote]\n\n"
                f"{message_body}"
            )
            print(f"Built fallback quote for {post['author']}.")
        else:
            print(f"ERROR: Could not find msg {target_msg_id} in DB for fallback.")
            sys.exit(1)

    # Step 5: Post the reply
    print("Posting reply...")
    success = post_reply(form_topic, form_board, sc, seqnum, subject, full_message, target_msg_id)

    if success:
        print("Done.")
    else:
        print("Reply posting failed.")
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
