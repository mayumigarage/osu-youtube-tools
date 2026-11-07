# osu2yt 設計 (v1.0 / ステップ 1〜8 実装済み)

osu!lazer の client.realm から曲一覧を読み、yt-dlp で YouTube 動画をマッチングし、YouTube Data API でプレイリストへ差分追加するローカル WebUI ツール。

## 0. 全体構成

```
ブラウザ (HTML + ES Modules, ビルド不要)
   │  fetch /api/*  (1秒ポーリングで進捗取得)
   ▼
Python 3.11+ / FastAPI / uvicorn  ── 127.0.0.1 のみ bind
   ├─ library   : realm を一時コピー → .NET ヘルパー起動 → JSON 取込 → SQLite
   ├─ matching  : yt_dlp (Python ライブラリ) で ytsearch → スコアリング
   ├─ youtube   : OAuth (loopback) + Data API v3 (playlists / playlistItems / videos)
   ├─ jobs      : バックグラウンドジョブ (1本ずつ直列実行・中断可能)
   └─ SQLite    : data/osu2yt.db
         ▲
.NET 9 CLI ヘルパー (OsuRealmExport.exe)
   Realm 20.1.0, IsDynamic=true, IsReadOnly=true, コピーしたファイルだけを開く
```

- 起動: `python -m osu2yt` → ヘルパー未ビルド/ソース更新ありなら `dotnet publish` を自動実行 → サーバー起動 → ブラウザを開く
- ヘルパーのビルドは `--disable-build-servers -nodeReuse:false` 付き (調査時、MSBuild のノード再利用でパイプが閉じずハングしたため)
- ヘルパー出力は stdout ではなく `--out <json>` でファイルに UTF-8 書き出し (文字化け・パイプ詰まり回避)
- yt-dlp は exe ではなく pip の `yt-dlp` パッケージを import して使う (プロセス起動コスト削減、例外処理しやすい)。winget の exe はなくても動く
- 曲の長さは ffprobe (音声ファイル実測) を優先、無ければ譜面 Length の最大値。ハッシュ単位でキャッシュ

## 1. ディレクトリ構成

```
osu-youtube-tools/
├─ pyproject.toml              # 依存: fastapi, uvicorn, yt-dlp, google-api-python-client,
│                              #       google-auth-oauthlib, pydantic / dev: pytest, httpx
├─ README.md
├─ .gitignore                  # data/, helper/bin/, helper/obj/, client_secret*.json, token*.json
├─ docs/DESIGN.md
├─ helper/
│  └─ OsuRealmExport/
│     ├─ OsuRealmExport.csproj # net9.0, Realm 20.1.0
│     └─ Program.cs            # export / schema サブコマンド
├─ src/osu2yt/
│  ├─ __main__.py              # 起動 (helper 準備 → uvicorn → ブラウザ)
│  ├─ app.py                   # FastAPI 生成, ルータ登録, static 配信
│  ├─ config.py                # パス解決 (data dir, lazer dir 既定値)
│  ├─ db.py                    # 接続, マイグレーション (PRAGMA user_version)
│  ├─ jobs.py                  # ジョブランナー (進捗/キャンセル)
│  ├─ logging_setup.py         # トークン・secret をマスクするフィルタ
│  ├─ library/
│  │  ├─ helper.py             # ヘルパーの検出/ビルド/実行
│  │  ├─ realm_copy.py         # 安全なコピー (前後で mtime/size 比較, 再試行)
│  │  ├─ scanner.py            # 取込・差分判定
│  │  ├─ normalize.py          # メタデータ正規化
│  │  └─ audio.py              # ffprobe で長さ取得
│  ├─ matching/
│  │  ├─ search.py             # yt-dlp ytsearch ラッパ
│  │  ├─ scoring.py            # 候補スコア計算 (純関数)
│  │  └─ matcher.py            # クエリ生成→検索→保存→自動採用/要確認
│  ├─ youtube/
│  │  ├─ auth.py               # OAuth loopback フロー, トークン保存/更新
│  │  ├─ client.py             # API 呼び出し + リトライ + クォータ計上
│  │  └─ quota.py              # 太平洋時間の日付単位で消費量管理
│  │  └─ playlists.py          # プレイリストの記録・中身の取得・生存確認
│  ├─ sync.py                  # 差分計画・実行・一時停止/再開・履歴
│  ├─ scheduler.py             # クォータリセット後の自動再開 (serve 時のみ)
│  ├─ api/                     # deps, core (状態/設定/ジョブ/スキャン/マッチ), library,
│  │                           # youtube (認証/プレイリスト/生存確認), sync
│  └─ web/                     # index.html, app.js, api.js, util.js, views/*.js, style.css
├─ tests/                      # 10 章参照 (fake_youtube.py, conftest.py でネットワーク遮断)
└─ data/                       # 実行時生成・gitignore
   ├─ osu2yt.db
   ├─ client_secret.json       # ユーザーが配置
   ├─ token.json
   └─ work/                    # realm コピー, ヘルパー出力 JSON
```

