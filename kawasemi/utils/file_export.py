"""ファイルエクスポートユーティリティ"""

import copy
import json
import re
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kawasemi.ai_input.resource_index.derive import format_citation_source
from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# ファイル名に使える文字（FHIR id は [A-Za-z0-9-.]{1,64} だが、登録時に
# クライアント提供値をそのまま採用する経路があるため、出力時にも防御的に
# サニタイズしてパストラバーサル/Zip Slip を防ぐ）
_FHIR_ID_MAX_LEN = 64
_SAFE_FHIR_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.")


def safe_fhir_id(fhir_id: str | int | None) -> str:
    """fhir_id をファイル名/ZIP エントリ名に使える安全な文字列へ正規化する。

    許可文字 [A-Za-z0-9-.] 以外を "_" に置換し、`..` や区切り文字による
    ディレクトリ遡上を無効化する。空になった場合は "unknown" を返す。
    """
    raw = str(fhir_id) if fhir_id is not None else ""
    cleaned = "".join(c if c in _SAFE_FHIR_ID_CHARS else "_" for c in raw)
    # 先頭・末尾のドットを除去し ".."（親ディレクトリ）由来の悪用を防ぐ。切り詰め後に
    # 末尾へドットが復活しうるため（例: "aaa...aaa..b" は切り詰めで "aaa...aaaaa." に
    # なる）、切り詰めた後に再度 strip する。
    cleaned = cleaned.strip(".")[:_FHIR_ID_MAX_LEN].strip(".")
    return cleaned or "unknown"


def ensure_output_directories() -> None:
    """出力ディレクトリが存在することを確認（存在しなければ作成）"""
    settings.questionnaire_output_dir_path.mkdir(parents=True, exist_ok=True)
    settings.questionnaire_response_output_dir_path.mkdir(parents=True, exist_ok=True)
    # **作ったディレクトリをそのまま出す。** 以前は `settings.output_dir` を出していたが、
    # 作るのはその下の2つで、別の場所を指していても気付けなかった。
    logger.info(
        "出力ディレクトリを確認: "
        f"{settings.questionnaire_output_dir_path} / "
        f"{settings.questionnaire_response_output_dir_path}"
    )


def export_questionnaire_to_file(
    questionnaire_id: int | str,
    fhir_id: str,
    content: dict[str, Any],
) -> Path:
    """
    Questionnaire を JSON ファイルとして出力

    Args:
        questionnaire_id: QuestionnaireのデータベースID
        fhir_id: QuestionnaireのFHIR ID
        content: Questionnaireの内容（FHIR JSON）

    Returns:
        Path: 出力されたファイルのパス
    """
    ensure_output_directories()

    # ファイル名の生成: Questionnaire_{fhir_id}_{timestamp}.json
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"Questionnaire_{safe_fhir_id(fhir_id)}_{timestamp}.json"
    filepath = settings.questionnaire_output_dir_path / filename

    # JSON ファイルとして出力
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    logger.info(f"Questionnaireをファイルに出力: {filepath}")
    return filepath


CLINICAL_ANNOTATION_EXTENSION_URL = (
    "https://kawasemi.example.jp/fhir/StructureDefinition/clinical-annotation"
)

# 複合 Extension の下位要素名。FHIR の慣例どおり、下位 extension の url は
# 親の canonical URL からの相対名（単純名）にする。**定義は
# `fhir/structuredefinitions/kawasemi-clinical-annotation.json`（StructureDefinition）が
# 正で、この定数と一致していることをテストで固定する。**
SUB_ANNOTATION = "annotation"
SUB_CITATION = "citation"
SUB_CITATION_UNIT_ID = "unitId"
SUB_CITATION_SOURCE = "source"
SUB_CITATION_PAGE = "page"
SUB_CITATION_CHAR_RANGE = "charRange"
SUB_CITATION_MIME = "mime"
SUB_CITATION_LLM_MODEL = "llmModel"


