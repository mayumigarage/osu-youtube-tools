"""Command line entry point: python -m osu2yt <command>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, db, logging_setup
from .library import helper
from .matching import matcher
from .youtube import auth, playlists, quota
from .youtube.client import PRIVACY_VALUES, QuotaExceeded, YouTubeClient, YouTubeError
from .library.realm_copy import RealmCopyError, copy_realm
from .library.scanner import scan

STATUSES = ("unmatched", "needs_review", "confirmed", "skipped")


def _lazer_dir(conn, override: str | None) -> Path:
    if override:
        db.set_setting(conn, "lazer_path", override)
        return Path(override)
    return Path(db.get_setting(conn, "lazer_path") or config.default_lazer_dir())


def _progress(stage: str, done: int, total: int) -> None:
    if stage == "audio":
        if done == total or done % 50 == 0:
            print(f"  音声の長さを取得中 {done}/{total}", flush=True)
    else:
        print({"copy": "  client.realm をコピー中…", "export": "  ヘルパーで読み取り中…"}.get(stage, stage), flush=True)


def cmd_scan(args, conn) -> int:
    lazer = _lazer_dir(conn, args.lazer_path)
    print(f"スキャン: {lazer}")
    include_protected = args.include_protected or db.get_setting(conn, "include_protected") == "1"
    r = scan(conn, lazer, config.WORK_DIR, force=args.force, include_protected=include_protected,
             progress=_progress)
    if r.status == "skipped":
        print("client.realm は前回スキャンから変更なし → スキップ (--force で強制)")
    else:
        print(f"完了 (scan #{r.scan_id}): {r.total} セット / 新規 {r.new} / 消えた {r.gone} / 復活 {r.returned}")
    if r.durations_probed:
        print(f"音声ファイルから長さを取得: {r.durations_probed} 件")
    for w in r.warnings:
        print(f"  警告: {w}")
    return 0


def cmd_list(args, conn) -> int:
    where, params = ["1=1"], []
    if args.status:
        where.append("match_status = ?")
        params.append(args.status)
    if args.gone:
        where.append("in_library = 0")
    elif not args.all:
        where.append("in_library = 1")
    if args.pack:
        where.append("is_pack = 1")
    if args.q:
        where.append("(title LIKE ? OR title_unicode LIKE ? OR artist LIKE ? OR artist_unicode LIKE ? OR source LIKE ?)")
        params += [f"%{args.q}%"] * 5
    rows = conn.execute(
        f"""SELECT key, artist_unicode, title_unicode, duration_sec, duration_source, is_pack, in_library, match_status
            FROM beatmapsets WHERE {' AND '.join(where)} ORDER BY date_added DESC LIMIT ?""",
        (*params, args.limit),
    ).fetchall()
    for r in rows:
        dur = f"{r['duration_sec']:.0f}s" if r["duration_sec"] else "?"
        flags = ("P" if r["is_pack"] else "-") + ("-" if r["in_library"] else "G")
        print(f"{r['key']:<14} {flags} {dur:>6} {r['duration_source'] or '':<5} {r['match_status']:<12} "
              f"{r['artist_unicode']} - {r['title_unicode']}")
    print(f"({len(rows)} 件表示, flags: P=パック G=ライブラリから消えた)")
    return 0


def cmd_stats(args, conn) -> int:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print(f"ライブラリ内: {q('SELECT COUNT(*) FROM beatmapsets WHERE in_library=1')}")
    print(f"消えた:       {q('SELECT COUNT(*) FROM beatmapsets WHERE in_library=0')}")
    print(f"パック:       {q('SELECT COUNT(*) FROM beatmapsets WHERE in_library=1 AND is_pack=1')}")
    for src in ("audio", "map", "unknown"):
        n = conn.execute("SELECT COUNT(*) FROM beatmapsets WHERE in_library=1 AND duration_source=?", (src,)).fetchone()[0]
        print(f"長さ({src}): {n}")
    for st in STATUSES:
        n = conn.execute("SELECT COUNT(*) FROM beatmapsets WHERE in_library=1 AND match_status=?", (st,)).fetchone()[0]
        print(f"{st:<13} {n}")
    last = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        print(f"最終スキャン: #{last['id']} {last['started_at']} {last['status']} {last['error'] or ''}")
    return 0


def cmd_match(args, conn) -> int:
    scope = "keys" if args.keys else args.scope
    rows = matcher.select_rows(conn, scope, args.keys, args.limit)
    if not rows:
        print("対象の曲がありません")
        return 0
    print(f"マッチング: {len(rows)} 曲 (並列 {args.workers})")
    marks = {"confirmed": "OK ", "needs_review": "?? "}

    def progress(done, total, o):
        if o.error:
            line = f"ERR {o.key}: {o.error[:80]}"
        else:
            top = o.candidates[0] if o.candidates else None
            desc = f"{top.score.total:.2f} {top.info.title[:60]}" if top else "(候補なし)"
            reason = f" [{o.decision.reason}]" if o.decision.reason else ""
            line = f"{marks.get(o.decision.status, '')}{o.key}: {desc}{reason}"
        print(f"[{done}/{total}] {line}", flush=True)

    s = matcher.run_matching(conn, rows, workers=args.workers, progress=progress)
    print(f"完了: 確定 {s.confirmed} / 要確認 {s.needs_review} / 失敗 {s.failed}")
    return 0


def cmd_rescore(args, conn) -> int:
    rows = matcher.select_rows(conn, "auto")
    s = matcher.rescore(conn, rows)
    print(f"再採点 {s.total} 曲: 確定 {s.confirmed} / 要確認 {s.needs_review} (検索なし)")
    return 0


def cmd_show(args, conn) -> int:
    r = conn.execute("SELECT * FROM beatmapsets WHERE key=?", (args.key,)).fetchone()
    if not r:
        print(f"見つかりません: {args.key}")
        return 1
    print(f"{r['key']}  {r['artist_unicode']} - {r['title_unicode']}")
    print(f"  romaji: {r['artist']} - {r['title']}   source: {r['source']}")
    print(f"  長さ: {r['duration_sec']}s ({r['duration_source']})  pack={bool(r['is_pack'])}  in_library={bool(r['in_library'])}")
    print(f"  状態: {r['match_status']}  採用: {r['chosen_video_id']} ({r['chosen_by']})  理由: {r['review_reason']}")
    for c in conn.execute("SELECT * FROM candidates WHERE set_key=? ORDER BY score DESC", (args.key,)):
        mark = "*" if c["video_id"] == r["chosen_video_id"] else " "
        detail = json.loads(c["score_detail"] or "{}")
        parts = " ".join(f"{k[:3]}={v:+.2f}" for k, v in detail.get("parts", {}).items())
        dur = f"{c['duration_sec']:.0f}s" if c["duration_sec"] else "?"
        print(f" {mark}{c['score']:.2f} {c['video_id']} {dur:>5} [{c['channel']}{' ✓' if c['channel_verified'] else ''}] {c['title']}")
        print(f"        {parts}  {' '.join(detail.get('notes', []))}")
    return 0


def cmd_choose(args, conn) -> int:
    by = matcher.choose(conn, args.key, args.video)
    print(f"{args.key}: {args.video} を採用しました ({by})")
    return 0


def cmd_confirm(args, conn) -> int:
    matcher.confirm(conn, args.key)
    print(f"{args.key}: 現在の候補で確定しました")
    return 0


def cmd_skip(args, conn) -> int:
    if args.undo:
        print(f"{args.key}: スキップ解除 → {matcher.unskip(conn, args.key)}")
    else:
        matcher.skip(conn, args.key)
        print(f"{args.key}: スキップしました")
    return 0


def _client(conn) -> YouTubeClient:
    return YouTubeClient.from_saved_credentials(conn)


def _print_quota(conn) -> None:
    st = quota.status(conn)
    print(f"本日のクォータ: {st.used} / {st.limit} unit 使用 (余裕 {st.margin} を確保、追加可能 約 {st.inserts_possible} 曲)"
          f"  リセット: {st.reset_at.astimezone():%m/%d %H:%M}")


def cmd_auth(args, conn) -> int:
    if args.action == "login":
        auth.login_interactive(open_browser=not args.no_browser)
        print("ログインしました (トークンは data/token.json に保存)")
    elif args.action == "logout":
        print("ログアウトしました" if auth.logout() else "ログインしていません")
    else:
        try:
            auth.check_client_secret()
            print("client_secret.json: OK")
        except auth.AuthError as e:
            print(f"client_secret.json: {e}")
        creds = auth.load_credentials()
        print("ログイン状態: " + ("ログイン済み" if creds and creds.valid else "未ログイン"))
    return 0


def cmd_quota(args, conn) -> int:
    _print_quota(conn)
    for method, units in quota.breakdown(conn).items():
        print(f"  {method:<22} {units}")
    return 0


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_playlist(args, conn) -> int:
    if args.action == "current":
        row = playlists.active(conn)
        if not row:
            print("プレイリスト未選択 (`playlist create` か `playlist use` で選んでください)")
            return 0
        n = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=? AND present=1", (row["id"],)).fetchone()[0]
        print(f"{row['id']}  {row['title']}  ({row['privacy']})  記録済み {n} 件  最終取得 {row['last_pulled_at'] or '-'}")
        print(f"  https://www.youtube.com/playlist?list={row['id']}")
        return 0

    if args.action == "use":
        playlists.set_active(conn, args.arg)
        print(f"使用するプレイリスト: {args.arg}")
        return 0

    client = _client(conn)
    if args.action == "list":
        active = (playlists.active(conn) or {"id": None})["id"]
        for p in playlists.refresh_my_playlists(conn, client):
            mark = "*" if p.id == active else " "
            print(f"{mark} {p.id}  {p.title}  ({p.privacy}, {p.item_count} 件)")
    elif args.action == "create":
        if not args.arg:
            print("タイトルを指定してください: playlist create \"osu! songs\"")
            return 1
        _print_quota(conn)
        if not _confirm(f"プレイリスト「{args.arg}」({args.privacy}) を作成します (50 unit)。よろしいですか?", args.yes):
            print("中止しました")
            return 0
        p = playlists.create(conn, client, args.arg, args.privacy, args.description or "")
        print(f"作成しました: {p.id} ({p.privacy}) → 使用するプレイリストに設定")
        print(f"  https://www.youtube.com/playlist?list={p.id}")
    elif args.action == "pull":
        pid = args.arg or (playlists.active(conn) or {"id": None})["id"]
        if not pid:
            print("プレイリスト未選択")
            return 1
        r = playlists.pull_items(conn, client, pid)
        print(f"{pid}: {r.total} 件 (新たに見つかった {r.new_remote} / 消えていた {r.went_missing} / 重複 {r.duplicates})")
    _print_quota(conn)
    return 0


def cmd_videos(args, conn) -> int:
    region = args.region or db.get_setting(conn, "region") or "JP"
    r = playlists.check_chosen_videos(conn, _client(conn), region=region, force=args.force)
    print(f"確認 {r.checked} 本 ({region}): 再生不可 {len(r.unavailable)} 本 → 要確認に戻した曲 {r.sets_flagged}")
    for vid, a in r.unavailable.items():
        print(f"  {vid}: {a}")
    _print_quota(conn)
    return 0


def serve(port: int = 8765, open_browser: bool = True) -> int:
    import socket
    import threading
    import webbrowser

    import uvicorn

    from .app import create_app

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            print(f"ポート {port} は使用中です。既に起動していれば http://127.0.0.1:{port}/ を開いてください "
                  f"(別のポートは --port で指定)。", file=sys.stderr)
            return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"osu2yt を起動しました: {url}  (終了は Ctrl+C)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app = create_app()
    app.state.auto_resume.start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def cmd_sync(args, conn) -> int:
    from . import sync

    active = playlists.active(conn)
    if not active:
        print("プレイリスト未選択 (`playlist create` か `playlist use` で選んでください)")
        return 1
    p = sync.plan(conn, active["id"])
    est = sync.estimate(conn, p, args.max)
    print(f"同期先: {active['title']} ({active['id']})  最終取得 {p.pulled_at or '-'}")
    print(f"  確定 {p.confirmed_sets} 譜面 → 動画 {p.unique_videos} 本 (同じ動画は 1 回だけ追加)")
    print(f"  追加済み {p.already_present} / YouTube 上で削除済み {p.removed_remote} (再追加しない) / "
          f"再生不可 {p.unavailable} / 要確認 {p.needs_review} (対象外)")
    print(f"  追加予定 {est['count']} 本: 推定 {est['total_units']:,} unit "
          f"(取得 {est['pull_units']} + 生存確認 {est['check_units']} + 追加 {est['insert_units']:,})")
    if est["count"]:
        print(f"  本日追加できる見込み {est['inserts_today']} 本 / 1 日あたり約 {est['per_day']} 本 → 約 {est['days']} 日")
    if args.plan:
        for it in p.to_add[: args.max or len(p.to_add)]:
            extra = f" (+{len(it.set_keys) - 1} 譜面)" if len(it.set_keys) > 1 else ""
            print(f"    {it.video_id}  {it.artist} - {it.title}{extra}")
        return 0
    if not est["count"] and not args.dry_run:
        print("追加するものはありません (YouTube 側の最新の中身と照合するには --dry-run)。")
        return 0
    if not args.dry_run and not _confirm(
            f"{est['count']} 本をプレイリストに追加します (最大 {est['total_units']:,} unit)。よろしいですか?", args.yes):
        print("中止しました")
        return 0

    def progress(stage, done, total):
        if stage == "insert" and total and (done % 10 == 0 or done == total):
            print(f"  追加中 {done}/{total}", flush=True)

    r = sync.run_sync(conn, _client(conn), active["id"], dry_run=args.dry_run, max_add=args.max,
                      region=db.get_setting(conn, "region") or "JP", trigger="cli", progress=progress, note=print)
    print(f"結果 (#{r.run_id} {r.status}): 追加 {r.added} / 重複スキップ {r.skipped_dup} / 再生不可 {r.unavailable} / "
          f"失敗 {r.failed} / 残り {r.remaining}  消費 {r.quota_used} unit")
    _print_quota(conn)
    if r.status == "paused_quota":
        print("クォータ上限で一時停止しました。リセット後にもう一度 `sync` を実行すると続きから追加します "
              "(WebUI を起動していれば自動で再開します)。")
        return 2
    return 0


def cmd_history(args, conn) -> int:
    from . import sync

    if args.run_id:
        d = sync.run_detail(conn, args.run_id)
        if not d:
            print("見つかりません")
            return 1
        print(f"#{d['id']} {d['status']} {d['started_at']} → {d['finished_at'] or '-'}  "
              f"{'ドライラン ' if d['dry_run'] else ''}{d['playlist_title'] or d['playlist_id']}")
        if d["error"]:
            print(f"  {d['error']}")
        for it in d["items"]:
            err = f"  ({it['last_error']})" if it["last_error"] else ""
            print(f"  {it['state']:<13} {it['video_id']}  {it['artist_unicode']} - {it['title_unicode']}{err}")
        return 0
    for r in sync.runs(conn, args.limit):
        print(f"#{r['id']:<4} {r['started_at'][:16]}  {r['status']:<13} {'dry ' if r['dry_run'] else '    '}"
              f"予定 {r['planned'] or 0:>4} 追加 {r['added'] or 0:>4} 残り {r['remaining'] or 0:>4} "
              f"失敗 {r['failed'] or 0:>3}  {r['quota_used'] or 0:>6} unit  {r['trigger'] or ''}")
    return 0


def cmd_helper(args, conn) -> int:
    if args.action == "build":
        print(f"ビルド済み: {helper.build()}")
        return 0
    lazer = _lazer_dir(conn, args.lazer_path)
    realm, _ = copy_realm(lazer, config.WORK_DIR)
    sys.stdout.write(helper.schema(realm))
    return 0


from .sync import SyncError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m osu2yt", description="osu!lazer → YouTube プレイリスト")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="WebUI を起動 (コマンド省略時の既定)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")

    p = sub.add_parser("scan", help="osu!lazer ライブラリをスキャン")
    p.add_argument("--lazer-path", help="osu!lazer データフォルダ (保存される)")
    p.add_argument("--force", action="store_true", help="client.realm が未変更でもスキャン")
    p.add_argument("--include-protected", action="store_true", help="lazer 同梱譜面も含める")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("list", help="スキャン結果を表示")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--q", help="曲名/アーティスト/ソースで検索")
    p.add_argument("--pack", action="store_true", help="パック譜面のみ")
    p.add_argument("--gone", action="store_true", help="ライブラリから消えたもののみ")
    p.add_argument("--all", action="store_true", help="消えたものも含める")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("stats", help="件数の集計")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("match", help="YouTube 動画をマッチング")
    p.add_argument("--scope", choices=("unmatched", "needs_review", "auto"), default="unmatched",
                   help="auto = 自動判定済みの曲を全部やり直す (手動で選んだ曲は対象外)")
    p.add_argument("--keys", nargs="+", help="指定した曲だけ再マッチ (例: o:1003093)")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=3)
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("rescore", help="保存済み候補を現在の採点ルールで再判定 (検索しない)")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("show", help="曲の詳細と候補")
    p.add_argument("key")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("choose", help="候補/任意の動画を採用")
    p.add_argument("key")
    p.add_argument("video", help="動画ID または URL")
    p.set_defaults(func=cmd_choose)

    p = sub.add_parser("confirm", help="要確認の曲を現在の候補で確定")
    p.add_argument("key")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("skip", help="この曲をスキップ")
    p.add_argument("key")
    p.add_argument("--undo", action="store_true", help="スキップ解除")
    p.set_defaults(func=cmd_skip)

    p = sub.add_parser("auth", help="YouTube (Google) ログイン")
    p.add_argument("action", choices=("login", "status", "logout"))
    p.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない (URL を表示)")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("playlist", help="プレイリストの一覧/作成/選択/取得")
    p.add_argument("action", choices=("list", "create", "use", "pull", "current"))
    p.add_argument("arg", nargs="?", help="create: タイトル / use: プレイリストID / pull: プレイリストID (省略時は選択中)")
    p.add_argument("--privacy", choices=PRIVACY_VALUES, default="private", help="既定: private (非公開)")
    p.add_argument("--description")
    p.add_argument("--yes", action="store_true", help="確認せずに実行")
    p.set_defaults(func=cmd_playlist)

    p = sub.add_parser("quota", help="本日のクォータ使用量")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("videos", help="採用動画の生存確認 (videos.list, 50本で1unit)")
    p.add_argument("action", choices=("check",))
    p.add_argument("--region", help="地域コード (既定: 設定値 or JP)")
    p.add_argument("--force", action="store_true", help="最近確認した動画も再確認")
    p.set_defaults(func=cmd_videos)

    p = sub.add_parser("sync", help="確定した曲をプレイリストに追加 (差分のみ)")
    p.add_argument("--dry-run", action="store_true", help="中身の取得と生存確認だけ行い、追加はしない")
    p.add_argument("--plan", action="store_true", help="ローカルの情報だけで追加予定を表示 (API を使わない)")
    p.add_argument("--max", type=int, help="今回追加する最大本数")
    p.add_argument("--yes", action="store_true", help="確認せずに実行")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("history", help="同期の履歴")
    p.add_argument("run_id", nargs="?", type=int, help="指定するとその回の詳細")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("helper", help=".NET ヘルパーの操作")
    p.add_argument("action", choices=("build", "schema"))
    p.add_argument("--lazer-path")
    p.set_defaults(func=cmd_helper)

    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging_setup.setup()
    config.ensure_dirs()
    if args.command in (None, "serve"):
        return serve(getattr(args, "port", 8765), not getattr(args, "no_browser", False))
    conn = db.connect(config.DB_PATH)
    try:
        return args.func(args, conn)
    except QuotaExceeded as e:
        print(f"クォータ: {e}", file=sys.stderr)
        return 2
    except (helper.HelperError, RealmCopyError, matcher.MatchActionError, auth.AuthError, YouTubeError,
            SyncError) as e:
        print(f"エラー: {logging_setup.mask(str(e))}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
