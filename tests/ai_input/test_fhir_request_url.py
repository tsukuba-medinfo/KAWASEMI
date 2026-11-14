"""`fhir_extract` の要求 URL は、設定した FHIR サーバーだけを指す（F21）。

クエリのリテラル部分は設計者（または LLM）が書く。以前は `base_url + query` の素の連結で、
ベース URL にパスが無いと、`@host/x`（ユーザー情報）や `.evil.tld/x`（ホスト名の後ろ）で
要求先が変わり、FHIR の認可ヘッダーごと別のホストへ送られた。
"""

from typing import Any
from urllib.parse import urlsplit

import pytest

from kawasemi.ai_input.dsl import ParamBinder
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.fetch_scope import FhirUrlError, fhir_request_url
from kawasemi.ai_input.models import FailureCode

BASES = ["https://fhir.example", "https://fhir.example/", "https://fhir.example/fhir"]


@pytest.mark.parametrize("base", BASES)
@pytest.mark.parametrize(
    "query",
    [
        "@evil.example/x?patient=P1",
        "/@evil.example/x?patient=P1",
        "//evil.example/x?patient=P1",
        "\\\\evil.example\\x?patient=P1",
        "/\\evil.example/x?patient=P1",
        "https://evil.example/x?patient=P1",
        "http:evil.example/x?patient=P1",
        "evil.example:8080/x?patient=P1",
        "user:pw@evil.example/x?patient=P1",
    ],
)
def test_a_query_cannot_change_the_request_authority(base: str, query: str) -> None:
    with pytest.raises(FhirUrlError):
        fhir_request_url(base, query)


@pytest.mark.parametrize("base", BASES)
@pytest.mark.parametrize(
    "query",
    [
        "/Observation?patient=P1&code=x",
        "Observation?patient=P1&code=x",  # 索引の規約（先頭の `/` なし）は補う
        "/Patient/P1",
        "/Observation?code=http%3A%2F%2Floinc.org%7C1-2&patient=P1",
    ],
)
def test_a_normal_query_keeps_the_configured_server(base: str, query: str) -> None:
    url = fhir_request_url(base, query)
    got, want = urlsplit(url), urlsplit(base)
    assert (got.scheme, got.netloc) == (want.scheme, want.netloc)
    assert got.username is None
    assert got.path.startswith(want.path.rstrip("/") + "/")
    assert got.query == urlsplit("x?" + query.partition("?")[2]).query


@pytest.mark.parametrize("base", BASES)
def test_a_host_suffix_stays_a_path_on_the_configured_server(base: str) -> None:
    """`.evil.example/x` は、以前は `https://fhir.example.evil.example/x` になった（先頭の `/` が無い）。"""
    got = urlsplit(fhir_request_url(base, ".evil.example/x?patient=P1"))
    assert got.netloc == urlsplit(base).netloc
    assert got.path.endswith("/.evil.example/x")


@pytest.mark.parametrize(
    "query",
    [
        "/Observation/../../admin?patient=P1",
        "/Observation/./x?patient=P1",
        "/Observation%2e%2e/x?patient=P1",
        "/%2e%2e/%2E%2E/x?patient=P1",
        "/Observation%2Fx?patient=P1",
        "/Observation%5cx?patient=P1",
        "/Observation\r\nHost: evil.example?patient=P1",
        "/Observation\nx?patient=P1",
        "/Observation\tx?patient=P1",
        "/Observation\x00x?patient=P1",
        "/Observation x?patient=P1",
        "/Observation?patient=P1\r\nX: y",
    ],
)
@pytest.mark.parametrize("base", BASES)
def test_the_function_itself_refuses_traversal_and_control_characters(
    base: str, query: str
) -> None:
    """検査（`fhir_query_scopes_patient`）に頼らず、この関数だけで拒否する（多層防御）。"""
    with pytest.raises(FhirUrlError):
        fhir_request_url(base, query)


USERINFO_BASES = ["https://user:pw@fhir.example/fhir", "https://user:pw@fhir.example"]


@pytest.mark.parametrize("base", USERINFO_BASES)
def test_a_base_url_with_userinfo_still_works_for_a_legit_query(base: str) -> None:
    url = fhir_request_url(base, "/Observation?patient=P1&code=x")
    got, want = urlsplit(url), urlsplit(base)
    assert got.netloc == want.netloc
    assert (got.username, got.password) == ("user", "pw")


