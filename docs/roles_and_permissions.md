# 利用者ロールと権限（正本）

最終更新日: 2026-10-22

本書は KAWASEMI の利用者ロールと、ロール×機能のアクセス権限の **正本（source of truth）** である。
下の表は **実装のガード（`require_roles`）を写したもの** で、「こうあるべき」ではなく
**「いまこう動く」** を書く。

## 4役割

7役割（admin / collection_lead / plan_designer / data_manager / data_entry / verifier / data_user）は
細かすぎた。設計の3役割は同じ人が同じ様式を段階ごとに育てる作業で、1様式を通すのに役割を3回
切り替えていた。キャンバス・403 の切替案内・ナビの 🔒・入り口は、この分割の副作用を吸収する
装置になっていた。4つに畳む。

| 機械名 | 表示名 | 持つもの | 持たないもの |
|---|---|---|---|
| `admin` | 管理者 | アカウント・役割の付与、接続と索引の状態、ログ、様式の取り込み（ファイルとして） | 設計・収集・検証の画面。**患者データを見ない。他の役割への切替もしない** |
| `designer` | 設計者 | 臨床的意味・所在・手順書・取得プラン・設計キャンバス・様式の内容（Questionnaire の項目）と ValueSet の編集・取得メトリクス | 回答の入力・送信 |
| `collector` | 収集者 | 次の患者の受け取り（先着の自動割り当て）・自動収集のバッチ起動と本番実行・手動入力・回答送信・チェック | 設計の確定、対象患者リストの登録 |
| `reviewer` | 閲覧・検証者 | 回答の閲覧・検証（verify）・差戻し（reject）・エクスポート・取得メトリクス・データセットの閲覧（チェックはしない） | 入力・設計 |

- 旧役割との対応: `collection_lead` / `plan_designer` / `data_manager` → `designer`、
  `data_entry` → `collector`、`verifier` / `data_user` → `reviewer`。旧名は `RoleType` に残さない
  （互換の別名を残すと、また7つに戻る）。既存 DB は alembic `d5e6f7a8b9c0_collapse_roles_to_four`
  で写す（旧3役割を同時に持っていた利用者は `designer` 1行に畳む。`user_roles` に重複は作らない）
- **管理者は他の役割のスーパーセットではない。** `User.switchable_role_names()` は保有する役割だけを
  返す。同じ人が設計もするなら設計者を明示的に付与するか、別のアカウントにする（役割は複数持てる）
- **来歴は変えない。** `confirmed_by_id` 等は利用者 ID なので、統合しても「誰が確定したか」は残る。
  `DesignRole`（`ai_input/models.py`。臨床・所在・手順書・プランの来歴の種別）は設計の**段階**の名前で
  あって役割ではないので、そのまま残す。`input_trial_runs.active_role` の旧値も残す
- `PRIORITY`（既定アクティブ＝先頭・切替UI表示順）: `[admin, designer, collector, reviewer]`。
  管理者が先頭なのは「運用の画面から始める」ためで、上位権限の意味ではない

## 認可方式（ハード・スコープ）

- ユーザーは複数ロールを保有できるが、**いつも「1つのアクティブ役割」として操作する**。認可は
  保有ロールではなく **アクティブ役割**（`User.active_role`）で判定する（`core/deps.py: require_roles`）。
- ロール切替は `POST /switch-role`。JWT の `act` クレームにアクティブ役割を持つ。`act` は
  `can_act_as`（＝保有する役割、かつこの入り口で使える役割）で再検証され、通らなければ既定の役割に落ちる。
- **セッションとトークン**（`core/sessions.py`・`core/deps.py`）:
  - アクセストークンには世代番号 `tv`（`User.token_version`）を載せ、リクエストごとに照合する。
    **管理者のパスワード再設定**と**ログアウト**でこの番号を +1 し、リフレッシュトークンも全件失効する。
    それまでに発行されたトークンは期限前でも使えなくなる（盗まれたトークンが再設定・ログアウト後に
    生き残らない）。ログアウトは他の端末のセッションも終わらせる。`tv` の無いトークン（世代導入前）は
    0 として扱うので、導入直後に全員が締め出されることはない。
  - `POST /switch-role` は**有効期限を延ばさない**（元のトークンの残り時間を引き継ぐ）。
  - ログアウトは `POST /logout`（CSRF 検証あり）。`GET /logout` はログイン画面へ戻すだけで何も変えない。
