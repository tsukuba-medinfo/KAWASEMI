"""KnowledgeDrafter: 確認済知識層の初回下書きを LLM で生成する。

施設ごとにバラバラな Excel/CSV レイアウトと information_schema を見て、マッピング・表役割・
患者キー・論理名補間の **下書き** を生成する。出力はそのまま使わず、必ず人が確認・修正して
確定する（draft → confirmed）。患者データ行は見ない（ヘッダ・物理名・型・論理名のみ）。

設計: docs/kawasemi_indexer_spec.md §5/§6/§9。LLM呼び出しは llm_json.chat_json に集約、
client は注入可能（テストは Ollama 非依存でスタブ）。key_type は key_types カタログに
照合し、未知なら None（捏造防止）。value_domains は名称ヒューリスティックで下書きする。
"""

from __future__ import annotations

import fnmatch
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from kawasemi_indexer.config import settings
from kawasemi_indexer.ingestion.models import (
    Enrichment,
    KnowledgeLayer,
    MasterTableSpec,
    PatientKeyBinding,
    SchemaSnapshot,
    TableSnapshot,
    TabularSource,
)
from kawasemi_indexer.ingestion.tabular import read_headers_and_records
from kawasemi_indexer.key_types import KeyTypeCatalog, get_catalog
from kawasemi_indexer.llm_json import chat_json
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)

# マッピング下書きでLLMへ見せるサンプル行数（値域の手掛かり。患者データ表でも
# ヘッダ解釈にデータ例は不要なため、定義書=メタ文書のみ対象＝PHI非該当）。
_SAMPLE_ROWS = 5
# 分類・補間で1コールに詰める表/列の上限（num_ctx 超過回避）。
_CHUNK = 8

_CANONICAL_FIELDS = ("table_id", "table_logical", "physical_name", "logical_name", "notes")

_STEM_SUFFIX_RE = re.compile(r"(_(MASTER|MST|CODE|CD|ID|KBN|KUBUN))+$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# LLM 応答スキーマ
# --------------------------------------------------------------------------- #


class _MappingOut(BaseModel):
    table_id: str | None = None
    table_logical: str | None = None
    physical_name: str | None = None
    logical_name: str | None = None
    notes: str | None = None


class _TableClass(BaseModel):
    table_id: str
    role: Literal["master", "patient_data"]
    code_column: str | None = None
    label_column: str | None = None


class _ClassifyOut(BaseModel):
    tables: list[_TableClass] = []


class _KeyOut(BaseModel):
    table_id: str
    physical_name: str | None = None
    key_type: str | None = None


class _KeysOut(BaseModel):
    keys: list[_KeyOut] = []


class _EnrichOut(BaseModel):
    db_qualified: str
    physical_name: str
    logical_name: str | None = None


class _EnrichmentsOut(BaseModel):
    items: list[_EnrichOut] = []


# --------------------------------------------------------------------------- #
# プロンプト
# --------------------------------------------------------------------------- #

_MAPPING_SYS = """あなたはDB定義書を読むアシスタントです。施設ごとに列構成が異なる定義書の
ヘッダ行を見て、各「正規フィールド」がどのヘッダ列に対応するかを判定してください。
正規フィールド: table_id(表物理名), table_logical(表論理名), physical_name(カラム物理名),
logical_name(カラム論理名), notes(備考)。対応するヘッダ名をそのまま値に入れる。
該当ヘッダが無ければ null。推測でヘッダ名を作らない。"""

_CLASSIFY_SYS = """あなたはDBスキーマを分類するアシスタントです。各表を role に分類します。
- master: コード→意味の対応表（性別・部門・検査項目コード等の参照表）。患者個人データを持たない。
- patient_data: 患者個人のデータ（検査値・受付・診療記録等）を持つトランザクション表。
master の場合のみ code_column（コード列）と label_column（名称列）を物理名で示す。
迷う場合は安全側＝patient_data。必ず与えた全表を返す。"""

_KEYS_SYS = """あなたは患者識別のアシスタントです。各表について患者を一意に識別するキー列
（患者番号・MRN等）の物理名を physical_name に示します。さらに、与えた key_type 一覧の中から
最も妥当な key_type の id を選びます（一覧に無ければ null。捏造しない）。
患者キーが無い表は physical_name=null。必ず与えた全表を返す。"""

_ENRICH_SYS = """あなたはカラムの論理名を推定するアシスタントです。物理名と表の文脈から、
日本語の論理名（人間が読む項目名）を推定して logical_name に入れます。確信が持てない場合は
null。推測は後で人が確認するため、それらしい妥当な日本語名を簡潔に与えてください。"""


# --------------------------------------------------------------------------- #
# 補間対象の選択（オンデマンド化・優先度付け）
# --------------------------------------------------------------------------- #


@dataclass
class EnrichSelection:
    """論理名補間の対象列と、絞り込みの内訳（大規模スキーマの費用可視化）。"""

    targets: list[tuple[str, str]] = field(
        default_factory=list
    )  # (db_qualified, physical) 優先度順
    total_columns: int = 0  # スナップショット全列
    doc_covered: int = 0  # 定義書で論理名が付く見込み（対象外）
    already_enriched: int = 0  # 既補間・人確認済み（only-missing で対象外）
    table_filtered: int = 0  # --tables に非該当（対象外）
    eligible: int = 0  # 絞り込み後の候補（limit 適用前）
    priority_note: str = ""  # 優先度付けの説明

    @property
    def selected(self) -> int:
        return len(self.targets)

    @property
    def dropped_by_limit(self) -> int:
        return max(0, self.eligible - self.selected)

    def summary(self) -> str:
        line = (
            f"全{self.total_columns}列 / 定義書被覆{self.doc_covered} / "
            f"既補間{self.already_enriched} / 表フィルタ除外{self.table_filtered} / "
            f"候補{self.eligible} → 補間{self.selected}"
        )
        if self.dropped_by_limit:
            line += f"（limit で{self.dropped_by_limit}列を後回し）"
        if self.priority_note:
            line += f" / 優先度: {self.priority_note}"
        return line


def _enrich_norm(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s if s is not None else "")).lower()


