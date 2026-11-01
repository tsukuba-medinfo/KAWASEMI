# AI機能フラグ仕様（ENABLE_AI_DRAFTING / ENABLE_AI_EXTRACTION）

最終更新: 2026-06-13
ステータス: 実装済み

## 1. 背景と課題

旧 `ENABLE_AI` は「AI機能を有効にする」という1軸のフラグで、`false` のとき
`ai_input_router` / `ai_web_router` をマウントしない実装だった（旧 `api/main.py`）。
この方式には構造的な矛盾があった。

- データ自動収集（ステップ1〜4）には **LLMが一切関与しない決定的な経路**がある
  （手動の所在登録・手順書ウィザード・確認済みDSLプランの実行・確認書生成）。
  ルーターごと落とすと、これらまで巻き添えで使えなくなる。
- c542610 で導入した **AI機能無効構成の確認書様式（ai_disabled）** は
  「`ENABLE_AI=false` でも確認書を出せる」前提の分岐だが、ルーターが
  マウントされないため永久に到達不可能だった。
- 「AI」と一括りにすると、**人間レビューを前提とする設計支援AI**と、
  **実行時に患者データへ触れるAI**という監査上まったく性質の異なる2つを
  同じスイッチで制御することになる。

## 2. 意味論：2フラグへの分割（設計成果物基準）

旧 `ENABLE_AI` を廃止し、**LLM関与の性質**で2つのフラグに分割する。
境界基準は「**患者データにLLMが触れるか**」。

### ENABLE_AI_DRAFTING — 候補探索（設計時AI）

そのデータをどのように取得すればよいかを Agentic に検討し、
**人間がレビュー・承認してから使われる設計成果物のドラフト**を生成する機能群。
LLMの出力はそのまま使われず、必ず人間の確認（draft → confirmed）を経る。
患者データには触れない（リソース索引＝地図と設問メタデータのみを見る）。

| 機能 | 実装 | 経路 |
|---|---|---|
| データ所在AI案（ステップ1役割2） | `AIResourceDrafter` | `POST .../source-survey/system/ai-draft` |
| 手順書生成・再生成（ステップ2） | `AIRunbookGenerator` | `POST .../runbook/generate`, `.../runbook/{link_id}/regenerate` |
| 取得プラン作成・再作成（ステップ3） | `AIPlanCompiler` | `POST .../plan/compile`, `.../plan/{link_id}/recompile` |
| キー種別候補の生成 | （indexer のワークフローへ移行） | `kawasemi-indexer draft-knowledge` が知識層と一緒に key_type を下書き |

### ENABLE_AI_EXTRACTION — 自動構造化（実行時AI）

ステップ4の実行中に **LLMが患者データに触れて**値を抽出・判定する機能。
エビデンス（実行トレース）にLLM関与の痕跡が残り、確認書様式を分ける唯一の基準。

| 機能 | 実装 | 経路 |
|---|---|---|
| LLM抽出 | `OpType.LLM_EXTRACT` | DSLインタプリタ（ステップ4実行） |
| LLM判定 | `OpType.LLM_JUDGE` | DSLインタプリタ（ステップ4実行） |

### フラグ対象外（常時利用可能）

- 手動の臨床的コンテキスト・データ所在登録、手順書の手動作成・編集・確認
- LLM op を含まないDSLプランの保存・確認・実行（決定的処理）
- データ収集手続確認書（CSV文書）の生成
- リソース索引の構築 `kawasemi-indexer build`（embedding検索の索引＝地図。indexer
  （このリポジトリの `indexer/`）の責務。生成的出力はなく患者データも投入しないため、フラグの統制対象としない）
- ID解決（resolver）、QuestionnaireResponse 生成、認証・RBAC・CSRF

## 3. 構成マトリクス

| DRAFTING | EXTRACTION | 構成の意味 | 確認書様式 |
|---|---|---|---|
| false | false | 全手動設計＋決定的実行（AI完全非関与） | ai_disabled |
| true | false | AI設計支援＋決定的実行（**推奨の規制対応構成**） | ai_disabled |
| false | true | 手動設計＋実行時LLM構造化 | ai_enabled |
| true | true | フルAI支援 | ai_enabled |

4通りすべて有効な構成。デフォルトはともに `false`（安全側・opt-in）。

## 4. マウントと端点ガード

### 常時マウント

`ai_input_router` / `ai_web_router` は**フラグに関係なく常にマウント**する。
データ自動収集（ステップ1〜4）は製品機能であり、AIはその中のオプションである。
AI設計者ロールのホームは常に `/ai-input/dashboard`。

### DRAFTING ガード（端点単位 403）

`ENABLE_AI_DRAFTING=false` のとき、§2のドラフト生成系エンドポイントは
`403 {"success": false, "message": "候補探索AIが無効です（ENABLE_AI_DRAFTING=false）"}`
を返す。UI側は対応するボタン（AI案・一括生成・再生成・一括作成・再作成）を
`enable_ai_drafting` テンプレートグローバルで非表示にする。