## 2. .NET ヘルパー仕様

```
OsuRealmExport.exe export --realm <コピー済み client.realm> --out <out.json>
OsuRealmExport.exe schema --realm <...>          # 診断用: クラス/プロパティ一覧
終了コード: 0=成功, 2=開けない(形式/バージョン不一致), 3=必要クラス欠落, 1=その他
```

出力 JSON (BeatmapSet 単位。難易度は集約済み):

```json
{
  "format": 1,
  "warnings": ["Beatmap.Length not found"],
  "sets": [{
    "id": "guid", "onlineId": 1003093, "hash": "...",
    "dateAdded": "2022-07-18T16:24:42Z", "deletePending": false, "protected": false,
    "difficultyCount": 4, "maxLengthMs": 87671,
    "variants": [
      {"title": "...", "titleUnicode": "...", "artist": "...", "artistUnicode": "...",
       "source": "...", "tags": "...", "audioFile": "om.mp3", "audioHash": "0698d2...",
       "maxLengthMs": 87671, "difficultyCount": 4}
    ]
  }]
}
```

- `variants` = (Artist, Title, AudioFile) の重複を除いた組。通常は1件。パック譜面や速度違い音源で複数件
- プロパティは schema を見て存在確認してから取得。欠けていれば null + warnings (lazer 更新に強くする)

## 3. スキャン / 差分検知

1. `client.realm` の mtime+size を前回スキャンと比較。同じで force でなければスキップ
2. `data/work/` へコピー (コピー前後で mtime/size が変わったら最大3回再試行)。元ファイルは読み取りのみ
3. ヘルパー実行 → JSON 取込
4. キー: `onlineId > 0` なら `o:<onlineId>`、それ以外は `g:<guid>`
5. 既存キーと比較:
   - 新規キー → 追加 (status=unmatched)
   - 既存キー → メタデータ更新 (マッチ結果は保持)
   - 今回出てこない or DeletePending → `in_library=0` (表示のみ。プレイリストからは消さない)
   - 消えたキーが再出現 → `in_library=1` に戻す
6. Protected (lazer 同梱の triangles 等) は既定で除外。設定で含められる
7. 長さ: 新規/音声ハッシュ変更分だけ ffprobe (並列4)

## 4. マッチング

- クエリ (順に試す。上位候補のスコアが十分なら打ち切り):
  1. `ArtistUnicode TitleUnicode`
  2. `Artist Title` (ローマ字。1 と同一なら省略)
  3. `TitleUnicode` のみ
  4. 日本語メタデータが無くローマ字として読めるタイトル (`Arumagedon`) は `Artist カタカナ` (`アルマゲドン`)。YouTube はローマ字を英単語に「訂正」してしまうため
  - クエリからは `(TV Size)` `(Cut Ver.)` `(Short Ver.)` `(Game Ver.)` `(osu! edit)` `-TV MIX-` `anime size` 等を除去 (スコア計算時には考慮)
  - YouTube の検索演算子になる文字を無害化: 語頭/語末の `-` `~` (`-sha-` が「sha を除外」になる), `"` `|`, 語頭の `#` `+` `@`。語中のハイフン (`shinra-bansho`) は残す
  - 検索結果が 0 件のクエリは `… song` → 助詞 (no/wo/ni …) 抜きの順で再試行。YouTube は `Hatsune Miku no Shoushitsu` のような一部のローマ字フレーズに 0 件を返す (2026-11-07 確認)
