# MAP.md — pst-search project map

Quick orientation guide: file responsibilities, dependencies, and constraints.

## Top-level files

| File | Purpose |
|---|---|
| `mailarch.sh` | Start/stop/extract/reindex launcher script. Reads `config.yaml`, exports `PST_*` env vars, manages PID file, polls `/health` after start. |
| `config.yaml` | User configuration (gitignored — contains local paths). Copy from `config.yaml.template`. |
| `config.yaml.template` | Template with placeholder paths. Committed to repo. |
| `pyproject.toml` | Project metadata and dependencies. Entry point: `pst-search = "pst_search.server:main"`. |
| `uv.lock` | Locked dependency versions (uv). |

## Source files — `src/pst_search/`

### `config.py`
Reads `PST_*` environment variables into a frozen `Config` dataclass.  
All paths are expanded (`~`) and resolved (symlinks) via `Path.expanduser().resolve()`.  
`PST_APP_DIR` is used as fallback for log/pid file placement when not set explicitly.  
**Consumed by:** `server.py`

### `extractor.py`
Wraps `readpst` CLI to extract `.pst` files into `.eml` directory trees.  
`extract_pst(pst_path, output_dir)` — single file.  
`extract_all(pst_dir, eml_dir, force=False)` — all `*.pst` in a directory; skips already-extracted archives unless `force=True`.  
**Constraint:** `readpst` must be installed (`apt install pst-utils`). Deleted items included (`-D` flag).  
**Called by:** `mailarch.sh --extract` (via Python import) and standalone use.

### `store.py`
`EmailStore` — the vector + metadata database.

**Storage:**
- `metadata.db` — SQLite (WAL mode). One row per chunk. Columns: `subject`, `from_addr`, `to_addr`, `cc_addr`, `date_str`, `folder`, `has_attachment`, `attachment_names`, `path`, `file_id`, `chunk_heading`, `chunk_idx`.
- `vectors.npy` + `row_ids.npy` — float32 numpy matrix (L2-normalized). Loaded fully into RAM on first query.
- BM25 index — built in-memory from SQLite on demand (`rank_bm25.BM25Okapi`), rebuilt lazily after any write.

**Key methods:**
- `upsert_batch(ids, embeddings, documents, metadatas)` — thread-safe batch write.
- `hybrid_query(embedding, text, n, where)` — RRF fusion of dense cosine + BM25.
- `query(embedding, n, where)` — dense only.
- `bm25_query(text, n, where)` — BM25 keyword only.
- `get_folders()` — `[(folder, email_count)]` from SQLite.
- `encode(text)` / `encode_batch(texts)` — lazy model load via `fastembed.TextEmbedding`.

**WHERE filter syntax** (subset of chromadb-style):  
`{"field": {"$eq": v}}`, `{"field": {"$gte": v}}`, `{"field": {"$lte": v}}`, `{"$or": [...]}`.  
Date range filtering works because `date_str` is stored in ISO format (lexicographic order = chronological order).

**Constraint:** Changing `model` requires wiping `data/` and reindexing — embedding dimensions differ between models.  
**Consumed by:** `indexer.py`, `tools.py`, `server.py`

### `indexer.py`
Parses `.eml` files and writes chunks to `EmailStore`.

**Email → chunks strategy:**
- Every email produces at least 1 chunk (`chunk_idx=0`, `chunk_heading="body"`): embed text = `Subject + From + To + Date + Folder + body`.
- Attachments with extractable text produce additional chunks (`chunk_idx=1..N`, `chunk_heading="attachment: <filename>"`): embed text = `Subject + From + Attachment + content`.
- Attachments without extractable text are listed in `attachment_names` metadata only.

**Attachment extractors (all silently skip on error):**
- `.pdf` → `pdfminer.high_level.extract_text`, capped at 8 000 chars.
- `.docx` → `python-docx`, capped at 8 000 chars.
- `.xlsx` / `.xls` → `openpyxl`, capped at 8 000 chars / 500 rows.

