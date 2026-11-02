# autofill 実行仕様（正本）

最終更新日: 2026-10-25

AI自動入力（autofill, ステップ4）の実行モデル・権限・責任境界を定義する。
実装は `kawasemi/ai_input/routes.py`（execute_autofill / execute_autofill_stream）、
`kawasemi/web/dataset_routes.py`（batch-autofill / check）、`kawasemi/ai_input/executor.py`。

## 基本原則

- autofill は **確定済みプランを実行する決定論的なシステム処理** である。正しさは「設計（プラン）の
  確定性」に由来し、**実行者に依存しない**。「誰が実行したか」ではなく「何を実行したか」が本質。
- これは手動入力（記入責任が人に帰属する）とは明確に異なる。autofill が設計どおり動けば、
  対話実行でも夜間バッチでも結果は同じ。
- 実行ゲートは **プランが確定（`PlanStatus.CONFIRMED`）であること**。確認済みプランが無ければ実行不可。
  `ENABLE_AI_EXTRACTION=false` のとき、LLM op（llm_extract/llm_judge）を含むプランは実行前に 403。

## 実行コンテキスト（誰が、ではなく、どの文脈か）

| コンテキスト | 起動者（アクティブ役割） | 永続 | 説明 |
|---|---|---|---|
| **試行 (trial)** | designer（設計者） | しない | 設計が正しく取得できるかの検証。結果（prefill/evidence）を永続せず、PHI を含まない `InputTrialRun`（誰が・いつ・どの患者・件数）のみ監査記録 |
| **本番・対話 (production)** | collector（収集者） | する | 収集者が自作業の一環で実行。`InputExecution` に永続 |
| **本番・バッチ (production)** | **collector のみ**（以前は admin だった） | する | 非対話。確定プランを患者集合へ一括適用（`/datasets/{id}/batch-autofill`）。収集の行為なので収集者が起動する。LLM サーバへ継続的に高負荷がかかる点は運用で把握する |

- 対話 autofill エンドポイント（`POST /ai-input/questionnaires/{id}/autofill`, `.../autofill/stream`）は
  `RequireAutofillRunner`（designer＋collector）で保護。試行/本番の区別は **アクティブ役割で導出**する
  （designer→試行・揮発 / collector→本番・永続）。admin は実行不可（患者データを見ない）。

## 取れなかったことを値にしない

autofill は「取れなかった」を「取れた値」に変えないようにしてある。対話・ストリーム・バッチの3経路で、
次の2つが働く。ただし塞げていない経路がある（下の「限界」）。

- **入力が使えない LLM op は呼ばない**: `llm_extract` / `llm_judge` が参照する前段 step が全て
  失敗か空なら、LLM を呼ばずに step を失敗にし、前段の `failure_code` を伝播する
  （詳細は [DSL 仕様](./dsl_specification.md) §8.4）
- **値を回答型へ正規化し、合わない値は prefill しない**: 実行の入口は Questionnaire を executor に
  渡す（`questionnaire_content`）。値を項目の `item.type` へ正規化し（`questionnaire_responses.normalize_answer`）、
  **prefill するのは正規化した値**である。元の値と違えば evidence の項目に `normalized_from` を残す。
  正規化できなければ prefill から外し、evidence の項目は `value: null`、`rejected_value` に却下した値、
  `failure_code: answer_type_mismatch` になる

### 型ごとに受ける値

