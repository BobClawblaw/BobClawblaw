"""Shared posting utility for post_wall_observer.py and buddyblocker.py."""

import re
import subprocess

COOKIE_PATH = "/root/.hermes/bobclawblaw/profile/bt_cookies.txt"
CREDS_PATH = "/root/.hermes/bobclawblaw/profile/credentials.json"


# ---------- helpers ----------

def _run_cmd(cmd):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=30
        )
        return result.stdout.decode('utf-8', errors='replace')
    except Exception as e:
        print(f"CMD ERROR: {e} | CMD: {cmd}")
        return ""


# ------------------------------------------------------------------
# credentials / login  (same logic that was in post_wall_observer)
# ------------------------------------------------------------------

def load_credentials():
    import json
    with open(CREDS_PATH, "r") as f:
        data = json.load(f)
    bits = data["bitcointalk"]
    return {
        "username": bits["username"],
        "password": bits["password"],
        "ccode": bits["captcha_bypass_url"],
        "thread": bits.get("wall_observer_thread", "178336"),
    }


def is_logged_in():
    """Check cookie auth via profile page."""
    if not __import__("os").path.exists(COOKIE_PATH):
        return False
    out = _run_cmd(
        f'curl -s -b {COOKIE_PATH} "https://bitcointalk.org/index.php?action=profile"'
    )
    return "Logout" in out or "Your profile" in out


def perform_login(creds):
    login_url = f"https://bitcointalk.org/index.php?action=login;ccode={creds['ccode']}"
    _run_cmd(f'curl -s -c {COOKIE_PATH} "{login_url}"')
    page = _run_cmd(f'curl -s -b {COOKIE_PATH} "{login_url}"')
    m = re.search(r'name="formhash" value="([^"]+)"', page)
    if not m:
        print("FAILED: Could not find formhash on login page")
        return False
    formhash = m.group(1)
    post_data = f"user={creds['username']}&passwrd={creds['password']}&formhash={formhash}"
    out = _run_cmd(
        f'curl -s -b {COOKIE_PATH} -c {COOKIE_PATH} -d "{post_data}" "https://bitcointalk.org/index.php?action=login"'
    )
    if "Logout" in out or "Welcome" in out:
        print("Login successful. Cookies saved.")
        return True
    print("Login failed.")
    return False


# ------------------------------------------------------------------
# token retrieval   (was get_post_tokens)
# ------------------------------------------------------------------

def get_post_tokens(topic_id, board="57"):
    url = f"https://bitcointalk.org/index.php?action=post;topic={topic_id}"
    page = _run_cmd(f'curl -s -b {COOKIE_PATH} "{url}"')
    sc = re.search(r'name="sc" value="([^"]+)"', page)
    seqnum = re.search(r'name="seqnum" value="([^"]+)"', page)
    num_replies = re.search(r'name="num_replies" value="([^"]+)"', page)
    return (
        sc.group(1) if sc else None,
        seqnum.group(1) if seqnum else None,
        num_replies.group(1) if num_replies else None,
    )


# ------------------------------------------------------------------
# the shared post_message (was a method in post_wall_observer)
# ------------------------------------------------------------------

def post_message(topic_id, subject, message, board="57"):
    sc, seqnum, num_replies = get_post_tokens(topic_id, board)
    if not sc or not seqnum:
        print("FAILED: Could not retrieve sc or seqnum tokens for posting")
        return False

    import urllib.parse, subprocess as sp
    post_url = f"https://bitcointalk.org/index.php?action=post2;start=0;board={board}"
    payload = {
        "topic": topic_id,
        "subject": subject,
        "message": message,
        "post": "Post",
        "notify": "0",
        "do_watch": "0",
        "goback": "1",
        "additional_options": "0",
        "sc": sc,
        "seqnum": seqnum,
    }
    if num_replies:
        payload["num_replies"] = num_replies
    encoded = urllib.parse.urlencode(payload)
    out = sp.run(
        f'curl -i -s -b {COOKIE_PATH} -d "{encoded}" "{post_url}"',
        shell=True, capture_output=True, text=True, timeout=30,
    )
    out_text = out.stdout

    # extract location redirect (case-insensitive)
    loc_match = re.search(r'(?i)location:\s*([^\r\n]+)', out_text)
    if loc_match:
        loc = loc_match.group(1)
        if f"topic={topic_id}" in loc:
            print("SUCCESS: Post submitted successfully!")
            return True
        elif "#new" in loc:
            print("SUCCESS: Post redirected to #new (standard SMF behavior).")
            return True
        print(f"POST FAILED: Redirected to unexpected URL: {loc}")
        return False

    if "90 seconds" in out_text:
        print("POST FAILED: Cooldown limit (90 seconds) triggered.")
    else:
        print("POST FAILED: Response did not indicate success.")
    return False


# ------------------------------------------------------------------
# convenience: login-then-POST
# ------------------------------------------------------------------

def post_with_login(topic_id, subject, message, board="57"):
    _ = load_credentials()
    if not is_logged_in():
        if not perform_login(load_credentials()):
            print("CRITICAL: Login failed.")
            return False
    return post_message(topic_id, subject, message, board)
