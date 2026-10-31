# KAWASEMI Dockerfile
FROM python:3.12-slim

WORKDIR /app

# uvのインストール。uv.lock を書いた版に揃える（latest だとロックの形式が変わったときに
# --frozen の sync が通らなくなりうる）。uv を上げたら uv.lock と一緒にここも上げる。
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /bin/uv

# uv sync に要るものを先にすべて置く。プロジェクト本体のビルドには pyproject の
# readme（README.md）とパッケージのソース（kawasemi/）が、ロックファイルの解決には
# workspace メンバー（indexer/）が要る。pyproject.toml と uv.lock だけでは sync が失敗する。
# .python-version は uv にイメージの Python 3.12 を選ばせるため（CI と同じ版）。
COPY pyproject.toml uv.lock README.md alembic.ini .python-version ./
COPY indexer ./indexer
COPY kawasemi ./kawasemi
COPY alembic ./alembic
RUN uv sync --frozen --no-dev

# 実行時は .venv を直接使う。`uv run` は既定で dev グループまで同期しようとし、
# ネットワークの無い院内では起動時に失敗する。
ENV PATH="/app/.venv/bin:$PATH"

# 書き込み先（SQLite の DB・出力・索引などの生成物）。root では動かさないので、
# 書き込むディレクトリだけを実行ユーザーの持ち物にする（コードと .venv は読み取りのみ）。
# 名前付きボリュームは初回のマウント時にこの所有者を引き継ぐ。
# HOME も書ける場所にする（/app は root の持ち物で、ライブラリが ~/.cache 等を作ろうとすると失敗する）。
RUN useradd --system --uid 10001 --no-create-home --home-dir /app/home kawasemi \
    && mkdir -p /app/home /app/data /app/var /app/output/questionnaires /app/output/questionnaire_responses \
    && chown -R kawasemi:kawasemi /app/home /app/data /app/var /app/output
ENV HOME=/app/home

# DB の既定の置き場所を書き込めるディレクトリにする（既定の ./kawasemi.db は /app 直下で書けない）
ENV DATABASE_URL="sqlite+aiosqlite:////app/data/kawasemi.db"

USER kawasemi

# ポート公開
EXPOSE 8000

# ヘルスチェック（python:3.12-slim には curl が無いので Python で叩く）
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# 起動コマンド。起動のたびに alembic upgrade head を当てる（最新なら何もしない）。
# 信頼プロキシIP限定: 既定はローカルのみ。リバースプロキシ配下では
# FORWARDED_ALLOW_IPS にプロキシの実IP/CIDR を設定する（"*" は IP 詐称を許すため非推奨）。
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn kawasemi.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