def _table_matches(table: TableSnapshot, patterns: list[str]) -> bool:
    """--tables のグロブパターンに table_id か db_qualified が（小文字で）一致するか。"""
    tid = table.table_id.lower()
    dbq = table.db_qualified.lower()
    return any(fnmatch.fnmatch(tid, p) or fnmatch.fnmatch(dbq, p) for p in patterns)


def select_enrichment_targets(
    snapshot: SchemaSnapshot,
    sources: list[TabularSource],
    *,
    already_enriched: set[str] | None = None,
    table_patterns: list[str] | None = None,
    priority_tables: set[str] | None = None,
    near_terms: list[str] | None = None,
    limit: int | None = None,
) -> EnrichSelection:
    """補間すべき列を優先度順に選び、絞り込みの内訳を返す（純関数・LLM非依存）。

    絞り込み順:
    1. 定義書で論理名が付く見込みの列を除外（従来の draft_enrichments と同じ前提）。
    2. already_enriched（'db_qualified.physical_name'）に載る列を除外（only-missing・冪等）。
    3. table_patterns 指定時、非該当表の列を除外（--tables）。
    優先度（降順・limit で上位のみ採用）:
    - 患者キー保持表 / patient_data 表（priority_tables）の列を先に（検索の当たりが良い）。
    - near_terms に物理名・表名が字面一致する列を加点（拾えなかった項目起点の近傍補間）。
    同点は (db_qualified, physical_name) 昇順で安定。limit で上位のみに絞る。
    """
    already = already_enriched or set()
    patterns = [p.lower() for p in (table_patterns or [])]
    prio_tables = priority_tables or set()
    terms = [_enrich_norm(t) for t in (near_terms or []) if t and t.strip()]
    covered = _doc_covered_logical(sources)

    sel = EnrichSelection()
    scored: list[tuple[int, str, str]] = []  # (優先度スコア, db_qualified, physical_name)
    for tbl in snapshot.tables:
        table_ok = (not patterns) or _table_matches(tbl, patterns)
        for col in tbl.columns:
            sel.total_columns += 1
            if (tbl.table_id, col.physical_name) in covered or (
                tbl.db_qualified,
                col.physical_name,
            ) in covered:
                sel.doc_covered += 1
                continue
            if f"{tbl.db_qualified}.{col.physical_name}" in already:
                sel.already_enriched += 1
                continue
            if not table_ok:
                sel.table_filtered += 1
                continue
            score = 10 if tbl.table_id in prio_tables else 0
            if terms:
                hay = _enrich_norm(col.physical_name) + " " + _enrich_norm(tbl.table_id)
                score += sum(1 for t in terms if t in hay)
            scored.append((score, tbl.db_qualified, col.physical_name))

    sel.eligible = len(scored)
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))  # 優先度降順 → 名前昇順で安定
    chosen = scored if limit is None else scored[: max(0, limit)]
    sel.targets = [(dbq, phys) for _score, dbq, phys in chosen]

    notes = []
    if prio_tables:
        notes.append("患者キー/patient_data表を優先")
    if terms:
        notes.append(f"近傍語 {near_terms}")
    sel.priority_note = "、".join(notes) if notes else "既定（表・列名順）"
    return sel


