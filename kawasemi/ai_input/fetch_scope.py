"""ファイル・FHIR の取得 step が患者で絞られているかの検査（F7 のうち SQL に依らない部分）

認可を通った**1人の患者**だけを取得するために、プランのパス・クエリが患者キー
（組み込みの `{{patient_id}}`）で決まることを要求する。SQL は対象外である
（`kawasemi.ai_input.executor` の「取得 step の患者の範囲」を参照）。

プレースホルダは組み込みの `{{patient_id}}` だけを認める。ほかのパラメータ（前段 step の出力を含む）は
患者に結び付く保証が無いので、患者キーの代わりにならない。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# FHIR の検索パラメータ名は大文字小文字を区別する（`Patient=` は別のパラメータ）
_FHIR_PATIENT_PARAMS = {"patient", "subject"}
# 絞った結果に**ほかの資源を連れてくる・条件を差し替える**パラメータ
_FHIR_WIDENING_PARAMS = ("_include", "_revinclude", "_has", "_filter", "_query")
# FHIR の検索パラメータ名として許す字種（符号化・空白・記号は許さない）
_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]*")
# FHIR のリソース型名（先頭大文字の英字だけ）
_RESOURCE_TYPE = re.compile(r"[A-Z][A-Za-z]*")
# `/Patient/{{patient_id}}` の下に続けてよい区間: リソース型、または `$operation`
_PATH_AFTER_PATIENT = re.compile(r"[A-Z][A-Za-z]*|\$[A-Za-z][A-Za-z0-9\-]*")


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
    """FHIR クエリが患者キーで絞る形か。**次の2つの形だけ**を認める。

    1. **資源の検索**: パスがリソース型1つだけ（`/Observation`）で、検索パラメータ
       `patient` / `subject`（小文字のとおり）が**ちょうど1つ**あり、値が**ちょうど**
       `{{patient_id}}`。
    2. **患者の下のパス**: `/Patient/{{patient_id}}`（`Patient` も大文字小文字のとおり）で始まり、
       続くパスはリソース型か `$operation`（`/Patient/{{patient_id}}/$everything`、
       `/Patient/{{patient_id}}/Observation`）。

    次は、絞っていても認めない:
    - パスが型レベルの操作（`/Patient/$everything?patient=...`）。`patient` が入力パラメータでない
      操作は、パラメータを無視して**全患者**を返しうる。`$` の区間は、形 2 の患者の下だけで許す。
    - `patient` / `subject` を表すパラメータが、2つ以上（`patient=...&patient=OTHER`、
      `patient=...&subject=OTHER`）、または修飾付き（`patient:Patient=`、`subject.identifier=`、
      大文字小文字違いの `Patient=` など）で混ざる。サーバーが最後の値や別の名前を採る恐れがある。
    - `_id`（資源を直接指す）、および `_include` / `_revinclude` / `_has` / `_filter` / `_query`
      （絞った資源から別の資源を連れてくる、または条件を差し替える）。名前は大文字小文字を区別せず見る。
    - `#`、空白、制御文字（プレースホルダの波括弧の中の空白を除く）
    - パスに `%`（符号化した `.` `/`、二重の符号化）、`.` / `..` の区間、空の区間
    - 検索パラメータ**名**に、字種（英数字と `_ . : -`）以外が含まれる（`%5Finclude` のような符号化、
      空白）。名前も値も**前後の空白を落とさない**（`patient ={{patient_id}}` は別の名前）

    パラメータの**値**の `%` は許す（`code=http%3A%2F%2F...` のような符号化は普通に使う）。
    値の中の `&` や `.` はサーバーが符号化を戻しても、パラメータやパスの区切りにはならない。
    """
    # `#` 以降は、HTTP クライアントが送る前に落とす（`...?code=x#&patient=...` や
    # `...#?patient=...` は、絞りの文字列が送られず、絞らない要求になる）。空白・制御文字も、
    # 検査が見た文字列と送られる要求を食い違わせる（`{{ patient_id }}` の波括弧の中の空白は除く）
    bare = _PLACEHOLDER.sub("", query)
    if "#" in bare or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in bare):
        return False
    path, _, qs = query.partition("?")
    if "%" in path:
        return False
    segments = path.split("/")
    if segments and segments[0] == "":
        segments = segments[1:]  # 先頭の `/` は、あってもなくてもよい（索引の規約）
    if not segments or any(seg in ("", ".", "..") for seg in segments):
        return False
    params = [p.partition("=") for p in qs.split("&") if p]
    patient_params: list[tuple[str, str]] = []
    for name, _, value in params:
        if not _PARAM_NAME.fullmatch(name):
            return False
        lowered = name.lower()
        if (
            lowered.split(":")[0].startswith(_FHIR_WIDENING_PARAMS)
            or lowered.split(":")[0] == "_id"
        ):
            return False
        # `patient` / `subject` の系統（修飾子 `:`、チェイン `.`、大文字小文字違いを含む）
        if re.split(r"[:.]", lowered, maxsplit=1)[0] in _FHIR_PATIENT_PARAMS:
            patient_params.append((name, value))

    def _whole(value: str) -> bool:
        m = _PLACEHOLDER.fullmatch(value)
        return m is not None and m.group(1) == "patient_id"

    # 形 2: 患者の下のパス。絞りはパスで決まるので、`patient` / `subject` は書かなくてよいが、
    # 書くなら 1 つだけで、同じ患者キーでなければならない。
    if len(segments) >= 2 and segments[0] == "Patient" and _whole(segments[1]):
        rest = segments[2:]
        if len(rest) > 2 or not all(_PATH_AFTER_PATIENT.fullmatch(seg) for seg in rest):
            return False
        return all(name in _FHIR_PATIENT_PARAMS and _whole(v) for name, v in patient_params) and (
            len(patient_params) <= 1
        )
    # 形 1: リソース型1つの検索。型レベルの `$operation` は認めない。
    if len(segments) != 1 or not _RESOURCE_TYPE.fullmatch(segments[0]):
        return False
    return (
        len(patient_params) == 1
        and patient_params[0][0] in _FHIR_PATIENT_PARAMS
        and _whole(patient_params[0][1])
    )


class FhirUrlError(ValueError):
    """FHIR のクエリから、設定したサーバー以外を指す URL ができてしまう。"""


def fhir_request_url(base_url: str, query_path: str) -> str:
    """設定の FHIR サーバーの URL と、描画済みのクエリから、要求する URL を作る（F21）。

    クエリの**リテラル部分**は設計者（または LLM）が書く。`base_url + query` の素の連結だと、
    ベース URL にパスが無いとき、`@host/x`（ユーザー情報）や `.evil.tld/x`（ホスト名の後ろ）で
    要求先のホストが変わり、FHIR の認可ヘッダーごと別のホストへ送られる。

    - 先頭の `/` が無ければ補う（索引の `Observation?...` と同じ規約。executor の規約は `/` 始まり）。
    - `//` で始まるもの、バックスラッシュ、スキーム（`:` が最初の `/` `?` より前）は拒否する。
    - 組み立てた URL を解析し、スキーム・ホスト（ユーザー情報を含む。ベース URL 自身のものだけ）がベース URL と同じで、
      パスがベース URL のパスの下にあることを確かめる（上の規則が破れても残る最後の線）。
    """
    base = base_url.rstrip("/")
    path = query_path if query_path.startswith("/") else f"/{query_path}"
    # 空白・制御文字（CR/LF/タブ/NUL を含む）は、URL の構造やヘッダーを変える
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in path):
        raise FhirUrlError("FHIR クエリに空白・制御文字があります")
    if path.startswith("//") or "\\" in path:
        raise FhirUrlError("FHIR クエリの先頭が不正です")
    path_only = path.split("?", 1)[0].split("#", 1)[0]
    # パスの `.` / `..` の区間と、符号化した `.` `/` `\`（`%2e%2e` など）は、ベース URL の下から出る
    if any(seg in (".", "..") for seg in path_only.split("/")) or re.search(
        r"%(2e|2f|5c)", path_only, re.IGNORECASE
    ):
        raise FhirUrlError("FHIR クエリのパスに、辿り直しを起こす部分があります")
    head = path[1:].split("?", 1)[0].split("/", 1)[0]
    if ":" in head or "@" in head:
        raise FhirUrlError("FHIR クエリの先頭に、スキームまたはユーザー情報に見える部分があります")
    url = f"{base}{path}"
    got, want = urlsplit(url), urlsplit(base)
    # ユーザー情報はベース URL 自身のものだけ（ベースに `user:pw@` があっても通す。クエリが
    # 持ち込んだものは、上の先頭の検査と、ここの netloc の一致で拒否される）
    if (
        (got.scheme, got.netloc) != (want.scheme, want.netloc)
        or (got.username, got.password) != (want.username, want.password)
        or not got.path.startswith(want.path.rstrip("/") + "/")
    ):
        raise FhirUrlError("FHIR クエリが、設定したサーバー以外を指しています")
    return url
