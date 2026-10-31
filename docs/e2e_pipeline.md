# E2E 決定的層: 関門 a〜e の一気通貫試験

E2E 試験環境のうち、**決定的な層**について書く。
資産（生成器・固定資産・gold）は [e2e_assets.md](e2e_assets.md)、二層の使い分けと実機層（bge-m3 と実際の LLM）の動かし方は [e2e_guide.md](e2e_guide.md) に書いてある。
工程そのもの（生成・索引・設計・実行・保存と関門の判定）は `kawasemi/ai_input/e2e/pipeline.py` の `run_pipeline` が持ち、決定的層と実機層で共有する。

```
make e2e        # = uv run pytest tests/e2e/test_pipeline_e2e.py -q
```

Ollama も実施設のデータも使わない。
CI の test ジョブ（`uv run pytest tests/`）で skip せずに走り、手元では10秒前後で終わる。

## 1. 何を通すか

| 工程 | 中身 | 本物か |
|---|---|---|
| 生成 | `sources.generate_all` が一時ディレクトリへ DWH・医事・レポート・FHIR スタブ・CSV を作る | 本物 |
| 索引 | `ingest_connection`（2接続）→ 連結 → `build_index` → 実行用識別子の付与 → 項目単位の追記 | indexer の実コード。embedder だけ `HashingEmbedder` |
| 臨床的意味 | `POST …/source-survey/clinical/ai-draft` → 人が確認して保存 | 典拠検索は本物、LLM は台本。**LLM が呼ばれるのは 13 項目中 2 項目**（pathogen / antibiotic_given）で、残り 11 項目は人の入力として保存する（§5） |
| 所在 | `POST …/source-survey/system/ai-draft` → 一推しを人が確認して保存 | **検索と順位は本物**、ヒント語と一推しは台本 |
| 手順書 | `POST …/runbook/generate` → 確認 | LLM は台本 |
| DSL | `POST …/plan/compile` → 確認済み | LLM は台本、op 引数の検証・是正（`enforce_derived_op_args`）は本物 |
| 実行 | 収集者が 10 名に `POST …/autofill`（本番） | executor・resolver・外部DB・ファイル・CSV は本物。FHIR はスタブをプロセス内で、llm_extract と Vision は台本 |
| 保存 | `POST /forms/{id}/submit` で QuestionnaireResponse を保存 | 本物 |

### 関門

項目ごとに5つを別々に判定し、`tests/e2e/test_pipeline_e2e.py` が項目×関門の表を出す（JSON も一時ディレクトリへ書く）。

| 関門 | 判定 |
|---|---|
| (a) | 索引に `gold.unit_id` がある（観察項目の単位 `#item_code` は前方一致） |
| (b) | 所在探索の上位 8 件に `gold.unit_id` が入り、台本の一推しがそれである。フォールバック項目は第2候補（`fallback_unit_id`）も上位 8 件に入る。**ヒント語が順位を左右しているのは crp だけ**（§5） |
| (c) | コンパイル結果に検証エラーと `needs_input` が無く、確認済みにできる |
| (d) | 10 名全員の実行値が gold と一致する。フォールバック項目は、DWH が空の患者（E2E-0003/0006/0009）だけ本文読みの step が走ったことを trace で見る |
| (e) | 回答を送信し、保存された QuestionnaireResponse の値が gold と一致する |

**ファイルが無い患者の変種**: 本体とは別の一時ディレクトリでもう1回通し、実行の直前に
E2E-0003 の `admission_summary.txt` を消す（`PipelineConfig.before_execute`）。発症日の本文読み
（`llm_extract`）が**呼ばれず**（台本 LLM の呼び出し記録に E2E-0003 が無い）、項目が値なし・
前段の失敗コード付きで終わることを見る。gold と本体の関門の結果は変えない。

**落ちた関門より後ろは判定しない**（表では `skip`）。
(b) で落ちた項目を (c) 以降でも FAIL と数えると、原因が複数あるように見えるからである。

表の例（全項目が通ったとき）:

```
| 項目 | 経路 | (a) | (b) | (c) | (d) | (e) |
|---|---|---|---|---|---|---|
| birth_date | db | PASS | PASS | PASS | PASS | PASS |
| body_weight | db | PASS | PASS | PASS | PASS | PASS |
...
- 注: 所在の提示順（0始まり・検索の順位）: {'sex': 1, 'crp_on_admission': 5, 'onset_date': 1, ...}
```

## 2. 索引の作り方