# --------------------------------------------------------------------------- #
# Drafter
# --------------------------------------------------------------------------- #


class KnowledgeDrafter:
    """確認済知識層の下書きを LLM で生成する（人確認前提）。"""

    def __init__(
        self,
        model: str | None = None,
        client: Any | None = None,
        catalog: KeyTypeCatalog | None = None,
    ) -> None:
        self._model = (model or settings.ollama_model).strip()
        self._catalog = catalog
        if client is not None:
            self._client = client
        else:
            import ollama

            self._client = ollama.AsyncClient(
                host=settings.ollama_base_url, timeout=settings.ollama_timeout
            )

    async def draft(
        self,
        *,
        connection_id: str,
        db_group: str,
        snapshot: SchemaSnapshot,
        tabular_paths: list[str] | None = None,
        enrich_only_missing: bool = False,
        enrich_tables: list[str] | None = None,
        enrich_limit: int | None = None,
        enrich_near: list[str] | None = None,
        enrich_dry_run: bool = False,
        existing: KnowledgeLayer | None = None,
    ) -> KnowledgeLayer:
        """マッピング・役割・患者キー・補間・値ドメインの下書き KnowledgeLayer を返す。

        補間の絞り込み: tables/limit/near/dry_run で論理名補間の対象を制御できる。
        `existing`（既存の knowledge YAML）を渡すと、構造的な下書き（マッピング・役割・患者キー・
        値ドメイン）はそれを再利用し、**未補間の列だけ**を追補する（大規模スキーマの再実行=
        checkpoint を安価にする）。既存の補間内容（人が confirmed にしたものを含む）は保持し
        上書きしない。enrich_only_missing は CLI 互換のため残すが、既存を渡した時点で常に
        未補間のみ追補になる（--resume 単独でも補間コストが下がる）。
        """
        reuse = existing is not None
        if reuse:
            assert existing is not None
            sources = list(existing.tabular_sources)
            roles: dict[str, Any] = dict(existing.table_roles)
            master_tables = dict(existing.master_tables)
            patient_keys = dict(existing.patient_keys)
            value_domains = dict(existing.value_domains)
            logger.info("既存 knowledge を再利用（構造下書きを再生成せず補間のみ追補）")
        else:
            sources = []
            for path in tabular_paths or []:
                src = await self.draft_mapping(path)
                if src is not None:
                    sources.append(src)
            roles, master_tables = await self.draft_classification(snapshot)
            patient_keys = await self.draft_patient_keys(snapshot, roles)
            value_domains = draft_value_domains(snapshot, roles, patient_keys)

        # 優先度: 患者キー保持表と patient_data 表（＝実際に患者値を引く表）の列を先に補間。
        priority_tables = set(patient_keys) | {
            t.table_id for t in snapshot.tables if roles.get(t.table_id) != "master"
        }
        # 既存 knowledge を土台にする場合（reuse=既存あり。--resume / --only-missing とも
        # existing 経由）、既に論理名が付いている補間（inferred/confirmed とも）は LLM に
        # 再送しない。checkpoint 再開時に既補間列ぶんの補間コストが丸ごと無駄になるのを防ぐ
        # （--resume 単独でも補間コストが下がる）。
        already = (
            {k for k, e in existing.enrichments.items() if e.logical_name}
            if existing is not None
            else None
        )
        new_enrichments = await self.draft_enrichments(
            snapshot,
            sources,
            already_enriched=already,
            table_patterns=enrich_tables,
            priority_tables=priority_tables,
            near_terms=enrich_near,
            limit=enrich_limit,
            dry_run=enrich_dry_run,
        )
        # 既存の補間（人確認済み含む）を保持しつつ、新規で欠落を埋める（既存優先）。
        enrichments = {**new_enrichments, **(existing.enrichments if existing is not None else {})}

        logger.info(
            f"知識層下書き完了: connection={connection_id} sources={len(sources)} "
            f"roles={len(roles)} keys={len(patient_keys)} enrich={len(enrichments)} "
            f"(新規{len(new_enrichments)}) value_domains={len(value_domains)}"
        )
        return KnowledgeLayer(
            connection_id=connection_id,
            db_group=db_group,
            tabular_sources=sources,
            table_roles=roles,
            patient_keys=patient_keys,
            master_tables=master_tables,
            value_domains=value_domains,
            enrichments=enrichments,
        )

    async def draft_mapping(self, path: str) -> TabularSource | None:
        """1つの定義書のヘッダ行から、正規フィールド→ヘッダ名のマッピングを下書きする。"""
        headers, samples = _read_headers_and_samples(path)
        if not headers:
            logger.warning(f"マッピング下書きスキップ（ヘッダ取得不可）: {path}")
            return None
        msg = _mapping_message(headers, samples)
        data = await chat_json(
            self._client,
            self._model,
            system_prompt=_MAPPING_SYS,
            user_message=msg,
            response_format=_MappingOut.model_json_schema(),
            label="マッピング下書き",
        )
        out = _MappingOut.model_validate(data)
        # 実在ヘッダに一致した対応のみ採用（捏造ヘッダを弾く）
        header_set = set(headers)
        columns = {
            f: getattr(out, f)
            for f in _CANONICAL_FIELDS
            if getattr(out, f) and getattr(out, f) in header_set
        }
        if "physical_name" not in columns or "table_id" not in columns:
            logger.warning(
                f"マッピング下書きが必須列(table_id/physical_name)を解決できず: {path}（要人手）"
            )
        return TabularSource(path=path, columns=columns)

    async def draft_classification(
        self, snapshot: SchemaSnapshot
    ) -> tuple[dict[str, Any], dict[str, MasterTableSpec]]:
        """表を master/patient_data に分類し、master の code/label 列を下書きする。"""
        roles: dict[str, Any] = {}
        masters: dict[str, MasterTableSpec] = {}
        for chunk in _chunks(snapshot.tables, _CHUNK):
            data = await chat_json(
                self._client,
                self._model,
                system_prompt=_CLASSIFY_SYS,
                user_message=_classify_message(chunk),
                response_format=_ClassifyOut.model_json_schema(),
                label="表役割分類下書き",
            )
            out = _ClassifyOut.model_validate(data)
            valid = {t.table_id for t in chunk}
            for tc in out.tables:
                if tc.table_id not in valid:
                    continue
                roles[tc.table_id] = tc.role
                if tc.role == "master" and tc.code_column and tc.label_column:
                    tbl = snapshot.table(tc.table_id)
                    cols = {c.physical_name for c in tbl.columns} if tbl else set()
                    if tc.code_column in cols and tc.label_column in cols:
                        masters[tc.table_id] = MasterTableSpec(
                            code_column=tc.code_column, label_column=tc.label_column
                        )
        return roles, masters

    async def draft_patient_keys(
        self, snapshot: SchemaSnapshot, roles: dict[str, Any]
    ) -> dict[str, PatientKeyBinding]:
        """patient_data 表の患者キー列と key_type を下書きする（key_type はカタログ照合）。"""
        catalog = self._catalog or get_catalog()
        valid_key_types = {kt.id for kt in catalog.all()}
        targets = [t for t in snapshot.tables if roles.get(t.table_id) != "master"]
        bindings: dict[str, PatientKeyBinding] = {}
        for chunk in _chunks(targets, _CHUNK):
            data = await chat_json(
                self._client,
                self._model,
                system_prompt=_KEYS_SYS,
                user_message=_keys_message(chunk, sorted(valid_key_types)),
                response_format=_KeysOut.model_json_schema(),
                label="患者キー下書き",
            )
            out = _KeysOut.model_validate(data)
            for k in out.keys:
                tbl = snapshot.table(k.table_id)
                if tbl is None or not k.physical_name:
                    continue
                if k.physical_name not in {c.physical_name for c in tbl.columns}:
                    continue  # 実在しない列は弾く（捏造防止）
                key_type = k.key_type if k.key_type in valid_key_types else None
                bindings[k.table_id] = PatientKeyBinding(
                    physical_name=k.physical_name, key_type=key_type
                )
        return bindings

    async def draft_enrichments(
        self,
        snapshot: SchemaSnapshot,
        sources: list[TabularSource],
        *,
        already_enriched: set[str] | None = None,
        table_patterns: list[str] | None = None,
        priority_tables: set[str] | None = None,
        near_terms: list[str] | None = None,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Enrichment]:
        """定義書で論理名が埋まらない列に、論理名の下書き（inferred）を付ける。

        既定は全対象列を補間（従来動作）。only-missing（already_enriched）・表指定
        （table_patterns）・近傍語（near_terms）・上限（limit）で対象を絞れる（大規模スキーマで
        全列一括の LLM コストを避ける）。定義書で logical_name が付く列は
        従来どおり対象外（下書きの肥大化防止）。dry_run は選択のみで LLM を呼ばない。
        """
        selection = select_enrichment_targets(
            snapshot,
            sources,
            already_enriched=already_enriched,
            table_patterns=table_patterns,
            priority_tables=priority_tables,
            near_terms=near_terms,
            limit=limit,
        )
        logger.info(f"論理名補間 選択: {selection.summary()}")
        if dry_run:
            print(f"[補間 dry-run] {selection.summary()}")
            preview = selection.targets[:20]
            for dbq, phys in preview:
                print(f"  - {dbq}.{phys}")
            if selection.selected > len(preview):
                print(f"  … 他 {selection.selected - len(preview)} 列")
            est_calls = (selection.selected + _CHUNK - 1) // _CHUNK
            print(f"  推定 LLM 呼び出し回数: {est_calls}（{_CHUNK}列/回）")
            return {}
        if not selection.targets:
            return {}
        sent = set(selection.targets)  # 送った列だけ採用（捏造列を弾く・他サブタスクと一貫）
        enrichments: dict[str, Enrichment] = {}
        llm_calls = 0
        started = time.perf_counter()
        for chunk in _chunks(selection.targets, _CHUNK):
            llm_calls += 1
            data = await chat_json(
                self._client,
                self._model,
                system_prompt=_ENRICH_SYS,
                user_message=_enrich_message(chunk),
                response_format=_EnrichmentsOut.model_json_schema(),
                label="論理名補間下書き",
            )
            out = _EnrichmentsOut.model_validate(data)
            for e in out.items:
                if not e.logical_name or (e.db_qualified, e.physical_name) not in sent:
                    continue  # 空 or 送っていない列（捏造）は弾く
                enrichments[f"{e.db_qualified}.{e.physical_name}"] = Enrichment(
                    logical_name=e.logical_name, inferred=True, confirmed=False
                )
        elapsed = time.perf_counter() - started
        logger.info(
            f"論理名補間 完了: {len(enrichments)}件採用 / LLM呼び出し{llm_calls}回 / "
            f"{elapsed:.1f}秒"
        )
        return enrichments


