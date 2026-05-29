"""Email vector store — numpy flat search + SQLite (metadata) + BM25 hybrid search.

Dense embeddings: float32 numpy matrix (L2-normalized), cosine similarity via dot product.
Sparse index:     rank_bm25 BM25Okapi, rebuilt in-memory from SQLite on demand.
Hybrid search:    RRF (Reciprocal Rank Fusion) over dense + BM25 ranked lists.
"""

from __future__ import annotations
import math
import re
import sqlite3
import threading
from pathlib import Path

import numpy as np


_UPSERT_CHUNK = 1000
_RRF_K = 60


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return v
    return [x / norm for x in v]


def _where_to_sql(where: dict) -> tuple[str, list]:
    """Convert where dict to SQL WHERE fragment + params.

    Supported operators: $eq, $gte, $lte, $or.
    """
    if "$or" in where:
        parts, params = [], []
        for sub in where["$or"]:
            clause, sub_params = _where_to_sql(sub)
            parts.append(f"({clause})")
            params.extend(sub_params)
        return " OR ".join(parts), params
    clauses, params = [], []
    for field, condition in where.items():
        if isinstance(condition, dict):
            if "$eq" in condition:
                clauses.append(f"{field} = ?")
                params.append(condition["$eq"])
            if "$gte" in condition:
                clauses.append(f"{field} >= ?")
                params.append(condition["$gte"])
            if "$lte" in condition:
                clauses.append(f"{field} <= ?")
                params.append(condition["$lte"])
        else:
            clauses.append(f"{field} = ?")
            params.append(condition)
    return (" AND ".join(clauses) if clauses else "1=1"), params


def _matches_where(meta: dict, where: dict) -> bool:
    if "$or" in where:
        return any(_matches_where(meta, sub) for sub in where["$or"])
    for field, condition in where.items():
        val = meta.get(field, "")
        if isinstance(condition, dict):
            if "$eq" in condition and val != condition["$eq"]:
                return False
            if "$gte" in condition and not (val >= condition["$gte"]):
                return False
            if "$lte" in condition and not (val <= condition["$lte"]):
                return False
        else:
            if val != condition:
                return False
    return True


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w]+", text.lower()) if t]


def _rrf(ranked_ids: list[int], k: int = _RRF_K) -> dict[int, float]:
    return {rid: 1.0 / (k + rank + 1) for rank, rid in enumerate(ranked_ids)}


