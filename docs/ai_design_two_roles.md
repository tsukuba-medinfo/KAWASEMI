# AI支援入力設計の役割分担（臨床／システム）

> **現行仕様の正本**: 本書はこの役割分担（臨床的コンテキストとシステム場所を分ける）の背景・
> データモデルを説明する設計メモである。ロール体系と権限の正本は [ロールと権限](./roles_and_permissions.md)
> を参照。現在は4役割で、設計の全段階（step1-A 臨床的コンテキスト・step1-B システム場所・
> step2 取得手順書・step3 取得プラン）を設計者（`designer`）が担当する。分割は役割ではなく
> 段階（`DesignRole`）として残っている。
>
> 本書中の「`ai_designer`」「`/design/...`」等の旧名称・旧パスは歴史的記述であり、現行の
> ルートは `/ai-input/.../source-survey/clinical`・`.../system`・`.../runbook`・`.../plan` である。

## 背景・目的

当初、AI支援入力設計（`kawasemi/ai_input/` のモード2）は、1つの `ai_designer`
ロールが1つの `InputDesign` レコードに「ソース種別・説明・データヒント・注意事項」を
まとめて入力していた。

実運用では、入力項目ごとに必要な情報が **2種類の異なる専門性** に分かれる:

1. **臨床的コンテキストアノテーション**（施設非依存）
   - 例: CGP検査提出時の末梢血/骨髄中の芽球（Blast）割合(%)を、どのイベントを起点に、
     どの時間窓・検体優先順・交絡条件で抽出すべきか、という臨床ロジック。
   - 治験実施計画書（プロトコル）がそのまま自由記述で書かれているため、**自由記述のみ** とし、
     複数施設で標準化・共有できるよう **Questionnaire の FHIR Extension へ書き戻し/読込** できる。

2. **システム場所情報**（施設固有）
   - 例: 「`198.51.100.3:5432` の `CGP_lab_result` テーブルにある」という、データが
     物理的にどこにあるかの情報。施設ごとに異なるため **Questionnaire には埋め込まない**。
   - この入力は、施設のDB定義書コーパスを根拠にAIが下書きできる（「AIによる案の作成」）。
     コーパスの構造とKAWASEMIの読み取り仕様は
     [DB定義書コーパス仕様](./db_definition_corpus.md) を参照。

本変更では、モード2をこの2役割に分割する。

## スコープ

### 今回実装する
- データモデルを2テーブルに分離（`clinical_annotations` / `system_locations`）し、
  従来の `InputDesign`（`ai_input_designs`）を廃止。
- 役割別編集画面（臨床的コンテキスト / システム場所）＋ 読取専用の統合確認ビュー。
- 臨床的コンテキストの FHIR Extension 双方向同期（書き戻し / 読込）。**正はDBテーブル**、同期は手動ボタン。
- レガシーAI設計系の廃止と統合（下記）。
- Alembic マイグレーションによる既存 `InputDesign` データの移行。
- モード3（プランニング）/ モード4（自動入力）は新2テーブルを読むよう **簡易対応** のみ。

### 今回やらない（別PR）
- 外部DB（`external_db`）への実接続・SQL実行。今は接続先・表・カラムを **記述するだけ**。
- モード3の本格再定義（→ メモ参照: 左の臨床的コンテキスト＋右のシステム情報から「抽出手順書」を
  生成する役割へシフトする構想）。

## データモデル

`InputDesign` を廃止し、`questionnaire_id + link_id` でユニークな2テーブルへ分離する。

### `clinical_annotations`（臨床的コンテキスト・施設非依存）
| カラム | 型 | 説明 |
|---|---|---|
| id | int PK | |
| questionnaire_id | FK(questionnaires) CASCADE | |
| link_id | str(255) index | Questionnaire item の linkId |
| item_text | text nullable | 表示用テキスト |
| annotation | text nullable | 臨床ロジックの自由記述（プロトコル由来） |