# --------------------------------------------------------------------------- #
# 値ドメイン下書き（名称ヒューリスティック・非LLM）
# --------------------------------------------------------------------------- #


def _stem(name: str) -> str:
    """末尾の _MASTER/_CODE/_CD/_ID 等を除いた語幹（大文字化）。"""
    return _STEM_SUFFIX_RE.sub("", name or "").upper()


def draft_value_domains(
    snapshot: SchemaSnapshot,
    roles: dict[str, Any],
    patient_keys: dict[str, PatientKeyBinding] | None = None,
) -> dict[str, str]:
    """非master列の語幹が master表の語幹と一致する場合に「列→master」を下書きする。

    例: 列 SEX_CD（語幹 SEX）と master 表 SEX_MASTER（語幹 SEX）→ 'T.SEX_CD': 'SEX_MASTER'。
    あくまで下書き（人が確認）。確証の無い対応は出さない（語幹完全一致のみ）。

    PK 列・患者キー列は除外する。これらは実体識別子であってコード値ではなく、`_ID` の
    語幹剥がしで `PT_ID`→`PT_MASTER` 等の誤マッチを生むため（患者キー列にマスタ語彙が
    畳み込まれると検索が汚れる）。
    """
    masters = {
        t.table_id: _stem(t.table_id) for t in snapshot.tables if roles.get(t.table_id) == "master"
    }
    if not masters:
        return {}
    pkeys = patient_keys or {}
    domains: dict[str, str] = {}
    for tbl in snapshot.tables:
        if roles.get(tbl.table_id) == "master":
            continue
        key_col = pkeys[tbl.table_id].physical_name if tbl.table_id in pkeys else None
        for col in tbl.columns:
            if col.pk or col.physical_name == key_col:
                continue  # 実体識別子は値ドメイン対象外（誤マッチ防止）
            col_stem = _stem(col.physical_name)
            if not col_stem:
                continue
            for master_id, m_stem in masters.items():
                if m_stem and col_stem == m_stem:
                    domains[f"{tbl.table_id}.{col.physical_name}"] = master_id
                    break
    return domains


