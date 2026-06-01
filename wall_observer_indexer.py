#!/usr/bin/env python3
"""Wall Observer indexer — truly incremental.

Indexes posts from Bitcointalk topic=178336 into a local SQLite DB.
Incrementality is via an _seen(msg_id) table.

This script used to be broken (parser had duplicate page() defs + inserts
mismapped columns). Rewritten to use the same robust SMF heuristics as the
working wallobindexer archive script.
"""

from __future__ import annotations

__version__ = "0.1.0"

import argparse
import os
import re
import json
import sqlite3
import subprocess
from typing import List, Tuple, Optional

from bs4 import BeautifulSoup

DB_PATH = "/root/.hermes/bobclawblaw/wall_posts.db"
COOKIE = "/root/.hermes/bobclawblaw/profile/bt_cookies.txt"
KB_PATH = "/root/.hermes/bobclawblaw/knowledge_base/wallobserver/profiles"
STATE_PATH = "/root/.hermes/bobclawblaw/knowledge_base/idx.json"

TOPIC = 178336
BT_UID = 3749923

WINDOW = 100


def msg_ids_on_page(html: str) -> List[int]:
    """Extract msg ids from raw topic page HTML (author/subject independent)."""
    return [int(m.group(1)) for m in re.finditer(r'<a\s+name="msg(\d+)">', html)]