`kawasemi/ai_input/e2e/index.py` の `build_e2e_index` が持つ。
`kawasemi e2e setup --build-index [--embedder hashing|ollama]` でも同じものを `var/e2e/resource_index/` に作る。

1. **2接続を別々に ingest して連結する。**
   `build_index` の `db_source` は1つしか受けないので、dwh と billing の `L2/column_index.jsonl` と `value_dictionary.jsonl` を1つの定義書ディレクトリへ連結してから渡す。
2. **db 単位の実行用の識別子（`payload.physical`）は indexer が付ける。**
   `derive._derive_db` は `physical` を持たない外部DBの単位から SQL を組まない（[resource_index_guide.md](resource_index_guide.md) §4.1 の `physical`）。
   indexer の `ingest` が reflection したスキーマ（SQLite の既定スキーマ `main`）で付け、`build` が payload へ写す。試験側では足さない。
   このため、DSL の SQL は `main.patients` のように修飾名で書く。
3. **項目単位は DB から作る。**
   `observation_items`（項目名）・`measurements`（測定名・単位）・`item_counts`（有効行の件数・患者数・数値率）を読んで `ItemSpec` に写し、`build_item_units` / `append_item_units` で追記する。
   手書きの spec は置かない（「DB → 項目単位」の経路を通すため）。
   体重測定（`weight`）は seq 1 と 2 が同居するが、1単位にまとめて測定名を両方載せる。
   seq の絞り込みは DSL の WHERE が担う（gold の `row_filters` の `seq='1'`）。

## 3. 台本 LLM

`kawasemi/ai_input/e2e/scripted_llm.py` の `ScriptedLLM`。
`make_design_llm_client()`（臨床・所在ヒント・所在・手順書・DSL の5モジュール）と `DSLInterpreter._ollama_client` を差し替える。
段階は system プロンプトの同一性で決める（各モジュールの定数そのものと比べる）。

**台本に無い入力は `ScriptMiss` を投げ、`ScriptedLLM.misses` に残す。**
台本の節の欠落（`hint:` を消した等）による `KeyError` も `ScriptMiss` に揃える。
製品の経路は LLM の失敗を握って縮退する（所在ヒントは拡張なしへ、チャンクの失敗は部分結果へ、llm_extract は値なしへ）ので、表には「上位8件に無い」のような別の症状でしか出ない。
そこで試験は `misses` が空であることを fixture の最後で確かめ、空でなければ原因（どの段階・どの項目か）で落とす。
患者は登録番号 → 氏名の対応だけを渡し、期待値を持つ `E2ESubject` は渡さない。

例外になる入力は次のとおりである。
知らない system プロンプト、台本に無い link_id、画像の対応が無い Vision、指示に当たるタスクが1件でない llm_extract、別の患者の本文、本文から値が読めない、のどれも例外になる。
黙って空を返すと、「LLM が答えなかった」と「試験の配線が壊れた」が区別できなくなる。

### 3.1 設計工程（`examples/e2e/script/design.yaml`）

項目ごとに次を書く。

| キー | 使う段階 | 意味 |
|---|---|---|
| `clinical.cite` | 臨床的意味 | 典拠に選ぶチャンクの本文に含まれる文字列。提示された候補に1件だけ当たればその cand_id を返す |
| `clinical.annotation` | 臨床的意味 | 記述。下書きが返らなかった項目は、試験が同じ文を人の入力として保存する |
| `hint.terms` / `hint.expected_types` | 所在ヒント | クエリへ足す語と型の加点。**順位は索引検索が決める** |
| `location.choose` | 所在の一推し | 候補の行（`cand_id=N: [DB] 生年月日 — DB=dwh 表=patients …`）にすべて含まれる文字列。提示されていなければ null（本物の LLM も選べない）。2件以上に当たれば台本の誤り |
| `location.alternates` | （人） | 第2候補以降の unit_id。人が提示枠から足す |
| `runbook` | 手順書 | ステップ列 |
| `program` | DSL | コンパイラの応答。op 引数はその後 `enforce_derived_op_args` が索引の payload で検証・是正する |

**患者キー（`key_type` / `physical_format`）は `program` に書かない。**
実機の LLM も書かなかったので、台本が書くと焼き込みの代わりに試されてしまう。
コンパイラの `bake_patient_keys` が、索引の `payload.key_type`（DB）と接続レジストリの `key_type`（file / csv / fhir）・`key_formats`（物理形）から付ける。
mrn で引く DWH と CSV の step が `physical_format: {pad: 10}` を持つことを `test_mrn_keyed_steps_carry_zero_padding` が確かめる（e2e_assets.md §2）。