class EmailStore:
    """Vector store for email chunks.

    Schema per entry:
      subject, from_addr, to_addr, cc_addr, date_str, folder,
      has_attachment, attachment_names, path, file_id,
      chunk_heading, chunk_idx
    """

    def __init__(self, data_dir: Path, model_name: str) -> None:
        self._data_dir = data_dir
        self._model_name = model_name
        self._model = None
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()

        self._mat: np.ndarray | None = None
        self._row_ids: list[int] = []
        self._rid_to_idx: dict[int, int] = {}
        self._vectors_loaded = False

        self._bm25 = None
        self._bm25_row_ids: list[int] = []
        self._bm25_dirty = True

    @property
    def model(self):
        if self._model is None:
            import warnings
            from fastembed import TextEmbedding
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self._model = TextEmbedding(self._model_name)
        return self._model

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            conn = sqlite3.connect(
                str(self._data_dir / "metadata.db"),
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS entries (
                    row_id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    str_id           TEXT UNIQUE NOT NULL,
                    document         TEXT NOT NULL DEFAULT '',
                    subject          TEXT NOT NULL DEFAULT '',
                    from_addr        TEXT NOT NULL DEFAULT '',
                    to_addr          TEXT NOT NULL DEFAULT '',
                    cc_addr          TEXT NOT NULL DEFAULT '',
                    date_str         TEXT NOT NULL DEFAULT '',
                    folder           TEXT NOT NULL DEFAULT '',
                    has_attachment   TEXT NOT NULL DEFAULT 'false',
                    attachment_names TEXT NOT NULL DEFAULT '',
                    path             TEXT NOT NULL DEFAULT '',
                    file_id          TEXT NOT NULL DEFAULT '',
                    chunk_heading    TEXT NOT NULL DEFAULT '',
                    chunk_idx        INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_path    ON entries(path);
                CREATE INDEX IF NOT EXISTS idx_file_id ON entries(file_id);
                CREATE INDEX IF NOT EXISTS idx_folder  ON entries(folder);
                CREATE INDEX IF NOT EXISTS idx_date    ON entries(date_str);
            """)
            conn.commit()
            self._db = conn
        return self._db

    def _load_vectors(self) -> None:
        if self._vectors_loaded:
            return
        self._vectors_loaded = True
        vec_path = self._data_dir / "vectors.npy"
        rid_path = self._data_dir / "row_ids.npy"
        if vec_path.exists() and rid_path.exists():
            self._mat = np.load(str(vec_path))
            row_ids = np.load(str(rid_path)).tolist()
            self._row_ids = [int(r) for r in row_ids]
            self._rid_to_idx = {rid: i for i, rid in enumerate(self._row_ids)}

    def _save_vectors(self) -> None:
        if self._mat is not None and len(self._row_ids) > 0:
            np.save(str(self._data_dir / "vectors.npy"), self._mat)
            np.save(str(self._data_dir / "row_ids.npy"), np.array(self._row_ids, dtype=np.int64))
        else:
            for f in ("vectors.npy", "row_ids.npy"):
                (self._data_dir / f).unlink(missing_ok=True)

    def _append_vectors(self, row_ids: list[int], embeddings: list[list[float]]) -> None:
        new_mat = np.array(embeddings, dtype=np.float32)
        if self._mat is None or len(self._row_ids) == 0:
            self._mat = new_mat
        else:
            self._mat = np.vstack([self._mat, new_mat])
        start = len(self._row_ids)
        self._row_ids.extend(row_ids)
        for i, rid in enumerate(row_ids):
            self._rid_to_idx[rid] = start + i

    def _remove_row_ids(self, row_ids: list[int]) -> None:
        if self._mat is None:
            return
        idxs_to_remove = {self._rid_to_idx[rid] for rid in row_ids if rid in self._rid_to_idx}
        if not idxs_to_remove:
            return
        keep = [i for i in range(len(self._row_ids)) if i not in idxs_to_remove]
        self._mat = self._mat[keep]
        self._row_ids = [self._row_ids[i] for i in keep]
        self._rid_to_idx = {rid: i for i, rid in enumerate(self._row_ids)}

    def _rebuild_bm25(self) -> None:
        from rank_bm25 import BM25Okapi
        rows = self.db.execute("SELECT row_id, document, subject FROM entries").fetchall()
        self._bm25_row_ids = [r["row_id"] for r in rows]
        corpus = [_tokenize(r["document"] + " " + r["subject"]) for r in rows]
        self._bm25 = BM25Okapi(corpus) if corpus else None
        self._bm25_dirty = False

    def _ensure_bm25(self) -> None:
        if self._bm25_dirty:
            self._rebuild_bm25()

    def encode(self, text: str) -> list[float]:
        return _normalize(next(iter(self.model.embed([text]))).tolist())

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [_normalize(v.tolist()) for v in self.model.embed(texts)]

    def _row_to_meta(self, row: sqlite3.Row) -> dict:
        return {
            "subject":          row["subject"],
            "from_addr":        row["from_addr"],
            "to_addr":          row["to_addr"],
            "cc_addr":          row["cc_addr"],
            "date_str":         row["date_str"],
            "folder":           row["folder"],
            "has_attachment":   row["has_attachment"],
            "attachment_names": row["attachment_names"],
            "path":             row["path"],
            "file_id":          row["file_id"],
            "chunk_heading":    row["chunk_heading"],
            "chunk_idx":        row["chunk_idx"],
        }

    def _delete_str_ids(self, ids: list[str]) -> list[int]:
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT str_id, row_id FROM entries WHERE str_id IN ({ph})", ids
        ).fetchall()
        row_ids = [r["row_id"] for r in rows]
        self.db.execute(f"DELETE FROM entries WHERE str_id IN ({ph})", ids)
        self.db.commit()
        return row_ids

    def _insert_entries(self, ids, documents, metadatas) -> list[int]:
        for i in range(0, len(ids), _UPSERT_CHUNK):
            sl = slice(i, i + _UPSERT_CHUNK)
            self.db.executemany(
                """INSERT INTO entries
                   (str_id, document, subject, from_addr, to_addr, cc_addr,
                    date_str, folder, has_attachment, attachment_names,
                    path, file_id, chunk_heading, chunk_idx)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (id_, doc,
                     meta.get("subject", ""),          meta.get("from_addr", ""),
                     meta.get("to_addr", ""),           meta.get("cc_addr", ""),
                     meta.get("date_str", ""),          meta.get("folder", ""),
                     meta.get("has_attachment", "false"), meta.get("attachment_names", ""),
                     meta.get("path", ""),              meta.get("file_id", ""),
                     meta.get("chunk_heading", ""),     meta.get("chunk_idx", 0))
                    for id_, doc, meta in zip(ids[sl], documents[sl], metadatas[sl])
                ],
            )
        self.db.commit()
        ph = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT str_id, row_id FROM entries WHERE str_id IN ({ph})", ids
        ).fetchall()
        mapping = {r["str_id"]: r["row_id"] for r in rows}
        return [mapping[id_] for id_ in ids]

    def upsert_batch(self, ids, embeddings, documents, metadatas) -> None:
        if not ids:
            return
        with self._lock:
            self._load_vectors()
            old_row_ids = self._delete_str_ids(ids)
            self._remove_row_ids(old_row_ids)
            new_row_ids = self._insert_entries(ids, documents, metadatas)
            self._append_vectors(new_row_ids, embeddings)
            self._save_vectors()
            self._bm25_dirty = True

    def delete_by_file_id(self, file_id: str) -> None:
        with self._lock:
            self._load_vectors()
            rows = self.db.execute(
                "SELECT str_id, row_id FROM entries WHERE file_id=?", (file_id,)
            ).fetchall()
            if not rows:
                return
            row_ids = [r["row_id"] for r in rows]
            str_ids = [r["str_id"] for r in rows]
            self.db.execute(
                f"DELETE FROM entries WHERE file_id=?", (file_id,)
            )
            self.db.commit()
            self._remove_row_ids(row_ids)
            self._save_vectors()
            self._bm25_dirty = True

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def count_emails(self) -> int:
        return self.db.execute(
            "SELECT COUNT(DISTINCT file_id) FROM entries"
        ).fetchone()[0]

    def get_all_file_ids(self) -> dict[str, str]:
        """Return {file_id: date_str} for all indexed emails."""
        rows = self.db.execute(
            "SELECT DISTINCT file_id, date_str FROM entries WHERE chunk_idx=0"
        ).fetchall()
        return {r["file_id"]: r["date_str"] for r in rows}

    def query(self, embedding, n, where=None) -> list[tuple]:
        with self._lock:
            return self._query_locked(embedding, n, where)

    def _query_locked(self, embedding, n, where) -> list[tuple]:
        self._load_vectors()
        if self._mat is None or not self._row_ids:
            return []
        q = np.array(embedding, dtype=np.float32)
        scores = self._mat @ q
        n_fetch = min(n, len(scores))
        top_idx = np.argpartition(scores, -n_fetch)[-n_fetch:]
        top_idx = top_idx[np.argsort(scores[top_idx])[::-1]]
        results = []
        for i in top_idx:
            row_id = self._row_ids[i]
            row = self.db.execute("SELECT * FROM entries WHERE row_id=?", (row_id,)).fetchone()
            if row is None:
                continue
            meta = self._row_to_meta(row)
            if where is not None and not _matches_where(meta, where):
                continue
            dist = float((1.0 - scores[i]) / 2.0)
            results.append((row["document"], meta, dist))
        return results

    def bm25_query(self, text, n, where=None) -> list[tuple]:
        with self._lock:
            self._ensure_bm25()
            if self._bm25 is None or not self._bm25_row_ids:
                return []
            tokens = _tokenize(text)
            raw_scores = self._bm25.get_scores(tokens)
            max_score = float(raw_scores.max()) if len(raw_scores) else 0.0
            if max_score == 0.0:
                return []
            order = np.argsort(raw_scores)[::-1]
            results = []
            seen_rids = set()
            for idx in order:
                if len(results) >= n:
                    break
                rid = self._bm25_row_ids[idx]
                if rid in seen_rids:
                    continue
                seen_rids.add(rid)
                score = float(raw_scores[idx])
                if score <= 0.0:
                    break
                row = self.db.execute("SELECT * FROM entries WHERE row_id=?", (rid,)).fetchone()
                if row is None:
                    continue
                meta = self._row_to_meta(row)
                if where is not None and not _matches_where(meta, where):
                    continue
                dist = 1.0 - score / max_score
                results.append((row["document"], meta, dist))
            return results

    def hybrid_query(self, embedding, text, n, where=None) -> list[tuple]:
        with self._lock:
            return self._hybrid_locked(embedding, text, n, where)

    def _hybrid_locked(self, embedding, text, n, where) -> list[tuple]:
        self._load_vectors()
        self._ensure_bm25()
        if not self._row_ids:
            return []

        n_fetch = min(n * 4, len(self._row_ids))

        q = np.array(embedding, dtype=np.float32)
        dense_scores = self._mat @ q
        top_n = min(n_fetch, len(dense_scores))
        top_idx = np.argpartition(dense_scores, -top_n)[-top_n:]
        top_idx = top_idx[np.argsort(dense_scores[top_idx])[::-1]]
        dense_ranked = [self._row_ids[i] for i in top_idx]

        bm25_ranked: list[int] = []
        if self._bm25 is not None and self._bm25_row_ids:
            tokens = _tokenize(text)
            bm25_scores = self._bm25.get_scores(tokens)
            order = np.argsort(bm25_scores)[::-1]
            bm25_ranked = [
                self._bm25_row_ids[i]
                for i in order[:n_fetch]
                if bm25_scores[i] > 0
            ]

        scores: dict[int, float] = {}
        for rank, rid in enumerate(dense_ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        for rank, rid in enumerate(bm25_ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)

        sorted_rids = sorted(scores, key=scores.__getitem__, reverse=True)
        max_score = scores[sorted_rids[0]] if sorted_rids else 1.0

        ph = ",".join("?" * len(sorted_rids))
        rows = self.db.execute(
            f"SELECT * FROM entries WHERE row_id IN ({ph})", sorted_rids
        ).fetchall()
        row_map = {r["row_id"]: r for r in rows}

        results = []
        for rid in sorted_rids:
            if len(results) >= n:
                break
            row = row_map.get(rid)
            if row is None:
                continue
            meta = self._row_to_meta(row)
            if where is not None and not _matches_where(meta, where):
                continue
            dist = 1.0 - scores[rid] / max_score
            results.append((row["document"], meta, dist))
        return results

    def get_folders(self) -> list[tuple[str, int]]:
        """Return [(folder, email_count)] sorted by count descending."""
        rows = self.db.execute(
            "SELECT folder, COUNT(DISTINCT file_id) as cnt FROM entries GROUP BY folder ORDER BY cnt DESC"
        ).fetchall()
        return [(r["folder"], r["cnt"]) for r in rows]
