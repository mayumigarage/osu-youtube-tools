# osu2yt — osu!lazer → YouTube プレイリスト

osu!lazer に入っている曲を読み取り、YouTube で動画を探して、自分の YouTube プレイリストに追加するローカルツールです。
譜面が増えたら**差分だけ**を追加します。すべて自分の PC 上で動き、WebUI のサーバーは `127.0.0.1` にだけ bind します。

```bash
.venv\Scripts\python -m osu2yt          # WebUI が http://127.0.0.1:8765/ で開きます
```

設計の詳細: [docs/DESIGN.md](docs/DESIGN.md)

## 仕組み

1. **スキャン** — `client.realm` (lazer のデータベース) を `data/work/` へ**コピー**し、コピーを .NET ヘルパーが読み取り専用で開く。元ファイルには一切書き込みません。譜面セット 1 つ = 1 曲。
2. **マッチング** — yt-dlp で YouTube を検索し、長さ・曲名・アーティスト・公式チャンネルかどうかで候補を採点。自信のあるものは自動で確定、怪しいものは「要確認」へ。検索に API クォータは使いません。
3. **同期** — YouTube Data API v3 で、確定した曲の動画をプレイリストへ追加。ここだけクォータを使います。

## セットアップ

必要なもの (Windows 11 で確認済み):

| ツール | 用途 | 入手 |
|---|---|---|
| Python 3.11 以上 | 本体 | https://www.python.org/ |
| .NET SDK 9 | client.realm 読み取りヘルパー (初回スキャン時に自動ビルド) | `winget install Microsoft.DotNet.SDK.9` |
| ffprobe (FFmpeg) | 音声ファイルの長さ取得 (無くても動くが精度が下がる) | `winget install Gyan.FFmpeg` |

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

yt-dlp は Python パッケージとして入ります。YouTube 側の変更で検索できなくなったら更新してください:

```bash
.venv\Scripts\python -m pip install -U yt-dlp
```

## Google Cloud の設定 (初回のみ)

プレイリストを作る/曲を追加するには、自分用の OAuth クライアントが必要です。

1. https://console.cloud.google.com/ でプロジェクトを作成 (名前は自由。例: `osu2yt`)
2. 「API とサービス」→「ライブラリ」で **YouTube Data API v3** を「有効にする」
3. 「Google Auth Platform」(旧「OAuth 同意画面」) を開いて「開始」
   - アプリ名: 自由、ユーザーサポートメール: 自分
   - 対象 (Audience): **外部**
   - 連絡先メール: 自分 → 同意して作成
4. 「対象」→「テストユーザー」に、**プレイリストを作る Google アカウント**を追加
5. 「クライアント」→「クライアントを作成」→ 種類 **デスクトップ アプリ** → 作成後「JSON をダウンロード」
6. ダウンロードした JSON を **`data/client_secret.json`** という名前で保存
7. WebUI の「設定 → YouTube アカウント → ログイン」(または `osu2yt auth login`)。ブラウザで許可します。
   「Google はこのアプリを確認していません」と出たら「続行」(自分で作ったアプリなので問題ありません)。

注意:

- プレイリストは YouTube **チャンネル**に属します。チャンネルの無いアカウントでは `channelNotFound` になるので、先に https://www.youtube.com/create_channel で作成してください。
- 「アクセスをブロック: … Google の審査プロセスを完了していません」→ そのアカウントが手順 4 のテストユーザーに入っていません。
- 同意画面が「テスト」のままだと**ログインは 7 日で切れます** (Google の仕様)。切れたらログインし直してください。
- `data/client_secret.json` と `data/token.json` は秘密情報です。`data/` ごと `.gitignore` 済みでコミットされませんが、他人に渡さないでください。ログにもトークン類は出力しません (マスクされます)。

## 使い方 (WebUI)

```bash
.venv\Scripts\python -m osu2yt                    # = serve。--port 8765 / --no-browser
```

