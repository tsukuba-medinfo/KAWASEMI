"""AIClinicalDrafter（規約文書を典拠とする臨床的意味の下書き）のユニットテスト

この機能の核は「LLM に典拠を捏造させない」ことなので、次の3点を主に固定する。

1. 検索対象が**規約文書（file_role=reference）に限られる**こと。DB カラムや患者データ
   ファイルを典拠に据えられない
2. 典拠の `unit_id` と出典表記が、選ばれたチャンクから**決定的に**組み立てられること。
   LLM の出力に文書名・ページを書かせる経路が無い
3. 典拠を選べなかった項目が「典拠なし」として区別され、**記述も採らない**こと。典拠の
   ない記述は LLM の事前学習知識と区別できないため
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import types
import unicodedata
import zlib
from pathlib import Path

import numpy as np
import pytest
from kawasemi_indexer.builder import build_index
from kawasemi_indexer.file_indexer import DEFAULT_CHUNK_CHARS

import kawasemi.ai_input.clinical_drafter as cd_mod
from kawasemi.ai_input.clinical_drafter import (
    _CHUNK_SIZE,
    _MAX_CAND_CHARS,
    _MIN_SCORE,
    _REQUIRED_DRAFT_KEYS,
    _TOP_K,
    DRAFT_SYSTEM_PROMPT,
    AIClinicalDrafter,
)
from kawasemi.ai_input.design_provenance import DesignLlmRecorder
from kawasemi.ai_input.llm_json import (
    DEFAULT_OPTIONS,
    LARGE_CONTEXT_OPTIONS,
    strict_response_format,
)
from kawasemi.ai_input.models import DesignRole
from kawasemi.ai_input.resource_index.derive import (
    build_citation_source,
    is_citable_reference_unit,
    is_index_of_contents_unit,
    is_reference_unit,
)
from kawasemi.ai_input.resource_index.embedder import l2_normalize
from kawasemi.ai_input.resource_index.schema import Kind, ResourceHit, ResourceUnit
from kawasemi.ai_input.resource_index.store import get_store, reset_store_cache
from kawasemi.ai_input.schemas import LLMClinicalDraft, LLMClinicalDrafts
from kawasemi.core.config import settings

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """test_resource_drafter.py と同じ疑似 embedding のトークン化（2-gram）。"""
    if not text:
        return []
    norm = unicodedata.normalize("NFKC", text).lower()
    tokens = list(_ASCII_TOKEN_RE.findall(norm))
    for run in re.findall(r"[^\x00-\x7f]+", norm):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class HashingEmbedder:
    """決定的な疑似 embedder（実 Ollama 不要）。"""

    def __init__(self, model: str = "bge-m3", dim: int = 64) -> None:
        self._model = model
        self._dim = dim

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        mat = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in _tokenize(text):
                mat[i, zlib.crc32(tok.encode("utf-8")) % self._dim] += 1.0
        return l2_normalize(mat)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_store_cache()
    yield
    reset_store_cache()


@pytest.fixture
def reference_index(tmp_path: Path) -> str:
    """規約文書（reference）と患者データファイル（patient_data）を含む索引を作る。

    典拠探索が reference だけを見ることを確かめたいので、**同じ索引に
    patient_data のファイルユニットも入れておく**（対象外であることが検証できる）。
    """
    docs = tmp_path / "manuals"
    docs.mkdir()
    (docs / "registry_manual.txt").write_text(
        "診断日は、組織学的または細胞学的に悪性と確定した日を記載する。"
        "臨床診断のみの場合は画像診断で悪性と判断した日を診断日とする。\n",
        encoding="utf-8",
    )
    # 「進行度」の項目でも足切り（_MIN_SCORE）を超えるだけの語彙を持たせる。
    # 2項目が同じ LLM 呼び出しに載る状況（cand_id の取り違え）を作るために必要。
    (docs / "staging_manual.txt").write_text(
        "進行度は治療開始前の臨床所見で記載する。"
        "進行度に手術所見は用いない。進行度は治療前の進行度である。\n",
        encoding="utf-8",
    )

    manifest = tmp_path / "file_manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "connection_id": "files",
                "file_role": "patient_data",
                "path_pattern": "{patient_id}/pathology.txt",
                "label": "病理レポート（診断日の記載あり）",
                "extract": "text",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    out = tmp_path / "resource_index"
    build_index(
        out_dir=out,
        embedder=HashingEmbedder(),
        file_source=docs,
        file_manifest=manifest,
    )
    return str(out)


def _minimal_pdf_bytes(pages: list[str]) -> bytes:
    """pypdf が読める最小の PDF を組み立てる（Helvetica / ASCII 本文）。

    求められているのは「規約文書 **PDF** を `--file-source` で索引に投入し、臨床的意味の
    典拠候補として検索できる」ことである。既存フィクスチャはすべて `.txt` 由来で
    payload の `page` が常に None になり、`build_citation_source` の `p.{page}` 分岐と
    unit_id の `#p{page}` 形式は**手作りの ResourceUnit でしか通っていなかった**。
    `char_range` で起きていた誤り（テストが索引の作らない文字列で書かれて
    いたため検出できていなかった）と同型なので、実物の PDF で通す。

    PDF 生成ライブラリを新たに足さないため、ここで最小の PDF を直接書く。既存の
    `kawasemi.ai_input.e2e.handwriting.make_handwriting_pdf` は**テキスト層を持たない**
    画像PDF（Vision OCR 縮退の検証用）を作るもので、`_extract_pages` に掛けると本文が
    空になり reference チャンクが1つも生まれないため、ここでは使えない。

    本文は ASCII のみ（日本語を出すには埋め込みフォントが要るが、ここで確かめたいのは
    ページ番号の伝播であって文字コードではない）。
    """
    font_ref = 3
    content_nums = [4 + 2 * i for i in range(len(pages))]
    page_nums = [5 + 2 * i for i in range(len(pages))]
    kids = b" ".join(b"%d 0 R" % n for n in page_nums)
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages)),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for text, cnum in zip(pages, content_nums):
        escaped = text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        objs.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font_ref, cnum)
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref_at,
    )
    return bytes(out)


@pytest.fixture
def pdf_reference_index(tmp_path: Path) -> str:
    """規約文書 **PDF** を `--file-source` に置いて索引を作る。

    テキストファイルも1本置いて、ページのある文書とない文書で unit_id / 出典表記の
    形式が分かれることを同じ索引の中で対比できるようにする。
    """
    docs = tmp_path / "manuals"
    docs.mkdir()
    (docs / "registry_manual.pdf").write_bytes(
        # 1ページ目は表紙。**2ページ目**に本文を置き、page が固定値 1 ではなく実際の
        # ページ番号として伝播していることを確かめられるようにする。
        _minimal_pdf_bytes(["cover page", "diagnosis date is the date of pathology. " * 8])
    )
    (docs / "notes.txt").write_text("zzz unrelated note\n", encoding="utf-8")

    out = tmp_path / "resource_index"
    build_index(out_dir=out, embedder=HashingEmbedder(), file_source=docs)
    return str(out)


@pytest.fixture
def multi_candidate_index(tmp_path: Path) -> str:
    """**1項目あたり `_TOP_K` 件**の典拠候補が出る索引を作る。

    `reference_index` は各項目につき `_MIN_SCORE` を超えるチャンクが1つしか無い。
    候補が1件だと「他項目の cand_id が**同じ項目の別候補**として解決される」という
    失敗が原理的に起こせず、`clinical_drafter` の cand_id 照合を位置索引
    （`hits[chosen_id]`）へ戻しても全件 green のままになる。足切りに助けられて通る
    構図で、`competing_index` で塞いだのと同じ穴である。

    ここでは規約文書1本を `_TOP_K` 個以上のチャンクへ分かれる長さにし、どのチャンクも
    項目名と語彙を共有して足切りを超えるようにする（語彙を繰り返して cosine を上げる
    のは `staging_manual.txt` と同じ手）。2項目とも別ファイルにして、項目ごとの候補が
    互いのファイルへ混ざらないようにしてある。
    """
    docs = tmp_path / "manuals"
    docs.mkdir()
    # 1回ぶんで約80文字。30回繰り返して約2,400文字 = 800文字チャンク（重なり100）で
    # 4チャンクになり、上位 _TOP_K 件がすべて同一項目の候補として並ぶ。
    dx = (
        "診断日は、組織学的または細胞学的に悪性と確定した日を診断日として記載する。"
        "診断日の確定は病理診断報告書の日付による。診断日が不明な場合も診断日欄は空にしない。"
    )
    stage = (
        "進行度は治療開始前の臨床的進行度を記載する。"
        "進行度に手術所見は用いない。進行度は治療前の進行度である。進行度の判定は進行度分類による。"
    )
    (docs / "dx_manual.txt").write_text(dx * 30 + "\n", encoding="utf-8")
    (docs / "stage_manual.txt").write_text(stage * 30 + "\n", encoding="utf-8")

    out = tmp_path / "resource_index"
    build_index(out_dir=out, embedder=HashingEmbedder(), file_source=docs)
    return str(out)


@pytest.fixture
def competing_index(tmp_path: Path) -> str:
    """患者データファイルが**規約文書より高スコアで並ぶ**索引を作る。

    `reference_index` の患者データユニットは足切り（`_MIN_SCORE`）に届かないため、
    典拠探索から `predicate=is_reference_unit` を外しても reference しか返らない。
    それでは「規約文書に限る」という防御が無検証のまま残る。

    ここでは患者データの label を項目名と**同一**にして cosine を最大化する。
    述語が効いていなければ、患者データが cand_id=0 で典拠候補に並ぶ。
    """
    docs = tmp_path / "manuals"
    docs.mkdir()
    (docs / "registry_manual.txt").write_text(
        "診断日は、組織学的または細胞学的に悪性と確定した日を記載する。"
        "臨床診断のみの場合は画像診断で悪性と判断した日を診断日とする。\n",
        encoding="utf-8",
    )

    manifest = tmp_path / "file_manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "connection_id": "files",
                "file_role": "patient_data",
                "path_pattern": "{patient_id}/pathology.txt",
                # 項目名と同一にして、述語が無ければ必ず1位に来るようにする
                "label": "診断日",
                "extract": "text",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    out = tmp_path / "resource_index"
    build_index(
        out_dir=out,
        embedder=HashingEmbedder(),
        file_source=docs,
        file_manifest=manifest,
    )
    return str(out)


@pytest.fixture
def buried_reference_index(tmp_path: Path) -> str:
    """規約文書が、スコア上位 `top_k` の**外**に沈む索引を作る。

    `store.search` は predicate を渡されたときだけ部分ソート（`argpartition`）をやめて
    全件を降順で見る。理由は「上位 k だけ取ると対象種別が k 件に届かない」ことだが、
    ユニット数が `top_k` 以下の索引では `argpartition` に戻しても同じ結果になり、
    分岐の存在理由が検証されない。

    ここでは項目名と同一ラベルの患者データユニットを `top_k` 件より多く置き、
    規約文書がそれらより下に来るようにする。
    """
    docs = tmp_path / "manuals"
    docs.mkdir()
    (docs / "registry_manual.txt").write_text(
        "診断日は、組織学的または細胞学的に悪性と確定した日を記載する。"
        "臨床診断のみの場合は画像診断で悪性と判断した日を診断日とする。\n",
        encoding="utf-8",
    )

    manifest = tmp_path / "file_manifest.jsonl"
    manifest.write_text(
        "\n".join(
            json.dumps(
                {
                    "connection_id": "files",
                    "file_role": "patient_data",
                    "path_pattern": f"{{patient_id}}/report{i}.txt",
                    "label": f"診断日{'　' * i}",  # 項目名と同語彙・高スコア
                    "extract": "text",
                },
                ensure_ascii=False,
            )
            for i in range(5)
        )
        + "\n",
        encoding="utf-8",
    )

    out = tmp_path / "resource_index"
    build_index(
        out_dir=out,
        embedder=HashingEmbedder(),
        file_source=docs,
        file_manifest=manifest,
    )
    return str(out)


def _patch_chat(drafter: AIClinicalDrafter, payload: dict, calls: list) -> None:
    async def fake_chat(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(payload)))

    drafter._client = types.SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]


def _patch_chat_dynamic(drafter: AIClinicalDrafter, responder, calls: list) -> None:
    """プロンプトを読んで応答を組み立てるフェイク（cand_id の実値に依存する検証用）。"""

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        payload = responder(kwargs["messages"][1]["content"])
        return types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(payload)))

    drafter._client = types.SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]


def _cand_ids_by_link(user_msg: str) -> dict[str, list[int]]:
    """プロンプトから linkId ごとの cand_id を読み取る。"""
    out: dict[str, list[int]] = {}
    current: str | None = None
    for line in user_msg.splitlines():
        head = re.match(r"### linkId: (.+)$", line.strip())
        if head:
            current = head.group(1)
            out[current] = []
            continue
        cand = re.search(r"cand_id=(\d+)", line)
        if cand and current is not None:
            out[current].append(int(cand.group(1)))
    return out


def _cand_bodies(user_msg: str) -> dict[int, str]:
    """プロンプトから cand_id ごとの候補本文を読み取る。

    「その番号でどのチャンクを見せたか」を知るために使う。典拠の解決が正しいかは
    「選ばれた番号で提示した本文を持つチャンクへ解決されたか」で判定できる。
    """
    out: dict[int, str] = {}
    for line in user_msg.splitlines():
        m = re.match(r"\s*- cand_id=(\d+): (.*)$", line)
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


@pytest.fixture
def index_of_contents_index(tmp_path: Path) -> str:
    """**目次チャンクが本文の定義より高スコアで並ぶ**索引を作る。

    実際の規約文書では、少なからぬ項目で目次チャンクが1位になり、上位3件がすべて目次になる
    項目もある。目次は項目名の羅列なので、項目テキストとの字面の一致度が本文の定義文より
    高い。**この競合が成立しないフィクスチャでは、述語を外しても症状が出ない。**
    """
    docs = tmp_path / "manuals"
    docs.mkdir()
    # **競合が成立する形にする。** 疑似 embedder は 2-gram の出現数で測るので、リーダの文字を
    # 長く並べるとそちらが支配的になり、実際の文書とは逆に本文が上位へ来る（最初に書いた
    # フィクスチャがそうなり、前提の assert が落ちて気づいた）。項目名の反復で目次を上位にする。
    # **両方が `_MIN_SCORE=0.5` を超えていること**も必要（超えない側は足切りで消えるので、
    # 述語ではなく足切りが結果を作ってしまう）。
    (docs / "toc.txt").write_text("診断日 診断日 診断日 診断日 ···· 31 32 33\n", encoding="utf-8")
    (docs / "body.txt").write_text("診断日 診断日は悪性と確定した日である。\n", encoding="utf-8")
    out = tmp_path / "resource_index"
    build_index(out_dir=out, embedder=HashingEmbedder(), file_source=docs)
    return str(out)


def _unit_id_by_body(index_dir: str) -> dict[str, str]:
    """索引の規約文書ユニットを「プロンプトへ載る本文 → unit_id」で引けるようにする。

    `_build_user_message` と同じ整形（strip / 改行をスペースへ）を掛ける。
    """
    return {
        (u.search_text or u.snippet or "").strip().replace("\n", " "): u.unit_id
        for u in get_store(index_dir).units
        if is_reference_unit(u)
    }


def _patch_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    import kawasemi.ai_input.clinical_drafter as cd_mod

    monkeypatch.setattr(cd_mod, "OllamaEmbedder", lambda **kwargs: HashingEmbedder())


# --------------------------------------------------------------------------- #
# derive（典拠の決定的導出）
# --------------------------------------------------------------------------- #


def test_is_reference_unit_distinguishes_file_roles() -> None:
    """規約文書と患者データファイルを述語で区別すること。"""
    ref = ResourceUnit(
        unit_id="file:manual.pdf#p3:0-800",
        kind=Kind.FILE,
        label="登録マニュアル",
        search_text="x",
        payload={"file_role": "reference", "path": "manual.pdf", "page": 3},
    )
    patient = ResourceUnit(
        unit_id="file:report",
        kind=Kind.FILE,
        label="病理レポート",
        search_text="x",
        payload={"file_role": "patient_data", "path_pattern": "{patient_id}/x.txt"},
    )
    db = ResourceUnit(
        unit_id="db:T.C",
        kind=Kind.DB,
        label="カラム",
        search_text="x",
        payload={"connection_id": "d"},
    )
    assert is_reference_unit(ref)
    assert not is_reference_unit(patient)
    assert not is_reference_unit(db)


def _reference_unit(text: str, *, page: int = 3) -> ResourceUnit:
    """規約文書のユニット（本文を差し替えて述語を測るため）。"""
    return ResourceUnit(
        unit_id=f"file:manual.pdf#p{page}:0-800",
        kind=Kind.FILE,
        label=f"manual.pdf（p{page}）",
        search_text=text,
        payload={"file_role": "reference", "path": "manual.pdf", "page": page},
    )


def test_is_index_of_contents_unit_reads_the_leader_not_the_page() -> None:
    """目次の判定が**リーダ**で行われ、ページ番号に依らないこと。

    **ページ番号で落とす実装を通さないことが要点**である。目次が p.3〜5 にしかない
    フィクスチャだと `page in (3, 4, 5)` という実装が全 assert を通る
    （収録文書が増えて目次が後ろのページへ来た瞬間に、除外が黙って止まる）。**遅いページの
    目次**と**早いページの本文**を両方置く。
    """
    toc = _reference_unit("[5] 発症日 ······················· 22  [6] 診断名 ········· 23")
    late_toc = _reference_unit("[付録 3] 架空の記号一覧 ················· 139", page=140)
    body = _reference_unit(
        "〈記入欄〉 番号／欄の名前／言い換え／必須か  [5] 発症日 発症の日付 必須 感染症の発症日。",
        page=28,
    )
    early_body = _reference_unit("本書の使い方。項目ごとに定義と選択肢を示す。", page=3)
    preface = _reference_unit("本書は登録様式の記入の手引きである。", page=6)

    assert is_index_of_contents_unit(toc)
    assert is_index_of_contents_unit(late_toc), "後ろのページの目次も落とす（ページに依らない）"
    assert not is_index_of_contents_unit(body)
    assert not is_index_of_contents_unit(early_body), "目次のページ範囲の本文を落としてはいけない"
    assert not is_index_of_contents_unit(preface), "目次でない前付けを落としてはいけない"


def test_is_index_of_contents_unit_reads_any_leader_character() -> None:
    """リーダの**文字種**を1つに決め打ちしないこと。

    目次のリーダは文書ごとに中黒（`·`）だったり **ASCII ピリオド**だったりする。1種類に
    決め打ちすると、別の文字で組まれた目次が本文として候補に残り、上位を占めてしまう。
    """
    for leader in ("···", "....", "．．．．", "・・・・", "……", "‥‥"):
        unit = _reference_unit(f"[付録 1] 微生物コード表 {leader * 4} 12", page=5)
        assert is_index_of_contents_unit(unit), f"リーダ {leader!r} の目次が落ちない"


def test_is_index_of_contents_unit_keeps_short_ellipses_in_the_body() -> None:
    """本文の**省略記号**を目次と誤判定しないこと。

    原典は本文で `・・・`（3文字）や `…`（1文字）を省略として使う。3文字以下まで目印にすると
    **本文の定義ページが典拠候補から落ちる**（分子が減る方向の事故で、症状が出にくい）。
    """
    for ellipsis in ("・・・", "…", "‥", "..."):
        unit = _reference_unit(f"次の場合は{ellipsis}に準じて判断する。 [項目区分] 必須", page=17)
        assert not is_index_of_contents_unit(unit), f"省略記号 {ellipsis!r} を目次と誤判定した"


def test_the_location_search_still_sees_the_index_of_contents(
    index_of_contents_index: str,
) -> None:
    """**所在探索（述語なし）は目次を落とさない**こと（過剰適用の検査）。

    目次の除外は**典拠探索という用途**の判断である。`store.search` の内部や
    `is_reference_unit` に持たせると、所在候補（所在の確からしさの判定の入力）まで変わる。
    「述語に関係なく常に目次を落とす」ミューテーションは、目次除外のテストの
    **前提 assert に依存して**しか殺されていなかったので、独立に固定する。
    """
    store = get_store(index_of_contents_index)
    query = HashingEmbedder().embed(["診断日"])[0]
    hits = store.search(query, top_k=5, min_score=0.0)
    assert hits, "候補が0件では過剰適用を測れない"
    assert any("toc.txt" in h.unit.payload.get("path", "") for h in hits), (
        "述語を渡さない検索から目次が消えている（典拠探索の判断が所在探索へ漏れている）"
    )


def test_is_citable_reference_unit_excludes_only_the_index_of_contents() -> None:
    """典拠候補の述語が「規約文書であること」と「目次でないこと」の両方を要求すること。

    **`is_reference_unit` は分類、こちらは用途**である。片方だけを見る実装だと、
    目次を候補に出す（分類だけ見る）か、患者データを候補に出す（目次判定だけ見る）。
    """
    body = _reference_unit("[5] 発症日 発症の日付 必須 感染症の発症日。", page=28)
    toc = _reference_unit("[5] 発症日 ······················· 22")
    patient_toc = ResourceUnit(
        unit_id="file:report",
        kind=Kind.FILE,
        label="病理レポート",
        search_text="所見 ······················· 1",
        payload={"file_role": "patient_data", "path_pattern": "{patient_id}/x.txt"},
    )
    patient_body = ResourceUnit(
        unit_id="file:report2",
        kind=Kind.FILE,
        label="病理レポート",
        search_text="組織型は腺癌である。",
        payload={"file_role": "patient_data", "path_pattern": "{patient_id}/y.txt"},
    )

    assert is_citable_reference_unit(body)
    assert not is_citable_reference_unit(toc)
    # **患者データは目次かどうかに関係なく候補にしない**（規約文書に限る防御を弱めていないこと）
    assert not is_citable_reference_unit(patient_toc)
    assert not is_citable_reference_unit(patient_body)
    # 分類の述語そのものは変えていない（目次も「規約文書のユニット」ではある）
    assert is_reference_unit(toc)


def test_build_citation_source_is_derived_from_payload() -> None:
    """出典表記が payload（パス・ページ・文字範囲）から決定的に決まること。

    `char_range` は索引が **`[start, end]` のリスト**で持つ（indexer の
    `file_indexer`）。文字列で書いたフィクスチャで固定すると、実データでだけ
    `（[800, 1600]）` と表示される誤りを取り逃がすので、実物の型で固定する。
    """
    unit = ResourceUnit(
        unit_id="file:manual.pdf#p3:800-1600",
        kind=Kind.FILE,
        label="登録マニュアル",
        search_text="x",
        payload={
            "file_role": "reference",
            "path": "manual.pdf",
            "page": 3,
            "char_range": [800, 1600],
        },
    )
    source = build_citation_source(unit)
    assert "manual.pdf" in source
    assert "p.3" in source
    assert "800-1600" in source
    assert "[" not in source  # Python のリスト表記をそのまま出さない


def test_build_citation_source_uses_real_index_payload(reference_index: str) -> None:
    """索引が実際に作る payload で出典表記が壊れないこと（型の思い込みを排す）。"""
    store = get_store(reference_index)
    unit = next(u for u in store.units if is_reference_unit(u))
    assert isinstance(unit.payload["char_range"], list)  # 実物はリスト
    source = build_citation_source(unit)
    assert source.startswith("規約文書:")
    assert "[" not in source


def test_pdf_reference_is_indexed_with_page_and_is_searchable(pdf_reference_index: str) -> None:
    """規約文書 PDF を索引へ投入し、ページつきの典拠候補として引けること。

    `page` の入った payload を作れるのは索引の PDF 経路だけである。`.txt` だけの
    フィクスチャでは `page` が常に None になり、`build_citation_source` の `p.{page}`
    分岐と unit_id の `#p{page}` 形式が実データで一度も通らない。手作りの
    `ResourceUnit` で書いたテストは、索引が実際に作る形と食い違っていても通る。
    """
    store = get_store(pdf_reference_index)
    refs = [u for u in store.units if is_reference_unit(u)]

    pdf_units = [u for u in refs if u.payload.get("path") == "registry_manual.pdf"]
    assert pdf_units, [u.unit_id for u in refs]
    body = next(u for u in pdf_units if "diagnosis" in (u.search_text or ""))

    # 索引が作る実 payload（page は表紙ではなく本文のあるページ番号）
    assert body.payload["page"] == 2
    assert body.payload["mime"] == "application/pdf"
    start, end = body.payload["char_range"]
    # unit_id の `file:{path}#p{page}:{start}-{end}` 形式
    assert body.unit_id == f"file:registry_manual.pdf#p2:{start}-{end}"
    # 出典表記の `p.{page}` 分岐
    assert build_citation_source(body) == f"規約文書: registry_manual.pdf p.2（{start}-{end}）"

    # ページを持たない文書は `#p` を付けず、出典にも p. を出さない（形式の対比）
    txt = next(u for u in refs if u.payload.get("path") == "notes.txt")
    assert txt.payload["page"] is None
    assert "#p" not in txt.unit_id
    assert "p." not in build_citation_source(txt)

    # 典拠候補として検索できる（足切りを超え、reference として返る）
    hits = store.search(
        HashingEmbedder().embed(["diagnosis date"])[0],
        top_k=_TOP_K,
        min_score=_MIN_SCORE,
        predicate=is_reference_unit,
    )
    assert hits, "PDF の規約文書が典拠候補として引けていない"
    assert hits[0].unit.unit_id == body.unit_id


# --------------------------------------------------------------------------- #
# 検索対象（規約文書に限る）
# --------------------------------------------------------------------------- #


def test_search_is_restricted_to_reference_documents(reference_index: str) -> None:
    """典拠探索が reference 文書だけを返すこと（patient_data / DB を掴まない）。

    同じ索引に patient_data のユニットが入っているので、述語が効いていなければ
    「病理レポート」が高スコアで返る。
    """
    store = get_store(reference_index)
    qv = HashingEmbedder().embed(["診断日"])[0]

    unrestricted = store.search(qv, top_k=10, min_score=0.0)
    restricted = store.search(qv, top_k=10, min_score=0.0, predicate=is_reference_unit)

    # 述語なしでは patient_data も返る（＝この索引で区別に意味がある）
    assert any(h.unit.payload.get("file_role") == "patient_data" for h in unrestricted)
    # 述語ありでは reference だけ
    assert restricted
    assert all(is_reference_unit(h.unit) for h in restricted)
    # cand_id は提示順の連番（LLM へ渡す番号が候補一覧と対応する）
    assert [h.cand_id for h in restricted] == list(range(len(restricted)))


# --------------------------------------------------------------------------- #
# 下書き生成
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_draft_passes_the_large_num_ctx(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """臨床的意味の下書きは**大きい方の num_ctx**（`LARGE_CONTEXT_OPTIONS`）で呼ぶ。

    ここは「値が何であるか」ではなく**共有の既定ではなく大きい方を渡している**ことを見る
    （予算そのものは `test_candidate_budget_fits_num_ctx` が測る）。既定（8192）へ戻ると、
    `think=True` で必ず出る thinking が予算を食い切り、`message.content` が
    空になる——複数のチャンクがまとめて落ちる。**症状は
    「一部の項目だけ典拠なしが増える」形で出る**ので、結線をここで固定する。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []
    _patch_chat(
        drafter,
        {"drafts": [{"link_id": "item-dx-date", "chosen_cand_id": 0, "annotation": "定義。"}]},
        calls,
    )

    await drafter.draft([{"link_id": "item-dx-date", "item_text": "診断日"}])

    assert calls[0]["options"]["num_ctx"] == LARGE_CONTEXT_OPTIONS["num_ctx"]
    assert calls[0]["options"]["num_ctx"] > int(DEFAULT_OPTIONS["num_ctx"])


