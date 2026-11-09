# E2E 試験環境の使い方（決定的層と実機層）

E2E 試験環境を使う人向けの手引きである。
資産（生成器・固定資産・gold）は [e2e_assets.md](e2e_assets.md)、決定的層の中身（台本・関門の判定）は [e2e_pipeline.md](e2e_pipeline.md) に書いてある。

## 1. 目的

架空の感染症様式（14 項目。うち登録番号を除く 13 項目を判定する）について、様式 → 臨床的意味（典拠） → 所在探索（indexer の索引） → 手順書 → DSL → 実行 → 回答の保存、を1本で通す。
ダミーデータは DWH（2接続）・ファイル共有・FHIR・CSV に散らばっており、それを索引で探し出して取れるかを**段階ごとの関門**で見る。
最後の値だけを見ると、どこで落ちたかが分からないからである。

## 2. 二層と使い分け

工程は1本（`kawasemi/ai_input/e2e/pipeline.py` の `run_pipeline`）で、二層は差し込む物だけが違う。

| | 決定的層 | 実機層 |
|---|---|---|
| コマンド | `make e2e` | `make e2e-live` |
| 試験 | `tests/e2e/test_pipeline_e2e.py` | `tests/e2e/test_pipeline_live.py`（marker `live`） |
| 索引と検索の embedding | `HashingEmbedder`（字面の 2-gram） | bge-m3（`EMBEDDING_MODEL` / `EMBEDDING_BASE_URL`） |
| 設計5段階と llm_extract | 台本 LLM（`examples/e2e/script/`） | `OLLAMA_MODEL`（`OLLAMA_BASE_URL`） |
| Vision（手書き同意書） | 台本の文字起こし | `OLLAMA_VISION_MODEL` |
| 合否 | **関門 a〜e の全部を assert する** | **assert しない。表を記録するだけ** |
| 走る場所 | CI と手元（10 秒前後） | 手元だけ（既定の `pytest` では外す） |
| 出力 | 失敗時に表を出す（一時ディレクトリ） | `var/e2e/live_runs/<時刻>/` |

使い分けは次のとおりである。

- **配線を変えたら決定的層。**
  executor・resolver・compiler・索引の構築・設計 API を触った変更は、`make e2e` で関門が全部 PASS のままかを見る。
  CI でも走る。
- **モデルや索引の embedding を変えたら実機層。**
  台本が隠している部分（LLM が所在のヒント語を出せるか、一推しを選べるか、DSL を書けるか、本文から値を読めるか）を通すのは実機層だけである。
  結果は「通ったか」の記録で、版の比較や調整の物差しには使わない（§5）。

## 3. 動かし方

### 3.1 決定的層

```
make e2e
```

Ollama も実施設のデータも使わない。
落ちたときは項目×関門の表が出る。

### 3.2 実機層

```
make e2e-live                                   # .env の OLLAMA_MODEL を使う
OLLAMA_MODEL=<モデル名> make e2e-live           # モデルを指定する
```

前提は次の3つで、どれかが欠けると理由を付けて skip する。

- `OLLAMA_BASE_URL` に到達でき、`OLLAMA_MODEL` がその一覧にある
- `EMBEDDING_BASE_URL`（空なら `OLLAMA_BASE_URL`）に `EMBEDDING_MODEL`（bge-m3）がある
- （Vision）`OLLAMA_VISION_MODEL` が `OLLAMA_BASE_URL` の一覧にある。**無くても skip しない。**
  同意取得日（`consent_date`）の関門 (d) を `skipped: vision unavailable` として表に残し、他は通す

生成物・索引・アプリDB・記録は `var/e2e/live_runs/<時刻>/` に残る（gitignore）。
出力先は `E2E_LIVE_OUT` で変えられる。

| ファイル | 中身 |
|---|---|
| `run.md` | モデル名と digest、工程ごとの所要時間、項目×関門の表と失敗の理由 |
| `run.json` | 上の全部に加えて、項目ごとの DSL プログラムと患者ごとの evidence（trace） |
| `app.db` | 設計済みの様式・手順書・プラン・回答（`kawasemi e2e serve` で見る） |
| `resource_index/` / `generated/` | その回の索引とダミー外部システム |

**制限時間を上げる（`OLLAMA_TIMEOUT` と `LLM_TOTAL_TIMEOUT`）。**
時間制限は 2 つあり、どちらに当たっても DSL のコンパイルが打ち切られて、一部の項目が「未試行」になる
（関門 (c) の理由に `未試行（…）` と出る）。

- `OLLAMA_TIMEOUT`（既定 120 秒）は **1 コール**の上限。1 コールが数分かかるモデル（例: `qwen3.8:27b`）では、
  コンパイルの塊が `ReadTimeout` になり、連続 3 回の失敗（`MAX_CONSECUTIVE_CHUNK_FAILURES`）で残りが打ち切られる。
  **`LLM_TOTAL_TIMEOUT` を上げても直らない**ので、まずこちらを上げる（`.env.example` に `900` の例がある）。