**`sync(eml_dir, store)`** — incremental: fetches `{file_id: date_str}` from SQLite, skips already-indexed emails, batches remaining in groups of 200 chunks (`_ENCODE_BATCH`), calls `store.encode_batch` + `store.upsert_batch`. Logs progress every 15 s.

**`full_reindex(eml_dir, store)`** — wipes SQLite + numpy files, then calls `sync`.

**`email_id(path, base)`** — SHA-256 of the relative path. Used as stable identifier across restarts.

**Consumed by:** `server.py` (startup sync in background thread), `tools.py` (`reindex` tool).

### `server.py`
FastMCP HTTP server entry point.

**Startup sequence:**
1. Load config, validate `eml_dir` exists.
2. Create `EmailStore`.
3. Launch `sync()` in a background daemon thread; set `_indexing` event while running.
4. Register MCP tools via `register_tools()`.
5. Register `/health` custom route (returns `{status, indexing, emails, chunks}`).
6. `uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=...)`.

**MCP endpoint:** `http://127.0.0.1:<port>/mcp`  
**Health endpoint:** `http://127.0.0.1:<port>/health`

**Idle timeout:** optional SIGTERM after N minutes of no tool calls (`PST_IDLE_TIMEOUT`, default 0 = disabled).

**Run as module:** `python -m pst_search.server` (requires `if __name__ == "__main__": main()` guard — present at bottom of file).

**Consumed by:** `mailarch.sh --start`

### `tools.py`
Registers four MCP tools on a `FastMCP` instance.

| Tool | Read-only | Description |
|---|---|---|
| `search_emails` | yes | Hybrid/dense/keyword search. Deduplicates by `file_id` (best chunk per email). Supports `folder`, `from_addr` (substring), `date_from`, `date_to`, `mode` filters. |
| `get_email` | yes | Reads raw `.eml` from disk, returns formatted headers + body + attachment text. Path validated against `cfg.eml_dir` (no path traversal). |
| `list_folders` | yes | Returns `get_folders()` with total count. |
| `reindex` | no | Calls `sync()` — indexes only new emails. Full rebuild via `mailarch.sh --reindex`. |

**`search_emails` deduplication:** after candidate retrieval, one result per `file_id` is kept (lowest distance wins). This prevents the same email appearing multiple times when both body and attachment chunks score highly.

**Consumed by:** `server.py`

## Data flow

```
PST file
  └─ readpst (extractor.py)
       └─ .eml files (eml_dir/)
            └─ indexer.py: parse_eml()
                 ├─ body text
                 └─ attachment text (PDF/docx/xlsx)
                      └─ store.py: upsert_batch()
                           ├─ metadata.db  (SQLite)
                           ├─ vectors.npy  (numpy float32)
                           └─ row_ids.npy
                                └─ tools.py: search_emails()
                                     └─ MCP client (Claude, etc.)
```

## Known constraints and limits

- **Model change** requires full reindex (`mailarch.sh --reindex`).
- **BM25 index** is rebuilt in-memory on each server start — takes a few seconds for large indexes.
- **numpy vectors** are loaded fully into RAM. At 384 dimensions × float32 × 8 573 chunks ≈ 13 MB — negligible.
- **Attachment extraction is best-effort** — corrupt or password-protected files are silently skipped.
- **`date_str` filtering** uses lexicographic SQL comparison; ISO 8601 format (`YYYY-MM-DDTHH:MM:SS±HH:MM`) preserves chronological order correctly for `$gte`/`$lte`.
- **WSL2:** project files and `data_dir` should be on Linux ext4 (`/home/...`). Running from `/mnt/c/` causes slow Python imports (~4 s vs ~0.1 s for numpy alone).
- **Port** defaults to `8766`. Change via `PST_PORT` env var or `config.yaml`.