- **ログイン失敗**（`core/login_throttle.py`）: アカウント単位のロックはしない。失敗を
  (ユーザー名, クライアント IP) の組で数え、`LOCKOUT_MAX_ATTEMPTS` 回に達した組を
  `LOCKOUT_DURATION_MINUTES` 分だけ受け付けない（正しいパスワードでも失敗）。失敗の応答は、
  ユーザー不在・非アクティブ・ブロック中・パスワード違いのすべてで同じ文面・同じ処理時間にする
  （ユーザー名の列挙と、他人のアカウントの締め出しを防ぐ）。ブロックに入ると監査ログ
  （`auth_events` の `locked`）に残るが、応答には出ない。状態はプロセス内メモリで、複数ワーカーでは
  ワーカーごとに数える。
- トップページ `/` は**アクティブ役割ごとに別のワークスペースへ振り分ける**（`web/routes.py: index`）。
  `collector`→`/input-targets`、`designer`→`/ai-input/dashboard`、`reviewer`→
  `/questionnaires?mode=response`、`admin`→メニュー画面（アカウント管理・様式の取り込み）。
- **ナビは項目を消さない。** アクティブ役割で使える項目はそのまま押せ、切替えれば使える項目
  （＝保有役割で届く項目）は 🔒 と担当役割を添えて出す。どの保有役割でも届かない項目だけを
  落とす（`web/template_globals.py: nav_items`）。単一役割の利用者には自分の項目しか出ない。
- **役割が足りないときの 403 は行き止まりにしない。** `require_roles` は
  `RoleRequiredError` を投げ、必要な役割・いまの役割・切替可能かを持たせる。画面
  （`Accept: text/html`）には `role_required.html` で「〈役割〉に切り替えて続ける」を出し、
  切替後は元の画面へ戻る（`next`。自サイト内の絶対パスのみ通す）。保有していない役割なら
  誰に依頼すればよいかを出す（管理者だけの利用者は常にこちらになる）。API には従来どおり
  `detail` の JSON を返し、`required_roles` / `active_role` を添える（`api/main.py: role_required_handler`）。

### 認可の実装がどこにあるか

| ファイル | 定義している別名 |
|---|---|
| `core/deps.py` | `RequireAdmin` `RequireDesigner` `RequireCollector` `RequireReviewer` `RequireMetricsViewer`（designer, reviewer） `RequireAutofillRunner`（designer, collector） `RequireAnyRole` |
| `web/dataset_routes.py` | `RequireDatasetViewer`（admin, collector, reviewer） `RequireAdmin`（登録・削除） `RequireCollector`（受け取り・チェック） |
| `web/admin_routes.py` | `RequireAdmin`（再定義） |

`web/routes.py` と `api/v100/*` は `require_roles("...")` の文字列直書きである。どの別名も最終的には
`core/deps.py: require_roles` を通るので、上の「切替の案内」は全経路で効く。

## 入り口（使う人ごとに画面と権限の範囲を分ける）

**画面と権限は使う人ごとに分ける。コードとデータは分けない。** 同じアプリを `ENTRY_ROLES` を変えて
複数のプロセスで起動し、nginx で URL（または port）を振り分ける。各プロセスは同じ DB を使う。

| 設定 | 意味 |
|---|---|
| `ENTRY_NAME` | 入り口の表示名（ヘッダに出す） |
| `ENTRY_ROLES` | この入り口でアクティブになれる役割。**役割と入り口は1対1**なので役割を1つ書く。空なら入り口の区別なし |
| `ENTRY_URLS` | 役割ごとの入り口の URL（`役割=URL`）。使えない役割の画面へ誘導するときに出す |

入り口は `User.switchable_role_names()` の1か所で効く。したがって、既定の役割・ヘッダの役割切替・
`can_act_as`・ナビ・403 の案内・ログインが、すべてその範囲に閉じる。

- **ログイン**: この入り口で使える役割を1つも持たない利用者は入れない（「○○の入り口からログインしてください」）。
  同じホストの別 port は Cookie を共有するので、別の入り口でログインした利用者のトークンもここに届くが、
  使える役割が無ければ**未認証と同じ扱い**（`/login` へ）になり、トークンの更新も拒否する
