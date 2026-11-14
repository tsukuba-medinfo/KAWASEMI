"""KAWASEMI CLI コマンド"""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kawasemi.core.auth import get_password_hash
from kawasemi.core.config import settings
from kawasemi.core.password_policy import password_policy_error
from kawasemi.models import Base, Role, RoleType, User, UserRole

if TYPE_CHECKING:
    # 実行時は各コマンドの中で遅延 import する（CLI の起動を軽くするため）。
    # 型注釈だけがモジュールスコープで名前を必要とする。
    import subprocess


async def get_async_session() -> AsyncSession:
    """非同期セッションを取得"""
    engine = create_async_engine(settings.database_url, echo=False, hide_parameters=True)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return async_session()


async def init_roles(session: AsyncSession) -> dict[str, Role]:
    """ロールを初期化する"""
    roles = {}
    for role_name in RoleType.ALL:
        result = await session.execute(select(Role).where(Role.name == role_name))
        role = result.scalar_one_or_none()

        if not role:
            role = Role(
                name=role_name,
                description=RoleType.DESCRIPTIONS.get(role_name, ""),
            )
            session.add(role)
            print(f"  ロール '{role_name}' を作成しました")
        else:
            print(f"  ロール '{role_name}' は既に存在します")

        roles[role_name] = role

    await session.commit()
    return roles


async def create_admin_user(
    username: str,
    email: str,
    password: str,
    full_name: str,
) -> bool:
    """管理者ユーザーを作成する。作成できたら True、拒否したら False（呼び出し側が終了コードにする）。"""
    policy_error = password_policy_error(password, username=username)
    if policy_error:
        print(f"エラー: {policy_error}")
        return False

    session = await get_async_session()

    try:
        # ロールの初期化
        print("ロールを初期化しています...")
        roles = await init_roles(session)

        # 既存ユーザーのチェック
        result = await session.execute(select(User).where(User.username == username))
        existing_user = result.scalar_one_or_none()

        if existing_user:
            print(f"エラー: ユーザー名 '{username}' は既に使用されています")
            return False

        result = await session.execute(select(User).where(User.email == email))
        existing_email = result.scalar_one_or_none()

        if existing_email:
            print(f"エラー: メールアドレス '{email}' は既に使用されています")
            return False

        # ユーザー作成
        password_hash = get_password_hash(password)
        user = User(
            username=username,
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            is_active=True,
        )
        session.add(user)
        await session.flush()

        # 管理者ロールを付与
        admin_role = roles[RoleType.ADMIN]
        user_role = UserRole(
            user_id=user.id,
            role_id=admin_role.id,
        )
        session.add(user_role)

        await session.commit()
        print("\n管理者ユーザーを作成しました:")
        print(f"  ユーザー名: {username}")
        print(f"  メール: {email}")
        print(f"  氏名: {full_name}")
        print("  ロール: admin")
        return True

    except Exception as e:
        await session.rollback()
        print(f"エラーが発生しました: {e}")
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """データベースを初期化する（`alembic upgrade head` ＋ロール作成）"""
    from kawasemi.core.database import init_db as _upgrade_to_head

    await _upgrade_to_head()

    print("データベーステーブルを作成しました（alembic upgrade head）")

    # ロールの初期化
    session = await get_async_session()
    try:
        print("\nロールを初期化しています...")
        await init_roles(session)
    finally:
        await session.close()


async def list_users() -> None:
    """ユーザー一覧を表示"""
    session = await get_async_session()

    try:
        result = await session.execute(select(User).order_by(User.id))
        users = result.scalars().all()

        if not users:
            print("ユーザーが登録されていません")
            return

        print(f"\n登録ユーザー一覧 ({len(users)}件):")
        print("-" * 60)
        for user in users:
            # ロールを取得
            role_result = await session.execute(
                select(Role)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user.id)
            )
            roles = role_result.scalars().all()
            role_names = ", ".join([r.name for r in roles]) or "なし"

            status = "有効" if user.is_active else "無効"
            print(f"  ID: {user.id} | {user.username} ({user.full_name})")
            print(f"       メール: {user.email} | 状態: {status} | ロール: {role_names}")
    finally:
        await session.close()