### `system_locations`（システム場所・施設固有）
| カラム | 型 | 説明 |
|---|---|---|
| id | int PK | |
| questionnaire_id | FK(questionnaires) CASCADE | |
| link_id | str(255) index | |
| item_text | text nullable | |
| source_category | str(32) default `manual` | `fhir`/`local_db`/`external_db`/`llm`/`manual` |
| location | text nullable | 接続先・表・カラムの自由記述 |

### `SourceCategory` への追加
- `external_db`（他サーバのPostgres等）を追加。`local_db`（アプリ自身のDB）と区別する。

### `InputPlan`（モード3, 既存を簡易改修）
- `design_id` FK と `design` リレーションを削除。
- `source_category`（str(32) default `manual`）を非正規化カラムとして追加し、
  実行（モード4）はこれを参照する。

## 同期（臨床的コンテキストのみ）
- **正 = `clinical_annotations` テーブル**。
- 「Extensionへ書き戻し」: Questionnaire JSON の各 item に、臨床的コンテキスト専用の Extension URL
  （`https://kawasemi.example.jp/fhir/StructureDefinition/clinical-annotation`）で `annotation` と
  **典拠（citations）**を埋め込む（複合 Extension。定義は
  `fhir/structuredefinitions/kawasemi-clinical-annotation.json`）。
  下位要素は `annotation` と `citation`（`unitId` / `source` / `page` / `charRange` / `mime` /
  `llmModel`）。
- **StructureDefinition は配布物（zip）に同梱しない。canonical URL からの参照だけにする**。
  zip は Questionnaire / QuestionnaireResponse の JSON のみに保つ。
  受け取った施設は Extension の定義を canonical URL から取得する（canonical URL を実在させる作業は未着手）。
  定義は HL7 FHIR Validator（R4B。`validator_cli.jar` 6.10.4 / `-version 4.3`）で検証済みで、
  複合 Extension の2段ネストに明示的な `slicing` 宣言と、子スライスより後ろに置く `Extension.url`
  の固定が要った（`Questionnaire.item` を `context` にすれば入れ子 item にも解決される）。
  検証結果は error 0 / warning 1（dom-6: narrative が無い。Best Practice で無害）。
- **配布するのは「索引された規約文書のチャンク」だけ**（`is_distributable_citation`）。
  `unit_id` が索引のアドレス形式に一致し、`file_role` が `reference`（または配布物から
  取り込んだ来歴 `origin`）である典拠に限る。**フェイルクローズ**（由来が分からないものは
  配布しない）。`unit_id` は UI の自由入力欄なので、形式検査が無いと人が書いた任意の文字列が
  そのまま出る。
- **payload から出すのはキーも値も検査したものだけ**（`kawasemi/utils/file_export.py` の
  `EXPORTABLE_CITATION_PAYLOAD_KEYS` = `page` / `char_range` / `mime`。表
  `_CITATION_PART_BUILDERS` から導出）。**規約文書の本文抜粋（`payload.snippet`）と施設の
  インフラ識別子（`connection_id`）は載せない**。`payload["path"]` も出さない——パスは
  `unit_id` から取る（保存 API から書ける値を優先すると `unit_id` の検査を迂回できる）。
  **`unit_id` の検査は形だけで、索引への実在確認ではない**（空白・制御文字を禁じ 200 文字までに
  制限しているが、空白を含まない任意の文字列は通る）。
  **許可キーであることと、値が安全であることは別**なので値の形も見る（`page` は非負整数で
  bool を除く、`char_range` は非負整数2個で `start <= end`、`mime` と `llmModel` は ASCII の
  トークン。文字クラスを `\w` で書くと**仮名・漢字が通る**）。**出典表記は、検査を通って
  実際に載った部品からだけ組み立てる**（部品を作る経路と表記を作る経路を1本にする。2本ある
  と片方だけ検査して安心する）。`snippet` を外す理由は、患者データを含む文書が誤って
  reference として索引された場合の漏洩面であり、第三者著作物の逐語配布でもあるため。
  出典表記（`source`）は保存値（`payload["label"]`。クライアント由来で検証されていない）では
  なく、`unit_id` のパスと検査を通った `page` / `char_range` から組み立て直す。
