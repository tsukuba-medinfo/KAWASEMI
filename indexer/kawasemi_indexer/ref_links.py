"""参照リンク（列→参照先マスタ）テーブルと下書き生成（RRF 第3チャネルのデータ層）。

「指定した臨床概念（例: 糖尿病）のデータが格納されていそうな DB カラムを上位に出す」
施策の中核機構。列とその値域マスタのリンクを成果物
`ref_links.jsonl` として持ち、クエリ時に二段展開（クエリ→マスタ特定→参照カラムを
ブースト）する。シグナルは **メタデータのみ**（列名・論理名・型・メモ）。患者データは
値プロファイリング含め一切使わない（索引＝地図の不変条件）。

リンク生成は既存 drafter 思想に従う:
- ルールベース推定（決定的・監査可能）→ status="rule"
- LLM 推論（ローカル Ollama。提示した unit_id 以外は受理しない）→ status="inferred"
- 人手確認済み → status="confirmed"（人が jsonl の status を書き換えて昇格させる）
チャネル内の順位も confirmed > rule > inferred。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.schema import REF_LINKS_FILENAME, Kind, ResourceUnit
from kawasemi_indexer.site_rules import MasterRef, load_indexer_rules

logger = get_logger(__name__)

# チャネル内の確度順（この順で候補を並べる）。
REF_STATUS_ORDER = ("confirmed", "rule", "inferred")

RefStatus = Literal["confirmed", "rule", "inferred"]
RefRole = Literal["code", "name"]


class RefLink(BaseModel):
    """参照リンクの 1 行（列 → 値域マスタ）。

    value_domains（列の値域注釈）と同じ意味論だが、JOIN 宣言ではなく検索チャネル用の
    成果物。人は status を confirmed に書き換えることでリンクを確定させる。
    """

    column_unit_id: str = Field(description="対象列の unit_id（例 'db:DEMO.DIAGNOSIS.DISEASE_CD'）")
    master_connection_id: str
    master_db_qualified: str
    status: RefStatus
    role: RefRole = Field(
        default="code", description="列に入るのがマスタのコードか名称か（WHERE 組み立ての将来用）"
    )
    source: str | None = Field(default=None, description="由来（ルール名 / llm:<model> / human）")
    note: str | None = None


class RefLinkIndex:
    """ref_links.jsonl のクエリ時ビュー（マスタ表 → 参照列群）。"""

    def __init__(self, links: list[RefLink]) -> None:
        self._links = links
        self._by_master: dict[tuple[str, str], list[RefLink]] = {}
        for ln in links:
            key = (ln.master_connection_id, ln.master_db_qualified)
            self._by_master.setdefault(key, []).append(ln)

    def __len__(self) -> int:
        return len(self._links)

    @property
    def links(self) -> list[RefLink]:
        return list(self._links)

    @classmethod
    def load(cls, index_dir: str | Path) -> RefLinkIndex:
        """ref_links.jsonl を読み込む。未配置なら空（チャネル不発の no-op）。"""
        path = Path(index_dir) / REF_LINKS_FILENAME
        links: list[RefLink] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        links.append(RefLink(**json.loads(line)))
            logger.info(f"参照リンクをロード: {path}（{len(links)}リンク）")
        return cls(links)

    def referencing_columns(
        self, connection_id: str, db_qualified: str
    ) -> list[tuple[str, str, str]]:
        """1つのマスタ表を参照する患者表の FK 列を (unit_id, role, status) で返す（FK逆引き）。

        master_ingestion_agreement の v2 ギャップ（マスタユニット → それを参照する患者表の
        FK 列一覧）を埋める。マスタ hit を「どの列で WHERE を組めるか」の候補で注釈するために
        使う。確度順（confirmed > rule > inferred）、同 status 内はファイル記載順（安定）。
        role（code / name）も返し、利用側が code 列で WHERE を組む判断に使える。
        """
        best: dict[str, tuple[str, str]] = {}  # column_unit_id -> (role, status)
        for ln in self._by_master.get((connection_id, db_qualified), []):
            cur = best.get(ln.column_unit_id)
            if cur is None or REF_STATUS_ORDER.index(ln.status) < REF_STATUS_ORDER.index(cur[1]):
                best[ln.column_unit_id] = (ln.role, ln.status)
        order = {s: i for i, s in enumerate(REF_STATUS_ORDER)}
        return sorted(
            ((uid, role, status) for uid, (role, status) in best.items()),
            key=lambda t: order[t[2]],
        )

    def candidates_for(self, masters: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """指定マスタ表群を参照する列を (unit_id, status) で返す。

        並びがチャネル内順位になる: status の確度順（confirmed > rule > inferred）、
        同 status 内はファイル記載順（安定）。同一列に複数リンクがあれば最良 status を採る。
        """
        best: dict[str, str] = {}
        for key in masters:
            for ln in self._by_master.get(key, []):
                cur = best.get(ln.column_unit_id)
                if cur is None or REF_STATUS_ORDER.index(ln.status) < REF_STATUS_ORDER.index(cur):
                    best[ln.column_unit_id] = ln.status
        # 安定な確度順ソート（同 status 内は挿入順=ファイル記載順を保つ）。
        order = {s: i for i, s in enumerate(REF_STATUS_ORDER)}
        return sorted(best.items(), key=lambda kv: order[kv[1]])


def write_ref_links(links: list[RefLink], out_dir: str | Path) -> Path:
    """ref_links.jsonl を書き出す（1行=1リンク。人が status を編集して確定する）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / REF_LINKS_FILENAME
    with path.open("w", encoding="utf-8") as fh:
        for ln in links:
            fh.write(json.dumps(ln.model_dump(), ensure_ascii=False) + "\n")
    logger.info(f"参照リンクを書き出しました: {path}（{len(links)}リンク）")
    return path


