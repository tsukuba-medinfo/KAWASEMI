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
    - `#`、空白、制御文字（プレースホルダの波括弧の中の空白を除く）
    - パスに `%`（符号化した `.` `/`、二重の符号化）、`.` / `..` の区間
    - 検索パラメータ**名**に、字種（英数字と `_ . : -`）以外が含まれる（`%5Finclude` のような符号化、
      空白）。名前も値も**前後の空白を落とさない**（`patient ={{patient_id}}` は別の名前）
    - `_include` / `_revinclude` / `_has` / `_filter` / `_query`（絞った資源から別の資源を
      連れてくる、または条件を差し替える。名前は大文字小文字を区別せず見る）

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


class FhirUrlError(ValueError):
    """FHIR のクエリから、設定したサーバー以外を指す URL ができてしまう。"""


def fhir_request_url(base_url: str, query_path: str) -> str:
    """設定の FHIR サーバーの URL と、描画済みのクエリから、要求する URL を作る（F21）。

    クエリの**リテラル部分**は設計者（または LLM）が書く。`base_url + query` の素の連結だと、
    ベース URL にパスが無いとき、`@host/x`（ユーザー情報）や `.evil.tld/x`（ホスト名の後ろ）で
    要求先のホストが変わり、FHIR の認可ヘッダーごと別のホストへ送られる。

    - 先頭の `/` が無ければ補う（索引の `Observation?...` と同じ規約。executor の規約は `/` 始まり）。
    - `//` で始まるもの、バックスラッシュ、スキーム（`:` が最初の `/` `?` より前）は拒否する。
    - 組み立てた URL を解析し、スキーム・ホスト（ユーザー情報を含む）がベース URL と同じで、
      パスがベース URL のパスの下にあることを確かめる（上の規則が破れても残る最後の線）。
    """
    base = base_url.rstrip("/")
    path = query_path if query_path.startswith("/") else f"/{query_path}"
    if path.startswith("//") or "\\" in path:
        raise FhirUrlError("FHIR クエリの先頭が不正です")
    head = path[1:].split("?", 1)[0].split("/", 1)[0]
    if ":" in head or "@" in head:
        raise FhirUrlError("FHIR クエリの先頭に、スキームまたはユーザー情報に見える部分があります")
    url = f"{base}{path}"
    got, want = urlsplit(url), urlsplit(base)
    if (
        (got.scheme, got.netloc) != (want.scheme, want.netloc)
        or got.username is not None
        or not got.path.startswith(want.path.rstrip("/") + "/")
    ):
        raise FhirUrlError("FHIR クエリが、設定したサーバー以外を指しています")
    return url
