"""確認済知識層（KnowledgeLayer）の YAML 永続。

生ソースから分離して永続することで、再構築時に再適用でき、人の確認コストを償却する
（docs/kawasemi_indexer_spec.md §3, §7）。1ファイル=1接続。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from kawasemi_indexer.ingestion.models import KnowledgeLayer
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)


def load_knowledge(path: str | Path) -> KnowledgeLayer:
    """知識層 YAML を読み込む。未配置はエラー（取り込みには最低限の宣言が要る）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"確認済知識層が見つかりません: {p}")
    data = yaml.safe_load(p.read_text("utf-8")) or {}
    return KnowledgeLayer.model_validate(data)


def save_knowledge(knowledge: KnowledgeLayer, path: str | Path) -> None:
    """知識層 YAML を書き出す（人確認後の確定・LLM下書きの保存）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = knowledge.model_dump(exclude_none=True)
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    logger.info(f"確認済知識層を書き出しました: {p}（connection={knowledge.connection_id}）")
