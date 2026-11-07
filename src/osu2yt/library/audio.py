"""Audio length via ffprobe, reading lazer's hash-named files read-only."""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def lazer_file_path(lazer_dir: Path, file_hash: str) -> Path:
    """lazer stores files as files/<h[0]>/<h[0:2]>/<hash>."""
    return Path(lazer_dir) / "files" / file_hash[0] / file_hash[:2] / file_hash


def probe_duration(path: Path, ffprobe: str, timeout: float = 30) -> float | None:
    if not path.is_file():
        return None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        value = float(proc.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


def probe_many(
    lazer_dir: Path,
    hashes: Iterable[str],
    workers: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, float | None]:
    """Durations (seconds) for each audio hash; None when unavailable. Empty dict if no ffprobe."""
    ffprobe = ffprobe_path()
    hashes = list(dict.fromkeys(hashes))
    if not ffprobe or not hashes:
        return {}
    results: dict[str, float | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {h: pool.submit(probe_duration, lazer_file_path(lazer_dir, h), ffprobe) for h in hashes}
        for i, (h, fut) in enumerate(futures.items(), start=1):
            results[h] = fut.result()
            if progress:
                progress(i, len(hashes))
    return results