@pytest.mark.asyncio
async def test_draft_builds_citation_deterministically(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM は cand_id と記述だけを返し、典拠は索引から決定的に組み立てられること。"""
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []
    _patch_chat(
        drafter,
        {
            "drafts": [
                {
                    "link_id": "item-dx-date",
                    "chosen_cand_id": 0,
                    "annotation": "組織学的または細胞学的に悪性と確定した日を記載する。",
                }
            ]
        },
        calls,
    )

    result = await drafter.draft(
        [{"link_id": "item-dx-date", "item_text": "診断日"}],
    )

    assert result.index_searchable is True
    assert result.no_hit_count == 0
    assert result.llm_model  # 実際に呼んだモデル名が返る
    assert len(result.items) == 1
    d = result.items[0]
    assert d.no_citation is False
    assert d.annotation == "組織学的または細胞学的に悪性と確定した日を記載する。"
    assert d.citation is not None
    # 典拠は索引のアドレス。LLM の出力には unit_id も文書名も無い
    assert d.citation.unit_id.startswith("file:")
    assert is_reference_unit(
        ResourceUnit(
            unit_id=d.citation.unit_id,
            kind=Kind.FILE,
            label="x",
            search_text="x",
            payload=d.citation.payload or {},
        )
    )
    assert d.citation.recommended is True
    assert d.citation.source.startswith("規約文書:")

    # LLM へ渡した内容の確認: システムプロンプトが典拠を書かせない指示を含み、
    # ユーザメッセージは cand_id と抜粋だけを提示する（文書名・ページを渡さない）
    system_msg = calls[0]["messages"][0]["content"]
    user_msg = calls[0]["messages"][1]["content"]
    assert "典拠の文書名・ページは書かないこと" in system_msg
    assert "cand_id=" in user_msg
    assert "registry_manual" not in user_msg  # 出典はプロンプトに渡さない
    assert "p." not in user_msg


@pytest.mark.asyncio
async def test_draft_marks_no_citation_when_llm_declines(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM が該当なしと判断した項目は「典拠なし」になり、記述も採らないこと。

    典拠のない記述は、規約文書に基づくのか事前学習知識なのか区別できない。
    「規約文書を引いても意味が決まらなかった項目」を数えられる状態を保つ。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    _patch_chat(
        drafter,
        {
            "drafts": [
                {
                    "link_id": "item-x",
                    "chosen_cand_id": None,
                    # 典拠なしなのに記述を書いてきた場合（指示違反）も捨てる
                    "annotation": "一般的には診断日は初診日を用いる。",
                }
            ]
        },
        [],
    )

    drafts = (await drafter.draft([{"link_id": "item-x", "item_text": "診断日"}])).items

    assert len(drafts) == 1
    assert drafts[0].no_citation is True
    assert drafts[0].citation is None
    assert drafts[0].annotation is None  # 典拠なしの記述は採らない
    # 候補自体は返す（人が選んで書けるようにする）
    assert drafts[0].candidates


@pytest.mark.asyncio
async def test_draft_rejects_citation_without_annotation(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cand_id だけ返して記述が空なら「典拠なし」にすること。

    片方だけ採ると「典拠つき」として数えられ、規約文書を引いても意味が
    決まらなかった項目の集計がずれる。画面も「典拠つきN件」と出すのに記述欄は空になる。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    _patch_chat(
        drafter,
        {"drafts": [{"link_id": "item-x", "chosen_cand_id": 0, "annotation": "   "}]},
        [],
    )

    result = await drafter.draft([{"link_id": "item-x", "item_text": "診断日"}])

    d = result.items[0]
    assert d.no_citation is True
    assert d.citation is None
    assert d.annotation is None
    # 候補一覧には残すが、「一推し」の印は付けない
    assert d.candidates
    assert not any(c.recommended for c in d.candidates)


def test_llm_has_no_field_for_writing_a_citation() -> None:
    """LLM の応答スキーマに、典拠を自由記述する口が無いこと。

    「典拠を捏造させない」の一番強い形の担保。`source` や `path` のようなフィールドを
    足せば、LLM が書いた文字列がそのまま典拠として通る経路ができる。
    """
    assert set(LLMClinicalDraft.model_fields) == {"link_id", "chosen_cand_id", "annotation"}


@pytest.mark.asyncio
async def test_draft_ignores_out_of_range_cand_id(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存在しない cand_id を返されたら典拠なしとして扱うこと（捏造を受理しない）。"""
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    _patch_chat(
        drafter,
        {"drafts": [{"link_id": "item-x", "chosen_cand_id": 999, "annotation": "でっちあげ"}]},
        [],
    )

    drafts = (await drafter.draft([{"link_id": "item-x", "item_text": "診断日"}])).items

    assert drafts[0].no_citation is True
    assert drafts[0].citation is None
    assert drafts[0].annotation is None


@pytest.mark.asyncio
async def test_draft_returns_nothing_when_index_not_searchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引が検索不可なら下書きせず、**そのことを区別できる形で**返すこと。

    所在側（AIResourceDrafter）は DB定義書 BM25 へフォールバックするが、典拠は索引の
    アドレスが無ければ成立しない。ここでフォールバックすると「典拠のない記述」が
    できてしまう。

    件数だけを返すと「索引が引けなかった」と「規約文書に該当が無かった」が
    区別できず、画面が緑の成功表示で自己矛盾した集計を出す。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=str(tmp_path / "missing"))
    called: list = []
    _patch_chat(drafter, {"drafts": []}, called)

    result = await drafter.draft([{"link_id": "item-x", "item_text": "診断日"}])

    assert result.items == []
    assert result.no_hit_count == 1
    assert result.index_searchable is False  # 探索そのものを実行していない
    assert result.llm_model is None  # 呼んでいないモデル名を名乗らない
    assert called == []  # LLM を呼ばない