### 3.2 実行工程（`examples/e2e/script/execution.yaml`）

llm_extract は、プロンプトの**指示部**（本文の区切り `===` より前）に `match` を含むタスクを1つ選び、**本文から** `pattern` で値を読む。
値を gold から引かないので、ファイルや前段の値がプロンプトへ届かなければ失敗する。
患者は指示部の `被験者 E2E-0000`（DSL の `{{patient_id}}` が展開したもの）で引き、本文に同じ患者の氏名が無ければ例外にする。

### 3.3 Vision（手書き同意書）

`build_vision_transcripts` が、各患者の `consent.pdf` を executor と同じ関数（`DSLInterpreter._render_pdf_to_png`）でラスタライズし、画像の sha256 → 文字起こしの表を作る。
文字起こしは生成器が描いた行（`sources.consent_lines`）そのもので、完全な OCR に当たる。
フォントは環境で違うので、ハッシュは固定資産にせず実行のたびに作る。

## 4. 項目を足すとき

e2e_assets.md §6 で資産と gold を足したあと、**同じ変更で台本も足す**。

1. `design.yaml` に項目を足す（`clinical` / `hint` / `location` / `runbook` / `program`）
2. 本文を LLM で読む項目なら `execution.yaml` にタスクを足す。`match` は他のタスクの指示部に現れない語にする
3. `make e2e` を流し、表で (a)〜(e) が PASS になることを見る。所在の提示順は表の注に出る

(b) で落ちたら、まず `hint.terms` を見直す。
**台本で順位を作らない**——候補に入らない単位を一推しにする手段は台本には無く、入れるべきでもない。
`HashingEmbedder` は字面の 2-gram しか見ないので、実機（bge-m3）で届く単位が決定的層では届かないことはありうる。
その場合は既知の失敗（`ItemGates.xfail`）として理由と一緒に表へ残し、黙って外さない。

## 5. 既知の限界

- **臨床的意味の典拠候補は2項目しか出ない。**
  `clinical_drafter._MIN_SCORE`（0.5）は意味の近さの閾値で、`HashingEmbedder` の cosine はおおむね 0.25〜0.55 に収まる。
  足切りを越えるのは pathogen と antibiotic_given だけで、他の 11 項目は下書きが返らず、人の入力として台本の記述を保存する。
  閾値は意味の近さの基準なので、試験のために下げない。
- **所在ヒントの語は台本が書いている。**
  索引の名前を知っている人が書いた語なので、実機の LLM が同じ語を出す保証は無い。
  決定的層が見ているのは「この語がクエリに足されたとき、索引検索の順位付けで期待単位が上位 8 件に入るか」までである。
- **関門 (b) をヒント語が実際に左右しているのは crp_on_admission だけである。**
  ヒント語を外して測ると（項目名＋臨床的意味のみ）、crp だけが上位 8 件から落ち、他の 12 項目は名前の字面の一致だけで上位 8 件に入る。
  したがって (b) は「名前が合う単位が上位に来るか」の検査としては 12 項目、「ヒントによるクエリ拡張が効くか」の検査としては 1 項目しか持っていない。
- **FHIR はスタブ、llm_extract は正規表現での読み取り。**
  取得の配線（キー解決・パス・プロンプトへの埋め込み）は検証できるが、モデルの読み取り精度は検証しない。それは実機層（[e2e_guide.md](e2e_guide.md)）の範囲である。
- **索引の `payload.physical` は SQLite の reflection から付いたものである。**
  indexer の `ingest` が既定スキーマ `main` を実行先のスキーマとして付ける（知識層に `physical_schemas` は書いていない）。
  定義書の名前と実行先の名前が同じなので、「定義書の名前では引けない」施設の形（`physical_schemas` の明示や、`enrich-physical` での追記）はこの試験では通らない。
  それは `indexer/tests/test_physical.py` が見ている。
- **物理形（ゼロ詰め）は接続単位で宣言している。**
  `connections.e2e.yaml` の `key_formats: {mrn: {pad: 10}}` は「この接続では mrn を10桁で持つ」という宣言で、列ごとではない。
  同じ接続の中で列によって桁数が違う施設は表せない。列ごとの宣言は indexer に語彙が要り、まだ無い。
- 関門 (d) の値の比較は数値なら数値として行う（DWH の `value_text` は文字列 `"44.1"` で返る）。
