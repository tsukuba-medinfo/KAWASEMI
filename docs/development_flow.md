# 開発フロー

この文書は KAWASEMI に変更を入れるときの手順をまとめたものである。
Issue の起票からマージまでを8段階に分け、検証（§4）の出口条件を定める。

## この文書が定めること

**検証は目的と出口条件で定める。道具と実行場所は問わない。**

したがって次のものはこの文書の対象外である。

- 検証をどこで走らせるか（GitHub Actions でもローカルでもよい）
- レビューを誰が、あるいは何を使って行うか
- 各開発者の環境設定（`gh` の認証、SSH の設定）

環境構築とコマンドの詳細は [DEVELOPMENT.md](../DEVELOPMENT.md) にある。
ここでは手順と出口条件だけを扱う。

なお本文書でいう「検証」は lint とテストによる自動検査を指す。
製品機能としての閲覧・検証者ロール（[roles_and_permissions.md](roles_and_permissions.md)）とは別の概念である。

## 全体像

```
1. Issue を立てる
2. ブランチを切る（最新の origin/main 起点）
3. 開発する
4. 検証する
5. レビュー（PR 作成前）
6. PR を出す
7. マージ前の確認
8. マージする
```

§4の出口条件は「lint、format、型、テストが通っている。skip と既知の失敗は 4.3 と 4.4 に従って切り分け済み」である。

`main` への直接 push は `pre-push` フックがブロックする（`make hooks` で有効にした場合）。
変更は PR を経由する。

## 0. 初回のみ行う設定

`DEVELOPMENT.md` の「初回セットアップ」に pre-commit フックの有効化が含まれている。

```bash
uv sync --all-packages --all-groups --all-extras
make hooks
```

`uv sync --all-packages --all-groups --all-extras` は indexer（このリポジトリの `indexer/`。uv の workspace メンバー）とその extras も入れる。

`make hooks` は `uv run pre-commit install --hook-type pre-commit --hook-type pre-push` を実行する。
commit 時に ruff / mypy が走り（`.pre-commit-config.yaml`）、push 時は `main` への直接 push をブロックするガードだけが走る（`scripts/pre_push_guard.sh`）。
クライアント側のガードなので、`git push --no-verify` で回避できる。

`git config core.hooksPath` が設定されていると pre-commit はインストールを拒否するため、`make hooks` は毎回 `core.hooksPath` の解除を先に行う。

## 1. Issue を立てる

作業は Issue を起点にする。
空 Issue は無効にしてあるので、テンプレート（バグ報告・機能要望・タスク）を使う。

### 受け入れ条件を書く

受け入れ条件はレビュー（§5）とマージ前の確認（§7）の判定基準になる。
完了条件が曖昧な Issue は§7で判定できず、後で手戻りになる。

関連する Issue は本文で明示する（`Refs #<番号>` / `Depends on #<番号>`）。

## 2. ブランチを切る

### 最新の `origin/main` を起点にする

```bash
git fetch origin
git checkout -b <type>/<要約> origin/main
```

`gh issue develop <番号> --checkout` を使ってもよい。
Issue との紐付けが自動で行われる。

**stale な local main から切ってはいけない。**
古い main から切ったブランチを rebase すると、別のブランチのコミットが PR へ混入することがある。

### 命名規約

`<type>/<英小文字の要約>`（Issue 番号を入れてもよい）とする。
`type` は§3のコミット規約の type に揃える。

## 3. 開発する

コミットメッセージは Conventional Commits に日本語の要約を組み合わせる。

```
<type>(<scope>): <日本語の要約>
```

| type | 用途 |
|---|---|
| `feat` | 新機能 |
| `fix` | 不具合修正 |
| `refactor` | 挙動を変えない内部改善 |
| `style` | フォーマットやスタイルの修正（ロジック変更なし） |
| `test` | テストの追加や修正 |
| `docs` | ドキュメントのみ |
| `chore` | ビルド設定や依存関係の更新 |

`scope` は任意である（例: `web` `security` `api` `indexer` `e2e`）。

型チェックを `# type: ignore[...]` で抑制するときは、理由をコメントで書く。

## 4. 検証する

どこで走らせてもよい。
ローカルで回してもよいし、GitHub Actions に回してもよい。
求めるのは、下記が通っていることと、その結果を§6で記録することである。