class _DigestEmbedder(HashingEmbedder):
    """クエリ側のダイジェストを名乗る embedder。"""

    def __init__(self, digest: str | None) -> None:
        super().__init__()
        self._digest = digest

    def digest(self) -> str | None:
        return self._digest


@pytest.mark.asyncio
async def test_draft_returns_nothing_when_the_embedding_weights_differ(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引とクエリで embedding の重みが違うなら、**索引が検索不可のときと同じ扱い**。

    名前（`bge-m3`）が同じでも、`EMBEDDING_BASE_URL` で別の重みのサーバへ向けられる。
    検索すると再現率が黙って落ちる。LLM も呼ばない。
    """
    monkeypatch.setattr(cd_mod, "OllamaEmbedder", lambda **kw: _DigestEmbedder("bb" * 32))
    store = get_store(reference_index)
    assert store.build_info is not None
    store.build_info.embedding_digest = "aa" * 32
    drafter = AIClinicalDrafter(index_dir=reference_index)
    called: list = []
    _patch_chat(drafter, {"drafts": []}, called)

    result = await drafter.draft([{"link_id": "item-x", "item_text": "診断日"}])

    assert result.index_searchable is False
    assert result.items == [] and result.no_hit_count == 1
    assert result.llm_model is None
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index_digest", "query_digest"),
    [("aa" * 32, "aa" * 32), (None, "bb" * 32), ("aa" * 32, None)],
    ids=["same", "index-unverified", "query-unverified"],
)
async def test_draft_proceeds_when_the_weights_match_or_cannot_be_compared(
    reference_index: str,
    monkeypatch: pytest.MonkeyPatch,
    index_digest: str | None,
    query_digest: str | None,
) -> None:
    """一致、または比べられないときは従来どおり進む（**肯定側も測る**。常に止める実装を落とす）。"""
    monkeypatch.setattr(cd_mod, "OllamaEmbedder", lambda **kw: _DigestEmbedder(query_digest))
    store = get_store(reference_index)
    assert store.build_info is not None
    store.build_info.embedding_digest = index_digest
    drafter = AIClinicalDrafter(index_dir=reference_index)
    called: list = []
    _patch_chat(drafter, {"drafts": []}, called)

    result = await drafter.draft([{"link_id": "item-x", "item_text": "診断日"}])

    assert result.index_searchable is True


@pytest.mark.asyncio
async def test_draft_keeps_partial_drafts_when_the_deadline_hits(
    reference_index: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """全体デッドラインで打ち切られても、**完了済みの下書きを捨てない**。

    項目数が多いと1コール数十秒のコールが数十回になるので、既定の 600 秒には収まらない。捨てると
    作業の最初の一歩（全項目の臨床的意味を下書きする）が何度やっても完走しない。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return types.SimpleNamespace(
                message=types.SimpleNamespace(
                    content=json.dumps(
                        {
                            "drafts": [
                                {
                                    "link_id": "item-dx-date",
                                    "chosen_cand_id": 0,
                                    "annotation": "悪性と確定した日を記載する。",
                                }
                            ]
                        }
                    )
                )
            )
        await asyncio.sleep(5)  # 2コール目でデッドラインを超えさせる
        return types.SimpleNamespace(message=types.SimpleNamespace(content="{}"))

    drafter._client = types.SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]
    monkeypatch.setattr(cd_mod, "_CHUNK_SIZE", 1)
    monkeypatch.setattr(settings, "llm_total_timeout", 1)

    with caplog.at_level(logging.INFO, logger="kawasemi.ai_input.clinical_drafter"):
        result = await drafter.draft(
            [
                {"link_id": "item-dx-date", "item_text": "診断日"},
                {"link_id": "item-stage", "item_text": "進行度"},
            ]
        )

    # **完了ログが未試行を「典拠なし」に混ぜないこと**。route の応答は assert で
    # 守られているのに drafter のログが守られていない、という非対称を塞ぐ。
    # ここを戻すと、同じ実行のログに「典拠なし1件」と「典拠なし0件」が並ぶ。
    done = [m for m in (r.getMessage() for r in caplog.records) if "AI下書き完了" in m]
    assert done, "完了ログが出ていること"
    assert "典拠なし0件" in done[-1], done[-1]
    assert "未試行1件" in done[-1], done[-1]

    assert result.index_searchable is True
    by_link = {d.link_id: d for d in result.items}
    # 1コール目の結果は残る
    assert by_link["item-dx-date"].citation is not None
    assert by_link["item-dx-date"].annotation == "悪性と確定した日を記載する。"
    # **打ち切られた項目は「典拠なし」ではなく「試していない」**として区別される
    assert result.unattempted_link_ids == ("item-stage",)
    assert by_link["item-stage"].citation is None