async def check_schema() -> None:
    """実スキーマとモデル宣言の食い違いを表示する。

    **CI では発火しない種類の欠陥**を見る。移行を当て損ねた既存 DB でしか起きないので、
    運用側から明示的に走らせる口が要る。食い違いがあれば終了コード 1 を返す。
    """
    from kawasemi.core.database import engine
    from kawasemi.core.readonly_db import readonly_route_warning
    from kawasemi.core.schema_check import inspect_schema_async

    drift = await inspect_schema_async(engine)
    from sqlalchemy.engine import make_url

    # パスワードは表示しない
    print(f"接続先: {make_url(str(settings.database_url)).render_as_string(hide_password=True)}")
    # 読み取り専用ロールの欠けは警告に留める（終了コードは変えない。スキーマの食い違いではない）
    readonly_warning = readonly_route_warning(engine.dialect.name)
    if readonly_warning:
        print(f"警告: {readonly_warning}")
    print(f"モデルの表: {len(Base.metadata.tables)}")
    if drift.recorded_revision or drift.head_revision:
        print(f"alembic_version: {drift.recorded_revision} / head: {drift.head_revision}")
    if not drift:
        print("実スキーマはモデル宣言と一致しています")
        return
    print("")
    print("食い違いがあります:")
    for line in drift.messages():
        print(f"  - {line}")
    print("")
    print(
        "`alembic upgrade head` では直らないことがあります"
        "（alembic は当該版を適用済みと信じて何もしないため）。"
        "欠けている列の DDL を手で当てるか、DB を作り直してください。"
    )
    sys.exit(1)


def search_resource_index(
    query: str,
    annotation: str | None,
    engine: str,
    top_k: int,
    min_score: float | None,
    model: str | None,
    index_dir: str | None,
    corpus_dir: str | None,
    as_json: bool,
    use_hints: bool = True,
    hint_terms: list[str] | None = None,
    hint_types: list[str] | None = None,
    hint_model: str | None = None,
) -> None:
    """リソース索引を1クエリで検索しデバッグ表示する（`kawasemi index search`）。

    本番の「データ所在AI案」と同じ検索経路を再現する。索引が検索可能なら embedding、
    不可なら DB定義書 BM25 へフォールバックする（--engine で経路を強制可能）。

    **取得は本番と同じ `resource_drafter.retrieve_hits` を通る**。
    検索ヒントも既定で本番同様に LLM が生成する。
    """
    from kawasemi.ai_input.resource_index.search_cli import (
        format_json,
        format_text,
        run_search,
    )

    result = run_search(
        query,
        annotation=annotation,
        engine=engine,
        top_k=top_k,
        min_score=min_score,
        model=model,
        index_dir=index_dir,
        corpus_dir=corpus_dir,
        use_hints=use_hints,
        hint_terms=hint_terms,
        hint_types=hint_types,
        hint_model=hint_model,
    )
    print(format_json(result) if as_json else format_text(result))


