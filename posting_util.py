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
    # creds['ccode'] is not consistently shaped across runs.
    # - sometimes it's just the ccode token
    # - sometimes it's a full captcha-bypass URL (action=login;ccode=...)
    ccode_val = creds['ccode']
    if isinstance(ccode_val, str) and 'action=login' in ccode_val:
        login_url = ccode_val
    else:
        login_url = f"https://bitcointalk.org/index.php?action=login;ccode={ccode_val}"

    login2_url = login_url
    # Most SMF login pages use action=login2;... for the actual POST.
    login2_url = re.sub(r'action=login;', 'action=login2;', login2_url)

    # Load login page to ensure we have a fresh sessionid in COOKIE_PATH.
    _run_cmd(f'curl -s -c {COOKIE_PATH} "{login_url}"')
    page = _run_cmd(f'curl -s -b {COOKIE_PATH} "{login_url}"')

    # Extract cookielength if present; SMF uses it in the login form.
    cookielength_m = re.search(r'name="cookielength" value="([^"]+)"', page)
    cookielength = cookielength_m.group(1) if cookielength_m else '60'

    # Extract sessionid from cookie jar.
    try:
        cookie_text = open(COOKIE_PATH, 'r', encoding='utf-8', errors='ignore').read()
    except Exception as e:
        print(f"FAILED: Could not read cookie jar for sessionid: {e}")
        return False

    # Netscape cookie format: ...\tsessionid\t<value>
    sess_m = re.search(r'\tsessionid\t([^\t\r\n]+)', cookie_text)
    sessionid = sess_m.group(1) if sess_m else None
    if not sessionid:
        print("FAILED: Could not find sessionid cookie; cannot compute hash_passwrd")
        return False

    # Newer SMF login uses hash_passwrd (client-side) not formhash.
    # hash_passwrd = sha1( sha1(lower(username)+password) + sessionid )
    import hashlib

    inner = hashlib.sha1((creds['username'].lower() + creds['password']).encode('utf-8')).hexdigest()
    hash_passwrd = hashlib.sha1((inner + sessionid).encode('utf-8')).hexdigest()

    # Try login2 via POST.
    import urllib.parse

    post_data = (
        f"user={urllib.parse.quote_plus(creds['username'])}"
        f"&passwrd={urllib.parse.quote_plus(creds['password'])}"
        f"&hash_passwrd={hash_passwrd}"
        f"&totp_value="
        f"&cookielength={cookielength}"
    )

    out = _run_cmd(
        f"curl -s -b {COOKIE_PATH} -c {COOKIE_PATH} -d \"{post_data}\" '{login2_url}'"
    )

    # Validate via profile (Logout link should be present when authenticated).
    profile = _run_cmd(f'curl -s -b {COOKIE_PATH} "https://bitcointalk.org/index.php?action=profile"')
    if "Logout" in profile or "Welcome" in profile:
        print("Login successful. Cookies saved.")
        return True

    # Backwards compatibility: if formhash exists, try the old flow too.
    m = re.search(r'name="formhash" value="([^"]+)"', page)
    if m:
        formhash = m.group(1)
        post_data_old = f"user={creds['username']}&passwrd={creds['password']}&formhash={formhash}"
        _run_cmd(
            f"curl -s -b {COOKIE_PATH} -c {COOKIE_PATH} -d \"{post_data_old}\" 'https://bitcointalk.org/index.php?action=login'"
        )
        profile2 = _run_cmd(f'curl -s -b {COOKIE_PATH} "https://bitcointalk.org/index.php?action=profile"')
        if "Logout" in profile2 or "Welcome" in profile2:
            print("Login successful (legacy formhash). Cookies saved.")
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