- `ytsearch8` × クエリ数 (1 ページ分なのでリクエスト数は同じ)、候補は重複除去して最大10件保存 (タイトル/チャンネル/チャンネルID/認証済み/長さ/サムネ)
- 並列3、失敗時は指数バックオフで再試行
- スコア (0〜1, 内訳を JSON 保存して UI に表示):

| 要素 | 内容 | 重み(案) |
|---|---|---|
| 長さ | 音声長との差 ≤3s 満点 / ≤10s 部分 / >30s ほぼ0 | 0.35 |
| 曲名 | 正規化後の動画タイトルに曲名 (Unicode or ローマ字) を含む | 0.25 |
| アーティスト | 動画タイトル or チャンネル名にアーティスト名を含む | 0.20 |
| 公式性 | 認証済みチャンネル / `アーティスト - Topic` チャンネル / チャンネル名=アーティスト | 0.20 |
| 減点 | cover, 歌ってみた, 弾いてみた, nightcore, sped up, 耐久, live, reaction, remix, dj, tiktok, trailer, 予告, ヲタ芸, 音程/pitch (譜面タイトルに無い語のみ) | -0.3 |
| 減点 | 音ゲーのプレイ・譜面確認動画 (下記) | -0.45 / -0.3 |

- 短縮版の扱い (決定事項3): 譜面タイトルに `TV Size` / `TV Ver.` / `Cut Ver.` / `Short Ver.` / `Game Ver.` があれば「フル尺優先モード」
  - 長さスコア: 「音声長 +20s 〜 音声長×4 (上限 600s)」を満点、音声長 ±15s (=短縮版そのもの) は 0.4 倍、それ以外は低め
  - 動画タイトルに `TV Size` / `TVサイズ` / `TV ver` / `short` 等を含む候補は -0.15
  - 検索クエリは短縮タグを除去したものを使う
- 自動採用 (`confirmed(auto)`) の条件 — 実データ100曲で調整済み (2026-11-06)。満たさなければ `needs_review` + 理由:
  - 1位スコア ≥ 0.70 (`low_score`)
  - 曲名 (Unicode かローマ字、括弧・短縮タグ除去後) が動画タイトルに丸ごと含まれる (`title_mismatch`)。部分一致 (75%以上の連続一致) は順位付けの加点のみ
  - アーティスト名がタイトル/チャンネルに含まれる (artist ≥ 0.5)、またはアーティスト本人/Topic チャンネル (`artist_mismatch`)
  - 2位との差 ≥ 0.08 (`close_second`)。ただし 1位 ≥ 0.85、または差が小さい候補の長さが ±5s 以内 (同じ音源の別アップロード) なら問題なし
  - パック譜面は常に要確認 (`pack`)、候補ゼロは `no_results`
- 再マッチは「自動で決めたもの」だけを上書き。ユーザーが選んだ/手動指定した動画は残し、候補一覧だけ更新する
- 音ゲーのプレイ・譜面確認動画 (`chart_signals`、UI では「譜面/プレイ動画」チップ):
  - 強いサイン -0.45 (=どれだけ他が満点でも自動採用されない): 譜面, 手元, map by, gameplay, played by, autoplay 等の語、
    `[Insane]` `(MXM)` `[CHAOS Lv.15]` `[pkhg's Insane]` `Nasya's Lunatic` `[Pure] {Amats}` `[CSP 17]` `(in. 15)`、
    ★7.09 / 6.3★ / 12★ / (2.29*)、412pp、98.5% / 99%、HDDT / +HD、SS / FC / AP / PUC / S rank / 1xMiss、7K、AR9.5 / CS6.5、
    990,063 などのスコア表記、choke / catch the beat / mouse only / tech map、音ゲー名 + play/playing/プレイ
    (`Playing「…」in maimai` はアーティスト本人の動画でも減点)、チャンネル名の replay / fanmade / 譜面
  - 弱いサイン -0.30: 音ゲー名だけ (osu, SDVX, IIDX, maimai, CHUNITHM, オンゲキ, 太鼓の達人, Arcaea, Phigros, ADOFAI, BMS, DTXMania …)、
    チャンネル名の osu、osu! のメタデータ行そのまま + 括弧 (`Artist - Title [Saikyou!!]`。括弧の中身が難易度名らしいときだけ:
    `[Monstercat Release]` `[Lyrics]` `[Dubstep]` `【フル】` `【試聴】` `[NOFX]` 等や、lyrics / official / MV / 字幕 を含む動画は対象外)
  - 譜面のタイトル/アーティスト/ソースに同じ語があれば無視 (SDVX 出身曲の `[SDVX] …` は普通の音源のことが多い。
    `[what's done is done]` のように括弧が曲名そのものなら難易度扱いしない)。
    音ゲー名はタグにあっても無視 (osu だけはタグでは免除しない)。`【音源】` `GST` `BGA` `NOFX` 付きは音ゲー名だけなら減点しない
  - アーティスト本人のアップロードは音ゲー名と紛らわしい括弧表記 (`[ULTIMATE]` 等) を減点しない
