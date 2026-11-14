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
アプリ本体の DB へ発行します。SELECT 限定の文字列検査は補助の層で境界ではないので、
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
- SQLite では `PRAGMA query_only`（書き込み）と接続の authorizer（アプリ自身の表の読み取り）で
  閉じるので、この設定は不要です（設定しても使いません）。authorizer は `users` などアプリ自身の表
  （`Base.metadata` の全表）と SQLite のスキーマ表を読む文を、DB が準備の時点で拒否します。
  SQLite で `sql_extract` が読めるのは、アプリの表ではない臨床データの表だけです。
- PostgreSQL では上のロールの権限が境界ですが、`sql_extract` は実行の前に `EXPLAIN (VERBOSE, FORMAT JSON)` で
  **計画に出る実表**を調べ、アプリ自身の表・`pg_catalog`・`information_schema` を読む文を
  実行しません（ビューは基表まで展開されます）。`pg_stat_activity`・`pg_settings`・`pg_locks` のような
  カタログの関数で作るビューや、`pg_get_viewdef(...)`・`pg_relation_size(...)` のような `pg_*` 関数の呼び出しも、
  計画の関数ノード（名前が `pg_` で始まるもの）と出力式で見つけて実行しません。`generate_series`・`unnest`・
  `jsonb_array_elements` のような組み込みの集合を返す関数は拒否しません。計画の文字列の走査はリテラルの中身も
  見るので、`'pg_x('` を含む定数は誤って拒否します（安全側）。これは追加の層で、ロールの権限の代わりではありません。
  **名前が `pg_` で始まらないカタログ関数は、この計画の検査では拒否しません**: `version()`・`has_table_privilege`・`to_regclass`・`inet_server_addr`・`txid_current` のような関数です（設定の開示などは防げません。境界はロールの権限です）。`set_config`・`current_setting`・`pg_notify` は、SELECT 限定の検査の禁止語（字句の層）が拒否しますが、これは多層の一つで境界ではありません（下の RLS の注意を参照）。
  **表を読む関数**（`query_to_xml`・`table_to_xml`・`database_to_xml` は `pg_catalog` の組み込み関数で、
  ユーザー定義ではありません。`dblink` は拡張です）は計画に実表として出ないので、この検査では捕まえません。
  これらは SELECT 限定の検査の禁止語の一覧（補助の層）に頼ります。拒否は**名前**で決まります: `users`・`roles`・`datasets`・`subjects` など
  アプリの表と同じ名前の表は、どのスキーマにあっても読めません（臨床データの表にそれらの名前を付けないでください）。そちらは SELECT 限定の検査の禁止語の一覧
  （補助の層）と、上のロールの権限（境界）に頼ります。PostgreSQL・SQLite 以外の DB は表を検査
  できないので、プログラム SQL を実行しません。