# 典拠 payload のうち**施設外へ出してよいキー**（許可リスト）。
#
# **許可リストであって拒否リストではない。** payload は型検査を通っていない開いた dict で
# （`routes._clean_citation` は dict かどうかしか見ない）、キーを供給する索引側は indexer
# （`indexer/`）である。拒否リストにすると、索引が payload にキーを1つ足した瞬間に
# それが施設外へ出る。
#
# 出さないもの（理由）:
# - `snippet`: **規約文書の本文そのもの**。患者データを含む文書が誤って reference として
#   索引された場合の直接の漏洩面であり、第三者著作物の逐語配布でもある。UI から人が任意の
#   文字列を書ける欄でもあるので、AI 経路より混入の危険が高い。
# - `connection_id`: 施設のインフラ識別子。
# - `label`: 出典表記だが**クライアントから送られた値**で検証されていない。配布する表記は
#   `path` / `page` / `char_range` から**組み立て直す**（`build_citation_source` と同じ形）。
# - 他 kind（db / master / fhir / patient_data）の payload キー全般: 施設固有のシステム内部
#   構造で、そもそも施設非依存であるはずの臨床的意味に混ざっていること自体が異常。
#
# **この集合は飾りではなく、下位要素を作る関数表そのものから導出する。** 許可リストを別に
# 書くと、リストから外しても出力側が別経路で出し続ける（＝守っているつもりで守っていない）
# 状態が作れてしまう。キーを配布から外すには、下の表からその行を消すしかない。
def _part_page(value: Any) -> dict[str, Any] | None:
    # bool は int の派生なので明示的に弾く（`True` が「1ページ目」になるのを防ぐ）。
    # 負のページは存在しないので、形として弾く。
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return {"url": SUB_CITATION_PAGE, "valueInteger": value}
    return None


def _part_char_range(value: Any) -> dict[str, Any] | None:
    # **`[start, end]`（整数ちょうど2個）だけを載せる。** 読み込み側は `start-end` しか
    # 戻せないので、それ以外を載せると往復で消える（書き出しだけ緩いと非可逆になる）。
    if _as_char_range(value) is None:
        return None
    start, end = _as_char_range(value) or (0, 0)
    return {"url": SUB_CITATION_CHAR_RANGE, "valueString": f"{start}-{end}"}


def _as_char_range(value: Any) -> tuple[int, int] | None:
    """`[start, end]`（bool でない整数2個）なら組にして返す。"""
    if not isinstance(value, list | tuple) or len(value) != 2:
        return None
    start, end = value
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if start < 0 or end < start:
        return None
    return start, end


# MIME タイプの形（`type/subtype`）。**許可キーであることと、値が安全であることは別**なので、
# 値の形も見る（`payload` は保存 API から任意の文字列を書ける）。
#
# **文字クラスは ASCII で書き下す。** Python の `\w` は仮名・漢字を含むので、`[\w.+-]*` と
# 書くと `application/pdf患者山田太郎…` が通る（同じ罠が過去に日本語の `\b` で起きている）。
_MIME_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")

# LLM のモデル名の形（例 `qwen3:8b`, `gemma3:27b-it-q4_K_M`）。
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")


def _part_mime(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str) and _MIME_TOKEN.match(value.strip()):
        return {"url": SUB_CITATION_MIME, "valueString": value.strip()}
    return None


# payload のキー → 下位 Extension を作る関数。
#
# **ここに無いキーは配布物に一切出ない。** `path` と `file_role` を載せていないのは意図的で、
# - `path`: 受け手へは `unitId` と `source` を通じて届く。どちらも**形を検査した `unit_id`
#   から決まる**ので、保存 API から書ける `payload["path"]` は配布に一切関与しない。
#   （`unit_id` の検査は形だけで、索引への実在確認ではない。`_REFERENCE_UNIT_ID` を参照。）
# - `file_role`: 値を出すためではなく**配布可否の判定**に使う（`is_distributable_citation`）。
_CITATION_PART_BUILDERS: dict[str, Callable[[Any], dict[str, Any] | None]] = {
    "page": _part_page,
    "char_range": _part_char_range,
    "mime": _part_mime,
}

EXPORTABLE_CITATION_PAYLOAD_KEYS: frozenset[str] = frozenset(_CITATION_PART_BUILDERS)