@pytest.mark.parametrize("base", USERINFO_BASES)
@pytest.mark.parametrize(
    "query",
    [
        "@evil.example/x?patient=P1",
        "evil:pw@evil.example/x?patient=P1",
        "//evil.example/x?patient=P1",
        "/@evil.example/x?patient=P1",
    ],
)
def test_a_base_url_with_userinfo_still_refuses_an_injected_authority(
    base: str, query: str
) -> None:
    got = None
    try:
        got = urlsplit(fhir_request_url(base, query))
    except FhirUrlError:
        return
    # 通るなら、ホストもユーザー情報もベースのまま（`/@evil.example/x` はパスの一部）
    assert got.netloc == urlsplit(base).netloc
    assert got.path.startswith(urlsplit(base).path.rstrip("/") + "/")


def test_the_pinned_base_path_is_kept() -> None:
    assert (
        fhir_request_url("https://fhir.example/fhir", "/Observation?patient=P1")
        == "https://fhir.example/fhir/Observation?patient=P1"
    )


async def _run(query: str, base_url: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    interp = DSLInterpreter()
    interp._fhir_client.base_url = base_url
    seen: list[str] = []

    async def fake_get_resource(url: str) -> dict[str, Any]:
        seen.append(url)
        return {"resourceType": "Bundle", "entry": []}

    interp._fhir_client._get_resource = fake_get_resource  # type: ignore[method-assign]
    step = {"op": "fhir_extract", "id": "f", "fhir_query": query, "multi": True}
    out, entry = await interp._op_fhir(
        step, "P1", {}, {"op": "fhir_extract", "id": "f"}, binder=ParamBinder({})
    )
    return {"out": out}, entry, seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "@evil.example/x?patient={{patient_id}}",
        "//evil.example/x?patient={{patient_id}}",
        "\\\\evil.example/x?patient={{patient_id}}",
    ],
)
async def test_the_executor_does_not_send_a_request_to_another_host(query: str) -> None:
    _, entry, seen = await _run(query, "https://fhir.example")

    assert seen == []  # 認可ヘッダーを付けた要求は出ていない
    assert entry["failure_code"] in (FailureCode.PATH_NOT_PERMITTED, FailureCode.UNCLASSIFIED)
    assert entry["output"] is None


@pytest.mark.asyncio
async def test_a_host_suffix_query_is_sent_only_to_the_configured_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """患者の範囲の検査が通ってしまった場合でも、URL 組み立ての層が要求先を変えさせない。

    `.evil.example/x` は、リソース型でないので患者の範囲の検査が先に拒否する（それは
    `test_program_sql_scope.py` が見る）。ここでは検査を**通す**ことで、検査が破れたときの
    最後の線（`fhir_request_url`）を、executor を通して確かめる（以前は
    `https://fhir.example.evil.example/x` へ認可ヘッダーごと送られた）。
    """
    from kawasemi.ai_input import executor as executor_module

    monkeypatch.setattr(executor_module, "fhir_query_scopes_patient", lambda _q: True)

    _, entry, seen = await _run(".evil.example/x?patient={{patient_id}}", "https://fhir.example")

    assert {urlsplit(u).netloc for u in seen} <= {"fhir.example"}
    # ホスト名の後ろに足した文字列は、パスとして設定のサーバーへ向かう
    assert seen == ["https://fhir.example/.evil.example/x?patient=P1"]
    assert entry.get("error") is None


@pytest.mark.asyncio
async def test_the_executor_still_calls_the_configured_server() -> None:
    _, entry, seen = await _run("Observation?patient={{patient_id}}&code=x", "https://fhir.example")

    assert seen == ["https://fhir.example/Observation?patient=P1&code=x"]
    assert entry.get("error") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["@evil.example/", "../x", "%2f", "a?b=c", "a#b", "a\r\nHost: evil", "a b", ".evil.example"],
)
async def test_a_hostile_parameter_value_cannot_change_the_request(value: str) -> None:
    """埋め込む値は字種を制限して符号化するので、URL の構造を変えられない。"""
    interp = DSLInterpreter()
    interp._fhir_client.base_url = "https://fhir.example"
    seen: list[str] = []

    async def fake_get_resource(url: str) -> dict[str, Any]:
        seen.append(url)
        return {"resourceType": "Bundle", "entry": []}

    interp._fhir_client._get_resource = fake_get_resource  # type: ignore[method-assign]
    step = {
        "op": "fhir_extract",
        "id": "f",
        "fhir_query": "/Observation?patient={{patient_id}}&code={{c}}",
    }
    binder = ParamBinder({"c": {"name": "c", "value": value}})
    try:
        await interp._op_fhir(step, "P1", {}, {"op": "fhir_extract", "id": "f"}, binder=binder)
    except Exception:  # noqa: BLE001 - 値の検査で弾かれてもよい（要求が出ないことを見る）
        pass

    for url in seen:
        got = urlsplit(url)
        assert got.netloc == "fhir.example"
        assert got.path == "/Observation"
        assert "\r" not in url and "\n" not in url and " " not in url
        assert got.query.count("patient=") == 1
