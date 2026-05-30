#!/usr/bin/env python3
"""
buddyblocker.py — detect a "buddychain" (randomized 3-5, force 6+ consecutive
ChartBuddy posts in the Wall Observer thread) and issue a rainbow-colored
banner to interrupt the streak.
"""

import sys
import subprocess
import random
from datetime import datetime, timezone
from pathlib import Path
from posting_util import post_with_login, load_credentials

THREAD = "178336"
DEDUP  = "/tmp/buddyblocker_last"
HEX = ["#EF3340", "#E48118", "#F8E41E",
       "#12C167", "#158CE0", "#D021E3"]

def rainbow(text, colors=HEX):
    out = []
    for i, ch in enumerate(text):
        col = colors[i % len(colors)]
        out.append('[COLOR={0}]{1}[/COLOR]'.format(col, ch))
    return ''.join(out)

def _parse_post(b):
    """Extract text from a ChartBuddy post block."""
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
    import re
    url = 'https://bitcointalk.org/index.php?topic={}.{}'.format(THREAD, page)
    html = subprocess.run(['curl', '-s', '-b', "/root/.hermes/bobclawblaw/profile/bt_cookies.txt", url], capture_output=True, text=True).stdout
    if not html: return []
    blocks = re.split(r'<a name="msg[0-9]+">', html)
    posts = []
    for b in blocks[1:]:
        if "110685" not in b.lower(): continue
        am = re.search(r'title="View the profile of ([^"]+)"', b)
        author = am.group(1) if am else "ChartBuddy"
        text = _parse_post(b)
        if text and len(text) > 5: posts.append({'author': author, 'msg': text})
        if len(posts) >= limit: break
    posts.reverse()
    return posts

def has_external_quote(msg):
    import re
    m = re.search(r'Quote from:\s*(\w+)', msg)
    return bool(m and m.group(1) and m.group(1) != 'ChartBuddy')

def find_chain(posts):
    cur = []
    for p in posts:
        if p['author'] == 'ChartBuddy' and not has_external_quote(p['msg']):
            cur.append(p)
            if len(cur) >= 6: return cur
            if len(cur) >= 3 and random.choice([True, False, False]): return cur
            continue
        cur = []
    return None

def build_message(chain, streak=3, test=False):
    lines = [rainbow('[B-B-B-B-Buddyblocker!!!]'), '']
    if test:
        lines.append('DEBUG: Ran with --test flag')
        lines.append('')
    if chain:
        for p in chain: lines.append('  ' + p['msg'][:120])
    return '\n'.join(lines)

def post_buddy(streak=3, test=False):
    body = build_message([], streak, test=test)
    return post_with_login(THREAD, '[B-B-B-B Buddyblocker!]!!!', body)

def main():
    import re
    post_flag = '--post' in sys.argv
    test_flag = '--test' in sys.argv
    posts = get_posts()
    chain = find_chain(posts)
    if chain:
        if post_flag or test_flag: post_buddy(streak=len(chain), test=test_flag)
        else: print(f"Found chain of {len(chain)}")