# 規約文書チャンクの `unit_id` の形（索引側 indexer の `file_indexer` が作る）。
# `file:{path}#p{page}:{start}-{end}` または `file:{path}:{start}-{end}`（ページ無し）。
#
# **これは形だけの検査で、索引に実在するかは見ていない。** パス部分は空白・制御文字を
# 禁じ、長さを 200 に制限して自由文が紛れ込む面を狭めているが、空白を含まない任意の
# 文字列（患者名を詰めたもの）は通りうる。実在確認は書き出し時ではなく
# 保存時に行う（`is_distributable_citation` を参照）。
_REFERENCE_UNIT_ID = re.compile(r"^file:(?P<path>[^\s\x00-\x1f\u3000#]{1,200}?)(?:#p\d+)?:\d+-\d+$")


# 取り込んだ典拠に付ける来歴。「配布された Extension から入った」ことを表す
# （`file_role` を捏造せずに再配布可否を判断するため）。
ORIGIN_IMPORTED = "imported-extension"


# `unit_id` に**現れてよい文字**を Unicode カテゴリで絞る。
#
# **正規表現の否定クラスに文字を足していく形では網羅が終わらない。** 実際に
# `[^\s\x00-\x1f\u3000#]` は**ゼロ幅空白（U+200B）を通す**（Python の `\s` は Unicode の
# 空白だけで、`Cf`（format）は含まない）。レビューで
# `file:患者<U+200B>山田太郎:0-0` が形式検査を通ることが実測された。
#
# **許可するカテゴリを決める**（fail-closed）。落とすのは制御文字（`Cc`）・書式文字（`Cf`）・
# 区切り（`Zs` / `Zl` / `Zp`）・未割り当て（`Cn`）・私用領域（`Co`）・サロゲート（`Cs`）である。
# 索引の実在確認が入ったのでこの層が無くても捏造は配布されないが、**索引を参照できない環境では
# こちらだけが働く**（層を2つ持つ）。
_FORBIDDEN_UNIT_ID_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Co", "Cs", "Zs", "Zl", "Zp"})


def has_forbidden_unit_id_chars(text: str) -> bool:
    """`unit_id` に制御文字・書式文字・区切り文字が含まれるか。

    **正規表現では書けない**（Python の `re` は Unicode カテゴリを取らない）ので、
    `unicodedata.category` で見る。**判定は許可リストではなく「禁止カテゴリ」だが、
    カテゴリ単位なので個別の文字を追いかける形にはならない**（`Cf` はゼロ幅空白・
    双方向制御・異体字セレクタ等をまとめて覆う）。
    """
    return any(unicodedata.category(ch) in _FORBIDDEN_UNIT_ID_CATEGORIES for ch in text)


def _unit_id_path(unit_id: str) -> str:
    """形を検査済みの `unit_id` からパス部分を取り出す（実在確認はしていない）。"""
    matched = _REFERENCE_UNIT_ID.match(unit_id.strip())
    return matched.group("path") if matched else unit_id


def is_distributable_citation(citation: dict[str, Any]) -> bool:
    """この典拠を施設外へ配布してよいか（**フェイルクローズ**）。

    配布するのは「索引された規約文書のチャンク」だけに限る。理由:

    - **`unit_id` は UI の自由入力欄**（`design_clinical.html` の典拠エディタ）で、人が
      「患者 山田太郎のカルテより」と書ける。許可リストは payload の**キー**を守るだけで、
      `unit_id` の**値**は守らない。形が索引のアドレスと一致するものだけ載せる。
      **この述語は索引を引かない。** 実在の照合は**保存時**に行う
      （`routes._verified_citation`）ので、照合の導入以降に新しく書かれた典拠については
      「索引に実在する」が payload の来歴で担保される。**既存レコードには掛かっていない**
      ので、この述語だけを見て「配布できる＝索引に実在する」とは読めない。既存レコードは
      **アドレスが索引に実在すれば保存し直しで洗浄される**（規則1）が、**実在しないアドレスの
      記録は規則2 が残すので洗浄されない**（一度きりの再照合で扱う）。
    - **`file_role` が `reference` 以外の典拠は配布しない。** 患者データを指す典拠
      （`patient_data`）が臨床的意味に混ざっていること自体が異常で、それを施設外へ出すのは
      二重に誤り。AI 経路は `is_reference_unit` で絞るが、保存ルートは絞らない。

    配布できない典拠は**手元に残る**（`routes._merge_imported_citations` が消さない）。
    """
    unit_id = citation.get("unit_id")
    if not isinstance(unit_id, str) or not _REFERENCE_UNIT_ID.match(unit_id.strip()):
        return False
    # **書式文字を含むアドレスは配布しない**。`_REFERENCE_UNIT_ID` の否定クラスは
    # ゼロ幅空白（U+200B）を通すので、カテゴリで落とす層をもう1つ置く。
    if has_forbidden_unit_id_chars(unit_id.strip()):
        return False
    payload = citation.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    # **フェイルクローズ。** 由来が分からないものは配布しない（`file_role` が無い＝
    # `unset` を拒む方針は `design_provenance` のガードと揃える）。
    #
    # ただし、**配布された Extension から取り込んだ典拠は `file_role` を持たない**
    # （復元すると由来の捏造になるので、意図的に補っていない）。受け取った施設が再配布
    # できなくなるのを避けるため、取り込み時に付ける来歴 `origin` を第2の根拠として認める。
    # どちらも保存 API から書ける自己申告だが、**値そのものが配布物へ出るわけではない**
    # （配布に出るのは unitId / source / page / charRange / mime / llmModel の6つだけで、
    # `file_role` も `origin` もその中に無い）。
    return payload.get("file_role") == "reference" or payload.get("origin") == ORIGIN_IMPORTED


