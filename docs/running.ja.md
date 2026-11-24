# システムの運用

デプロイ、スキーマ、解析エンジン、ホスト上のパスは稼働中のアプリのガイド（`/guide`）と運用ノートを参照してください。ホストの一覧と nginx の構成は [notes/DEPLOYMENT.md](https://github.com/muscat-team/muscatdb/blob/test/notes/DEPLOYMENT.md) にあります。

## 画像プレビュー

測光画面は表示サイズの PNG を生成し、`$MUSCAT_TMPDIR/image-previews`（既定は `~/temp/image-previews`）に合計 256 MiB まで保存します。元ファイルのメタデータと指定幅をキーとし、キャッシュを共有するプロセス間で一度に 1 枚を生成します。応答は ETag 付きの `private, no-cache` で、ブラウザーは訪問ごとに再検証します。ファイルの更新や権限の変更は次のリクエストに反映され、元の解析結果は保持されます。

## 測光 HTTP API

稼働中のアプリの `/docs` と `/openapi.json` に入力項目と JSON 応答があります。ブラウザーと同じ認証・観測課題の閲覧権限を使います。既存の SSH トンネル経由ではブラウザー用の Basic 認証を使用できます。専用 API トークンは不要です。変更を加えるリクエストにはアプリ URL と一致する `Origin` も必要です。

コマンドを確認するには `POST /api/photometry/command` に次のような JSON を送ります。

```json
{
  "inst": "muscat4",
  "date": "250512",
  "target": "TOI-6715",
  "test_run": true,
  "options": {"bands": ["gp", "rp"], "overwrite": false}
}
```

同じ内容を `/api/photometry/run` に送るとジョブが開始します。`test_run` は明示してください。省略時は `/run` が true、`/command` が false です。

- **実行名の重複**。HTTP 409 と `"conflict": "run_exists"` が返り、ジョブは開始しません。応答の `owner`、`own_run`、`suggested_run_name`、`confirm_token` を確認します。別名で送信するか、元のリクエストに `"confirm_overwrite": "<confirm_token>"` を追加して上書きします。トークンは所有者と最終更新時刻に対応し、他の再実行後は無効です。閲覧できない実行では別名候補と `"can_overwrite": false` のみが返り、上書きできません。トランジットフィット（`/api/transit-fit/run`）、TTV（`/api/ttv-fit/start`）、Jobs の再実行（`/api/jobs/rerun`、新しい `run_name` も指定可能）も同じ扱いです。変更権限がない場合は 403 です。
- **進行状況**。`/api/photometry/status` に `inst`、`date`、`target` と、返された `run_id` を `run` として渡します。`pending`、`running`、`cancelling`、`finalizing` は進行中、`done`、`error`、`cancelled` は終了、`none` は追跡中のジョブなしを表します。ログは同じ状態を Server-Sent Events でも配信します。`/status-batch` では最大 100 件を照会でき、不正な項目は個別にエラーを返します。
- **結果の取得**。`GET /api/photometry/runs` に `inst`、`date`、`target` を渡すと結果のある実行を新しい順に取得できます。`GET /api/photometry/outputs` に同じ指定と `run=<run_id>` を渡すとファイル名を取得できます。空の `run` は旧形式の観測日ディレクトリを指します。複数サイトの結果は `site`、`telescope`、`mode` で絞れます。
- **ダウンロード**。名前付き実行は `/api/photometry/file/{inst}/{date}/{target}/run/{run_id}/{name}`、旧形式の結果と較正マスターは `/api/photometry/file/{inst}/{date}/{name}` を使います。対応する `/download-all` は ZIP を返します。閲覧権限のない実行の検索結果は空です。

保存済みフォームとの互換性のため、未知の入力項目は無視します。数値項目には数値、数値文字列、空文字列が使えます。空の実行オプションは既定値を保持しますが、後処理の `sigma` が空ならシグマクリッピングを無効化します。カンマ区切りの ID や JD は文字列で指定し、数値 1 個は文字列に変換します。パイプラインの構文検証も適用されます。型や構造が不正なら HTTP 422、その他の設定違反は各項目のエラーを返します。この API 仕様の対象は測光で、トランジット・TTV 用モデルは #264 の後続作業です。