@pytest.mark.asyncio
async def test_draft_marks_items_the_llm_omitted_as_unattempted(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM が応答に含めなかった項目も「試していない」に入ること。

    プロンプトは「必ず全ての項目を drafts 配列に含めること」と指示しているが**保証はない**。
    落ちた項目を「規約文書に定義が無い」と数えると、意味が決まらなかった項目の件数が水増しになる。**打ち切りと
    応答漏れを同じ扱いにする**ので、この検査は順序に依存しない。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    _patch_chat(
        drafter,
        {
            "drafts": [
                {
                    "link_id": "item-dx-date",
                    "chosen_cand_id": 0,
                    "annotation": "悪性と確定した日を記載する。",
                }
            ]
        },
        [],
    )

    result = await drafter.draft(
        [
            {"link_id": "item-dx-date", "item_text": "診断日"},
            {"link_id": "item-stage", "item_text": "進行度"},
        ]
    )
    assert result.unattempted_link_ids == ("item-stage",)


@pytest.mark.asyncio
async def test_draft_never_offers_index_of_contents_chunks(
    index_of_contents_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目次チャンクが典拠候補に出ないこと（結線）。

    **前提を assert する**: 述語を「規約文書か」だけにすると目次が1位に来る索引である
    （そうでなければ、この検査は述語を外しても落ちない）。
    """
    _patch_embedder(monkeypatch)
    store = get_store(index_of_contents_index)
    query = HashingEmbedder().embed(["診断日"])[0]

    # 前提: 分類の述語だけなら目次が1位（競合が成立している）
    loose = store.search(query, top_k=5, min_score=0.0, predicate=is_reference_unit)
    assert loose, "候補が0件では競合が成立しない"
    assert "toc.txt" in loose[0].unit.payload["path"], (
        f"目次が1位でないフィクスチャでは目次除外の検査にならない: {loose[0].unit.unit_id}"
    )
    # **目次が足切りを超えていること**（超えていなければ、述語ではなく `_MIN_SCORE` が
    # 目次を消すので、この検査は述語を外しても落ちない）
    assert loose[0].score >= _MIN_SCORE, (
        f"目次が足切りで消えるフィクスチャでは述語を測れない: {loose[0].score}"
    )

    # 用途の述語では目次が1件も出ない
    strict = store.search(query, top_k=5, min_score=0.0, predicate=is_citable_reference_unit)
    assert strict, "本文が候補に残ること"
    assert all("toc.txt" not in h.unit.payload["path"] for h in strict), strict

    # 下書きの経路（`draft`）が用途の述語を使っていること
    drafter = AIClinicalDrafter(index_dir=index_of_contents_index)
    _patch_chat(
        drafter,
        {
            "drafts": [
                {
                    "link_id": "item-dx-date",
                    "chosen_cand_id": 0,
                    "annotation": "悪性と確定した日を記載する。",
                }
            ]
        },
        [],
    )

    result = await drafter.draft([{"link_id": "item-dx-date", "item_text": "診断日"}])
    assert result.items[0].candidates, "候補が空では結線を測れない"
    assert all(
        "toc.txt" not in (c.payload or {}).get("path", "") for c in result.items[0].candidates
    ), result.items[0].candidates
    assert result.items[0].citation is not None
    assert "body.txt" in (result.items[0].citation.payload or {}).get("path", "")


@pytest.mark.asyncio
async def test_clinical_draft_requires_chosen_cand_id_in_the_response_format(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM へ渡すスキーマが **`chosen_cand_id` を必須にしている**こと（結線）。

    既定値つきのフィールドは `model_json_schema()` の `required` に入らないので、structured
    output は**モデルにキーの省略を許す**。実測では、モデルが正しい記述を書きながら
    `chosen_cand_id` だけを省略し、**その記述が捨てられていた**（典拠を選べなかったら記述も
    採らない方針）。その状態では典拠の付与率が実態より低く出る。

    **`strict_response_format` の単体テストだけでは、それが渡っていることを測れない。**
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []
    _patch_chat(
        drafter,
        {"drafts": [{"link_id": "item-dx-date", "chosen_cand_id": 0, "annotation": "aaa"}]},
        calls,
    )

    await drafter.draft([{"link_id": "item-dx-date", "item_text": "診断日"}])

    assert calls, "LLM を呼んでいる前提"
    schema = calls[0]["format"]
    required = schema["$defs"]["LLMClinicalDraft"]["required"]
    assert "chosen_cand_id" in required, required
    assert "annotation" in required, required


def _chunk_item_counts(calls: list) -> list[int]:
    """各コールの user メッセージに何項目載ったかを数える（プロンプトは `### linkId:` で並べる）。"""
    return [c["messages"][1]["content"].count("### linkId:") for c in calls]


@pytest.mark.asyncio
async def test_draft_splits_items_by_the_frozen_chunk_size(
    multi_candidate_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`_CHUNK_SIZE`（2）が実際に分割へ渡っている**こと。

    `chunk_size=_CHUNK_SIZE` を `max(1, len(with_hits))`（分割を潰す）へ変えても、
    **このファイルの25件すべてが通っていた**。分割が潰れると
    1コールに全項目が載り、`num_ctx=8192` を超えて**空 content** になる——症状は「典拠の付与率が
    下がる」形で出るので、原因を分割の潰れに辿れない。

    **期待値は逐語で書く。** `ceil(n / _CHUNK_SIZE)` と書くと**定数を変えても両辺が一緒に動いて
    通る**。5項目・幅2なら `[2, 2, 1]` で、幅 1 / 3 / 4 / 5 のどれとも違う。
    """
    assert _CHUNK_SIZE == 2, "前提: 刻み幅は2（変えたらこの期待値も変える）"
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=multi_candidate_index)
    calls: list = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        ids = [
            line.split(": ", 1)[1]
            for line in kwargs["messages"][1]["content"].splitlines()
            if line.startswith("### linkId: ")
        ]
        payload = {
            "drafts": [{"link_id": i, "chosen_cand_id": None, "annotation": None} for i in ids]
        }
        return types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(payload)))

    drafter._client = types.SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]

    await drafter.draft([{"link_id": f"item-{i}", "item_text": "診断日"} for i in range(5)])

    assert _chunk_item_counts(calls) == [2, 2, 1], "分割が `_CHUNK_SIZE`（2）で行われていない"


