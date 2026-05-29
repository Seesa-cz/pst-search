"""MCP tool definitions for PST email search."""

from __future__ import annotations
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import Config
from .store import EmailStore

logger = logging.getLogger(__name__)

_MAX_BODY_PREVIEW = 800   # chars shown in search results
_MAX_BODY_FULL = 20000    # chars returned by get_email


def register_tools(mcp: FastMCP, cfg: Config, store: EmailStore, touch_fn) -> None:

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search_emails(
        query: str = "",
        k: int = 10,
        folder: str | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        mode: str = "hybrid",
    ) -> str:
        """Search emails by semantic query and/or filters.

        Args:
            query:     Natural language description of what you're looking for.
            k:         Number of results (default 10).
            folder:    Filter to specific folder, e.g. "archiv-aso/Inbox". Use list_folders() to see options.
            from_addr: Filter by sender substring (case-insensitive).
            date_from: ISO date lower bound, e.g. "2023-01-01".
            date_to:   ISO date upper bound, e.g. "2023-12-31".
            mode:      "hybrid" (default), "dense" (semantic only), "keyword" (BM25 exact match).
        """
        touch_fn()
        total = store.count()
        if total == 0:
            return "Index is empty. Run reindex() first."

        # Build WHERE filter
        where_parts = []
        if folder:
            where_parts.append({"folder": {"$eq": folder}})
        if date_from:
            where_parts.append({"date_str": {"$gte": date_from}})
        if date_to:
            where_parts.append({"date_str": {"$lte": date_to + "Z"}})

        where = None
        if len(where_parts) == 1:
            where = where_parts[0]
        elif len(where_parts) > 1:
            # Merge into single AND dict (store supports multiple keys per dict)
            merged = {}
            for part in where_parts:
                merged.update(part)
            where = merged

        n_fetch = min(k * 4, total)

        if query.strip():
            if mode == "keyword":
                candidates = store.bm25_query(query, n_fetch, where)
            elif mode == "dense":
                embedding = store.encode(query)
                candidates = store.query(embedding, n_fetch, where)
            else:
                embedding = store.encode(query)
                candidates = store.hybrid_query(embedding, query, n_fetch, where)
        else:
            # Browse mode — just return filtered emails
            from .store import _where_to_sql
            if where:
                clause, params = _where_to_sql(where)
                rows = store.db.execute(
                    f"SELECT * FROM entries WHERE chunk_idx=0 AND ({clause}) ORDER BY date_str DESC LIMIT ?",
                    params + [k]
                ).fetchall()
            else:
                rows = store.db.execute(
                    "SELECT * FROM entries WHERE chunk_idx=0 ORDER BY date_str DESC LIMIT ?", (k,)
                ).fetchall()
            candidates = [(r["document"], store._row_to_meta(r), None) for r in rows]

        # Filter by from_addr substring (not in SQL — keep it simple)
        if from_addr:
            fa_lower = from_addr.lower()
            candidates = [
                (doc, meta, dist) for doc, meta, dist in candidates
                if fa_lower in meta["from_addr"].lower()
            ]

        # Deduplicate by file — keep best chunk per email
        seen: dict[str, tuple] = {}
        for doc, meta, dist in candidates:
            fid = meta["file_id"]
            if fid not in seen or (dist is not None and (seen[fid][2] is None or dist < seen[fid][2])):
                seen[fid] = (doc, meta, dist)
        candidates = list(seen.values())[:k]

        if not candidates:
            parts = [f"query='{query}'" if query.strip() else "browse"]
            if folder:
                parts.append(f"folder='{folder}'")
            if from_addr:
                parts.append(f"from='{from_addr}'")
            if date_from or date_to:
                parts.append(f"date={date_from or ''}…{date_to or ''}")
            return f"No emails found: {', '.join(parts)}"

        results = []
        for doc, meta, dist in candidates:
            header = f"### {meta['subject']}"
            if dist is not None:
                relevance = round((1.0 - dist) * 100, 1)
                header += f" — {relevance}% relevance"
            date_short = meta["date_str"][:10] if meta["date_str"] else "?"
            from_short = meta["from_addr"][:60]
            folder_short = meta["folder"]
            att_info = f"  📎 {meta['attachment_names']}" if meta["has_attachment"] == "true" else ""
            # Extract body snippet from document (skip header lines)
            body_lines = doc.split("\n\n", 1)
            snippet = (body_lines[1] if len(body_lines) > 1 else doc)[:_MAX_BODY_PREVIEW]
            if len(snippet) == _MAX_BODY_PREVIEW:
                snippet += "…"
            results.append(
                f"{header}\n"
                f"**Od:** {from_short}  |  **Datum:** {date_short}  |  **Složka:** {folder_short}{att_info}\n"
                f"**Path:** `{meta['path']}`\n\n"
                f"{snippet}"
            )

        return "\n\n---\n\n".join(results)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_email(path: str) -> str:
        """Return full content of an email (headers + body).

        Args:
            path: Relative path as returned by search_emails, e.g. "archiv-aso/Inbox/email.eml".
        """
        touch_fn()
        eml_path = (cfg.eml_dir / path).resolve()
        if not eml_path.is_relative_to(cfg.eml_dir) or not eml_path.exists():
            return f"Email not found: {path}"

        try:
            with open(eml_path, "rb") as f:
                import email as _email
                msg = _email.message_from_binary_file(f)
        except Exception as e:
            return f"Cannot read {path}: {e}"

        from .indexer import _decode_header, _get_body, _get_attachments
        subject = _decode_header(msg.get("Subject", ""))
        from_addr = _decode_header(msg.get("From", ""))
        to_addr = _decode_header(msg.get("To", ""))
        cc_addr = _decode_header(msg.get("Cc", ""))
        date_str = msg.get("Date", "")
        body = _get_body(msg)
        attachments = _get_attachments(msg)

        lines = [
            f"# {subject}",
            f"**Od:** {from_addr}",
            f"**Komu:** {to_addr}",
        ]
        if cc_addr:
            lines.append(f"**CC:** {cc_addr}")
        lines.append(f"**Datum:** {date_str}")
        if attachments:
            att_list = ", ".join(fn for fn, _ in attachments)
            lines.append(f"**Přílohy:** {att_list}")
        lines.append("")
        lines.append(body[:_MAX_BODY_FULL])
        if len(body) > _MAX_BODY_FULL:
            lines.append(f"\n… (zkráceno, celkem {len(body)} znaků)")

        # Attachment text
        for filename, text in attachments:
            if text.strip():
                lines.append(f"\n---\n### Příloha: {filename}\n{text[:3000]}")

        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_folders() -> str:
        """List all email folders with their email counts."""
        touch_fn()
        folders = store.get_folders()
        if not folders:
            return "No folders indexed yet."
        total = store.count_emails()
        lines = [f"Total emails: {total}\n"]
        for folder, count in folders:
            lines.append(f"  {folder}: {count}")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    def reindex() -> str:
        """Index new emails from eml_dir that aren't in the index yet.

        Skips already-indexed emails. To force full reindex, use the --reindex
        flag on the mailarch.sh script.
        """
        touch_fn()
        from .indexer import sync
        n = sync(cfg.eml_dir, store)
        total = store.count_emails()
        return f"Indexed {n} new email(s). Total in index: {total}."
