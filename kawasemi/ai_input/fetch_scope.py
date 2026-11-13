"""ファイル・FHIR の取得 step が患者で絞られているかの検査（F7 のうち SQL に依らない部分）

認可を通った**1人の患者**だけを取得するために、プランのパス・クエリが患者キー
（組み込みの `{{patient_id}}`）で決まることを要求する。SQL は対象外である
（`kawasemi.ai_input.executor` の「取得 step の患者の範囲」を参照）。

プレースホルダは組み込みの `{{patient_id}}` だけを認める。ほかのパラメータ（前段 step の出力を含む）は
患者に結び付く保証が無いので、患者キーの代わりにならない。
"""

from __future__ import annotations

import re

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# FHIR の検索パラメータ名は大文字小文字を区別する（`Patient=` は別のパラメータ）
_FHIR_PATIENT_PARAMS = {"patient", "subject"}
# 絞った結果に**ほかの資源を連れてくる・条件を差し替える**パラメータ
_FHIR_WIDENING_PARAMS = ("_include", "_revinclude", "_has", "_filter", "_query")
_ENCODED_SEPARATOR = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)


def path_scopes_patient(pattern: str) -> bool:
    """`path_pattern` のパス区切りの1区間が、患者キー（`{{patient_id}}`）で決まるか。

    区間の中で他のプレースホルダと混ぜない（`{{patient_id}}.txt` はよいが、
    `{{a}}{{patient_id}}` は区間を患者で決めたと言えない）。
    """
    for segment in pattern.split("/"):
        names = [m.group(1) for m in _PLACEHOLDER.finditer(segment)]
        if names == ["patient_id"]:
            return True
    return False


def fhir_query_scopes_patient(query: str) -> bool:
    """FHIR クエリが患者キーで絞る形か。

    - 検索パラメータ `patient` / `subject`（小文字のとおり）の値が**ちょうど** `{{patient_id}}`
    - または `/Patient/{{patient_id}}` で始まるパス（`Patient` も大文字小文字のとおり）

    次のクエリは絞っていても認めない: 符号化した `.` `/` `\\`（`%2e` など。パスの意味を
    変えうる）、`.` / `..` の区間、`_include` / `_revinclude` / `_has` / `_filter` / `_query`
    （絞った資源から別の資源を連れてくる、または条件を差し替える）。
    """
    path, _, qs = query.partition("?")
    if _ENCODED_SEPARATOR.search(query):
        return False
    segments = [s for s in path.split("/") if s]
    if any(seg in (".", "..") for seg in segments):
        return False
    params = [p.partition("=") for p in qs.split("&") if p]
    for name, _, _ in params:
        if name.strip().split(":")[0].startswith(_FHIR_WIDENING_PARAMS):
            return False

    def _whole(value: str) -> bool:
        m = _PLACEHOLDER.fullmatch(value.strip())
        return m is not None and m.group(1) == "patient_id"

    if len(segments) >= 2 and segments[0] == "Patient" and _whole(segments[1]):
        return True
    return any(name.strip() in _FHIR_PATIENT_PARAMS and _whole(value) for name, _, value in params)
