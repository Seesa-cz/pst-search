"""PST extraction — runs readpst to convert .pst files to .eml directory trees."""

from __future__ import annotations
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_pst(pst_path: Path, output_dir: Path) -> int:
    """Extract a .pst file to individual .eml files under output_dir/<pst_stem>/.

    Uses readpst -e (individual email files) -D (include deleted items).
    Returns number of .eml files created.
    """
    target = output_dir / pst_path.stem
    target.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["readpst", "-e", "-D", "-o", str(target), str(pst_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("readpst failed for %s: %s", pst_path.name, result.stderr)
        raise RuntimeError(f"readpst failed: {result.stderr.strip()}")

    count = sum(1 for _ in target.rglob("*.eml"))
    logger.warning("Extracted %d emails from %s → %s", count, pst_path.name, target)
    return count


def extract_all(pst_dir: Path, eml_dir: Path, force: bool = False) -> dict[str, int]:
    """Extract all .pst files in pst_dir. Skip already-extracted ones unless force=True.

    Returns {pst_filename: email_count}.
    """
    results = {}
    pst_files = list(pst_dir.glob("*.pst")) + list(pst_dir.glob("*.PST"))
    if not pst_files:
        logger.warning("No .pst files found in %s", pst_dir)
        return results

    for pst_path in pst_files:
        target = eml_dir / pst_path.stem
        if not force and target.exists() and any(target.rglob("*.eml")):
            count = sum(1 for _ in target.rglob("*.eml"))
            logger.warning("Skipping %s — already extracted (%d emails)", pst_path.name, count)
            results[pst_path.name] = count
            continue
        try:
            count = extract_pst(pst_path, eml_dir)
            results[pst_path.name] = count
        except RuntimeError as e:
            logger.error("Failed to extract %s: %s", pst_path.name, e)
            results[pst_path.name] = -1

    return results
