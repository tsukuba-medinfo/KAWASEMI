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