- **Cookie**: 別の入り口で切り替えた役割（JWT の `act`）は、この入り口で使えなければ既定の役割に落ちる
- **ナビ**: 使えない役割の項目は、**利用者がその役割を保有し**、`ENTRY_URLS` に URL があれば「↗ ○○の入り口」への
  案内として出し、無ければ出さない（持たない役割の入り口へは送らない）
- **403**: 使えない役割の画面を開くと、切替ではなく「この入り口では使えません。○○の入り口へ」を出す
- **起動**: `ENTRY_ROLES` / `ENTRY_URLS` に無い役割名（旧7役割の名前を含む）があれば起動時に止まる

入り口の割り当て: 設計（`designer`）／収集（`collector`）／検証（`reviewer`）／運用（`admin`）。
起動例は `docs/reverse_proxy.md` §2.5。

## ロール × 機能 権限マトリクス（画面）

凡例: ✅=そのアクティブ役割で可 / 認証=ロールを問わずログインしていれば可 / ✗=不可。
「（切替）」の列は無い——管理者は切り替えられず、他の役割は保有していれば切り替えられる。

| 機能 | admin | designer | collector | reviewer |
|---|---|---|---|---|
| アカウント管理（`/admin/users*`） | ✅ | ✗ | ✗ | ✗ |
| Questionnaire 一覧（`/questionnaires`） | ✅ | ✅ | ✗ | ✅（`?mode=response` の導線） |
| Questionnaire の取り込み（`/questionnaires/new`。JSON をファイルとして登録）・削除 | ✅ | ✗ | ✗ | ✗ |
| Questionnaire 詳細（`/questionnaires/{id}`） | ✅ | ✅ | ✗ | ✗ |
| Questionnaire の内容の編集（`/questionnaires/{id}/edit`） | ✗ | ✅ | ✗ | ✗ |
| ValueSet 管理（`/valuesets*`） | ✗ | ✅ | ✗ | ✗ |
| データセット（対象患者リスト）の登録・削除（`/datasets/new*`, `/datasets/{id}/delete`。見えるのは患者IDだけ） | ✅ | ✗ | ✗ | ✗ |
| データセット一覧・詳細（`/datasets`, `/datasets/{id}`） | ✅ | ✗ | ✅ | ✅ |
| バッチ autofill の起動（`POST /datasets/{id}/batch-autofill`） | ✗ | ✗ | ✅ | ✗ |
| バッチジョブの進捗確認・取消（`/batch-jobs/{id}/*`） | ✗ | ✗ | ✅ | ✗ |
| check 画面・check の確定/フラグ（`/datasets/{id}/check*`。自分が有効なリースを持つ AIドラフト対象のみ。閲覧・検証者向けのチェックの流れは無い） | ✗ | ✗ | ✅ | ✗ |
| 入力対象一覧・次の患者の受け取り（`/input-targets*`） | ✗ | ✗ | ✅ | ✗ |
| フォーム画面の閲覧（`/forms`, `/forms/{id}`）・患者/Encounter 参照（`/api/patient/*`, `/api/encounter/*`） | ✗ | ✗ | ✅ | ✗ |
| 回答の提出・確定保存（`POST /forms/{id}/submit`） | ✗ | ✗ | ✅ | ✗ |
| 回答の一覧・詳細（`/questionnaires/{id}/responses`, `/responses/{id}`） | ✗ | ✗ | ✗ | ✅ |
| 回答の編集（`/responses/{id}/edit`。未検証/差戻しのみ・検証済は施錠） | ✗ | ✗ | ✅ | ✗ |
| 検証 verify / 差戻し reject | ✗ | ✗ | ✗ | ✅ |
| 臨床的コンテキスト step1-A（`/ai-input/.../clinical`）・典拠単位一覧 | ✗ | ✅ | ✗ | ✗ |
| システム場所 step1-B（`/ai-input/.../system`）・統計プロファイル・所在の確からしさの判定 | ✗ | ✅ | ✗ | ✗ |
| 取得手順書 step2（`/ai-input/.../runbook`） | ✗ | ✅ | ✗ | ✗ |
| 取得プラン step3（`/ai-input/.../plan`） | ✗ | ✅ | ✗ | ✗ |
| ダッシュボード／キャンバス／統合確認ビュー | ✗ | ✅ | ✗ | ✗ |
| autofill 画面の閲覧（`GET .../autofill`） | ✗ | ✅ | ✅ | ✗ |
| 所在の確からしさの判定の一覧（`GET .../system/assessments`）・典拠被覆（`GET .../clinical/citation-coverage`。集計値のみ） | ✗ | ✅ | ✗ | ✗ |
| autofill の実行（`POST .../autofill`, `/stream`） | ✗ | 試行（揮発） | 本番（永続） | ✗ |
| 実行記録の確認書（`.../executions/{id}/validation`） | ✗ | 自分の実行のみ | 自分の実行のみ | ✅（全件） |
| 取得困難度メトリクス（画面・CSV） | ✗ | ✅ | ✗ | ✅ |

