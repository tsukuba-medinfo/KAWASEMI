"""`kawasemi.core.config.Settings` のユニットテスト

検証対象:
- 既定値とフィールドのバリデーション（必須シークレット / Literal）
- `normalize_root_path`（サブパスの先頭/末尾スラッシュ正規化）
- `normalize_sqlite_database_url`（相対SQLiteパスの絶対化）
- `reject_legacy_enable_ai` のうち **.env ファイル走査ブランチ**
  （環境変数ブランチは tests/ai_input/test_ai_flags.py で検証済みのため重複させない）
- 派生プロパティ（allowed_hosts_list / embedding_host / *_dir_path）

実 `.env` や実環境変数の漏れ込みを排除するため、`isolated_env` フィクスチャで
`Settings.model_config["env_file"]` を無効化し、関連環境変数をクリアして決定論的にする。
"""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from kawasemi.core.config import PROJECT_ROOT, Settings

SQLITE_PREFIX = "sqlite+aiosqlite:///"


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """実 .env / OS環境変数の漏れ込みを完全に排除し、Settings 構築を決定論的にする。

    os.environ を空dictへ差し替え、env_file も無効化する。これにより CI/シェルに
    export された SECRET_KEY / DEBUG / LOG_LEVEL 等が既定値テストへ漏れ込まない。
    """
    monkeypatch.setattr(os, "environ", {})
    # reject_legacy_enable_ai が実プロジェクトの .env を走査しないよう無効化する
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    return monkeypatch


@pytest.fixture
def make_settings(isolated_env: pytest.MonkeyPatch):
    """必須シークレットを補完して Settings を組み立てるファクトリ。"""

    def _make(**overrides) -> Settings:
        base: dict = {"secret_key": "x" * 32, "csrf_secret_key": "y" * 32}
        base.update(overrides)
        return Settings(**base)  # type: ignore[arg-type]

    return _make


# ---------------------------------------------------------------------------
# 既定値
# ---------------------------------------------------------------------------


def test_defaults(make_settings) -> None:
    s = make_settings()
    assert s.app_name == "KAWASEMI"
    assert s.environment == "development"
    assert s.debug is False
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.root_path == ""
    assert s.enable_ai_drafting is False
    assert s.enable_ai_extraction is False
    assert s.jwt_algorithm == "HS256"
    assert s.jwt_cookie_name == "kawasemi_token"
    assert s.log_level == "INFO"
    assert s.log_format == "json"
    assert s.rate_limit_login == "5/minute"


def test_vllm_default_port_differs_from_app_port(make_settings) -> None:
    """vLLM の既定 URL はアプリ自身のポートを指さない（同じ機械で両方を動かすと衝突する）。"""
    from urllib.parse import urlsplit

    s = make_settings()
    assert s.vllm_base_url == "http://localhost:8100"
    assert urlsplit(s.vllm_base_url).port != s.port


# ---------------------------------------------------------------------------
# 必須フィールド / 長さ制約
# ---------------------------------------------------------------------------


def test_secret_key_required(isolated_env: pytest.MonkeyPatch) -> None:
    # \b で csrf_secret_key への部分一致を避け、secret_key 固有のエラーを保証する
    with pytest.raises(ValidationError, match=r"\bsecret_key\b"):
        Settings(csrf_secret_key="y" * 32)  # type: ignore[call-arg]


def test_csrf_secret_key_required(isolated_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="csrf_secret_key"):
        Settings(secret_key="x" * 32)  # type: ignore[call-arg]


def test_secret_key_too_short(make_settings) -> None:
    with pytest.raises(ValidationError, match=r"\bsecret_key\b"):
        make_settings(secret_key="short")


def test_csrf_secret_key_too_short(make_settings) -> None:
    with pytest.raises(ValidationError, match="csrf_secret_key"):
        make_settings(csrf_secret_key="short")


def test_secret_key_exactly_32_chars_ok(make_settings) -> None:
    s = make_settings(secret_key="a" * 32)
    assert len(s.secret_key) == 32


# ---------------------------------------------------------------------------
# Literal フィールド
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["development", "staging", "production"])
def test_valid_environment(make_settings, env: str) -> None:
    assert make_settings(environment=env).environment == env


def test_production_forbids_debug(make_settings) -> None:
    """production + DEBUG=true は fail-fast（SQL経由のPHIログ露出を防ぐ）。"""
    with pytest.raises(ValidationError, match="DEBUG"):
        make_settings(environment="production", debug=True)


def test_production_forbids_log_level_debug(make_settings) -> None:
    """production + LOG_LEVEL=DEBUG も fail-fast。"""
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        make_settings(environment="production", log_level="DEBUG")


def test_invalid_environment(make_settings) -> None:
    with pytest.raises(ValidationError):
        make_settings(environment="prod")


def test_invalid_log_level(make_settings) -> None:
    with pytest.raises(ValidationError):
        make_settings(log_level="TRACE")


def test_invalid_log_format(make_settings) -> None:
    with pytest.raises(ValidationError):
        make_settings(log_format="yaml")


# ---------------------------------------------------------------------------
# normalize_root_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("kawasemi", "/kawasemi"),
        ("/kawasemi", "/kawasemi"),
        ("/kawasemi/", "/kawasemi"),
        ("", ""),
        ("/", ""),
        ("  /api/v1/  ", "/api/v1"),
        ("///x///", "/x"),
    ],
)
def test_normalize_root_path(make_settings, raw: str, expected: str) -> None:
    assert make_settings(root_path=raw).root_path == expected