def e2e_setup(build_index: bool = False, embedder: str = "hashing") -> None:
    """E2E 試験環境のダミーデータ一式を生成する（`kawasemi e2e setup`）。

    **アプリDBには触らない**（破壊的な初期化をしない）。生成するのは外部システム
    側の生成物（`var/e2e/`）と、同じ乱数から作る正解 `examples/e2e/gold.jsonl`、
    実行時に読む `connections.yaml` / `key_types.yaml` の複製である。索引の構築
    （`ingest → build → 項目単位の追記`）と様式・利用者の投入は E2E の試験が行う。
    """
    import shutil

    from kawasemi.ai_input import e2e
    from kawasemi.ai_input.e2e import sources

    print("=" * 60)
    print("E2E 試験環境のセットアップ（架空の感染症様式・10 名）")
    print("=" * 60)

    print(f"\n[1/2] ダミー外部システムを生成中（{e2e.GENERATED_DIR}/）...")
    sources.generate_all()

    print("\n[2/2] connections.yaml / key_types.yaml を配置中（e2e 専用パス）...")
    e2e.CONNECTIONS_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(e2e.CONNECTIONS_E2E, e2e.CONNECTIONS_RUNTIME)
    print(f"  {e2e.CONNECTIONS_E2E} → {e2e.CONNECTIONS_RUNTIME}")
    shutil.copyfile(e2e.KEY_TYPES_E2E, e2e.KEY_TYPES_RUNTIME)
    print(f"  {e2e.KEY_TYPES_E2E} → {e2e.KEY_TYPES_RUNTIME}")

    if build_index:
        from kawasemi.ai_input.e2e.index import (
            RESOURCE_INDEX_DIR,
            build_e2e_index,
            make_embedder,
        )

        print(f"\n[索引] 索引を構築中（embedder={embedder}、{RESOURCE_INDEX_DIR}/）...")
        result = build_e2e_index(
            RESOURCE_INDEX_DIR,
            make_embedder(embedder, settings.embedding_model),
            work_dir=e2e.GENERATED_DIR / "index_work",
        )
        print(
            f"  {result.unit_count}単位（項目単位 {len(result.item_units)}件: "
            f"{', '.join(result.item_units)}）"
        )
        for note in result.notes:
            print(f"  注: {note}")
        print(f"  使うとき: RESOURCE_INDEX_DIR={RESOURCE_INDEX_DIR}")

    print("\n" + "=" * 60)
    print("セットアップ完了。")
    print("=" * 60)
    print(f"  患者          : {len(e2e.SUBJECTS)}名 → {', '.join(s.reg_no for s in e2e.SUBJECTS)}")
    print(f"  DWH           : {e2e.WAREHOUSE_DB}（DWH_DSN={e2e.DWH_DSN}）")
    print(f"  医事会計      : {e2e.BILLING_DB}（BILLING_DSN={e2e.BILLING_DSN}）")
    print(f"  レポート/FHIR/CSV: {e2e.REPORTS_DIR} / {e2e.FHIR_STUB_DIR} / {e2e.DPC_EF_CSV}")
    print(f"  正解          : {e2e.GOLD_JSONL}")
    print(
        "  索引の入力    : kawasemi-indexer ingest examples/e2e/knowledge.yaml "
        f"--dsn {e2e.DWH_DSN} --out <出力先>"
    )


def _reset_e2e_passwords(db_path: Path) -> str:
    """E2E 実行の app.db にいる利用者のパスワードを `demo_password()` に揃えて返す。

    実機層は実行のたびに使い捨てのパスワードで利用者を作るので、閲覧のときに知らせる値を
    ここで決め直す（パスワードをファイルに残さない）。
    """
    import sqlite3

    from kawasemi.ai_input.e2e.seed import seed_users
    from kawasemi.core.demo_secrets import demo_password

    password = demo_password()
    hashed = get_password_hash(password)
    with sqlite3.connect(db_path) as con:
        # この版より前に作られた実行結果の app.db には token_version 列が無い（その場合は世代を
        # 上げる対象が無いので、パスワードだけ入れ替える）
        user_cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
        has_generation = "token_version" in user_cols
        has_refresh = bool(
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='refresh_tokens'"
            ).fetchone()
        )
        for username in seed_users():
            # パスワードを変えるので、発行済みのトークンも無効にする（API の再設定と同じ。
            # `core/sessions.py: revoke_user_sessions` の SQL 版。世代 +1 とリフレッシュ全件失効）
            bump = ", token_version=token_version+1" if has_generation else ""
            con.execute(
                f"UPDATE users SET password_hash=?{bump} WHERE username=?",
                (hashed, username),
            )
            if has_refresh:
                con.execute(
                    "UPDATE refresh_tokens SET revoked=1 "
                    "WHERE user_id IN (SELECT id FROM users WHERE username=?)",
                    (username,),
                )
    return password


