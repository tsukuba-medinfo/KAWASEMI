"""file_export のファイル名サニタイズに関するセキュリティテスト

fhir_id は登録時にクライアント提供値をそのまま採用する経路があるため、
出力ファイル名/ZIP エントリ名に直挿入するとパストラバーサル/Zip Slip に
つながる。safe_fhir_id がこれを無害化することを確認する。

書き込み側は当初から safe_fhir_id を通していたが、**探す側**（エクスポート済み
ファイルのパスを返す2関数）と **ZIP 自体の置き場所**が素通しだった。
書き込みと読み取りで正規化が揃っていることを、ここで併せて固定する。
"""

import zipfile
from pathlib import Path

import pytest

from kawasemi.core.config import settings
from kawasemi.utils.file_export import (
    create_bulk_export_zip,
    export_questionnaire_response_to_file,
    export_questionnaire_responses_to_zip,
    export_questionnaire_to_file,
    get_questionnaire_export_path,
    get_questionnaire_response_export_path,
    safe_fhir_id,
)


def test_safe_fhir_id_strips_path_traversal():
    # パス区切り文字を無害化し、単一のパス要素に収める（遡上を不能にする）
    for evil in ["../../etc/passwd", "..\\..\\windows\\system32", "/abs/path", "a/b/c"]:
        cleaned = safe_fhir_id(evil)
        assert "/" not in cleaned
        assert "\\" not in cleaned
        # base ディレクトリ配下から外に出られないこと
        base = Path("/tmp/out")
        resolved = (base / f"X_{cleaned}.json").resolve()
        assert str(resolved).startswith(str(base.resolve()))


def test_safe_fhir_id_allows_valid_fhir_id():
    # 正規の FHIR id（英数・ハイフン・ドット）はそのまま温存
    assert safe_fhir_id("abc-123.v2") == "abc-123.v2"
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    assert safe_fhir_id(uuid) == uuid


def test_safe_fhir_id_handles_empty_and_none():
    assert safe_fhir_id("") == "unknown"
    assert safe_fhir_id(None) == "unknown"
    assert safe_fhir_id("...") == "unknown"  # ドットのみは strip 後に空


def test_safe_fhir_id_truncates_long_input():
    assert len(safe_fhir_id("a" * 200)) <= 64


def test_safe_fhir_id_truncation_does_not_revive_trailing_dot():
    """64文字目がドットだと切り詰め後に末尾ドットが復活しうる。"""
    evil = "a" * 63 + "." + "a" * 10  # strip(".")は無効（先頭末尾はaのまま）。[:64]の末尾がドット
    cleaned = safe_fhir_id(evil)
    assert not cleaned.endswith(".")


def test_safe_fhir_id_accepts_int():
    assert safe_fhir_id(42) == "42"


# --------------------------------------------------------------------------- #
# 探す側の正規化
# --------------------------------------------------------------------------- #


@pytest.fixture()
def output_dirs(tmp_path, monkeypatch):
    """出力ディレクトリを tmp_path 配下へ向ける。

    遡上の成否を見るため、出力ディレクトリの1段上に、探索パターンに合致する名前の
    囮ファイルを置く。
    """
    q = tmp_path / "out" / "questionnaires"
    qr = tmp_path / "out" / "responses"
    monkeypatch.setattr(settings, "questionnaire_output_dir", str(q))
    monkeypatch.setattr(settings, "questionnaire_response_output_dir", str(qr))
    q.mkdir(parents=True)
    qr.mkdir(parents=True)
    (tmp_path / "out" / "evil_20260101_000000.json").write_text("{}", encoding="utf-8")
    # 末尾スラッシュ形（`a/../../`）はパターン末尾が `_*.json` になるので、
    # `_` 始まりの囮でないと届かない。
    (tmp_path / "out" / "_x_20260101_000000.json").write_text("{}", encoding="utf-8")
    return tmp_path


# 遡上の成立条件は次の3つである。
#
# 1. fhir_id が `/` を含み、パターンが複数のパス要素に分割される
#    （POSIX では `\` は区切りにならないので `\..\..\evil` は不成立）
# 2. `..` `.` を除くすべての中間要素が、実在するディレクトリとして解決できる。
#    パターンは `Questionnaire_{fhir_id}_*.json` なので**最初の要素は必ず
#    リテラル `Questionnaire_` で始まる**。足場の名前もこの接頭辞を持つ必要がある
#    （Response 側は `QuestionnaireResponse_`）
# 3. `..` の個数が外へ出るのに足りる。ただし足場が外を指す symlink なら `..` は不要
#
# 先頭 `/` は、最初の要素がちょうど接頭辞ぴったりになる特殊ケースにすぎない。
# サフィックスにはワイルドカードを書けるので、攻撃側は足場の**サフィックスを知る
# 必要はない**。ただし接頭辞は固定なので「任意の名前のディレクトリ1つ」ではない。
_ESCAPE_INPUTS = [
    ("/../../evil", "Questionnaire_"),  # 足場のサフィックスが空
    ("a/../../evil", "Questionnaire_a"),  # 先頭スラッシュ無しでも成立
    ("*/../../evil", "Questionnaire_a"),  # サフィックスをワイルドカードで伏せる
    ("a/../../", "Questionnaire_a"),  # 末尾スラッシュ（`_*.json` が親に効く）
    ("a/../../ev*", "Questionnaire_a"),  # 末尾もワイルドカード
]