1. **設定** — osu!lazer のデータフォルダ (既定 `%APPDATA%\osu`) を確認し「スキャン」。
2. **設定 → YouTube マッチング** — 「未マッチの曲を検索」。初回は 800 曲で 15〜20 分ほど。上部のバーに進み具合が出ます。
3. **ライブラリ → 要確認** — 自動で決めきれなかった曲を見ていきます。曲を開くと候補がプレビュー付きで並びます。

   | キー | 操作 |
   |---|---|
   | ↑ ↓ / j k | 候補を選ぶ (プレイヤーで再生) |
   | Enter | 選んだ候補を採用して次の曲へ |
   | C | 今の動画で確定して次の曲へ |
   | S | この曲はスキップ (追加しない) |
   | R | 再検索 |
   | ← → | 前後の曲 |

   候補に無いときは YouTube の URL を貼って「URL を指定して採用」。
4. **設定 → プレイリスト** — 新しく作る (既定は非公開) か、既存のものを選びます。
5. **同期** — 追加予定と推定クォータを確認して「同期を開始」。確認ダイアログが出ます。「ドライラン」は中身の取得と生存確認だけ行い、追加はしません。
6. **履歴** — 各回の結果。行をクリックすると動画ごとの結果 (追加 / 重複スキップ / 再生不可 / 失敗) が見られます。

新しい譜面を入れたら: スキャン → 未マッチの曲を検索 → (要確認を見る) → 同期。追加されるのは差分だけです。

### 同期のルール

- 複数の譜面が同じ動画を選んでいても、プレイリストには **1 回だけ**追加します。
- 同期の最初に YouTube から**プレイリストの今の中身を取得**し、既にあるものは追加しません (自分で手動追加した動画も含む)。
- YouTube 上で**自分で外した動画は再追加しません**。
- プレイリストから**削除することはありません**。
- 追加直前に動画の生存確認 (削除・非公開・地域制限) を行い、再生できないものは除外して、その曲を「要確認」に戻します。
- 「要確認」「スキップ」の曲は対象外です。追加順は osu! に入れた順 (古いものから)。

## クォータについて

YouTube Data API は 1 日 **10,000 unit** まで (Google Cloud プロジェクト単位)。日付は**太平洋時間の 0 時** (日本時間 17 時 / 米国夏時間中は 16 時) で切り替わります。

| 操作 | 消費 |
|---|---|
| プレイリストへの曲の追加 | **50** / 曲 |
| プレイリスト作成 | 50 |
| プレイリストの中身取得 | 1 / 50 曲 |
| 動画の生存確認 | 1 / 50 本 |
| 自分のプレイリスト一覧 | 1 |

- 曲の追加は **1 日あたり約 190 曲**まで (安全のため 300 unit を残す設定。設定の「残しておく余裕」で変更可)。
- 上限に達すると、その回は**安全に一時停止**します (Google から quotaExceeded が返った場合も、手元の予算計算で足りない場合も)。
- **自動再開**: WebUI (サーバー) を起動したままにしておけば、クォータのリセット後 5 分以内に続きから自動で同期します。起動していなかった場合は、次に起動して約 30 秒後に再開します。設定の「詳細設定」でオフにできます。
- CLI で使う場合は、リセット後にもう一度 `osu2yt sync` を実行すれば続きから追加します。
- 検索は yt-dlp で行うので、マッチングではクォータを消費しません。

## 使い方 (CLI)

WebUI と同じことが CLI でもできます。以下 `osu2yt` は `.venv\Scripts\python -m osu2yt` の略です。

```bash
# ライブラリ
osu2yt scan                                      # client.realm が前回から変わっていなければスキップ (--force)
osu2yt scan --lazer-path D:\osu-data             # データフォルダが既定と違う場合
osu2yt stats

# マッチング
osu2yt match                                     # 未マッチの曲を検索 (--scope needs_review で要確認を再検索)
osu2yt list --status needs_review --limit 200
osu2yt show o:1957501                            # 候補とスコア内訳
osu2yt choose o:1957501 https://youtu.be/XXXXXXXXXXX   # 候補 or 任意の動画を採用
osu2yt confirm o:1957501                         # 今の候補で確定
osu2yt skip o:1957501                            # この曲は追加しない (--undo で解除)
osu2yt rescore                                   # 保存済み候補を今の採点ルールで再判定 (検索しない)

# YouTube
osu2yt auth login | status | logout
osu2yt playlist create "osu! songs"              # 既定は非公開。--privacy unlisted|public
osu2yt playlist list                             # 既存のプレイリストを使うなら playlist use <ID>
osu2yt playlist pull                             # 中身を取得
osu2yt videos check                              # 採用動画の生存確認
osu2yt quota

# 同期
osu2yt sync --plan                               # 追加予定と見積もりだけ表示 (API を使わない)
osu2yt sync --dry-run                            # 取得と生存確認だけ (追加しない)
osu2yt sync                                      # 確認のうえ追加 (--max 50 で本数を制限、--yes で確認省略)
osu2yt history                                   # 同期の履歴 (history <番号> で詳細)
```

