# JASPEHR 実装ガイド準拠状況

対象: JASPEHR 実装ガイド **v1.0.0**（FHIR R4 / 4.0.1 ベース）

公式プロファイル canonical URL:

- Questionnaire: `http://www.hosp.ncgm.go.jp/JASPEHR/fhir/StructureDefinition/jaspehr-questionnaire`
- QuestionnaireResponse: `http://www.hosp.ncgm.go.jp/JASPEHR/fhir/StructureDefinition/jaspehr-questionnaireresponse`

> KAWASEMI はデモ実装です。下記「対応済み」で構造的な準拠を進めていますが、
> 「対象外」の項目が残るため **公式プロファイルへの完全準拠は表明していません**。
> 各 Questionnaire テンプレートの `description` 末尾にもその旨を注記しています。

## 対応済み（本リポジトリで修正）

### 1. profile URL の統一
Questionnaire の `meta.profile` は公式 canonical URL（`.../jaspehr-questionnaire`）を使う。
旧来混在していた `http://jpfhir.jp/fhir/jaspehr/StructureDefinition/JP_Questionnaire_JASPEHR`
および `https://jaspehr.jp/StructureDefinition/jaspehr-questionnaire` は使わない。
ValueSet リソースには Questionnaire プロファイルを付与しない。

### 2. `Questionnaire.name`（制約 jsp-5: 15 バイト以下）
`name` は 15 バイト以下にする（例: `UterineCancerRegistration` → `UterineCancer`、
`examples/sample_questionnaire.json` は `PatientBasic`、E2E 試験用の架空様式
`examples/e2e/questionnaire_e2e_infection.json` は `E2EInfection`）。

### 3. `linkId`（制約 jsp-4: 許可文字 `[A-Za-z0-9!#%/:;?@_~]`）
jsp-4 で非許可の `-`（ハイフン）・`.`（ドット）は linkId に使わない。
kebab-case は snake_case にし（`patient-id` → `patient_id` 等）、階層付き linkId
（`drug.regimen.trial-type` 等）は `.` と `-` を `_` へ平坦化する。`enableWhen.question`
（linkId 参照）も同一規則に従う。

### 4. QuestionnaireResponse の構造準拠
QR 生成の共通ビルダー
（`kawasemi/api/v100/questionnaire_responses.py:build_questionnaire_response_resource`）
に集約し、Web フォーム作成・編集経路も同ビルダーを使用:

- `meta.profile` に公式 QR プロファイル URL を付与。
- `questionnaire` 参照を `Questionnaire/{id}` リテラルから
  **canonical `{url}|{version}` 形式**へ変更。
- choice 回答は対応する Questionnaire の `answerOption.valueCoding`
  と照合し **`valueCoding`（system/code/display）** を出力。
  一致しない場合は `valueString` へフォールバック。
- `item.type`（integer/decimal/boolean）に応じた値型を付与。

## 対象外（自然に対応できないため未対応・今後の課題）

| 項目 | 理由 |
|---|---|
| `item.code` の付与 | JASPEHR 共通用語集が公式に未整備（IG も「今後整備予定」）。参照先コードが存在しない。 |
| 独自 CodeSystem の正規化 | `http://jaspehr.jp/CodeSystem/...` 等。公式 CodeSystem が未定義のため暫定の独自定義を維持。 |
| 標準 Extension 対応 | `itemControl` / `minValue` / `maxValue` / `itemHidden` 等（IG で MustSupport）の生成・解釈は未実装。 |
| FHIR R4B → R4 | 検証基盤（`fhir.resources` 7.1.0）が R4B。R4 専用の不変条件（jsp-4/5 等）は構造検証では捕捉されない。 |
| `QuestionnaireResponse.identifier` | 保険医療機関番号^被保険者識別子^施設内ID の複合形式。施設の番号体系を持たないため未付与。 |
| `clinical-annotation` 拡張 | `https://kawasemi.example.jp/...` の非標準拡張（KAWASEMI 独自。JASPEHR IG に対応物が無い）。StructureDefinition は `fhir/structuredefinitions/kawasemi-clinical-annotation.json` に定義したが、**canonical URL は `example.jp` のプレースホルダのまま**で、実在のドメインへの差し替えは施設・配布方針が決まってから。 |

> 注: `clinical-annotation` 拡張は手動エンドポイント（「Extensionへ書き戻し」）でのみ付与するが、
> 付与すると `Questionnaire.content` に**永続化される**ので、**以降の自動 export にも含まれる**
> （`export_questionnaire_to_file` は content をそのまま書き出す）。施設外へ出す前に、
> 何が載っているかは `docs/ai_design_two_roles.md`「同期（臨床的コンテキストのみ）」を参照。

> 注: `meta.profile` に公式 URL を入れているが KAWASEMI の検証はスキーマ構造のみで
> StructureDefinition の解決・プロファイル準拠検証は行わない
> （`kawasemi/utils/fhir_utils.py`）。上記「対象外」項目は公式バリデータでは指摘され得る。
