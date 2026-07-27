# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A data-export pipeline for a phpBB forum (DealersEdge "Parts Managers" forum, `forum_id=3`). The goal is to produce a `posts/` directory of plain-text thread files suitable for use in a RAG system.

The two-stage pipeline is:
1. **Spin up MariaDB** from a SQL dump via Docker Compose.
2. **Run `export_threads.py`** to query the database and write one `.txt` file per thread.

## Running the pipeline

### Start the database

```bash
docker-compose up -d
```

MariaDB loads `backup.sql` (~166 MB) on first start; wait until healthy before running the export:

```bash
docker-compose ps   # check Status is "healthy"
```

### Run the export

```bash
python3 export_threads.py
```

Writes `posts/<topic_id>.txt` for every visible topic in forum 3. Currently produces ~9,500 files.

### Inspect the database directly

phpMyAdmin is available at http://localhost:8080 (root / rootpassword).

## Database

- Engine: MariaDB 11.4, database `db_forums`
- Credentials: `root` / `rootpassword`, host `127.0.0.1:3306`
- Key tables: `phpbb_topics`, `phpbb_posts`, `phpbb_users`
- Posts are stored as phpBB XML (not raw HTML); `export_threads.py` parses this with `xml.etree.ElementTree`.

## Post file format

Each `posts/<topic_id>.txt` has a header block (Title / Forum / Date), then chronological posts separated by dashes. The first post is labelled "Posted by", replies are "Reply by". The source URL is appended at the end.

## Dependencies

Python: `pymysql` (only runtime dependency).