> 注: 旧 CLI `kawasemi index keytype-draft`（`KeyTypeDrafter`）は廃止され、key_type の付与は
> indexer（このリポジトリの `indexer/`）のワークフローへ一本化された（`kawasemi-indexer draft-knowledge` が
> 知識層と一緒に key_type を下書き → 人が knowledge YAML を確認・修正 → `kawasemi-indexer ingest`）。
> 索引生成系は KAWASEMI 本体の外（独立 CLI）に移ったため、本フラグ（DRAFTING）の統制対象では
> なくなった。本フラグでガードする設計支援AIは WebUI 側の AI 下書き
> （resource_drafter / runbook / compiler / location_drafter）に限られる。

### EXTRACTION ガード（二段＋確認書の三層防御）

`ENABLE_AI_EXTRACTION=false` のとき:

1. **登録時（早期拒否・UX）** — `save_plan` は `llm_extract` / `llm_judge` を
   含む program を 400 で拒否する。AIコンパイル（DRAFTING=true 構成）が
   LLM op を含む program を生成した場合、当該項目は draft 保存せず
   件数とメッセージで報告する（人間が手順書を決定的処理に修正して再作成する）。
2. **実行開始時（権威ガード）** — `execute_autofill` は確認済みプラン全件の
   program を走査し、LLM op が1つでもあれば実行前に 403 で拒否する。
   フラグ変更や他構成DBの持ち込みがあっても「この構成でLLMが患者データに
   触れた実行は存在しない」を不変条件にするため、実行時ガードを正とする。
3. **確認書生成時（409）** — §5 参照。

## 5. データ収集手続確認書（CSV文書）

### 様式の分岐キー

様式は1つのテンプレート `kawasemi/web/templates/ai_input/validation/validation.html` で、
`ai_enabled`（= `settings.enable_ai_extraction`）により記載を切り替える。

- `ai_enabled` が真 — 実行時LLM関与を記録する記載
  （モデル・プロンプト・生出力のトレース表示）
- `ai_enabled` が偽 — **実行時LLM非関与（処理の決定性）**を宣言する記載

ai_enabled が偽のときの宣言範囲は「実行時」に限定する。設計過程のAI利用は
決定性の宣言と矛盾しない（コンパイル済みDSLプランは内容が確定しており、
その実行は決定的であるため）。

### 409 ガードの縮小

ai_disabled 様式の生成拒否（409）条件は **実行トレース中の LLM op 痕跡のみ**とする。
旧実装の「`llm_model` 非null（LLMコンパイル済み）プランの409拒否」は廃止し、
開示（下記）へ転換する。人間レビュー済みの設計成果物は文書上の汚染ではない。

### 設計過程におけるAI利用の開示欄（両様式共通）

両様式に「設計過程におけるAI利用と人間レビューの記録」セクションを設け、
実行対象項目ごとに4層の設計系譜を開示する:

| 層 | データ源 | 開示内容 |
|---|---|---|
| ステップ1 臨床的意味 | `ClinicalAnnotation.llm_model` / `citations[].llm_model` / `status` | AI下書き由来のモデル名（手動なら「手動」）、確認状況、典拠（規約文書の該当箇所）の件数とアドレス |
| ステップ1 データ所在 | `SystemLocation.candidates[].llm_model` | AI案由来候補の有無とモデル名（手動なら「手動」） |
| ステップ2 手順書 | `InputRunbook.llm_model` / `status` | 生成モデル名（手動なら「手動」）と確認状況 |
| ステップ3 取得プラン | `InputPlan.llm_model` / `status` | コンパイルモデル名（手動なら「手動」）と確認状況 |

`status=confirmed` は人間による確認・承認を意味する（ステップ4は確認済みプランのみ
実行できるため、実行された項目は全層で人間確認を経ている）。

確認操作の帰属（ALCOA+）: `status=confirmed` への昇格時、確認操作者と日時を
`confirmed_by_id` / `confirmed_at` として各設計成果物（`ClinicalAnnotation` /
`InputRunbook` / `InputPlan`）に記録する。
`QuestionnaireResponse.verified_by_id` と同形式。

- **確認 → draft へ戻す**（再生成 `regenerate` / 再コンパイル `recompile`）では
  `confirmed_by` / `confirmed_at` をクリアする（未確認へ戻るため帰属を残さない）。
- **AI の応答にその項目が含まれなかったとき（未試行）は何も変えない**。
  以前は空の手順書 / 空のプログラムを draft 保存していたので、**応答漏れ1回で確認済みの
  内容が消えていた**（未試行を「生成結果」として記録する形）。いまは 502 を返して既存の
  行に触らないので、**status も `confirmed_by` も変わらない**。押し直せば埋まる。
- **手順書（`InputRunbook`）と取得プラン（`InputPlan`）は、内容を変えた保存で draft へ戻る**。
  `confirm=false` の保存で確認済みの内容（手順書の steps／プランの program）が変わったら、
  status を draft にし `confirmed_by` / `confirmed_at` をクリアする（再生成・再コンパイルと
  同じ扱い）。人が承認したのは変更前の内容であり、元の確認者の名前で未承認の内容を
  実行させない。内容が同一（正準形の JSON が一致）の保存だけは確認済みと元の確認者を保つ。
  再確認は明示操作（`confirm=true`）で行い、内容編集だけで確認者を書き換えない
  （誤帰属の防止）。