def _exportable_citation(citation: dict[str, Any]) -> dict[str, Any] | None:
    """典拠1件を配布用の下位 Extension へ変換する（許可キーのみ・出典表記は再計算）。

    配布して良くない典拠（`is_distributable_citation` が偽）は None（載せない）。
    """
    if not is_distributable_citation(citation):
        return None
    unit_id = str(citation["unit_id"]).strip()
    raw_payload = citation.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

    # **配布物に載せる値は1本の経路で作る。** 下位要素を作る経路と出典表記を作る経路が
    # 2本あると、片方だけ検査して安心する（実際に2度続けて起きた形）。
    # まず検査を通った部品を作り、**出典表記はその部品からだけ**組み立てる。
    parts: list[dict[str, Any]] = [{"url": SUB_CITATION_UNIT_ID, "valueString": unit_id}]
    for key, build in _CITATION_PART_BUILDERS.items():
        part = build(payload.get(key))
        if part is not None:
            parts.append(part)
    emitted = {part["url"]: part for part in parts}

    # 出典表記は保存値（`payload["label"]`）ではなく毎回組み立て直す。保存値はクライアント
    # 由来で検証されていない。パスは `unit_id` から取る（`payload["path"]` を優先すると
    # `unit_id` の形式検査を迂回できる）。
    parts.insert(
        1,
        {
            "url": SUB_CITATION_SOURCE,
            "valueString": format_citation_source(
                path=_unit_id_path(unit_id),
                page=emitted.get(SUB_CITATION_PAGE, {}).get("valueInteger"),
                char_range=emitted.get(SUB_CITATION_CHAR_RANGE, {}).get("valueString"),
            ),
        },
    )

    llm_model = citation.get("llm_model")
    # AI 利用の開示に必要。**ただし値は無検査ではない**——`_clean_citation` は strip する
    # だけなので、保存 API から任意の文字列を書ける。モデル名の形（英数字と `.:_+-`）に
    # 合うものだけ載せる（コメントで「モデル名だけ」と主張するなら、それを強制する）。
    if isinstance(llm_model, str) and _MODEL_NAME.match(llm_model.strip()):
        parts.append({"url": SUB_CITATION_LLM_MODEL, "valueString": llm_model.strip()})
    return {"url": SUB_CITATION, "extension": parts}