- ローマ字・特殊文字への対応 (曲名/アーティスト一致の補助。通常の一致で満点が出ないときだけ):
  - ローマ字の揺れを統一 (Shōshitsu / Shoushitsu / Shoshitsu、l→r、m+b/p→n、wo→o) し、動画タイトルのかなもローマ字に変換して比較 (漢字は辞書が無いので変換しない)。日本語メタデータがある曲か、タイトル全体がローマ字として読める曲だけ (英語曲の Shot/Shout を混同しない)。4 文字未満のキーは使わない
  - 記号は 1 文字の置き換え/省略を許す (`Gregorius | Symphony` ≈ `Gregorius I Symphony`, `TRI△NGLE` ≈ `TRIANGLE`)、ラテン文字のアクセントは除去 (`Cö shu Nie`, `café`)、`feat.` 以降を除いた曲名でも比較
- CLI: `match [--scope unmatched|needs_review|auto] [--keys ...]`, `rescore` (保存済み候補を再採点、検索なし), `show KEY`, `choose KEY ID|URL`, `confirm KEY`, `skip KEY [--undo]`
- 実績 (834曲): 自動確定 647 / 要確認 187 / 失敗 0。確定のランダム抽出70件の目視で誤りは修正済みの数件のみ
- 2026-11-07 譜面動画対策・ローマ字検索の追加後 (要確認曲を再検索 + 再採点): 確定 678 / 要確認 153 / スキップ 3
- パック譜面 (variants が複数で曲が違う) は常に `needs_review` (パックバッジ付き)
- 正規化: NFKC, 小文字化, 全角/半角統一, 記号・空白除去, `feat.`/`ft.` 統一, 括弧内の除去版も比較
- 手動操作: 候補切替 / URL・動画ID直指定 (`manual`) / スキップ / 再検索

## 5. YouTube 連携

- OAuth: Google Cloud の「デスクトップアプリ」クライアントの `client_secret.json` を `data/` に置く
  - `InstalledAppFlow.run_local_server(host=127.0.0.1, port=0)` の一時 loopback サーバーで受け取る
  - スコープ: `https://www.googleapis.com/auth/youtube`
  - トークンは `data/token.json`。ログには一切出さない (ログフィルタでマスク)
  - README 注意: OAuth 同意画面が「テスト」状態だとリフレッシュトークンが7日で失効する → 再ログインが必要
- 使う API とクォータ:

| 操作 | API | units |
|---|---|---|
| 自分のプレイリスト一覧 | playlists.list | 1/ページ |
| プレイリスト作成 | playlists.insert | 50 |
| 既存内容の取得 (重複回避) | playlistItems.list | 1/50件 |
| 動画の生存確認 (削除/非公開/地域制限/埋込不可) | videos.list | 1/50件 |
| 追加 | playlistItems.insert | 50/件 |

- クォータの日付は太平洋時間 0:00 リセットに合わせて計上。上限 10,000 と安全マージン (既定 300) は設定可能
- `quotaExceeded` / `dailyLimitExceeded`、または手元の予算 (上限 − マージン) 不足 → 呼び出し前に止めて paused_quota → リセット後に自動再開 (6 章)
- 5xx / ネットワークエラー / `backendError` → 指数バックオフ (1,2,4,8,16s + ジッタ, 最大5回)
- 地域制限は設定の国コード (既定 JP) で判定

## 6. 同期フロー