- `LLM_TOTAL_TIMEOUT`（既定 600 秒）は設計ロールのチャンク処理**全体**の締め切り。

どちらの値も `run.md` の「全体の制限時間」「1 コールの上限」に記録される。

```
OLLAMA_TIMEOUT=900 LLM_TOTAL_TIMEOUT=3600 OLLAMA_MODEL=<モデル名> make e2e-live
```

実機層は未試行の項目を自動では押し直さない（押し直しの結果を混ぜず、打ち切りをそのまま記録するため）。

`make e2e-live` は手元の記録を作るものなので、記録を残すときは `run.md` の表を手元の記録へ写す。
記録はこのリポジトリの `docs/` ではなく、リポジトリの外に置く。

### 3.3 ブラウザで見る

```
make e2e-serve                                  # = uv run kawasemi e2e serve（最新の実行）
uv run kawasemi e2e serve --run-dir var/e2e/live_runs/20261023-010000
```

FHIR スタブ（:8084）とアプリ（既定 `127.0.0.1:8030`）を起動する。
アプリDB・索引・接続レジストリ・外部DB をその回の出力へ向けるので、設計済みのプランや手順書、保存された回答をそのまま開ける。
ログインは `e2e_designer`（設計者）/ `e2e_collector`（収集者）/ `e2e_reviewer`（閲覧・検証者）で、パスワードは起動時に表示する（`DEMO_PASSWORD` を渡せばその値）。
画面から AI 案の再生成や autofill をやり直すと、その回の `app.db` が書き換わる。

### 3.4 索引だけを作る

```
uv run kawasemi e2e setup --build-index                    # HashingEmbedder
uv run kawasemi e2e setup --build-index --embedder ollama  # bge-m3
```

`var/e2e/resource_index/` に作る。
`kawasemi index search` で検索を手で確かめるときに使う。

## 4. 関門の表の読み方

| 関門 | 判定 |
|---|---|
| (a) | 索引に期待単位（`gold.unit_id`）がある |
| (b) | 所在探索の上位 8 件に期待単位が入り、一推しがそれである（フォールバック項目は第2候補も上位 8 件） |
| (c) | 手順書が生成され、DSL が検証エラーと `needs_input` 無しでコンパイルでき、確認済みにできる |
| (d) | 10 名全員の実行値が gold と一致する（フォールバック項目は経路も見る） |
| (e) | 回答を送信し、保存された QuestionnaireResponse の値が gold と一致する |

- `PASS` / `FAIL`: その関門の判定。
- `skip`（理由なし）: **前の関門で落ちたので判定していない。** 原因は左にある最初の `FAIL` である。
- `skip`（理由あり）: 判定の材料が無い。いまは実機層の `skipped: vision unavailable` だけ。
- `XFAIL`: 既知の理由で落ちると宣言した関門（決定的層で使う。理由を必ず添える）。

表の下に、落ちた関門ごとの理由が並ぶ。
読む順は次のとおりである。

1. **(a) で落ちた**: 索引の構築の問題（ingest の宣言・連結・項目単位の追記）。LLM は関係しない
2. **(b) で落ちた**: 「上位 8 に無い」は検索（embedding とヒント語）、「一推しが期待単位でない」は所在 LLM の選択。理由に上位 3 件が出る
3. **(c) で落ちた**: 手順書か DSL。理由が `未試行` なら全体の制限時間による打ち切りで、モデルが書けなかったのではない（§3.2）。`空のプログラム` / `LLM の応答を棄却` / `検証エラー` / `needs_input` はそれぞれ別の原因である。`run.json` の `programs` にコンパイル結果がある。実機層では `physical_format: {pad: 10}`（mrn の 10 桁ゼロ詰め）の欠落が (d) の 0 件として出ることにも注意（e2e_assets.md §2）
4. **(d) で落ちた**: 患者ごとの「期待 / 実行（failure_code）」が出る。`run.json` の `evidence` に step ごとの trace がある
5. **(e) で落ちた**: 実行値は合ったのに保存値が違う。フォームの送信と型の変換を見る

実機層の表の補足には、典拠つきの臨床下書きが返った項目と、所在の応答の中での期待単位の位置が出る。

## 5. 実機層の結果は記録であって指標ではない

実機層の結果は「この組み合わせで通るか」の記録に留める。
ダミーデータでの到達率（PASS の数）を評価の数字として使わない。
ダミーは構造だけを実環境に似せた架空のもので、判定する項目は 13（様式は登録番号を含めて 14 項目）、患者は 10 名しかない。
また、所在の一推しも DSL も LLM の出力なので、同じ設定でも回ごとに揺れる。
そのため `test_pipeline_live.py` は関門 (b)〜(e) の合否を assert せず、次の2点だけを確かめる。

- 工程が最後まで走った（例外で止まらない）
- 全項目が終端に着いた（全関門 PASS か、最初に落ちた関門が `FAIL` / `XFAIL` か、理由付きの `skip`）

