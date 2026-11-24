# LCO の観測予約と追跡

**LCO → Schedule Observations** では、MuSCAT3、MuSCAT4、Sinistro、QHY600 のトランジット観測を計画し、予約からデータの到着まで追跡できます。

## 準備

LCO のプロフィールから API トークンをコピーし、**Settings → LCO API Token → Token** に貼り付けて **Save token** を押します。**Clear user token** で削除できます。保存後、予約画面の **Proposal ID** に、そのアカウントで使用できる観測課題が表示されます。

LCO ページ上部の **API token**、**Download root**、**Live submit** を確認します。**Live submit** が緑色でなければ、送信前に管理者に有効化を依頼してください。

自分の暦表を使う場合は、リンク共有または公開済みの Google スプレッドシートを **Settings → My ephemeris Google Sheet** に登録します。**Sheet URL or ID** を入力し、必要に応じて **Ephemeris tab** と **Transit-centers tab** を変更します。**Load columns** で列を対応付け、**Save sheet** を押します。

## 観測を計画する

1. **Target & Transit Windows** に **Target** と惑星の記号（**Planet**）を入力します。**Fetch ephemeris** でカタログと最新のフィット結果を読み込みます。
2. **Ephemeris source** の **Auto** は NASA、TOI、ローカルカタログの順に採用します。NASA、TOI、線形フィット、共有または個人の Google シート、アップロードした中心時刻、完了済みの各 TTV モデルも選べます。シートの選択には事前登録が必要です。
3. **Duration (hr)** の下で **t0 (BJD)**、**Period (d)**、継続時間の出典と、登録があれば論文を確認します。手入力で変更すると出典は manual になります。
4. **Range start/end (UTC)** と、食の前後に含める **Pre-ingress / Post-egress (min)** を設定します。サイト、薄明、最大エアマス、月からの最小角距離、最大月相で観測可能性を評価します。最大エアマス（既定 2）と最小月距離（既定 30°）は LCO にも送信され、装置を変更しても設定は保持されます。
5. **Generate windows** を押します。**Transit obs** に全体（full）、一部（partial）、2 サイトのリレー（split）、観測不可（none）が表示されます。初期案は **Default plan** で設定し、行ごとに変更できます。選択したトランジットが **Requests** に入ります。
6. **Imaging Request** で課題と装置を選びます。MuSCAT の **Show Exp** と **Show FOV** では **Science** タブの **Site(s)** または **Test** タブの **Site** を使います。ogg は MuSCAT3、coj は MuSCAT4 です。どちらか一方を選んでください。装置変更時のガイドの初期値は MuSCAT が OFF、Sinistro/QHY600 が ON です。ガイド設定の修正後、初回だけ旧設定がリセットされます。
7. Sinistro/QHY600 は **Exp time (s)**、**Filter**、**Readout**、**Exp count** を、MuSCAT は g、r、i、z 各バンドの露出秒数を設定します。**Show ObsLog** で過去の露出を確認し、行のクリックで値を再利用できます。
8. **Requests** の **Observing duration (hr)** は、LCO の準備時間を引いた観測窓内の最大時間に追従します。手入力で短縮でき、**Reset** で自動追従に戻ります。
9. **Strategy** の **Single** は各トランジットを 1 件とします。**Overlapping** は長さを保って **Overlap shift (min)** ずつ開始をずらします。**Chunked** は **Requests per window** で指定した数に等分し、各区間に **Chunk padding before/after (min)** を足します。各リクエストは独立した予約です。
10. **Create drafts** で下書きを作成します。**JSON** から送信内容を編集できます。**Check settings** は予約せずに LCO の検証を実行します。合格すると **checked** と許容される IPP の最大値が表示されます。地平線上にある時間より長い場合は繰り返し露出の長さを短縮して再検証し、失敗時は LCO のエラーを表示します。
11. **Submit** で 1 件、**Submit all** で検証済みの全件を送信し、ダイアログで確定します。ある予約が失敗しても、成功した予約は残ります。

## 予約とデータを追跡する

**Submitted Request Monitoring** の **Request** から LCO に移動できます。**LCO state** は予約の状態、**Pipeline** は処理の状態、**Frames** は取得済みフレームと生フレームに対する処理済みフレームの数です。

観測窓の終了後に最終処理済みフレームを自動取得し、すべての生フレームに対応する処理済みフレームがそろうとデータベースに登録して `complete` になります。一部が届かない場合は猶予期間後に到着分で確定するか、`terminal_no_data` になります。完了時は **Action** の望遠鏡ボタンからそのデータセットの測光を開けます。muscat-db 外で予約した観測の取得には **Download LCO Data** を使います。

- **Clone** は設定をフォームにコピーします。観測窓は改めて生成します。
- **Cancel on LCO** は確認後、グループ内の未完了リクエストを取り消します。リレーではもう一方も取り消すか確認されます。
- **Delete** はローカルの追跡を削除します。LCO の予約と登録済みデータは残ります。取り消しと削除は送信者と管理者が行えます。

## 実施されなかった観測の自動再予約

**Auto-resubmit dropped observations** は既定ではオフです。**Create drafts** の前に有効にすると、中断・失敗した観測、または未実施のまま観測窓が終了した観測について、代替予約を最大 1 回試みます。

残りの観測可能時間が **Minimum original-window fraction**（元の窓に対する割合）と **Minimum remainder (min)** のうち正のしきい値をすべて満たす必要があります。両方が 0 なら無効です。対象はサイトを指定した単一リクエストで、構成は REPEAT_EXPOSE が 1 件である必要があります。自分のトークン、有効な本送信設定、管理者が設定した課題予算が必要です。予算は全ユーザーで共有し、リセットされません。使用量は監視表の上に表示されます。

LCO で待機中・実行中は待ち、取消済み・完了済みの観測は置き換えません。代替分を先に予約し、元の予約がまだ未スケジュールの場合のみ取り消します。すでにスケジュール済み・実行中なら両方を維持し、重複の可能性を個別に通知します。タイムアウトやサーバーエラー時は固有名で予約を照会し、二重送信を防ぎます。代替分はさらに再予約しません。結果または見送り理由は **Detail** に表示されます。望遠鏡時間は保証されません。

サイトの装置配備状況は変わります。**currently unavailable** のサイトも選択できますが、既定の計画は他に候補があればそちらを優先します。