def embed_clinical_annotations(
    content: dict[str, Any],
    annotations_map: dict[str, str],
    citations_map: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], int]:
    """臨床的コンテキストアノテーションを各 item の FHIR Extension として埋め込んだ
    Questionnaire content（dict）を返す。

    施設非依存の臨床的コンテキストのみを埋め込む（システム場所情報は含めない）。
    既存の clinical-annotation Extension は置き換える（冪等）。

    **複合 Extension**: 記述だけでなく典拠も同じ Extension の下位要素として
    運ぶ。受け取った施設が記述の根拠を辿れるようにするため。典拠の payload は
    `EXPORTABLE_CITATION_PAYLOAD_KEYS` の許可キーだけを載せる（`snippet` = 規約文書の本文は
    **載せない**）。

    Args:
        content: Questionnaire の内容（FHIR JSON）
        annotations_map: {link_id: annotation テキスト}
        citations_map: {link_id: 典拠のリスト}（省略時は典拠なし）

    Returns:
        (Extension を埋め込んだ content の deep copy, **実際に埋め込んだ典拠の件数**)

        件数を返すのは、配布できない典拠（`is_distributable_citation` が偽）が落ちるため。
        手元の典拠の数をそのまま報告すると、監査ログと API 応答が実態とずれる。
    """
    annotated = copy.deepcopy(content)
    embedded_citations = 0

    def _annotate_items(items: list[dict[str, Any]]) -> None:
        for item in items:
            link_id = item.get("linkId")
            # 既存の clinical-annotation Extension を一旦除去
            existing = [
                e
                for e in item.get("extension", [])
                if e.get("url") != CLINICAL_ANNOTATION_EXTENSION_URL
            ]
            annotation = annotations_map.get(link_id) if link_id else None
            if annotation:
                parts: list[dict[str, Any]] = [{"url": SUB_ANNOTATION, "valueString": annotation}]
                for citation in (citations_map or {}).get(link_id or "", []):
                    sub = _exportable_citation(citation) if isinstance(citation, dict) else None
                    if sub:
                        parts.append(sub)
                        nonlocal embedded_citations
                        embedded_citations += 1
                existing.append(
                    {
                        "url": CLINICAL_ANNOTATION_EXTENSION_URL,
                        "extension": parts,
                    }
                )
            if existing:
                item["extension"] = existing
            elif "extension" in item:
                del item["extension"]
            if "item" in item:
                _annotate_items(item["item"])

    if "item" in annotated:
        _annotate_items(annotated["item"])

    return annotated, embedded_citations


def _citation_from_extension(sub: dict[str, Any]) -> dict[str, Any] | None:
    """配布された下位 Extension 1件を、保存形（`{unit_id, payload, llm_model}`）へ戻す。

    載っていないキー（`snippet` など）は復元しない。**受け取った施設にとっては最初から
    存在しない情報**であり、書き出した施設には手元の値が残っている（`routes` 側の取り込みが
    unit_id で突き合わせて保持する）。
    """
    unit_id: str | None = None
    payload: dict[str, Any] = {}
    llm_model: str | None = None
    for part in sub.get("extension", []):
        if not isinstance(part, dict):
            continue
        url = part.get("url")
        if url == SUB_CITATION_UNIT_ID:
            unit_id = part.get("valueString")
        elif url == SUB_CITATION_PAGE:
            page = part.get("valueInteger")
            if isinstance(page, int) and not isinstance(page, bool):
                payload["page"] = page
        elif url == SUB_CITATION_CHAR_RANGE:
            text = part.get("valueString")
            if isinstance(text, str) and "-" in text:
                start, _, end = text.partition("-")
                if start.strip().isdigit() and end.strip().isdigit():
                    payload["char_range"] = [int(start), int(end)]
        elif url == SUB_CITATION_MIME:
            mime = part.get("valueString")
            if isinstance(mime, str) and mime.strip():
                payload["mime"] = mime.strip()
        elif url == SUB_CITATION_LLM_MODEL:
            llm_model = part.get("valueString")
    if unit_id:
        # path は unit_id から決まる（`file:{path}#p{page}:{start}-{end}`）ので二重には
        # 載せず、取り込み側で unit_id から取り出す。**`file_role` は復元しない**——
        # 配布物に無いものを補うのは由来の捏造で、`reference` と書けば下流の検査
        # （`is_reference_unit` など）をすり抜ける形を自分で作ることになる。
        path = _path_from_unit_id(unit_id)
        if path:
            payload["path"] = path
        # **由来を記録する（捏造ではない）。** `file_role` は配布物に無いので復元しないが、
        # 「配布された Extension から入った」ことは事実として書ける。受け取った施設は
        # これを根拠に再配布できる。
        payload["origin"] = ORIGIN_IMPORTED
    if not unit_id and not payload:
        return None
    return {"unit_id": unit_id, "payload": payload or None, "llm_model": llm_model}


