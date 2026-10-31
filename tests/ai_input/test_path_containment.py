"""file_extract / csv_extract の接続ルート封じ込め

`connection_id` は DSL 上は任意なので「接続ルートが解決できない」状態はプラン作成者が
選べる。以前はその場合に生パスをそのまま開いていたため、`RequirePlanEditor` を持つ
アクターが `/etc/app/secrets.json` や `/mnt/other-study/subjects.csv` を指すプランを
確定でき、アプリホスト上の任意ファイルを読めた（読み取り結果は step 値として後続の
LLM へ渡り evidence にも保存される）。

ここでは「ルート未設定なら読まない（フェイルクローズ）」「絶対パスを拒否する」
「root 配下に収まることを resolve 後に確認する」の3点を、両 op について固定する。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.dsl import validate_program
from kawasemi.ai_input.executor import (
    DSLInterpreter,
    PathOutsideRootError,
    resolve_under_connection_root,
)
from kawasemi.ai_input.models import FailureCode, InputPlan
from kawasemi.ai_input.resource_index import connections as conn_mod


def _plan(link_id: str, program: dict) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id=link_id, program=program, status="confirmed")


def _use_root(monkeypatch: pytest.MonkeyPatch, root: Path | None) -> None:
    """接続レジストリを差し替える（root=None で「接続未登録」を再現）。"""
    conns = (
        {}
        if root is None
        else {"fs": {"kind": "file", "scope": "external", "root_path": str(root)}}
    )
    registry = conn_mod.ConnectionRegistry(conns)
    monkeypatch.setattr(conn_mod, "get_registry", lambda: registry)
    monkeypatch.setattr("kawasemi.ai_input.executor.get_registry", lambda: registry)


# --------------------------------------------------------------------------- #
# ヘルパ単体
# --------------------------------------------------------------------------- #


def test_resolve_requires_connection_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """ルートが解決できないときは読まない（旧実装は生パスを開いていた）。"""
    _use_root(monkeypatch, None)
    with pytest.raises(PathOutsideRootError):
        resolve_under_connection_root(None, "reports/x.txt")
    with pytest.raises(PathOutsideRootError):
        resolve_under_connection_root("unregistered", "reports/x.txt")


@pytest.mark.parametrize(
    "rel",
    [
        "/etc/app/secrets.json",  # 絶対パス（Path 結合では root を捨てる）
        "../outside.txt",  # 上位参照
        "sub/../../outside.txt",  # 途中の上位参照
    ],
)
def test_resolve_rejects_escape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _use_root(monkeypatch, root)
    with pytest.raises(PathOutsideRootError):
        resolve_under_connection_root("fs", rel)


def test_resolve_rejects_absolute_path_even_inside_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """root 配下を指す絶対パスも拒否する。

    配下検査は通ってしまうため、この判定だけが防御になるケース。root が `/` の場合や
    `root_path: /etc` に対する `/etc/passwd` のように、指定先がたまたま root の配下に
    含まれる設定では、絶対パス拒否が無いと素通りする。
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("x", encoding="utf-8")
    _use_root(monkeypatch, root)
    with pytest.raises(PathOutsideRootError, match="絶対パス"):
        resolve_under_connection_root("fs", str(root / "ok.txt"))


@pytest.mark.parametrize("rel", ["a\x00/etc/passwd", 123, None, [], {}])
def test_resolve_rejects_non_string_and_nul(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: object
) -> None:
    """契約外の入力でも PathOutsideRootError に寄せる（ValueError/TypeError を漏らさない）。"""
    root = tmp_path / "root"
    root.mkdir()
    _use_root(monkeypatch, root)
    with pytest.raises(PathOutsideRootError):
        resolve_under_connection_root("fs", rel)  # type: ignore[arg-type]


def test_resolve_accepts_path_under_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "reports").mkdir(parents=True)
    _use_root(monkeypatch, root)
    got = resolve_under_connection_root("fs", "reports/x.txt")
    assert got == (root.resolve() / "reports" / "x.txt")


