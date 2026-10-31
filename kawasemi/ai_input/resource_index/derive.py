"""選択された索引ユニット(payload) からの決定的導出

LLM は候補の cand_id を選ぶだけで、location文字列・source_category は本モジュールが
unit.payload から決定的に組み立てる（存在しない表/カラム/パスの捏造を構造的に防ぐ）。
ステップ3（DSLコンパイル）の op 引数も同じ payload から導出する（`derive_op_args`）。

source_category は kind と接続レジストリ(connections.yaml)の scope から決める:
- db: 接続が external なら external_db、それ以外（self/未登録）は local_db。
- master: マスタ表(コード表)はDB常駐の参照語彙で `WHERE <code列>='...'` で引く。
  値の取得元はDBなので db と同じく接続 scope で local_db / external_db を決める。
- fhir: fhir。
- file: file_role=patient_data なら file（実行は file_extract）、
  reference は参照知識なので manual（実行時に値を取らない）。

臨床的意味の典拠は所在とは別軸で扱う。`is_reference_unit` が規約文書の
ユニットを判定し、`build_citation_source` が出典表記を payload から決定的に組み立てる。
所在候補としての reference の扱い（manual へ寄せる）は変えない。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from kawasemi.ai_input.models import OpType, SourceCategory
from kawasemi.ai_input.resource_index.connections import (
    SCOPE_EXTERNAL,
    ConnectionRegistry,
    get_registry,
)
from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit


def derive_source_category(unit: ResourceUnit, registry: ConnectionRegistry | None = None) -> str:
    """ユニットの kind と接続 scope から source_category を導出する。"""
    if unit.kind in (Kind.DB, Kind.MASTER):
        # master もDB常駐のコード表（WHERE code列='...' で引く）なので接続 scope に従う。
        reg = registry if registry is not None else get_registry()
        scope = reg.scope_of(unit.payload.get("connection_id"))
        return SourceCategory.EXTERNAL_DB if scope == SCOPE_EXTERNAL else SourceCategory.LOCAL_DB
    if unit.kind == Kind.FHIR:
        return SourceCategory.FHIR
    if unit.kind == Kind.FILE:
        if unit.payload.get("file_role") == "patient_data":
            return SourceCategory.FILE
        return SourceCategory.MANUAL  # reference は参照知識（実行時に値を取らない）
    return SourceCategory.MANUAL


FILE_ROLE_REFERENCE = "reference"


def is_reference_unit(unit: ResourceUnit) -> bool:
    """規約文書（入力マニュアル・記入の手引き・実施計画書）のユニットか。

    `source_category` の導出では reference を manual（実行時に値を取らない）へ寄せて
    いるが、**臨床的意味の典拠**としては別の扱いをする。所在候補としての扱い
    （derive_source_category）とは目的が違うので、述語を分けて定義する。
    """
    return unit.kind == Kind.FILE and unit.payload.get("file_role") == FILE_ROLE_REFERENCE


# 目次・項目一覧のチャンクの目印。原典は目次を「項目名 ·········· ページ番号」の形で組むので、
# **リーダ（点の連なり）**が現れる。
#
# **文字は1種類に決め打ちできない。** 文書によって中黒（`·`）で組まれることも
# **ASCII ピリオド**で組まれることもある（片方を取りこぼすと、目次のチャンクが
# 本文として残り、検索の上位を占める）。
#
# **4文字以上の連なりに限る。** 本文は省略として `・・・`（3文字）や `…`（1文字）を使うので、
# 3文字以下を目印にすると本文を落とす。4文字以上なら、**落ちるのは目次のページだけで、
# 定義表の見出し（例 `項目区分`）を含むチャンクとは重ならない**。
_INDEX_OF_CONTENTS_LEADER = re.compile(r"[·.．・…‥]{4,}")


def is_index_of_contents_unit(unit: ResourceUnit) -> bool:
    """目次・項目一覧のチャンクか。

    **項目名の羅列なので、項目テキストとの字面の一致度が本文の定義文より高い。** 少なからぬ
    項目で目次チャンクが1位になり、上位3件がすべて目次の項目もあった
    （その項目では LLM に定義が1件も見えないので「規約文書を引いても意味が決まらなかった」と
    数えられる＝検索の取り逃がしがその件数を作る）。

    **ページ番号で除外しない。** 原典の前書き・凡例（目次ではない）まで落ちるし、収録文書を
    足したときや目次が後ろのページに来たときに効かなくなる。目次であることの目印で判定する。
    """
    return bool(_INDEX_OF_CONTENTS_LEADER.search(unit.search_text or ""))


def is_citable_reference_unit(unit: ResourceUnit) -> bool:
    """臨床的意味の**典拠候補になりうる**規約文書のユニットか。

    `is_reference_unit` は「規約文書のユニットか」という**分類**で、こちらは「典拠として
    提示してよいか」という**用途**である。目次は典拠に求める「マニュアルの該当箇所」
    ではなく、該当箇所への**ポインタ**なので、典拠候補から外す。

    **原典は原典のまま索引する**（収録文書の一覧は変えない）。除外は
    典拠探索という用途の側で行う。
    """
    return is_reference_unit(unit) and not is_index_of_contents_unit(unit)


def _format_char_range(char_range: object) -> str:
    """`char_range` を出典表記用の文字列にする。

    索引は `[start, end]` の**リスト**で持つ（indexer の `file_indexer`）。
    そのまま f-string へ入れると `[0, 800]` と表示され、検証者が原文の位置を追える
    表記にならない。`start-end` へ書き下す。
    """
    if isinstance(char_range, list | tuple):
        return "-".join(str(v) for v in char_range)
    return str(char_range)


def format_citation_source(*, path: Any, page: Any = None, char_range: Any = None) -> str:
    """典拠1件の出典表記を、**値を明示的に受け取って**組み立てる。

    payload の dict を渡す形にしないのは、`label` のような**検証されていない保存値**を
    フォールバックとして拾う裏口を残さないため。配布物に載る表記はここを通る。
    """
    where = f"{path} p.{page}" if page is not None else str(path)
    if char_range:
        where = f"{where}（{_format_char_range(char_range)}）"
    return f"規約文書: {where}"


def build_citation_source_from_payload(
    payload: Mapping[str, Any], *, label: str | None = None
) -> str:
    """典拠1件の出典表記を payload から組み立てる（`build_citation_source` の本体）。

    索引の `ResourceUnit` を持たない場所から同じ表記を作れるようにするための入口。
    `label` は**索引ユニットのラベル**（`ResourceUnit.label`）のフォールバック用で、
    キーワード専用にしてある——`payload["label"]`（クライアント由来で検証されていない値）を
    誤って渡す形を型で塞ぐため。
    """
    return format_citation_source(
        path=payload.get("path") or label or "（文書不明）",
        page=payload.get("page"),
        char_range=payload.get("char_range"),
    )


def build_citation_source(unit: ResourceUnit) -> str:
    """典拠1件の人間可読な出典表記を payload から決定的に組み立てる。

    LLM に典拠の文字列を書かせない。LLM は候補の cand_id を選ぶだけで、出典は
    索引が持つパスとページから機械的に決まる（存在しない文書・ページの捏造を
    構造的に防ぐ。所在側で location を derive するのと同じ方式）。
    """
    return build_citation_source_from_payload(unit.payload, label=unit.label)


def _item_row_filters(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """項目コードで行を選ぶ単位の、有効な行の条件（`payload.item_row_filters`）。"""
    raw = payload.get("item_row_filters")
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, Mapping) and f.get("column")]


def build_location(unit: ResourceUnit) -> str:
    """ユニットの payload からデータ所在の定型文を決定的に組み立てる。"""
    p = unit.payload
    if unit.kind == Kind.DB:
        db = p.get("connection_id") or "（DB不明）"
        table = p.get("db_qualified") or "（表不明）"
        if p.get("table_logical"):
            table = f"{table}（{p['table_logical']}）"
        column = p.get("physical_name") or "（カラム不明）"
        if p.get("logical_name"):
            column = f"{column}（{p['logical_name']}）"
        base = f"DB: {db} / 表: {table} / カラム: {column}"

        # **文書種別の単位は、ここまでの3つが全単位で同一になる**。
        # 本文はすべて文書の表の同じ本文列に入っており、種別で分かれるのは
        # 文書ヘッダの表側の絞り込み条件だけだからである。種別を書かないと
        # 文書を所在とする項目の `location` が1文字も違わなくなり、
        #
        # - 画面は「詳細」を開かないとプログレスノートか病理検査レポートか分からない
        # - **手順書の生成（ステップ2）は `location` しか受け取らない**ので、
        #   それらの項目がすべて「文書の本文列を読め」という同じ入力になる
        # - 所在を評価する判定者が読む材料も同一文字列になる
        #
        # `location` は「人間可読の所在記述」なので、**別の場所を同じ文字列で表すのは
        # この欄自身の契約違反**である。機械可読な `payload` があるから省いてよい、
        # という話ではない（ステップ2は自然言語の工程で payload を読まない）。
        code = p.get("doc_type_code")
        if code:
            name = p.get("doc_type_name") or unit.label
            out = f"{base} / 文書種別: {code}（{name}）"
            ftable, fcolumn = p.get("doc_filter_table"), p.get("doc_filter_column")
            if ftable and fcolumn:
                out += f" / 絞り込み: {ftable}.{fcolumn}='{code}'"
            return out
        # **項目コードで行を選ぶ単位も同じ形になる**（経過表のような縦持ちの表）。
        # 体温も脈拍も同じ値の列に入っており、何の値かは同じ表の項目コード列の値で決まる。
        item = p.get("item_code")
        if item:
            name = p.get("item_name") or unit.label
            out = f"{base} / 項目: {item}（{name}）"
            fcolumn = p.get("item_filter_column")
            if fcolumn:
                conds = [f"{p.get('db_qualified')}.{fcolumn}='{item}'"]
                # 有効な行の条件も書く。手順書の生成は location しか受け取らないので、
                # ここに無ければ削除行・履歴行を除く条件を知る手段がない
                conds += [f"{f.get('column')}='{f.get('equals')}'" for f in _item_row_filters(p)]
                out += " / 絞り込み: " + " AND ".join(conds)
            return out
        return base
    if unit.kind == Kind.MASTER:
        db = p.get("connection_id") or "（DB不明）"
        table = p.get("db_qualified") or "（表不明）"
        code = p.get("code")
        label = p.get("label") or unit.label
        entry = f"{code}（{label}）" if code is not None else label
        return f"マスタ: {db} / 表: {table} / コード: {entry}"
    if unit.kind == Kind.FHIR:
        rtype = p.get("resource_type") or "（リソース型不明）"
        query = p.get("query_template") or ""
        path = p.get("fhir_path") or ""
        parts = [f"FHIR: {rtype}"]
        if query:
            parts.append(f"クエリ: {query}")
        if path:
            # FHIRPath ではなく簡易ドット記法（executor.resolve_fhir_path）。
            # この location は手順書表示とコンパイラのプロンプトに載るため、
            # 「FHIRPath」と書くと DSL の実装能力を誤って伝えてしまう。
            parts.append(f"値抽出パス: {path}")
        return " / ".join(parts)
    if unit.kind == Kind.FILE:
        loc = p.get("path") or p.get("path_pattern") or "（パス不明）"
        role = p.get("file_role")
        return f"ファイル: {loc}" + (f"（{role}）" if role else "")
    return unit.label


# ===========================================================================
# ステップ3（DSLコンパイル）の op 引数の決定的導出
# ===========================================================================
#
# 索引が持つ「識別子」＝接続ID・表・カラム・患者絞り込みキー列・FHIRクエリ雛形・
# FHIRPath・ファイルパスパターンは、**コード側が最終決定する**（LLM の推測に委ねない）。
# 一方で「時間窓・並び順・絞り込み条件（ORDER BY / 追加の WHERE）」は臨床ロジックで
# あり手順書・臨床的コンテキストからしか決まらないため、LLM の担当として残す。
#
# 責務分界の実装:
#   1) 本モジュールが payload から op と引数（sql / fhir_query / fhir_path /
#      path_pattern / connection_id）を組み立てる（DerivedOpArgs）。
#   2) コンパイラ（compiler.py）が LLM 出力の各 step を required_tokens で検証し、
#      索引の識別子を踏んでいれば臨床ロジックごと採用、踏んでいなければ本モジュールの
#      引数で上書き（決定的に組めないときは needs_input=true）する。

# データ源ファミリ（step の op と候補の kind を突き合わせる単位）
FAMILY_DB = "db"
FAMILY_FHIR = "fhir"
FAMILY_FILE = "file"

# step.op → データ源ファミリ。csv_extract は索引側に成果物契約が無い
# （payload に path/key_column/column が来ない）ため、あえて対象外にする。
OP_FAMILY: dict[str, str] = {
    OpType.SQL_EXTRACT: FAMILY_DB,
    OpType.EXTERNAL_DB_EXTRACT: FAMILY_DB,
    OpType.FHIR_EXTRACT: FAMILY_FHIR,
    OpType.FILE_EXTRACT: FAMILY_FILE,
}

# SQL へ埋め込んでよい識別子（表・カラム）の形。索引由来の値でも、そのまま SQL 文へ
# 連結するため文字種を制限する（引用符・空白・記号による構文注入を防ぐ）。公開名
# （モジュール外からの import 可）。statistical_profile.py が同じ規則で
# 統計プロファイル生成の集計SQLへ識別子を埋め込む際にも使う。
SQL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*(\.[A-Za-z_][A-Za-z0-9_$#]*)*$")

# 絞り込みに埋めるリテラル。索引由来のコード（文書種別コードなど）だけを想定し、
# **英数字と下線に限る。** 患者データは通らないし、通す必要もない。形が合わなければ
# SQL を組み立てず不足として返す（引用符の中だからと緩めない）。
SQL_LITERAL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 患者ごとの取得であることを保証するプレースホルダ。executor は `{{patient_id}}` と
# `{{ patient_id }}` の両綴りを置換するため、検査も両方を受理する。
_PATIENT_PLACEHOLDER = re.compile(r"\{\{\s*patient_id\s*\}\}")


def has_patient_placeholder(text: str) -> bool:
    """FHIRクエリ／ファイルパスが患者ごとの絞り込みを含むか。

    含まない取得は「全患者から先頭1件」になりうる（executor は Bundle の
    resources[0] を黙って採る）。他患者の値を当該患者の CRF に確定保存する事故に
    直結するため、決定的導出では必須とする（DB の patient_key と対称の位置づけ）。
    """
    return bool(_PATIENT_PLACEHOLDER.search(text))


@dataclass(frozen=True)
class RequiredToken:
    """生成された SQL / FHIRクエリに現れていなければならない索引由来の識別子。

    accepted のいずれか1つが（大小文字を無視して）含まれていればよい。表名は
    `DEMO.LAB` と `LAB` のどちらの記法も実務上ありうるため両方を受理する。
    """

    label: str
    accepted: tuple[str, ...]

    def matches(self, text: str) -> bool:
        # 単語境界で照合する。単純な部分文字列一致だと `LAB` が `LABS` や
        # `COLLABORATION` にも当たり、別の表を指す SQL を「索引の識別子を踏んだ」と
        # 誤認する。識別子に使える文字（英数字・_・$・#）が前後に無いことを要求する。
        for a in self.accepted:
            if not a:
                continue
            pattern = rf"(?<![A-Za-z0-9_$#]){re.escape(a)}(?![A-Za-z0-9_$#])"
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False


@dataclass(frozen=True)
class DerivedOpArgs:
    """索引ユニットの payload から決定的に導出した op とその引数。

    args は step へそのまま焼き込める引数（sql / connection_id / fhir_query /
    fhir_path / path_pattern）。missing が空でなければ索引の情報が足りず引数を
    決定的に組み立てられなかったことを表す（例: db に患者絞り込みキー列が無い）。
    """

    op: str
    family: str
    args: dict[str, Any] = field(default_factory=dict)
    required_tokens: tuple[RequiredToken, ...] = ()
    unit_id: str | None = None
    location: str | None = None
    # 索引が宣言する患者キー種別（`payload.key_type`）。プロンプトに示すだけでなく、
    # compiler の `bake_patient_keys` が step の `key_type` へ焼き込む。物理形
    # （ゼロ詰め等）は接続レジストリの `key_formats` から引く。file / csv / fhir の単位は
    # 普通これを持たず、接続の既定 `key_type` が使われる。
    key_type: str | None = None
    # 「患者で絞らない表」と宣言された単位か（`is_patient_unscoped`）。False なら
    # compiler は患者キーを焼き込まず、LLM が書いた key_type も外す。
    patient_scoped: bool = True
    missing: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """引数を決定的に組み立てられたか（False なら人の補完が必要）。"""
        return not self.missing

    def matches(self, text: str | None) -> bool:
        """生成物（SQL / FHIRクエリ / パス）が索引の識別子を踏んでいるか。

        照合すべき識別子が1つも無いときは False（＝索引由来の引数を優先する）。
        「検証できない」を「検証に通った」と扱わない。
        """
        if not text or not self.required_tokens:
            return False
        return all(t.matches(text) for t in self.required_tokens)


def infer_kind(unit_id: str | None, payload: Mapping[str, Any]) -> str | None:
    """候補レコード（unit_id + payload）から索引ユニットの kind を推定する。

    SystemLocation.candidates は kind を保存しない（location 文字列と payload だけを
    持つ）ため、unit_id の接頭辞（`db:` / `fhir:` / `file:` / `master:`）を第一の
    手がかりにし、無ければ payload のキー構成から判定する。判定できなければ None。
    """
    if unit_id:
        prefix = unit_id.split(":", 1)[0]
        if prefix in Kind.ORDER:
            return prefix
    if payload.get("file_role") or payload.get("path_pattern") or payload.get("path"):
        return Kind.FILE
    if payload.get("resource_type") or payload.get("query_template"):
        return Kind.FHIR
    if payload.get("db_qualified") and payload.get("physical_name"):
        return Kind.DB
    if payload.get("db_qualified") and payload.get("code") is not None:
        return Kind.MASTER
    return None


def resolve_db_op(candidate: Mapping[str, Any], registry: ConnectionRegistry | None) -> str | None:
    """DB候補を自アプリDB（sql_extract）か外部DB（external_db_extract）か決める。

    **接続レジストリ（connections.yaml）の scope を唯一の根拠とする。** 候補の
    source_category は参考にしない。候補の source_category は索引ドラフト生成時に
    `derive_source_category` が付けた値で、そこでは未登録接続を local_db と既定して
    いる（本モジュール冒頭の説明のとおり）。しかも candidates はクライアント送信値を
    そのまま保存する（`routes.save_system` → `_clean_candidate` は列挙値の検査のみ）
    ため、`source_category=local_db` を先に見ると「未登録接続だが自アプリDB」と称する
    候補が作れてしまう。`_op_sql` は connection_id を参照せずアプリ本体の業務セッション
    へ SQL を発行するので、それは任意の表の読み出しになる。

    scope で判定できないときは **None を返す（フェイルクローズ）**。ただし候補が
    外部DBを宣言している場合だけは external_db_extract を返す。未登録接続の
    external_db_extract は実行時に接続解決で失敗する（SOURCE_UNAVAILABLE）だけで、
    アプリ本体の DB には到達しないため。呼び出し側は None を不足として扱い、人の補完
    （needs_input）へ回すこと。
    """
    payload = candidate.get("payload") or {}
    reg = registry if registry is not None else get_registry()
    scope = reg.scope_of(candidate.get("connection_id") or payload.get("connection_id"))
    if scope == SCOPE_EXTERNAL:
        return OpType.EXTERNAL_DB_EXTRACT
    if scope is not None:
        return OpType.SQL_EXTRACT
    if candidate.get("source_category") == SourceCategory.EXTERNAL_DB:
        return OpType.EXTERNAL_DB_EXTRACT
    return None


def normalize_fhir_query(query: str) -> str:
    """FHIRクエリを executor の規約（base_url へ連結する先頭 `/` 付き）へ正規化する。

    索引の query_template は `Observation?code=...` のように先頭 `/` を持たない
    （`docs/resource_index_guide.md` §4.2）。executor は `base_url + fhir_query` で
    URL を作るため、ここで `/` を必ず付ける。
    """
    q = query.strip()
    return q if q.startswith("/") else f"/{q}"


def normalize_path_pattern(pattern: str) -> str:
    """ファイルパスパターンを executor の規約（接続 root_path 起点の相対）へ正規化する。

    索引のマニフェストは `/reports/{{patient_id}}/*.pdf` のように先頭 `/` 付きで
    宣言されうるが、executor は root_path と結合するため絶対パスだと root 外判定で
    失敗する。先頭の `/` を落として相対パスにする。
    """
    return pattern.strip().lstrip("/")


def has_physical_identifiers(payload: Mapping[str, Any]) -> bool:
    """ユニットが**実DBで使える識別子**を持っているか。

    索引の `db_qualified` は施設が渡した**定義書＝元システム側の名前**であり、実際の
    DB の名前とは違う（定義書の `SRC.DOCUMENT` が実DBでは `ehr.DOCUMENT`、定義書で
    スキーマ無しの `PATIENT` が実DBでは `mart.PATIENT`、のように読み替わる）。実行に使う名前は別に持つ約束で、
    それが `payload.physical` の schema/table/column である。3つとも揃って初めて
    「どのスキーマのどの表のどの列か」が決まるので、1つでも欠けたら持っていないと見なす。
    """
    physical = payload.get("physical")
    if not isinstance(physical, Mapping):
        return False
    return all(str(physical.get(k) or "").strip() for k in ("schema", "table", "column"))


# 文書種別の単位であることを示す payload のキー（施設側のスクリプトが付ける。
# indexer `physical.DOC_UNIT_KEYS` と同じ）
DOC_UNIT_KEYS = ("doc_type_code", "doc_filter_table", "doc_filter_column")


def is_doc_unit(payload: Mapping[str, Any]) -> bool:
    """文書種別の単位か（本文の表を種別で絞る必要がある単位）。"""
    return any(payload.get(k) for k in DOC_UNIT_KEYS)


def _qualified(physical: Mapping[str, Any]) -> str:
    """`schema.table`。**両方が識別子の形をしていることは呼び出し側が確かめる。**"""
    return f"{physical['schema']}.{physical['table']}"


def is_patient_unscoped(payload: Mapping[str, Any]) -> bool:
    """ユニットが「患者で絞らない表である」と**宣言されている**か。

    マスタ参照・施設固定値（施設マスタ、コード表など）の表には、患者を識別する列が
    原理的に無い。そういう表に「患者絞り込みキー列が無い」と報告するのは、報告する理由が
    誤りである。

    **宣言は `payload.patient_scoped` が真偽値の `False` のときだけ**認める。文字列 `"false"`
    や `0`、キーの欠落は認めない（宣言が無ければ従来どおり患者で絞る）。**表論理名に
    「マスタ」を含むか等の名前から推測しない**——患者データの表でキーの推定に失敗したときに、
    **全患者の行を返す SQL** ができるためである（患者IDらしい列名の推測で避けてきた形と同じ）。
    宣言は施設の確認を経たものに限る（`docs/resource_index_guide.md`）。
    """
    return payload.get("patient_scoped") is False


def build_unscoped_sql(payload: Mapping[str, Any], base: str) -> str | None:
    """「患者で絞らない」と宣言されたユニットの SELECT を組む。

    WHERE 句を持たない。**呼び出し側が `is_patient_unscoped` を確かめてから呼ぶこと**
    （宣言なしに WHERE を省くと、全患者の行を返す SQL になる）。結合を伴う到達経路
    （`access_path`）は患者への絞り込みのためのものなので、宣言と矛盾する。経路があれば
    組まない（`None`）。
    """
    column = payload.get("physical_name")
    if not SQL_IDENT.match(str(base or "")) or not SQL_IDENT.match(str(column or "")):
        return None
    if payload.get("access_path"):
        return None
    return f"SELECT {column} FROM {base}"


def build_access_sql(payload: Mapping[str, Any], base: str) -> str | None:
    """`payload.physical` と `payload.access_path` から SELECT を組む。

    `base` は**呼び出し側が解決した実行用の表名**である（外部DBは `payload.physical` から、
    自アプリDBは `db_qualified` から）。ここでは検査だけして埋める。

    **経路が無ければ単表で組む。** 索引が経路を持たないユニットの挙動は従来どおりである。

    経路がある場合に足すのは**絞り込みのための結合**だけで、**値を持ってくる結合はしない**。
    別表の値が要るならそれは別のユニットであり、1ユニット＝1つの場所という
    `build_location` の前提を壊さないためである。

    並び順は索引が持つ既定値をそのまま出す。**どれを採るか（最新/初回/全部）は DSL 側**
    （`multi` と `select`）が決める。
    """
    column = payload.get("physical_name")
    if not SQL_IDENT.match(str(base or "")) or not SQL_IDENT.match(str(column or "")):
        return None
    patient_key = payload.get("patient_key")
    if not patient_key or not SQL_IDENT.match(str(patient_key)):
        return None

    column = str(column)
    path = payload.get("access_path")
    if not isinstance(path, Mapping):
        return f"SELECT {column} FROM {base} WHERE {patient_key} = :patient_id"

    # 結合する相手ごとに別名を振る。同じ相手を複数の条件で使うので1つに畳む。
    joins: dict[str, tuple[str, tuple[str, ...]]] = {}

    def alias_for(via: Mapping[str, Any]) -> str | None:
        for key in ("schema", "table"):
            if not SQL_IDENT.match(str(via.get(key) or "")):
                return None
        on = tuple(str(c) for c in (via.get("on") or ()))
        if not on or not all(SQL_IDENT.match(c) for c in on):
            return None
        name = f"{via['schema']}.{via['table']}"
        if name not in joins:
            joins[name] = (f"j{len(joins)}", on)
        return joins[name][0]

    wheres = [f"c.{patient_key} = :patient_id"]
    for f in path.get("filters") or ():
        if not isinstance(f, Mapping) or not SQL_IDENT.match(str(f.get("column") or "")):
            return None
        via = f.get("via")
        ref = "c"
        if isinstance(via, Mapping):
            ref = alias_for(via) or ""
            if not ref:
                return None
        value = f.get("equals")
        lit = str(value) if isinstance(value, int) and not isinstance(value, bool) else None
        if lit is None:
            # **文字列はリテラルで埋める。** 値は索引由来のコード（文書種別コードなど）で、
            # 患者データではない。埋め込む前に形を検査し、通らなければ組み立てない。
            raw = str(value)
            if not SQL_LITERAL.match(raw):
                return None
            lit = f"'{raw}'"
        wheres.append(f"{ref}.{f['column']} = {lit}")

    orders = []
    for o in path.get("order_by") or ():
        if not isinstance(o, Mapping) or not SQL_IDENT.match(str(o.get("column") or "")):
            return None
        via = o.get("via")
        ref = "c"
        if isinstance(via, Mapping):
            ref = alias_for(via) or ""
            if not ref:
                return None
        direction = "DESC" if str(o.get("dir") or "asc").lower() == "desc" else "ASC"
        orders.append(f"{ref}.{o['column']} {direction}")

    sql = f"SELECT c.{column} FROM {base} c"
    for name, (alias, on) in joins.items():
        cond = " AND ".join(f"{alias}.{c} = c.{c}" for c in on)
        sql += f" JOIN {name} {alias} ON {cond}"
    sql += " WHERE " + " AND ".join(wheres)
    if orders:
        sql += " ORDER BY " + ", ".join(orders)
    return sql


def _derive_db(
    candidate: Mapping[str, Any],
    payload: Mapping[str, Any],
    registry: ConnectionRegistry | None,
) -> DerivedOpArgs | None:
    table = payload.get("db_qualified")
    column = payload.get("physical_name")
    if not table or not column:
        return None  # 表・カラムが無い候補は索引由来ではない（手入力等）
    op = resolve_db_op(candidate, registry)
    connection_id = candidate.get("connection_id") or payload.get("connection_id")
    args: dict[str, Any] = {}
    if connection_id:
        args["connection_id"] = str(connection_id)
    # **実識別子を持つなら、それだけを受け入れる**。compiler は LLM が書いた SQL が
    # 索引を踏んでいるかをこのトークンで判定し、踏んでいれば**LLM の SQL をそのまま採用する**
    # （時間窓や並び順など臨床ロジックを含みうるため）。ここで定義書上の名前も許すと、
    # **実DBに無い表を指す SQL が「踏んでいる」と判定されて残る**。実際に、定義書上の名前の
    # ままの SQL が複数本通ったことがある。
    # 実識別子を持たないユニットは、そもそも SQL を組み立てないので従来の名前のままでよい。
    physical_raw = payload.get("physical")
    physical = physical_raw if isinstance(physical_raw, Mapping) else {}
    if physical.get("schema") and physical.get("table"):
        # **素の表名は受け入れない。** スキーマ無しの表名だけの SQL は実DBでは「表が
        # 存在しない（invalid object name）」になる（スキーマが要る）。素の名前を許すと、まさにその SQL が「索引を踏んでいる」と
        # 判定されて残る（実際に起きた）。修飾名だけを受理する。
        table_label = _qualified(physical)
        table_accepted = [table_label]
    else:
        table_label = str(table)
        table_accepted = [str(table), str(table).split(".")[-1]]
    tokens = [
        RequiredToken(label=f"表 {table_label}", accepted=tuple(dict.fromkeys(table_accepted))),
        RequiredToken(label=f"カラム {column}", accepted=(str(column),)),
    ]
    item_code = payload.get("item_code")
    if item_code:
        # 表と列だけで「踏んでいる」と判定すると、項目コード列で絞らない SQL が残り、
        # 体温の欄に全バイタルの値が混ざる。項目コードも踏ませる。
        tokens.append(RequiredToken(label=f"項目コード {item_code}", accepted=(str(item_code),)))
        # 有効な行の条件の列も踏ませる。**列名が現れることしか見ない**ので、`DELETED <> '0'`
        # や `ITEM_CODE IN (…)` のような誤りまでは捕まえない（トークン照合の限界）
        for f in _item_row_filters(payload):
            col = str(f.get("column"))
            tokens.append(RequiredToken(label=f"有効行の条件 {col}", accepted=(col,)))
    patient_key = payload.get("patient_key")
    unscoped = is_patient_unscoped(payload)
    missing: tuple[str, ...] = ()
    if op is None:
        # 接続レジストリで scope を判定できない。自アプリDBへ倒すと任意の表を
        # アプリ本体のセッションで読む SQL になるため、組み立てず不足として扱う
        # （resolve_db_op の docstring 参照）。
        return DerivedOpArgs(
            op=OpType.SQL_EXTRACT,
            family=FAMILY_DB,
            args={},
            required_tokens=tuple(tokens),
            unit_id=candidate.get("unit_id"),
            location=candidate.get("location"),
            key_type=None if unscoped else payload.get("key_type"),
            patient_scoped=not unscoped,
            missing=(
                "接続の種別（connections.yaml に未登録のため自アプリDB/外部DBを判定できない）",
            ),
        )
    if op == OpType.EXTERNAL_DB_EXTRACT and not connection_id:
        # 外部DBは connection_id が必須（executor は接続レジストリ経由でしか到達しない）。
        missing = ("外部DBの接続ID（payload.connection_id）",)
    elif unscoped and (patient_key or payload.get("access_path") or payload.get("key_type")):
        # 「患者で絞らない」と宣言しているのに、患者キー・患者への到達経路・患者キー種別の
        # どれかもある。宣言が矛盾している。**どちらを信じても事故になりうる**（宣言を信じると WHERE を省いて
        # 全患者の行を返す）ので、組み立てずに人へ回す。
        # compiler が「索引に{missing}が無く…」と埋め込むので、名詞句で書く
        missing = (
            "矛盾しない患者絞り込みの設定（payload.patient_scoped=false なのに patient_key・"
            "access_path・key_type のいずれかがある）",
        )
    elif not patient_key and not unscoped:
        # 索引が患者絞り込みキー列を決められなかった（docs/resource_index_guide.md §4.1）。
        # 推測でWHERE句を作らせず、不足として後段（人の補完）へ回す。
        # **「患者で絞らない表である」という宣言も無い**ことを理由に含める。
        # 患者データの表でキーが決まらないのか、マスタ参照で絞る必要が無いのかは、
        # 宣言（施設の確認を経たもの）が無い限り区別できない。
        missing = (
            "患者絞り込みキー列（payload.patient_key）。患者で絞る必要がない表"
            "（マスタ参照・施設固定値）なら payload.patient_scoped=false の宣言が要る",
        )
    elif not (SQL_IDENT.match(str(table)) and SQL_IDENT.match(str(column))):
        missing = ("SQLへ埋め込めない表名/カラム名（識別子の形式が不正）",)
    elif patient_key and not SQL_IDENT.match(str(patient_key)):
        missing = ("SQLへ埋め込めない患者キー列名（識別子の形式が不正）",)
    elif is_doc_unit(payload) and not payload.get("access_path"):
        # 文書種別の単位なのに種別で絞る経路が無い。本文の表が種別の列を持たない構成では、組み立てると
        # **その患者の全文書（削除済みを含む）**を返す SQL になる（indexer が経路を付けずに
        # physical だけ付けた索引で起きうる）。単表へ落とさず不足として人へ回す（多重防御）。
        missing = (
            "文書種別で絞る到達経路（payload.access_path。文書種別の単位で経路が無いと、"
            "その患者の全文書を返す SQL になるため組み立てない）",
        )
    elif op == OpType.EXTERNAL_DB_EXTRACT and not has_physical_identifiers(payload):
        # 外部DBは `db_qualified`（定義書＝元システム側の名前）では引けない。生成済みの SQL を
        # 実DBへ当てたところ、**有効な表名は1本も無く**、全部が「表が存在しない（invalid object
        # name）」で落ちた。しかも SQL は形として成立するので
        # `needs_input` が立たず、「確定できた項目」に数えられたまま実行時に落ちる。
        # 実識別子（payload.physical）を持たないユニットは組み立てず不足として返し、
        # **見えない失敗を見える失敗にする**（patient_key 欠落と同じ扱い）。
        # 自アプリDB（sql_extract）はアプリ自身のスキーマなので、この読み替えは起きない。
        missing = (
            "外部DBで実行できる識別子（payload.physical の schema/table/column。"
            "db_qualified は定義書上の名前で、実DBには無い表を指すため組み立てない）",
        )
    else:
        # 実行に使う表名。**外部DBは実識別子、自アプリDBは `db_qualified`**（自分のスキーマ
        # なので読み替えが起きない）。上の分岐で外部DBの識別子は確認済みである。
        physical_base = payload.get("physical")
        base_table = (
            _qualified(physical_base)
            if op == OpType.EXTERNAL_DB_EXTRACT and isinstance(physical_base, Mapping)
            else str(table)
        )
        sql = (
            build_unscoped_sql(payload, base_table)
            if unscoped
            else build_access_sql(payload, base_table)
        )
        if sql is None:
            # 識別子も患者キーも通ったのに組み立てられないのは、経路の記述が壊れている
            # ときだけである（結合先の表名が識別子の形をしていない等）。**推測で単表へ
            # 落とさない**——それは「黙って経路を落とす」そのものである。
            missing = ("到達経路の記述（payload.access_path が SQL にできない形）",)
        else:
            args["sql"] = sql
            if not unscoped:
                # 患者で絞らない表の SQL には :patient_id が無い
                tokens.append(
                    RequiredToken(label=":patient_id バインド", accepted=(":patient_id",))
                )
    return DerivedOpArgs(
        op=op,
        family=FAMILY_DB,
        args=args,
        required_tokens=tuple(tokens),
        unit_id=candidate.get("unit_id"),
        location=candidate.get("location"),
        key_type=None if unscoped else payload.get("key_type"),
        patient_scoped=not unscoped,
        missing=missing,
    )


def _derive_fhir(candidate: Mapping[str, Any], payload: Mapping[str, Any]) -> DerivedOpArgs | None:
    resource_type = payload.get("resource_type")
    query_template = payload.get("query_template")
    if not resource_type and not query_template:
        return None
    args: dict[str, Any] = {}
    connection_id = candidate.get("connection_id") or payload.get("connection_id")
    if connection_id:
        args["connection_id"] = str(connection_id)
    if payload.get("fhir_path"):
        args["fhir_path"] = str(payload["fhir_path"])
    tokens: list[RequiredToken] = []
    if resource_type:
        tokens.append(
            RequiredToken(label=f"リソース型 {resource_type}", accepted=(str(resource_type),))
        )
    code = payload.get("code")
    missing: tuple[str, ...] = ()
    if not query_template:
        missing = ("FHIRクエリ雛形（payload.query_template）",)
    elif not has_patient_placeholder(normalize_fhir_query(str(query_template))):
        # 患者絞り込みの無いクエリは全患者を対象にしてしまう。executor は multi 未指定なら
        # Bundle の先頭リソースを黙って採るため、他患者の値が当該患者の値として確定保存
        # されうる。索引の宣言が不十分なので確定せず人の補完へ回す（DB の patient_key
        # 欠落と同じ扱い）。LLM が正しく書いた患者絞り込みを索引の値で上書きしない。
        missing = ("FHIRクエリの患者絞り込み（query_template に {{patient_id}} が無い）",)
    else:
        args["fhir_query"] = normalize_fhir_query(str(query_template))
        tokens.append(
            RequiredToken(
                label="患者絞り込み {{patient_id}}",
                # executor は両綴りを置換するため、どちらで書かれていても踏んだとみなす。
                accepted=("{{patient_id}}", "{{ patient_id }}"),
            )
        )
        if code:
            # クエリ雛形がコード絞り込みを含むなら、そのコードは生成物にも必須。
            # （コードを落とすと当該患者の全 Observation を取ってしまう）
            code_token = str(code).split("|")[-1]
            if code_token and code_token in args["fhir_query"]:
                tokens.append(RequiredToken(label=f"コード {code_token}", accepted=(code_token,)))
    return DerivedOpArgs(
        op=OpType.FHIR_EXTRACT,
        family=FAMILY_FHIR,
        args=args,
        required_tokens=tuple(tokens),
        unit_id=candidate.get("unit_id"),
        location=candidate.get("location"),
        key_type=payload.get("key_type"),
        missing=missing,
    )


def _derive_file(candidate: Mapping[str, Any], payload: Mapping[str, Any]) -> DerivedOpArgs | None:
    if payload.get("file_role") == "reference":
        return None  # 参照知識（実行時に値を取らない）→ op を導出しない
    pattern = payload.get("path_pattern")
    if not pattern:
        return None
    normalized = normalize_path_pattern(str(pattern))
    args: dict[str, Any] = {"path_pattern": normalized}
    connection_id = candidate.get("connection_id") or payload.get("connection_id")
    if connection_id:
        args["connection_id"] = str(connection_id)
    missing: tuple[str, ...] = ()
    if not has_patient_placeholder(normalized):
        # file_role=patient_data は患者ごとの文書を指す宣言。{{patient_id}} を含まない
        # パスは全患者で同じファイルを読むことになり、他患者の文書から値を取りうる。
        missing = ("ファイルパスの患者絞り込み（path_pattern に {{patient_id}} が無い）",)
        args.pop("path_pattern")
    elif any(ch in normalized for ch in "*?["):
        # 索引のマニフェストは `/reports/{{patient_id}}/*.pdf` のようにワイルドカードで
        # 宣言できるが（docs/resource_index_guide.md §4.3）、executor の _op_file は
        # 置換後のパスを Path.is_file() でリテラルに見るだけで glob を展開しない。
        # そのまま焼き込むと必ず不成立になり、患者ディレクトリが実在する場合は
        # NOT_FOUND_FOR_PATIENT（当該患者のデータが無い）と誤って記録されてしまう。
        # 具体的なファイル名は人が確定する。
        missing = ("ワイルドカードを含まない具体的なファイルパス（実行時に glob 展開は行わない）",)
        args.pop("path_pattern")
    elif ".." in PurePosixPath(normalized).parts:
        # 接続 root 外へ抜ける相対指定は受け付けない（executor の root 配下検査に
        # 到達する前に、導出・是正の段で閉じる多層防御）。
        missing = ("接続 root 配下の相対パス（`..` を含むパスは使用できない）",)
        args.pop("path_pattern")
    return DerivedOpArgs(
        op=OpType.FILE_EXTRACT,
        family=FAMILY_FILE,
        args=args,
        required_tokens=(
            () if missing else (RequiredToken(label=f"パス {normalized}", accepted=(normalized,)),)
        ),
        unit_id=candidate.get("unit_id"),
        location=candidate.get("location"),
        key_type=payload.get("key_type"),
        missing=missing,
    )


def derive_op_args(
    candidate: Mapping[str, Any],
    registry: ConnectionRegistry | None = None,
) -> DerivedOpArgs | None:
    """データ所在候補1件から DSL の op 引数を決定的に導出する。

    Args:
        candidate: SystemLocation.candidates の1要素
            （{source_category, location, unit_id, connection_id, payload}）。

    Returns:
        導出できた op 引数。導出対象でなければ None。None を返すのは
        - payload が無い/空（手入力候補・索引を使っていない候補）
        - kind=master（コード表の1エントリ。値の取得 op ではなく WHERE 句の材料）
        - kind=file の file_role=reference（参照知識で実行時に値を取らない）
        - kind を判定できない、または kind 別の必須キーが欠けている
        いずれの場合も LLM 生成物には手を入れない（従来どおりの挙動）。
    """
    payload = candidate.get("payload")
    if not isinstance(payload, dict) or not payload:
        return None
    kind = infer_kind(candidate.get("unit_id"), payload)
    if kind == Kind.DB:
        return _derive_db(candidate, payload, registry)
    if kind == Kind.FHIR:
        return _derive_fhir(candidate, payload)
    if kind == Kind.FILE:
        return _derive_file(candidate, payload)
    # master は「コード表の1エントリ」であり値の取得 op には対応しない
    # （payload に code/label はあるがコード列・ラベル列の物理名が無い）。
    return None


def derive_op_args_list(
    candidates: list[dict[str, Any]] | None,
    registry: ConnectionRegistry | None = None,
) -> list[DerivedOpArgs]:
    """優先順の候補配列から、導出できた op 引数を優先順のまま列挙する。"""
    derived: list[DerivedOpArgs] = []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        spec = derive_op_args(cand, registry)
        if spec is not None:
            derived.append(spec)
    return derived