| item.type | 受ける値 → prefill する値 | 合わない例 |
|---|---|---|
| `date` | `YYYY-MM-DD` / `YYYY/M/D` / `YYYY-M-D`（ゼロ埋め任意）/ `YYYYMMDD`（8桁・暦に在る日付）/ 時刻つき（`YYYY-MM-DD hh:mm[:ss]`、ISO 8601）/ `date`・`datetime` オブジェクト → **`YYYY-MM-DD`**。FHIR の部分日付 `YYYY` / `YYYY-MM` は**そのまま**（有効な FHIR date） | `20260599`、`2026-02-30`、`77777777`、`令和7年8月5日`、断り文 |
| `dateTime` | 上の年月日（時刻なし → `YYYY-MM-DD`）、時刻つき → **`YYYY-MM-DDThh:mm:ss`**（タイムゾーンがあれば `+09:00` 形式で付ける。`Z` は `+00:00`） | `不明` |
| `integer` | `7` / `"7"` / `"7.0"` → `7` | `"7.5"`、`"三"`、真偽値 |
| `decimal` | `"38.5"` → `38.5`、数値はそのまま | `"38,5"`、`"38.5℃"`、`nan`、真偽値 |
| `boolean` | `true` / `1` / `はい` / `yes` / `あり` → `True`、`false` / `0` / `いいえ` / `no` / `なし` → `False`（大文字小文字・全角は同一視） | `不明`、`on` |
| `choice`（answerOption あり） | 選択肢のコード → そのコード（文字列）。**表示名**に一致（strip と NFKC の後で完全一致）→ そのコード | どちらにも一致しない値 |
| `string` / `text` / `open-choice` / 型不明 | 検査も正規化もしない | — |

文字列は判定の前に NFKC で正規化する（全角数字を受ける）。リストは全要素が合うときだけ合う。

フォームの表示との対応: `date` の `<input type="date">` は `YYYY-MM-DD` を表示できる。**部分日付
（`YYYY` / `YYYY-MM`）は有効な FHIR date として prefill するが、この入力欄には表示されない**（値は
送信されないので、人が日付を入れ直す）。`dateTime` の `<input type="datetime-local">` は
タイムゾーンなしの `YYYY-MM-DDThh:mm:ss` を表示できるが、**タイムゾーン付きの値は表示されない**。
`boolean` は真偽値で prefill するので、以前のように文字列 `"false"` がチェック済みに見えることはない。

**登録様式の番兵**: 登録様式によっては、日付項目の該当せず・未施行・生存を `77777777` や
`99999999` のような番兵で表す。これらは暦に無い日付なので `answer_type_mismatch` になり、
prefill されない。**意図どおりである**——この様式の Questionnaire は「該当せず」を未回答で表す
ので、
番兵を値として入れないのは様式の規則と一致する。集計上は「値なし」なので、番兵を返していた
実行は自動充足率の**分子から外れ、分母には残る**（`answer_type_mismatch` は分母から除かない）。

### 限界

- **判定 op を `branch` で使う形**: 入力が使えずに止めた `llm_judge` の出力は None で、`branch` の
  `when` は None を偽として扱う。`else_branch` が値（例: `"2"`＝施行なし）なら、その値が
  **失敗コードなしで**項目に入る（判定 op の step にはコードが付くが、項目の値が非空なので項目の
  `failure_code` は付かない）
- **前段を参照しない LLM step は呼ばれる**: プロンプトが `{{patient_id}}` だけなど、前段 step を
  参照しない `llm_extract` / `llm_judge` は入力の検査を受けない
- **string / text の項目は断り文を止められない**: 本文が取れていて LLM が断った場合、断り文は
  文字列として正しい型なので値になる

## 責任境界（autofill ではなく確定保存に置く）

- autofill には患者（症例）スコープの縛りを **課さない**（電子カルテ閲覧権限は関係者共通という前提）。
- 記入責任と症例スコープは **確定保存**（`POST /forms/{id}/submit`）に置く。submit は `collector` のみで、
  `_user_can_access_patient` により「自分に割り当てられた症例」に限定する（admin の全許可は無い）。
- すなわち「設計の確定で担保される自動処理（autofill）」と「人が記入責任を負う保存（submit）」を分離する。

## 関連

- ロール×機能の権限一覧 → [ロールと権限](./roles_and_permissions.md)
- AI機能フラグ（DRAFTING / EXTRACTION）の意味 → [AI機能フラグ仕様](./ai_flags_spec.md)