- **1様式の設計（臨床的意味→所在→手順書→プラン）は設計者だけで、役割を切り替えずに通る。**
  キャンバスの4段階（`_CANVAS_STAGES`）はすべて `designer` が担当である（`tests/test_four_roles.py` で固定）。
- **autofill の試行と本番はアクティブ役割で決まる**（`_is_trial_run` = `active_role == designer`）。
  試行は揮発（PHI を含まない `InputTrialRun` のメタだけ残す）、`collector` の実行は永続。
  試行であっても患者スコープは無条件では通らない（合成・ホワイトリスト患者、または有効なリースのある患者に限る）。
- **認証だけで開く画面は無い**（ホーム `/` を除く）。以前「認証」だったフォーム・autofill 画面・設計の集計は、
  管理者が開いて 403 の行き止まりに落ちないよう、その操作ができる役割に閉じた。患者データの取得はさらに担当スコープ
  （InputTarget / CaseAssignment）で絞る（`core/authz.py: user_can_access_patient`）。
  **管理者の全許可は無い**（以前は admin は全患者を通していた。4役割へ畳んだときに外した）。
- **実行記録の確認書は PHI を含む**ため、ガード（designer / collector / reviewer）に加えてハンドラ内で絞る。
  `reviewer` は全件、それ以外のロールは `executed_by_id` が自分の実行記録だけを開ける。
- 取得困難度メトリクスは件数・率のみで PHI を含まないため設計者と閲覧・検証者に開く。**admin は含まれない。**

## ロール × 機能 権限マトリクス（API v100）

| エンドポイント | 許可されるアクティブ役割 |
|---|---|
| `POST /api/v100/questionnaire-responses/` | `collector` |
| `GET /api/v100/questionnaire-responses/`, `GET /{response_id}` | `reviewer` |
| `POST /{response_id}/export`, `POST /bulk-export` | `reviewer`（既定は検証済のみ。`include_unverified=true` で全件） |
| `POST /api/v100/questionnaires/`（取り込み）, `DELETE /{id}` | `admin` |
| `PUT /api/v100/questionnaires/{id}`（内容の更新） | `designer` |
| `GET /api/v100/questionnaires/`, `GET /{id}` | ロール不問（認証のみ） |
| `POST /api/v100/questionnaires/{id}/export` | `reviewer` |

詳細仕様:
- autofill の実行コンテキスト（試行/本番/バッチ）と責任境界 → [autofill 実行仕様](./autofill_execution_spec.md)
- 自動取得不能項目の manual 縮退（提案・承認・確定・手入力・検証） → [manual 縮退フロー](./manual_fallback_flow.md)
- 設計の2段階（臨床/システム）の背景（旧7役割の名前のまま） → [AI支援入力設計の役割分担](./ai_design_two_roles.md)

## 職務分離（SoD）

### 役割でなく割り当てで表す

4役割では設計の段階に役割の境界が無い。「臨床的意味を書く人と所在を決める人を分ける」が
要るときは、**様式への担当の割り当て**（同じ様式に設計者を複数置き、誰がどの段階を担当するかを
施設の運用記録に残す）で表し、役割を増やさない。来歴（`confirmed_by_id` / `assessed_by_id` 等）は
利用者 ID で記録されるので、「誰が確定したか」は役割を統合しても失われない。
manual 縮退の提案・承認・確定操作の分離も同じ（[manual 縮退フロー](./manual_fallback_flow.md) §3）。