`sync` は一時停止 (クォータ) のとき終了コード 2 を返します。

## マッチングの考え方

- 候補の点数 = 長さの近さ 35% + 曲名 25% + アーティスト 20% + 公式らしさ (Topic / 公式アーティストチャンネル) 20%。
- cover / nightcore / 歌ってみた / live / osu! のプレイ動画などは減点 (osu! 側の曲名にその語が含まれる場合を除く)。
- 自動で確定するのは、0.70 点以上・曲名が完全に含まれる・アーティストか公式チャンネルが一致・2 位と差がある、をすべて満たすときだけ。それ以外は「要確認」(理由つき)。
- 「TV Size」「Cut Ver.」などの短縮版は、**フルサイズの動画を優先**します。
- 曲パック (1 セットに複数の曲) は常に要確認。

## データの置き場所

すべて `data/` (git 管理外) に置きます。環境変数 `OSU2YT_DATA` で変更できます。

| パス | 内容 |
|---|---|
| `data/osu2yt.db` | SQLite (曲・候補・プレイリストの記録・同期履歴・クォータ記録) |
| `data/work/` | client.realm のコピーとヘルパーの出力 |
| `data/client_secret.json` | OAuth クライアント (自分で置く) |
| `data/token.json` | ログイン後のトークン |

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| 「client.realm をコピーできませんでした (アクセス拒否)」 | osu!lazer を終了してからスキャン |
| 「client.realm を開けませんでした」 | lazer の更新で DB 形式が変わった可能性。`helper/OsuRealmExport` の Realm パッケージ更新が必要 |
| スキャンで .NET のビルドが止まる | `osu2yt helper build` を単独で実行して出力を確認 |
| 検索が全部失敗する | `pip install -U yt-dlp` |
| 「ポート 8765 は使用中です」 | 既に起動しています。そのままブラウザで開くか `--port` で変更 |
| ログインが切れた (7 日) | 設定から再ログイン (同意画面が「テスト」の間の仕様) |
| 同期が「クォータで一時停止」 | 正常です。リセット後に自動 (または手動) で再開します |

## 開発

```bash
.venv\Scripts\python -m pytest -q
```

実データ (自分の osu!lazer とビルド済みヘルパー) での読み取りテストも含める場合:

```bash
set OSU2YT_INTEGRATION=1 && .venv\Scripts\python -m pytest -q
```

テストは YouTube にアクセスしません (検索・API はすべてフェイク)。

| 場所 | 内容 |
|---|---|
| `helper/OsuRealmExport/` | client.realm 読み取り (.NET / Realm dynamic API、読み取り専用)。`osu2yt helper build` / `helper schema` |
| `src/osu2yt/library/` | realm のコピー・スキャン・差分検知・正規化 |
| `src/osu2yt/matching/` | 検索 (yt-dlp)・採点・判定 |
| `src/osu2yt/youtube/` | OAuth・API クライアント (再試行・指数バックオフ)・クォータ・プレイリスト |
| `src/osu2yt/sync.py` | 差分同期 (計画・実行・一時停止/再開・履歴) |
| `src/osu2yt/scheduler.py` | クォータリセット後の自動再開 |
| `src/osu2yt/api/`, `app.py` | FastAPI (localhost 限定・CSRF 対策) |
| `src/osu2yt/web/` | WebUI (ビルド不要の HTML/JS) |