# --------------------------------------------------------------------------- #
# ルールベース推定
# --------------------------------------------------------------------------- #
class RefRule(BaseModel):
    """列名パターン → マスタ表のリンク規則（決定的・監査可能）。"""

    name: str
    master_connection_id: str
    master_db_qualified: str
    role: RefRole = "code"
    logical_pattern: str | None = None  # 論理名（NFKC 正規化・小文字）への正規表現
    physical_pattern: str | None = None  # 物理名（小文字）への正規表現
    exclude_pattern: str | None = None  # 論理名がこれに当たれば除外（区分・日付等）


def _norm(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s if s is not None else "")).lower()


# 概念（病名・検体検査項目）を表す列名のパターン。**施設に依らない部分だけをコードに持つ。**
# リンク先のマスタ表（connection_id / db_qualified）は施設の `indexer_rules.yaml` の
# `ref_link_masters` から読む（site_rules.py）。マスタが書かれていない概念は下書きしない。
# 名称系の列も「臨床概念の格納先」なので role="name" でリンクする（コード列と同格にブースト）。
_EXCLUDE_COMMON = (
    r"区分|フラグ|flag|種別|開始|終了|日付|日時|年月|回数|件数|点数|単位|コメント"
    # 手術・処置は病名/検査の値域ではない（ICD9-CM は処置コード）。索引番号も値でない。
    r"|手術|処置|icd-?9|インデックス|index"
)


class ConceptPattern(BaseModel):
    """列名パターン → 概念（リンク先を持たない RefRule の雛形）。"""

    name: str
    concept: str  # `ref_link_masters` のキー（byomei / exam）
    role: RefRole = "code"
    logical_pattern: str | None = None
    physical_pattern: str | None = None
    exclude_pattern: str | None = None


# 記載順が強い順（同一列×同一マスタは先のパターンを採る）。
CONCEPT_PATTERNS: list[ConceptPattern] = [
    ConceptPattern(
        name="byomei_code",
        concept="byomei",
        role="code",
        logical_pattern=r"(病名|傷病|icd).*(コード|cd)|(コード|cd).*(病名|傷病)",
        exclude_pattern=_EXCLUDE_COMMON,
    ),
    ConceptPattern(
        name="byomei_name",
        concept="byomei",
        role="name",
        logical_pattern=r"病名|傷病名",
        exclude_pattern=_EXCLUDE_COMMON + r"|コード|cd\b|番号",
    ),
    ConceptPattern(
        name="exam_code",
        concept="exam",
        role="code",
        logical_pattern=r"(検査項目|jlac).*(コード|cd)|jlac10?",
        physical_pattern=r"jlac",
        exclude_pattern=_EXCLUDE_COMMON,
    ),
    ConceptPattern(
        name="exam_name",
        concept="exam",
        role="name",
        logical_pattern=r"検査項目",
        exclude_pattern=_EXCLUDE_COMMON + r"|コード|cd\b|番号",
    ),
]


