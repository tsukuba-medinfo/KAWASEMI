"""リソース索引の生成（indexer 参照実装の中核）

`kawasemi-indexer build` から呼ばれ、入力源から ResourceUnit を組み立て、embedding を
事前計算して索引ディレクトリ（*_index.jsonl + embeddings.npy + build_info.json）へ
書き出す。出力は schema.py の契約に従う（施設は同契約で別実装に差し替え可）。

v1 は kind=db のみ対応。db の入力源は「DB定義書由来のみ」（本番DB非接続）。
既存の DB定義書コーパス `L2/column_index.jsonl`（1行1カラム）をそのまま入力に使う。

索引は「地図」であり患者データを載せない（DB定義書はスキーマ情報のみ）。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from kawasemi_indexer.embedder import Embedder
from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.physical import PHYSICAL_KEYS
from kawasemi_indexer.schema import (
    BUILD_INFO_FILENAME,
    EMBEDDINGS_FILENAME,
    SCHEMA_VERSION,
    BuildInfo,
    Kind,
    ResourceUnit,
)
from kawasemi_indexer.site_rules import PatientKeyRules, load_indexer_rules

logger = get_logger(__name__)

# patient_key 推定: 患者を識別するカラム名のパターン（physical_name に対して）
#
# **施設の命名に当たらないことが多い。** 患者ID列の物理名は施設・ベンダーごとに
# 略号（`PID` や番号付きの列名など）であることが多く、`_id$` で終わるとは限らない。
#
# **論理名（下の `_PATIENT_KEY_LOGICAL_ORDER`）を先に見る。** このパターンはフォールバックとして
# 残す——別の施設や別のスキーマでは当たりうるし、消すと後方互換が切れる。
_PATIENT_KEY_RE = re.compile(r"(^|_)(pt|patient|kanja)_?id$", re.IGNORECASE)

# 患者キーと判定する**論理名の完全一致**の集合。
#
# **部分一致にしない。** 部分一致だと、同じ表に修飾付きの患者IDが並ぶ表で候補が2件になる。
# 例（架空）:
#
#   DEMO.ADMISSION  PT_NO(患者番号)  ORIG_PT_NO(移行元患者番号)
#   DEMO.REFERRAL   PT_ID(患者ID)    SRC_PT_ID(他院患者ID)
#   DEMO.DPC_CASE   ID(患者ID)       ID_RAW(変換前患者ID)
#
# **他院の ID や変換前の ID を持つ列を患者キーにすると取得が静かに壊れる。**
#
# **これは論理名の文字列一致という機械的な規則であって、施設に確認したものではない。**
# 多数の表へ同じ物理名・論理名で複製される列は、全表で同じ意味かを施設に確かめるまで
# 限界として扱う。
#
# **順序に意味がある。** 同じ表に複数が並ぶとき、先に書いたものを採る。患者を指す列が
# 2種類（番号と内部の識別子など）並ぶ表では、順序が無いと「2件あるので付けない」へ倒れる。
_PATIENT_KEY_LOGICAL_ORDER: tuple[str, ...] = (
    "患者番号",
    "患者ID",
    "患者ＩＤ",
    "診療録番号",
    "カルテ番号",
)

# 施設固有の規則は `indexer_rules.yaml` の `patient_key` から読む（site_rules.py）。
#
# - `additional_logical_names`: **上の集合に足す論理名。** 上のどれも無いときだけ、書いた順に
#   採る（上の順序の後ろに続く）。施設の命名に固有の論理名はここに書く。
# - `conditional_logical_names`: **曖昧な論理名（`ID` など）を条件付きで認める。** 論理名も
#   物理名も `ID` という列は上の集合へそのまま入れない。命名の一貫性を確かめた接続
#   （connection_id）の、PK の列にだけ認める。**別の DB の一貫性を根拠に、別の命名体系の表へ
#   適用してはいけない**——同じ `ID` が患者と無関係なマスタ表の主キーでもありうり、
#   そこへ患者キーを付けると `WHERE ID = :patient_id` が組み立てられてしまう。
# - `ambiguous_tables`: **論理名の完全一致でも候補が2件以上残る表**（`db_qualified`）。
#   **患者キーを付けない。** 同じ表に「患者ID」が2つあるという事実自体が、その表が2人の患者を
#   関係づけている可能性を示す（2人の患者を並べる列や、他院の ID を持つ列と同じ形）。
#
# **`needs_input` が残るのは見える失敗、間違った `patient_key` は見えない失敗である。**
# どちらも施設に確認した規則ではないので、施設の判断として site 側に置く。


def _column_search_text(col: dict[str, Any]) -> str:
    """カラムの検索語彙（embedding対象）を組み立てる。"""
    parts = [
        col.get("table_logical"),
        col.get("physical_name"),
        col.get("logical_name"),
        col.get("notes"),
        col.get("db_group"),
    ]
    return " ".join(str(p) for p in parts if p)


def _column_label(col: dict[str, Any]) -> str:
    return col.get("logical_name") or col.get("physical_name") or "(無名カラム)"


def _column_snippet(col: dict[str, Any]) -> str:
    """候補リストでLLM/人が妥当性判断に使う1行抜粋。"""
    bits = [
        f"DB={col.get('db_group')}",
        f"表={col.get('db_qualified')}",
    ]
    if col.get("table_logical"):
        bits.append(f"（{col['table_logical']}）")
    bits.append(f"型={col.get('type_raw')}")
    if col.get("pk"):
        bits.append("PK")
    if col.get("notes"):
        bits.append(f"メモ={col['notes']}")
    return " ".join(b for b in bits if b)


def infer_patient_key(
    columns: list[dict[str, Any]], rules: PatientKeyRules | None = None
) -> str | None:
    """1テーブルのカラム群から患者識別カラム（physical_name）を推定する。

    優先順: **論理名の完全一致** > 物理名パターンに一致する列 > パターン一致かつPK > 単一PK。
    推定できなければ None（→ ステップ3で needs_input にする）。

    論理名を先に見るのは、**物理名パターンが施設の命名に当たらないことが多い**ためである。
    詳細は `_PATIENT_KEY_LOGICAL_ORDER` のコメントを参照。

    `rules` は施設の規則（条件付きの論理名・曖昧な表）。省略時は `indexer_rules.yaml`
    から読む（無ければ空＝施設に依らない振る舞い）。

    **候補が2件以上残る表では None を返す。** 片方を選ぶと間違いが不可視のまま取得へ流れる。
    """
    if rules is None:
        rules = load_indexer_rules().patient_key

    order = _PATIENT_KEY_LOGICAL_ORDER + tuple(
        n for n in rules.additional_logical_names if n not in _PATIENT_KEY_LOGICAL_ORDER
    )
    known = frozenset(order)

    def _logical_ok(col: dict[str, Any]) -> bool:
        name = (col.get("logical_name") or "").strip()
        if name in known:
            return True
        allowed = rules.conditional_logical_names.get(name)
        if allowed is None:
            return False
        return bool(col.get("pk")) and (col.get("connection_id") or "") in allowed

    logical_hits = [c for c in columns if c.get("physical_name") and _logical_ok(c)]
    # **`_PATIENT_KEY_LOGICAL_ORDER`（＋施設の追加分）の順に、最初に当たった論理名だけを残す。**
    # 同じ論理名が2列あるときだけ「候補が2件」として付けない（複製された列と本体の両方に
    # 同じ論理名の列がある表など）。
    if len(logical_hits) > 1:
        for name in order:
            picked = [c for c in logical_hits if (c.get("logical_name") or "").strip() == name]
            if picked:
                logical_hits = picked
                break
    if logical_hits:
        table = (columns[0].get("db_qualified") or "").strip() if columns else ""
        if table in rules.ambiguous_tables:
            return None
        if len(logical_hits) > 1:
            logger.warning(
                f"論理名が患者キーに完全一致する列が {len(logical_hits)} 件ある（付けない）: "
                f"{table} {[c['physical_name'] for c in logical_hits]}"
            )
            return None
        physical_logical: str = logical_hits[0]["physical_name"]
        return physical_logical

    pattern_hits = [
        c for c in columns if c.get("physical_name") and _PATIENT_KEY_RE.search(c["physical_name"])
    ]
    if pattern_hits:
        pk_among = [c for c in pattern_hits if c.get("pk")]
        chosen = pk_among[0] if pk_among else pattern_hits[0]
        return chosen.get("physical_name")

    # **「単一PKなら採用」の段は落とした。**
    #
    # 患者キーでない列を患者キーとして拾っていた（日付やマスタのコード、別の実体の
    # 識別子が単一PKの表）。
    #
    # **`WHERE EVENT_ID = :patient_id` のような条件は動いてしまうぶんだけ悪質**で、SQL として
    # は通り、結果が0件か誤った行になる。`patient_key` を付けなければステップ3が `needs_input` を
    # 立てるので、人が「この表は別の実体の ID で引くので患者との紐付けは別表を経由する」と書ける。
    #
    # **`needs_input` が残るのは見える失敗、間違った `patient_key` は見えない失敗である**
    # （曖昧な表を付けないのと同じ原則）。
    return None


def resolve_patient_key(
    columns: list[dict[str, Any]], rules: PatientKeyRules | None = None
) -> tuple[str | None, str | None]:
    """1テーブルの患者キー列（physical_name）と key_type を決める。

    取り込みレイヤ（kawasemi-indexer）が人確認のうえ宣言した ``is_patient_key`` を
    最優先で採用する（取り違え防止の根拠が明示的なため）。宣言が無い場合のみ従来の
    ``infer_patient_key`` ヒューリスティックへフォールバックする（後方互換）。

    Returns: (patient_key 物理名, key_type)。key_type は宣言時のみ。
    """
    declared = [c for c in columns if c.get("is_patient_key") and c.get("physical_name")]
    if declared:
        if len(declared) > 1:
            logger.warning(
                "複数列が is_patient_key 宣言されています（先頭を採用）: "
                f"{[c['physical_name'] for c in declared]}"
            )
        chosen = declared[0]
        return chosen["physical_name"], chosen.get("key_type")
    return infer_patient_key(columns, rules), None


def build_db_units(
    columns: list[dict[str, Any]], rules: PatientKeyRules | None = None
) -> list[ResourceUnit]:
    """DB定義書のカラム配列（column_index.jsonl 由来）から db ユニットを組み立てる。

    connection_id は当面 db_group を流用する（接続レジストリ connections.yaml は
    別PR。実行時に connection_id → 接続詳細を解決する想定）。
    patient_key は同一テーブル内のカラム群から決め（宣言優先・推定フォールバック）、
    key_type とともに全カラムに付与する。
    """
    # テーブル単位（db_group, db_qualified）でグルーピングして patient_key を決定
    tables: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for col in columns:
        key = (col.get("db_group"), col.get("db_qualified"))
        tables.setdefault(key, []).append(col)
    if rules is None:
        rules = load_indexer_rules().patient_key
    table_keys = {key: resolve_patient_key(cols, rules) for key, cols in tables.items()}
    # 「患者で絞らない表」の宣言。取り込みで人が確認した宣言（`patient_scoped=False`）
    # だけを採る。宣言された表は、推定された patient_key も落とす（宣言と患者キーの併存は
    # クエリ側で矛盾として人へ回るので、**宣言の側を正にして索引では併存させない**）。
    unscoped_tables = {
        key for key, cols in tables.items() if any(c.get("patient_scoped") is False for c in cols)
    }
    for key in unscoped_tables:
        if table_keys[key][0] is not None:
            logger.warning(
                f"患者で絞らない表の宣言を優先して patient_key を落とす: {key[1]}"
                f"（{table_keys[key][0]}）"
            )
        table_keys[key] = (None, None)

    units: list[ResourceUnit] = []
    for col in columns:
        physical = col.get("physical_name")
        if not physical:
            continue  # 物理名なしは索引対象外
        db_qualified = col.get("db_qualified")
        key = (col.get("db_group"), db_qualified)
        patient_key, key_type = table_keys.get(key, (None, None))
        unit_id = f"db:{db_qualified}.{physical}"
        payload: dict[str, Any] = {
            "connection_id": col.get("db_group"),
            "db_qualified": db_qualified,
            "table_logical": col.get("table_logical"),
            "physical_name": physical,
            "logical_name": col.get("logical_name"),
            "type_raw": col.get("type_raw"),
            "pk": bool(col.get("pk")),
            "patient_key": patient_key,
            "key_type": key_type,
            "notes": col.get("notes"),
            # 論理名がLLM下書き（人確認前）由来なら真（debug の ⚑inferred 表示に使う）
            "inferred": bool(col.get("logical_name_inferred")),
        }
        if key in unscoped_tables:
            # 宣言された表だけに**キーごと**足す（宣言の無い表の payload は従来と同じ形のまま。
            # `patient_scoped: true` は書かない——クエリ側は `False` のときだけ意味を読む）
            payload["patient_scoped"] = False
        physical_id = col.get("physical")
        if isinstance(physical_id, dict) and all(physical_id.get(k) for k in PHYSICAL_KEYS):
            # 実行用の識別子。ingest が決めたものだけを写し、無い列には**キーごと**
            # 付けない（KAWASEMI は揃っていない単位の SQL を組まない）
            payload["physical"] = {k: str(physical_id[k]) for k in PHYSICAL_KEYS}
        units.append(
            ResourceUnit(
                unit_id=unit_id,
                kind=Kind.DB,
                label=_column_label(col),
                search_text=_column_search_text(col),
                snippet=_column_snippet(col),
                payload=payload,
            )
        )
    return units


def _read_column_index(source: str | Path) -> list[dict[str, Any]]:
    """db 入力源を読む。ディレクトリなら L2/column_index.jsonl を、ファイルなら直接読む。"""
    path = Path(source)
    if path.is_dir():
        path = path / "L2" / "column_index.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"DB定義書が見つかりません: {path}")
    columns: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                columns.append(json.loads(line))
    return columns


# value_dictionary（マスタの統制語彙）を search_text に畳み込む際の上限。
# 巨大コード表（検体検査の項目マスタ等）で search_text/payload が膨れるのを防ぐ。
_VALUE_FOLD_LABEL_CAP = 200
_VALUE_DICT_PAYLOAD_CAP = 500


def _resolve_value_dict_path(source: str | Path) -> Path:
    """db 入力源に対応する value_dictionary.jsonl の場所を返す（無くてもパスは返す）。"""
    path = Path(source)
    if path.is_dir():
        return path / "value_dictionary.jsonl"
    # ファイル指定（…/L2/column_index.jsonl）なら corpus ルート直下を見る
    return path.parent.parent / "value_dictionary.jsonl"


def _read_value_dictionary(source: str | Path) -> list[dict[str, Any]]:
    """value_dictionary.jsonl を読む。未配置なら空（マスタ語彙は任意）。"""
    path = _resolve_value_dict_path(source)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def fold_value_dictionary(units: list[ResourceUnit], entries: list[dict[str, Any]]) -> int:
    """小コード表の code→label を、対応する **FK 列** ユニットの search_text/payload へ畳む。

    `physical_name` 付きエントリ（値ドメイン注釈の FK 列＝性別・血液型等の小区分）のみを
    対象とする。検索は label に当たる（「男性」で当該 SEX 列が surface）。
    `payload['value_dict']` は利用側が WHERE 句を組み立てるための候補メタ。上限超過は切詰。

    `physical_name` なし（マスタ表自身）のエントリは **本関数では畳まない**。大規模臨床
    マスタ（病名/検査/薬剤）は1列の search_text に詰めると検索が潰れるため、
    `build_master_units` で 1エントリ=1ユニット（kind=master）として索引化する（索引仕様 §6.2）。
    Returns: 畳み込み対象となったユニット数。
    """
    by_col: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for e in entries:
        if not e.get("physical_name"):
            continue  # マスタ表自身 → build_master_units 側で処理
        pair = {"code": e.get("code"), "label": e.get("label")}
        by_col.setdefault((e.get("db_qualified"), e.get("physical_name")), []).append(pair)

    folded = 0
    truncated = 0
    for u in units:
        if u.kind != Kind.DB:
            continue
        pairs = by_col.get((u.payload.get("db_qualified"), u.payload.get("physical_name")))
        if not pairs:
            continue
        if len(pairs) > _VALUE_DICT_PAYLOAD_CAP:
            truncated += 1
        labels = [p["label"] for p in pairs if p.get("label")]
        if labels:
            u.search_text = (u.search_text + " " + " ".join(labels[:_VALUE_FOLD_LABEL_CAP])).strip()
        u.payload["value_dict"] = pairs[:_VALUE_DICT_PAYLOAD_CAP]
        folded += 1
    if truncated:
        logger.warning(
            f"value_dict が上限 {_VALUE_DICT_PAYLOAD_CAP} 件で切り詰められたユニット: {truncated}件"
            f"（候補メタが不完全な可能性）"
        )
    if folded:
        logger.info(f"value_dictionary を {folded} ユニットに畳み込みました")
    return folded


def build_master_units(entries: list[dict[str, Any]]) -> list[ResourceUnit]:
    """マスタ表自身の code→label（physical_name なしエントリ）を 1エントリ=1ユニットに展開。

    大規模臨床マスタ（病名/検査/薬剤等）を、各ラベルが独立した embedding を持つ検索ユニット
    にする。これにより「急性骨髄性白血病」のような語が、200件畳込上限に潰されず直接 hit する。
    payload はマスタ表の場所と code/label を持ち、利用側が `WHERE <code列>='...'` を組める。
    """
    units: list[ResourceUnit] = []
    seen: set[str] = set()
    for e in entries:
        if e.get("physical_name"):
            continue  # FK 列は fold_value_dictionary 側
        code = e.get("code")
        label = e.get("label")
        dbq = e.get("db_qualified")
        conn = e.get("connection_id")
        if code is None or label is None or dbq is None:
            continue
        unit_id = f"master:{conn}:{dbq}:{code}"
        if unit_id in seen:
            continue  # 同一 code の重複は1件に
        seen.add(unit_id)
        aliases = _normalize_aliases(e.get("aliases"))
        search_text = " ".join([str(label), *aliases]).strip()
        payload: dict[str, Any] = {
            "connection_id": conn,
            "db_qualified": dbq,
            "code": code,
            "label": label,
        }
        if aliases:
            payload["aliases"] = aliases
        units.append(
            ResourceUnit(
                unit_id=unit_id,
                kind=Kind.MASTER,
                label=str(label),
                search_text=search_text,
                snippet=f"マスタ {dbq} {label}（code={code}）",
                payload=payload,
            )
        )
    return units


def _normalize_aliases(raw: Any) -> list[str]:
    """別名を list[str] に正規化する。CSV 由来は ';' 区切り文字列、JSONL 由来は配列を許容。"""
    if not raw:
        return []
    if isinstance(raw, str):
        return [a.strip() for a in raw.split(";") if a.strip()]
    if isinstance(raw, list):
        return [str(a).strip() for a in raw if str(a).strip()]
    return []


def write_index(
    units_by_kind: dict[str, list[ResourceUnit]],
    embedder: Embedder,
    out_dir: str | Path,
    indexer_version: str = "0.1.0",
) -> BuildInfo:
    """ユニット群を embedding して索引ディレクトリへ書き出す。

    embeddings.npy の行順は Kind.ORDER に従って各 *_index.jsonl を連結した順。
    検索側（store）はこの順序規約に依存する。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Kind.ORDER 順に連結（jsonl 書き出し順 = npy 行順）
    ordered: list[tuple[str, ResourceUnit]] = []
    kind_counts: dict[str, int] = {}
    for kind in Kind.ORDER:
        kind_units = units_by_kind.get(kind, [])
        if not kind_units:
            continue
        kind_counts[kind] = len(kind_units)
        for u in kind_units:
            ordered.append((kind, u))

    # 各 kind の jsonl を書き出し
    for kind in Kind.ORDER:
        kind_units = units_by_kind.get(kind, [])
        if not kind_units:
            continue
        path = out / Kind.INDEX_FILENAMES[kind]
        with path.open("w", encoding="utf-8") as fh:
            for u in kind_units:
                fh.write(json.dumps(u.model_dump(), ensure_ascii=False) + "\n")

    # embedding（連結順）
    texts = [u.search_text for _, u in ordered]
    matrix = embedder.embed(texts) if texts else np.zeros((0, 1), dtype=np.float32)
    np.save(out / EMBEDDINGS_FILENAME, matrix.astype(np.float32))

    dim = int(matrix.shape[1]) if matrix.size else getattr(embedder, "dim", 0)
    info = BuildInfo(
        schema_version=SCHEMA_VERSION,
        indexer_version=indexer_version,
        embedding_model=embedder.model,
        embedding_digest=_embedder_digest(embedder),
        dim=dim,
        normalized=True,
        unit_count=len(ordered),
        kind_counts=kind_counts,
        built_at=datetime.now(UTC).isoformat(),
    )
    (out / BUILD_INFO_FILENAME).write_text(
        json.dumps(info.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        f"リソース索引を書き出しました: {out} "
        f"（{len(ordered)}ユニット, model={embedder.model}, dim={dim}）"
    )
    return info


def _embedder_digest(embedder: Embedder) -> str | None:
    """embedder が名乗るモデルのダイジェスト。無い・取れない・例外なら None（未検証）。"""
    fn = getattr(embedder, "digest", None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:  # noqa: BLE001 - ダイジェストの取得が索引ビルドを壊さない
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def build_db_index(
    source: str | Path,
    out_dir: str | Path,
    embedder: Embedder,
    indexer_version: str = "0.1.0",
) -> BuildInfo:
    """DB定義書由来で kind=db の索引を生成して書き出す（db単独で自己完結）。"""
    return build_index(out_dir, embedder, db_source=source, indexer_version=indexer_version)


# --------------------------------------------------------------------------- #
# kind=fhir: 臨床項目辞書（手キュレーション JSONL）由来
# --------------------------------------------------------------------------- #


def _fhir_search_text(item: dict[str, Any]) -> str:
    """臨床項目の検索語彙（embedding対象）。label＋別名＋種別＋コード＋メモ。"""
    parts: list[Any] = [item.get("label")]
    aliases = item.get("aliases") or []
    if isinstance(aliases, list):
        parts.extend(aliases)
    parts.extend([item.get("resource_type"), item.get("code"), item.get("notes")])
    return " ".join(str(p) for p in parts if p)


def _fhir_snippet(item: dict[str, Any]) -> str:
    bits = [item.get("resource_type")]
    if item.get("code"):
        bits.append(str(item["code"]))
    if item.get("fhir_path"):
        bits.append(str(item["fhir_path"]))
    if item.get("notes"):
        bits.append(f"メモ={item['notes']}")
    return " ".join(b for b in bits if b)


def build_fhir_units(items: list[dict[str, Any]]) -> list[ResourceUnit]:
    """臨床項目辞書（1項目=1臨床概念）から fhir ユニットを組み立てる。

    payload は fhir_extract がそのまま消費できる query_template + fhir_path を持つ。
    connection_id は辞書側の指定（無ければ None）。
    """
    units: list[ResourceUnit] = []
    for item in items:
        label = item.get("label")
        rtype = item.get("resource_type")
        if not label or not rtype:
            continue  # label と resource_type は必須
        code = item.get("code")
        unit_id = f"fhir:{rtype}:{code or label}"
        units.append(
            ResourceUnit(
                unit_id=unit_id,
                kind=Kind.FHIR,
                label=label,
                search_text=_fhir_search_text(item),
                snippet=_fhir_snippet(item),
                payload={
                    "connection_id": item.get("connection_id"),
                    "resource_type": rtype,
                    "query_template": item.get("query_template"),
                    "fhir_path": item.get("fhir_path"),
                    "code": code,
                },
            )
        )
    return units


def _read_fhir_dictionary(source: str | Path) -> list[dict[str, Any]]:
    """fhir 入力源を読む。ディレクトリなら fhir_dictionary.jsonl を、ファイルなら直接。"""
    path = Path(source)
    if path.is_dir():
        path = path / "fhir_dictionary.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"FHIR臨床項目辞書が見つかりません: {path}")
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def build_fhir_index(
    source: str | Path,
    out_dir: str | Path,
    embedder: Embedder,
    indexer_version: str = "0.1.0",
) -> BuildInfo:
    """臨床項目辞書由来で kind=fhir の索引を生成して書き出す（fhir単独で自己完結）。"""
    return build_index(out_dir, embedder, fhir_source=source, indexer_version=indexer_version)


# --------------------------------------------------------------------------- #
# 複数 kind を1回で索引化（embeddings.npy の行整合のため一括書き出し）
# --------------------------------------------------------------------------- #


def build_index(
    out_dir: str | Path,
    embedder: Embedder,
    *,
    db_source: str | Path | None = None,
    fhir_source: str | Path | None = None,
    file_source: str | Path | None = None,
    file_manifest: str | Path | None = None,
    indexer_version: str = "0.1.0",
) -> BuildInfo:
    """指定された各 kind の入力源からユニットを集め、単一の索引として書き出す。

    embeddings.npy は全 kind を Kind.ORDER 順に連結した1ファイルなので、複数 kind は
    必ず1回の build_index で一括生成する（別々に書くと行がずれて整合しない）。

    file kind は reference 文書ディレクトリ(file_source)＋patient_data マニフェスト
    (file_manifest)の両方を受け、合わせて Kind.FILE のユニットにする。
    """
    units_by_kind: dict[str, list[ResourceUnit]] = {}
    if db_source is not None:
        units = build_db_units(_read_column_index(db_source))
        # 同梱の value_dictionary.jsonl（マスタ統制語彙）:
        #  - FK 列（小区分）→ 列の search_text/候補メタへ畳込
        #  - マスタ表自身（病名/検査/薬剤）→ 1エントリ=1ユニット（kind=master）
        value_entries = _read_value_dictionary(db_source)
        fold_value_dictionary(units, value_entries)
        units_by_kind[Kind.DB] = units
        master_units = build_master_units(value_entries)
        if master_units:
            units_by_kind[Kind.MASTER] = master_units
            logger.info(f"マスタ語彙から {len(master_units)} エントリユニット(kind=master)を構築")
        logger.info(f"DB定義書から {len(units)} カラムユニットを構築")
    if file_source is not None or file_manifest is not None:
        from kawasemi_indexer.file_indexer import (
            build_manifest_units,
            build_reference_units,
            read_manifest,
        )

        file_units: list[ResourceUnit] = []
        if file_source is not None:
            file_units.extend(build_reference_units(file_source))
        if file_manifest is not None:
            file_units.extend(build_manifest_units(read_manifest(file_manifest)))
        units_by_kind[Kind.FILE] = file_units
    if fhir_source is not None:
        units = build_fhir_units(_read_fhir_dictionary(fhir_source))
        units_by_kind[Kind.FHIR] = units
        logger.info(f"FHIR臨床項目辞書から {len(units)} ユニットを構築")
    return write_index(units_by_kind, embedder, out_dir, indexer_version)
