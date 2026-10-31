# E2E テスト

`tests/e2e/` は、架空の感染症様式（`examples/e2e/questionnaire_e2e_infection.json`）と
ダミーの外部システム（DWH・医事会計・ファイル共有・CSV・FHIR）を使って、
**散らばったダミーデータを索引で探し出し、取得プランで取り出して回答として保存するまで**を
1本で通すテストです。
ダミーデータと正解（`examples/e2e/gold.jsonl`）はコードから決定的に生成します
（`kawasemi/ai_input/e2e/`。資産の説明は [`docs/e2e_assets.md`](../../docs/e2e_assets.md)）。

工程そのものは `kawasemi/ai_input/e2e/pipeline.py` にあり、決定的層と実機層が共有します。
2つの層の違いは、差し込む embedding と LLM だけです。

---

## ファイル構成

| ファイル | 内容 | 前提 |
|---|---|---|
| `test_pipeline_e2e.py` | 決定的層。関門 (a)〜(e) を項目ごとに判定する（[`docs/e2e_pipeline.md`](../../docs/e2e_pipeline.md)） | なし。Ollama も施設データも使わず、CI で skip されない |
| `test_pipeline_live.py` | 実機層。同じ資産・同じ関門を bge-m3 と実際の LLM で通し、結果を表に記録する（[`docs/e2e_guide.md`](../../docs/e2e_guide.md)） | `OLLAMA_MODEL` と `EMBEDDING_MODEL` に届くこと。marker `live` で既定の `pytest` から外してある |
| `test_ollama_skip_condition.py` | 実 Ollama を使うテストの skip 条件（`helpers.ollama_unavailable_reason`）そのもののテスト | なし（`httpx.get` を差し替える） |
| `helpers.py` | ログインフォームの CSRF 抽出（`extract_form_csrf_token`）と、実 Ollama を使えない理由を返す `ollama_unavailable_reason` | — |

`helpers.py` は `tests/` 直下のテスト（認証・CSRF）と `tests/ai_input/test_file_vision.py` からも使います。

---

## 決定的層（`test_pipeline_e2e.py`）

`pipeline` fixture（module スコープ）が工程を1回だけ走らせ、関門ごとのテストがその結果を見ます。

1. **索引**: 生成した DB から `ingest → 連結 → build_index → 項目単位の追記` を
   indexer（`indexer/`）の実コードで通す。embedding は `HashingEmbedder`（決定的）。関門 (a)
2. **設計**: HTTP（ASGITransport）で臨床的意味の下書き → 所在の下書き → 手順書 → DSL の
   コンパイル → 確認済み。LLM は台本（`kawasemi/ai_input/e2e/scripted_llm.py`）で、
   所在探索の順位は索引検索が決める。関門 (b)(c)
3. **実行**: 収集者が 10 名に autofill → 回答を送信して QuestionnaireResponse を保存する。
   外部 DB は SQLite 2 接続、FHIR は E2E のスタブ（`kawasemi/ai_input/e2e/fhir_stub.py`）を
   プロセス内で使う。関門 (d)(e)

アプリ DB・生成物・索引はすべて pytest の一時ディレクトリ（`tmp_path_factory`）配下に作るので、
作業ツリーの `kawasemi.db` や `var/` は汚しません。
落ちたときは項目×関門の表を出力し、JSON（`gate_report.json`）を一時ディレクトリへ書きます。

台本に無い入力が LLM へ渡った場合（`ScriptMiss`）は、関門の症状ではなく原因として fixture で落とします。

---

## 実機層（`test_pipeline_live.py`）

関門 (b)〜(e) の合否は assert しません。
確かめるのは「工程が最後まで走り、全項目が終端（PASS か、最初に落ちた関門か、理由付き skip）に着いた」ことだけで、
表は `var/e2e/live_runs/<時刻>/run.{md,json}` に記録します。
LLM サーバや embedding モデルに届かなければ、理由付きで skip します。
記録した実行は `make e2e-serve`（`kawasemi e2e serve`）でブラウザから見られます。

---

## 実行方法

前提として `uv sync --all-packages --all-groups --all-extras` で開発用依存を入れておきます（`DEVELOPMENT.md` の「初回セットアップ」）。

```bash
make e2e        # = uv run pytest tests/e2e/test_pipeline_e2e.py -q（決定的層）
make e2e-live   # 実機層（-m live）
make e2e-serve  # 実機層の最新の実行をブラウザで見る（127.0.0.1:8030）
```

`tests/e2e/` 全体を `uv run pytest tests/e2e/` で回すと、実機層は既定の `addopts`（`-m 'not live'`）で外れます。
依存ごとの skip 範囲は [`docs/development_flow.md`](../../docs/development_flow.md) の 4.3 にまとめています。
