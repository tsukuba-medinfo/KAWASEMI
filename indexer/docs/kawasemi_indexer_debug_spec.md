# kawasemi-indexer 対話デバッガ仕様（索引診断コンソール）

最終更新: 2026-06-20
ステータス: 要件確定。実装着手。

## 1. 目的と位置づけ

ビルド済みリソース索引（`var/resource_index/`）に対し、**臨床的コンテキスト
（設問テキスト・注釈等の NL）を対話的に入力**し、「その設問の答えがどの接続先の
どの表のどのカラムにあるか（＝データの場所）」を返す**コンソール診断ツール**。

本ツールの正体は **索引の診断（デバッガ）**であって、以下ではない:

- NL→SQL 生成ではない（SQL 本文・JOIN は indexer の責務外。本体仕様 §1 と整合）。
- 本番 WebUI クエリ側の忠実な再現でもない（別ネットワークで動く WebUI 側の挙動を
  indexer に持ち込まない。接点は `CONTRACT.md` のファイルのみ）。

索引を作る SE が「**期待したカラムが拾える/拾えない原因を切り分ける**」ための道具で
ある。既存の一発実行 `kawasemi-indexer search`（`cli.py:cmd_search`）を、連続試行・
直接内省・診断情報付き表示に拡張した対話版に相当する。

## 2. 全体動作

1. 起動時に `ResourceIndexStore.load(index_dir)` で索引をロードし、`is_searchable`
   を判定。**embedding が使えなければ起動失敗**（§4）。
2. 索引の素性（起動ヘッダ §6.4）を表示。
3. プロンプトで1行入力を受け、`:` 始まりは**メタコマンド**、それ以外は**クエリ**
   として `OllamaEmbedder` でベクトル化→`store.search`→診断情報付きで候補を表示。
4. `:show`/`:why`/`:json` 等で直近候補や索引実体へドリルダウン。
5. `:quit` / `Ctrl-D` で終了。

## 3. 診断能力（2 系統）

| 系統 | 実体 | 用途 |
|---|---|---|
| embedding 検索 | `store.search(qv, top_k, min_score)` | 「この言い回しで何位に来るか」 |
| 直接内省 | `store.units`（メモリ上の `ResourceUnit` 群）への線形スキャン | 「索引にそもそも在るか／payload は何か／なぜ上位に来ないか」 |

直接内省は store を改変せず `store.units`（公開プロパティ）を読むだけ。PHI を増やさ
ない（索引＝地図の不変条件を維持）。「期待カラムが拾えない」が **索引に無い**のか
**順位が低いだけ**なのかを切り分けるのがデバッガの目玉（§7 `:why`）。

## 4. embedding 依存（必須）

embedding 検索を**必須**とする。起動時に
`store.is_searchable(model)`（ロード済み・schema 互換・モデル一致）が偽なら、理由を
表示して**非ゼロ終了で起動失敗**する。Ollama 不通・`build_info` のモデル/バージョン
不一致はこの段で弾く。

含意: 本ツールは **build 成功後の正常系**で索引を覗く前提。embedding が壊れている
状況での内省は対象外（割り切り）。

## 5. 入力モデル

- 既定: **1 行＝1 クエリ**（軽快な試行錯誤を優先）。
- 複数行: `:multi` で複数行モードに入り、終端行 `;;` までを 1 クエリとして連結。
  本番の「設問＋注釈まるごと」を貼って試す用途。
- `:` 始まりの行はメタコマンド（§7）。`:multi` 中は `;;` 以外すべて本文。

## 6. 出力（既定表示）

### 6.1 候補リスト（1 候補 = 2 行 + 診断注記）

```
[0] 0.8123  db  DEMO_DB / DEMO.LAB / BLAST_PCT
            芽球割合  NUMBER(5,2)  patient_key=PT_ID(mrn)
--- min_score=0.30 ---
[1] 0.2980  db  DEMO_DB / DEMO.PATIENT / SEX   ⚠ambiguous(Δ0.004)
            性別  CHAR(1)              ← value_dict: "男性"=SEX'1'
            ↳ hit: 「男性」∈ search_text
```

- 1 行目: `cand_id` / score / kind / **場所** = `connection_id / db_qualified / physical_name`
- 2 行目: `logical_name` / 型(`type_raw`) / `pk` / `patient_key`(+`key_type`) / 品質フラグ
- 足切り境界（`min_score`）に区切り線を挿入。

### 6.2 既定の診断注記（質問6 で既定採用）

1. **value_dict 由来**: 候補がコード辞書ラベル経由で surface した場合に
   `← value_dict: "<label>"=<col>'<code>'` を出す（SE が WHERE 値まで分かる）。
   判定: クエリ語が当該ユニットの `payload['value_dict']` のラベルに一致するか。
2. **語ヒット強調**: クエリ語が候補の `search_text`/`logical_name`/`physical_name` の
   どこに現れるかを `↳ hit:` 行で示す。語彙一致ゼロなら `↳ 意味的近接のみ（語彙一致なし）`。
3. **僅差マーカー**: 隣接候補とのスコア差が閾値（既定 0.01）未満なら `⚠ambiguous(Δ…)`。

### 6.3 品質フラグ（既定行 + `:show`）

- `⚑inferred`: `payload` 由来で論理名が LLM 補間（推定）。
- 論理名欠落（物理名のみ）/ `search_text` が物理名のみ＝語彙貧弱。
- 重複: 同一 `logical_name`/`physical_name` の競合（`:why`/`:show` で件数提示）。

### 6.4 起動ヘッダ（索引の素性 = drift 認識）