- 「Extensionから読込」: 既存 Extension をパースして `clinical_annotations` に取り込む（上書き）。
  典拠は `unit_id` で突き合わせ（同じ `unit_id` が複数あるときは出現順に1件ずつ）、
  **配布に載らない手元の値（snippet 等）も、配布物で表現できない手元の典拠（索引に無い
  紙資料など）も消さない**ので、自分が書き出したものを読み戻しても情報が減らない。
  取り込んだ典拠には `origin` を記録する（`file_role` は配布物に無いので復元しない＝
  由来を捏造しない。代わりに「配布物から入った」という事実を書き、再配布の根拠にする）。旧形式（`valueString` 単一値）も読める
  （その場合は典拠を運んでいないので手元の典拠に触らない）。
  記述**または典拠の集合**が変われば確認済みは未確認へ戻す（典拠は確認の材料そのもの）。
- システム場所は Extension に **含めない**。

## UI / ルート（`/ai-input` 配下, 現行）
| ルート | 説明 | 権限（アクティブ役割） |
|---|---|---|
| `GET /questionnaires/{id}/source-survey` | 統合確認ビュー（読取専用＋編集導線） | designer |
| `GET/POST /questionnaires/{id}/source-survey/clinical` | 臨床的コンテキストの編集・保存 | designer |
| `GET/POST /questionnaires/{id}/source-survey/system` | システム場所の編集・保存 | designer |
| `POST /questionnaires/{id}/source-survey/clinical/ai-draft` | 規約文書を典拠とする臨床的意味のAI下書き。索引が検索不可なら 409 で拒否する（典拠を刻めないため縮退しない） | designer |
| `POST /questionnaires/{id}/source-survey/system/ai-draft` | データ所在のAI案（索引→DB定義書BM25へフォールバック） | designer |
| `POST /questionnaires/{id}/source-survey/clinical/export-extension` | Extensionへ書き戻し | designer |
| `POST /questionnaires/{id}/source-survey/clinical/import-extension` | Extensionから読込 | designer |
| `GET/POST /questionnaires/{id}/runbook...` | 取得手順書（step2） | designer |
| `GET/POST /questionnaires/{id}/plan...` | 取得プラン（step3） | designer |
| `.../autofill` | 自動入力（step4） | [autofill 実行仕様](./autofill_execution_spec.md) 参照（試行/本番/バッチ） |

> 注: 上表は現行ルート。以前の `/design/...` パスと `ai_designer` 単一権限は廃止済み。

作業順序は独立・並行。確認ビューで「コンテキストのみ / システムのみ / 両方未入力」を可視化する。

## レガシー統合（廃止済み）
2ロール化に先立つ単一ロールの AI 設計経路は廃止した。対象は次のとおり。
- 旧プランのモデル（`AIDataFetchPlan` / `DataSourceType`）と、それを組み立てるサービス（`AIPlanner` / `AIAgent`）
- 旧 AI 設計画面のテンプレートと、モード2の LLM 自動生成モジュール（臨床的コンテキストは手動自由記述のため不要）
- `web/routes.py` のレガシールート: `/forms/{id}/generate-plan`, `/forms/{id}/plan`(GET/PUT),
  `/forms/{id}/autofill`, `/questionnaires/{id}/ai-design`(GET/POST), `/questionnaires/{id}/export-annotated`
- `form.html` 内のレガシーAIアシストブロック
- `file_export.py` のレガシー `export_annotated_questionnaire`（臨床的コンテキスト用の新関数に置換）