def curl_to_stdout(url: str) -> bytes:
    # Bitcointalk is happier with POST in some environments.
    # If this ever fails, switch back to GET.
    res = subprocess.run(
        ["curl", "-s", "-X", "POST", "-b", COOKIE, url],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return res.stdout


def topic_anchors(mode: str = "stdout") -> List[int]:
    # On SMF topic pages, pagination anchors use topic=TOPIC.<n>
    # where <n> is the page start index.
    url = f"https://bitcointalk.org/index.php?topic={TOPIC}"
    raw = curl_to_stdout(url)
    html = raw.decode("utf-8", errors="ignore")

    anchors = set()
    for a in BeautifulSoup(html, "html.parser").find_all(
        "a",
        href=True,
    ):
        h = a.get("href") or ""
        if f"topic={TOPIC}." not in h:
            continue
        m = re.search(rf"topic={TOPIC}\.(\d+)", h)
        if m:
            anchors.add(int(m.group(1)))

    print(f"[DIAG {TOPIC}] anchors={len(anchors)} (mode={mode})")
    return sorted(anchors)


def _clean_body_from_html(content_html: str) -> str:
    # Remove quoted blocks and images.
    content_html = re.sub(r"\[QUOTE=[^\]]*\s*\[\/QUOTE\]", "", content_html, flags=re.DOTALL)
    content_html = re.sub(r"<blockquote[^>]*>.*?</blockquote>", "", content_html, flags=re.DOTALL | re.I)
    content_html = re.sub(r"<img[^>]+>", "", content_html, flags=re.I)

    # Convert to text.
    soup = BeautifulSoup(content_html, "html.parser")
    text = soup.get_text("\n")

    # Normalize.
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(Quote|quoted) from:", line, re.I):
            continue
        lines.append(line)

    cleaned = "\n\n".join(lines).strip()
    return cleaned


def parse_posts(html: str, page_num: int) -> List[Tuple[int, str, int, str, int, str, str, str]]:
    """Parse one SMF topic page into DB rows.

    We chunk by <a name="msg<id>"> anchors. In this SMF HTML, the author/subject
    elements are often *not* in the same parent table as the profile link, so
    table-based heuristics miss data.

    Returns rows for DB insertion:
      (msg_id, author, author_uid, date, page_num, is_chart, subject, body)
    """
    anchors: List[Tuple[int, int]] = [
        (int(m.group(1)), m.start())
        for m in re.finditer(r'<a\s+name="msg(\d+)">', html)
    ]
    if not anchors:
        return []

    out: List[Tuple[int, str, int, str, int, str, str, str]] = []
    for i, (msg_id, start) in enumerate(anchors):
        end = anchors[i + 1][1] if i + 1 < len(anchors) else len(html)
        chunk = html[start:end]

        # Author + uid (profile link in the chunk)
        am = re.search(
            r'<a\s+href="[^"]*profile;u=(\d+)[^"]*"[^>]*>\s*(.*?)\s*</a>',
            chunk,
            re.I | re.S,
        )
        if not am:
            continue
        uid = int(am.group(1))
        author = BeautifulSoup(am.group(2), "html.parser").get_text(" ", strip=True)
        if not author or len(author) < 2:
            continue
        if author.lower() == "ignore":
            continue
        if author.isdigit():
            continue

        # Subject (SMF often wraps it as <div class="subject"><a ...>...</a></div>)
        soup_chunk = BeautifulSoup(chunk, "html.parser")
        subj_div = soup_chunk.find("div", class_="subject")
        if subj_div:
            subject = subj_div.get_text(" ", strip=True)
        else:
            # Fallback: older/alternate markup where subject is directly an <a class="subject">...
            sm = re.search(
                r'<a[^>]*class="subject"[^>]*>(.*?)</a>',
                chunk,
                re.I | re.S,
            )
            if sm:
                subject = BeautifulSoup(sm.group(1), "html.parser").get_text(" ", strip=True)
            else:
                subject = ""

        # Body
        post_div = soup_chunk.find("div", class_="post")
        if post_div:
            content_html = post_div.decode_contents()
        else:
            td_post = soup_chunk.find("td", class_=re.compile(r"\bpost\b"))
            content_html = td_post.decode_contents() if td_post else ""

        body = _clean_body_from_html(content_html)[:4000] if content_html else ""
        if len(body) < 20:
            continue

        # Timestamp (best-effort; sometimes empty)
        timestamp = ""
        st = soup_chunk.find("span", class_="smalltext")
        if st:
            t = st.get_text(" ", strip=True)
            # Avoid legends-only content; keep if it looks like a dated post.
            if "Posted" in t or re.search(r"\b(AM|PM)\b", t) or re.search(r"\b20\d{2}\b", t):
                timestamp = t

        # Chart detection (conservative)
        blob = (subject + "\n" + body).lower()
        is_chart = "1" if ("chartbuddy" in blob or "chart" in blob) else "0"

        out.append((msg_id, author, uid, timestamp, int(page_num), is_chart, subject[:200], body))

    return out


def rebuild(conn: sqlite3.Connection) -> None:
    os.makedirs(KB_PATH, exist_ok=True)
    for author, uid in conn.execute("SELECT author, author_uid FROM posts GROUP BY author"):
        d = os.path.join(KB_PATH, author.lower())
        os.makedirs(d, exist_ok=True)
        cnt = conn.execute("SELECT COUNT(*) FROM posts WHERE author=?", (author,)).fetchone()[0]
        with open(os.path.join(d, "profile.json"), "w") as f:
            json.dump({"name": author, "uid": uid, "count": cnt}, f, indent=2)


def run(
    max_anchors: Optional[int] = None,
    prune_missing: bool = False,
    prune_anchors: Optional[int] = None,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS posts (
        msg_id INTEGER PRIMARY KEY,
        author TEXT,
        author_uid INTEGER,
        date TEXT,
        page_num INTEGER,
        is_chart TEXT,
        subject TEXT,
        body TEXT
    )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS _seen (msg_id INTEGER PRIMARY KEY)")
    conn.commit()

    anchors = topic_anchors(mode="stdout")
    if not anchors:
        print("No anchors on topic page.")
        conn.close()
        return

    if max_anchors is None:
        window = anchors[-(WINDOW * 2) :]
    else:
        window = anchors[-max_anchors:]

    prune_window = window
    if prune_missing and prune_anchors is not None:
        prune_window = anchors[-prune_anchors:]

    prune_window = sorted(set(prune_window))

    print(f"-- processing anchors {min(window)}..{max(window)} (count={len(window)}) --")

    inserted = 0
    for anc in window:
        url = f"https://bitcointalk.org/index.php?topic={TOPIC}.{int(anc)}"
        html = curl_to_stdout(url).decode("utf-8", errors="ignore")

        rows = parse_posts(html, page_num=int(anc))
        for row in rows:
            msg_id = row[0]
            if conn.execute("SELECT 1 FROM _seen WHERE msg_id=?", (msg_id,)).fetchone():
                continue

            conn.execute(
                """INSERT INTO posts
                (msg_id, author, author_uid, date, page_num, is_chart, subject, body)
                VALUES (?,?,?,?,?,?,?,?)""",
                row,
            )
            conn.execute("INSERT INTO _seen (msg_id) VALUES (?)", (msg_id,))
            inserted += 1

    if prune_missing and prune_window:
        print(f"-- pruning missing posts in anchors {min(prune_window)}..{max(prune_window)} (count={len(prune_window)}) --")
        present_ids = set()
        for anc in prune_window:
            url = f"https://bitcointalk.org/index.php?topic={TOPIC}.{int(anc)}"
            html = curl_to_stdout(url).decode("utf-8", errors="ignore")
            present_ids.update(msg_ids_on_page(html))

        cand = conn.execute(
            f"SELECT msg_id FROM posts WHERE page_num IN ({','.join(['?'] * len(prune_window))})",
            tuple(int(x) for x in prune_window),
        ).fetchall()

        to_delete = [r[0] for r in cand if r[0] not in present_ids]

        if to_delete:
            conn.executemany("DELETE FROM posts WHERE msg_id=?", [(mid,) for mid in to_delete])
            conn.executemany("DELETE FROM _seen WHERE msg_id=?", [(mid,) for mid in to_delete])
            conn.commit()
            print(f"[prune] Deleted {len(to_delete)} missing posts")
        else:
            print("[prune] No missing posts detected in prune window")

    state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}
    state["last_anchor"] = anchors[-1]
    state["last"] = "now"
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(state, open(STATE_PATH, "w"), indent=2)

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    bob = conn.execute("SELECT COUNT(*) FROM posts WHERE author_uid=?", (BT_UID,)).fetchone()[0]
    auth = conn.execute("SELECT COUNT(DISTINCT author) FROM posts").fetchone()[0]

    print(f"Wall Observer indexer - anchor {anchors[-1]} (inserted {inserted})")
    print(f"Total: {total} Bob: {bob} Authors: {auth}")

    rebuild(conn)
    print("Rebuilt profiles.")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--max-anchors",
        type=int,
        default=None,
        help="Limit topic anchors processed (saves time during maintenance).",
    )
    ap.add_argument(
        "--prune-missing",
        action="store_true",
        help="Check recent anchors for missing msg ids and delete them from the DB.",
    )
    ap.add_argument(
        "--prune-anchors",
        type=int,
        default=10,
        help="How many most-recent anchors to check when using --prune-missing.",
    )
    args = ap.parse_args()
    run(
        max_anchors=args.max_anchors,
        prune_missing=args.prune_missing,
        prune_anchors=args.prune_anchors,
    )
