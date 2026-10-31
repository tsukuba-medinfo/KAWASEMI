"""ロギング設定（KAWASEMI 本体 kawasemi.core.logging_config の indexer 自前版）。

indexer は `kawasemi` パッケージに依存しないので、最小限を自前で持つ。
get_logger は標準 logging のラッパ。CLI 起動時に setup_logging を呼ぶ。
"""

from __future__ import annotations

import logging
import sys

from kawasemi_indexer.config import settings


def setup_logging() -> None:
    """ルートロガーをテキスト形式でセットアップする。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """ロガーを取得する（通常は __name__ を渡す）。"""
    return logging.getLogger(name)
