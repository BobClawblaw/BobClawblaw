#!/usr/bin/env python3
"""Update the date column for posts that are missing it.

Batches anchor pages (100 per page) and extracts timestamps from all
td_headerandpost elements in each page at once. Much faster than
one-fetch-per-post.
"""

import re
import sqlite3
import subprocess
import time
from bs4 import BeautifulSoup

DB_PATH = "/root/.hermes/bobclawblaw/wall_posts.db"
COOKIE = "/root/.hermes/bobclawblaw/profile/bt_cookies.txt"
TOPIC = 178336
REQUEST_TIMEOUT_S = 60
MAX_RETRIES = 3


def fetch_html(url, retries=MAX_RETRIES):
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(1.5 ** attempt)
        res = subprocess.run(
            ["curl", "-s", "-X", "POST", "-b", COOKIE, url,
             "-w", "\n%{http_code}"],
            capture_output=True, timeout=REQUEST_TIMEOUT_S,
        )
        m = re.search(rb"\n(\d{3})$", res.stdout)
        if m:
            http_code = int(m.group(1))
            body = res.stdout[:m.start()]
            if http_code == 200 and len(body) > 200:
                return body.decode("utf-8", errors="ignore")
    return None


def extract_timestamps_from_page(html):
    """Extract (msg_id, timestamp) from a single paginated topic page.

    Walks all td_headerandpost blocks and extracts the date before
    Quote from:/# markers.
    """
    results = {}
    soup = BeautifulSoup(html, "html.parser")

    anchor_tags = soup.find_all("a", href=True)
    # Group anchors by the topic anchor value (page_num)
    # Each page has multiple posts, each with its own msg anchor and td_headerandpost

    # Find all post tables (td_headerandpost is inside them)
    post_tds = soup.find_all("td", class_="td_headerandpost")
    if not post_tds:
        return results

    # Find msg anchors near these post tables
    msg_anchors = soup.find_all("a", attrs={"name": re.compile(r"^msg\d+$")})
    msg_ids = [int(a.get("name").replace("msg", "")) for a in msg_anchors]

    for td in post_tds:
        all_strings = list(td.strings)
        date_parts = []
        for s in all_strings:
            s_stripped = s.strip()
            if not s_stripped:
                continue
            if s_stripped.startswith("#") or s_stripped.startswith("Quote from:"):
                break
            if (re.search(r'\b(AM|PM)\b', s_stripped) or
                    re.search(r'\b20\d{2}\b', s_stripped) or
                    s_stripped.lower() == "today"):
                date_parts.append(s_stripped)
        if date_parts:
            ts = " ".join(date_parts)
            # Associate this timestamp with the nearest msg anchor
            if msg_ids:
                results[msg_ids[0]] = ts
                msg_ids.pop(0)

    return results


def main():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    missing = conn.execute(
        "SELECT msg_id, page_num FROM posts WHERE date = '' ORDER BY page_num"
    ).fetchall()

    print(f"Total posts: {total}, missing dates: {len(missing)}")
    if not missing:
        print("Nothing to do.")
        conn.close()
        return

    # Group by unique page_num to batch fetch
    page_nums = sorted(set(pn for _, pn in missing))
    print(f"Unique page_nums to fetch: {len(page_nums)}")

    updated = 0
    errors = 0
    timestamps_found = {}

    for pn in page_nums:
        url = f"https://bitcointalk.org/index.php?topic={TOPIC}.{int(pn)}"
        html = fetch_html(url)
        if html:
            found = extract_timestamps_from_page(html)
            timestamps_found[int(pn)] = found
            print(f"  page {pn}: found {len(found)} timestamps")
        else:
            errors += 1
            print(f"  page {pn}: fetch failed")
        time.sleep(0.25)

    # Apply timestamps to the DB
    for msg_id, page_num in missing:
        pn = int(page_num)
        if pn in timestamps_found:
            msg_ts = timestamps_found[pn].get(msg_id)
            if msg_ts:
                conn.execute(
                    "UPDATE posts SET date = ? WHERE msg_id = ?",
                    (msg_ts, msg_id)
                )
                updated += 1

    conn.commit()
    print(f"Done. updated={updated} errors={errors}")
    remaining = conn.execute("SELECT COUNT(*) FROM posts WHERE date = ''").fetchone()[0]
    print(f"Remaining without dates: {remaining}")
    conn.close()


if __name__ == "__main__":
    main()
