"""施設ごとの索引規則（`<SITE_DIR>/indexer_rules.yaml`）。

索引の生成で施設の表名・接続名に依る判断は、コードに持たずにこのファイルから読む。
コードに残るのは施設に依らない部分（概念語の正規表現、論理名の一般的な規則）だけである。

**ファイルが無ければ空の規則**（施設に依らない既定の振る舞い）になり、エラーにしない。
形式が不正なら ValueError にする（黙って空にすると患者キーや参照リンクが静かに変わる）。

形式（`site.example/indexer_rules.yaml` に架空の例がある）::

    # 参照リンク（ref_links.py）: 概念名 → 値域マスタの表。
    # 概念名はコードが持つ（byomei = 病名、exam = 検体検査項目）。書いた概念だけ下書きする。
    ref_link_masters:
      byomei: {connection_id: ehr, db_qualified: MASTER.DISEASE}
      exam:   {connection_id: dwh, db_qualified: MASTER.LAB_ITEM}

    # 患者キーの推定（builder.infer_patient_key）
    patient_key:
      # 既定の論理名（患者番号・患者ID など）に足す論理名。既定のどれも無いときだけ、書いた順に採る。
      additional_logical_names: [受診者番号]
      # 論理名 → その論理名を患者キーと認める connection_id の一覧（PK の列に限る）。
      # 「ID」のように曖昧な論理名を、命名の一貫性を確かめた接続にだけ認める。
      conditional_logical_names:
        ID: [claims_demo]
      # 論理名の完全一致でも患者キーを付けない表（db_qualified）。
      ambiguous_tables: [DEMO.PATIENT_LINK]
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)


class MasterRef(BaseModel):
    """値域マスタの表（参照リンクのリンク先）。"""

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    db_qualified: str


class PatientKeyRules(BaseModel):
    """患者キー推定の施設固有の部分。"""

    model_config = ConfigDict(extra="forbid")

    additional_logical_names: list[str] = Field(default_factory=list)
    conditional_logical_names: dict[str, list[str]] = Field(default_factory=dict)
    ambiguous_tables: list[str] = Field(default_factory=list)


class IndexerRules(BaseModel):
    """`indexer_rules.yaml` 全体。既定値（空）は施設に依らない振る舞いになる。"""

    model_config = ConfigDict(extra="forbid")

    ref_link_masters: dict[str, MasterRef] = Field(default_factory=dict)
    patient_key: PatientKeyRules = Field(default_factory=PatientKeyRules)


def load_indexer_rules(path: str | Path | None = None) -> IndexerRules:
    """規則を読む。`path` 省略時は設定の `indexer_rules_file`。無ければ空の規則。"""
    if path is None:
        from kawasemi_indexer.config import settings

        path = settings.indexer_rules_file
    p = Path(path)
    if not p.exists():
        logger.info(f"施設の索引規則が無い（施設に依らない既定で動く）: {p}")
        return IndexerRules()
    try:
        data = yaml.safe_load(p.read_text("utf-8")) or {}
        return IndexerRules.model_validate(data)
    except (yaml.YAMLError, ValidationError) as e:
        raise ValueError(f"施設の索引規則の形式が不正: {p}: {e}") from e
