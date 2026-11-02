# KAWASEMI デプロイメントガイド

## Docker を使用したデプロイ

### 前提条件

- Docker 20.10以上
- Docker Compose 2.0以上

### クイックスタート

1. **リポジトリのクローン**

```bash
git clone https://github.com/tsukuba-medinfo/KAWASEMI.git
cd KAWASEMI
```

2. **環境変数の設定**

```bash
cp .env.example .env
# SECRET_KEY / CSRF_SECRET_KEY は必須（32文字以上、互いに別の値）。空のままだとコンテナが
# 設定の検証エラーで起動しない。次の1行で生成して末尾に追記できる（後の行が優先される）
python3 -c "import secrets; [print(f'{k}=\"{secrets.token_urlsafe(32)}\"') for k in ('SECRET_KEY', 'CSRF_SECRET_KEY')]" >> .env
```

`docker-compose.yml` は `env_file: .env` でこの値をコンテナへ渡します。`DATABASE_URL`・`ENVIRONMENT`・
`DEBUG` などは `docker-compose.yml` の `environment` が優先されます。

コンテナは root ではなく専用ユーザー（uid 10001）で動きます。書き込むのは `/app/data`（SQLite の DB）・
`/app/var`・`/app/output` だけです。root で動かしていた旧版のボリュームを引き継ぐ場合は、先に所有者を
合わせてください（例: `docker compose run --rm --user root kawasemi chown -R 10001:10001 /app/data /app/output`）。

AI 機能を使う場合は、`docker-compose.yml` のコメントにある `OLLAMA_BASE_URL`（vLLM なら `LLM_PROVIDER=vllm` と
`VLLM_BASE_URL`）を推論サーバのアドレスにします。コンテナ内の `localhost` はコンテナ自身を指します。

認証の Cookie には既定で Secure 属性が付きます（`COOKIE_SECURE=true`）。ブラウザは Secure 属性の Cookie を
HTTPS か `http://localhost` でしか保存しないので、`http://<ホスト名>:8000/` のように平文の http で別の機械から
開くと、Cookie が保存されずログインできません。運用では下の「HTTPS の設定」で HTTPS を
終端し、`COOKIE_SECURE=true` のままにします。HTTPS を用意できない閉じた検証環境で試すときだけ `.env` に
`COOKIE_SECURE=false` を書きます（Cookie が平文の通信に載るので、患者データを扱う環境では使いません）。

施設の設定（`site/`）とリソース索引（`var/resource_index/`）はイメージに入りません（`.dockerignore`）。
使う場合は `docker-compose.yml` の `volumes` にあるコメントを外し、ホストのディレクトリを読み取り専用で
マウントします。コンテナ内の作業ディレクトリは `/app` なので、`SITE_DIR` / `VAR_DIR` の既定値（`site` / `var`）の
ままで `/app/site` と `/app/var/resource_index` が読まれます。索引はホストで `kawasemi-indexer` を使って作り、
作り直したらコンテナを再起動します。

```yaml
    volumes:
      - ./site:/app/site:ro
      - ./var/resource_index:/app/var/resource_index:ro
```

3. **Dockerイメージのビルドと起動**

```bash
docker-compose up -d
```

4. **データベースのスキーマ**

コンテナは起動のたびに `alembic upgrade head` を当ててから uvicorn を起動します（最新なら何もしません）。
手動で当てる場合:

```bash
docker-compose exec kawasemi alembic upgrade head
```

5. **サンプルQuestionnaireの登録（オプション）**

`examples/` はイメージに入っていないので、コンテナへ複製してから実行します:

```bash
docker-compose cp examples kawasemi:/app/examples
docker-compose exec kawasemi python examples/load_sample.py
```

6. **アクセス確認**

- Web UI: http://localhost:8000/
- API ドキュメント: http://localhost:8000/docs
- Health Check: http://localhost:8000/health

### 停止と削除

```bash
# 停止
docker-compose down

# データも含めて削除
docker-compose down -v
```

## PostgreSQLを使用する場合

`docker-compose.yml` のコメントを解除して、PostgreSQLサービスを有効化します：

```yaml
# postgres セクションのコメントを解除
# kawasemi サービスの DATABASE_URL を変更
DATABASE_URL=postgresql+asyncpg://kawasemi:password@postgres:5432/kawasemi
```

その後、マイグレーションを実行：

```bash
docker-compose up -d postgres
docker-compose exec kawasemi alembic upgrade head
```