def e2e_serve(host: str, port: int, run_dir: str | None = None) -> None:
    """E2E 実機層の1回分をブラウザで見られるように起動する（`kawasemi e2e serve`）。

    `make e2e-live` が `var/e2e/live_runs/<時刻>/` に残したアプリDB・索引・
    生成物へ実行系を向け、FHIR スタブ（:8084）とアプリ（既定 127.0.0.1:8030）を起動する。
    設計済みのプラン・手順書・回答をそのまま見られ、AI 案の再生成や autofill も同じ資産で
    やり直せる（LLM は `OLLAMA_*` の設定を使う）。
    """
    from kawasemi.ai_input import e2e
    from kawasemi.ai_input.e2e.pipeline import connections_yaml, latest_live_run
    from kawasemi.ai_input.e2e.seed import seed_users

    run = Path(run_dir) if run_dir else latest_live_run()
    if run is None or not (run / "app.db").exists():
        print(
            "E2E の実行結果が見つかりません。先に `make e2e-live` を実行するか、"
            "`--run-dir` に app.db のあるディレクトリを渡してください。"
        )
        raise SystemExit(1)
    run = run.resolve()
    generated = run / "generated"
    fhir_url = f"http://localhost:{e2e.FHIR_STUB_PORT}/fhir"
    conn_file = connections_yaml(run / "connections.serve.yaml", generated, fhir_base=fhir_url)
    password = _reset_e2e_passwords(run / "app.db")

    print("=" * 60)
    print(f"E2E 実行結果を表示: {run}")
    print("=" * 60)
    print(f"  ログイン: {', '.join(seed_users())} / パスワード: {password}")
    print(f"  様式一覧: http://{host}:{port}/ai-input/questionnaires")
    _serve(
        label="（E2E）",
        env_extra={
            "DATABASE_URL": f"sqlite+aiosqlite:///{run / 'app.db'}",
            "ENABLE_AI_DRAFTING": "true",
            "ENABLE_AI_EXTRACTION": "true",
            "LLM_PROVIDER": "ollama",
            "RESOURCE_INDEX_DIR": str(run / "resource_index"),
            "CONNECTIONS_FILE": str(conn_file),
            "KEY_TYPES_FILE": str(e2e.KEY_TYPES_E2E.resolve()),
            "REFERENCE_SCOPES_FILE": str(e2e.REFERENCE_SCOPES.resolve()),
            "DWH_DSN": f"sqlite+aiosqlite:///{generated / e2e.WAREHOUSE_DB.name}",
            "BILLING_DSN": f"sqlite+aiosqlite:///{generated / e2e.BILLING_DB.name}",
            "FHIR_SERVER_URL": fhir_url,
            "FHIR_STUB_DIR": str(generated / e2e.FHIR_STUB_DIR.name),
            "DEMO_PASSWORD": password,
            # 入り口の制限は外す。.env が入り口を絞っていても e2e の利用者が入れる
            # ように（run_pipeline と同じ）
            "ENTRY_NAME": "",
            "ENTRY_ROLES": "",
            "ENTRY_URLS": "",
        },
        fhir_stub_port=e2e.FHIR_STUB_PORT,
        fhir_stub_dir=str(generated / e2e.FHIR_STUB_DIR.name),
        host=host,
        port=port,
    )


def _serve(
    *,
    label: str,
    env_extra: dict[str, str],
    fhir_stub_port: int,
    fhir_stub_dir: str | None,
    host: str,
    port: int,
    ollama_base_url: str | None = None,
    ollama_model: str | None = None,
    ollama_vision_model: str | None = None,
) -> None:
    """FHIRスタブとアプリを起動する。

    実行系をどのダミーへ向けるかは `env_extra` と FHIR スタブのポート・ディレクトリで渡す。
    """
    import os
    import subprocess

    env = os.environ.copy()
    env.update(env_extra)
    _apply_ollama_overrides(
        env,
        base_url=ollama_base_url,
        model=ollama_model,
        vision_model=ollama_vision_model,
    )

    where = f"（{fhir_stub_dir}）" if fhir_stub_dir else ""
    print(f"FHIRスタブを起動: http://localhost:{fhir_stub_port}/fhir{where}")
    fhir_proc = _start_fhir_stub(fhir_stub_port, env)
    try:
        print(f"アプリを起動: http://{host}:{port}{label}")
        subprocess.run(
            ["uvicorn", "kawasemi.api.main:app", "--host", host, "--port", str(port)],
            env=env,
        )
    finally:
        _stop_fhir_stub(fhir_proc)


