# pst-search

Full-text and semantic search over Outlook PST archives — exposed as an [MCP](https://modelcontextprotocol.io) server so AI assistants (Claude, etc.) can search your email directly.

No Outlook required. Runs entirely on Linux / WSL2.

## Features

- **Hybrid search** — combines dense vector embeddings (semantic) with BM25 keyword ranking via RRF fusion
- **Multilingual** — uses `paraphrase-multilingual-MiniLM-L12-v2`; works well with Czech, Slovak, German, English, and 50+ other languages
- **Attachment text extraction** — indexes content of PDF, Word (`.docx`), and Excel (`.xlsx`) attachments
- **MCP server** — exposes `search_emails`, `get_email`, `list_folders`, and `reindex` tools over HTTP
- **Fast after first index** — index stored in SQLite + numpy on local filesystem; subsequent starts skip already-indexed emails
- **No cloud, no telemetry** — everything runs locally

## Architecture

```
PST file(s)
    │
    ▼  readpst (one-time extraction)
.eml files  (folder hierarchy preserved)
    │
    ▼  indexer (parallel: body + attachment text)
EmailStore  (SQLite metadata + numpy float32 vectors + BM25 index)
    │
    ▼  FastMCP HTTP server  →  Claude / any MCP client
```

## Requirements

- Linux or WSL2 (Windows filesystem works but indexing is slower)
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) package manager
- `readpst` — part of the `libpst` / `pst-utils` package

```bash
# Ubuntu / Debian / WSL
sudo apt install pst-utils
```

## Installation

```bash
git clone https://github.com/yourname/pst-search
cd pst-search
uv sync
```

## Configuration

Copy and edit the template:

```bash
cp config.yaml.template config.yaml
```

`config.yaml`:

```yaml
pst_dir:  /path/to/your/pst/files      # directory containing *.pst files
eml_dir:  /path/to/extracted/eml       # where readpst output goes
data_dir: /path/to/index               # vector store + SQLite (keep on fast filesystem)
port:     8766
model:    sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
log_level: WARNING
idle_timeout: 0                        # minutes; 0 = never shut down
```

All paths also accept `~` expansion. Environment variables `PST_*` override `config.yaml`.

> **WSL2 tip:** Put `data_dir` on the Linux ext4 filesystem (`/home/...`), not on `/mnt/c/`. Python startup and search are significantly faster on ext4.

## Usage

### First time

```bash
# 1. Extract PST archives to .eml files (one-time, may take several minutes)
bash mailarch.sh --extract

# 2. Start the MCP server (downloads embedding model + indexes all emails on first run)
bash mailarch.sh --start
```

Indexing progress is logged to `server.log`. For a 3.9 GB PST (~8 000 emails), expect roughly 20–30 minutes on first run.

### Day-to-day

```bash
bash mailarch.sh --start     # start server (only indexes new emails)
bash mailarch.sh --status    # show running status and email count
bash mailarch.sh --stop      # stop server
bash mailarch.sh --restart   # stop + start
bash mailarch.sh --reindex   # wipe index and rebuild from scratch
bash mailarch.sh --config    # show resolved configuration
```

When you add new PST files:

```bash
bash mailarch.sh --extract   # skips already-extracted archives
bash mailarch.sh --restart   # picks up new emails automatically
```

## MCP integration

Add to your MCP client configuration (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pst-search": {
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

### Available tools

| Tool | Description |
|---|---|
| `search_emails` | Hybrid / dense / keyword search with optional filters |
| `get_email` | Return full content of an email by path |
| `list_folders` | List all folders with email counts |
| `reindex` | Index any new emails since last run |

#### `search_emails` parameters

| Parameter | Default | Description |
|---|---|---|
| `query` | `""` | Natural language query (omit to browse) |
| `k` | `10` | Number of results |
| `folder` | — | Filter to a specific folder (as returned by `list_folders`) |
| `from_addr` | — | Filter by sender substring |
| `date_from` | — | ISO date lower bound, e.g. `"2023-01-01"` |
| `date_to` | — | ISO date upper bound, e.g. `"2023-12-31"` |
| `mode` | `"hybrid"` | `"hybrid"` · `"dense"` · `"keyword"` |

## Supported attachment formats

| Format | Library |
|---|---|
| PDF | `pdfminer.six` |
| Word (`.docx`) | `python-docx` |
| Excel (`.xlsx` / `.xls`) | `openpyxl` |

Other attachment types are recorded by filename but their content is not indexed.

## Health endpoint

```bash
curl http://127.0.0.1:8766/health
# {"status":"ok","indexing":false,"emails":8177,"chunks":8573}
```

## Changing the embedding model

1. Edit `model:` in `config.yaml`
2. Run `bash mailarch.sh --reindex` (full rebuild required when model changes)

Available multilingual models (via [fastembed](https://github.com/qdrant/fastembed)):

| Model | Size | Notes |
|---|---|---|
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 0.22 GB | **Default** — fast, good multilingual quality |
| `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | 1.0 GB | Higher quality, slower |
| `intfloat/multilingual-e5-large` | 2.24 GB | Best quality, requires more RAM |

## Contributors

- [Seesa-cz](https://github.com/Seesa-cz)
- [Claude](https://claude.ai) (Anthropic AI)

## License

MIT
