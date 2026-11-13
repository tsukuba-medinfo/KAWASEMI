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
# FHIR の検索パラメータ名として許す字種（符号化・空白・記号は許さない）
_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]*")


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

    次のクエリは絞っていても認めない:
    - パスに `%`（符号化した `.` `/`、二重の符号化）、`.` / `..` の区間
    - 検索パラメータ**名**に、字種（英数字と `_ . : -`）以外が含まれる（`%5Finclude` のような符号化、
      空白）。名前も値も**前後の空白を落とさない**（`patient ={{patient_id}}` は別の名前）
    - `_include` / `_revinclude` / `_has` / `_filter` / `_query`（絞った資源から別の資源を
      連れてくる、または条件を差し替える。名前は大文字小文字を区別せず見る）

    パラメータの**値**の `%` は許す（`code=http%3A%2F%2F...` のような符号化は普通に使う）。
    値の中の `&` や `.` はサーバーが符号化を戻しても、パラメータやパスの区切りにはならない。
    """
    path, _, qs = query.partition("?")
    if "%" in path:
        return False
    segments = [seg for seg in path.split("/") if seg]
    if any(seg in (".", "..") for seg in segments):
        return False
    params = [p.partition("=") for p in qs.split("&") if p]
    for name, _, _ in params:
        if not _PARAM_NAME.fullmatch(name):
            return False
        if name.lower().split(":")[0].startswith(_FHIR_WIDENING_PARAMS):
            return False

    def _whole(value: str) -> bool:
        m = _PLACEHOLDER.fullmatch(value)
        return m is not None and m.group(1) == "patient_id"

    if len(segments) >= 2 and segments[0] == "Patient" and _whole(segments[1]):
        return True
    return any(name in _FHIR_PATIENT_PARAMS and _whole(value) for name, _, value in params)