def test_normalize_root_path_non_string_becomes_empty(make_settings) -> None:
    # str 以外は "" に丸める（ルート直下）
    assert make_settings(root_path=None).root_path == ""


# ---------------------------------------------------------------------------
# normalize_sqlite_database_url
# ---------------------------------------------------------------------------


def test_relative_sqlite_url_becomes_absolute(make_settings) -> None:
    s = make_settings(database_url=f"{SQLITE_PREFIX}./sub/test.db")
    assert s.database_url.startswith(SQLITE_PREFIX)
    db_path = s.database_url[len(SQLITE_PREFIX) :]
    assert Path(db_path).is_absolute()
    assert db_path.endswith("test.db")
    # プロジェクトルート基準で解決される
    assert PROJECT_ROOT.as_posix() in db_path


def test_absolute_sqlite_url_unchanged(make_settings) -> None:
    url = f"{SQLITE_PREFIX}/var/data/abs.db"
    assert make_settings(database_url=url).database_url == url


def test_non_sqlite_url_unchanged(make_settings) -> None:
    url = "postgresql+asyncpg://user:pw@host:5432/db"
    assert make_settings(database_url=url).database_url == url


def test_sqlite_url_without_path_unchanged(make_settings) -> None:
    # パス部が空（":memory:" でもない裸の prefix）はそのまま返す
    url = SQLITE_PREFIX
    assert make_settings(database_url=url).database_url == url


@pytest.mark.parametrize(
    "path",
    [":memory:", "file:shared?mode=memory&cache=shared&uri=true", "file:rel/x.db?uri=true"],
)
def test_in_memory_and_uri_sqlite_urls_unchanged(make_settings, path: str) -> None:
    """`:memory:` と `file:` URI はパスではないので、リポジトリ直下のファイルに化けさせない。"""
    url = f"{SQLITE_PREFIX}{path}"
    assert make_settings(database_url=url).database_url == url


# ---------------------------------------------------------------------------
# 派生プロパティ
# ---------------------------------------------------------------------------


def test_embedding_host_falls_back_to_ollama_base_url(make_settings) -> None:
    s = make_settings(ollama_base_url="http://llm:80", embedding_base_url="  ")
    assert s.embedding_host == "http://llm:80"


def test_embedding_host_prefers_embedding_base_url(make_settings) -> None:
    s = make_settings(ollama_base_url="http://llm:80", embedding_base_url="http://localhost:11434")
    assert s.embedding_host == "http://localhost:11434"
    assert s.ollama_base_url == "http://llm:80"


def test_allowed_hosts_list_default(make_settings) -> None:
    assert make_settings().allowed_hosts_list == ["localhost", "127.0.0.1"]


def test_allowed_hosts_list_strips_whitespace(make_settings) -> None:
    s = make_settings(allowed_hosts="a.com, b.com ,c.com")
    assert s.allowed_hosts_list == ["a.com", "b.com", "c.com"]


def test_output_dir_path_properties(make_settings) -> None:
    # `output_dir` / `output_dir_path` は参照が無かったので削除した。
    # 実際に作られるのは下の2つである。
    s = make_settings(
        questionnaire_output_dir="/tmp/q",
        questionnaire_response_output_dir="/tmp/qr",
    )
    assert s.questionnaire_output_dir_path == Path("/tmp/q")
    assert s.questionnaire_response_output_dir_path == Path("/tmp/qr")


# ---------------------------------------------------------------------------
# reject_legacy_enable_ai（.env ファイル走査ブランチ + 環境変数の大小無視）
# 環境変数ブランチの基本ケースは tests/ai_input/test_ai_flags.py で検証済み。
# ---------------------------------------------------------------------------


def test_legacy_enable_ai_in_dotenv_rejected(
    isolated_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ENABLE_AI=true\n", encoding="utf-8")
    isolated_env.setitem(Settings.model_config, "env_file", env_file)
    with pytest.raises(ValidationError, match="ENABLE_AI は廃止されました"):
        Settings(secret_key="x" * 32, csrf_secret_key="y" * 32)  # type: ignore[call-arg]


def test_commented_legacy_enable_ai_in_dotenv_allowed(
    isolated_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # コメント行の ENABLE_AI は誤検出しない
    env_file = tmp_path / ".env"
    env_file.write_text("# ENABLE_AI=true\n", encoding="utf-8")
    isolated_env.setitem(Settings.model_config, "env_file", env_file)
    s = Settings(secret_key="x" * 32, csrf_secret_key="y" * 32)  # type: ignore[call-arg]
    assert s.enable_ai_drafting is False
    assert s.enable_ai_extraction is False


def test_legacy_enable_ai_env_case_insensitive(isolated_env: pytest.MonkeyPatch) -> None:
    # 小文字 enable_ai も大文字化して検出する（環境変数ブランチの大小無視を補完）
    isolated_env.setenv("enable_ai", "1")
    with pytest.raises(ValidationError, match="ENABLE_AI は廃止されました"):
        Settings(secret_key="x" * 32, csrf_secret_key="y" * 32)  # type: ignore[call-arg]
