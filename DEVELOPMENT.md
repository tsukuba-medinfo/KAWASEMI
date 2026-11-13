# 開発者ガイド

開発環境のセットアップから、コードスタイル規約・テスト・デプロイまでを説明します。

## 目次

1. [開発環境のセットアップ](#1-開発環境のセットアップ)
2. [プロジェクト構造](#2-プロジェクト構造)
3. [コードスタイルとLint](#3-コードスタイルとlint)
4. [型チェック（mypy）](#4-型チェックmypy)
5. [テスト](#5-テスト)
6. [データベース・マイグレーション](#6-データベースマイグレーション)
7. [環境変数リファレンス](#7-環境変数リファレンス)
8. [コントリビューションフロー](#8-コントリビューションフロー)

---

## 1. 開発環境のセットアップ

### 前提条件

| ツール | バージョン | 用途 |
|---|---|---|
| Python | 3.12 以上 | ランタイム（CI の検証対象は 3.12。`.python-version` で固定）|
| [uv](https://github.com/astral-sh/uv) | 最新 | パッケージ管理・タスクランナー |
| [pre-commit](https://pre-commit.com/) | 4.0 以上 | コミット前自動チェック（`uv sync --all-packages --all-groups --all-extras` で入る） |

### 初回セットアップ

```bash
# 1. リポジトリをクローン
git clone https://github.com/tsukuba-medinfo/KAWASEMI.git
cd KAWASEMI

# 2. 依存関係をインストール（開発用パッケージと、uv workspace のメンバー indexer/ を含む）
uv sync --all-packages --all-groups --all-extras

# 3. 環境変数を設定
cp .env.example .env
# .env の SECRET_KEY / CSRF_SECRET_KEY を設定する（必須。32文字以上、互いに別の値）。
# 次の1行で生成して末尾に追記できる（同じキーが2回あると後の行が優先される）
uv run python -c "import secrets; [print(f'{k}=\"{secrets.token_urlsafe(32)}\"') for k in ('SECRET_KEY', 'CSRF_SECRET_KEY')]" >> .env

# 4. データベースのスキーマを作る（毎回必須。最新なら何もしない）
uv run alembic upgrade head

# 5. git フックを有効化（commit 時に ruff/mypy、push 時に main への直接 push ガード）
make hooks

# 6. 開発サーバを起動
uv run uvicorn kawasemi.api.main:app --reload
```

ブラウザで http://localhost:8000 にアクセスできれば起動成功です。

> `make hooks` は `uv run pre-commit install --hook-type pre-commit --hook-type pre-push`
> を実行します。以前の手順（`git config core.hooksPath scripts/git-hooks`）を実行済みの
> 環境では、`make hooks` を実行し直せば `core.hooksPath` が解除され、新しい方式に
> 切り替わります。`core.hooksPath` が残っていると pre-commit は
> インストールを拒否し、commit 時の検証が走りません。

### 管理者ユーザーの作成

```bash
uv run kawasemi create_admin -u <username> -e <email> -n <name>
```

オプションでパスワードを `-p <password>` で指定できます（省略時はプロンプト入力）。

---

## 2. プロジェクト構造

```
KAWASEMI/
├── kawasemi/               # アプリケーション本体
│   ├── api/
│   │   ├── main.py         # FastAPI インスタンス・ミドルウェア設定
│   │   └── v100/           # REST API v100 エンドポイント
│   │       ├── __init__.py         # ルーター集約・CSRF トークン発行
│   │       ├── questionnaires.py   # Questionnaire CRUD
│   │       ├── questionnaire_responses.py  # QuestionnaireResponse CRUD
│   │       └── schemas.py          # Pydantic スキーマ
│   ├── ai_input/            # AI入力サブシステム（design系ロール・ドラフト/実行）
│   │   ├── runbook.py       # ステップ2: 取得手順書（runbook）の生成
│   │   ├── compiler.py      # ステップ3: 確認済み手順書 → DSLプログラムのコンパイル
│   │   ├── executor.py      # ステップ4: DSLプログラムの安全インタプリタ（AI自動入力実行）
│   │   ├── dsl.py           # DSL 共通ユーティリティ（op許可・$id健全性・needs_input）
│   │   ├── location_drafter.py  # ステップ1(役割2): データ所在の AI 案生成
│   │   ├── resource_drafter.py  # 同上（resource_index embedding 検索版）
│   │   ├── db_definition.py # DB定義書コーパスのロードとBM25検索
│   │   ├── llm_json.py      # Ollama 構造化JSON呼び出しヘルパー（チャンク分割を含む）
│   │   ├── batch.py         # バッチAI自動入力ランナー
│   │   ├── metrics.py       # 取得困難度メトリクスの集計
│   │   ├── statistical_profile.py  # 所在候補の統計プロファイル生成（最小セルサイズ規則）
│   │   ├── viz.py           # DSLプログラムの Mermaid DAG 生成
│   │   ├── labels.py        # 機能のユーザ向け表示ラベル（単一の真実源）
│   │   ├── models.py        # DB モデル定義
│   │   ├── schemas.py       # Pydantic スキーマ
│   │   ├── routes.py        # AI 関連 Web ルーター
│   │   ├── key_resolver/    # 被験者ID解決（fhir / http backend）
│   │   ├── resource_index/  # リソース索引（embedding 検索）
│   │   └── e2e/             # E2E 試験環境（架空の感染症様式・ダミー外部システムの生成と工程）
│   ├── core/
│   │   ├── auth.py         # パスワードハッシュ・JWT 生成/検証
│   │   ├── authz.py        # 患者データへのアクセス制御（ハード・スコープ）
│   │   ├── config.py       # 環境変数を Pydantic Settings で管理
│   │   ├── csrf.py         # CSRF トークンのセッション内安定化
│   │   ├── database.py     # SQLAlchemy 非同期エンジン・セッション
│   │   ├── deps.py         # FastAPI 依存性注入（認証・DB セッション）
│   │   ├── exceptions.py   # カスタム例外クラス
│   │   ├── lease.py        # 入力対象の先着自動割り当てと排他リース
│   │   └── logging_config.py  # JSON / テキストログ設定
│   ├── models/             # SQLAlchemy ORM モデル
│   │   ├── base.py                 # DeclarativeBase・TimestampMixin
│   │   ├── questionnaire.py        # Questionnaire テーブル
│   │   ├── questionnaire_response.py  # QuestionnaireResponse テーブル
│   │   ├── validation_log.py       # バリデーション結果ログ
│   │   ├── user.py                 # ユーザー・ユーザーロール
│   │   ├── role.py                 # ロール定義
│   │   ├── case_assignment.py      # 症例割り当て（旧機能。認可には使わない）
│   │   ├── subject.py              # 被験者（Subject＝Hub）
│   │   ├── dataset.py              # データセット
│   │   ├── input_target.py         # 入力対象
│   │   ├── batch_job.py            # バッチAI自動入力ジョブ
│   │   ├── valueset.py             # ValueSet
│   │   ├── auth_event.py           # 認証・アカウントロックの監査ログ
│   │   └── refresh_token.py        # リフレッシュトークン
│   ├── utils/
│   │   ├── fhir_client.py  # FHIR サーバ REST クライアント
│   │   ├── fhir_utils.py   # Questionnaire メタデータ抽出ユーティリティ
│   │   └── file_export.py  # JSON ファイルエクスポート
│   ├── web/
│   │   ├── routes.py               # Web UI ルーター（Questionnaire・フォーム）
│   │   ├── admin_routes.py         # 管理者ルーター（ユーザー管理）
│   │   ├── auth_routes.py          # 認証ルーター（ログイン・ログアウト）
│   │   ├── dataset_routes.py       # データセット・入力対象ルーター
│   │   ├── form_generator.py       # Questionnaire → HTML フォーム変換エンジン
│   │   ├── filter_utils.py         # 一覧フィルタ入力のパースと通知
│   │   ├── template_globals.py     # Jinja2 共通グローバルの登録
│   │   ├── static/                 # CSS / JavaScript
│   │   └── templates/              # Jinja2 テンプレート
│   │       ├── base.html           # 共通レイアウト
│   │       ├── form.html           # フォーム入力画面
│   │       ├── admin/              # 管理者画面
│   │       ├── ai_input/           # AI入力サブシステムの画面
│   │       └── ...                 # その他画面
│   └── cli.py              # CLI コマンド（create_admin など）
├── alembic/                # データベースマイグレーション
│   ├── env.py              # マイグレーション実行環境（モデルの import 集約）
│   └── versions/           # マイグレーションスクリプト
├── tests/                  # テストスイート
│   ├── api/                # API エンドポイントテスト
│   ├── core/               # core パッケージのテスト
│   ├── models/             # モデル単体テスト
│   ├── ai_input/           # AI入力サブシステムのテスト
│   ├── fixtures/           # テスト用の入力データ
│   ├── e2e/                # E2E 試験（サーバ起動不要）
│   │   ├── test_pipeline_e2e.py    # 決定的層（関門 a〜e。make e2e）
│   │   └── test_pipeline_live.py   # 実機層（bge-m3 と LLM サーバ。make e2e-live）
│   ├── conftest.py         # pytest フィクスチャ
│   └── test_*.py           # その他テスト
├── indexer/                # kawasemi-indexer（索引ビルド側。uv workspace のメンバー）
├── site.example/           # 施設固有の設定のテンプレート（site/ へ複製して使う。site/ はコミットしない）
├── docs/                   # ドキュメント
├── examples/               # サンプル Questionnaire・ロードスクリプト・E2E 試験環境の固定資産（examples/e2e/）
├── scripts/                # 運用スクリプト
├── .github/                # GitHub Actions のワークフローと各種テンプレート
├── .env.example            # 環境変数テンプレート
├── .pre-commit-config.yaml # pre-commit フック設定
├── Makefile                # 開発用ターゲット（hooks / test / e2e など）
├── pyproject.toml          # プロジェクト設定・ツール設定
├── CONTRACT.md             # indexer と本体の成果物契約
├── DESIGN.md               # 設計方針
├── DEVELOPMENT.md          # 本ドキュメント
├── README.md               # プロジェクト概要
├── QUICKSTART.md           # クイックスタートガイド
└── QUICKSTART_AI.md        # AI 機能を起動して動かすまで（詳細は docs/ai_mode_guide.md、DSL は docs/dsl_specification.md）
```

構成ツリーは主要なモジュールとディレクトリを示す。次は省略している。

- `__init__.py` と、各パッケージの下位ファイル
- ビルド・実行設定（`Dockerfile` / `docker-compose.yml` / `alembic.ini` / `uv.lock`）
- リポジトリ運用ファイル（`LICENSE` / `.gitignore` などのドットファイル）

---

## 3. コードスタイルとLint

### ツール構成

| ツール | 役割 | 設定箇所 |
|---|---|---|
| [ruff](https://docs.astral.sh/ruff/) | リント + フォーマット | `pyproject.toml` `[tool.ruff]` |
| [mypy](https://mypy.readthedocs.io/) | 静的型チェック | `pyproject.toml` `[tool.mypy]` |

> **注**: `black` はかつて使用していましたが、`ruff format` に統一しました。
> `ruff format` は black 互換の出力を生成するため、見た目の差異はありません。

### コマンド一覧

```bash
# リントチェック（エラーのみ表示）。CI と同じくリポジトリ全体を見る
uv run ruff check .

# リントチェック + 自動修正
uv run ruff check --fix .

# フォーマットチェック（差分を表示するだけで変更しない）。CI と同じくリポジトリ全体を見る
uv run ruff format --check .

# フォーマット適用
uv run ruff format .

# 型チェック（CI と同一）
uv run mypy kawasemi/ scripts/
(cd indexer && uv run mypy kawasemi_indexer/)

# 全チェックを一括実行
uv run ruff check . && uv run ruff format --check . && uv run mypy kawasemi/ scripts/ \
  && (cd indexer && uv run mypy kawasemi_indexer/)
```

### 有効なルールセット

`pyproject.toml` の `[tool.ruff.lint]` で以下のルールセットを有効にしています。

| コード | カテゴリ | 概要 |
|---|---|---|
| E, W | pycodestyle | PEP 8 スタイルエラー・警告 |
| F | Pyflakes | 未使用インポート・未定義変数 |
| I | isort | インポート順序 |
| N | pep8-naming | 命名規則 |
| UP | pyupgrade | 古い Python 構文の更新 |

**プロジェクト固有の除外設定:**

```toml
ignore = [
    "E501",  # 行長: ruff format に委ねるため無効化
    "N818",  # Exception サフィックス規約: プロジェクト方針として許容
]
```

### pre-commit フック

`make hooks` 実行後、`git commit` のたびに以下が自動実行されます（`.pre-commit-config.yaml`）。

```yaml
- ruff-check --fix   # リント + 自動修正（リポジトリ全体、CI の `ruff check .` と同一）
- ruff-format        # フォーマット整形（リポジトリ全体、CI の `ruff format --check .` と同一）
- mypy kawasemi/ scripts/  # 型チェック（CI の `mypy kawasemi/ scripts/` と同一）
```

mypy エラーがあるとコミットがブロックされます。`# type: ignore[...]` で抑制する場合は、コメントで理由を明記してください。
ruff が自動修正した場合もコミットは失敗します。修正内容を確認して `git add` し直してください。

mypy のみ検査範囲を `kawasemi/` と `scripts/` に限定しています（CI と同一。indexer は CI が別に `indexer/` で回します）。
`tests/` `examples/` `alembic/` などを mypy の対象に広げる場合は CI 側の対象も同時に広げてください。

`git push` 時は `main` への直接 push をブロックするガードだけが走ります
（`scripts/pre_push_guard.sh`）。変更は PR で入れる運用なので、手元での誤 push を止めます。

```bash
# 緊急回避（理由を PR やコミットに残すこと）
git commit --no-verify
git push --no-verify
```

---

## 4. 型チェック（mypy）

`pyproject.toml` で `strict = true` を設定しています。主な要件：

- 全関数に引数・戻り値の型アノテーションが必要
- `Any` の使用は最小限に抑える
- 循環インポートを避けるため、モデル間の型参照は `TYPE_CHECKING` ガードと `from __future__ import annotations` を使用する

```python
# モデルでの循環インポート回避パターン
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kawasemi.models.other_model import OtherModel
```

---

## 5. テスト

KAWASEMI のテストは**ユニット・統合テスト**と**E2E シナリオテスト**の2層構成です。

### 5-1. ユニット・統合テスト

```bash
# 全テスト実行（tests/ と indexer/tests/。並列は make test）
uv run pytest -v

# カバレッジ付き
uv run pytest -v --cov=kawasemi --cov-report=term-missing

# 特定ファイルのみ
uv run pytest tests/api/ -v
uv run pytest tests/test_form_generator.py -v

# キーワードでフィルタ
uv run pytest -k "questionnaire" -v
```

#### テスト構成

```
tests/
├── conftest.py             # 共通フィクスチャ（テスト用 DB セッションなど）
├── api/                    # API エンドポイントテスト
├── core/                   # 設定・依存性注入・FHIR クライアント・ログのテスト
├── models/                 # ORM モデル単体テスト
├── ai_input/               # AI入力サブシステムのテスト（DSL・コンパイラ・実行器ほか）
├── fixtures/               # テスト用の入力データ（DB定義書・FHIR辞書・ファイル）
├── e2e/                    # E2E シナリオテスト（5-2 を参照）
├── test_fhir_integration.py  # FHIR クライアント統合テスト
├── test_form_generator.py    # フォーム生成エンジンテスト
└── test_security.py          # 認証・CSRF テスト
```

`tests/` 直下にはこのほかにも `test_*.py` があります。

テストは SQLite インメモリ DB（`sqlite+aiosqlite:///:memory:`）を使用します。
FHIR 連携テスト（`test_fhir_integration.py` / `tests/core/test_fhir_client.py`）は
**外部サーバを必要としません**。`tests/fhir_resources.py` の `httpx.MockTransport` が
決まった応答を返します。

---

### 5-2. E2E 試験

`tests/e2e/` は、架空の感染症様式とダミーの外部システム（DWH・医事会計・ファイル・FHIR）で
様式 → 臨床的意味 → 所在探索 → 手順書 → DSL → 実行 → 回答の保存 を一気通貫で通す
**エンドツーエンドテスト**です。httpx + ASGITransport でアプリをインプロセスで動かします。

```bash
make e2e        # 決定的層（台本 LLM と字面の embedding。関門 a〜e を assert。Ollama 不要・CI でも走る）
make e2e-live   # 実機層（bge-m3 と LLM サーバ。assert せず var/e2e/live_runs/ に表を記録）
```

二層の使い分け・前提条件・結果の読み方は [docs/e2e_guide.md](docs/e2e_guide.md)、
テストの構成は [`tests/e2e/README.md`](tests/e2e/README.md) を参照してください。

---

## 6. データベース・マイグレーション

### 開発環境（SQLite）

`DATABASE_URL=sqlite+aiosqlite:///./kawasemi.db` の場合、サーバ起動時に自動でテーブルが作成されます。マイグレーション不要です。

### 本番環境（PostgreSQL）

```bash
# マイグレーションの状態確認
uv run alembic current

# 最新まで適用
uv run alembic upgrade head

# 1つ前に戻す
uv run alembic downgrade -1
```

### マイグレーションファイルの作成

モデルを変更したら、マイグレーションファイルを生成します。

```bash
# 差分を自動検出して生成（--autogenerate）
uv run alembic revision --autogenerate -m "変更内容の説明"

# 生成されたファイルを確認・手修正してから適用
uv run alembic upgrade head
```

> 自動生成は完全ではないため、生成されたスクリプトを必ず確認してください。
> 特に `JSON` カラムや制約の変更は手動で補正が必要な場合があります。

### ORM 宣言と物理制約の食い違い（意図的なもの）

**SQLite は既存テーブルへの `ALTER ADD CONSTRAINT` / `ALTER COLUMN` を持ちません。** そのため、テーブルを作った後に足した外部キーと NOT NULL は**物理制約になりません**。KAWASEMI はこれを是正せず、**参照整合性と非NULL性を ORM 宣言とアプリ層で担保する**方針を採っています。

現在その状態にあるのは **外部キー 11 箇所・NOT NULL 3 箇所**で、一覧は `tests/test_timestamp_server_defaults_migration.py` の `_KNOWN_MISSING_FOREIGN_KEYS` / `_KNOWN_NULLABLE_IN_DB` にあります。個々の理由は、その列を足したマイグレーションの docstring に書いてあります。

是正（`render_as_batch=True` ＋ テーブル再構築）を採らない理由は、対象に `questionnaire_responses` / `case_assignments` / `input_targets` といった中核テーブルを含み、**再構築のデータ損失リスクが、宣言と物理を一致させる利得を上回る**と判断したためです。PostgreSQL へ移行すればこの制約は消えるので、そのときにまとめて再検討します。

**帰結として `alembic revision --autogenerate` の差分は常に非空で、`alembic check` も
既知の差分（物理 FK の欠け・NOT NULL の食い違い）を報告して失敗します。** 自動生成した
スクリプトにはこれらの操作が混ざるので、**必ず目で見て、意図した変更以外の操作を消してから**
コミットしてください。 差分を目で見て異常を検出する運用は、この14件に紛れて新しい差分を見落とします。そこで**既知の集合と一致すること**をテストで固定してあり、増えても減っても落ちます。

- 新しく物理 FK を張らない列を足すとき: マイグレーションの docstring に理由を書いてから `_KNOWN_MISSING_FOREIGN_KEYS` へ足す
- 物理制約化したとき: 一覧から消す（消し忘れるとテストが落ちる）

### 版の記録と実スキーマの食い違いを見る

```bash
uv run kawasemi check_schema
```

モデル宣言（`Base.metadata`）にあって DB に無い表と列、および `alembic_version` と移行の head の食い違いを表示します。食い違いがあれば終了コード 1 を返します。アプリ起動時にも同じ検査が走り、食い違いがあれば ERROR ログを 1 行出します（**起動は止めません**。運用中のデータを保持する DB では、起動を止めると業務も止まるためです）。

**この検査は CI では発火しません。** CI もテストも DB を新規に作るので、モデルどおりの表と列が必ず揃います。食い違いが起きるのは移行を当て損ねた既存 DB だけで、そこを見る口がこれです。たとえば `alembic_version` が head を主張したまま `input_targets.needs_attention` が存在しない DB では、その列を読む `/input-targets` が 500 になるまで欠けに気付けません。

**`alembic upgrade head` では直りません。** alembic は当該版を適用済みと信じて何もしないので、欠けている列の DDL を手で当てるか、DB を作り直します。

型・`nullable`・`server_default` の違いは見ません（列の有無では検出できないため）。移行で入れる既定値は `tests/test_timestamp_server_defaults_migration.py` が移行を実際に走らせて検査しています。

---

## 7. 環境変数リファレンス

`.env.example` を `cp .env.example .env` でコピーして使用します。

| 変数 | デフォルト | 説明 |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./kawasemi.db` | DB 接続 URL |
| `SECRET_KEY` | （必須）| JWT 署名キー。32文字以上のランダム文字列 |
| `CSRF_SECRET_KEY` | （必須）| CSRF トークン署名キー。`SECRET_KEY` とは別の値を使用 |
| `FHIR_SERVER_URL` | `http://localhost:8080/fhir` | FHIR サーバ URL |
| `FHIR_SERVER_TIMEOUT` | `30` | FHIR サーバ接続タイムアウト（秒）|
| `LOG_LEVEL` | `INFO` | ログレベル（`DEBUG` / `INFO` / `WARNING` / `ERROR`）|
| `LOG_FORMAT` | `json` | ログフォーマット（`json` / `text`）|
| `ENVIRONMENT` | `development` | 環境識別子 |
| `DEBUG` | `false` | デバッグモード |

### AI / Ollama 系

AI 機能フラグと Ollama 呼び出しの設定です。正本は `kawasemi/core/config.py` の Field 定義です。

| 変数 | デフォルト | 説明 |
|---|---|---|
| `ENABLE_AI_DRAFTING` | `false` | 候補探索AI（設計時）: データ所在AI案・手順書生成・取得プラン作成など、人間レビュー前提の設計ドラフト生成を有効化 |
| `ENABLE_AI_EXTRACTION` | `false` | 自動構造化AI（実行時）: `llm_extract` / `llm_judge` op による患者データへの LLM 適用を有効化（確認書様式の分岐キー）|
| `CLAIM_LEASE_MINUTES` | `120` | 収集者が「次の患者を受け取る」で得た担当（排他リース）の長さ（分）。開く・保存・autofill のたびに延長し、無操作でこの時間が過ぎると失効して他の収集者へ回る |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama サーバ URL |
| `EMBEDDING_BASE_URL` | （空）| embedding（`EMBEDDING_MODEL`）だけを別の Ollama へ向ける URL。空なら `OLLAMA_BASE_URL` を使う。LLM・Vision の接続先は変わらない。**索引を作ったサーバと bge-m3 の digest（`ollama list`）を揃えること**——build_info はモデル名しか照合しないので、digest が違っても検索は止まらず黙って劣化する |
| `OLLAMA_MODEL` | `qwen3.8:27b` | AI 機能に使用する Ollama モデル名 |
| `OLLAMA_VISION_MODEL` | （空）| 手書き/スキャン PDF 読取用のマルチモーダル Ollama モデル名。空なら `OLLAMA_MODEL` を流用するが、`OLLAMA_MODEL` が画像入力に対応しているとは限らないので、手書き・スキャン PDF を読むなら画像入力に対応したモデルを指定する |
| `OLLAMA_TIMEOUT` | `120` | Ollama タイムアウト（秒・1コール単位）。手順書生成は4項目ずつの分割コール|
| `LLM_TOTAL_TIMEOUT` | `600` | チャンク分割 LLM 処理全体のデッドライン（秒）。`OLLAMA_TIMEOUT` より大きくしないと1コールの途中で全体が先に切れる |
| `VISION_PDF_MAX_PAGES` | `20` | Vision OCR 対象 PDF の最大ページ数（超過時 step 失敗）|
| `VISION_PDF_MAX_BYTES` | `33554432` | Vision OCR 画像の合計バイト上限（超過時 step 失敗）|

### リソース索引・DB定義書系

データ所在の AI 案作成（ステップ1役割2）が参照する施設固有データの場所と、embedding 検索の設定です。正本は同じく `kawasemi/core/config.py` の Field 定義です。

| 変数 | デフォルト | 説明 |
|---|---|---|
| `DB_DEFINITION_DIR` | `site/db_definition` | DB定義書コーパスのディレクトリ（`column_index.jsonl` 等を含む）|
| `RESOURCE_INDEX_DIR` | `var/resource_index` | リソース索引ディレクトリ（`*_index.jsonl` + `embeddings.npy` + `build_info.json`）|
| `EMBEDDING_MODEL` | `bge-m3` | リソース索引の embedding モデル名（Ollama）。**索引生成時と一致させること** |
| `RESOURCE_INDEX_MIN_SCORE` | `0.5` | embedding 検索の足切り閾値（cosine）|

### シークレットキーの生成

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# または
uv run python scripts/generate_secret_key.py
```

---

## 8. コントリビューションフロー

ブランチの切り方、検証の手順、skip される範囲の切り分け、成果物契約を変えるときの追加手順は
**[docs/development_flow.md](docs/development_flow.md)** にまとめています。
本章では、そこから参照される GitHub Actions の設定だけを説明します。

### GitHub Actions

検証はローカルで回してもよく、GitHub Actions は選択肢の1つです。

`.github/workflows/ci.yml` は `main` への push・PR 時に実行されます。

| ジョブ | 内容 |
|---|---|
| コード品質チェック | ruff check・ruff format・mypy（`kawasemi/` `scripts/` と `indexer/`）・JS 構文チェック・mermaid 構文チェック |
| テスト | pytest（`tests/` と `indexer/tests/`。E2E の決定的層を含む） |

AI 機能フラグは CI が `true` に設定します。Ollama が無いので、実 Ollama を使うテストは
個別に skip され、E2E の実機層（marker `live`）は既定で外れます。
skip される範囲と、その場合に何を追加で確認すべきかは `docs/development_flow.md` の 4.3 にまとめています。