```
プレビュー (GET /api/sync/preview, ローカルのみ・クォータ 0)
  確定譜面 → 動画 ID で重複排除 → playlist_items と照合 → 追加予定 + 見積もり
        │ 確認ダイアログ (追加本数・最大 unit・今日の上限・リセット時刻)
        ▼
実行 (POST /api/sync → ジョブ, sync.run_sync)
  0. sync_runs に running で記録。以前の run の pending は carried_over にする
  1. playlistItems.list で今の中身を取得 → playlist_items に反映 (消えたものは present=0)
  2. 計画を作り直し、sync_queue に pending で登録 (古い譜面から順, --max で上限)
  3. 7 日以上未確認の動画だけ videos.list → 再生不可は unavailable にして譜面を needs_review へ
  4. dry_run ならここで終了 (pending → planned)
  5. 1 本ずつ playlistItems.insert → 成功ごとに playlist_items / sync_queue をコミット
       - 挿入前に present=1 を再確認 (dup)
       - videoNotFound → unavailable、playlistNotFound → 全体を中止
       - その他のエラー → failed として次へ。5 回連続で中止
       - 中断要求 → cancelled
  6. QuotaExceeded (Google の quotaExceeded / 手元の予算不足) → paused_quota で安全に停止
  7. 残り件数・消費 unit (quota_log の差分) を sync_runs に記録
```

- 同じ動画 ID が複数セットに採用されていても追加は 1 回。代表 (最古) のセットを playlist_items.set_key に記録
- YouTube 上でユーザーが外した動画 (present=0) は再追加しない。ツールからの削除は一切しない
- 再開 = 次の run が差分を計算し直すだけ (キューの引き継ぎは不要)。`scheduler.AutoResume` が 5 分ごとに
  「最新の本番 run が paused_quota かつ PT 日付が変わった」かを確認し、予算があれば自動で同期ジョブを起動
  (設定 `auto_resume`、`serve` 起動時のみ動作)
- サーバー停止で running のまま残った run は起動時に interrupted にする

## 7. DB スキーマ (SQLite)

```sql
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- lazer_path, region, quota_limit, quota_margin, active_playlist_id, include_protected, ...

CREATE TABLE scans (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
  realm_mtime REAL, realm_size INTEGER,
  status TEXT,                       -- running/done/error
  sets_total INTEGER, sets_new INTEGER, sets_gone INTEGER, warnings TEXT, error TEXT);

CREATE TABLE audio_durations (       -- ffprobe 結果キャッシュ
  audio_hash TEXT PRIMARY KEY, duration_sec REAL);

CREATE TABLE beatmapsets (
  key TEXT PRIMARY KEY,              -- 'o:<onlineId>' | 'g:<guid>'
  online_id INTEGER, realm_guid TEXT,
  title TEXT, title_unicode TEXT, artist TEXT, artist_unicode TEXT, source TEXT, tags TEXT,
  audio_hash TEXT, duration_sec REAL, duration_source TEXT,   -- 'audio'|'map'
  variants_json TEXT, is_pack INTEGER DEFAULT 0,
  date_added TEXT, first_seen_scan INTEGER, last_seen_scan INTEGER,
  in_library INTEGER DEFAULT 1,
  match_status TEXT DEFAULT 'unmatched',  -- unmatched/needs_review/confirmed/skipped
  chosen_video_id TEXT, chosen_by TEXT,   -- auto/user/manual
  match_score REAL, review_reason TEXT, matched_at TEXT, updated_at TEXT);
CREATE INDEX ix_sets_status ON beatmapsets(match_status, in_library);

CREATE TABLE candidates (
  set_key TEXT REFERENCES beatmapsets(key) ON DELETE CASCADE,
  video_id TEXT, rank INTEGER, query TEXT,
  title TEXT, channel TEXT, channel_id TEXT, channel_verified INTEGER,
  duration_sec REAL, thumbnail_url TEXT,
  score REAL, score_detail TEXT, fetched_at TEXT,
  PRIMARY KEY (set_key, video_id));

CREATE TABLE video_checks (          -- videos.list の結果キャッシュ
  video_id TEXT PRIMARY KEY, availability TEXT,  -- ok/deleted/private/region_blocked/not_embeddable
  checked_at TEXT);

CREATE TABLE playlists (
  id TEXT PRIMARY KEY, title TEXT, privacy TEXT, created_by_tool INTEGER, created_at TEXT);

CREATE TABLE playlist_items (
  playlist_id TEXT, video_id TEXT, playlist_item_id TEXT,
  set_key TEXT,                      -- ツール外で追加されたものは NULL
  origin TEXT,                       -- tool/remote
  added_at TEXT, sync_run_id INTEGER,
  PRIMARY KEY (playlist_id, video_id));

CREATE TABLE sync_runs (
  id INTEGER PRIMARY KEY, playlist_id TEXT, started_at TEXT, finished_at TEXT,
  status TEXT,                       -- running/done/paused_quota/cancelled/error
  dry_run INTEGER, planned INTEGER, added INTEGER, skipped_dup INTEGER, failed INTEGER,
  quota_used INTEGER, error TEXT,
  trigger TEXT, unavailable INTEGER, remaining INTEGER, pt_date TEXT);   -- v3

CREATE TABLE sync_queue (
  sync_run_id INTEGER, set_key TEXT, video_id TEXT,
  state TEXT,                        -- pending/planned/added/dup/unavailable/failed/carried_over
  attempts INTEGER DEFAULT 0, last_error TEXT,
  position INTEGER, updated_at TEXT,  -- v3
  PRIMARY KEY (sync_run_id, set_key));

CREATE TABLE quota_log (
  id INTEGER PRIMARY KEY, ts TEXT, pt_date TEXT, method TEXT, units INTEGER);
```