### 4.1 必ず回すもの

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy kawasemi/ scripts/
(cd indexer && uv run mypy kawasemi_indexer/)
make test          # = uv run pytest -n 8（並列）
make e2e           # = uv run pytest tests/e2e/test_pipeline_e2e.py -q（E2E 決定的層。Ollama 不要）
```

**`make test` は並列で回す。** worker 数は `make test WORKERS=4` で変えられる。
**`-n auto` は使わない**——コア数いっぱいにすると、実 Ollama を使うテスト（下の 4.4）が同一サーバへ同時に投げられる密度が上がる。
壁時計の下限は最長の単一テストなので、8 より増やしても縮まない。

**素の `uv run pytest` は直列のままである。** 1件だけ回すとき（4.4 の切り分け）は
worker 起動のオーバーヘッドを払わず、失敗の出力も worker で混ざらないほうがよい。

**並列のときは同じファイルのテストを同じ worker へ寄せる**（`pyproject.toml` の
`addopts` の `--dist loadfile`）。既定の `--dist load` はテスト単位で配るので、module スコープの
fixture が worker ごとに動く。`loadfile` は module スコープの fixture を宣言どおり
モジュールごとに1回だけ動かすためのもので、速度のためではない（実測では `load` より数%遅い）。
DB を作るテストは実行ごとの一時ディレクトリ（`tmp_path_factory`）に置く——固定パスは同時に走る
別の pytest プロセス（IDE のランナーとターミナルなど）とも共有されるので、`loadfile` だけでは競合が残る。
**求めるのは並列・直列のどちらか一方ではなく、通っていることである**——
並列でしか通らない（あるいは直列でしか通らない）状態は、それ自体が欠陥である。

`ruff check` と `ruff format --check` はリポジトリ全体を見る（GitHub Actions も同じ）。
一方 **`mypy` の範囲は `kawasemi/` と `scripts/`、および `indexer/kawasemi_indexer/`** である。
`tests/` `examples/` `alembic/` の**型エラーは検査をすり抜ける**。

`.pre-commit-config.yaml` の ruff-check / ruff-format フックはパス引数を持たないため、pre-commit を有効にした環境ではステージされた全 Python ファイルを対象にする。範囲は CI と一致している。
mypy フックのみ `files: ^(kawasemi|scripts)/` で絞っており、CI の `mypy kawasemi/ scripts/` と同一である。

### 4.2 変更内容に応じて回すもの

| 変更した箇所 | 追加で必要なこと |
|---|---|
| モデル定義 | Alembic のマイグレーションを作る（`DEVELOPMENT.md` の「データベース・マイグレーション」） |
| AI 入力の経路（executor・resolver・compiler・索引・設計 API） | `make e2e` の関門が全部 PASS のままかを見る。モデルや embedding を変えたなら `make e2e-live`（実機層。[e2e_guide.md](e2e_guide.md)） |
| 索引や成果物契約 | 契約のゴールデンテスト（`tests/test_contract.py`）が通ることを確認する（下記「成果物契約を変更するときの追加手順」） |
| 画面の JS・監査図（mermaid） | `npm ci` のあと、CI の「JS 構文チェック」「mermaid 構文チェック」と同じ検査を回す（`.github/workflows/ci.yml`） |

### 4.3 実行環境によって skip される範囲

依存が無い環境では、テストが黙って skip される。
skip は「通った」ではない。
その経路を変更したなら、依存を揃えた環境で回す必要がある。

| 依存 | 揃っていない場合に skip される範囲 |
|---|---|
| 実 Ollama（`OLLAMA_BASE_URL` に到達でき、`OLLAMA_VISION_MODEL` か `OLLAMA_MODEL` が一覧にある） | `tests/ai_input/test_file_vision.py::test_handwritten_consent_real_vision` の1件（`tests/e2e/helpers.py` の `ollama_unavailable_reason` が理由付きで skip する） |
| `-m live`（既定の `addopts` が外す） | `tests/e2e/test_pipeline_live.py`（E2E 実機層）。`make e2e-live` で回し、LLM サーバと embedding モデルに届かなければ理由付きで skip する |
| node（pytest の外） | JS と mermaid の構文検査は pytest に含まれない。GitHub Actions では node が無ければジョブが落ちる（skip しない） |

GitHub Actions には実 Ollama も LLM サーバも無い。
**GitHub Actions が green であっても、実機の LLM を使う経路が検証された証拠にはならない。**
E2E の決定的層（`make e2e`）は Ollama を使わないので、GitHub Actions でも skip せずに走る。

### 4.4 テスト失敗が自分の変更起因かを切り分ける

実 Ollama を使う環境では、外部のモデル出力に依存するために不安定なテストがある。

| テスト | 失敗の内容 |
|---|---|
| `tests/ai_input/test_file_vision.py::test_handwritten_consent_real_vision` | Vision の読み取り結果が期待とずれる。モデルが一覧に無い場合は失敗ではなく理由付きで skip する |

切り分けは worktree で行う。
作業中のツリーで `git stash` と `git checkout` を使うと、作業中の変更と混ざる。

```bash
git worktree add /tmp/kawasemi-triage origin/main
cd /tmp/kawasemi-triage && uv sync --all-packages --all-groups --all-extras
uv run pytest <落ちたテスト> -q
```

`origin/main` でも同じく落ちるなら既存の問題であり、自分の変更起因ではない。
その判断を PR 本文に書く。
`--deselect` で除外したならその旨も書く。

確認が終わったら worktree を片付ける。

```bash
git worktree remove /tmp/kawasemi-triage
```

## 5. レビュー（PR 作成前）

PR を作る前に、実装の妥当性を実装者以外の視点（または時間を置いた自分の視点）で見直す。

§4の検証は機械的な検査であり、実装が正しいかどうかは見ない。
この段階はそこを埋めるためにある。

見るべき観点は、正しさ、保守性、セキュリティ、性能である。
コーディングスタイルの好みは対象にしない（ruff と ruff format が担う）。

解消しない指摘があれば、その判断と理由を PR 本文の「レビュー観点 / 補足」欄に残す。

## 6. PR を出す

### 意図しないコミットの混入を確認する（push の前）

```bash
git fetch origin
git log origin/main..HEAD --oneline
```

`git fetch` を先に行わないと、古い `origin/main` との比較になって意味がない。

### push して PR を作る

```bash
git push -u origin <branch>
gh pr create
```

- タイトルは Conventional Commits 形式にする
- 本文は PR テンプレートに従う
- その Issue の受け入れ条件をすべて満たすなら `Closes #<番号>`、一部だけなら `Refs #<番号>` と書き、
  残った作業を Issue にコメントで残す