# --------------------------------------------------------------------------- #
# 入出力ヘルパ
# --------------------------------------------------------------------------- #


def _doc_covered_logical(sources: list[TabularSource]) -> set[tuple[str, str]]:
    """定義書から logical_name が付く (表識別子, physical_name) の集合を返す。"""
    from kawasemi_indexer.ingestion.tabular import read_tabular

    covered: set[tuple[str, str]] = set()
    for src in sources:
        if "logical_name" not in src.columns:
            continue
        try:
            data = read_tabular(src)
        except (FileNotFoundError, ValueError):
            continue
        for row in data.rows:
            if row.logical_name:
                covered.add((row.match_value, row.physical_name))
    return covered


def _read_headers_and_samples(path: str) -> tuple[list[str], list[dict[str, str]]]:
    headers, records = read_headers_and_records(path)
    return headers, records[:_SAMPLE_ROWS]


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _mapping_message(headers: list[str], samples: list[dict[str, str]]) -> str:
    lines = ["定義書のヘッダ一覧:", ", ".join(headers), "", "サンプル行:"]
    for rec in samples:
        lines.append(str(rec))
    lines.append("\n各正規フィールドに対応するヘッダ名を返してください。")
    return "\n".join(lines)


def _classify_message(tables: list[TableSnapshot]) -> str:
    lines = ["以下の各表を role に分類してください:\n"]
    for t in tables:
        cols = ", ".join(c.physical_name for c in t.columns)
        lines.append(f"### {t.table_id}\n  カラム: {cols}")
    return "\n".join(lines)


def _keys_message(tables: list[TableSnapshot], key_types: list[str]) -> str:
    lines = [f"利用可能な key_type: {', '.join(key_types) or '(なし)'}\n", "各表の患者キー列:\n"]
    for t in tables:
        cols = ", ".join(f"{c.physical_name}{'(PK)' if c.pk else ''}" for c in t.columns)
        lines.append(f"### {t.table_id}\n  カラム: {cols}")
    return "\n".join(lines)


def _enrich_message(cols: list[tuple[str, str]]) -> str:
    lines = ["以下の各カラムの論理名を推定してください:\n"]
    for db_qualified, physical in cols:
        lines.append(f"- db_qualified={db_qualified}, physical_name={physical}")
    return "\n".join(lines)
