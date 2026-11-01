# KAWASEMI データ自動収集（AIモード）ガイド

KAWASEMI のデータ自動収集機能（旧称: AI支援入力）を使うと、電子カルテの構造化データや病理報告書テキストから各 Questionnaire 項目を LLM が自動補完します。

手動入力の基本操作については [QUICKSTART.md](QUICKSTART.md) を参照してください。

> **ロール体系**: KAWASEMI は **4役割**（admin 管理者 / designer 設計者 / collector 収集者 / reviewer 閲覧・検証者）で運用します。設計工程（臨床的意味→所在→手順書→プラン）は設計者1人で役割を切り替えずに通ります。管理者はシステムの運用だけを持ち、設計・収集・検証の画面には入りません。正本は [docs/roles_and_permissions.md](docs/roles_and_permissions.md) を参照してください。

---

> このファイルは**起動して動かすまで**に絞っている。アーキテクチャ・データモデル・各ステップの操作・通しの作例・トラブルシューティングは [docs/ai_mode_guide.md](docs/ai_mode_guide.md)、**DSL の仕様**（op・意味論・検証・監査）は [docs/dsl_specification.md](docs/dsl_specification.md)（こちらが正本）。

## 1. 前提条件

- Python 3.12+・uv（[QUICKSTART.md](QUICKSTART.md) 参照）
- [Ollama](https://ollama.com/) — ローカル LLM ランタイム

```bash
# Ollama インストール後、推奨モデルを取得
ollama pull qwen3.8:27b
```

`.env` でデフォルトモデルを指定します:

```env
OLLAMA_MODEL=qwen3.8:27b
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_TIMEOUT=120
# 手書き同意書・スキャンPDF（テキスト層なし）を読むなら、画像入力に対応したモデルを指定する。
# 空なら OLLAMA_MODEL を流用するが、OLLAMA_MODEL が画像を受け付けるとは限らない
# OLLAMA_VISION_MODEL=<画像入力に対応したモデル>
```

vLLM（`LLM_PROVIDER=vllm`）で設計ロールを回す場合、`VLLM_MODEL`（既定 `qwen3.8-27b`）は vLLM サーバの
`--served-model-name` と同じ名前にしてください。

> 各ステップの画面にある **「LLMモデル（任意）」** 欄でモデルを実行時に上書きできます。

---

## 2. セットアップ

[QUICKSTART.md](QUICKSTART.md) の「セットアップ」手順1〜4（`.env` の SECRET_KEY / CSRF_SECRET_KEY の設定と
`uv run alembic upgrade head`）を済ませてから進めてください。鍵が空だと起動時に設定の検証エラーで止まり、
マイグレーションを当てていないと `no such table: roles` で止まります。

```bash
# 0. データベースのスキーマ（QUICKSTART.md の手順4。済んでいれば何もしない）
uv run alembic upgrade head

# 1. 利用者・ロール・サンプル Questionnaire の初期化（未実施の場合。既存のDBはリセットされる）
uv run python examples/init_demo_data.py

# 2. Ollama 起動確認
ollama serve   # 別ターミナルで（すでに起動中なら不要）

# 3. アプリ起動（AIモードを有効化して起動）
ENABLE_AI_DRAFTING=true ENABLE_AI_EXTRACTION=true uv run kawasemi serve
# または .env に ENABLE_AI_DRAFTING=true / ENABLE_AI_EXTRACTION=true を設定済みであれば:
# uv run kawasemi serve
```

ログイン:

```
http://localhost:8000/login
ユーザー名: designer / パスワード: DEMO_PASSWORD の値（未指定なら init_demo_data.py が表示した値）
```

> `designer` ユーザーは **設計者**（designer）を保有し、全設計工程（step1-A 臨床的意味、step1-B 所在、step2-3 手順書・プラン）を役割を切り替えずに通します。ロールごとの権限は [docs/roles_and_permissions.md](docs/roles_and_permissions.md) を参照してください。

所在探索・実行には施設のデータ（接続レジストリ・リソース索引）が要ります。
施設のデータが無いときは、架空の感染症様式とダミーの外部システムで工程を通す E2E 試験環境を使ってください（`uv run kawasemi e2e setup` / `make e2e` / `make e2e-live` → `uv run kawasemi e2e serve`。[docs/e2e_guide.md](docs/e2e_guide.md)）。
実施設では `site.example/` を `site/` へ複製して編集し、索引を作ります（[docs/resource_index_guide.md](docs/resource_index_guide.md)）。

---

## 3. 次に読むもの

- [アーキテクチャ・データモデル](docs/ai_mode_guide.md)（1・2章）
- [ステップごとの操作](docs/ai_mode_guide.md)（6〜9章: データソース調査 → 取得手順書 → 取得プラン → AI自動入力）
- [DSL 仕様](docs/dsl_specification.md)（op 一覧・`guard`・フォールバック・`params`・検証・監査）
- [トラブルシューティング](docs/ai_mode_guide.md)