マイグレーション: v2 で `playlist_items.present / last_seen_at` (YouTube 上で外されたものを 0) と
`playlists.item_count / last_pulled_at`、v3 で上記の同期用カラムを追加。

ライブラリ画面の状態表示 = `match_status` + 選択中プレイリストの `playlist_items` 有無 → 未マッチ/要確認/確定/追加済み/スキップ。

## 8. API エンドポイント

すべて `/api` 配下。状態を変えるリクエストは `X-Osu2yt: 1` ヘッダ必須 (CSRF 対策)、Host / Origin は localhost のみ (DNS リバインディング対策)。

| メソッド | パス | 内容 |
|---|---|---|
| GET | /api/status | 環境チェック, 認証状態, 選択中プレイリスト, クォータ, 最終スキャン, ジョブ |
| GET/PUT | /api/settings | lazer_path, region, quota_limit, quota_margin, include_protected, auto_resume |
| GET | /api/jobs/current, /api/jobs/{id} | 進捗 (done/total/メッセージ/ログ末尾/結果) |
| POST | /api/jobs/{id}/cancel | 中断要求 |
| POST | /api/library/scan | スキャン開始 `{force}` → job |
| POST | /api/match | マッチング開始 `{scope: unmatched/needs_review/auto/keys, keys?, workers}` → job |
| POST | /api/match/rescore | 保存済み候補で再判定 (検索なし) |
| GET | /api/quota | 今日の消費と内訳 |
| GET | /api/library | 一覧 `?view=&q=&pack=&reason=&sort=&page=&per_page=` + 件数 |
| GET | /api/library/keys | 現在の絞り込みでのキー一覧 (詳細画面の前後移動用) |
| GET | /api/library/{key} | 詳細 + 候補 + 同じ動画を使う譜面 |
| POST | /api/library/{key}/choose | 候補 or 任意 URL/ID を採用 `{video}` |
| POST | /api/library/{key}/confirm, /skip, /unskip, /rematch | 確定 / スキップ / 解除 / この曲だけ再検索 |
| GET | /api/auth | ログイン処理の状態 |
| POST | /api/auth/login, /api/auth/logout | loopback OAuth をスレッドで開始 / 失効 + 削除 |
| GET | /api/playlists | 記録済みの一覧と選択中 |
| POST | /api/playlists/refresh | 自分のプレイリスト一覧を取得 (1 unit) |
| POST | /api/playlists | 新規作成 `{title, privacy}` (50 unit, 選択中にする) |
| PUT | /api/playlists/active | 使うプレイリストを選択 |
| POST | /api/playlists/pull | 選択中の中身を取得 |
| POST | /api/videos/check | 採用動画の生存確認 |
| GET | /api/sync/preview | 追加予定・除外内訳・見積もり・前回 run・自動再開の予定 (クォータ 0) |
| POST | /api/sync | 同期開始 `{dry_run, max_add}` → job |
| GET | /api/sync/runs, /api/sync/runs/{id} | 同期履歴と明細 |