### 読み取り専用ロール（READONLY_DATABASE_URL）

DSL の `sql_extract` と統計プロファイルの自アプリDB集計は、LLM や索引が組んだ SELECT を
アプリ本体の DB へ発行します。SELECT 限定の文字列検査は多層防御の1枚にすぎないので、
PostgreSQL では**読み取り専用ロール**を用意し、その DSN を `READONLY_DATABASE_URL` に設定してください。

```sql
-- アプリのオーナーロール（例: kawasemi）で接続して実行する。
-- ロールを作るには CREATEROLE 権限（またはスーパーユーザー）が要る。
CREATE ROLE kawasemi_ro LOGIN PASSWORD '...';
ALTER ROLE kawasemi_ro SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE kawasemi TO kawasemi_ro;
GRANT USAGE ON SCHEMA public TO kawasemi_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO kawasemi_ro;
-- 必須: 認証・権限の表は読ませない（パスワードハッシュ・リフレッシュトークンの jti・認証の記録）
REVOKE ALL ON users, user_roles, roles, refresh_tokens, auth_events FROM kawasemi_ro;
```

```bash
# .env
READONLY_DATABASE_URL=postgresql+asyncpg://kawasemi_ro:...@postgres:5432/kawasemi
```

- **`REVOKE` の行は省略しないでください。** 省くと `sql_extract` から
  `SELECT password_hash FROM users` が読めます。対象はモデルのうち認証・権限を持つ表
  （`kawasemi/models/user.py`・`role.py`・`refresh_token.py`・`auth_event.py`）で、
  `tests/test_deployment_readonly_role.py` がこの一覧とモデルの食い違いを検査します。
- **後から増えた表は自動では読めません**（上の SQL は `ALTER DEFAULT PRIVILEGES` を使いません）。
  マイグレーションで表が増え、`sql_extract` から読む必要があるときは、その表に
  `GRANT SELECT ON <表> TO kawasemi_ro;` を個別に当ててください。
  `ALTER DEFAULT PRIVILEGES ... GRANT SELECT ON TABLES` を使うと手間は減りますが、
  **認証・秘密情報を持つ表が増えたときも自動で読めるようになる**ので、そのたびに `REVOKE` が要ります。
- 書き込み・DDL・関数の作成の権限は与えません。
- **アプリDBに `dblink` / `postgres_fdw` を入れないでください。** `dblink` は別のセッションを
  開くので、読み取り専用トランザクションの外で書けます。入れる必要があるときは、
  関数の実行権限を `PUBLIC` から外してください（関数の `EXECUTE` は既定で `PUBLIC` に
  付いているので、`kawasemi_ro` から外すだけでは効きません）。
  例: `REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA <dblink を入れたスキーマ> FROM PUBLIC;`。
  外部表（`postgres_fdw`）は表として `GRANT` しない限り読めません。
  なお DSL の文字列検査も `dblink*` などを拒否しますが、これは補助の層で、境界ではありません。
- 設定すると、プログラム SQL はこの DSN の別エンジン（初回利用時に生成し、終了時に破棄）で、
  **読み取り専用トランザクション**（`BEGIN READ ONLY`）の中で実行され、毎回ロールバックされます。
  トランザクションの先頭で `statement_timeout` を掛けます（`READONLY_STATEMENT_TIMEOUT_SECONDS`、
  既定 60 秒、0 で無効）。接続をプールへ返す前に `pg_advisory_unlock_all()` を呼びます。
- **未設定のとき、`ENVIRONMENT=production` では `sql_extract` と統計プロファイルの
  自アプリDB集計を実行しません**（`sql_extract` は `source_unavailable` として記録）。
  読み取り専用トランザクションは書き込みを止めますが、`users.password_hash` のような
  読み取りは止めません。それを閉じられるのは専用ロールだけだからです。
- production 以外（development / staging）で未設定のときは、アプリ本体のロールから別接続を取り、
  読み取り専用トランザクションで実行します。**`ENVIRONMENT` を設定しないと既定は
  `development`** なので、この経路になります。本番では必ず `ENVIRONMENT=production` を
  設定してください。この経路では、実行のたびにアプリの接続プールから**2本目の接続**を
  借ります（リクエストのセッションが持つ1本とは別）。プールの大きさを見積もるときに数えてください。
- どちらの場合も、未設定であることを起動時のログと `kawasemi check_schema` が警告します。
- SQLite では `PRAGMA query_only` で閉じるので、この設定は不要です（設定しても使いません）。

