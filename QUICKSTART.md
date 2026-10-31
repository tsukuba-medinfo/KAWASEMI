# KAWASEMI クイックスタート

## 前提条件

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## セットアップ

```bash
# 1. クローン
git clone https://github.com/tsukuba-medinfo/KAWASEMI.git
cd KAWASEMI

# 2. 依存関係インストール（indexer/ も uv workspace のメンバーとして入る）
uv sync --all-packages --all-groups --all-extras

# 3. 環境変数（DATABASE_URL は SQLite の既定のままで可）
cp .env.example .env
# SECRET_KEY / CSRF_SECRET_KEY は必須（32文字以上、互いに別の値）。空のままだと
# kawasemi serve・alembic・make e2e が設定の検証エラーで止まる。次の1行で生成して
# .env の末尾に追記できる（同じキーが2回あると後の行が優先される）
uv run python -c "import secrets; [print(f'{k}=\"{secrets.token_urlsafe(32)}\"') for k in ('SECRET_KEY', 'CSRF_SECRET_KEY')]" >> .env

# 4. データベースのスキーマを作る（SQLite・PostgreSQL とも毎回必須。最新なら何もしない）
uv run alembic upgrade head

# 5. サーバ起動
uv run kawasemi serve
```

アクセス先:

- **Web UI**: http://localhost:8000/
- **API ドキュメント**: http://localhost:8000/docs

### 別の機械から http で開く場合（`COOKIE_SECURE`）

認証の Cookie には既定で Secure 属性が付きます（`COOKIE_SECURE=true`）。ブラウザは Secure 属性の
Cookie を HTTPS か `http://localhost` でしか保存しないので、`http://<サーバのIPやホスト名>:8000/` で
開くと Cookie が保存されず、ログインできません。

- 本番やチームでの利用は、リバースプロキシで HTTPS を終端し、`COOKIE_SECURE=true` のままにします（[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)）。
- HTTPS を用意できない閉じた検証環境で、別の機械から http で試すときだけ `.env` に `COOKIE_SECURE=false` を書きます。
  Cookie が平文の通信に載るので、患者データを扱う環境では使いません。

### PostgreSQL を使う場合

`.env` の `DATABASE_URL` を変更してから、手順4のマイグレーションを実行してください:

```env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/kawasemi
```

```bash
uv run alembic upgrade head
```

## 手動入力を試す

```bash
# ユーザー・ロール・サンプル Questionnaire・症例割り当てを作成（既存のDBはリセットされる。
# 全表を消してから alembic upgrade head を当て直す）
uv run python examples/init_demo_data.py
```

パスワードは `DEMO_PASSWORD` で与えた値か、実行時に表示される生成値です。

## 自己完結のデモ（E2E 試験環境）

施設のデータが無くても、架空の感染症様式とダミーの外部システム（DWH・医事会計・ファイル・FHIR）で
データ自動収集の工程を一通り動かせます。

```bash
# ダミー外部システムと正解データを var/e2e/ に生成する（アプリDBには触らない）
uv run kawasemi e2e setup

# 決定的層: 様式 → 臨床的意味 → 所在探索 → 手順書 → DSL → 実行 → 回答の保存 を
# 台本 LLM で通し、段階ごとの関門 a〜e を確かめる（Ollama 不要）
make e2e

# 実機層（bge-m3 と LLM サーバが必要）の実行結果をブラウザで見る
make e2e-live
uv run kawasemi e2e serve      # http://127.0.0.1:8030/
```

詳しくは [docs/e2e_guide.md](docs/e2e_guide.md) を参照してください。

## 実施設で使う

施設固有の設定（接続レジストリ・キー種別・所在ルールなど）は `site/` に置きます。
テンプレートは `site.example/` にあるので、複製して編集してください。

```bash
cp -r site.example site
```

索引の成果物や出力ファイルなど、実行で生まれるものは `var/` に置かれます。
索引の作り方は [docs/resource_index_guide.md](docs/resource_index_guide.md) を参照してください。

## 次のステップ

- データ自動収集（AIモード）の使い方 → [QUICKSTART_AI.md](QUICKSTART_AI.md)
