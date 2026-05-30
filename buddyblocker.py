#!/usr/bin/env python3
"""
buddyblocker.py — detect a "buddychain" (3+ consecutive ChatBuddy
posts in the Wall Observer thread) and issue a rainbow-colored
banner to interrupt the streak.

Wall Observer: topic=178336, ChatBuddy profile u=110685.

Uses post_wall_observer.post_with_login() for posting so the
formatting and posting pipeline is unified across bobclawblaw.
"""

import os
import re
import sys
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime, timezone

_scripts_dir = Path(__file__).resolve().parent
_project_dir = _scripts_dir.parent
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))

from posting_util import post_with_login

COOKIE = "/root/.hermes/bobclawblaw/profile/bt_cookies.txt"
THREAD = "178336"
DEDUP = "/tmp/buddyblocker_last"
HEX = ["#EF3340", "#E48118", "#F8E41E",
       "#12C167", "#158CE0", "#D021E3"]


def sh(cmd):
    """Execute shell command, return stdout."""
    p = subprocess.run(cmd, shell=True,
                       capture_output=True, timeout=60)
    return p.stdout.decode(errors="replace")


def rainbow(text, colors=HEX):
    """Color a text string by character, outputting BB-style tags.

    Format: [COLOR=#HEX]char[/COLOR]  (each char gets its #color#).
    SMF / BobClawblaw renders these as <span> elements with color.
    """
    out = []
    for i, ch in enumerate(text):
        col = colors[i % len(colors)]
        out.append('[COLOR={0}]{1}[/COLOR]'.format(col, ch))
    return ''.join(out)


def _parse_post(b):
    """Extract text from a ChatBuddy post block."""
    idx_msg = b.find('id="message')
    if idx_msg < 0:
        idx_msg = b.find('id="ignoremessage')
    idx_msg = b.find('>', idx_msg)
    idx_end = b.find('</div>', idx_msg)
    text = b[idx_msg + 1:idx_end]

    text = re.sub(r'<img[^>]*>', '', text)
    text = text.replace('<br />', ' ')
    text = re.sub(r'<a class="ul"[^>]*>[^<]*</a>', '', text)
    text = re.sub(r'<b>[^<]*</b>', '', text)

    text = re.sub(r'^\s*(<br>)?\s*', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_posts(page=714600, limit=200):
    """Fetch ChatBuddy posts from the Wall Observer page."""
    url = 'https://bitcointalk.org/index.php?topic={}.{}'.format(
        THREAD, page)
    html = sh('curl -s -b "{0}" "{1}"'.format(COOKIE, url))
    if not html:
        return []

    blocks = re.split(r'<a name="msg[0-9]+">', html)

    posts = []
    for b in blocks[1:]:
        if "110685" not in b.lower():
            continue

        am = re.search(
            r'title="View the profile of ([^"]+)"', b)
        author = am.group(1) if am else "ChatBuddy"

        text = _parse_post(b)
        if text and len(text) > 5:
            posts.append({'author': author, 'msg': text})
        if len(posts) >= limit:
            break

    posts.reverse()  # newest first
    return posts


def has_external_quote(msg):
    """True if this post explicitly quotes someone other than ChatBuddy."""
    m = re.search(r'Quote from:\s*(\w+)', msg)
    return bool(m and m.group(1) and m.group(1) != 'ChartBuddy')


def find_chain(posts, threshold=3):
    """Find the longest streak of consecutive ChatBuddy posts."""
    best = None
    cur = []

    for p in posts:
        if (p['author'] == 'ChartBuddy'
            and not has_external_quote(p['msg'])):
            cur.append(p)
            continue

        if len(cur) >= threshold:
            if best is None or len(cur) > len(best):
                best = list(cur)
            cur = []
        else:
            cur = [p]

    # Tail
    if len(cur) >= threshold:
        if best is None or len(cur) > len(best):
            best = list(cur)

    return best


def build_message(chain, streak=3):
    """Build BBCode body for the post — single rainbow line, no self-ref."""
    n = max(4, streak)
    # B-chain on its own, then "Buddyblocker!" with full chain below
    header = rainbow('B' * n + 'B' + ' ' + 'Buddyblocker!')
    parts = [header]
    if chain:
        for p in chain:
            parts.append(p['msg'][:120])
    return ' '.join(parts)


def post_buddy(streak=3):
    """Post the Buddyblocker banner via the unified pipeline."""
    body = build_message([], streak)
    subj = '[B-B-B-B Buddyblocker!]!!!'
    return post_with_login(THREAD, subj, body, board="57")


def main():
    post_flag = '--post' in sys.argv
    dry = '--dry' in sys.argv
    streak = 3
    for a in sys.argv[1:]:
        if a.startswith('--streak'):
            parts = a.split('=', 1)
            streak = int(parts[1]) if len(parts) > 1 else 3

    # Dedup (1h threshold)
    last = 0.0
    try:
        last = float(open(DEDUP).read().strip())
    except Exception:
        pass
    now = datetime.now(timezone.utc).timestamp()
    age = now - last
    if 0 < age < 3600:
        print('[buddy] Skip: posted %.0f sec ago.' % age)
        sys.exit(0)

    print('[buddy] Fetching posts...')
    posts = get_posts(714600, limit=200)
    print('[buddy] Found %d posts.' % len(posts))

    if not posts:
        print('[buddy] 0 posts.')
        sys.exit(0)

    chain = find_chain(posts, threshold=streak)
    if not chain:
        print('[buddy] No chain (min=%d).' % streak)
        sys.exit(0)

    count = len(chain)
    print('[buddy] Found a chain of %d!' % count)
    for i, p in enumerate(chain):
        print('  [%d] %s: %s' %
              (i + 1, p['author'], p['msg'][:120]))

    if post_flag:
        post_buddy(streak=count)
    elif dry:
        body = build_message(chain, count)
        with open('/tmp/buddyblocker_out.html', 'w') as fh:
            fh.write(body)
        print('[buddy] Saved to /tmp/buddyblocker_out.html')
    else:
        post_buddy(streak=count)

    sys.exit(0)


if __name__ == '__main__':
    main()
