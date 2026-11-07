"""Locate, build and run the .NET client.realm export helper."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

EXE_NAME = "OsuRealmExport.exe" if os.name == "nt" else "OsuRealmExport"

# Exit codes defined in helper/OsuRealmExport/Program.cs
EXIT_OPEN, EXIT_SCHEMA = 2, 3


class HelperError(RuntimeError):
    """Raised with a user-facing (Japanese) message."""


def dotnet_path() -> str | None:
    return shutil.which("dotnet")


def exe_path() -> Path:
    return config.HELPER_PUBLISH_DIR / EXE_NAME


def _source_mtime() -> float:
    files = [*config.HELPER_PROJECT_DIR.glob("*.cs"), *config.HELPER_PROJECT_DIR.glob("*.csproj")]
    return max((f.stat().st_mtime for f in files), default=0.0)


def needs_build() -> bool:
    exe = exe_path()
    return not exe.exists() or exe.stat().st_mtime < _source_mtime()


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    # MSBuild node reuse keeps child processes holding our pipes open, which made builds hang.
    env["MSBUILDDISABLENODEREUSE"] = "1"
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env["DOTNET_NOLOGO"] = "1"
    return env


def build(timeout: float = 600) -> Path:
    dotnet = dotnet_path()
    if not dotnet:
        raise HelperError(
            ".NET SDK が見つかりません。https://dotnet.microsoft.com/download から .NET 9 SDK を"
            "インストールしてください (winget なら: winget install Microsoft.DotNet.SDK.9)。"
        )
    csproj = config.HELPER_PROJECT_DIR / "OsuRealmExport.csproj"
    cmd = [
        dotnet, "publish", str(csproj), "-c", "Release", "-o", str(config.HELPER_PUBLISH_DIR),
        "--nologo", "-v", "q", "--disable-build-servers", "-nodeReuse:false",
    ]
    log.info("building realm helper: %s", " ".join(cmd[1:3]))
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired as e:
        raise HelperError(f"ヘルパーのビルドが {timeout:.0f} 秒以内に終わりませんでした。") from e
    if proc.returncode != 0 or not exe_path().exists():
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        raise HelperError(f"ヘルパーのビルドに失敗しました (exit {proc.returncode}):\n{tail}")
    # publish may not touch the exe when nothing changed; make the freshness check stable
    exe_path().touch()
    return exe_path()


def ensure_built() -> Path:
    if needs_build():
        return build()
    return exe_path()


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    exe = ensure_built()
    try:
        return subprocess.run(
            [str(exe), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired as e:
        raise HelperError(f"ヘルパーが {timeout:.0f} 秒以内に終了しませんでした。") from e


def export(realm_copy: Path, out_json: Path, timeout: float = 300) -> dict:
    """Run `export` against an already copied realm file and return the parsed JSON."""
    proc = _run(["export", "--realm", str(realm_copy), "--out", str(out_json)], timeout)
    if proc.returncode == EXIT_OPEN:
        raise HelperError(
            "client.realm を開けませんでした。osu!lazer の更新でファイル形式が変わった可能性があります "
            f"(ヘルパーの Realm バージョン更新が必要)。詳細: {proc.stderr.strip()}"
        )
    if proc.returncode == EXIT_SCHEMA:
        raise HelperError(f"client.realm に BeatmapSet が見つかりません: {proc.stderr.strip()}")
    if proc.returncode != 0:
        raise HelperError(f"ヘルパーがエラー終了しました (exit {proc.returncode}): {proc.stderr.strip()}")
    with open(out_json, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != 1:
        raise HelperError(f"未対応のヘルパー出力形式です: {data.get('format')}")
    return data


def schema(realm_copy: Path, timeout: float = 120) -> str:
    proc = _run(["schema", "--realm", str(realm_copy)], timeout)
    if proc.returncode != 0:
        raise HelperError(f"schema 取得に失敗しました (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout
