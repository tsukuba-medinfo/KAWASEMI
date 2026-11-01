# KAWASEMI データ自動収集（AIモード）— アーキテクチャ・操作ガイド

KAWASEMI のデータ自動収集機能（旧称: AI支援入力）の、アーキテクチャ・データモデル・各ステップの操作・通しの作例・トラブルシューティング。**起動して動かすまで**は [QUICKSTART_AI.md](../QUICKSTART_AI.md)、**DSL の仕様**（op・実行意味論・検証・再現性と監査）は [dsl_specification.md](dsl_specification.md) を参照。

> **ロール体系**: 正本は [roles_and_permissions.md](roles_and_permissions.md) を参照してください。

---

## 1. アーキテクチャ概要

データ自動収集は **4ステップのワークフロー** で構成されます。前3ステップは Questionnaire 単位の事前設定、ステップ4 が患者 ID を渡して実行する処理です。

```
┌──────────────────────────────────────────────────────────────────┐
│  事前設定（Questionnaire 単位・設計者 designer が担当）            │
│                                                                  │
│  ステップ1: データソース調査                                      │
│    ├─ step1-A  臨床的コンテキストアノテーション（施設非依存）       │
│    │           「CGP検査提出時の blast 割合を、○○基準で抽出」     │
│    └─ step1-B  システム場所（施設固有）                            │
│                「198.51.100.3:5432 の CGP_lab_result テーブル」 │
│                              ↓                                   │
│  ステップ2: 取得手順書の作成（LLM が自動生成、人間が確認）         │
│    「① SQLで病理報告書テキストを取得 → ② LLMで組織型を抽出」      │
│                              ↓                                   │
│  ステップ3: 取得プランの作成（LLM が DSL にコンパイル）            │
│    { "steps": [...], "return": "$result" }                       │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                               ↓
┌──────────────────────────────────────────────────────────────────┐
│  実行（患者 ID 単位）                                             │
│                                                                  │
│  ステップ4: AI自動入力                                            │
│    患者 ID を渡すと DSL インタプリタが各 step を実行し、           │
│    補完値と根拠（参照元SQL・LLM応答）をフォームに反映              │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### URL 構造

```
/ai-input/questionnaires/{id}/source-survey   # ステップ1
/ai-input/questionnaires/{id}/runbook         # ステップ2
/ai-input/questionnaires/{id}/plan            # ステップ3
/ai-input/questionnaires/{id}/autofill        # ステップ4
```

---

## 2. データモデル

### ステップ1: 2つのテーブル

| テーブル | 役割 | Questionnaire FHIR Extension |
|---|---|---|
| `clinical_annotations` | 臨床的コンテキスト（施設非依存）。プロトコル由来の自由記述 | ✅ 書き戻し・読込可 |
| `system_locations` | システム場所（施設固有）。接続先・テーブル・カラムの記述 | ❌ 含めない |

**臨床的コンテキストアノテーション** (`clinical_annotations`):

| カラム | 型 | 説明 |
|---|---|---|
| `questionnaire_id` | FK | |
| `link_id` | str | Questionnaire item の linkId |
| `item_text` | text | 表示用テキスト |
| `annotation` | text | 臨床ロジックの自由記述（起点・時間窓・優先順・条件分岐など） |

**システム場所** (`system_locations`):

| カラム | 型 | 説明 |
|---|---|---|
| `questionnaire_id` | FK | |
| `link_id` | str | |
| `source_category` | str | `fhir` / `local_db` / `external_db` / `llm` / `manual` |
| `location` | text | 接続先・テーブル・カラムの自由記述 |

> **正は DB テーブル**。`clinical_annotations` の内容は、Questionnaire JSON の FHIR Extension（`https://kawasemi.example.jp/fhir/StructureDefinition/clinical-annotation`）へ手動ボタンで書き戻し・読込できます。`system_locations` は施設固有のため Extension には含めません。

### ステップ2: 取得手順書

`ai_input_runbooks` テーブル。`steps` カラム（JSON 配列）に順序付きステップを保持します。

```json
{
  "steps": [
    {
      "order": 1,
      "title": "病理報告書テキストの取得",
      "description": "pathology_report テーブルから最新の observation を SQL で取得する",
      "action_type": "fetch",
      "data_hint": "SELECT observation FROM pathology_report WHERE patient_id = :patient_id ORDER BY examination_date DESC LIMIT 1"
    },
    {
      "order": 2,
      "title": "組織型の抽出",
      "description": "取得したテキストを LLM に渡し、組織型のみを抽出する",
      "action_type": "llm"
    }
  ]
}
```

`action_type` は手順書ステップの種別タグで、語彙は `fetch` / `select` / `transform` / `llm` /
`manual` / `condition` / `branch` の7種です（正本は `kawasemi/ai_input/schemas.py` の
`RUNBOOK_ACTION_TYPES`）。**語彙外の値は保存時に `null` へ落とされます**（弾かないのは、
タグ1つの綴り違いで LLM 生成のチャンク全体が失敗するのを避けるため）。

取得元が患者の文脈で変わる場合や、優先順の候補が複数ある場合は、`condition`（何を見てどちらと
判定するか）と `branch`（判定ごとにどこから取るか・どの値を採るか）の2ステップに分けて書きます。
ステップ3は `condition` を判定 op（`llm_judge` / `compare`）へ、`branch` を `guard` の付与と
`branch` へ写します。

**タグは手順の意味であって、ステップ3がどの op で実装するかの予告ではありません。**
「行があれば1・無ければ2」は、SQL の `CASE` 1本で実装されるとしても `condition` + `branch` と
書きます（実装の選択はステップ3の裁量で、手順書を書く時点では見えません）。`select` は
同一の取得結果から1件を選ぶ・絞り込むことで、**所在候補の選択は `select` ではなく
`condition` + `branch`** です。

手順書の `title` / `description` にコードや SQL は書きません（クエリ例は `data_hint` に置きます）。

ステータス: `draft`（生成直後）→ `confirmed`（人が確認済み）。`confirmed` の手順書のみステップ3でコンパイル可能です。

### ステップ3: 取得プラン（DSL プログラム）

`ai_input_plans` テーブル。`program` カラム（JSON）に DSL プログラムを保持します。DSL の正本は [DSL 仕様](dsl_specification.md)。

ステータス: `draft`（生成直後）→ `confirmed`（人が確認済み）。`confirmed` のプランのみステップ4で実行可能です。  
`needs_input: true` の step が残る限り `confirmed` に昇格できません（SQL 等を人間が補完してから確認してください）。

### ステップ4: 実行ログ

`ai_input_executions` テーブル。補完結果（`prefill_data`）と根拠（`evidence`）を保存します。

```json
{
  "prefill_data": { "pathology-histology-type": "類内膜癌" },
  "evidence": {
    "pathology-histology-type": {
      "source_type": "llm",
      "query_used": "SELECT observation FROM pathology_report ...",
      "raw_data": "類内膜癌",
      "reasoning": "報告書テキストから「類内膜癌、Grade 2」を検出"
    }
  }
}
```

---

## 3. ステップ1: データソース調査

```
http://localhost:8000/ai-input/questionnaires/{id}/source-survey
```

Questionnaire の各 item に対して、**臨床的コンテキスト（step1-A）とシステム場所（step1-B）**を入力します。どちらも設計者（`designer`）が役割を切り替えずに入力できます。

### step1-A: 臨床的コンテキストアノテーション

**何を抽出したいか**を記述します。施設に依存しない臨床ロジック（プロトコル準拠）を自由記述で入力します。

入力例（`pathology-myometrial-invasion` 項目）:

```
CGP 検査提出時点の病理報告書から筋層浸潤深度を判定する。
「1/2 を超える」または「Myometrial invasion > 1/2」の記述があれば deep、
「1/2 未満」または「< 1/2」の場合は shallow とする。
記述が曖昧な場合は null を返し、手動確認フラグを立てる。
```

### step1-B: システム場所

**どこにデータがあるか**を記述します。施設固有の接続情報（Questionnaire には含めない）を入力します。

| source_category | 使用場面 | location 記述例 |
|---|---|---|
| `local_db` | アプリ自身の SQLite/PostgreSQL | `pathology_report テーブルの observation カラム` |
| `external_db` | 他サーバの DB | `198.51.100.3:5432 / CGP_lab_result テーブル / blast_ratio カラム` |
| `fhir` | FHIR サーバ | `Patient/{id} → name.text` |
| `llm` | 構造化データなし・テキストから推論 | `病理報告書テキストから LLM で抽出` |
| `manual` | 自動取得不可（手動入力で収集） | 確定時は理由記載必須（下記注意） |
| `not_available` | この施設にデータが存在しない（収集対象外） | （手順書・プランを生成しない） |

> **「manual 確定」の意味に注意**: `manual` は入力欄の**既定値**でもあるため、
> 「未調査のまま manual」と「調査の結果 manual と確定」は値だけでは区別できません。
> 運用上は、候補を登録しない＝未調査、`manual` 候補を明示的に登録＝確定として扱い、
> 確定の際は location 欄に理由区分（取得手段なし / 品質不足。データ不存在は `manual` でなく
> `not_available` を使用）と承認日を**必ず記載**します（location が空欄の manual 候補は
> 手順書・プラン生成の対象外となり、手動入力手順を手順書に載せられません）。
> 確定には承認が必要です。
> 正式な手続きは [docs/manual_fallback_flow.md](manual_fallback_flow.md) を参照してください。

### 統合確認ビュー

ステップ1 の入口画面では、各 item の「臨床的コンテキストのみ入力済み / システム場所のみ入力済み / 両方未入力」を色分けで確認できます。

### FHIR Extension への同期（任意）

臨床的コンテキストは、画面上のボタンで Questionnaire の FHIR Extension へ書き戻し・読込できます。  
DB テーブルが正（マスター）で、Extension は配布用の副本です。

---

## 4. ステップ2: 取得手順書の作成

```
http://localhost:8000/ai-input/questionnaires/{id}/runbook
```

ステップ1 の臨床/システム両入力を突き合わせ、LLM が「どうデータを取得するか」の手順書（順序付きステップ）を自動生成します（担当: `designer`）。

生成後は **人間が内容を確認**し、「確認済み」に変更してからステップ3へ進みます。  
手順書の各ステップは自由文で記述されており、SQL 例やプロンプト案も含まれます。

---

## 5. ステップ3: 取得プランの作成

```
http://localhost:8000/ai-input/questionnaires/{id}/plan
```

確認済みの手順書を LLM が DSL プログラム（JSON）にコンパイルします。

**操作**:
1. 「LLMでプランを一括生成」ボタンをクリック（モデルを指定する場合は先に入力）
2. 生成されたプログラムを確認
3. `needs_input: true` の step には SQL やプロンプトを手動で補完
4. すべて補完完了したら「確認済み」に変更

> モデル名は `ai_input_plans.llm_model` に保存されます。ステップ4で `model` 未指定の場合はこの値が使われます。

---

## 6. ステップ4: AI自動入力

```
http://localhost:8000/ai-input/questionnaires/{id}/autofill
```

患者 ID を入力して「AI自動補完を実行」ボタンをクリックすると、確認済みのプランに基づいて各 item の値が補完されます。

> **試行と本番の区別**: autofill は確定済みプランを実行する**決定論的な処理で、実行者に依存しません**。設計者（`designer`）が実行すると**試行**（結果は永続せず設計検証用・揮発）、収集者（`collector`）が実行すると**本番・対話**（`InputExecution` に永続）になります。非対話の**本番・バッチ**（`/datasets/{id}/batch-autofill`）も **collector** が起動します。詳細は [docs/autofill_execution_spec.md](autofill_execution_spec.md) を参照してください。
>
> **収集後の流れ（検証→エクスポート）**: 収集者が確定保存（submit）した回答は、閲覧・検証者（`reviewer`）が verify/reject します（自己検証は禁止＝SoD、検証済は施錠）。エクスポートも `reviewer` が行い、**既定では検証済のみ**（`include_unverified=true` で全件）。詳細は [roles_and_permissions.md](roles_and_permissions.md) を参照してください。

**実行の流れ**:

```
患者 ID 入力
    ↓
確認済みプランを item ごとに取得
    ↓
DSL インタプリタが各 step を順次実行:
  - sql_extract   → アプリ DB に SELECT を発行
  - fhir_extract  → FHIR サーバに問い合わせ
  - llm_extract   → Ollama にプロンプト送信
  - transform     → コードマップ変換 / 単位換算
  - ...
    ↓
補完値 + 根拠（参照元 SQL / LLM 応答）をフォームに反映
```

**根拠パネル**（画面右側）で各値の取得根拠を確認できます:

```
[組織型（病理）]
ソース: llm
SQL:    SELECT observation FROM pathology_report WHERE patient_id = 'DEMO-0001' ...
生データ: 「類内膜癌」
根拠:  報告書テキストから「類内膜癌、Grade 2」を検出
```

---

## 7. 通しの作例（E2E 試験環境）

アプリ・外部DB・ファイル・FHIR スタブ・索引を自己完結で揃えた通しの作例として、架空の感染症様式
（14 項目）の E2E 試験環境を同梱しています。様式 → 臨床的意味 → 所在探索 → 手順書 → DSL → 実行 →
回答の保存までを、Ollama なしで決定的に通せます。

```bash
uv run kawasemi e2e setup      # ダミーデータ（外部DB・ファイル・FHIR 辞書）を var/e2e/ に生成
make e2e                       # 決定的層（台本の LLM で関門 a〜e を判定）
uv run kawasemi e2e serve      # 実機層の最新の実行をブラウザで見る（127.0.0.1:8030）
```

使い方は [e2e_guide.md](e2e_guide.md)、資産は [e2e_assets.md](e2e_assets.md)、工程と関門は
[e2e_pipeline.md](e2e_pipeline.md) を参照してください。手順書とプランの対の作例は
`examples/e2e/script/design.yaml` にあります。

---

## 8. トラブルシューティング

### LLM 抽出結果が期待値と異なる

モデルによって出力形式が変わります。ステップ3 のプラン画面でプロンプトを直接編集して調整してください。

精度を上げるコツ:
- 「○○のみを回答してください」と明示する
- 回答例を含める（few-shot 形式）
- `transform` step でコードマップ変換を定義する（例: `{"type":"code_map","map":{"Grade 2":"G2"}}`）

### Ollama がタイムアウトする

`.env` でタイムアウトを延長するか、軽量モデルに変更してください:

```env
OLLAMA_TIMEOUT=300
OLLAMA_MODEL=<より軽いモデル>
```

### `needs_input: true` の step でプランが確認済みにできない

ステップ3 のプラン画面で該当 step の SQL・プロンプト等を補完してから「確認済み」に変更してください。  
`needs_input` は LLM が情報不足と判断した step に付与されます。**コンパイラが付けることも
あります** — 索引から引数を決定的に組み立てられないとき、同じ所在候補を2つの step が使って
いるとき、所在候補の優先順を後戻りしているとき、フォールバックの `branch` が取得できた値を
捨てているとき、空振り判定の向き（空だったときに走るのか、値があるときに走るのか）を
判定できないときです。理由は各 step の `reasoning` に記録されます。

**`needs_input: true` でも検査は走ります**。免除されるのは「必須フィールドが
埋まっているか」の検査ただ1つで、**書いてある内容の検査は旗の有無に関わらず走ります**。

| 書き方 | `needs_input: true` での扱い |
|---|---|
| フィールドを書いていない（キー無し・`null`・空文字） | 免除される。保存できる |
| 書いてあるが値が許可外（`cmp: "bogus"` / `pick: "latest"` / `func: "median"`） | **弾かれる** |
| 書いてあるが型が違う（`column: {...}` / `column: false`） | **弾かれる** |
| 列指定に `{{name}}` を書いた（パラメータ展開は効かない） | **弾かれる** |
| 未宣言のパラメータを参照している | **弾かれる**（旗で回避できない） |
| `match` の要素を書いたが `column` / `value` が欠けている | **弾かれる**（要素を書いた時点で「書いた」扱い） |

`0` や `false` は「書かれた値」なので免除されません（空文字とは区別されます）。
補完しないまま保存したいときは、**そのフィールドを書かない**でください——中途半端に
書いた値を残すと、確定を待たずにエラーになります。これは意図した挙動です（書いた内容が
実行時に必ず拒否されることを、確定前に知らせる）。
