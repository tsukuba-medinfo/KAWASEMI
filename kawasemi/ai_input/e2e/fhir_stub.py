"""E2E 用の最小 FHIR スタブサーバ。

`sources.build_fhir_stub` が書き出した Bundle(JSON) を、患者ID・クエリ種別ごとに
配信する。実 FHIR サーバや Docker を使わずに fhir_extract の実HTTP経路をそのまま
動かすための置き換え。`kawasemi e2e serve` が別ポート（`e2e.FHIR_STUB_PORT`）で起動し、
FHIR_SERVER_URL をこのスタブへ向ける。決定的層はプロセス内（ASGI）で使う。

配信例:
    GET /fhir/Patient?identifier=urn:kawasemi:e2e:trial|E2E-0001
                                                → fhir_stub/_patient_index/E2E-0001.json（crosswalk）
    GET /fhir/Observation?patient=fhirpat-e2e-0001&code=pathogen
                                                → fhir_stub/fhirpat-e2e-0001/Observation-pathogen.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from kawasemi.ai_input.e2e import FHIR_STUB_DIR

# パスセグメントに使える文字。FHIR id は [A-Za-z0-9-.]{1,64} だが、スタブは
# 試験登録番号（E2E-0001）や観察コード（specimen-date）も同じ位置に受けるため `_` も許す。
_SEGMENT_MAX_LEN = 64
_SAFE_SEGMENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")

# ファイル名は「利用者由来の値 + 固定の接頭辞/接尾辞」で合成する。上限を合成後の
# ファイル名へ当てると、受理される値の実効上限が接頭辞・接尾辞のぶんだけ短くなり、
# 上限内の値が実在しても静かに引けなくなる。
# **上限は利用者由来の値そのものに当てる**ため、ファイル名セグメント側の上限は
# 固定部分の長さを足して導出する。直値を書くと接頭辞・接尾辞を変えた瞬間に
# 実効上限が静かにずれるので、必ず len() から導出すること。
_BUNDLE_SUFFIX = ".json"
_OBSERVATION_PREFIX = "Observation-"
_PATIENT_INDEX_FILENAME_MAX_LEN = _SEGMENT_MAX_LEN + len(_BUNDLE_SUFFIX)
_OBSERVATION_FILENAME_MAX_LEN = len(_OBSERVATION_PREFIX) + _SEGMENT_MAX_LEN + len(_BUNDLE_SUFFIX)


def _base_dir() -> Path:
    """Bundle ルート。環境変数 FHIR_STUB_DIR で上書き可（テスト用）。"""
    return Path(os.getenv("FHIR_STUB_DIR", str(FHIR_STUB_DIR)))


def is_safe_segment(value: str, *, max_len: int = _SEGMENT_MAX_LEN) -> bool:
    """クエリパラメータ由来の値を単一のパスセグメントとして使ってよいか判定する。

    区切り文字・`..`・NUL・許可外の文字を拒否する。値を置換せず
    拒否するのは、スタブが行うのが「探す」操作だからである。`safe_fhir_id` のように
    置換すると、要求されたものと別の Bundle を黙って返すことになる。

    max_len はセグメントの種別ごとに変える。合成したファイル名を
    渡すときは接尾辞のぶん長い上限を指定する。長さ以外の検査（`..`・字種）は
    種別によらず同じである。
    """
    if not value or len(value) > max_len:
        return False
    if value.strip(".") == "":  # "." ".." "..." など
        return False
    return all(c in _SAFE_SEGMENT_CHARS for c in value)


def _resolve_under_base(*segments: str, filename_max_len: int | None = None) -> Path:
    """配信ルート起点でパスを解決し、ルート配下に収まることを保証する。

    セグメント検査（`is_safe_segment`）を通った値だけを結合し、結合後に
    `resolve()` して配下であることを確かめる二段構えにしている。前段だけでも
    脱出は止まるが、`FHIR_STUB_DIR` に symlink を含む構成でも配下に留まることを
    後段が担保する。

    Args:
        filename_max_len: **最後のセグメント（＝ファイル名）だけ**に適用する長さ上限。
            固定の接頭辞・接尾辞を足して合成したファイル名を渡すときに指定する。
            中間ディレクトリのセグメントは常に `_SEGMENT_MAX_LEN`
            で、緩和が別のセグメントへ波及しない。長さ以外の検査（`..`・字種）は
            セグメントの種別によらず同じである。

    Raises:
        HTTPException: セグメントが不正、または解決後がルート外
    """
    last = len(segments) - 1
    for i, segment in enumerate(segments):
        limit = (
            filename_max_len if (filename_max_len is not None and i == last) else _SEGMENT_MAX_LEN
        )
        if not is_safe_segment(segment, max_len=limit):
            raise HTTPException(status_code=400, detail="パスに使えない値が含まれています")
    base = _base_dir().resolve()
    path = base.joinpath(*segments).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(status_code=400, detail="パスが配信ルート外を指しています")
    return path


def _load_bundle(
    patient: str, filename: str, *, filename_max_len: int | None = None
) -> dict[str, Any]:
    path = _resolve_under_base(patient, filename, filename_max_len=filename_max_len)
    if not path.is_file():
        # 配信ルートは resolve 済みの絶対パスなので、そのまま出すとホストの
        # ディレクトリ構成を漏らす。要求された位置だけを返す。
        raise HTTPException(
            status_code=404, detail=f"Bundle が見つかりません: {patient}/{filename}"
        )
    bundle: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return bundle


def create_app() -> FastAPI:
    app = FastAPI(title="KAWASEMI Demo FHIR Stub", docs_url=None, redoc_url=None)

    @app.get("/fhir/Condition")
    async def get_condition(patient: str) -> dict[str, Any]:
        # ファイル名は固定なので緩和は要らない（利用者由来の値を合成しない）。
        return _load_bundle(patient, f"Condition{_BUNDLE_SUFFIX}")

    @app.get("/fhir/Observation")
    async def get_observation(patient: str, code: str | None = None) -> dict[str, Any]:
        # code も患者索引と同じく合成後のファイル名になる。上限を合成後に当てると
        # `Observation-`(12) + `.json`(5) のぶん code の実効上限が縮むので、
        # ファイル名側の上限を固定部分の長さから導出して渡す（患者索引と同型）。
        if code:
            # 利用者由来の code を合成するときだけ、固定部分のぶん上限を緩める。
            return _load_bundle(
                patient,
                f"{_OBSERVATION_PREFIX}{code}{_BUNDLE_SUFFIX}",
                filename_max_len=_OBSERVATION_FILENAME_MAX_LEN,
            )
        return _load_bundle(patient, f"Observation{_BUNDLE_SUFFIX}")

    @app.get("/fhir/Patient")
    async def search_patient(identifier: str | None = None) -> dict[str, Any]:
        """identifier=system|value（または value）で crosswalk Patient を検索する。

        resolver(FhirIdentifierResolver) が登録番号で患者を引く入口。該当が無ければ
        空の searchset を返す（resolver は not_found 扱い）。
        """
        empty = {"resourceType": "Bundle", "type": "searchset", "total": 0, "entry": []}
        value = identifier.split("|", 1)[1] if identifier and "|" in identifier else identifier
        if not value:
            return empty
        # 検査は _resolve_under_base に一本化する（セグメント検査・配下検査とも）。
        # ここで 400 を返すと FhirIdentifierResolver が例外を再送出し、その呼び出しは
        # プランごとの try の外にあるため DSL プログラム全体が FAILED になって
        # 全項目が失われる。**この入口だけは 400 を返してはならない**ので、関門が
        # 投げる 400 を捕まえて空 searchset に落とす（不変条件は
        # test_patient_search_never_returns_400 が固定している）。
        #
        #
        # 検査対象は合成後のファイル名だが、上限は接尾辞のぶん長く取る。
        # _PATIENT_INDEX_FILENAME_MAX_LEN を接尾辞の長さから導出しているので、
        # **登録番号そのものの上限は _SEGMENT_MAX_LEN と一致する**（境界の両側は
        # test_patient_search_accepts_identifier_at_segment_limit と
        # test_patient_search_rejects_identifier_over_segment_limit が固定する）。
        #
        # 値そのものへの is_safe_segment は置かない。合成後の検査が封じ込めについて
        # 完全だからである。単一セグメントで遡上しうる値は厳密に `.` と `..` だけで、
        # `value + 接尾辞` がドットのみになるのは接尾辞がドットのみのときに限られ、
        # その場合は is_safe_segment 自身のドットのみ検査が拒否する。長さと字種も
        # 合成後の検査に包含される。
        # 単独で外しても落ちるテストが無い検査を「層」として残すと、効いていない防御を
        # 効いていると誤認するので置かない。
        filename = f"{value}{_BUNDLE_SUFFIX}"
        try:
            path = _resolve_under_base(
                "_patient_index", filename, filename_max_len=_PATIENT_INDEX_FILENAME_MAX_LEN
            )
        except HTTPException:
            return empty
        if not path.is_file():
            return empty
        bundle: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return bundle

    @app.get("/fhir/Patient/{patient_id}")
    async def get_patient(patient_id: str) -> dict[str, Any]:
        return {
            "resourceType": "Patient",
            "id": patient_id,
            "identifier": [{"value": patient_id}],
        }

    @app.get("/fhir/metadata")
    async def metadata() -> dict[str, Any]:
        return {"resourceType": "CapabilityStatement", "status": "active", "fhirVersion": "4.0.1"}

    return app


app = create_app()