def rules_for(masters: Mapping[str, MasterRef]) -> list[RefRule]:
    """概念パターンに施設のマスタ表を当てて RefRule にする（マスタの無い概念は落とす）。"""
    rules: list[RefRule] = []
    for p in CONCEPT_PATTERNS:
        m = masters.get(p.concept)
        if m is None:
            continue
        rules.append(
            RefRule(
                name=p.name,
                master_connection_id=m.connection_id,
                master_db_qualified=m.db_qualified,
                role=p.role,
                logical_pattern=p.logical_pattern,
                physical_pattern=p.physical_pattern,
                exclude_pattern=p.exclude_pattern,
            )
        )
    return rules


def draft_rule_links(
    units: list[ResourceUnit], rules: list[RefRule] | None = None
) -> list[RefLink]:
    """db ユニットの列名メタにルールを当て、status="rule" のリンク下書きを作る。

    `rules` 省略時は施設の `indexer_rules.yaml` のマスタ表から組む（無ければ下書きしない）。
    """
    if rules is None:
        rules = rules_for(load_indexer_rules().ref_link_masters)
    if not rules:
        logger.info("参照リンクのマスタ表が施設の索引規則に無いので、ルールでは下書きしない")
        return []
    links: list[RefLink] = []
    seen: set[tuple[str, str, str]] = set()
    for u in units:
        if u.kind != Kind.DB:
            continue
        logical = _norm(u.payload.get("logical_name"))
        physical = _norm(u.payload.get("physical_name"))
        for r in rules:
            hit = bool(
                (r.logical_pattern and logical and re.search(r.logical_pattern, logical))
                or (r.physical_pattern and physical and re.search(r.physical_pattern, physical))
            )
            if not hit:
                continue
            if r.exclude_pattern and logical and re.search(r.exclude_pattern, logical):
                continue
            key = (u.unit_id, r.master_connection_id, r.master_db_qualified)
            if key in seen:
                continue  # 同一列×同一マスタは最初のルール（記載順=強い順）を採る
            seen.add(key)
            links.append(
                RefLink(
                    column_unit_id=u.unit_id,
                    master_connection_id=r.master_connection_id,
                    master_db_qualified=r.master_db_qualified,
                    status="rule",
                    role=r.role,
                    source=f"rule:{r.name}",
                )
            )
    logger.info(f"ルールベースで {len(links)} リンクを下書きしました")
    return links


# --------------------------------------------------------------------------- #
# LLM 推論（ローカル Ollama。既存 drafter 思想: 下書き→人確認・非提示 ID は不受理）
# --------------------------------------------------------------------------- #
# LLM に判定させるマスタ概念はプロンプトの語彙（byomei / exam）。リンク先は施設の
# `indexer_rules.yaml` の `ref_link_masters` から引く（書かれていない概念は捨てる）。
# LLM 候補の事前絞り込み: 概念語を含む列だけを判定にかける（全列走査はしない）。
_LLM_CANDIDATE_RE = re.compile(r"病名|傷病|疾患|icd|診断|検査項目|検体|jlac|検査(結果|値|名)")
_LLM_CHUNK = 8

_LLM_SYS = """あなたは病院情報システムの DB スキーマに詳しいデータアーキテクトです。
DB カラムのメタデータ（表・論理名・物理名・型・メモ）だけを見て、そのカラムに何が
格納されるかを判定してください。値のプロファイリング結果はありません。

判定: 各カラムについて master を1つ選ぶ。
- "byomei": 病名・傷病名のコードまたは名称が格納される（ICD10・レセ電・院内病名コード、病名文字列）
- "exam":   検体検査項目のコードまたは名称が格納される（JLAC10・院内検査項目コード、検査項目名）
- "none":   上記のいずれでもない（区分・フラグ・日付・数量・その他）

role はコード値なら "code"、名称文字列なら "name"。
確信が持てない場合は "none" にしてください（過剰リンクは検索を汚します）。
提示された unit_id のみを返し、判定できたものだけ含めてください。"""


class _LlmJudgement(BaseModel):
    unit_id: str
    master: Literal["byomei", "exam", "none"]
    role: RefRole = "code"


class _LlmBatch(BaseModel):
    judgements: list[_LlmJudgement] = Field(default_factory=list)


