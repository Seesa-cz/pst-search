"""PST Search MCP server entry point."""

from __future__ import annotations
import logging
import os
import signal
import threading
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from .config import load as load_config
from .store import EmailStore
from .indexer import sync
from .tools import register_tools


def main() -> None:
    import uvicorn

    cfg = load_config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger(__name__)
    log.warning("Starting pst-search server on port %d (pid %d)", cfg.port, os.getpid())

    if not cfg.eml_dir.exists():
        raise RuntimeError(f"PST_EML_DIR does not exist: {cfg.eml_dir}. Run mailarch.sh --extract first.")

    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    pid_path = cfg.pid_file
    pid_path.write_text(str(os.getpid()))

    store = EmailStore(cfg.data_dir, cfg.model)

    _sync_done = threading.Event()
    _indexing = threading.Event()
    _indexing.set()

    def _initial_sync():
        try:
            n = sync(cfg.eml_dir, store)
            log.warning("Startup sync: %d emails indexed", n)
        except Exception:
            log.exception("Startup sync failed")
        finally:
            _indexing.clear()
            _sync_done.set()

    threading.Thread(target=_initial_sync, daemon=True).start()

    _last_activity = time.monotonic()

    def touch():
        nonlocal _last_activity
        _last_activity = time.monotonic()

    if cfg.idle_timeout > 0:
        def _idle_watcher():
            while True:
                time.sleep(30)
                if time.monotonic() - _last_activity > cfg.idle_timeout:
                    log.warning("Idle timeout reached — shutting down")
                    os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=_idle_watcher, daemon=True).start()

    mcp = FastMCP("pst-search")
    register_tools(mcp, cfg, store, touch)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "indexing": _indexing.is_set(),
            "emails": store.count_emails(),
            "chunks": store.count(),
        })

    app = mcp.streamable_http_app()
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
