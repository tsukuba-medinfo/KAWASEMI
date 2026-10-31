"""デモ FHIR スタブの配信ルート封じ込め

`_load_bundle` と `search_patient` は、クエリパラメータの値を検証せずそのまま
パス結合していた。`patient=../../..` のような値で配信ルート外のファイルを読めた
（basename は固定されるので `Condition.json` / `Observation*.json` / `<名前>.json`
に限られるが、遡上そのものは成立していた）。

ここでは「区切り文字や `..` を含む値を拒否する」「結合後もルート配下であることを
確認する」の2段構えを、3つの入口すべてについて固定する。正常系も併せて固定し、
デモが従来どおり動くことを担保する。

DSL executor の file/csv の封じ込めとはアプリが別で、こちらは `kawasemi e2e serve` が
別プロセスで起動するスタブサーバである。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from kawasemi.ai_input.e2e.fhir_stub import (
    _BUNDLE_SUFFIX as BUNDLE_SUFFIX,
)
from kawasemi.ai_input.e2e.fhir_stub import (
    _OBSERVATION_FILENAME_MAX_LEN as OBS_FILENAME_MAX_LEN,
)
from kawasemi.ai_input.e2e.fhir_stub import (
    _OBSERVATION_PREFIX as OBS_PREFIX,
)
from kawasemi.ai_input.e2e.fhir_stub import (
    _PATIENT_INDEX_FILENAME_MAX_LEN as INDEX_FILENAME_MAX_LEN,
)
from kawasemi.ai_input.e2e.fhir_stub import (
    _SEGMENT_MAX_LEN as SEGMENT_MAX_LEN,
)
from kawasemi.ai_input.e2e.fhir_stub import _resolve_under_base, create_app, is_safe_segment


@pytest.fixture()
def stub_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """配信ルートを tmp_path に向け、正常系の Bundle と「外」のファイルを置く。"""
    root = tmp_path / "fhir_stub"
    (root / "fhirpat-0009").mkdir(parents=True)
    (root / "fhirpat-0009" / "Condition.json").write_text(
        json.dumps({"resourceType": "Bundle", "id": "cond-ok"}), encoding="utf-8"
    )
    (root / "fhirpat-0009" / "Observation-figo-stage.json").write_text(
        json.dumps({"resourceType": "Bundle", "id": "obs-ok"}), encoding="utf-8"
    )
    (root / "_patient_index").mkdir()
    (root / "_patient_index" / "UC-0009.json").write_text(
        json.dumps({"resourceType": "Bundle", "type": "searchset", "total": 1}), encoding="utf-8"
    )
    # 受理側の境界。登録番号ちょうど _SEGMENT_MAX_LEN 文字。
    (root / "_patient_index" / f"{'L' * SEGMENT_MAX_LEN}.json").write_text(
        json.dumps({"resourceType": "Bundle", "type": "searchset", "total": 1}), encoding="utf-8"
    )
    # 拒否側の境界。**索引ファイルは実在させる**。実在させないと「長さで拒否した」のか
    # 「ファイルが無かった」のか区別できず、上限を緩めても拒否側のテストが通ってしまう。
    (root / "_patient_index" / f"{'X' * (SEGMENT_MAX_LEN + 1)}.json").write_text(
        json.dumps({"resourceType": "Bundle", "type": "searchset", "total": 1}), encoding="utf-8"
    )
    # Observation の code も合成後のファイル名になる（登録番号と同型）。受理側・拒否側とも
    # Bundle を実在させて、境界がどちらの理由で決まったかを判別できるようにする。
    (root / "fhirpat-0009" / f"{OBS_PREFIX}{'C' * SEGMENT_MAX_LEN}.json").write_text(
        json.dumps({"resourceType": "Bundle", "id": "obs-limit"}), encoding="utf-8"
    )
    (root / "fhirpat-0009" / f"{OBS_PREFIX}{'D' * (SEGMENT_MAX_LEN + 1)}.json").write_text(
        json.dumps({"resourceType": "Bundle", "id": "obs-over"}), encoding="utf-8"
    )
    # 配信ルートの外。遡上できれば読めてしまうもの。
    (tmp_path / "Condition.json").write_text(
        json.dumps({"resourceType": "Bundle", "id": "SECRET"}), encoding="utf-8"
    )
    (tmp_path / "secrets.json").write_text(
        json.dumps({"resourceType": "Bundle", "id": "SECRET"}), encoding="utf-8"
    )
    monkeypatch.setenv("FHIR_STUB_DIR", str(root))
    return root


@pytest.fixture()
def client(stub_root: Path) -> TestClient:
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# セグメント検査の単体
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "..",
        ".",
        "...",
        "../etc",
        "sub/../..",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "C:\\Windows",
        "\\\\server\\share",
        "x\x00y",
        "",
        "a" * 65,
    ],
)
def test_unsafe_segments_rejected(value: str) -> None:
    assert not is_safe_segment(value)


@pytest.mark.parametrize("value", ["fhirpat-0009", "UC-0009", "figo-stage", "Condition.json", "a"])
def test_safe_segments_accepted(value: str) -> None:
    assert is_safe_segment(value)


# --------------------------------------------------------------------------- #
# 攻撃シナリオの再現
# --------------------------------------------------------------------------- #


def test_condition_rejects_traversal(client: TestClient, stub_root: Path) -> None:
    """patient で遡上して配信ルート外の Condition.json を読めないこと。"""
    res = client.get("/fhir/Condition", params={"patient": "../"})
    assert res.status_code == 400
    assert "SECRET" not in res.text


def test_observation_rejects_traversal_via_patient(client: TestClient) -> None:
    res = client.get("/fhir/Observation", params={"patient": "..", "code": "figo-stage"})
    assert res.status_code == 400


def test_observation_rejects_traversal_via_code(client: TestClient) -> None:
    """code もファイル名へ埋め込まれるので、区切り文字を含む値を拒否すること。"""
    res = client.get(
        "/fhir/Observation", params={"patient": "fhirpat-0009", "code": "../../secrets"}
    )
    assert res.status_code == 400


def test_patient_search_rejects_traversal(client: TestClient) -> None:
    """identifier は system|value 形式。value 側で遡上できないこと。

    索引は `<root>/_patient_index/` の下なので、配信ルートの外へ出るには2段上がる。
    """
    res = client.get("/fhir/Patient", params={"identifier": "urn:x|../../secrets"})
    assert res.status_code == 200
    assert res.json()["total"] == 0
    assert "SECRET" not in res.text


@pytest.mark.parametrize("length", [7, 55, 59, 60, 62, 64, 65, 100])
def test_patient_search_never_returns_400(client: TestClient, length: int) -> None:
    """長さによらず、この入口は 400 を返さないこと。

    400 を返すと `FhirIdentifierResolver` が例外を再送出し、その呼び出しは
    プランごとの try の外にあるため DSL プログラム全体が FAILED になって全項目が
    失われる。

    修正前は `.json` を足した合成後の長さで判定していたため 60〜64 が境界だった
    （現在は解消）。現在の境界は 64/65 で、parametrize の 59・60・62 はその名残。
    """
    res = client.get("/fhir/Patient", params={"identifier": "u" * length})
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_patient_search_rejects_bare_traversal(client: TestClient) -> None:
    """`|` を含まない場合は identifier 全体が value になる経路。"""
    res = client.get("/fhir/Patient", params={"identifier": "../../secrets"})
    assert res.status_code == 200
    assert res.json()["total"] == 0
    assert "SECRET" not in res.text


# --------------------------------------------------------------------------- #
# 後段の関門（resolve 後の配下検査）
# --------------------------------------------------------------------------- #
#
# 前段（is_safe_segment）は名前しか見ないので、配信ルート内に外を指す symlink が
# あると素通りする。後段の is_relative_to だけがこれを止める。前段だけでも遡上は
# 防げるため、この2件が無いと後段を消しても既存テストは全通過してしまう。


def test_containment_blocks_directory_symlink_escape(client: TestClient, stub_root: Path) -> None:
    """ルート配下のディレクトリ symlink で外へ出られないこと。"""
    (stub_root / "escape").symlink_to(stub_root.parent, target_is_directory=True)
    res = client.get("/fhir/Condition", params={"patient": "escape"})
    assert res.status_code == 400
    assert "配信ルート外" in res.json()["detail"]
    assert "SECRET" not in res.text


def test_containment_blocks_file_symlink_escape(client: TestClient, stub_root: Path) -> None:
    """Bundle 自体が外を指す symlink でも読ませないこと。"""
    link = stub_root / "fhirpat-0009" / "Observation-linked.json"
    link.symlink_to(stub_root.parent / "secrets.json")
    res = client.get("/fhir/Observation", params={"patient": "fhirpat-0009", "code": "linked"})
    assert res.status_code == 400
    assert "配信ルート外" in res.json()["detail"]
    assert "SECRET" not in res.text


# --------------------------------------------------------------------------- #
# 正常系（過剰な縮退になっていないこと）
# --------------------------------------------------------------------------- #


def test_condition_reads_bundle_under_root(client: TestClient) -> None:
    res = client.get("/fhir/Condition", params={"patient": "fhirpat-0009"})
    assert res.status_code == 200
    assert res.json()["id"] == "cond-ok"


def test_observation_reads_coded_bundle(client: TestClient) -> None:
    res = client.get("/fhir/Observation", params={"patient": "fhirpat-0009", "code": "figo-stage"})
    assert res.status_code == 200
    assert res.json()["id"] == "obs-ok"


def test_patient_search_finds_crosswalk(client: TestClient) -> None:
    res = client.get("/fhir/Patient", params={"identifier": "urn:kawasemi:demo:trial|UC-0009"})
    assert res.status_code == 200
    assert res.json()["total"] == 1


def test_missing_bundle_still_404(client: TestClient, stub_root: Path) -> None:
    """存在しない患者は 404 のまま（不正入力の 400 と切り分かれていること）。

    detail に配信ルートの絶対パスを載せないことも併せて固定する。
    `_resolve_under_base` の戻り値は resolve 済みなので、そのまま出すとホストの
    ディレクトリ構成を漏らす。
    """
    res = client.get("/fhir/Condition", params={"patient": "fhirpat-9999"})
    assert res.status_code == 404
    assert res.json()["detail"] == "Bundle が見つかりません: fhirpat-9999/Condition.json"
    assert str(stub_root) not in res.text


def test_patient_search_returns_empty_for_symlink_escape(
    client: TestClient, stub_root: Path
) -> None:
    """索引内に外を指す symlink があっても、この入口は 400 ではなく空を返すこと。

    後段の配下検査も 400 を投げるため、`_resolve_under_base` を素で呼ぶと
    「この入口は 400 を返さない」という不変条件が symlink 軸で破れる。
    """
    (stub_root / "_patient_index" / "LINKED.json").symlink_to(stub_root.parent / "secrets.json")
    res = client.get("/fhir/Patient", params={"identifier": "urn:x|LINKED"})
    assert res.status_code == 200
    assert res.json()["total"] == 0
    assert "SECRET" not in res.text


# --------------------------------------------------------------------------- #
# 長さ上限の適用単位
#
# 上限を「合成後のファイル名」に当てると、受理される登録番号は接尾辞のぶん短くなる。
# 拒否側（65文字）だけを固定していると、受理側の境界が静かに縮んでも気づけない。
# --------------------------------------------------------------------------- #


def test_segment_max_len_boundary_accepts_exactly_the_limit() -> None:
    """セグメント単体の受理境界（上限ちょうどは受理・1文字超で拒否）。"""
    assert is_safe_segment("a" * SEGMENT_MAX_LEN)
    assert not is_safe_segment("a" * (SEGMENT_MAX_LEN + 1))


def test_segment_max_len_is_per_kind() -> None:
    """ファイル名セグメントには接尾辞のぶん長い上限を指定できること。"""
    filename = "a" * SEGMENT_MAX_LEN + BUNDLE_SUFFIX
    # 既定の上限では合成後のファイル名は入らない（これが短すぎる上限の原因だった）
    assert not is_safe_segment(filename)
    # ファイル名用の上限なら受理される
    assert is_safe_segment(filename, max_len=INDEX_FILENAME_MAX_LEN)
    assert not is_safe_segment("a" * (INDEX_FILENAME_MAX_LEN + 1), max_len=INDEX_FILENAME_MAX_LEN)


def test_patient_search_accepts_identifier_at_segment_limit(client: TestClient) -> None:
    """上限ちょうど（_SEGMENT_MAX_LEN 文字）の登録番号が実際に引けること。

    修正前は `.json` を足した合成後の長さで判定していたため、60文字以上は索引に
    実在しても空 searchset になっていた（拒否ではなく静かな「該当なし」なので、
    利用者からは「登録されていない」と区別がつかない）。
    """
    res = client.get("/fhir/Patient", params={"identifier": "urn:x|" + "L" * SEGMENT_MAX_LEN})
    assert res.status_code == 200
    assert res.json()["total"] == 1


def test_patient_search_rejects_identifier_over_segment_limit(client: TestClient) -> None:
    """上限を1文字超えた登録番号は、400 ではなく空 searchset で返すこと。

    索引ファイルは実在させてある（fixture 参照）。実在しない名前で試すと
    「長さで拒否した」のか「ファイルが無かった」のかを区別できず、上限を緩める
    変更を入れてもこのテストが通ってしまう。
    """
    over = "X" * (SEGMENT_MAX_LEN + 1)
    res = client.get("/fhir/Patient", params={"identifier": "urn:x|" + over})
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_directory_segment_limit_is_unchanged(client: TestClient) -> None:
    """中間ディレクトリのセグメント（patient）の上限は据え置きであること。

    ファイル名側の上限を伸ばした影響が、`_load_bundle` 経由の入口へ及んでいない
    ことを固定する。
    """
    res = client.get("/fhir/Condition", params={"patient": "p" * (SEGMENT_MAX_LEN + 1)})
    assert res.status_code == 400
    res_ok = client.get("/fhir/Condition", params={"patient": "p" * SEGMENT_MAX_LEN})
    assert res_ok.status_code == 404  # 長さは通り、存在しないので 404


def test_observation_code_accepts_value_at_segment_limit(client: TestClient) -> None:
    """Observation の code も上限ちょうどまで引けること（登録番号の上限と同型の欠陥）。

    `Observation-`(12) + code + `.json`(5) を合成後のファイル名として既定の上限に
    当てると、code の実効上限は 47 文字まで縮む。上限は code そのものに当てる。
    """
    res = client.get(
        "/fhir/Observation", params={"patient": "fhirpat-0009", "code": "C" * SEGMENT_MAX_LEN}
    )
    assert res.status_code == 200
    assert res.json()["id"] == "obs-limit"


def test_observation_code_rejects_value_over_segment_limit(client: TestClient) -> None:
    """上限を1文字超えた code は 400 で拒否すること（Bundle は実在させてある）。"""
    res = client.get(
        "/fhir/Observation",
        params={"patient": "fhirpat-0009", "code": "D" * (SEGMENT_MAX_LEN + 1)},
    )
    assert res.status_code == 400


def test_filename_limit_does_not_leak_to_directory_segment() -> None:
    """ファイル名向けの緩和が、中間ディレクトリのセグメントへ波及しないこと。

    `_resolve_under_base` の上限を「呼び出し単位で全セグメントへ一律」にすると、
    ファイル名のために緩めた上限が patient 側にも黙って効く。最後のセグメント
    だけに適用していることを直接固定する。
    """
    long_dir = "p" * (SEGMENT_MAX_LEN + 1)
    with pytest.raises(HTTPException):
        _resolve_under_base(long_dir, "Condition.json", filename_max_len=OBS_FILENAME_MAX_LEN)

    # 最後のセグメント（ファイル名）側は緩和が効く
    filename = f"{OBS_PREFIX}{'c' * SEGMENT_MAX_LEN}{BUNDLE_SUFFIX}"
    assert len(filename) > SEGMENT_MAX_LEN
    _resolve_under_base("fhirpat-0009", filename, filename_max_len=OBS_FILENAME_MAX_LEN)