def _path_from_unit_id(unit_id: str) -> str | None:
    """`file:{path}#p{page}:{start}-{end}` から path を取り出す（形が違えば None）。"""
    if not unit_id.startswith("file:"):
        return None
    path = unit_id[len("file:") :].split("#", 1)[0]
    return path or None


@dataclass(frozen=True)
class ExtractedAnnotations:
    """`extract_clinical_annotations` の戻り値。

    `citations` は**複合形式の Extension があった項目にだけキーが立つ**（典拠0件でも空リスト）。
    旧形式（`valueString` 単一値）の項目はキーが立たず、`legacy_link_ids` に入る。この違いは
    取り込み側の分岐そのもの（旧形式は典拠を運んでいないので手元の典拠に触らない）なので、
    **不在で意味を運ばずに明示する**。`citations.get(link_id, [])` と書かれると「旧形式を
    読んだら手元の典拠が全部消える」バグが復活する。
    """

    annotations: dict[str, str]
    legacy_link_ids: frozenset[str]
    # **生の dict は公開しない。** `citations.get(link_id, [])` と書けば「旧形式を読んだら
    # 手元の典拠が全部消える」バグが復活する。読むときは `citations_for()` を通す。
    _citations: dict[str, list[dict[str, Any]]]

    def citations_for(self, link_id: str) -> list[dict[str, Any]] | None:
        """その項目の典拠。**None は「運んでいない」（旧形式）**で、`[]` は「0件」。"""
        return self._citations.get(link_id)

    def link_ids_with_citations(self) -> frozenset[str]:
        """典拠を運んできた項目（典拠0件の項目も含む）。"""
        return frozenset(self._citations)


def extract_clinical_annotations(content: dict[str, Any]) -> ExtractedAnnotations:
    """Questionnaire content から臨床的コンテキストアノテーション Extension を抽出する。

    **旧形式（`valueString` 単一値）も読める**（複合 Extension 以前に書き出された Questionnaire。
    その場合、典拠は空になる）。

    Args:
        content: Questionnaire の内容（FHIR JSON）

    Returns:
        `ExtractedAnnotations`（記述・典拠・旧形式だった link_id）
    """
    annotations: dict[str, str] = {}
    citations: dict[str, list[dict[str, Any]]] = {}
    legacy: set[str] = set()

    def _walk(items: list[dict[str, Any]]) -> None:
        for item in items:
            link_id = item.get("linkId")
            if link_id:
                for ext in item.get("extension", []):
                    if ext.get("url") != CLINICAL_ANNOTATION_EXTENSION_URL:
                        continue
                    # 旧形式: valueString 単一値（典拠なし）
                    value = ext.get("valueString")
                    if value:
                        annotations[link_id] = value
                        legacy.add(link_id)
                        break
                    found: list[dict[str, Any]] = []
                    for sub in ext.get("extension", []):
                        if not isinstance(sub, dict):
                            continue
                        if sub.get("url") == SUB_ANNOTATION:
                            text = sub.get("valueString")
                            if text:
                                annotations[link_id] = text
                        elif sub.get("url") == SUB_CITATION:
                            citation = _citation_from_extension(sub)
                            if citation:
                                found.append(citation)
                    if link_id in annotations:
                        # **複合形式なら典拠0件でもキーを立てる。** 「典拠が無い」と
                        # 「典拠を運ばない形式だった」は違う。旧形式（valueString）は上の
                        # 分岐で抜けるのでここへ来ず、キーが立たない＝取り込み側は手元の
                        # 典拠を保持する。
                        citations[link_id] = found
                    break
            if "item" in item:
                _walk(item["item"])

    if "item" in content:
        _walk(content["item"])

    return ExtractedAnnotations(
        annotations=annotations, legacy_link_ids=frozenset(legacy), _citations=citations
    )