def llm_candidates(units: list[ResourceUnit], exclude_ids: set[str]) -> list[ResourceUnit]:
    """LLM 判定にかける候補列（概念語を含み、ルール済みでない db 列）を返す。

    ルールの除外パターン（区分・フラグ・日付等）に当たる列は決定的に「値域でない」と
    判るので LLM にも回さない（判定コストと過剰リンクの両方を抑える）。
    """
    out: list[ResourceUnit] = []
    for u in units:
        if u.kind != Kind.DB or u.unit_id in exclude_ids:
            continue
        logical = _norm(u.payload.get("logical_name"))
        hay = logical + " " + _norm(u.payload.get("notes"))
        if not _LLM_CANDIDATE_RE.search(hay):
            continue
        if logical and re.search(_EXCLUDE_COMMON, logical):
            continue
        out.append(u)
    return out


def _column_line(u: ResourceUnit) -> str:
    p = u.payload
    bits = [
        f"unit_id={u.unit_id}",
        f"表={p.get('db_qualified')}（{p.get('table_logical') or '-'}）",
        f"論理名={p.get('logical_name') or '-'}",
        f"物理名={p.get('physical_name')}",
        f"型={p.get('type_raw') or '-'}",
    ]
    if p.get("notes"):
        bits.append(f"メモ={p['notes']}")
    return " / ".join(bits)


async def draft_llm_links(
    candidates: list[ResourceUnit],
    model: str,
    client: Any = None,
    masters: Mapping[str, MasterRef] | None = None,
) -> list[RefLink]:
    """候補列を LLM で判定し、status="inferred" のリンク下書きを作る。

    既存 drafter と同じ規律: 提示した unit_id 以外の応答は捨てる（幻覚防止）。
    client は注入可能（テストはスタブで駆動。既定は ollama.AsyncClient）。
    `masters`（概念 → マスタ表）省略時は施設の `indexer_rules.yaml` から読む。
    空なら LLM を呼ばずに空を返す。
    """
    from kawasemi_indexer.llm_json import chat_json_chunked

    if masters is None:
        masters = load_indexer_rules().ref_link_masters
    if not masters or not candidates:
        if not masters:
            logger.info("参照リンクのマスタ表が施設の索引規則に無いので、LLM では下書きしない")
        return []

    if client is None:
        import ollama

        from kawasemi_indexer.config import settings

        client = ollama.AsyncClient(host=settings.ollama_base_url)

    presented = {u.unit_id for u in candidates}

    def _build_message(chunk: list[ResourceUnit]) -> str:
        return "次の DB カラムを判定してください:\n" + "\n".join(_column_line(u) for u in chunk)

    def _parse(data: dict[str, Any]) -> list[_LlmJudgement]:
        return _LlmBatch(**data).judgements

    judged: list[_LlmJudgement] = await chat_json_chunked(
        client,
        model,
        candidates,
        system_prompt=_LLM_SYS,
        build_message=_build_message,
        response_format=_LlmBatch.model_json_schema(),
        parse=_parse,
        label="参照リンク推論",
        chunk_size=_LLM_CHUNK,
    )

    links: list[RefLink] = []
    dropped = 0
    for j in judged:
        if j.master == "none":
            continue
        if j.unit_id not in presented:
            dropped += 1  # 提示していない unit_id（幻覚）は不受理
            continue
        target = masters.get(j.master)
        if target is None:
            continue  # 施設がマスタ表を書いていない概念
        links.append(
            RefLink(
                column_unit_id=j.unit_id,
                master_connection_id=target.connection_id,
                master_db_qualified=target.db_qualified,
                status="inferred",
                role=j.role,
                source=f"llm:{model}",
            )
        )
    if dropped:
        logger.warning(f"LLM 応答のうち提示外 unit_id を {dropped} 件棄却しました")
    logger.info(f"LLM 推論で {len(links)} リンクを下書きしました")
    return links


def merge_links(rule_links: list[RefLink], llm_links: list[RefLink]) -> list[RefLink]:
    """ルール由来と LLM 由来を統合する（同一 列×マスタ はルール優先）。"""
    seen = {
        (ln.column_unit_id, ln.master_connection_id, ln.master_db_qualified) for ln in rule_links
    }
    merged = list(rule_links)
    for ln in llm_links:
        key = (ln.column_unit_id, ln.master_connection_id, ln.master_db_qualified)
        if key not in seen:
            seen.add(key)
            merged.append(ln)
    return merged


def _read_db_units(index_dir: str | Path) -> list[ResourceUnit]:
    """索引ディレクトリの db_index.jsonl を ResourceUnit として読む（embedding 不要）。"""
    path = Path(index_dir) / Kind.INDEX_FILENAMES[Kind.DB]
    units: list[ResourceUnit] = []
    if not path.exists():
        return units
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                units.append(ResourceUnit(**json.loads(line)))
    return units