`build_info` から `embedding_model / schema_version / built_at / unit_count` と、
kind 別・connection_id 別の内訳を表示。「古い索引を見て悩む」事故を防ぐ。

## 7. メタコマンド

`:show`/`:why`/`:json` は**直近クエリの候補リスト**を参照する（検索→ドリルダウン）。

| コマンド | 動作 |
|---|---|
| `<text>` | 臨床的コンテキストとして検索（embedding・全 kind 横断） |
| `:multi` … `;;` | 複数行ブロックを 1 クエリとして検索 |
| `:show N` | 候補 N の全 payload＋`search_text`＋`snippet`＋品質フラグ |
| `:why <自由語\|カラム名\|unit_id>` | 索引内の存在確認＋現クエリでの順位（top-k 外も）|
| `:grep <語>` | `physical_name`/`logical_name`/`unit_id` への部分一致スキャン（内省） |
| `:k N` | top-k 変更（既定 8） |
| `:t X` | min_score 変更（既定 0.0） |
| `:kind db\|file\|fhir\|master\|all` | kind 絞り込み（既定 all。`master`=病名/検査/薬剤マスタ） |
| `:conn <id>\|all` | connection_id 絞り込み（既定 all） |
| `:json N\|all` | 候補 N（または直近結果全体）を JSON 出力（機械可読・コピー用） |
| `:help` | コマンド一覧 |
| `:quit` | 終了 |

- 絞り込み（`:kind`/`:conn`）は検索後/内省時に `unit.kind`・`payload['connection_id']`
  でフィルタ（store 改変不要）。
- `:why` は全ユニットをスコアリングして対象の順位を出すため、内部で top_k=全件相当の
  検索を行う（または対象 unit のベクトル行と現クエリベクトルの cosine を直接算出）。
- `:why <自由語>` は内省スキャン（部分一致）で対象ユニットを特定 → その順位を提示。

## 8. CLI と作法

```
kawasemi-indexer debug [--index-dir DIR] [--model NAME] [--top-k 8] [--min-score 0.0]
```

- フラグは `search` と揃える。起動後は `:k`/`:t` で動的変更。
- **行編集・履歴**: 標準ライブラリ `readline`（import で↑↓履歴・カーソル移動）。
  履歴は `~/.kawasemi_indexer_history` に永続（既定 ON、読み書き失敗時は黙って無効）。
- **割り込み**: `Ctrl-C` は入力をキャンセルしてプロンプトへ戻る（セッション継続）。
  `Ctrl-D`（EOF）は `:quit` 同様に終了。
- **エラー耐性**: 未知コマンド／直近結果が無いのに `:show`／embedder 例外
  （Ollama 一時不通）は、その場でメッセージを出してセッションは落とさない。
- **色付け**: TTY なら軽い ANSI（場所＝強調、`⚑inferred`/`⚠ambiguous`＝色）。
  非 TTY（パイプ）や `NO_COLOR` 環境変数設定時はプレーン。

## 9. 実装構造

| 要素 | 内容 |
|---|---|
| `kawasemi_indexer/debug_repl.py` | 新設。セッション状態＋`dispatch()`＋I/O ループ |
| `kawasemi_indexer/cli.py` | `cmd_debug` と `p_debug` サブパーサを追加 |
| 再利用 | `store.get_store` / `OllamaEmbedder` / `ResourceHit`（store 改変なし） |

- **テスト容易性**: I/O ループ（`input`/`print`/`readline`/色）と**コマンド処理を分離**。
  純粋関数 `dispatch(line, session) -> list[str]`（出力行リスト）を pytest で叩く
  （TTY 不要・Ollama は embedder 注入でモック。既存テストの注入流儀に合わせる）。
- セッション状態: `store` / `embedder` / `top_k` / `min_score` / `kind_filter` /
  `conn_filter` / `last_hits`（直近候補）/ `multiline_buffer`。

### テスト（`tests/test_debug_repl.py`）

一時索引（既存テストの sqlite/索引生成ヘルパ流用）＋ embedder モックで:
クエリ→候補表示、`:show`/`:why`（top-k 外順位）/`:grep`/`:k`/`:t`/`:kind`/`:conn`/
`:json`、value_dict 由来注記、僅差マーカー、未知コマンド・空 `last_hits` のエラー耐性、
起動ヘッダ生成を検証。

## 10. 確定した設計判断

| # | 論点 | 決定 |
|---|---|---|
| 1 | 正体 | 索引の診断ツール（NL→SQL 生成・WebUI 再現ではない） |
| 2 | 入力 | 1 行＝1 クエリ既定＋`:multi`、メタコマンド層あり |
| 3 | 診断能力 | embedding 検索＋`store.units` 直接内省 |
| 4 | embedding 依存 | 必須（不可時は起動失敗。正常系前提） |
| 5 | 既定表示 | 2 行（場所＝connection_id/db_qualified/physical_name）＋足切り線 |
| 6 | 追加表示 | 既定: value_dict 由来・語ヒット強調・僅差マーカー。起動ヘッダ。`:why`/`:show` で順位・品質 |
| 7 | 対象/絞込 | 全 kind 横断＋`:kind`/`:conn` |
| 8 | コマンド | `debug` サブコマンド。`:multi`/`:show`/`:why`/`:grep`/`:k`/`:t`/`:kind`/`:conn`/`:json`/`:help`/`:quit` |
| 9 | 実装 | `debug_repl.py`＋`cli.py`。純粋関数 `dispatch()`＋薄い I/O ループ |
| 10 | 作法 | `readline`（履歴永続）、Ctrl-C 継続/Ctrl-D 終了、エラー耐性、NO_COLOR 対応 |