def export_questionnaire_response_to_file(
    response_id: int | str,
    fhir_id: str,
    content: dict[str, Any],
) -> Path:
    """
    QuestionnaireResponse を JSON ファイルとして出力

    Args:
        response_id: QuestionnaireResponseのデータベースID
        fhir_id: QuestionnaireResponseのFHIR ID
        content: QuestionnaireResponseの内容（FHIR JSON）

    Returns:
        Path: 出力されたファイルのパス
    """
    ensure_output_directories()

    # ファイル名の生成: QuestionnaireResponse_{fhir_id}_{timestamp}.json
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"QuestionnaireResponse_{safe_fhir_id(fhir_id)}_{timestamp}.json"
    filepath = settings.questionnaire_response_output_dir_path / filename

    # JSON ファイルとして出力
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    logger.info(f"QuestionnaireResponseをファイルに出力: {filepath}")
    return filepath


def get_questionnaire_export_path(fhir_id: str) -> Path | None:
    """
    Questionnaire の最新エクスポートファイルのパスを取得

    Args:
        fhir_id: QuestionnaireのFHIR ID

    Returns:
        Path: エクスポートファイルのパス（存在しない場合はNone）
    """
    # 書き込み側（export_questionnaire_to_file）が safe_fhir_id でファイル名を作るので、
    # 探す側も同じ正規化を通す。通さないと (1) 正規化で文字が置換された ID のファイルを
    # 取り逃し、(2) fhir_id に `../` を含む値が来たとき出力ディレクトリの外を
    # 走査できる。
    pattern = f"Questionnaire_{safe_fhir_id(fhir_id)}_*.json"
    files = list(settings.questionnaire_output_dir_path.glob(pattern))
    if files:
        # 最新のファイルを返す
        return max(files, key=lambda p: p.stat().st_mtime)
    return None


def get_questionnaire_response_export_path(fhir_id: str) -> Path | None:
    """
    QuestionnaireResponse の最新エクスポートファイルのパスを取得

    Args:
        fhir_id: QuestionnaireResponseのFHIR ID

    Returns:
        Path: エクスポートファイルのパス（存在しない場合はNone）
    """
    # 書き込み側と同じ正規化を通す（理由は get_questionnaire_export_path と同じ）。
    pattern = f"QuestionnaireResponse_{safe_fhir_id(fhir_id)}_*.json"
    files = list(settings.questionnaire_response_output_dir_path.glob(pattern))
    if files:
        # 最新のファイルを返す
        return max(files, key=lambda p: p.stat().st_mtime)
    return None


def export_questionnaire_responses_to_zip(
    responses: list[dict[str, Any]],
) -> Path:
    """
    複数の QuestionnaireResponse を ZIP ファイルとしてエクスポート

    Args:
        responses: QuestionnaireResponseのリスト（各要素は id, fhir_id, content を含む）

    Returns:
        Path: 作成されたZIPファイルのパス
    """
    ensure_output_directories()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"QuestionnaireResponses_{timestamp}.zip"
    zip_path = settings.questionnaire_response_output_dir_path / zip_filename

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for response in responses:
            fhir_id = response.get("fhir_id", str(response.get("id", "unknown")))
            content = response.get("content", {})

            # JSONファイル名（Zip Slip 防止のためサニタイズ）
            json_filename = f"QuestionnaireResponse_{safe_fhir_id(fhir_id)}.json"

            # JSONをZIPに追加
            json_content = json.dumps(content, ensure_ascii=False, indent=2)
            zf.writestr(json_filename, json_content)

    logger.info(f"{len(responses)}件のQuestionnaireResponseをZIPにエクスポート: {zip_path}")
    return zip_path


def create_bulk_export_zip(
    questionnaire_responses: list[tuple[str, dict[str, Any]]],
) -> Path:
    """
    一括エクスポート用のZIPファイルを作成

    Args:
        questionnaire_responses: (fhir_id, content) のタプルのリスト

    Returns:
        Path: 作成されたZIPファイルのパス
    """
    ensure_output_directories()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"BulkExport_{timestamp}.zip"
    zip_path = settings.questionnaire_response_output_dir_path / zip_filename

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fhir_id, content in questionnaire_responses:
            json_filename = f"QuestionnaireResponse_{safe_fhir_id(fhir_id)}.json"
            json_content = json.dumps(content, ensure_ascii=False, indent=2)
            zf.writestr(json_filename, json_content)

    logger.info(f"{len(questionnaire_responses)}件をZIPにエクスポート: {zip_path}")
    return zip_path
