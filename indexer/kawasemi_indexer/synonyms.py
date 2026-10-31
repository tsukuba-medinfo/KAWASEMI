"""標準コード変換テーブル（synonym_index.jsonl）とクエリ書き換え層。

施設マスタ（kind=master）は 1エントリ内に 製品名⇔一般名⇔HOT/YJ/
レセ電/ICD10/JLAC10 を同居させている（builder.build_master_units と、施設側のマスタ抽出スクリプト）。
現状これは aliases 文字列として search_text に畳み込まれるだけで、**コード体系間の
変換辞書としては機能していない**。

本モジュールはマスタ索引を再編成し、`code ↔ label ↔ aliases` の正規化テーブルを
成果物として切り出す（新規データ取得なし・決定的・監査可能・embedding 非介在）。
クエリ書き換え層は、クエリがテーブルの語（一般名・製品名・コード）に **完全一致** したら、
同一エントリの別名・標準コードでクエリを展開してから embedding / 字面検索に流す。

過剰マージ（語彙グラフの推移閉包による無関係語の連鎖）を避けるため、展開は
**一段（クエリ語 → それを含むエントリ → そのエントリの他の語）** に限定する。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kawasemi_indexer import lexical
from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.schema import SYNONYM_INDEX_FILENAME as _SYN_FILE
from kawasemi_indexer.schema import Kind, ResourceUnit

logger = get_logger(__name__)

# 1 エントリが引く展開語・1 クエリが当てるエントリ数の上限（暴発防止・監査性）。
_MAX_ENTRIES_PER_QUERY = 20
_MAX_EXPANSIONS = 40

# コードらしさ: 正規化後（NFKC/小文字）に ASCII 英数と一部記号のみ・数字を含む・2文字以上。
# HOT13(数字13)・YJ(英数12桁)・レセ電(9桁)・ICD10(D481)・JLAC10(数字) を幅広く拾う。
_CODE_RE = re.compile(r"^[0-9a-z][0-9a-z.\-]*$")


def classify_term(term_norm: str) -> str:
    """正規化済みの語を "code"（標準コード類）か "name"（名称類）に分類する。"""
    if len(term_norm) >= 2 and _CODE_RE.match(term_norm) and any(c.isdigit() for c in term_norm):
        return "code"
    return "name"


class SynonymEntry(BaseModel):
    """変換テーブルの 1 行（マスタ 1 エントリを名称群/コード群に再編成したもの）。

    横断展開に使うのは names（名称類）と codes（**標準**コード類。aliases 由来の
    HOT/YJ/レセ電/ICD10/JLAC10）のみ。internal_code（payload.code = 院内オーダ/病名
    コード）は接続ローカルで衝突しうるため terms に含めず、表示・特定用に保持する。
    """

    unit_id: str
    label: str
    connection_id: str | None = None
    db_qualified: str | None = None
    internal_code: str | None = Field(
        default=None, description="院内コード（横断展開には使わない）"
    )
    names: list[str] = Field(default_factory=list, description="名称類（製品名/一般名/略称/カナ）")
    codes: list[str] = Field(default_factory=list, description="標準コード類（HOT/YJ/ICD10 等）")

    @property
    def terms(self) -> list[str]:
        """横断展開・検索キーに使う語群（名称＋標準コード。院内コードは含めない）。"""
        return [*self.names, *self.codes]


def _iter_master_units(index_dir: str | Path) -> list[ResourceUnit]:
    """索引ディレクトリの master_index.jsonl を ResourceUnit として読む（embedding 不要）。"""
    path = Path(index_dir) / Kind.INDEX_FILENAMES[Kind.MASTER]
    units: list[ResourceUnit] = []
    if not path.exists():
        return units
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                units.append(ResourceUnit(**json.loads(line)))
    return units


def build_synonym_entries(units: list[ResourceUnit]) -> list[SynonymEntry]:
    """マスタユニット群を変換テーブルのエントリへ再編成する。

    各エントリの label + aliases + code を正規化して重複排除し、名称/コードに分類する。
    名称もコードも 1 件のみ（別名なし）のエントリは変換価値が無いので落とす。
    """
    entries: list[SynonymEntry] = []
    for u in units:
        if u.kind != Kind.MASTER:
            continue
        p = u.payload or {}
        # label + aliases のみを横断語に。payload.code(院内コード)は internal_code へ退避。
        raw_terms: list[Any] = [u.label]
        aliases = p.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        raw_terms.extend(aliases)

        names: list[str] = []
        codes: list[str] = []
        seen: set[str] = set()
        for raw in raw_terms:
            if raw is None:
                continue
            display = str(raw).strip()
            norm = lexical.normalize(display)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            (codes if classify_term(norm) == "code" else names).append(display)

        # 展開できる相手が居ない（横断語が1個以下）エントリは変換テーブルに載せない。
        if len(seen) < 2:
            continue
        entries.append(
            SynonymEntry(
                unit_id=u.unit_id,
                label=u.label,
                connection_id=p.get("connection_id"),
                db_qualified=p.get("db_qualified"),
                internal_code=(str(p["code"]) if p.get("code") is not None else None),
                names=names,
                codes=codes,
            )
        )
    return entries


def write_synonym_index(entries: list[SynonymEntry], out_dir: str | Path) -> Path:
    """変換テーブルを synonym_index.jsonl として書き出す（1行=1エントリ）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / _SYN_FILE
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e.model_dump(), ensure_ascii=False) + "\n")
    logger.info(f"変換テーブルを書き出しました: {path}（{len(entries)}エントリ）")
    return path