- **SQL を認可された患者に絞ることは、運用者がデータベース側で行う責任です。アプリは保証しません。**
  `sql_extract` と `external_db_extract` の SQL は、`:patient_id` で絞る形を要求しません。
  設計者が保存した SQL が `WHERE` を省けば、認可を通った1人の実行でも、そのアカウントが読める
  表の全患者の行を返します。アプリは SQL の構造を読んで患者の範囲を保証しません
  （結合・導出表・定数の作り方でいくつもすり抜けられ、閉じ切れないため）。したがって、
  臨床データを持つデータベースでは、次を運用者が整えてください。
  - (a) **最小権限の読み取り専用アカウント**: 計画が読む表・ビューにだけ `GRANT SELECT` を与える。
    アプリ自身の表（認証・回答・実行記録）を読めないのは、**`sql_extract`（アプリ DB）の経路だけ**です
    （上の F8/F9）。`external_db_extract`（外部 DB）は表を検査しないので、外部 DB の表の範囲は
    接続アカウントの `GRANT` だけが決めます。アプリ DB の臨床データの表も、
    `READONLY_DATABASE_URL` のロールの `GRANT` が決めます
  - (b) **患者ごとに絞るビュー、または行レベルセキュリティ（RLS）**: 参照の入口を、
    現在の患者の行だけが見える形にする。PostgreSQL の一例を下に示す。
    **アプリは、患者の設定を入れる仕組みを提供しません。** 設定する経路は運用者が用意します。
    **計画の SQL が書き換えられる設定は境界ではありません**（下の例の注意）
  - (c) **運用**: 設計者が自分の SQL 計画を確定でき、`TRIAL_RUN_PATIENT_IDS` の患者への試行実行もできます。
    実患者を含むデータベースに対しては、設計者に確定と試行を任せない運用（確定は別の人、試行は
    検証用のデータだけ）にしてください

  PostgreSQL の RLS の例です（**そのまま使わず、自分のスキーマと接続の経路に合わせて書き直してください**）。

  > **注意（実機 PostgreSQL 17 で確認）**: 方針が `current_setting('app.patient_id', true)` を信頼するとき、
  > その設定を**計画の SQL が書ける**と、境界になりません。`set_config` は SELECT から呼べるので、
  > 読み取りロールが `SELECT set_config('app.patient_id', '他の患者', true)` を実行すれば、
  > 同じトランザクションで他の患者の行が読めます。アプリが SQL の禁止語で `set_config` を拒否するのは
  > 多層の一つで、字句の検査は境界ではありません。設定は、**計画の SQL が書けない経路**
  > （接続を受け持つ信頼できる層）で入れ、読み取りロールからは `set_config` の実行権を外してください。
  > アプリはこの設定を入れません。下の例は、その経路があるときに成り立つ形です。

  ```sql
  -- 例: lab_results(patient_id, result_value, ...) を、現在の患者の行だけに絞る
  ALTER TABLE lab_results ENABLE ROW LEVEL SECURITY;
  ALTER TABLE lab_results FORCE ROW LEVEL SECURITY;  -- 表の所有者にも効かせる
  CREATE POLICY patient_scope ON lab_results
    FOR SELECT
    USING (patient_id = current_setting('app.patient_id', true));
  -- 計画の SQL を実行するロール（表の所有者や BYPASSRLS を持つロールは使わない）
  CREATE ROLE plan_reader NOBYPASSRLS;
  GRANT SELECT ON lab_results TO plan_reader;
  -- plan_reader から set_config の実行権を外す。設定を入れる側（信頼できる層）のロールだけに与える
  REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC;
  CREATE ROLE conn_layer LOGIN;                       -- 信頼できる層が使う接続ロール
  GRANT plan_reader TO conn_layer;
  GRANT EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) TO conn_layer;
  -- 信頼できる層が、計画の SQL を流す直前に、同じトランザクションで行う:
  --   BEGIN;
  --   SELECT set_config('app.patient_id', '<認可された患者の ID>', true);
  --   SET LOCAL ROLE plan_reader;   -- 以降の文は plan_reader として動く（設定が無ければ 0 行）
  --   ... 計画の SELECT ...
  --   COMMIT;
  ```

  `PUBLIC` からの `REVOKE` は、他のロール（アプリ本体や監視など）の `set_config` も止めます。
  影響を確かめてから行い、PostgreSQL の更新のあとに権限が残っていることを確かめてください。
  この形は、SELECT だけの SQL が `RESET ROLE` や `SET` を実行できないこと（アプリの検査）と、
  `set_config` 以外に設定を書く関数を入れていないこと（拡張に注意）に依存します。