def _apply_ollama_overrides(
    env: dict[str, str],
    *,
    base_url: str | None = None,
    model: str | None = None,
    vision_model: str | None = None,
) -> None:
    """Ollama の接続先・モデルを環境変数へ上書きする（指定があるものだけ）。

    アプリ/executor は settings.ollama_base_url / ollama_model / ollama_vision_model
    （= 環境変数 OLLAMA_BASE_URL / OLLAMA_MODEL / OLLAMA_VISION_MODEL）を参照する。
    未指定（None）の項目はホスト環境変数の値（あれば）をそのまま使う。
    """
    if base_url:
        env["OLLAMA_BASE_URL"] = base_url
    if model:
        env["OLLAMA_MODEL"] = model
    if vision_model:
        env["OLLAMA_VISION_MODEL"] = vision_model
    embedding = env.get("EMBEDDING_BASE_URL", settings.embedding_base_url).strip()
    print(
        "  Ollama: "
        f"{env.get('OLLAMA_BASE_URL', settings.ollama_base_url)} / "
        f"model={env.get('OLLAMA_MODEL', settings.ollama_model)}"
        + (f" / vision={env['OLLAMA_VISION_MODEL']}" if env.get("OLLAMA_VISION_MODEL") else "")
        + (f" / embedding={embedding}" if embedding else "")
    )


