# はじめに

ここでは、ひとつの天体を選び、測光してトランジットをフィットするまでの手順を紹介します。[本番アプリ](https://db.muscat-team.jp)にはアカウントが必要です。管理者に登録してもらったメールアドレスで使い捨ての PIN を受け取り、ログインしてください。詳しくは[ログインと閲覧権限](signing-in.md)をご覧ください。

## ホームとナビゲーション

ホームの地球儀には **MuSCAT3 (OGG)** など各観測所へのボタンがあり、LCO 各サイトの天候も確認できます。どの画面からでも **MuSCAT-db** をクリックするとホームに戻ります。

| メニュー | できること |
|---|---|
| Targets / Obs / Projects | 天体の検索と絞り込み、一晩の観測ログの閲覧、観測プログラム別の天体管理 |
| Photometry / Transit Fit | 一晩の測光と光度曲線の確認、トランジットのフィット |
| Ephemeris / Jobs | トランジット時刻と TTV の解析、実行中・終了したジョブの確認 |
| LCO / FOV / Exp Calc | LCO の観測予約、視野と比較星の選択、露出時間の計算 |
| TOI / NExScI / Settings | TESS・NASA カタログの閲覧、API トークンなどの個人設定 |

**Photometry**、**Transit Fit**、**Ephemeris** を開くと、そのブラウザーで最後に使用した観測日または天体が表示されます。

## 天体と観測日を選ぶ

1. **Targets** の **Search by object name in fits header…** に天体名を入力します。装置を指定するには **Filter by instrument** を使います。
2. **Normalized Target** 列の名前をクリックします。**Photometric Observations** に観測日、装置、フィルター、エアマスの範囲、フレーム数が表示されます。
3. 表の **Phot** と **Fit** の **Y** は、本測光またはトランジットフィットの結果があることを示します。テスト測光のみの場合、**Phot** は **N** です。日付をクリックすると[その晩の観測ログ](browse-a-night.md)が開きます。

表の下には ExoFOP、NASA ADS などのパネルがあります。JWST などは登録のある天体で表示されます。**LCO archive** では天体を含む科学観測フレームを検索できます。ページ末尾から、この天体の暦表、LCO の予約、アーカイブ検索に移動できます。

## 測光する

1. **Phot** 列の望遠鏡アイコンをクリックし、**Instrument**、**Obs date**、**Target** を確認します。
2. **Run reduction**（初回実行後は **Configure & Run Photometry**）で **Pipeline options** を開き、**Bands** のバンドを選びます。
3. **Run name** に実行名（例：`first-pass`）を入力します。空欄なら `default` です。
4. **▶ Run Test Reduction (first N frames)** をクリックします。テストでは各バンドの先頭 10 枚を同じ設定で処理します。枚数は **Reduction & Binning** の **# of test-run frames** で変更できます。他の項目は初回は既定値で始められます。
5. テスト結果を確認し、**▶ Run Full Reduction (all frames)** で全フレームを処理します。生データが見つからずボタンが無効な場合は管理者にご相談ください。

実行名を再利用すると **Save as**、**Overwrite**、**Cancel** を選ぶダイアログが開きます。異なる実行名の結果は共存し、**Run:** で切り替えられます。終了後、**Light curves & Covariates** に概要図と各バンドの **Download csv** が表示されます。**Per-band products** にはバンド別のタブがあり、**Download all output (.zip)** で結果一式を取得できます。

### ジョブを確認する

ページ上部に状態、実行ボタンの下にログが表示されます。他の本測光が進行中は **Pending** で待機します。**To Jobs** またはメニューの **Jobs** を開くと、**Photometry Jobs** で状態（Running、Pending、Done、Failed、Cancelled）を確認できます。**📄 Log** でログを読み、**Cancel** で実行を中止します。中止や削除ができるのは、実行者、管理者、または実行者と同じグループのメンバーです。

## トランジットをフィットする

1. 測光画面の **To Transit Fit**、または天体ページの **Fit** 列のグラフアイコンをクリックします。
2. **Discovered Lightcurves** で使う光度曲線にチェックを入れます。**Select All** と **Deselect All** で一括選択・解除ができます。
3. **Fitting Configurations & Parameters** を開き、**NASA Archive** または **TOI Catalog** を選んで **Query Archive** をクリックします。取得した恒星・惑星パラメーターを確認し、赤く示された必須欄を埋めます。
4. **Run name** を入力し、**▶ Test run (optimize)** をクリックします。テストは最適化だけを行います。結果を確認したら **▶ Run Full Fit** で MCMC サンプリングを開始します。光度曲線が見つからないときは画面のリンクから測光に戻れます。

### トランジット中心時刻の事前分布

**Transit-center prior** は、選択した全光度曲線の最初と最後の `BJD_TDB` の中点に最も近い `t0 + N × period` を惑星ごとに選びます。同距離なら遅い方、周期の事前分布が一様分布なら周期範囲の中点を予測に使います。暦表や曲線の変更は再読み込み後も反映されます。中心時刻を手入力した場合は、欄を消すかカタログを再照会するまでその値を使います。

- **± N sigma** は伝播した暦表誤差の指定倍数（既定は片側 3σ）を使います。基準時刻からの周期数を E とすると、1σ は `sqrt(sigma_t0² + (E × sigma_P)²)` です。周期を固定する場合も基準時刻と周期の両方を `平均,誤差` で入力します。画面には 1σ と実際の範囲が表示されます。
- **full width (d)** は中心時刻の一様事前分布の全幅を日単位で指定します（既定は 0.04 日）。上下限は中心 ± 全幅の半分です。TTV がある惑星では線形暦表から伝播した誤差が過小になるため、全幅を指定します。片側 3σ 未満では警告が出ますが実行できます。

中心がデータの期間外にある場合は実行前に確認を求められます。自動予測できない場合は、中心を指定するか、基準時刻、正の周期、読み取り可能な `BJD_TDB` を与えます。API でも `tc_pred_<planet>` を省略すると同じ予測を使い、最初の惑星には接尾辞のないキーも使えます。`meta.yaml` の `transit_center_priors` と開始ログで、中心、幅、上下限、固定状態、幅の指定方法、σ の数と伝播誤差を確認できます。

**Transit Fit Results** にはモデルの図、**Systematics Plots**、**Fitted Parameters Summary**、**Download all output (.zip)** があります。ジョブは **Jobs → Transit Fit Jobs** にも表示されます。測光には prose2、トランジットフィットには timer を使用します。

## 次のステップ

TTV を解析するには **Ephemeris → Load Datasets** でフィット済みの天体を選び、**Configure & Run TTV Fitting** を開きます。TTV の解析エンジンは harmonic です。[LCO の観測予約](lco-scheduling.md)と[露出計算](../exposure.md)も参照してください。開発・インストールは [README](https://github.com/muscat-team/muscatdb#readme)、不具合報告・機能要望は [Issues](https://github.com/muscat-team/muscatdb/issues) をご覧ください。
