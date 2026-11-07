"""Copy client.realm to a private work directory. The original file is only ever read."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

REALM_NAME = "client.realm"


class RealmCopyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RealmStat:
    mtime: float
    size: int


def realm_file(lazer_dir: Path) -> Path:
    return Path(lazer_dir) / REALM_NAME


def stat_realm(lazer_dir: Path) -> RealmStat:
    path = realm_file(lazer_dir)
    if not path.is_file():
        raise RealmCopyError(f"{path} が見つかりません。osu!lazer のデータフォルダを設定してください。")
    st = path.stat()
    return RealmStat(st.st_mtime, st.st_size)


def _clear_previous_copy(dest: Path) -> None:
    """Remove an old copy and the side files Realm creates next to it."""
    for p in (dest, dest.with_name(dest.name + ".lock"), dest.with_name(dest.name + ".note")):
        p.unlink(missing_ok=True)
    mgmt = dest.with_name(dest.name + ".management")
    if mgmt.is_dir():
        shutil.rmtree(mgmt, ignore_errors=True)


def copy_realm(lazer_dir: Path, work_dir: Path, retries: int = 3, delay: float = 1.0) -> tuple[Path, RealmStat]:
    """Copy client.realm into work_dir.

    If the file changes while it is being copied (lazer writing), the copy may be torn, so the
    mtime/size are compared before and after and the copy is retried.
    """
    src = realm_file(lazer_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / REALM_NAME
    tmp = work_dir / (REALM_NAME + ".copying")
    last_error: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(delay)
        try:
            before = stat_realm(lazer_dir)
            shutil.copyfile(src, tmp)
            after = stat_realm(lazer_dir)
        except PermissionError as e:
            last_error = e
            continue
        if before == after:
            _clear_previous_copy(dest)
            tmp.replace(dest)
            return dest, after
        last_error = RealmCopyError("コピー中に client.realm が更新されました")
    tmp.unlink(missing_ok=True)
    if isinstance(last_error, PermissionError):
        raise RealmCopyError(
            f"client.realm をコピーできませんでした (アクセス拒否)。osu!lazer を終了してから再試行してください: {last_error}"
        )
    raise RealmCopyError(f"client.realm を安定してコピーできませんでした: {last_error}")