当初案から外したもの: Web 版 OAuth コールバック (デスクトップ loopback に統一)、`/api/sync/resume` と
`/api/sync/auto` (再開は再実行で足りるため)、プレイリストからの削除 API (「自動削除しない」方針を優先)。

## 9. 画面

SPA (hash ルーティング)、上部に常時「実行中ジョブの進捗バー」と「今日のクォータ x / 10,000」を表示。

1. **設定** `#/settings`
   - lazer データパス (既定 `%APPDATA%\osu`, 存在チェック), 環境チェック一覧 (.NET / ヘルパー / ffprobe / yt-dlp)
   - Google 認証状態 + ログイン/ログアウト、client_secret.json 未配置時は手順リンク
   - プレイリスト選択 (一覧から) / 新規作成 (タイトル, 公開設定: 非公開[既定]/限定公開/公開)
   - 詳細: 地域コード, クォータ上限/マージン, Protected 譜面を含めるか
2. **ライブラリ** `#/library`
   - 状態タブ (全部/未マッチ/要確認/確定/追加済み/スキップ/消えた) + 件数
   - 検索 (曲名/アーティスト/ソース), パックのみ, 並び替え (追加日/スコア)
   - 行: サムネ, Artist - Title (Unicode/ローマ字), 長さ, 採用動画, スコア, 状態
   - 一括操作: 選択分を再検索/スキップ
3. **曲詳細** `#/library/<key>`
   - メタデータ, 音声長, variants (パック時は全曲表示)
   - 候補リスト (スコア内訳表示) + youtube-nocookie 埋め込みプレビュー
   - 採用 / URL 指定 / スキップ / 再検索、前後の要確認曲へ移動
4. **同期** `#/sync`
   - 「同期」→ 進捗 → 要確認があれば件数とリンク
   - プレビュー: 追加予定, 重複除外, 生存確認で弾かれた曲, 推定クォータ, 今日中に追加できる件数
   - 実行 / dry-run / 一時停止中 run の再開
5. **履歴** `#/history`
   - run 一覧 (日時, プレイリスト, 追加数, 状態, 消費クォータ) → 明細

## 10. テスト方針

`pytest` で約 200 件。YouTube API は `tests/fake_youtube.py` のフェイク、yt-dlp 検索は `conftest.py` で
ネットワークを遮断したうえで差し替え、ヘルパーは JSON で代替。

| ファイル | 内容 |
|---|---|
| test_normalize.py | 表記ゆれ・括弧/バージョン表記/サブタイトル除去 (必須) |
| test_scoring.py | 長さ・曲名含有・アーティスト・公式・減点・自動確定条件・誤爆事例 (必須) |
| test_scanner_diff.py | 新規/更新/消失/再出現/DeletePending/Protected/音声長 (必須: 差分検知) |
| test_matcher.py | 検索クエリ・保存・手動選択の保護・操作 |
| test_quota.py | 太平洋時間の日付境界・予算 |
| test_youtube_client.py | 再試行と指数バックオフ・quotaExceeded・チャンネル無し・可用性判定 |
| test_playlists.py | 中身の取得と照合・生存確認・ログのマスク |
| test_sync.py | 差分計画・重複動画・手動削除の尊重・再生不可・一時停止と再開・dry-run・失敗/中止・中断 |
| test_api.py | localhost/CSRF ガード・一覧/詳細/操作・設定検証・ジョブ・同期 API・自動再開 |
| test_safety.py | client.realm を書き換えないこと・コピーの再試行・127.0.0.1 bind・秘密情報の置き場所とマスク |

`OSU2YT_INTEGRATION=1` で実際の osu!lazer データをヘルパーで読み取るテストも実行。

## 11. 決定事項 (2026-11-06 承認)

1. パック譜面: 1セット=1曲で常に「要確認」(パックバッジ + variants 一覧表示)
2. 同一動画が複数セットに採用されたら追加は1回
3. TV Size 等の短縮版: **フルサイズ動画を上位にする** (4章「短縮版の扱い」)
4. データ置き場: リポジトリ内 `data/` (gitignore)