def _stop_fhir_stub(proc: "subprocess.Popen[bytes]") -> None:
    """FHIR スタブを停止する。

    `terminate` に応じないときだけ `kill` へ落とす。
    """
    import subprocess

    print("\nFHIRスタブを停止します...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _start_fhir_stub(port: int, env: dict[str, str]) -> "subprocess.Popen[bytes]":
    """FHIR スタブを別プロセスで起動する。

    `--host 127.0.0.1` を明示するのは、スタブが**サーバ間通信専用**で外部へ
    公開しないためである。uvicorn の既定と同じ値なので消しても
    挙動は変わらず、消えたことに気づけない。

    アプリ側（`kawasemi.api.main:app`）は運用者が `--host` で指定した値を使う。
    両者を混同してはいけない。
    """
    import subprocess

    return subprocess.Popen(
        [
            "uvicorn",
            "kawasemi.ai_input.e2e.fhir_stub:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
    )


def _add_ollama_args(parser: argparse.ArgumentParser) -> None:
    """serve サブコマンドへ Ollama 接続先・モデルの上書きオプションを登録する。

    未指定なら環境変数（OLLAMA_BASE_URL / OLLAMA_MODEL / OLLAMA_VISION_MODEL）、
    それも無ければ設定の既定値を使う。
    """
    parser.add_argument(
        "--ollama-base-url",
        default=None,
        help=f"OllamaサーバURL（例: http://198.51.100.10:11434。既定: {settings.ollama_base_url}）",
    )
    parser.add_argument(
        "--ollama-model",
        default=None,
        help=f"使用するOllamaモデル名（既定: {settings.ollama_model}）",
    )
    parser.add_argument(
        "--ollama-vision-model",
        default=None,
        help="手書き/画像PDF用のマルチモーダルモデル名（既定: ollama-model を流用）",
    )


def main() -> None:
    """メインエントリーポイント"""
    parser = argparse.ArgumentParser(description="KAWASEMI CLI")
    subparsers = parser.add_subparsers(dest="command", help="コマンド")

    # create_admin コマンド
    admin_parser = subparsers.add_parser("create_admin", help="管理者ユーザーを作成")
    admin_parser.add_argument("--username", "-u", required=True, help="ユーザー名")
    admin_parser.add_argument("--email", "-e", required=True, help="メールアドレス")
    admin_parser.add_argument("--name", "-n", required=True, help="氏名")
    admin_parser.add_argument("--password", "-p", help="パスワード（省略時は対話入力）")

    # init_db コマンド
    subparsers.add_parser("init_db", help="データベースを初期化")

    # list_users コマンド
    subparsers.add_parser("list_users", help="ユーザー一覧を表示")

    # init_demo コマンド
    subparsers.add_parser("init_demo", help="デモデータを初期化（開発・テスト用）")

    # check_schema コマンド。CI では発火しない食い違いを運用側から見る
    subparsers.add_parser(
        "check_schema", help="実スキーマとモデル宣言の食い違いを検査（既存DBの移行漏れ）"
    )

    # index コマンド（索引の点検）。索引の生成・取り込み・key_type 下書きは
    # indexer（`indexer/`。SE 向けバックエンドツール）が担う。
    # ここに置くのは成果物を読む点検系のみ（search）。生成は `kawasemi-indexer build` 等。
    index_parser = subparsers.add_parser(
        "index", help="リソース索引の点検（生成は kawasemi-indexer）"
    )
    index_sub = index_parser.add_subparsers(dest="index_command", help="索引サブコマンド")

    # index search サブコマンド（検索の単体チューニング／デバッグ）
    index_search = index_sub.add_parser(
        "search", help="1クエリで索引を検索し、ヒット・スコア・経路を表示（デバッグ）"
    )
    index_search.add_argument("query", help="検索クエリ（項目名など。本番同様 annotation と連結）")
    index_search.add_argument(
        "--annotation", default=None, help="臨床的コンテキスト（クエリへ連結する任意文）"
    )
    index_search.add_argument(
        "--engine",
        choices=["auto", "embedding", "bm25"],
        default="auto",
        help="検索経路。auto=本番同様（searchableならembedding、不可ならBM25）",
    )
    index_search.add_argument("--top-k", type=int, default=8, help="表示する上位件数")
    # 検索ヒント。既定は本番と同じく LLM に生成させる。
    index_search.add_argument(
        "--no-hints",
        action="store_true",
        default=False,
        help="検索ヒントを生成しない（拡張も型加点も無い素の検索）",
    )
    index_search.add_argument(
        "--hint-term",
        action="append",
        default=None,
        help="院内での呼ばれ方を明示（複数可）。指定すると LLM を呼ばない",
    )
    index_search.add_argument(
        "--hint-type",
        action="append",
        default=None,
        help="想定型を明示（複数可）。指定すると LLM を呼ばない",
    )
    index_search.add_argument(
        "--hint-model",
        default=None,
        help="ヒント生成に使うモデル（既定: 設計ロールのモデル）",
    )
    index_search.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="cosine足切り（embedding時。無指定で設定値）",
    )
    index_search.add_argument(
        "--model", default=None, help="クエリ側embeddingモデル名（無指定で設定値）"
    )
    index_search.add_argument(
        "--index-dir", default=None, help="索引ディレクトリ（無指定で設定値）"
    )
    index_search.add_argument(
        "--corpus-dir",
        default=None,
        help="BM25フォールバック用DB定義書ディレクトリ（無指定で設定値）",
    )
    index_search.add_argument("--json", action="store_true", help="構造化JSONで出力")

    # e2e コマンド（E2E 試験環境。架空の感染症様式・ダミーデータ生成）
    e2e_parser = subparsers.add_parser(
        "e2e", help="E2E 試験環境（架空の感染症様式）のダミーデータ生成／実行結果の表示"
    )
    e2e_sub = e2e_parser.add_subparsers(dest="e2e_command", help="e2e サブコマンド")
    e2e_setup_parser = e2e_sub.add_parser(
        "setup", help="ダミー外部システムと正解(gold.jsonl)を生成（アプリDBには触らない）"
    )
    e2e_setup_parser.add_argument(
        "--build-index",
        action="store_true",
        help="索引も作る（ingest → 連結 → build → 項目単位の追記）",
    )
    e2e_setup_parser.add_argument(
        "--embedder",
        choices=("hashing", "ollama"),
        default="hashing",
        help="索引の embedder（hashing=決定的・Ollama 不要 / ollama=実機の embedding）",
    )
    e2e_serve_parser = e2e_sub.add_parser(
        "serve",
        help="実機層（make e2e-live）の実行結果をブラウザで見る（FHIRスタブ:8084 とアプリ）",
    )
    e2e_serve_parser.add_argument(
        "--host", default="127.0.0.1", help="ホスト（既定 127.0.0.1。外へ公開しない）"
    )
    e2e_serve_parser.add_argument("--port", type=int, default=8030, help="ポート番号（既定 8030）")
    e2e_serve_parser.add_argument(
        "--run-dir",
        default=None,
        help="表示する実行（var/e2e/live_runs/<時刻>/。既定は最新）",
    )

    # serve コマンド
    serve_parser = subparsers.add_parser("serve", help="開発サーバを起動")
    serve_parser.add_argument(
        "--enable-ai-drafting",
        action="store_true",
        default=False,
        help="候補探索AIを有効にする（所在AI案・手順書生成・取得プラン作成）",
    )
    serve_parser.add_argument(
        "--enable-ai-extraction",
        action="store_true",
        default=False,
        help="自動構造化AIを有効にする（実行時の llm_extract / llm_judge）",
    )
    serve_parser.add_argument(
        "--host", default=settings.host, help=f"ホスト (デフォルト: {settings.host})"
    )
    serve_parser.add_argument(
        "--port", type=int, default=settings.port, help=f"ポート番号 (デフォルト: {settings.port})"
    )
    serve_parser.add_argument(
        "--reload", action="store_true", default=False, help="ホットリロードを有効にする"
    )

    args = parser.parse_args()

    if args.command == "create_admin":
        password = args.password
        if not password:
            password = getpass.getpass("パスワード: ")
            password_confirm = getpass.getpass("パスワード（確認）: ")
            if password != password_confirm:
                print("エラー: パスワードが一致しません")
                sys.exit(1)

        created = asyncio.run(
            create_admin_user(
                username=args.username,
                email=args.email,
                password=password,
                full_name=args.name,
            )
        )
        if not created:
            # パスワードの方針違反・重複などで作らなかった。スクリプトが失敗を検知できるようにする
            sys.exit(1)

    elif args.command == "init_db":
        asyncio.run(init_db())

    elif args.command == "list_users":
        asyncio.run(list_users())

    elif args.command == "check_schema":
        asyncio.run(check_schema())

    elif args.command == "init_demo":
        from examples.init_demo_data import init_demo_data

        asyncio.run(init_demo_data())

    elif args.command == "serve":
        import os
        import subprocess

        env = os.environ.copy()
        if args.enable_ai_drafting:
            env["ENABLE_AI_DRAFTING"] = "true"
        if args.enable_ai_extraction:
            env["ENABLE_AI_EXTRACTION"] = "true"
        cmd = ["uvicorn", "kawasemi.api.main:app", "--host", args.host, "--port", str(args.port)]
        # リバースプロキシ(nginx)配下で X-Forwarded-Proto/For を信頼し、
        # scheme（https判定→Cookie secure）とクライアントIP（レート制限）を正しく扱う。
        # 信頼プロキシIP限定: 既定はローカルのみ。プロキシ配下では
        # FORWARDED_ALLOW_IPS にプロキシの実IP/CIDR を設定する（"*" は直接到達可能な
        # 環境で X-Forwarded-For 詐称→レート制限回避・監査ログ汚染を許すため非推奨）。
        # 空文字も既定へ倒す（Dockerfile の ${VAR:-127.0.0.1} と挙動を揃える）。
        forwarded_allow_ips = env.get("FORWARDED_ALLOW_IPS") or "127.0.0.1"
        cmd += ["--proxy-headers", "--forwarded-allow-ips", forwarded_allow_ips]
        if args.reload:
            cmd.append("--reload")
        subprocess.run(cmd, env=env)

    elif args.command == "e2e":
        if args.e2e_command == "setup":
            e2e_setup(build_index=args.build_index, embedder=args.embedder)
        elif args.e2e_command == "serve":
            e2e_serve(host=args.host, port=args.port, run_dir=args.run_dir)
        else:
            e2e_parser.print_help()

    elif args.command == "index":
        if args.index_command == "search":
            search_resource_index(
                query=args.query,
                annotation=args.annotation,
                engine=args.engine,
                top_k=args.top_k,
                min_score=args.min_score,
                model=args.model,
                index_dir=args.index_dir,
                corpus_dir=args.corpus_dir,
                as_json=args.json,
                use_hints=not args.no_hints,
                hint_terms=args.hint_term,
                hint_types=args.hint_type,
                hint_model=args.hint_model,
            )
        else:
            index_parser.print_help()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
