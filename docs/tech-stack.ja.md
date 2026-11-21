# 技術構成

muscat-db は観測データの検索、測光・トランジット解析、LCO の予約、系外惑星カタログの閲覧をまとめたウェブアプリです。ウェブサービスと観測データベースを中央で管理し、重い解析ジョブをワーカーに配分します。

## 実行環境とウェブ層

| 用途 | 技術 |
|---|---|
| 言語・パッケージ管理 | Python 3.12 以降、uv、uv.lock、.venv |
| ビルド・CLI | setuptools 75 以降、pyproject.toml、Typer。入口は muscat-db → muscat_db.cli:app |
| ウェブアプリ | FastAPI と ASGI サーバー uvicorn[standard] |
| HTML・入力検証 | Jinja2 によるサーバー側レンダリングと Pydantic |
| プロキシ | nginx がアプリに転送。HTTP Basic 認証を使う構成では nginx が htpasswd を検証 |

## データベース

観測索引は SQLite（Python 標準の sqlite3）で、既定のファイルは `muscat.db`、場所は `MUSCAT_DB_PATH` で指定できます。WAL モードで並行読み出しを可能にし、SQL で直接アクセスします。スキーマは初期化時の SQL と必要な ALTER TABLE で更新します。

| テーブル | 役割 |
|---|---|
| frames / summaries / targets | FITS フレームのメタデータ、天体・日付・CCD ごとの集計、天体ごとの集計 |
| target_notes / target_overrides | 観測メモ、天体の同定に対する手動指定 |
| jobs / users / db_meta | ジョブの状態、ユーザー、データベースの管理情報 |
| exposure_coeffs / exposure_jobs | 露出較正係数、較正ジョブの記録 |
| ephemeris_views | 保存した暦表ページの状態 |

## フロントエンド

アプリは `static/styles.css` と通常の JavaScript を配信し、画面から fetch() で FastAPI を呼び出します。フロントエンドのビルドは不要です。`static/js/` に保存・書式・ジョブのポーリング（storage.js、format.js、jobPolling.js）などを置き、ページ固有の処理はテンプレートにも記述します。カタログの図は Plotly、ガイドの図は Mermaid を使います。フォントはシステム標準、等幅は JetBrains Mono / Fira Code などです。アプリの配色は CSS カスタムプロパティと localStorage で管理します。

## 主な Python パッケージ

FastAPI、uvicorn、Jinja2、Typer のほか、astropy は FITS の読み書き・座標・WCS、astroquery は Gaia や VizieR の照会に使用します。cryptography の Fernet はユーザーの API トークンの暗号化、pyarrow は表形式データの変換、rich は CLI の表示、python-dotenv は環境変数の読み込みに使います。テスト・開発には pytest、pytest-mock、httpx、Ruff を使用します。バージョン制約は pyproject.toml、解決済みバージョンは uv.lock を参照してください。

## 配備と処理の流れ

ブラウザーからのリクエストは nginx（:8000）を経て uvicorn（:8001）に届きます。FastAPI は観測索引を照会し、Jinja2 テンプレートと CSS/JS を配信します。解析の起動時には別プロセスを使い、標準出力・標準エラーをジョブ別ログに保存します。nginx の構成例は `deploy/nginx.conf` にあります。

中央ホストは 24 コア、メモリ 100 GB で、tmux と cron を使って運用します。日々の観測ログの走査と索引作成には `muscat-db scan-yesterday` と `muscat-db build-db` を使用します。配備とホストの詳細は[システムの運用](running.md)を参照してください。

### システム構成図

```text
ブラウザー
    ↓
nginx (:8000) ── proxy_pass ──→ uvicorn (:8001) / FastAPI
                                   ├─ Jinja2 テンプレートと静的ファイル (CSS/JS)
                                   ├─ 観測索引の読み書き → SQLite (muscat.db, WAL)
                                   └─ 解析ジョブの起動 → prose2 / timer / harmonic
```

### 外部パイプラインとワーカー

測光は別リポジトリの prose2（conda の prose 環境で run_photometry.py を実行）、トランジットフィットは timer、TTV は harmonic が受け持ちます。ウェブアプリは uv 環境から独立した解析環境を起動します。重い処理は `deploy/workers.py` と `deploy/workers.toml` に定義したワーカー群で実行し、PostgreSQL の管理データベースで調整します。Celery と Redis への移行案は #424 で取り下げられました。

### 外部サービス

LCO Observation Portal は予約・IPP・アーカイブダウンロード、nova.astrometry.net は MuSCAT/MuSCAT2 の WCS 解決、NASA ADS は天体に関する論文検索、Gaia DR3（ESA/VizieR）は視野の星の検索、NASA Exoplanet Archive は暦表と可視性の照合に利用します。

## ソースコードの配置

| src/muscat_db/ 内のモジュール | 用途 |
|---|---|
| web.py / database.py | HTTP ルートとページ、SQLite スキーマとデータベース構築 |
| photometry.py / transit_fit.py | 測光・トランジットフィットの起動と結果表示 |
| lco.py / fov.py | LCO API、Gaia を使った視野の最適化 |
| cli.py / transit_obs.py / exposure.py | コマンド、トランジットの観測可能性、露出計算 |
| jobs.py / job_store.py | バックグラウンドジョブの状態管理と永続化 |
| coord.py / config.py | 座標の検証・集計、環境変数の定義 |
| instruments.py / band_utils.py / cache.py | 装置情報、フィルター・バンド定義、キャッシュ |