### 外部DBの接続アカウント（connections.yaml）

DSL の `external_db_extract` は、接続レジストリ（`connections.yaml`）に登録した外部DB（DWH など）へ、
LLM や索引が組んだ SELECT を発行します。コードは外部DBの接続を読み取り専用にしません。
**書き込みを止める境界は、接続に使うアカウントの権限です。** SELECT 限定の文字列検査は補助の層で、
境界ではありません（`docs/dsl_specification.md` §6.3）。次を必ず守ってください。

- **接続アカウントは読み取り専用にする**。書き込み・DDL・関数やプロシージャの作成・実行の権限を与えません。
- **抽出に要る表とビューにだけ `SELECT` を与える**。読み取り専用のアカウントでも、与えられた表は
  すべて読めます（たとえば `UNION` で別の表を読み足すことは、文字列検査が止めなくても権限が止める）。
  スキーマ全体への `GRANT SELECT` は避け、患者情報以外の機微な表（認証情報など）は含めないでください。
- **DB 側で文の実行時間の上限を掛ける**。外部DBへの SQL には、アプリからは `statement_timeout` を
  掛けません。アカウントに設定してください。

PostgreSQL の例:

```sql
CREATE ROLE kawasemi_extract LOGIN PASSWORD '...';
ALTER ROLE kawasemi_extract SET default_transaction_read_only = on;
ALTER ROLE kawasemi_extract SET statement_timeout = '60s';
GRANT CONNECT ON DATABASE dwh TO kawasemi_extract;
GRANT USAGE ON SCHEMA main TO kawasemi_extract;
-- 抽出に要る表・ビューだけを列挙する（スキーマ全体には与えない）
GRANT SELECT ON main.infection_cases TO kawasemi_extract;
```

- `dblink` などの拡張が入っている外部DBでは、アプリDBと同じく関数の実行権限を `PUBLIC` から外してください
  （上の「読み取り専用ロール」を参照）。読み取り専用のトランザクションでも、別のセッションを開く関数は書けます。
- PostgreSQL 以外の DB でも同じ考え方で、読み取り専用のアカウント、必要な表だけへの参照権限、
  DB 側の実行時間の上限を設定してください。

## 本番環境へのデプロイ

### セキュリティ設定

1. **環境変数の設定**

```bash
# 必ず変更すること
SECRET_KEY=<ランダムな文字列>

# 別オリジンのフロントから API を呼ぶときだけ: CORS で許可するオリジン（スキーム付き）
# ALLOWED_HOSTS=https://example.org

# ログ設定
LOG_LEVEL=WARNING
LOG_FORMAT=json

# デバッグモードを無効化
DEBUG=false
ENVIRONMENT=production
```

2. **HTTPS の設定**

リバースプロキシ（Nginx、Caddy等）を使用してHTTPSを設定します。

Nginx設定例：

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### データのバックアップ

```bash
# SQLiteの場合（WAL モードなので .db を cp すると壊れた複製になり得る。backup API を使う）
docker-compose exec kawasemi python -c "
import sqlite3, time
src = sqlite3.connect('/app/data/kawasemi.db')
dst = sqlite3.connect(time.strftime('/app/output/backup-%Y%m%d.db'))
src.backup(dst)
"

# PostgreSQLの場合
docker-compose exec postgres pg_dump -U kawasemi kawasemi > backup-$(date +%Y%m%d).sql
```

### ログの管理

```bash
# ログの確認
docker-compose logs -f kawasemi

# ログのローテーション設定（docker-compose.yml に追加）
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

## スケーリング

### 水平スケーリング

複数インスタンスを起動する場合：

```bash
docker-compose up -d --scale kawasemi=3
```

この場合、ロードバランサー（Nginx、HAProxyなど）が必要です。

## トラブルシューティング

### ヘルスチェックが失敗する

```bash
# コンテナのログを確認
docker-compose logs kawasemi

# コンテナ内でヘルスチェックを手動実行
docker-compose exec kawasemi python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read())"
```

### データベース接続エラー

```bash
# データベースが初期化されているか確認
docker-compose exec kawasemi ls -la /app/data/

# スキーマを最新へ（未適用のマイグレーションを当てる）
docker-compose exec kawasemi alembic upgrade head

# 実スキーマとモデル宣言の食い違いを表示する
docker-compose exec kawasemi kawasemi check_schema
```

### ディスク容量不足

```bash
# 不要なDockerリソースを削除
docker system prune -a

# ボリュームの確認
docker volume ls
```