@pytest.mark.asyncio
async def test_draft_keeps_the_first_of_duplicated_entries(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**同じ項目に2エントリ返る応答で、有効な下書きを捨てない**こと。

    実際の生成で起きた形: 1エントリ目が `cand_id` と記述あり、2エントリ目が両方 null。
    dict 内包は後勝ちなので、**典拠と記述が黙って捨てられ**、その項目が「LLM が明示的に
    定義なしと答えた」＝意味が決まらなかった項目として集計されていた。

    **矛盾した応答であることは別に記録する**（採用した値と混ぜない）。集計はこの項目を
    典拠つきの分子からも意味が決まらなかった項目の件数からも外して別掲する。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    _patch_chat(
        drafter,
        {
            "drafts": [
                {"link_id": "item-dx-date", "chosen_cand_id": 0, "annotation": "有効な記述"},
                {"link_id": "item-dx-date", "chosen_cand_id": None, "annotation": None},
            ]
        },
        [],
    )

    result = await drafter.draft([{"link_id": "item-dx-date", "item_text": "診断日"}])

    item = result.items[0]
    assert item.annotation == "有効な記述", "後勝ちで有効な下書きが捨てられている"
    assert item.citation is not None
    assert item.no_citation is False
    # **矛盾した応答として記録される**（採用しただけで黙らない）
    assert result.duplicated_link_ids == ("item-dx-date",)
    # 未試行とは別の状態である（混ぜると原因が追えない）
    assert result.unattempted_link_ids == ()


@pytest.mark.asyncio
async def test_draft_does_not_flag_normal_responses_as_duplicated(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重複していない応答を「矛盾した応答」と数えないこと（**正常系で発火しない**）。

    ここが常に立つと、矛盾していない項目まで分子から外れて**典拠の付与率を過小に見せる**。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    _patch_chat(
        drafter,
        {"drafts": [{"link_id": "item-dx-date", "chosen_cand_id": 0, "annotation": "aaa"}]},
        [],
    )

    result = await drafter.draft([{"link_id": "item-dx-date", "item_text": "診断日"}])

    assert result.duplicated_link_ids == ()


@pytest.mark.asyncio
async def test_draft_identifies_unattempted_by_response_not_by_order(
    multi_candidate_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未試行を**応答の有無**で定義していること（順序ではない）。

    2項目のフィクスチャで「最後を落とす」形だけを測ると、**末尾からの並びで実装しても
    区別できない**（末尾スライスへ置き換えたミューテーションが生存する）。
    3項目で**中間**を落とすと、並び順の実装は「応答があった3件目を未試行と呼び、落ちた
    2件目を典拠なしと呼ぶ」ので落ちる。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=multi_candidate_index)
    items = [
        {"link_id": "item-a", "item_text": "診断日"},
        {"link_id": "item-b", "item_text": "進行度"},
        {"link_id": "item-c", "item_text": "診断日"},  # 候補が付くテキストを使う（前提）
    ]
    # 2件目（item-b）だけ応答に含めない
    _patch_chat(
        drafter,
        {
            "drafts": [
                {"link_id": "item-a", "chosen_cand_id": 0, "annotation": "aaa"},
                {"link_id": "item-c", "chosen_cand_id": 0, "annotation": "ccc"},
            ]
        },
        [],
    )
    monkeypatch.setattr(cd_mod, "_CHUNK_SIZE", 3)  # 1コールに3項目

    result = await drafter.draft(items)
    with_cands = {d.link_id for d in result.items if d.candidates}
    assert {"item-a", "item-b", "item-c"} <= with_cands, "3項目すべてに候補がある前提"
    assert result.unattempted_link_ids == ("item-b",), (
        "落ちたのは中間の項目。末尾からの並びで数えると item-c が未試行になる"
    )


@pytest.mark.asyncio
async def test_draft_does_not_call_items_without_candidates_unattempted(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**候補が0件だった項目は「試していない」ではない**（正常系）。

    探索は実行し、候補が1件も無かったという結果である。ここを混ぜると、逆に
    「規約文書に定義が無い項目」が未試行として数から抜ける。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    _patch_chat(
        drafter,
        {
            "drafts": [
                {
                    "link_id": "item-dx-date",
                    "chosen_cand_id": 0,
                    "annotation": "悪性と確定した日を記載する。",
                }
            ]
        },
        [],
    )

    result = await drafter.draft(
        [
            {"link_id": "item-dx-date", "item_text": "診断日"},
            {"link_id": "item-none", "item_text": "まったく無関係な語彙"},
        ]
    )
    assert result.no_hit_count == 1, "候補0件の項目がある前提"
    assert result.unattempted_link_ids == ()


@pytest.mark.asyncio
async def test_draft_skips_items_without_candidates(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """典拠候補が閾値を超えない項目は LLM に渡さず、典拠なしで返すこと。"""
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []
    _patch_chat(drafter, {"drafts": []}, calls)

    # 規約文書と語彙がまったく重ならない項目名
    result = await drafter.draft(
        [{"link_id": "item-unrelated", "item_text": "zzzz-unrelated-token"}]
    )

    assert result.no_hit_count == 1
    assert len(result.items) == 1
    assert result.items[0].no_citation is True
    assert result.items[0].candidates == []
    assert calls == []  # 候補ゼロなら LLM を呼ばない
    # 索引は引けている（＝「候補が無い」であって「索引が使えない」ではない）。
    # ここを取り違えると、索引の故障を「規約文書に規定が無い」と数えてしまう。
    assert result.index_searchable is True
    assert result.llm_model is None  # 1回も呼んでいないのでモデル名は名乗らない


# --------------------------------------------------------------------------- #
# 「典拠は規約文書に限る」防御が drafter 層で効いていること
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_drafter_excludes_patient_data_even_when_it_outranks(
    competing_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """患者データが1位に来る索引でも、典拠候補に混じらないこと。

    `AIClinicalDrafter.draft` から `predicate=is_reference_unit` を外すと、この
    テストだけが落ちる。足切りに助けられて通っていた従来のフィクスチャでは、
    述語を消しても全件 green だった。
    """
    _patch_embedder(monkeypatch)

    # 前提の確認: 述語なしなら患者データが1位に来る索引である
    store = get_store(competing_index)
    qv = HashingEmbedder().embed(["診断日"])[0]
    unrestricted = store.search(qv, top_k=3, min_score=0.5)
    assert unrestricted
    assert unrestricted[0].unit.payload.get("file_role") == "patient_data"

    drafter = AIClinicalDrafter(index_dir=competing_index)
    calls: list = []
    _patch_chat_dynamic(
        drafter,
        lambda msg: {
            "drafts": [
                {
                    "link_id": "item-dx-date",
                    "chosen_cand_id": _cand_ids_by_link(msg)["item-dx-date"][0],
                    "annotation": "組織学的または細胞学的に悪性と確定した日を記載する。",
                }
            ]
        },
        calls,
    )

    result = await drafter.draft([{"link_id": "item-dx-date", "item_text": "診断日"}])

    d = result.items[0]
    assert d.candidates  # 規約文書の候補は出ている
    assert all((c.payload or {}).get("file_role") == "reference" for c in d.candidates)
    assert d.citation is not None
    assert (d.citation.payload or {}).get("file_role") == "reference"
    # 患者データの所在パターンがプロンプトへ入っていないこと
    assert "{patient_id}" not in calls[0]["messages"][1]["content"]


def test_search_finds_reference_below_top_k(buried_reference_index: str) -> None:
    """規約文書がスコア上位 top_k の外にあっても、述語つき検索が拾えること。

    `predicate` 経路が部分ソート（`argpartition`）に戻ると、上位 k 件がすべて
    患者データで埋まり規約文書が0件になる。全件を降順で見る分岐の存在理由を固定する。
    """
    store = get_store(buried_reference_index)
    qv = HashingEmbedder().embed(["診断日"])[0]

    top3 = store.search(qv, top_k=3, min_score=0.0)
    assert len(top3) == 3
    # 前提: 上位3件はすべて患者データ（＝規約文書は上位 top_k の外にいる）
    assert all(h.unit.payload.get("file_role") == "patient_data" for h in top3)

    restricted = store.search(qv, top_k=3, min_score=0.0, predicate=is_reference_unit)
    assert restricted, "規約文書が上位 top_k の外にあると拾えていない"
    assert all(is_reference_unit(h.unit) for h in restricted)


# --------------------------------------------------------------------------- #
# cand_id の取り違え（1コールに複数項目が載る）
# --------------------------------------------------------------------------- #


_TWO_ITEMS: list[dict[str, str | None]] = [
    {"link_id": "item-dx-date", "item_text": "診断日"},
    {"link_id": "item-stage", "item_text": "進行度"},
]


def test_multi_candidate_index_offers_top_k_per_item(multi_candidate_index: str) -> None:
    """前提の確認: この索引は1項目あたり `_TOP_K` 件の候補を返すこと。

    以下の cand_id 系テストは「同じ項目に候補が複数ある」ことが成り立って初めて
    防御を検証できる。1件しか出ない索引で書くと、取り違えのテストは「番号が範囲外
    だったから落ちた」だけになり、照合を位置索引へ戻しても通ってしまう。前提が
    崩れたことに気づけるよう、フィクスチャ自身の性質をここで固定する。
    """
    store = get_store(multi_candidate_index)
    embedder = HashingEmbedder()
    for query in ("診断日", "進行度"):
        hits = store.search(
            embedder.embed([query])[0],
            top_k=_TOP_K,
            min_score=_MIN_SCORE,
            predicate=is_reference_unit,
        )
        assert len(hits) == _TOP_K, (query, [(h.unit.unit_id, h.score) for h in hits])
        assert all(is_reference_unit(h.unit) for h in hits)
        # 候補は互いに別チャンク（同じチャンクが複数回並ぶのでは意味がない）
        assert len({h.unit.unit_id for h in hits}) == _TOP_K


@pytest.mark.asyncio
async def test_cand_ids_are_unique_across_items_in_one_call(
    multi_candidate_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1回の呼び出しに載る全項目を通して cand_id が重複しないこと。

    store は項目ごとに 0 から振り直す。そのまま提示すると、LLM が項目 A の記述に
    項目 B の cand_id を付けても**項目 A の別候補として解決**され、記述と典拠が
    食い違ったまま通る。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=multi_candidate_index)
    calls: list = []
    _patch_chat_dynamic(drafter, lambda msg: {"drafts": []}, calls)

    await drafter.draft(list(_TWO_ITEMS))

    assert len(calls) == 1  # 2項目が同じプロンプトに載っている
    by_link = _cand_ids_by_link(calls[0]["messages"][1]["content"])
    assert len(by_link) == 2
    # 前提: 各項目に候補が複数ある（1件ずつだと通し番号でなくても重複しない）
    assert all(len(ids) >= 2 for ids in by_link.values()), by_link
    all_ids = [i for ids in by_link.values() for i in ids]
    assert len(all_ids) == len(set(all_ids)), by_link


@pytest.mark.asyncio
async def test_cand_id_from_another_item_falls_back_to_no_citation(
    multi_candidate_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """他項目の cand_id を返されたら「典拠なし」に落ちること（取り違えを受理しない）。

    **後の項目（item-stage）が前の項目（item-dx-date）の cand_id を返す**向きで
    再現する。通し番号なので前の項目の番号は 0 から始まり、後の項目の候補列の
    **位置としては有効**な値になる。照合を `hits[chosen_id]` のような位置索引へ
    戻すと、item-stage の記述に item-dx-date が指していた規定の典拠が付き、記述と
    典拠が別の規定を指したまま「典拠つき」で通る。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=multi_candidate_index)
    calls: list = []
    seen: dict[str, list[int]] = {}

    def responder(msg: str) -> dict:
        by_link = _cand_ids_by_link(msg)
        seen.update(by_link)
        return {
            "drafts": [
                {
                    "link_id": "item-stage",
                    "chosen_cand_id": by_link["item-dx-date"][0],
                    "annotation": "治療開始前の臨床的進行度を記載する。",
                }
            ]
        }

    _patch_chat_dynamic(drafter, responder, calls)

    result = await drafter.draft(list(_TWO_ITEMS))

    # 前提: 借りた cand_id は、借りた側（item-stage）の候補列の**位置としては有効**な
    # 番号である。ここが成り立たないと、このテストは「範囲外だから落ちた」だけになり、
    # 位置索引への退行を検出できない。
    borrowed = seen["item-dx-date"][0]
    assert 0 <= borrowed < len(seen["item-stage"]), seen
    assert borrowed not in seen["item-stage"], seen

    stage = next(d for d in result.items if d.link_id == "item-stage")
    assert stage.no_citation is True
    assert stage.citation is None
    assert stage.annotation is None
    assert not any(c.recommended for c in stage.candidates)


@pytest.mark.asyncio
async def test_correct_cand_id_resolves_to_the_chunk_shown_under_that_number(
    multi_candidate_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正しい cand_id は、**その番号で提示したチャンク**へ解決されること。

    取り違えを弾く側（上のテスト）だけでは、`chosen_cand_id` を常に無視して典拠なしに
    する実装でも通ってしまう。肯定側 ——「選ばれた番号がどのチャンクを指すか」—— を
    固定して初めて、cand_id の照合が両方向で意味を持つ。

    後の項目については、通し番号の cand_id は候補列の**位置としては範囲外**になる。
    照合を位置索引へ戻すと item-stage が典拠なしに落ちるので、この向きで固定する。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=multi_candidate_index)
    calls: list = []
    seen: dict[str, list[int]] = {}
    bodies: dict[int, str] = {}

    def responder(msg: str) -> dict:
        seen.update(_cand_ids_by_link(msg))
        bodies.update(_cand_bodies(msg))
        # 各項目とも**自分の2件目**の候補を選ぶ（1件目だと前の項目では位置と一致して
        # しまい、位置索引への退行を通してしまう）
        return {
            "drafts": [
                {
                    "link_id": link_id,
                    "chosen_cand_id": seen[link_id][1],
                    "annotation": f"{link_id} の記述。",
                }
                for link_id in ("item-dx-date", "item-stage")
            ]
        }

    _patch_chat_dynamic(drafter, responder, calls)

    result = await drafter.draft(list(_TWO_ITEMS))

    # 前提: item-stage の選んだ番号は、候補列の位置としては範囲外である
    assert seen["item-stage"][1] >= len(seen["item-stage"]), seen

    by_body = _unit_id_by_body(multi_candidate_index)
    for link_id, prefix in (
        ("item-dx-date", "file:dx_manual.txt"),
        ("item-stage", "file:stage_manual.txt"),
    ):
        d = next(x for x in result.items if x.link_id == link_id)
        chosen = seen[link_id][1]
        expected_unit_id = by_body[bodies[chosen]]
        assert d.no_citation is False, link_id
        assert d.annotation == f"{link_id} の記述。"
        assert d.citation is not None
        # 選ばれた番号で見せた本文を持つチャンクへ解決されている
        assert d.citation.unit_id == expected_unit_id, (link_id, chosen)
        # 典拠はその項目自身のファイルを指す（隣の項目の規定を掴んでいない）
        assert d.citation.unit_id.startswith(prefix), link_id
        assert d.citation.recommended is True
        # 候補一覧の先頭へ繰り上がっている（画面の「一推し」表示と一致する）
        assert d.candidates[0].unit_id == expected_unit_id


# --------------------------------------------------------------------------- #
# 候補の提示内容（抜粋ではなくチャンク全文）
# --------------------------------------------------------------------------- #


def test_user_message_presents_full_chunk_not_snippet() -> None:
    """候補本文が `snippet`（先頭200文字）ではなく `search_text`（全文）であること。

    該当規定が 201 文字目以降にあるチャンクを抜粋だけで見せると、LLM は「該当なし」
    と判断する。その項目は「規約文書を引いても意味が決まらなかった項目」として誤って
    数えられ、その件数が体系的に狂う。
    """
    body = "あ" * 300 + "診断日は病理確定日とする。" + "い" * 300
    unit = ResourceUnit(
        unit_id="file:manual.txt:0-800",
        kind=Kind.FILE,
        label="manual.txt",
        search_text=body,
        snippet=body[:200],  # 索引が作る抜粋（file_indexer と同じ切り方）
        payload={"file_role": "reference", "path": "manual.txt", "char_range": [0, 800]},
    )
    drafter = AIClinicalDrafter(index_dir="/nonexistent")
    msg = drafter._build_user_message(
        [("item-dx-date", "診断日", [ResourceHit(cand_id=0, unit=unit, score=0.9)])]
    )

    assert "診断日は病理確定日とする。" in msg  # 200文字目以降の規定が届いている
    assert body in msg
    # 出典（パス・ページ）は相変わらず渡さない
    assert "manual.txt" not in msg


def test_candidate_cap_matches_indexer_chunk_size() -> None:
    """候補本文の上限が、索引が実際に作るチャンク長と一致していること。

    本番の WebUI は索引の成果物を読むだけで indexer に依存しないため、`_MAX_CAND_CHARS`
    は値を直接持っている。indexer 側でチャンク長を上げたときに**ここだけ取り残されると、
    実運用のプロンプトが静かに num_ctx を溢れる**。結合をテストで固定する。
    """
    assert _MAX_CAND_CHARS == DEFAULT_CHUNK_CHARS


# 1コールが返す出力（drafts の JSON）のために空けておく文字数。1項目あたりの記述を
# 500文字（`_MAX_CAND_CHARS` の抜粋を要約した記述としては十分に長い）と見て、
# link_id・JSON の構文ぶんを含めて丸めた値。
_OUTPUT_RESERVE_CHARS = 500

# 臨床側の経路で想定する thinking の最大長（thinking の長いモデルを基にした値）。
# `think=True` は固定なので thinking は必ず出る。既定（8192）ではこの余地が
# 約1,891 しか無く、この長さの thinking が単独で超えて空応答になる。
_THINKING_OBSERVED_MAX_CHARS = 2138


def test_candidate_budget_fits_num_ctx() -> None:
    """**実際に組み立てたプロンプト**が num_ctx の予算に収まること。

    チャンク全文を渡す以上、`_TOP_K` と `_CHUNK_SIZE` は num_ctx とセットで決まる。
    溢れると **プロンプトの末尾（後ろの項目の候補）から落ちる**ので、症状は
    「特定の項目だけ典拠なしが増える」という形で出て原因を追いにくい。

    定数だけの恒等式（`_CHUNK_SIZE * _TOP_K * _MAX_CAND_CHARS`）で書くと、候補本文
    以外が測られない。実際にコールへ載るのは candidates 本文だけではなく、
    システムプロンプト・構造化出力のスキーマ・項目名・見出し行も同じ num_ctx を
    食う。ここでは `_build_user_message` が組み立てた最大構成の文字列を実測する。

    **見積もりの根拠（1文字＝1トークンは最悪値ではない）**: 日本語 1 文字を 1
    トークンとする換算は上界ではない。BPE の byte fallback により、語彙に無い漢字は
    UTF-8 の 3 バイト＝3トークンへ分解されうる（逆に頻出語は複数文字で1トークン）。
    したがってこのテストは「絶対に溢れない」ことの証明ではなく、**定数を上げたときに
    気づくための回帰テスト**である。真の上界を張れない以上、余裕は次の形で取る。

    - 候補本文は `_MAX_CAND_CHARS` で必ず切り詰める（実装側。索引のチャンク長が
      変わっても入力側の上限は動かない）
    - 出力ぶんを `_OUTPUT_RESERVE_CHARS × _CHUNK_SIZE` として**明示的に予約**する
      （旧テストの「1/4 を空ける」は system prompt とスキーマだけでほぼ埋まり、
      出力ぶんが残らなかった）
    - 残る不確実性は「語彙外の漢字が連続する規約文書」だけで、その場合の症状は
      後ろの項目の典拠なしが増えることである。運用でこれを観測したら
      `_CHUNK_SIZE` を下げる（`_TOP_K` を下げると LLM に選ばせる候補が消える）
    """
    # 最大構成: 1コールに `_CHUNK_SIZE` 項目、各項目に `_TOP_K` 件、本文は上限いっぱい
    body = "あ" * _MAX_CAND_CHARS
    contexts: list[tuple[str, str | None, list[ResourceHit]]] = []
    for i in range(_CHUNK_SIZE):
        contexts.append(
            (
                f"item-{i:04d}",
                "項目名" * 20,  # 長めの項目名
                [
                    ResourceHit(
                        cand_id=i * _TOP_K + j,
                        unit=ResourceUnit(
                            unit_id=f"file:manual.pdf#p{j}:0-{_MAX_CAND_CHARS}",
                            kind=Kind.FILE,
                            label="manual.pdf",
                            search_text=body,
                            payload={"file_role": "reference", "path": "manual.pdf"},
                        ),
                        score=0.9,
                    )
                    for j in range(_TOP_K)
                ],
            )
        )

    user_msg = AIClinicalDrafter(index_dir="/nonexistent")._build_user_message(contexts)
    # 候補本文が丸ごと載っている（切り詰めが効いていれば上限ちょうど）
    assert user_msg.count(body) == _CHUNK_SIZE * _TOP_K

    # **出荷するスキーマで測る**。`model_json_schema()` で測ると、
    # 実際に送るのは `strict_response_format(...)`（`required` を足した分だけ長い）なので、
    # 予算テストが**送っていないもの**を測ることになる。`_MAX_CAND_CHARS` / `_CHUNK_SIZE` を
    # 上げたときに気づくための回帰テストなので、差が32文字でも測定器は実物に合わせる。
    schema = json.dumps(
        strict_response_format(LLMClinicalDrafts, *_REQUIRED_DRAFT_KEYS), ensure_ascii=False
    )
    # **前提を assert として書く**（測っているのが出荷するスキーマであること）。これが無いと、
    # 測る対象を `model_json_schema()` へ戻す退行がテスト自身の中で起きても誰も気づかない
    # （実測でこの形のミューテーションが生存した）。
    assert "chosen_cand_id" in json.loads(schema)["$defs"]["LLMClinicalDraft"]["required"], (
        "前提: 予算は LLM へ実際に送るスキーマ（required を足したもの）で測る"
    )
    prompt_chars = len(DRAFT_SYSTEM_PROMPT) + len(schema) + len(user_msg)
    total = prompt_chars + _OUTPUT_RESERVE_CHARS * _CHUNK_SIZE
    # **予算は `LARGE_CONTEXT_OPTIONS`**。この経路は `options` を明示的に渡す
    # （`test_draft_passes_the_large_num_ctx` が結線を測る）。
    num_ctx = int(LARGE_CONTEXT_OPTIONS["num_ctx"])

    assert total <= num_ctx, {
        "system": len(DRAFT_SYSTEM_PROMPT),
        "schema": len(schema),
        "user": len(user_msg),
        "output_reserve": _OUTPUT_RESERVE_CHARS * _CHUNK_SIZE,
        "total": total,
        "num_ctx": num_ctx,
    }

    # **thinking のぶんが残ることまで測る**。想定する最大（2,138字）に安全率2倍を
    # 掛けた値を下限として置く。**上限の証明ではない**——1文字=1トークンは上界ではないので、
    # 定数や予算を動かしたときに気づくための回帰である。
    headroom = num_ctx - total
    assert headroom >= _THINKING_OBSERVED_MAX_CHARS * 2, {
        "headroom": headroom,
        "thinking_observed_max": _THINKING_OBSERVED_MAX_CHARS,
        "note": "thinking が予算を食い切ると message.content が空になる",
    }


def test_long_candidate_body_is_clipped() -> None:
    """索引のチャンクが想定より長くても、プロンプトへ入る本文は上限で切り詰めること。"""
    body = "あ" * (_MAX_CAND_CHARS + 500)
    unit = ResourceUnit(
        unit_id="file:manual.txt:0-1300",
        kind=Kind.FILE,
        label="manual.txt",
        search_text=body,
        payload={"file_role": "reference", "path": "manual.txt"},
    )
    drafter = AIClinicalDrafter(index_dir="/nonexistent")
    msg = drafter._build_user_message(
        [("item-x", "診断日", [ResourceHit(cand_id=0, unit=unit, score=0.9)])]
    )

    assert body not in msg
    assert body[:_MAX_CAND_CHARS] in msg


@pytest.mark.asyncio
async def test_draft_declares_the_model_digest_on_the_recorder(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成のたびにモデルのダイジェストが来歴へ宣言されること。

    **列を足しただけでは記録されない。** 呼び出し側が宣言しなければ `model_digest` は
    永久に null のままで、それは「記録していない」と読める（索引の同一性の記録と同じ
    結線の問題）。ここで固定するのは**その結線**である。

    タグだけでは実装が特定できない——`ollama pull` で上流が更新されても、ローカルで同名
    タグを作り直しても `model` 列は同じ文字列のままである。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []
    _patch_chat(drafter, {"drafts": []}, calls)

    async def fake_list() -> types.SimpleNamespace:
        return types.SimpleNamespace(
            models=[types.SimpleNamespace(model="model-a:12b", digest="sha256:feed")]
        )

    drafter._client.list = fake_list  # type: ignore[attr-defined]

    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    await drafter.draft(
        [{"link_id": "item-dx-date", "item_text": "診断日"}],
        model="model-a:12b",
        recorder=recorder,
    )

    assert recorder.model_digest == "sha256:feed"
    assert recorder.model_digest_unavailable is None


@pytest.mark.asyncio
async def test_draft_still_generates_when_the_digest_cannot_be_read(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ダイジェストが取れなくても生成は続き、**理由が残る**こと。

    フェイルクローズにしない判断である。多数の項目を生成する途中で推論サーバへの1回の往復が
    失敗したときに、成功した生成を捨てる理由は無い（`provider_of` が同じ形で測定を
    壊した実測が `llm_provider` の docstring にある）。
    """
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []
    _patch_chat(
        drafter,
        {
            "drafts": [
                {
                    "link_id": "item-dx-date",
                    "chosen_cand_id": 0,
                    "annotation": "組織学的または細胞学的に悪性と確定した日を記載する。",
                }
            ]
        },
        calls,
    )

    async def broken_list() -> types.SimpleNamespace:
        # **`TimeoutError` は使わない**——`model_digest_of` の上限時間の分岐
        # （`asyncio.wait_for` が投げるのと同じ型）に吸収されて、理由が「N 秒で応答が無い」
        # になる。ここで測るのは「往復が失敗しても生成が続く」ことなので、別の形を使う。
        raise ConnectionError("接続できない")

    drafter._client.list = broken_list  # type: ignore[attr-defined]

    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    result = await drafter.draft(
        [{"link_id": "item-dx-date", "item_text": "診断日"}],
        model="model-a:12b",
        recorder=recorder,
    )

    # 生成は成功している（記録の失敗が測定を壊していない）
    assert len(result.items) == 1
    assert result.items[0].citation is not None
    assert recorder.model_digest is None
    assert recorder.model_digest_unavailable is not None
    assert "ConnectionError" in recorder.model_digest_unavailable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paths", "visible"),
    [(frozenset({"registry_manual.txt"}), True), (frozenset({"staging_manual.txt"}), False)],
)
async def test_draft_offers_only_the_references_in_the_scope(
    reference_index: str, monkeypatch: pytest.MonkeyPatch, paths: frozenset[str], visible: bool
) -> None:
    """範囲の外の文書は候補にも出さない（様式と無関係な領域の文書が候補に入らないように）。"""
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []
    _patch_chat(drafter, {"drafts": []}, calls)

    result = await drafter.draft(
        [{"link_id": "item-dx-date", "item_text": "診断日"}], reference_paths=paths
    )

    shown = calls[0]["messages"][1]["content"] if calls else ""
    assert ("組織学的または細胞学的" in shown) is visible
    assert all(
        (c.payload or {}).get("path") in paths for d in result.items for c in d.candidates
    ), "範囲の外の文書が候補に入った"


@pytest.mark.asyncio
async def test_draft_shows_the_parent_text_to_the_llm(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """臨床的意味の提示にも親の text を付ける（「数値」だけでは何の値か分からない）。"""
    _patch_embedder(monkeypatch)
    drafter = AIClinicalDrafter(index_dir=reference_index)
    calls: list = []
    _patch_chat(drafter, {"drafts": []}, calls)

    # 親の text も索引の語にしておく（足切りを超えて LLM まで届かせるため）
    await drafter.draft([{"link_id": "x", "item_text": "診断日", "parent_text": "細胞学的"}])

    assert calls, "候補が足切りで消えて LLM まで届いていない"
    assert "項目名: 細胞学的 / 診断日" in calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_draft_searches_with_the_parent_text(
    reference_index: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """臨床的意味の検索クエリにも親の text が入る。"""
    seen: list[list[str]] = []

    class _SpyEmbedder(HashingEmbedder):
        def embed(self, texts: list[str]):  # noqa: ANN201
            seen.append(list(texts))
            return super().embed(texts)

    monkeypatch.setattr(cd_mod, "OllamaEmbedder", lambda **kwargs: _SpyEmbedder())
    drafter = AIClinicalDrafter(index_dir=reference_index)
    _patch_chat(drafter, {"drafts": []}, [])

    await drafter.draft([{"link_id": "x", "item_text": "数値(mg/dL)", "parent_text": "CRP"}])

    assert ["CRP / 数値(mg/dL)"] in seen
