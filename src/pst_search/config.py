"""PST Search configuration — reads PST_* env vars."""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    pst_dir: Path       # directory with .pst files
    eml_dir: Path       # readpst output (.eml files)
    data_dir: Path      # vector store + SQLite
    port: int
    model: str
    log_level: str
    log_file: Path
    pid_file: Path
    idle_timeout: int   # seconds; 0 = never


def load() -> Config:
    app_dir = Path(os.environ.get("PST_APP_DIR", Path(__file__).parent.parent.parent))
    return Config(
        pst_dir=Path(os.environ["PST_DIR"]).expanduser().resolve(),
        eml_dir=Path(os.environ["PST_EML_DIR"]).expanduser().resolve(),
        data_dir=Path(os.environ["PST_DATA_DIR"]).expanduser().resolve(),
        port=int(os.environ.get("PST_PORT", "8766")),
        model=os.environ.get("PST_MODEL", "BAAI/bge-small-en-v1.5"),
        log_level=os.environ.get("PST_LOG_LEVEL", "WARNING").upper(),
        log_file=Path(os.environ.get("PST_LOG_FILE", str(app_dir / "server.log"))).expanduser().resolve(),
        pid_file=Path(os.environ.get("PST_PID_FILE", str(app_dir / "server.pid"))).expanduser().resolve(),
        idle_timeout=int(os.environ.get("PST_IDLE_TIMEOUT", "0")) * 60,
    )