- **ファイルと FHIR の患者の範囲は、アプリが確かめます。** 次の形でなければ実行しません（`path_not_permitted`）。
  - ファイル: `path_pattern` のパス区間の1つが `{{patient_id}}`（`{{patient_id}}.txt` は可、
    `{{a}}{{patient_id}}` は不可）
  - FHIR: `patient=` / `subject=`（小文字のとおり）の値が `{{patient_id}}`、または `/Patient/{{patient_id}}`
    で始まるパス。符号化した `.` `/`（`%2e` など）、`..` の区間、`_include` / `_revinclude` /
    `_has` / `_filter` / `_query` を含むクエリ、`#`・空白・制御文字（`#` 以降は HTTP クライアントが送らず、
    絞りの無い要求になる）、パスの `%`、検索パラメータ名の `%` や空白（符号化・
    前後の空白で別の名前にする形）を含むクエリは実行しません
  - 患者キーに使えるのは組み込みの `{{patient_id}}` だけです（前段 step の出力から決まる
    パラメータは患者キーの代わりになりません）。`csv_extract` は従来どおり、実行時に key_column の等値を強制します
  - パラメータの値の中の `;` や `%` は拒否しません（サーバーはパラメータの区切りとして扱いません）
  - プランに書かれた `unit_id` は、どの op でも免除の根拠にしません
  - **挙動の変更:** 患者キーを使わないパス・FHIR クエリ（全患者共通のファイル、`patient=` の無い検索など）は
    実行されなくなります

### 外部DBの接続アカウント（connections.yaml）

DSL の `external_db_extract` は、接続レジストリ（`connections.yaml`）に登録した外部DB（DWH など）へ、
LLM や索引が組んだ SELECT を発行します。アプリは、使える接続を `kind: db` かつ `scope: external` の
ものに限り（fhir・file・自アプリ DB の接続は拒否します）、抽出を**読み取り専用トランザクション**の中で
実行し（PostgreSQL は `READ ONLY` と `statement_timeout`〔`READONLY_STATEMENT_TIMEOUT_SECONDS`〕、SQLite は
`query_only`、ほかの方言は `SET TRANSACTION READ ONLY`。通らない方言は失敗します）、終了時に必ずロールバックします。
これは追加の層です。**書き込みを止める境界は、接続に使うアカウントの権限です。** SELECT 限定の文字列検査も
補助の層で、境界ではありません（`docs/dsl_specification.md` §6.3）。また、**どの患者の行を返すかは
アプリが保証しません**（上の「SQL を認可された患者に絞ることは…」）。次を必ず守ってください。

- **`connections.yaml` の DB 接続には `kind: db` と `scope: external` の両方を書く**。`kind` を省いた
  `scope: external` の接続は、`external_db_extract` に使えません。起動（レジストリの読み込み）時に
  接続名つきで警告を出し、コンパイルは `external_db_extract` を作らず（不足として人の補完に回す）、
  実行時にも `path_not_permitted` で断ります。
- **読み取り専用の違反と文タイムアウトによる取り消しは、投げ直しません**（待っても通らないため）。
  接続断・デッドロックのような一時的なエラーだけを、10 秒待って最大 3 回投げ直します。
- **接続アカウントは読み取り専用にする**。書き込み・DDL・関数やプロシージャの作成・実行の権限を与えません。
- **抽出に要る表とビューにだけ `SELECT` を与える**。読み取り専用のアカウントでも、与えられた表は
  すべて読めます（たとえば `UNION` で別の表を読み足すことは、文字列検査が止めなくても権限が止める）。
  スキーマ全体への `GRANT SELECT` は避け、患者情報以外の機微な表（認証情報など）は含めないでください。
- **DB 側で文の実行時間の上限も掛ける**。アプリが掛ける上限は PostgreSQL だけです。アカウントにも
  設定してください（アプリの上限とは独立の、境界側の設定です）。

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

- PostgreSQL は既定で、ほとんどの関数の `EXECUTE` を `PUBLIC` に与えています。アカウントから外すだけでは効かないので、
  `dblink` などの拡張が入っている外部DBでは、アプリDBと同じく関数の実行権限を `PUBLIC` から外してください
  （上の「読み取り専用ロール」を参照）。読み取り専用のトランザクションでも、別のセッションを開く関数は書けます。
- PostgreSQL 14 以前では、既定で `PUBLIC` が `public` スキーマに `CREATE` を持ちます。関数や表を作らせないよう
  `REVOKE CREATE ON SCHEMA public FROM PUBLIC;` を当ててください（15 以降は既定で外れています）。
  これで「関数の作成・実行の権限を与えない」が満たせます。
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