### 検証結果を記録する

PR テンプレートのチェックボックス（pytest、ruff、mypy）は§4の結果を反映する。

あわせて、次を本文に書く。

- どこで回したか。実 Ollama や LLM サーバが揃っていたか（4.3 の表で skip 範囲が変わる）
- 4.4 の切り分けを行ったなら、その判断
- 実行したコマンドと結果

GitHub Actions の結果が出るまでマージしない。

## 7. マージ前の確認

マージの直前に、次が揃っているかを確認する。

1. §4の検証結果と、それをどこで回したか。第三者が再現できる形で PR 本文に残っている
2. 4.3 により skip された範囲が、今回の変更範囲に関係しない
3. Issue の受け入れ条件それぞれに対応する確認内容

受け入れ条件のうち確認できていないものが1つでもあれば、マージしない。

## 8. マージする

squash merge を既定とする。

```bash
gh pr merge <番号> --squash --delete-branch
```

`--delete-branch` を付ける。
squash merge はブランチのコミットを `main` の祖先にしないため、後から `git branch -d` を実行すると "not fully merged" で拒否される。

マージ後は `main` を pull してからローカルの feature ブランチを削除する。
順序を逆にすると、次のブランチを stale な main から切ることになる。

## セキュリティ上の問題の扱い

脆弱性を見つけた場合は、公開の Issue ではなく [SECURITY.md](../SECURITY.md) に従って報告する。
同じ層の修正は相互に影響するので、関連する修正はまとめて1つの PR にしてレビューする。

## 成果物契約を変更するときの追加手順

索引の成果物契約（ファイル形式、レコードスキーマ、埋め込み仕様）を変えるときは、[CONTRACT.md](../CONTRACT.md) の規定に従う。
indexer（ビルド側）と KAWASEMI 本体（クエリ側）は別のネットワークで動くため、`schema` / `embedder` を双方が別々に持つ。

- `SCHEMA_VERSION` を両方で上げる（`kawasemi/ai_input/resource_index/schema.py` と `indexer/kawasemi_indexer/schema.py`）
- 契約のゴールデンテスト（`tests/test_contract.py`）の期待値を更新する。両方の複製を同じ期待値で検査するので、片方だけ変えるとここで落ちる。これが意図的な変更であることの関門になる
- `CONTRACT.md` と `indexer/docs/kawasemi_indexer_spec.md` の記述を合わせる
