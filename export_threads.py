#!/usr/bin/env python3
"""Export all threads from all forums to ./posts/*.txt"""

import pymysql
import os
import re
import html
from datetime import datetime
import xml.etree.ElementTree as ET

BASE_URL = "https://forums.dealersedge.com"
SCRIPT_PATH = ""
OUTPUT_DIR = "./posts"


def extract_element_text(elem):
    """Recursively convert a phpBB XML element tree to plain text."""
    parts = []

    if elem.text:
        parts.append(elem.text)

    for child in elem:
        # <s> and <e> hold raw BBCode syntax like [b] and [/b] — skip content
        if child.tag in ('s', 'e'):
            if child.tail:
                parts.append(child.tail)
            continue

        if child.tag == 'URL':
            url = child.get('url', '')
            inner = extract_element_text(child).strip()
            if inner and inner != url:
                parts.append(f"{inner} ({url})")
            elif url:
                parts.append(url)

        elif child.tag == 'EMAIL':
            email = child.get('email', '')
            parts.append(email or extract_element_text(child).strip())

        elif child.tag == 'QUOTE':
            author = child.get('author', '')
            inner = extract_element_text(child).strip()
            header = f"--- Quote from {author} ---" if author else "--- Quote ---"
            parts.append(f"\n{header}\n{inner}\n--- End Quote ---\n")

        elif child.tag == 'CODE':
            inner = extract_element_text(child).strip()
            parts.append(f"\n[Code]\n{inner}\n[/Code]\n")

        elif child.tag == 'IMG':
            src = child.get('src', '')
            parts.append(f"[Image: {src}]")

        elif child.tag in ('LI', 'LISTITEM'):
            inner = extract_element_text(child).strip()
            parts.append(f"\n  - {inner}")

        else:
            parts.append(extract_element_text(child))

        if child.tail:
            parts.append(child.tail)

    return ''.join(parts)


def phpbb_to_plain(post_text):
    """Convert phpBB XML post storage format to plain text."""
    if not post_text:
        return ""
    decoded = html.unescape(post_text)
    try:
        root = ET.fromstring(decoded)
        return extract_element_text(root).strip()
    except ET.ParseError:
        return re.sub(r'<[^>]+>', '', decoded).strip()


def fmt_date(ts):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    conn = pymysql.connect(
        host='127.0.0.1',
        port=3306,
        user='root',
        password='rootpassword',
        database='db_forums',
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.topic_id, t.topic_title, t.topic_time, f.forum_name
                FROM phpbb_topics t
                JOIN phpbb_forums f ON t.forum_id = f.forum_id
                WHERE t.topic_visibility = 1
                ORDER BY t.topic_id
            """)
            topics = cur.fetchall()

        print(f"Found {len(topics)} topics. Exporting to {OUTPUT_DIR}/...")

        for i, topic in enumerate(topics, 1):
            topic_id = topic['topic_id']
            topic_title = html.unescape(topic['topic_title'])

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.post_id, p.post_time, p.post_text,
                           COALESCE(NULLIF(u.username, ''), p.post_username, 'Unknown') AS author
                    FROM phpbb_posts p
                    LEFT JOIN phpbb_users u ON p.poster_id = u.user_id
                    WHERE p.topic_id = %s AND p.post_visibility = 1
                    ORDER BY p.post_time ASC
                """, (topic_id,))
                posts = cur.fetchall()

            if not posts:
                continue

            forum_name = html.unescape(topic['forum_name'])
            lines = [
                f"Title: {topic_title}",
                f"Thread ID: {topic_id}",
                f"Forum: {forum_name}",
                f"Date: {fmt_date(topic['topic_time'])}",
                "=" * 60,
                "",
            ]

            for j, post in enumerate(posts):
                label = "Posted by" if j == 0 else "Reply by"
                lines.append(f"{label}: {post['author']}  |  Post ID: {post['post_id']}  |  {fmt_date(post['post_time'])}")
                lines.append("")
                lines.append(phpbb_to_plain(post['post_text']))
                lines.append("")
                lines.append("-" * 40)
                lines.append("")

            thread_url = f"{BASE_URL}{SCRIPT_PATH}/viewtopic.php?t={topic_id}"
            lines.append(f"Source: {thread_url}")

            with open(os.path.join(OUTPUT_DIR, f"{topic_id}.txt"), 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))

            if i % 1000 == 0:
                print(f"  {i}/{len(topics)} done...")

        print(f"Complete. {len(topics)} files written to {OUTPUT_DIR}/")

    finally:
        conn.close()


if __name__ == '__main__':
    main()