- 保存は楽観的排他で行う。画面は読み込んだ（最後に保存した）時点の版 `version` を
  `base_version` として送り、サーバ側の行がその後に変わっていれば 409 を返して上書き
  しない（別の利用者の確認・編集を黙って消さない）。
  - 版は `updated_at` ではなく、内容（正準形）・status・確認者・確認日時から作るハッシュ
    である（`routes.plan_version` / `routes.runbook_version`）。`updated_at` は DB の
    `now()` で、SQLite では秒単位なので同じ秒の2回の保存を区別できない。
  - **確認（`confirm=true`）と「下書きに戻す」（`/plan/{link_id}/demote`）では
    `base_version` が必須**で、無ければ 428 を返す。明示の `null` は「行がまだ無い」の
    意味で受け付ける。何を見て判断したかを版で示させるためである。
  - **下書き保存（`confirm=false`）では省略できる**。省略すると版を検査しない（API・
    移行スクリプトとの互換）。画面は常に送る。
  - 行がまだ無い項目を2つの要求が同時に作った場合、後着は一意制約に当たる。500 では
    なく 409 で返す。
- **取得プランの確認には、手順書が確認済みであることを要する**。手順書が draft へ
  戻っても取得プランは連動させない（実行されるのはプランの program で、それはプランの
  確認で承認されている）。その間プラン画面は当該項目を「手順書が未確認」として
  読み取り専用で表示し、「下書きに戻す」操作だけを許す。
- 臨床的コンテキスト（`ClinicalAnnotation`）とデータ所在（`SystemLocation`）は現状、
  確認済みを維持する編集で元の確認者・確認日時を保持する（内容変更で draft へ戻す
  規則は未適用）。

## 6. 来歴（プロビナンス）の記録

- `InputRunbook.llm_model` / `InputPlan.llm_model` — 既存列。AI生成・AIコンパイル時に
  モデル名を記録、手動保存時は既存値を保持（null なら手動由来）。変更なし。
- `SystemLocation.candidates[]` の各要素に **`llm_model` キーを追加**（JSON内・
  DBマイグレーション不要）。AI案で差し込まれた候補はフォーム→保存まで
  `llm_model` を持ち回り、手入力候補は null。
  - `SystemLocationCandidate` / `SystemLocationDraftItem` スキーマに
    `llm_model: str | None` を追加
  - `_clean_candidate` は `llm_model` を保持する
  - ステップ1のフォーム（design_system.html）はAI案差し込み時に hidden で保持し、
    人間が location 文字列を**編集した場合も AI 由来表示は維持**する
    （編集は承認行為であり、由来の事実は変わらないため）

## 7. 旧 ENABLE_AI の移行（クリーンブレイク）

- `Settings.enable_ai` を削除。`enable_ai_drafting` / `enable_ai_extraction`
  （ともにデフォルト `false`）を追加。
- 環境変数または `.env` に `ENABLE_AI` が設定されている場合、**起動時に
  設定検証エラー**とし、新フラグへの移行方法をメッセージで案内する。
  エイリアス・自動展開は行わない（設定値と実効値の乖離を作らない）。
- CLI: `kawasemi serve --enable-ai` を廃止し、`--enable-ai-drafting` /
  `--enable-ai-extraction` を追加。

## 8. UI仕様

- テンプレートグローバル: `enable_ai` を廃止し `enable_ai_drafting` /
  `enable_ai_extraction` を登録（`template_globals.py`）。
- ナビ（base.html）: AI設計者のダッシュボードリンクはフラグ非依存で常時表示。
- ステップ1（design_system.html）: AI案ボタン・モデル選択は `enable_ai_drafting` で表示制御。
- ステップ2（design_runbook.html）・ステップ3（plan.html）: 一括生成／再生成／一括作成／
  再作成ボタンとモデル選択は `enable_ai_drafting` で表示制御。手動編集・確認は常時可能。
- ステップ4（autofill.html）: 実行ボタンは常時表示。LLMモデル上書き入力は
  `enable_ai_extraction` で表示制御。
- 起動ログ: `候補探索AI(DRAFTING): 有効/無効` と `自動構造化AI(EXTRACTION): 有効/無効` を出力。

## 9. テスト要件

- 旧 `test_validation_doc.py` を新分岐キー（`enable_ai_extraction`）に追従。
  「llm_model コンパイル済みプランの409」テストは「409にならず開示欄に出る」に反転。
- 新規 `tests/ai_input/test_ai_flags.py`:
  - 両フラグ false でも `/ai-input/dashboard` ほか手動経路が 200
  - DRAFTING=false でドラフト生成系 4 端点が 403
  - EXTRACTION=false で LLM op 入り `save_plan` が 400
  - EXTRACTION=false で LLM op 入り確認済みプランの `execute_autofill` が 403
  - 旧 `ENABLE_AI` 設定時に `Settings` が ValueError
