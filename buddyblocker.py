#!/usr/bin/env python3
"""
buddyblocker.py — detect a "buddychain" (3+ consecutive ChatBuddy
posts in the Wall Observer thread) and issue a rainbow-colored
banner to interrupt the streak.

Wall Observer: topic=178336, ChatBuddy profile u=110685.

Uses firecrawl + BeautifulSoup for HTML extraction (matching the
wallobindexer), and post_wall_observer.post_with_login() for posting
so the formatting and posting pipeline is unified across bobclawblaw.
"""

import os
import re
import sys
import subprocess
import random
from pathlib import Path
from datetime import datetime, timezone
from bs4 import BeautifulSoup

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
KB = Path("/root/.hermes/bobclawblaw/knowledge_base/wallobserver/profiles")
PROFILE_JSON = KB / "-btc-" / "profile.json"
LATEST_POST_ID_FILE = KB / "-btc-" / "latest_post_id.txt"

FIRECRAWL_URL = "http://localhost:3002/v1/scrape"


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


def _get_current_page():
    """Determine which page BuddyBlocker should scrape.

    Uses WALLOBSERVER's latest post from the KB. Falls back to
    a cached fallback of 714600.
    """
    # 1. Try to read the latest post ID from KB
    try:
        latest = int(LATEST_POST_ID_FILE.read_text().strip())
    except Exception:
        latest = 714600

    # 2. Use the WALLOBSERVER KB directly if available
    if PROFILE_JSON.exists():
        try:
            data = __import__('json').load(open(PROFILE_JSON))
            if data.get('posts'):
                posts = data['posts']
                cb_posts = [p for p in posts
                            if 'ChartBuddy' in p.get('author', '')]
                if cb_posts:
                    cb_posts.sort(key=lambda p: p['page'])
                    latest_key_post = cb_posts[-1]['page']
                    return latest_key_post
        except Exception:
            pass

    # 3. Fallback: use page from latest post ID
    return latest // 20 if latest > 100000 else 35800


def fetch_firecrawl(pn):
    """Fetch a page via firecrawl, returning HTML."""
    try:
        url = 'https://bitcointalk.org/index.php?topic=%s.%d' % (
            THREAD, int(pn))
        res = subprocess.Popen(
            ['curl', '-s', '-X', 'POST', '-d',
             '{"url": "%s", "formats": ["html"]}' % url,
             FIRECRAWL_URL],
            stdout=subprocess.PIPE, timeout=60)
        out = res.communicate(timeout=30)[0]
        return out.decode(errors='replace')
    except Exception:
        return None


def fetch_curl_page(pn):
    """Fetch a page via raw curl."""
    url = 'https://bitcointalk.org/index.php?topic=%s.%d' % (
        THREAD, int(pn))
    return sh('curl -s -b "%s" "%s"' % (COOKIE, url))


def extract_posts_firecrawl(html, limit=None):
    """Extract ChatBuddy posts from firecrawl HTML."""
    soup = BeautifulSoup(html, "html.parser")
    post_divs = soup.find_all("div", class_="post")
    posts = []
    seen_keys = set()
    cb_uid = "110685"

    for post_div in post_divs:
        text = post_div.get_text(" ")
        text = re.sub(r"\r\n", " ", text)
        text = " ".join(text.split())

        # Check if this is a CB post (uid in profile link)
        author_links = []
        for a in post_div.find_all("a", href=lambda h: h and
                                   "action=profile" in str(h)):
            author_links.append(a)
        for anc in post_div.parents:
            for a in anc.find_all("a",
                                   href=lambda h: h and
                                   "action=profile" in str(h)):
                if a not in author_links:
                    author_links.append(a)

        if not author_links:
            continue
        href = str(author_links[0].get('href', ''))
        uid_m = re.search(r'u=(\d+)', href)
        if not uid_m or uid_m.group(1) != cb_uid:
            continue

        author = author_links[0].get_text(strip=True) or 'ChartBuddy'
        post_key = '%s-%d' % (author, len(posts))
        if post_key in seen_keys:
            continue
        seen_keys.add(post_key)

        if text and len(text) > 5:
            posts.append({'author': author, 'msg': text})
            if limit and len(posts) >= limit:
                break

    return posts


def extract_posts_curl(html, limit=None):
    """Extract ChatBuddy posts from curl HTML (legacy fallback)."""
    blocks = re.split(r'<a name="msg[0-9]+">', html)
    posts = []

    for b in blocks[1:]:
        if "110685" not in b.lower():
            continue

        am = re.search(
            r'title="View the profile of ([^"]+)"', b)
        author = am.group(1) if am else 'ChartBuddy'

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

        if text and len(text) > 5:
            posts.append({'author': author, 'msg': text})
            if limit and len(posts) >= limit:
                break

    posts.reverse()  # newest first
    return posts


def get_posts(limit=200):
    """Fetch ChatBuddy posts, preferring firecrawl with curl fallback."""
    page = _get_current_page()
    print('[buddy] Trying firecrawl on page %d...' % page)

    html = fetch_firecrawl(page)
    if html:
        posts = extract_posts_firecrawl(html, limit=limit)
        if len(posts) > 3:
            print('[buddy] Firecrawl: Found %d posts.' % len(posts))
            return posts

    # Fallback to raw curl
    print('[buddy] Trying raw curl...')
    html = fetch_curl_page(page)
    if html:
        posts = extract_posts_curl(html, limit=limit)
        print('[buddy] Curl: Found %d posts.' % len(posts))
        return posts

    return []


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
            cur = [p]
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
    posts = get_posts(limit=200)
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
