# リバースプロキシ（nginx）配下での運用

KAWASEMI は nginx などのリバースプロキシ配下で動かせる。**ルート直下**と
**サブパス配下**（例 `https://example.com/kawasemi/`）の両方に対応する。

アプリ内のリンク・フォーム・JS の fetch・リダイレクトはすべて
`ROOT_PATH` を前置するよう実装されている（テンプレートの `{{ base_path() }}`、
および Location 補正ミドルウェア）。`ROOT_PATH` が空なら従来どおりルート直下。

## 1. ルート直下に置く（最も単純）

`ROOT_PATH` は空のまま。

```nginx
server {
    listen 443 ssl;
    server_name example.com;
    # ... ssl 証明書 ...

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;   # https 判定→Cookie secure
    }
}
```

ポートは `proxy_pass` の数字を変えるだけで自由に変更できる（アプリ側は相対URLのため追従する）。

## 2. サブパス配下に置く（例: /kawasemi/）

アプリに `ROOT_PATH="/kawasemi"` を設定する（`.env` か環境変数）。

```bash
ROOT_PATH="/kawasemi"
```

nginx は末尾スラッシュ付き `proxy_pass` でプレフィックスを strip して転送する。

```nginx
location /kawasemi/ {
    proxy_pass         http://127.0.0.1:8000/;   # 末尾 / で /kawasemi を除去して転送
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_set_header   X-Forwarded-Prefix /kawasemi;  # 任意（記録用途）
}
```

`FastAPI(root_path=ROOT_PATH)` が `scope["root_path"]` を設定し、Starlette は
照合前に root_path を除去するため、**nginx がプレフィックスを strip しても
しなくてもルーティングは成立する**。strip 構成（上記）を推奨。

## 2.5 使う人ごとの入り口を分ける

同じアプリを `ENTRY_ROLES` を変えて複数起動し、URL の接頭辞（`ROOT_PATH`）で振り分ける。
コードと DB は1つで、プロセスだけ複数になる。索引（数万単位＋ベクトル）はプロセスごとに載る。
**役割と入り口は1対1**: 4役割（`admin` / `designer` / `collector` / `reviewer`）をそのまま
4つの入り口にし、`ENTRY_ROLES` には役割を1つだけ書く。

```bash
# 設計（port 8000）
ROOT_PATH="/design"  ENTRY_NAME="設計" ENTRY_ROLES="designer" \
ENTRY_URLS="collector=https://host/collect/,reviewer=https://host/review/,admin=https://host/admin/" \
uvicorn kawasemi.api.main:app --port 8000
# 収集（port 8001）
ROOT_PATH="/collect" ENTRY_NAME="収集" ENTRY_ROLES="collector" \
ENTRY_URLS="designer=https://host/design/,reviewer=https://host/review/,admin=https://host/admin/" \
uvicorn kawasemi.api.main:app --port 8001
# 検証（port 8002）
ROOT_PATH="/review"  ENTRY_NAME="検証" ENTRY_ROLES="reviewer" \
ENTRY_URLS="designer=https://host/design/,collector=https://host/collect/,admin=https://host/admin/" \
uvicorn kawasemi.api.main:app --port 8002
# 運用（port 8003）
ROOT_PATH="/admin"   ENTRY_NAME="運用" ENTRY_ROLES="admin" \
ENTRY_URLS="designer=https://host/design/,collector=https://host/collect/,reviewer=https://host/review/" \
uvicorn kawasemi.api.main:app --port 8003
```

```nginx
location /design/  { proxy_pass http://127.0.0.1:8000/; }
location /collect/ { proxy_pass http://127.0.0.1:8001/; }
location /review/  { proxy_pass http://127.0.0.1:8002/; }
location /admin/   { proxy_pass http://127.0.0.1:8003/; }
```

Cookie は同じホストで共有されるので、両方の入り口で使える役割を持つ利用者はログインが1回で済む。
その入り口で使える役割を持たない利用者のトークンは未認証と同じ扱いになる。別の入り口で切り替えた役割は、
その入り口で使えなければ既定の役割に落ちる（`docs/roles_and_permissions.md`「入り口」）。

## 3. HTTPS 終端時の注意

- nginx で HTTPS 終端し、アプリは HTTP で動く構成が一般的。
- `X-Forwarded-Proto` を送り、uvicorn は `--proxy-headers` 付きで起動する。
  信頼するプロキシの IP/CIDR を **`FORWARDED_ALLOW_IPS`** 環境変数で指定する
  （既定 `127.0.0.1`。Dockerfile / `kawasemi serve` とも対応済み）。
  `*` は直接到達可能な環境で `X-Forwarded-For` 詐称（レート制限回避・監査ログ汚染）を
  許すため本番非推奨。nginx とアプリが別ホスト/別コンテナの構成では、必ず
  `FORWARDED_ALLOW_IPS` にプロキシの実 IP を設定すること（未設定だと scheme 判定や
  クライアント IP が正しく扱われない）。ログイン失敗のスロットルも (ユーザー名, クライアント IP)
  で数えるので、これが無いと全員がプロキシの IP になり、一人の失敗が全員のブロックになる。
- **リクエストボディの上限はプロキシ側で設定する**（nginx なら `client_max_body_size`、既定 1m）。
  アプリはボディの大きさに上限を持たない。編集中のプランや回答など、認証済みの利用者が送る JSON は、
  通常は数十 KB 以内である。大きなボディを受け付けると、その解析にワーカーの時間を使う。
- クライアント IP は、IPv6 なら **/64 にまとめて**数える（ログインのレート制限と失敗のスロットル）。
  同じサブネットの別アドレスを使っても、数え直しにならない。
- 認証 Cookie / CSRF Cookie の `secure` 属性は `ENVIRONMENT=production` で有効化される。
  本番では `ENVIRONMENT="production"` を設定すること。
- `ALLOWED_HOSTS` は CORS で許可するオリジン（`allow_origins`）で、ホスト名によるアクセス制限
  ではない。別オリジンのフロントから API を呼ぶときだけ、スキーム付きで書く
  （例 `https://example.org`）。同一オリジンのアクセスでは CORS は発動しないので設定不要。

## 4. 動作確認

サブパス挙動の回帰テスト: `tests/test_subpath.py`
（ルーティング成立・リンクのプレフィックス・リダイレクト Location 補正・後方互換）。
