"""列の充填状況（`column_fill.jsonl`）を読み、所在候補の足切りに使う。

**なぜ要るか。** 索引は定義書から作っているので、実DBに**存在しない列**や
**1件も値が入っていない列**も候補として提示しうる。定義書と実DBが
ずれている施設では、空の列や存在しない列が提示枠の一部を占めうる。

**足切りは0件のときだけである。** 充填率が低いことは「使えない」の証拠にならない。
測ったのは表全体の充填率で、欲しいのは対象集団（例: 特定の疾患の症例）での充填率である。
表全体では充填率の低い列も、対象集団では確定済みの所在でありうる。
**閾値を置くと、狙った症例で必ず入っている列を落とす。**
0件だけは部分集合へ伝播する（全行で0件なら、どの部分集合でも0件である）ので、
これだけが安全に外せる。

**この成果物は embedding 契約に依存しない。** 無ければ足切りをしないだけで、
索引の再構築は要らない（`CONTRACT.md` の任意成果物と同じ扱い）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit

logger = logging.getLogger(__name__)

COLUMN_FILL_FILENAME = "column_fill.jsonl"


@dataclass(frozen=True)
class Fill:
    """1列の充填状況。`exists=False` は実DBにその列が無いこと。"""

    exists: bool
    total_count: int | None
    filled_count: int | None
    filled_rate: float | None
    measured_at: str

    @property
    def is_acquirable(self) -> bool:
        """取得しうるか。**存在しない、または1件も値が無いものは取得しえない。**"""
        return self.exists and bool(self.filled_count)


@dataclass(frozen=True)
class DocTypeReach:
    """文書種別ユニット（`db:…#<文書種別コード>`）の到達可能性。

    **所在の特定と到達可能性は別の問題である。** 文書種別の定義があっても、複製された
    文書（ヘッダ）が1件も無い種別がある（例: 使われていない手術記録の種別）。一方、
    ヘッダがあるのに本文が実DBへ未連携の種別もある（例: 院内の別システムが持つ病理レポート）。
    前者は取得のしようが無いので候補から外し、後者は**外さない**——「所在は特定できるが現在の
    取得経路では引けない」は評価で記録すべき所見で、後者は施設の意思決定（連携の費用を含む調整）に属する。
    """

    has_header: bool
    has_body: bool
    header_count: int | None  # 度数が閾値未満のセルは実数を出さない（小さいセルの秘匿）
    body_count: int | None
    measured_at: str

    @property
    def is_acquirable(self) -> bool:
        """候補として提示してよいか。**落とすのは、ヘッダが1件も無いものだけ**。"""
        return self.has_header

    @property
    def body_unreachable(self) -> bool:
        """所在は特定できるが、現在の取得経路では本文を引けない（ヘッダ有り・本文無し）。"""
        return self.has_header and not self.has_body


class ColumnFillIndex:
    """`(connection_id, db_qualified, physical_name)` から充填状況を引く。

    文書種別ユニットの到達可能性（`unit_id` 鍵の行）も同じファイルから読む。
    """

    def __init__(
        self,
        table: dict[tuple[str, str, str], Fill] | None = None,
        reach: dict[str, DocTypeReach] | None = None,
    ) -> None:
        self._table = table or {}
        self._reach = reach or {}

    def __len__(self) -> int:
        return len(self._table) + len(self._reach)

    @property
    def is_loaded(self) -> bool:
        return bool(self._table) or bool(self._reach)

    def reach_of(self, unit: ResourceUnit) -> DocTypeReach | None:
        """文書種別ユニットの到達可能性。測っていないユニット・文書種別でないユニットは None。"""
        return self._reach.get(unit.unit_id)

    def reach_of_id(self, unit_id: str) -> DocTypeReach | None:
        """`reach_of` の `unit_id` 版（保存済みの候補はユニットではなく `unit_id` を持つ）。"""
        return self._reach.get(unit_id)

    @classmethod
    def load(cls, index_dir: str | Path) -> ColumnFillIndex:
        """未配置・壊れていれば空を返す（足切りをしないだけで検索は動く）。"""
        path = Path(index_dir) / COLUMN_FILL_FILENAME
        if not path.exists():
            logger.info(f"列の充填状況が未配置: {path}（所在候補の足切りをしない）")
            return cls()
        table: dict[tuple[str, str, str], Fill] = {}
        reach: dict[str, DocTypeReach] = {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if "unit_id" in r:
                        # 文書種別ユニットの到達可能性。列の行とは `unit_id` の有無で分ける
                        reach[str(r["unit_id"])] = DocTypeReach(
                            has_header=bool(r["has_header"]),
                            has_body=bool(r["has_body"]),
                            header_count=r.get("header_count"),
                            body_count=r.get("body_count"),
                            measured_at=str(r.get("measured_at") or ""),
                        )
                        continue
                    table[(r["connection_id"], r["db_qualified"], r["physical_name"])] = Fill(
                        exists=bool(r["exists"]),
                        total_count=r.get("total_count"),
                        filled_count=r.get("filled_count"),
                        filled_rate=r.get("filled_rate"),
                        measured_at=str(r.get("measured_at") or ""),
                    )
        except (OSError, KeyError, ValueError) as e:
            logger.error(f"列の充填状況の読み込みエラー: {path}: {e}（足切りをしない）")
            return cls()
        logger.info(
            f"列の充填状況を読み込んだ: {len(table)} 列 / 文書種別の到達可能性 {len(reach)} 件"
        )
        return cls(table, reach)

    def of(self, unit: ResourceUnit) -> Fill | None:
        """そのユニットの充填状況。db 以外の kind と、測っていない列は None。"""
        if unit.kind != Kind.DB:
            return None
        p = unit.payload
        key = (p.get("connection_id"), p.get("db_qualified"), p.get("physical_name"))
        if not all(isinstance(x, str) for x in key):
            return None
        return self._table.get(key)  # type: ignore[arg-type]

    def is_acquirable(self, unit: ResourceUnit) -> bool:
        """所在の候補として提示してよいか。

        **測っていないものは通す。** 測定は実DBの1時点であり、
        「測っていない」を「取得できない」に倒すと、測定の穴がそのまま
        候補の穴になる。落とすのは、測ったうえで0件だった列と、
        実DBに存在しないと分かった列だけである。
        """
        # 文書種別ユニットは到達可能性（ヘッダの有無）で決める。**ヘッダが無いものだけ**落とす
        reach = self.reach_of(unit)
        if reach is not None:
            return reach.is_acquirable
        # **文書種別ユニットは、測っていなければ通す。** 種別ユニットは本文の列（鍵が
        # (connection_id, db_qualified, physical_name)）を共有するので、列の行へ落とすと、
        # **未測定の種別が、別の種別の本文で決まる列全体の充填状況（0件）に引きずられて落ちる**
        # （実測で再現した）。列の行は表全体の状況で、種別を区別できない。
        if isinstance(unit.payload, dict) and unit.payload.get("doc_type_code"):
            return True
        fill = self.of(unit)
        return True if fill is None else fill.is_acquirable