def build_synonym_index(index_dir: str | Path, out_dir: str | Path | None = None) -> Path:
    """索引ディレクトリの master_index.jsonl から変換テーブルを生成して書き出す。"""
    units = _iter_master_units(index_dir)
    entries = build_synonym_entries(units)
    return write_synonym_index(entries, out_dir or index_dir)


# --------------------------------------------------------------------------- #
# クエリ書き換え
# --------------------------------------------------------------------------- #
class RewriteResult(BaseModel):
    """クエリ書き換えの結果（展開語と展開元の明示）。"""

    query: str
    expansions: list[str] = Field(default_factory=list, description="追加する別名・標準コード群")
    provenance: list[dict[str, Any]] = Field(
        default_factory=list, description="展開の由来（unit_id/label/matched_term）"
    )

    @property
    def matched(self) -> bool:
        return bool(self.expansions)

    @property
    def all_terms(self) -> list[str]:
        """字面チャネルに渡す語群（元クエリ＋展開語）。"""
        return [self.query, *self.expansions]

    @property
    def expanded_text(self) -> str:
        """embedding に渡す拡張テキスト（元クエリに展開語を連結）。"""
        return " ".join([self.query, *self.expansions]) if self.expansions else self.query


class QueryRewriter:
    """synonym_index.jsonl を引いてクエリを別名・標準コードへ一段展開する。"""

    def __init__(self, entries: list[SynonymEntry]) -> None:
        self._entries = entries
        # 正規化語 → その語を含むエントリ index（一段展開のための転置索引）。
        self._by_term: dict[str, list[int]] = {}
        for i, e in enumerate(entries):
            for t in e.terms:
                self._by_term.setdefault(lexical.normalize(t), []).append(i)

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @classmethod
    def load(cls, index_dir: str | Path) -> QueryRewriter:
        """synonym_index.jsonl を読み込む。未配置なら空の rewriter（no-op）。"""
        path = Path(index_dir) / _SYN_FILE
        entries: list[SynonymEntry] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        entries.append(SynonymEntry(**json.loads(line)))
            logger.info(f"変換テーブルをロード: {path}（{len(entries)}エントリ）")
        return cls(entries)

    def rewrite(self, query: str) -> RewriteResult:
        """クエリがテーブル語に完全一致したら、同一エントリの別名・コードへ一段展開する。"""
        qn = lexical.normalize(query)
        result = RewriteResult(query=query)
        if not qn or not self._by_term:
            return result

        matched_idx = self._by_term.get(qn, [])[:_MAX_ENTRIES_PER_QUERY]
        seen_exp: set[str] = {qn}
        for i in matched_idx:
            e = self._entries[i]
            added: list[str] = []
            for t in e.terms:
                tn = lexical.normalize(t)
                if tn in seen_exp:
                    continue
                seen_exp.add(tn)
                added.append(t)
                result.expansions.append(t)
                if len(result.expansions) >= _MAX_EXPANSIONS:
                    break
            if added:
                result.provenance.append(
                    {"unit_id": e.unit_id, "label": e.label, "matched_term": query, "added": added}
                )
            if len(result.expansions) >= _MAX_EXPANSIONS:
                break
        return result