### いま技術的に止まるもの

| 操作 | 実装 | 弾く条件 |
|---|---|---|
| 回答の verify / reject | `web/routes.py: verify_response` / `reject_response` | `response.submitted_by_id == current_user.id`（**提出者だけを見る**） |
| check モードの確定（`/datasets/{id}/check/{target_id}/confirm`） | `web/dataset_routes.py: confirm_check` | 収集者だけが入れる。自分が有効なリースを持つ AIドラフト対象でなければ確定できない（欠損を埋める人が確定も行うので、閲覧・検証者のチェックの流れと、その自己検証の分岐は無い） |
| 所在の確からしさの判定 | `ai_input/routes.py` | 作成者と判定者が同一なら `is_self_assessment=True` を記録（止めない） |
| 管理者と患者データ | `require_roles` / `user_can_access_patient` | 管理者は回答・入力対象・データセット・設計の画面に入れない。患者スコープの全許可も無い |

- **止まらない経路が1つ残っている**（以前からの既知。4役割へ畳んだときも変えていない）。A が `collector` として
  autofill 本番を実行し（`executed_by_id=A`）、B が提出した回答（`submitted_by_id=B`）は、
  **A が `reviewer` も保有していれば検証できる**。`verify_response` は提出者しか見ないので、
  autofill の実行者との同一性は止まらない。
- 1人の利用者に `collector` と `reviewer` を同時に付与すると、自己検証禁止は上表の判定だけで効く。
  監査の厳しい用途では、同じ人に収集者と閲覧・検証者を同時に付与しない運用で担保する。
- 管理者はアカウントと役割の付与を持つので、**自分に設計者や収集者を付与できる**。これは
  役割の付与の監査（`user_roles.assigned_by_id`）で追う。管理者アカウントは最小限の運用担当者に限定する。

## 業務上の立場との対応

### データ収集を行いたい医療研究者

「何を・どういう臨床的意味で集めたいか」を定義し、どこから・どう取るかを設計する立場である。
システム上は **設計者（`designer`）** として、臨床的コンテキスト・所在・取得手順書・取得プランを
1人で（または様式ごとの担当の割り当てに従って分担して）設計する。

### データ収集を担当する人

カルテ等を確認しながら該当データを収集し、CRF に記入・確定保存する立場である。システム上は
**収集者（`collector`）** にあたり、管理者が登録した対象患者リスト（データセット）から「次の患者を受け取る」で
先着の自動割り当てを受け（一度に1件、期限付きの排他リース）、自動収集を起動し、記入に責任を持つ。
収集結果の妥当性を独立に確認する **閲覧・検証者（`reviewer`）** が、検証・差戻し・エクスポートを行う
（職務分離のため、自分が入力した回答は検証できない）。check モード（AIドラフトの確認・確定）は収集者の仕事で、
閲覧・検証者は入らない。

### データ収集を実施する施設のシステム管理者

アプリの運用・アカウント管理・接続と索引の状態の把握・様式のファイル取り込みは
**管理者（`admin`）**。患者データには触れない。データがどこにあるか（所在）の定義は設計者の作業で、
施設のSEが設計者として参加する場合は設計者を付与する。
院内DB定義書から索引を構築する前処理は、indexer（このリポジトリの `indexer/`）を施設のSEが実行する
（下記「スコープ外」の indexer 実行者）。

### データ利用者

収集・検証されたデータを使う立場である。システム上は **閲覧・検証者（`reviewer`）** として、
回答の閲覧・エクスポート・取得メトリクスの確認を行う（エクスポートは既定で検証済みのデータのみを対象とする）。

## スコープ外（業務上の登場人物だが、システムの認可対象ではない）

- **CRF（Questionnaire）設計者**: 収集項目そのものの定義。KAWASEMI は定義済み CRF を受け取る前提
  （取り込みは管理者、取り込んだ後の内容の編集は設計者）。
- **研究事務局／スタディ統括**: 複数施設横断の臨床的コンテキスト配布・統制（マルチインスタンス運用。テナント機能なし）。
- **indexer 実行者（SE）**: indexer（`kawasemi-indexer`）を別NWでオフライン実行し、索引成果物をファイル授受する運用ロール。