@pytest.mark.parametrize(("evil", "scaffold"), _ESCAPE_INPUTS)
def test_questionnaire_export_path_cannot_escape_with_scaffold(output_dirs, evil, scaffold):
    """足場ディレクトリを実際に作った上で、出力ディレクトリの外を拾えないこと。"""
    qdir = output_dirs / "out" / "questionnaires"
    (qdir / scaffold).mkdir()
    # 足場から2段上がると出力ディレクトリの外（out/ 直下）の囮に届く。
    assert (output_dirs / "out" / "evil_20260101_000000.json").is_file()
    assert get_questionnaire_export_path(evil) is None


@pytest.mark.parametrize(("evil", "scaffold"), _ESCAPE_INPUTS)
def test_response_export_path_cannot_escape_with_scaffold(output_dirs, evil, scaffold):
    """Response 側も同じ関門を持つこと（Questionnaire 側だけ直す誤りの防止）。"""
    qrdir = output_dirs / "out" / "responses"
    (qrdir / scaffold.replace("Questionnaire_", "QuestionnaireResponse_")).mkdir()
    assert (output_dirs / "out" / "evil_20260101_000000.json").is_file()
    assert get_questionnaire_response_export_path(evil) is None


def test_export_path_cannot_escape_with_multi_level_scaffold(output_dirs):
    """中間要素が2段でも塞がっていること（成立条件2が全要素にかかることの確認）。"""
    qdir = output_dirs / "out" / "questionnaires"
    (qdir / "Questionnaire_a" / "b").mkdir(parents=True)
    assert get_questionnaire_export_path("a/b/../../../evil") is None


def test_export_path_cannot_escape_via_symlink_scaffold(output_dirs):
    """足場が外を指す symlink なら `..` すら要らない。それも塞がっていること。"""
    qdir = output_dirs / "out" / "questionnaires"
    (qdir / "Questionnaire_a").symlink_to(output_dirs / "out", target_is_directory=True)
    assert get_questionnaire_export_path("a/evil") is None


def test_export_path_getters_find_written_file(output_dirs):
    """書き込み側と探す側の正規化が揃っていること（正常系）。"""
    written = export_questionnaire_to_file(1, "abc-123", {"resourceType": "Questionnaire"})
    assert get_questionnaire_export_path("abc-123") == written

    written_resp = export_questionnaire_response_to_file(
        1, "resp-1", {"resourceType": "QuestionnaireResponse"}
    )
    assert get_questionnaire_response_export_path("resp-1") == written_resp


def test_export_path_getters_find_file_written_under_sanitized_name(output_dirs):
    """正規化で文字が置換される ID でも、書いたファイルを見つけられること。

    正規化を通さないと、書き込み側は `A_B` で保存するのに探す側は `A/B` で
    探すことになり、必ず取り逃す（セキュリティだけでなく機能の欠陥でもある）。
    """
    written = export_questionnaire_to_file(1, "a/b", {"resourceType": "Questionnaire"})
    assert written.name.startswith("Questionnaire_a_b_")
    assert get_questionnaire_export_path("a/b") == written

    written_resp = export_questionnaire_response_to_file(
        1, "a/b", {"resourceType": "QuestionnaireResponse"}
    )
    assert written_resp.name.startswith("QuestionnaireResponse_a_b_")
    assert get_questionnaire_response_export_path("a/b") == written_resp


def test_zip_export_always_writes_under_output_dir(output_dirs):
    """呼び出し側が ZIP 名を選べない（常にタイムスタンプ生成）ので出力先は常に配下。"""
    path = export_questionnaire_responses_to_zip([])
    assert path.parent == settings.questionnaire_response_output_dir_path


# --------------------------------------------------------------------------- #
# ZIP エントリ名（Zip Slip）
# --------------------------------------------------------------------------- #
#
# 書き込み側は当初から safe_fhir_id を通していたが、**エントリ名を検査するテストが
# 1件も無かった**。とくに create_bulk_export_zip は
# `api/v100/questionnaire_responses.py::bulk_export_questionnaire_responses` から
# 本番配線されており、fhir_id は safe_fhir_id の docstring が言うとおり
# 「登録時にクライアント提供値をそのまま採用する経路がある」値である。
# 正規化を外しても全テストが通る状態だったので、ここで固定する。

# 正規化後に重複しない値を選ぶ（ZipFile は同名エントリを警告付きで二重に書く）。
_ZIP_SLIP_IDS = ["../../evil", "/abs/evil", "a/b/c", "..", "x\\y"]


def _entry_names_are_contained(names: list[str]) -> bool:
    return all(
        "/" not in n and "\\" not in n and Path(n).name == n and ".." not in Path(n).parts
        for n in names
    )


def test_bulk_export_zip_entry_names_are_contained(output_dirs):
    """本番経路。エントリ名が単一のパス要素に収まること。"""
    zip_path = create_bulk_export_zip([(fid, {"id": fid}) for fid in _ZIP_SLIP_IDS])
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert len(names) == len(_ZIP_SLIP_IDS)
    assert _entry_names_are_contained(names), names


def test_responses_zip_entry_names_are_contained(output_dirs):
    zip_path = export_questionnaire_responses_to_zip(
        [{"fhir_id": fid, "content": {"id": fid}} for fid in _ZIP_SLIP_IDS]
    )
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert len(names) == len(_ZIP_SLIP_IDS)
    assert _entry_names_are_contained(names), names


# 展開側の封じ込め（extractall が対象ディレクトリの外へ出さないこと）は
# CPython の zipfile が常に行うため、テストしても本リポジトリのコードを検証しない。
# 固定すべきなのは上記2件、すなわち**エントリ名を作る側**である。