# --------------------------------------------------------------------------- #
# op 経由（実行して読まれないことを確認する）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_file_extract_refuses_when_root_unset(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """攻撃シナリオ: connection_id を書かず絶対パスを読む。

    **保存時の検証で層が1つ増えた。** この形（`connection_id` 無し・絶対パス）は
    いまや `validate_program` で止まるので、**step まで到達しない**（trace が空になる）。
    実行時のフェイルクローズ（`resolve_under_connection_root`）は残っているが、そこへ
    届く前に落ちる。ここで守るのは「読まれないこと」なので、**どの層で止まったかに
    依存しない形**（値が入らない・秘密が evidence に出ない）で書く。

    step 単位の拒否コード（`path_not_permitted`）が付く経路は、`connection_id` はあるが
    ルートが解決できない／ルート外を指す場合である（下の2つのテスト）。
    """
    secret = tmp_path / "secrets.json"
    secret.write_text('{"token": "SECRET"}', encoding="utf-8")
    _use_root(monkeypatch, None)

    program = {
        "steps": [{"op": "file_extract", "id": "doc", "path_pattern": str(secret)}],
        "return": "$doc",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("q", program)], "P1", db_session)

    assert "q" not in prefill
    assert evidence["q"]["value"] is None
    assert "SECRET" not in str(evidence)
    # 保存時にも同じ形が弾かれること（fail-fast 層）
    errors = validate_program(program)
    assert any("connection_id" in e for e in errors), errors
    assert any("絶対パス" in e for e in errors), errors


@pytest.mark.asyncio
async def test_the_path_rejection_is_not_unclassified(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """パスのポリシー拒否に `path_not_permitted` が付くこと。

    `unclassified` は「分類不能・コード未付与（旧レコード）の受け皿」で、`failure_code` は
    **項目別の自動充足率・失敗理由内訳の一次データ**である。設定・プランの誤りをそこへ
    入れると、**「所在同定が困難な項目」を特定するための指標が汚れる**。

    `connection_id` は書いてあるがレジストリに無い（＝ルートが解決できない）形を使う。
    これは `validate_program` を通るので、**実行時のフェイルクローズだけが止める**経路である。
    """
    secret = tmp_path / "secrets.json"
    secret.write_text('{"token": "SECRET"}', encoding="utf-8")
    _use_root(monkeypatch, None)  # 接続レジストリが空＝"fs" は未登録

    program = {
        "steps": [
            {
                "op": "file_extract",
                "id": "doc",
                "connection_id": "fs",
                "path_pattern": "reports/x.txt",
            }
        ],
        "return": "$doc",
    }
    assert validate_program(program) == []  # 保存時には通る形である

    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("q", program)], "P1", db_session)

    assert "q" not in prefill
    trace = evidence["q"]["trace"][0]
    assert trace["output"] is None
    assert "root_path が未設定" in trace["error"], trace["error"]
    assert trace["failure_code"] == FailureCode.PATH_NOT_PERMITTED
    assert evidence["q"]["failure_code"] == FailureCode.PATH_NOT_PERMITTED
    assert "SECRET" not in str(evidence)


@pytest.mark.asyncio
async def test_the_path_rejection_is_logged_without_the_raw_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """拒否がログに残り、しかも**生パスは残らない**こと。

    以前は `raise` にも except 節にも `logger` 呼び出しが無く、`/etc/shadow` や他研究の
    ディレクトリを総当たりで書いて実行しても**サーバ側に痕跡が残らなかった**（evidence を
    読まないと気づけず、能動的な検知ができない）。

    一方 `rel` は `{{patient_id}}` の展開後の値を含みうる（＝PHI）ので、**残すのは長さと
    sha256 の先頭12桁だけ**にする。総当たりはダイジェストの一致で数えられる。
    """
    root = tmp_path / "root"
    root.mkdir()
    _use_root(monkeypatch, root)
    rel = "../他研究/患者12345.csv"

    with caplog.at_level(logging.WARNING):
        with pytest.raises(PathOutsideRootError):
            resolve_under_connection_root("fs", rel)

    messages = [r.getMessage() for r in caplog.records]
    assert any("パス拒否" in m for m in messages), messages
    rejection = next(m for m in messages if "パス拒否" in m)
    assert "connection_id=fs" in rejection
    assert hashlib.sha256(rel.encode()).hexdigest()[:12] in rejection
    # 生パス（患者IDを含みうる）はログに出さない
    assert "患者12345" not in rejection
    assert rel not in rejection


@pytest.mark.asyncio
async def test_csv_extract_refuses_when_root_unset(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "subjects.csv"
    other.write_text("患者ID,値\nP1,SECRET\n", encoding="utf-8")
    _use_root(monkeypatch, None)

    program = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "n",
                "path": str(other),
                "key_column": "患者ID",
                "column": "値",
            }
        ],
        "return": "$n",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("q", program)], "P1", db_session)

    assert "q" not in prefill
    assert "SECRET" not in str(evidence)


@pytest.mark.asyncio
async def test_csv_extract_enforces_root_containment(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """root があっても配下検査が無かった（見落とされていた経路）。"""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("患者ID,値\nP1,SECRET\n", encoding="utf-8")
    _use_root(monkeypatch, root)

    program = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "n",
                "connection_id": "fs",
                "path": "../outside.csv",
                "key_column": "患者ID",
                "column": "値",
            }
        ],
        "return": "$n",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("q", program)], "P1", db_session)

    assert "q" not in prefill
    assert "SECRET" not in str(evidence)


@pytest.mark.asyncio
async def test_file_extract_reads_file_under_root(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常系は従来どおり読める（過剰な縮退になっていないこと）。"""
    root = tmp_path / "root"
    (root / "P1").mkdir(parents=True)
    (root / "P1" / "note.txt").write_text("所見テキスト", encoding="utf-8")
    _use_root(monkeypatch, root)

    program = {
        "steps": [
            {
                "op": "file_extract",
                "id": "doc",
                "connection_id": "fs",
                "path_pattern": "{{patient_id}}/note.txt",
            }
        ],
        "return": "$doc",
    }
    interp = DSLInterpreter()
    prefill, _ = await interp.execute([_plan("q", program)], "P1", db_session)

    assert prefill["q"] == "所見テキスト"
