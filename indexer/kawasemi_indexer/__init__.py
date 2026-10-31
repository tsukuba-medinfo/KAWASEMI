"""kawasemi-indexer: KAWASEMI リソース索引のビルド/取り込みツール。

KAWASEMI 本体（WebUI）とは独立した、SE 向けのバックエンド前処理ツール。
異種の DB 定義書（Excel/CSV）と生きDBの information_schema を取り込み、
検索基盤の成果物（column_index.jsonl / embeddings.npy / *_index.jsonl /
build_info.json）を生成する。WebUI とは別ネットワークで動き、成果物ファイルの
授受で協働する（コードは共有せず、CONTRACT.md の契約に従う）。
"""

__version__ = "0.1.0"