記録を残すときは、モデル名と digest、所要時間、項目ごとの関門と落ちた理由を書き、「何項目通った」を成績として書かない。

## 6. 任意: `draft-knowledge` から通す

固定資産の `knowledge.yaml` / `knowledge.billing.yaml` は「人が確認済みの知識層」である。
実機層では、これを `kawasemi-indexer draft-knowledge`（LLM の下書き）から作り直して通すこともできる。
既定では走らせない。

```
uv run kawasemi e2e setup                       # dwh.db / billing.db を作る
mkdir -p var/e2e/drafted
uv run kawasemi-indexer draft-knowledge --connection dwh --db-group dwh \
    --dsn sqlite+aiosqlite:///var/e2e/dwh.db \
    --doc examples/e2e/definitions/dwh_definition.csv \
    --out var/e2e/drafted/knowledge.yaml
uv run kawasemi-indexer draft-knowledge --connection billing --db-group billing \
    --dsn sqlite+aiosqlite:///var/e2e/billing.db \
    --doc examples/e2e/definitions/billing_definition.csv \
    --out var/e2e/drafted/knowledge.billing.yaml
# 下書きを人が確認・修正する（患者キー・表役割・patient_unscoped_tables・値ドメイン）
E2E_KNOWLEDGE_DWH=var/e2e/drafted/knowledge.yaml \
E2E_KNOWLEDGE_BILLING=var/e2e/drafted/knowledge.billing.yaml \
OLLAMA_MODEL=<モデル名> make e2e-live
```

`db_group` は接続 ID と同じにする（builder が `connection_id = db_group` を流用する。e2e_assets.md §5）。
`run.md` の「知識層」の行に、どちらを使ったかが出る。
下書きをそのまま通すと、`item_counts` の二重宣言（`patient_data` かつ `patient_unscoped_tables`。e2e_assets.md §2）のような人の判断が抜けて (a) や (d) で落ちることがある。
それは下書きの限界の記録であって、関門の誤りではない。

## 7. 項目を足すとき

**gold と台本を同じ変更で足す。**
決定的層は台本に無い入力を `ScriptMiss` で落とすので、片方だけ足すと `make e2e` が通らない。

1. 資産と gold を足す（e2e_assets.md §6。生成器 → 様式 → 記入要領 → `build_gold` → `kawasemi e2e setup` で `gold.jsonl` を作り直す）
2. 台本を足す（e2e_pipeline.md §4。`design.yaml` の `clinical` / `hint` / `location` / `runbook` / `program`、本文読みなら `execution.yaml`）
3. `make e2e` で (a)〜(e) が PASS になることを見る
4. 必要なら `make e2e-live` で実機の記録を取る（実機層は台本を読まない。人の入力に当たる `clinical.annotation` と `location.alternates` だけを使う）

## 8. 限界

- **外部DBの単位の `payload.physical` は indexer の `ingest` が SQLite の reflection から付ける**。
  スキーマは `main` で、定義書の名前と実行先の名前が同じである。両層とも同じ索引の作り方なので、実機層でも同じ前提である。
- **患者キー（`key_type` / `physical_format`）はコンパイラが焼き込む**。
  初回の実機層では LLM が書かず、mrn で引く DWH と CSV が 0 件、ファイルが登録番号のパスになった（birth_date / sex / onset_date）。
  今は両層とも索引の `payload.key_type` と `connections.e2e.yaml` の `key_type` / `key_formats` から決まり、台本も書かない。
  物理形は接続単位の宣言で、列ごとには書けない（e2e_pipeline.md §5）。
- **LLM の断り文（「不明…本文が記載されていない」）を止められない経路が残る。**
  前段の取得が全て失敗した llm_extract は呼ばないが、前段を参照しない LLM step や string / text 項目の断り文は値として保存される
  （[autofill_execution_spec.md](autofill_execution_spec.md)「限界」）。実機層の (d) にはこの値がそのまま「実行値」として出る。
- **関門 (b) はヒント語に依存する。**
  決定的層のヒント語は台本が書いたもので、それが順位を左右しているのは crp_on_admission だけである（e2e_pipeline.md §5）。
  実機層では LLM が出したヒント語で検索するので、(b) の違いはヒントの違いと embedding の違いが重なった結果である。
- **決定的層の臨床的意味は 13 項目中 2 項目しか LLM の下書きを通らない。**
  `HashingEmbedder` の cosine が典拠候補の足切り（0.5）に届かないためで、残り 11 項目は人の入力として保存する。
  実機層（bge-m3）では足切りを越える項目が増えるが、越えなかった項目は同じく人の入力で埋める。
- **FHIR は両層ともスタブ**（プロセス内）。FHIR サーバとの通信は検証しない。
- **人の確認は自動化した「設計者の標準的な操作」である。**
  一推しを受け入れ、台本の第2候補が提示枠にあれば足し、手順書と DSL はそのまま確認する。
  人が画面で直すような修正は入らないので、実機層の (c) 以降は「LLM の出力をそのまま確認したら」の記録である。
