"""ステップ3: 確認済み手順書（InputRunbook）→ DSLプログラム（InputPlan.program）のコンパイル

確認済み手順書のステップ列に、データ所在（接続先・表・カラム）と臨床的コンテキスト
（分岐条件の根拠）を突き合わせ、Ollama で限定プリミティブの実行可能 DSL を生成する。

重要な安全方針:
- 接続先・表・カラムが特定できず具体的な SQL を書けない場合、推測で SQL を捏造させない。
  その op は needs_input=true / sql=null とし、不足情報を reasoning に明記させる。
- SQL は SELECT のみ。書込・DDL は生成しない。

op 引数の決定的導出:
  データ所在が索引ユニット由来（unit_id / payload を持つ）なら、表・カラム・患者絞り込み
  キー列・接続ID・FHIRクエリ雛形・FHIRPath・ファイルパスは **payload から決定的に導出**
  し（`resource_index.derive.derive_op_args`）、LLM 出力を検証・是正する。LLM に委ねるのは
  時間窓・並び順・絞り込みといった手順書由来の臨床ロジックだけにする。所在生成側（ステップ1
  役割2）が「LLM は候補番号を選ぶだけ・文字列は payload から組み立てる」方式なので、
  ステップ3も同じ思想を踏襲する。確定に使った索引ユニットは step.unit_id に刻む（典拠の伝播）。
"""

from __future__ import annotations

from typing import Any

from kawasemi.ai_input.design_provenance import DesignLlmRecorder
from kawasemi.ai_input.dsl import (
    UNARY_COMPARE_OPS,
    is_select_only_sql,
    placeholder_names,
    prompt_dollar_names,
    step_schema,
)
from kawasemi.ai_input.llm_json import (
    LARGE_CONTEXT_OPTIONS,
    LLMCallRecorder,
    PartialResultException,
    chat_json_chunked,
    strict_response_format,
)
from kawasemi.ai_input.llm_provider import design_llm_model, make_design_llm_client
from kawasemi.ai_input.models import OpType
from kawasemi.ai_input.resource_index import connections as conn_mod
from kawasemi.ai_input.resource_index import key_types as kt_mod
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry
from kawasemi.ai_input.resource_index.derive import (
    FAMILY_DB,
    FAMILY_FHIR,
    OP_FAMILY,
    DerivedOpArgs,
    derive_op_args_list,
    normalize_fhir_query,
    normalize_path_pattern,
)
from kawasemi.ai_input.resource_index.key_types import KeyTypeCatalog
from kawasemi.ai_input.schemas import (
    MAX_LOCATION_CANDIDATES,
    DSLProgram,
    DSLStep,
    GeneratedPlanItem,
    GeneratedPlans,
)
from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# プロンプトへ出す payload のキー（value_dict / notes 全文などで肥大させない）
_PAYLOAD_DIGEST_KEYS = (
    "connection_id",
    "db_qualified",
    "table_logical",
    "physical_name",
    "logical_name",
    "type_raw",
    "patient_key",
    "key_type",
    "resource_type",
    "query_template",
    "fhir_path",
    "code",
    "file_role",
    "path_pattern",
    "path",
    "extract",
)

# プロンプトへ出す代替候補（fallback）の最大件数。num_ctx を溢れさせない。
#
# **保存できる候補数の上限から導く**。ここが保存側より小さいと、保存はできる
# のにプロンプトに現れない候補ができる（記録と取得計画が食い違う）。主候補1件を差し引く。
_MAX_ALT_CANDIDATES = MAX_LOCATION_CANDIDATES - 1

# 1コールへ詰める項目数。
#
# **既定の `DEFAULT_CHUNK_SIZE`（4）では `LARGE_CONTEXT_OPTIONS` の 32768 にも収まらない。**
# コンパイラのプロンプトは1項目あたり約3,058文字（所在候補3件ぶんの確定済み op 引数・
# 必須識別子・payload 要約と、確認済み手順書ステップ）で、固定部が 15,393 文字ある。
# 出力側は分岐を含む長いプランでも溢れないよう、1項目 3,000 文字を予約する。
#
#     15,393 + 3,058×2 + 3,000×2 = 27,509 <= 32,768   （幅3 だと 33,599 で超える）
#
# 手順書生成が `RUNBOOK_CHUNK_SIZE` を自前で持っているのと同じ理由で、コンパイラも
# 自前の刻み幅を持つ。**測定に効く**（打ち切りの位置が未試行件数を決める）ので、
# 変えるときは測定条件の変更として扱う。
COMPILE_CHUNK_SIZE = 2

# structured output で**モデルに省略を許さないキー**（判定 op の `answer` と同型）。
#
# `DSLProgram` は `params` / `steps` / `return` のすべてに既定値があるため、
# `model_json_schema()` の `required` が **`None`** になる。モデルが
# `{"link_id":"x","program":{}}` を返すと**空プログラムが黙って通る**。
#
# **`DSLStep` の op 固有引数は入れない**——op ごとにキーが変わるので**正当な省略**である。
# 必須にするのは **op に依らない一様なキー**だけ。
_REQUIRED_PLAN_KEYS = ("params", "steps", "return")


def _plan_response_format() -> dict[str, Any]:
    """structured output のスキーマ。**step を op ごとの合併型へ差し替える**。

    `strict_response_format` は名前を挙げたキーを必須にするだけなので、`DSLStep` の
    37フィールドはすべて省略可のままだった。その結果、**`compare` が `left` 無しで通り**、
    項目の過半が `validate_program` で落ちた。

    **後段の是正では直せない。** `_repair_op_args` が救えるのは取得系（`sql_extract` /
    `fhir_extract` / `file_extract`）だけで、索引の payload から引数を焼き込めるからである。
    `compare` / `branch` / `transform` は**何と何を比べるべきかが索引に無い**ので、
    救済経路が原理的に書けない。是正が走ったうえでも多くの項目が落ちていた。

    **プロンプトを強めるのも効かない。** 現行のプロンプトには `{op,id,left,cmp,right}` と
    書いてあり、それでも従わなかった。残る経路は文法で強制することだけである。

    `dsl.step_schema()` が `_REQUIRED_FIELDS` から variant を導くので、**スキーマと検証の
    強さがずれない**（ずれを機械で止める契約テストを `test_plan_schema_contract` 側に置いた）。
    """
    schema = strict_response_format(GeneratedPlans, *_REQUIRED_PLAN_KEYS)
    defs = schema.get("$defs") or {}
    program = defs.get("DSLProgram")
    if program is None:  # 受け皿の形が変わったら黙って緩めない
        raise ValueError(
            "GeneratedPlans のスキーマに DSLProgram が無い（step スキーマの差し替え先）"
        )
    program["properties"]["steps"]["items"] = step_schema()
    # 参照が無くなった定義は落とす（プロンプトへ載る JSON を無駄に太らせない）
    defs.pop("DSLStep", None)
    return schema


COMPILE_SYSTEM_PROMPT = """あなたはFHIR準拠EDCのAIコンパイラです。人間が確認済みの「データ取得手順書」を、
実行可能な**限定DSLプログラム**へ変換します。各入力項目について program を1つ生成してください。

# DSLの構造
program = { "params": [ ...引数宣言... ], "steps": [ ...命令(op)... ], "return": "$id" }
- steps は上から順に実行される線形リスト。各 step は一意の id を持ち、結果は id に束縛される。
- 後続の step は "$id" で前の step の結果を参照できる（前方参照のみ。後方参照は禁止）。
- return には、最終的にこの項目の値として採用する step の id を "$id" 形式で指定する。
- どの op にも任意で "guard" を付けられる（下記「条件付き実行」参照）。
- params は省略可。patient_id 以外の値を SQL・FHIRクエリ・ファイルパスへ差し込むときだけ宣言する。

# params（プログラム引数）
宣言: {"name":"名前", "kind":"value"|"identifier", "value": リテラル or "$前段id", "allowed":[許容値...]}
- 組み込みパラメータ patient_id は**宣言せずに**参照できる（再宣言は禁止）。
  study_id もあるがバッチ実行でしか供給されないので、通常は使わないこと。
- kind="value"（既定）: 値として使う。SQL では `:name` のバインドで参照する。
- kind="identifier": 表名・カラム名として SQL のテキストへ `{{name}}` で展開する。
  値は英数字とアンダースコアのみ。**allowed に許容表名を必ず列挙すること（省略すると検証エラー）。**
- value に "$前段id" を書くと、前段 step の取得結果を引数にできる。
  これにより「まず区分値を取得し、その値で参照テーブルや FHIR の code= を組み立てる」ができる。
- 参照の書き方（**params の参照**）: SQL は値=`:name` / 識別子=`{{name}}`、
  fhir_query・path_pattern・path・prompt は `{{name}}`。
- 参照の書き方（**前段 step の値の参照**）: prompt では `$id`（`{{id}}` は互換のため
  受理されるが使わない）。prompt 以外のスロットへ差し込むときは params に
  `{"name":"...", "value":"$id"}` を宣言して、その name を参照する。
- **宣言していない名前を参照してはいけない。** SQL・fhir_query・path_pattern・path と
  prompt の `{{name}}` は検証エラーになる（prompt の `$未知名` だけは自然文の字面と
  区別できないため黙認されるが、書いても解決されないので使わない）。値は必ずサニタイズされ、
  区切り文字や記号を含む値はその step が失敗する（パスやクエリを組み立て直すことはできない）。

例（区分値で参照テーブルを切り替える）:
  {"params":[{"name":"ref_tbl","kind":"identifier","value":"$kubun","allowed":["labs_a","labs_b"]}],
   "steps":[{"op":"sql_extract","id":"kubun","sql":"SELECT tbl FROM ref_map WHERE patient_id = :patient_id"},
            {"op":"sql_extract","id":"v","sql":"SELECT v FROM {{ref_tbl}} WHERE patient_id = :patient_id"}],
   "return":"$v"}

# 使用可能な op（これ以外は使わない）
- sql_extract:  {op,id,sql,multi?}  自アプリDBからSELECTで取得。SQLは必ずSELECTのみ。
                プレースホルダは :patient_id（患者ID）と params の :name。表名の切替は {{name}}（kind=identifier）。
                multi=true で複数行リスト。
- fhir_extract: {op,id,fhir_query,fhir_path,multi?}  FHIRサーバ取得。{{patient_id}} と params の {{name}} を差し込める。
                例 fhir_query="/Observation?patient={{patient_id}}&code={{loinc}}", fhir_path="Observation.valueQuantity.value"
                fhir_path は FHIRPath ではない。"." 区切りの辿りと "name[0]" 形式の整数添字のみ対応。
                where()/ofType()/as/| 等の FHIRPath 構文は解釈されないので使わない。
                絞り込みは fhir_query の検索パラメータで表現する。
- external_db_extract: {op,id,sql,connection_id}  外部DBからSELECTで取得。connection_id は接続レジストリの接続ID。
- file_extract: {op,id,path_pattern,connection_id}  患者ごとの外部ファイル（PDF/txt等）を読みテキストを返す。
                path_pattern は接続の root_path 起点の相対パスで {{patient_id}} を含む
                （例 "{{patient_id}}/pathology.txt"）。叙述からの値の取り出しは後続の llm_extract で行う。
- llm_extract:  {op,id,prompt}  自由記述テキスト等から値を抽出。prompt内で {{patient_id}}・{{params名}}・$他id を参照可。
- llm_judge:    {op,id,prompt}  真偽を判定する。**分類には使えない**（システムが応答を
                yes / no / unknown に閉じ、真偽か「判定不能」に正規化する）。prompt は
                **はい・いいえで答えられる問い**にすること（「〜か？」）。区分や値が欲しい
                ときは llm_extract を使う。
- compare:      {op,id,left,cmp,right}  比較しboolを返す。cmp は < <= > >= == != in contains
                is_empty is_not_empty。left/right は "$id" 参照かリテラル。
                **is_empty / is_not_empty は right を取らない**（左辺が空かどうかだけを見る）。
                空とは「値なし・空文字・0件」。前段の取得が空振りしたかの判定に使う。
- branch:       {op,id,when,then,else_branch}  when が真なら then、偽なら else_branch を値とする。
                when/then/else_branch は "$id" 参照かリテラル（null可）。
- transform:    {op,id,input,rule}  値を変換。rule は
                {"type":"code_map","map":{...}} か {"type":"unit_conversion","formula":"value/18.01","decimals":2}。
- select:       {op,id,source,pick}  multi抽出の結果($id)から1件選ぶ。pick は first / last。
                絞り込み条件や並び順は select では実行されない（where/order は非実行のヒント）。
                必要な絞り込み・並び順は抽出側の SQL（WHERE / ORDER BY）やFHIRクエリで表現する。
- aggregate:    {op,id,source,func}  multi抽出の結果($id)を集計。func は count / min / max / avg / first。

# 条件付き実行（guard）
どの op にも任意フィールド "guard" を付けられる。guard が真のときだけその step を実行し、
偽なら**実行せずスキップ**する（外部DB・FHIR・ファイルへのアクセスも LLM 呼び出しも行わない）。
- guard は "$id" 参照（前段の compare / llm_judge の結果など。前方参照のみ）か true/false のみ。
  文字列リテラル（例 "is_inpatient"）は使えない。参照は必ず "$" を付ける（付け忘れは検証エラー）。
- **guard 付き step の結果を使う後続 step には同じ guard を付ける**。付けないと、未実行で空に
  なった値のまま後続が実行され（例: 空データで llm_extract が呼ばれ）、無駄な呼び出しと
  誤った値が生じる。
- 「条件Aなら表X、条件Bなら文書Y」のように取得元が文脈で変わる場合は、判定 step を先に置き、
  各取得 step に guard を付けて**採用する側だけを実行**する。最後に branch で値を選ぶ。
- guard と branch の役割は違う。guard =「実行するか否か」、branch =「計算済みの値のどちらを採るか」。
  branch だけで書くと両方の取得が実行され、不要な患者データ取得が起きる。必ず guard を併用する。
- 例（入院患者は外部DB、外来患者はFHIRから取得する）:
  {"op":"llm_judge","id":"is_inpatient","prompt":"患者 {{patient_id}} は入院中か"}
  {"op":"external_db_extract","id":"a","guard":"$is_inpatient","connection_id":"hosp","sql":"SELECT ..."}
  {"op":"compare","id":"is_outpatient","left":"$is_inpatient","cmp":"==","right":false}
  {"op":"fhir_extract","id":"b","guard":"$is_outpatient","fhir_query":"/Observation?patient={{patient_id}}"}
  {"op":"branch","id":"v","when":"$is_inpatient","then":"$a","else_branch":"$b"}

# フォールバック（所在候補が複数あるとき）
データ所在に**優先順の候補が複数**示されている場合は、「まず第1候補から取り、空なら第2候補」
という取得計画にする。文脈で取得元が決まる上の例とは違い、**取ってみるまでどちらになるか
分からない**のがフォールバックである。
- 定型: 取得 → is_not_empty で空振り判定 → その否定 → guard 付きで次の候補を取得 → branch で選ぶ。
- **必ず guard を付ける**。付けないと第1候補が成功しても第2候補の患者データ取得が走る。
- **各取得 step には、その step が使う候補の「確定済みop引数」を書くこと。**「代替の所在候補2」
  以降にも、それぞれの確定済み引数が示されている。第1候補の引数を第2候補の step にも書くと
  「Aを試して、空ならAをもう一度試す」計画になるので、システムがその step を
  needs_input=true にして人へ回す（典拠も刻まれない）。引数が示されていない候補は推測で
  書かず、その step を needs_input=true にする。
- **候補は示された順に試すこと**（配列順が優先順である）。第1候補より先に代替候補を試す
  計画にすると、システムがその step を needs_input=true にして人へ回す。両方に値がある
  被験者では、先に試した側の値が採用されてしまうためである。
- branch の when には**必ず is_not_empty の結果**（"$has_a"）を置く。取得結果そのもの（"$a"）を
  置いてはいけない。branch の真偽化は guard と規則が違い、0件（[]）を真、文字列 "0" を偽と
  みなすため、取り直した値が捨てられる。
- 例（FHIR にあればそれを使い、無ければ院内DBから取る）:
  {"op":"fhir_extract","id":"a","fhir_query":"/Observation?patient={{patient_id}}&code=1234-5"}
  {"op":"compare","id":"has_a","left":"$a","cmp":"is_not_empty"}
  {"op":"compare","id":"no_a","left":"$has_a","cmp":"==","right":false}
  {"op":"sql_extract","id":"b","guard":"$no_a","sql":"SELECT v FROM t WHERE patient_id = :patient_id"}
  {"op":"branch","id":"v","when":"$has_a","then":"$a","else_branch":"$b"}
- 候補が3つ以上あるときは、**2段目以降の判定 step にも同じ guard を付ける**。付け忘れると、
  skip された step の値は null になるので判定が「空だった」と出て、**第1候補が成功しているのに
  第3候補の患者データ取得が走る**。branch は入れ子にする。
- 例（FHIR → 院内DB → 文書、の順に試す）:
  {"op":"fhir_extract","id":"a","fhir_query":"/Observation?patient={{patient_id}}&code=1234-5"}
  {"op":"compare","id":"has_a","left":"$a","cmp":"is_not_empty"}
  {"op":"compare","id":"no_a","left":"$has_a","cmp":"==","right":false}
  {"op":"sql_extract","id":"b","guard":"$no_a","sql":"SELECT v FROM t WHERE patient_id = :patient_id"}
  {"op":"compare","id":"has_b","guard":"$no_a","left":"$b","cmp":"is_not_empty"}
  {"op":"compare","id":"no_b","guard":"$no_a","left":"$has_b","cmp":"==","right":false}
  {"op":"file_extract","id":"c","guard":"$no_b","connection_id":"docs","path_pattern":"{{patient_id}}/report.txt"}
  {"op":"branch","id":"v2","when":"$has_b","then":"$b","else_branch":"$c"}
  {"op":"branch","id":"v","when":"$has_a","then":"$a","else_branch":"$v2"}

# 厳守事項
1. 接続先・表・カラムが手順書とデータ所在から特定できず、具体的なSQLを書けない場合は、
   **推測でSQLを書かないこと**。その step は "needs_input": true とし、"sql": null、
   "reasoning" に不足している情報（例「検査結果テーブル名が不明」）を日本語で書く。
   fhir_query が定まらない fhir_extract も同様に needs_input=true とする。
2. SQLは必ずSELECT文のみ。INSERT/UPDATE/DELETE/DDL は絶対に生成しない。
3. 手順書のステップ構造をできるだけ尊重し、1手順≒1op に分解する。
   起点取得→絞り込み(select)→比較(compare)→分岐(branch)→変換(transform) のように並べる。
   取得元が条件で分かれるときは、判定(llm_judge/compare)→guard 付き取得→branch の順に並べる。
   **手順書のステップには種別のタグが付いている**（`[fetch]` `[condition]` `[branch]` 等）。
   `[condition]` は判定 step（llm_judge / compare）へ、`[branch]` は guard の付与と branch へ
   写す。`[branch]` が「空なら次の候補」と書いているならフォールバックの定型（上記）で、
   「文脈でどちらか」と書いているなら条件付き実行の定型（上記）で書く。
   **データ所在に優先順の候補が複数示されている**なら、手順書が1候補しか書いていなくても
   フォールバックの定型（上記）で書く。候補の配列順が優先順である。
4. manual 中心で自動化できない項目は、空の steps と return:null を返してよい。
5. 必ず全ての項目を plans 配列に含めること。
6. 項目に「確定済みop引数」が示されている場合は、その op を使い、示された引数
   （sql / connection_id / fhir_query / fhir_path / path_pattern）を**そのまま**使うこと。
   これらは施設のリソース索引から決定的に導出された値で、システム側が最終確定する
   （あなたが書き換えても上書きされる）。あなたが足してよいのは手順書由来の臨床ロジック
   （時間窓のWHERE条件・ORDER BY・LIMIT・並び順・絞り込み）だけである。
   その際も「必須識別子」に挙がった表名・カラム名・:patient_id・コードは必ず残すこと
   （欠けるとシステムが決定的な引数で丸ごと上書きする）。
7. 「不足」が示された項目は、決定的に引数を組み立てられない要素があるということなので、
   その取得 step は needs_input=true とし、reasoning に不足内容を日本語で書くこと。
8. "unit_id" は出力しないこと（所在の典拠はシステムが刻む）。
9. params は必要なときだけ宣言する。patient_id だけで足りるなら params は空でよい。
   kind="identifier" を使うときは、手順書・データ所在から**実在が確認できた表名だけ**を
   allowed に列挙する（allowed の省略は検証エラー）。候補が特定できないなら推測で書かず
   needs_input=true とする。"""


def candidates_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    """コンパイラ入力1項目から、データ所在候補の配列（優先順）を取り出す。

    新形式は item["candidates"]（SystemLocation.candidate_list 相当）。単一所在を
    平坦に持つ旧形式（source_category/location/unit_id/connection_id/payload）も
    1候補として受理する（既存呼び出し・テストの後方互換）。
    """
    cands = item.get("candidates")
    if isinstance(cands, list):
        return [c for c in cands if isinstance(c, dict)]
    flat = {
        "source_category": item.get("source_category"),
        "location": item.get("location"),
        "unit_id": item.get("unit_id"),
        "connection_id": item.get("connection_id"),
        "payload": item.get("payload"),
    }
    return [flat] if any(v for k, v in flat.items() if k != "source_category") else []


def _payload_digest(payload: dict[str, Any] | None) -> str:
    """payload の要点を1行に整形する（プロンプト提示用。肥大するキーは出さない）。"""
    if not payload:
        return ""
    parts = [f"{k}={payload[k]}" for k in _PAYLOAD_DIGEST_KEYS if payload.get(k) is not None]
    notes = payload.get("notes")
    if notes:
        parts.append(f"notes={str(notes)[:80]}")
    return ", ".join(parts)


def _derived_args_line(spec: DerivedOpArgs) -> str:
    """確定済み op 引数を1行に整形する。"""
    args = ", ".join(f"{k}={v}" for k, v in spec.args.items())
    return f"op={spec.op}" + (f", {args}" if args else "")


def _location_lines(item: dict[str, Any], derived: list[DerivedOpArgs]) -> list[str]:
    """データ所在の構造化情報（索引ユニット・確定済みop引数・代替候補）を提示する。"""
    lines: list[str] = []
    candidates = candidates_of(item)
    primary = candidates[0] if candidates else {}
    if primary.get("location"):
        lines.append(f"  データ所在（接続先/表/カラム）: {primary['location']}")
    elif item.get("location"):
        lines.append(f"  データ所在（接続先/表/カラム）: {item['location']}")
    if primary.get("unit_id") or primary.get("connection_id"):
        lines.append(
            f"  索引ユニット（所在の典拠）: unit_id={primary.get('unit_id') or '（なし）'}"
            f" / 接続ID={primary.get('connection_id') or '（なし）'}"
        )
    digest = _payload_digest(primary.get("payload"))
    if digest:
        lines.append(f"  索引の構造化情報(payload): {digest}")
    if derived:
        # 主たる所在が索引由来でない場合（手入力候補が先頭など）もありうるので、
        # 確定済み引数はそれを導出した候補の unit_id とともに提示する。
        spec = derived[0]
        if spec.args:
            lines.append(
                "  確定済みop引数（システムが最終確定。推測で書き換えないこと。"
                f"典拠 unit_id={spec.unit_id or '（なし）'}）:"
            )
            lines.append(f"    {_derived_args_line(spec)}")
        if spec.required_tokens:
            tokens = " / ".join(t.label for t in spec.required_tokens)
            lines.append(f"  必須識別子（生成するSQL/クエリに必ず残すこと）: {tokens}")
        if spec.key_type:
            # 書き方の指示は op の族ごとに変える。FHIR クエリとファイルパスに `:patient_id` は
            # 無い（`{{patient_id}}` で埋まる）。DB の文言は従来どおり
            how = (
                "SQLでは :patient_id を使う"
                if spec.family == FAMILY_DB
                else "クエリ・パスの {{patient_id}} がこの種別の値で埋まる"
            )
            lines.append(
                f"  患者キー種別: {spec.key_type}（実行時に被験者IDから解決される。{how}）"
            )
        if spec.missing:
            lines.append(f"  不足（needs_input=true にすること）: {' / '.join(spec.missing)}")
        # 代替候補にも、フォールバックの取得 step を具体的に書けるだけの情報を出す。要約
        # だけだと LLM は第2候補の引数を書けず、厳守事項1に従って needs_input=true を返す
        # （どの候補も確定しないので fallback 取得計画が成立しない）。
        for i, alt in enumerate(derived[1 : 1 + _MAX_ALT_CANDIDATES], start=2):
            lines.append(
                f"  代替の所在候補{i}: {alt.location or alt.unit_id or ''}"
                f"（unit_id={alt.unit_id or '（なし）'}, op={alt.op}）"
            )
            if alt.args:
                lines.append(
                    "    確定済みop引数（この候補から取る step にはこれをそのまま使う。"
                    f"典拠 unit_id={alt.unit_id or '（なし）'}）:"
                )
                lines.append(f"      {_derived_args_line(alt)}")
            if alt.required_tokens:
                tokens = " / ".join(t.label for t in alt.required_tokens)
                lines.append(f"    必須識別子（この候補の step に必ず残すこと）: {tokens}")
            if alt.key_type:
                lines.append(f"    患者キー種別: {alt.key_type}")
            if alt.missing:
                lines.append(
                    f"    不足（この候補を使う step は needs_input=true）: {' / '.join(alt.missing)}"
                )
    return lines


# 索引由来の引数を焼き込まず人へ回す理由（reasoning と fixes の両方に残す）。
# {arg} には確定できなかった引数の名（SQL / FHIRクエリ / ファイルパス）が入る。
_BLOCK_REASONS = {
    "候補重複": (
        "同じ所在候補を先行 step が使用済みのため、この step の{arg}は確定しない"
        "（意味の異なる step が同値になるのを避ける。取得条件の違い、あるいはどの候補から"
        "取るかを人が補完する）"
    ),
    "空振り判定の向き不明": (
        "guard が所在候補の空振り判定に依存しているが、その向き（空だったときに走るのか、"
        "値があるときに走るのか）を判定できないため、この step の{arg}は確定しない"
        "（`has_a == false` のような定型で書き直すか、人が確定する）"
    ),
    "guard無しの複数候補取得": (
        "同じ項目の所在候補に対応する取得 step が2つ以上あり、そのいずれにも guard が"
        "無いため、この step の{arg}は確定しない（**両方が無条件に実行される**ので step の"
        "並び順は優先順を表さず、どちらの値を採るかは branch の書き方次第で優先順と逆に"
        "なりうる。フォールバックは「前段が空だったら走る」guard で書くか、人が確定する）"
    ),
    "優先順の逆転": (
        "所在候補の優先順（候補の配列順）を後戻りする計画のため、この step の{arg}は"
        "確定しない（第1候補より先に代替候補を試す形になっている。どの候補を先に試すかを"
        "人が確定する）"
    ),
}


def _ref_id(value: Any) -> str | None:
    """`"$id"` 形式なら id を返す（リテラルなら None）。"""
    return value[1:] if isinstance(value, str) and value.startswith("$") else None


def _unary_compare_ids(program: DSLProgram) -> set[str]:
    """空判定（`is_empty` / `is_not_empty`）を行う compare step の id。"""
    return {s.id for s in program.steps if s.op == OpType.COMPARE and s.cmp in UNARY_COMPARE_OPS}


def _literal_truth(value: Any) -> bool | None:
    """比較の右辺リテラルを真偽として読む（読めなければ None）。

    `_loose_eq`（executor）は数値へ落として比べるので、`has_a == 0` / `== "0"` / `!= 1` は
    実行時には `== false` / `!= true` と**同じに動く**。ここで真偽リテラルだけを読むと、
    同じ意味の別表記で極性が読めず検査が外れる。
    """
    if isinstance(value, bool):
        return value
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    # `_loose_eq` は Python の真偽値を 1.0 / 0.0 として数値比較する。したがって実行時に
    # 真偽と一致するのは 0 と 1 だけである。`2` や `-1` は**常に偽**（`==`）／**常に真**
    # （`!=`）になるので、真偽として読んではいけない（読むと、常に走る guard を
    # 「空だったときに走る」と誤読して逆転・結線の検査を通してしまう）。
    if number == 0.0:
        return False
    if number == 1.0:
        return True
    return None


def _same_emptiness_polarity(
    ref: str | None, tester: DSLStep, by_id: dict[str, DSLStep]
) -> bool | None:
    """`$ref` が真のとき、空判定 `tester` が真か偽かを返す（無関係なら None）。

    `tester` 自身への参照だけでなく、**同じ値を見る別の空判定**（`is_not_empty($a)` に対する
    `is_empty($a)`）も読む。定型は「その否定」を `has_a == false` と書くが、`is_empty($a)` を
    guard に直結する短い書き方も検証を通る（`docs/resource_index_guide.md` が正しい形として
    認めている）。ここを片方だけ読むと、書き方の違いで検査が丸ごと切れる。
    """
    if ref is None:
        return None
    if ref == tester.id:
        return True
    other = by_id.get(ref)
    if other is None or other.op != OpType.COMPARE or other.cmp not in UNARY_COMPARE_OPS:
        return None
    if _ref_id(other.left) != _ref_id(tester.left) or _ref_id(tester.left) is None:
        return None
    return other.cmp == tester.cmp


def _guard_polarity(
    guard: Any, tester: DSLStep, program: DSLProgram, closure: dict[str, set[str]]
) -> bool | None:
    """`guard` が発火するときの空判定 `tester` の真偽を返す（判定できなければ None）。

    読む形は3つ。`guard: "$has_a"`（空判定を直に参照、または同じ値を見る別の空判定）、
    `guard: "$no_a"` で `no_a` が `has_a == false` / `false == has_a`、および `!=` 版。
    """
    by_id = {s.id: s for s in program.steps}
    ref = _ref_id(guard)
    if ref is None:
        return None
    direct = _same_emptiness_polarity(ref, tester, by_id)
    if direct is not None:
        return direct
    step = by_id.get(ref)
    if step is None or step.op != OpType.COMPARE or step.cmp not in ("==", "!="):
        return None
    for one, other in ((step.left, step.right), (step.right, step.left)):
        inner = _same_emptiness_polarity(_ref_id(one), tester, by_id)
        truth = _literal_truth(other)
        if inner is None or truth is None:
            continue
        # guard は「参照先 == other」（!= なら反転）のときに発火する。
        fires_when_ref_is = truth if step.cmp == "==" else not truth
        return fires_when_ref_is if inner else not fires_when_ref_is
    return None


def _emptiness_gated_fetches(
    guard: Any,
    program: DSLProgram,
    closure: dict[str, set[str]],
    fetch_ids: set[str],
) -> set[str]:
    """`guard` が「空だった」と判定した取得 step の id を返す。

    これがフォールバックの判別である。`guard` の参照先に空判定（`is_empty` /
    `is_not_empty`）があり、**その空判定が「空だった」を意味する向きで参照されている**とき、
    左辺が辿る取得 step が「前の試行」にあたる。

    `guard` が前段の取得値に依存していても、空判定を経ていなければフォールバックではない
    （例: 検査値が閾値以上かで取得元を変える条件付き実行、区分値で参照表を切り替える形）。
    **向きも見る。** `guard: "$has_consent"`（非空のときに走るゲート）を「前段が空だったから
    走る試行」と数えると、フォールバックでない正当なプランが候補の配列順だけで人の手待ちに
    なる。ここを区別せずに弾くと、プロンプトの厳守事項3（判定 → guard 付き取得 → branch）に
    従って書かれた正当なプランが止まる。
    """
    by_id = {s.id: s for s in program.steps}
    gated: set[str] = set()
    for uid in _reachable(_ref_id(guard), closure) & _unary_compare_ids(program):
        tester = by_id[uid]
        if _guard_polarity(guard, tester, program, closure) is not (tester.cmp == "is_empty"):
            continue
        gated |= _reachable(_ref_id(tester.left), closure) & fetch_ids
    return gated


def _resolve_emptiness_condition(
    value: Any,
    program: DSLProgram,
    closure: dict[str, set[str]],
    candidate_fetches: set[str],
) -> tuple[DSLStep, bool] | None:
    """`value` が依存している空判定と、`value` が真のときのその空判定の真偽を返す。

    `$has_a`（直参照）だけでなく `$no_a`（`has_a == false`）や `$yes_a`
    （`has_a == true`）も読む。候補の空判定に依存していない値なら None。
    """
    # 走査順を program の並びで固定する（集合の走査順に依存させない）。
    for tester in program.steps:
        if tester.id not in _reachable(_ref_id(value), closure) & _unary_compare_ids(program):
            continue
        if not (_reachable(_ref_id(tester.left), closure) & candidate_fetches):
            continue
        polarity = _guard_polarity(value, tester, program, closure)
        if polarity is not None:
            return tester, polarity
    return None


def _branch_is_truth_paraphrase(step: DSLStep) -> bool:
    """`branch` が「値の選択」ではなく**真偽の言い換え**か。

    判定は「**両側がリテラル**（`$id` 参照でない）」だけで行う。両側がリテラルなら取得した値を
    運んでいないので、`when` の真偽を符号化して返しているだけである（`then: false, else: true`
    は否定、`then: 0, else: "該当なし"` も `_truthy` で False / True なので否定、
    `then: "1", else: "2"` は後段の `== "2"` で否定として使える）。

    真偽が同じ側どうしでも辿る。後段の比較で極性が取り出せるので、辿らないと
    「読めないのに止まらない」隙間になる。値を運ぶ `branch`（片側が `$id` 参照）は境界の
    ままなので、取れた値の大小で分ける後段の条件は巻き込まない。
    """
    return _ref_id(step.then) is None and _ref_id(step.else_branch) is None


def _condition_chain(value: Any, by_id: dict[str, DSLStep]) -> set[str]:
    """`value` から**真偽の言い換えを辿って**到達する step の id。

    `branch` は原則として境界にする（「値を選ぶ」ので、そこから先は条件の言い換えではなく
    値の系列になる）。**ただし両側がリテラル（`$id` 参照でない）の `branch` は取得した値を
    運んでいない**ので、`when` の真偽を符号化しているだけとみなして `when` を辿る
    （`branch(when=$has_a, then=false, else=true)` は `has_a == false` と同じ意味で、実行時も
    同じに動く）。この境界で「否定を重ねた条件」と「取れた値を使う後段の条件」を分けられる。
    """
    start = _ref_id(value)
    if start is None:
        return set()
    seen: set[str] = set()
    stack = [start]
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        step = by_id.get(sid)
        if step is None:
            continue
        if step.op == OpType.BRANCH:
            # `branch` は原則として境界にする（取れた値を使う後段の条件まで辿らないため）。
            # ただし**両側がリテラル**の `branch` は取得した値を運んでいないので、`when` の
            # 真偽を符号化しているだけとみなして `when` を辿る（`branch(when=$has_a,
            # then=false, else=true)` は `has_a == false` と同じ意味で、実行時も同じに動く）。
            if _branch_is_truth_paraphrase(step):
                stack.extend(r for r in _value_refs(step) if r == _ref_id(step.when))
            continue
        stack.extend(_value_refs(step))
    return seen


def _has_gated_alternative(
    value: Any,
    program: DSLProgram,
    closure: dict[str, set[str]],
    candidate_fetches: set[str],
) -> bool:
    """`value` が見ている空判定に `guard` で依存している**別の候補の取得**があるか。

    フォールバックかどうかの門である（極性が読めない場合に使う。読めるときは
    `_enforce_branch_routing` が極性で代替候補を特定する）。
    """
    by_id = {s.id: s for s in program.steps}
    for uid in _condition_chain(value, by_id) & _unary_compare_ids(program):
        tested_reach = _reachable(_ref_id(by_id[uid].left), closure)
        if not (tested_reach & candidate_fetches):
            continue
        for fid in candidate_fetches - tested_reach:
            if uid in _condition_chain(by_id[fid].guard, by_id):
                return True
    return False


def _has_unreadable_emptiness(
    value: Any,
    program: DSLProgram,
    closure: dict[str, set[str]],
    candidate_fetches: set[str],
) -> bool:
    """`value` が候補の空判定に依存しているのに、向きを判定できないか。

    **判定できないことを「検査に通った」と扱わない。** 極性の読み取りは字面の列挙なので
    （`== false` / `!= true` / 同じ値を見る別の空判定）、列挙外の同義表記（`== 0` など。
    実行時には `_loose_eq` が同じに扱う）では None になる。そこで検査を飛ばすと、
    フォールバックとして正常に動くプランが2つの検査のどちらも通らずに確定してしまう。
    `resource_index/derive.py` の `matches` と同じ方針（fail-closed）で人へ回す。
    """
    if _resolve_emptiness_condition(value, program, closure, candidate_fetches) is not None:
        return False  # ひとつでも向きが読めれば、それが判定の根拠になる
    # **`branch` を境界に推移的に辿る。** 1段だけだと、空判定を2段以上の真偽の言い換えで
    # 包んだ形（`(has_a == false) == false`、`llm_judge` を2段挟む形）が「読めないのに
    # 読めない扱いにもならない」隙間になる。一方で `branch` を越えて辿ると、取れた値の
    # 大小で分ける後段の条件（`compare($v, ">", 100)`。`$v` は `branch`）まで巻き込む。
    # 真偽の言い換えに `branch` を使う形は「両側がリテラル」（取得した値を運んでいない）で
    # 見分ける。そこだけ `when` を辿ることで両者を分けられる（`_condition_chain` 参照）。
    by_id = {s.id: s for s in program.steps}
    unary_ids = _unary_compare_ids(program)
    for uid in _condition_chain(value, by_id) & unary_ids:
        if _reachable(_ref_id(by_id[uid].left), closure) & candidate_fetches:
            return True
    return False


def _enforce_branch_routing(
    program: DSLProgram, closure: dict[str, set[str]], candidate_fetches: set[str]
) -> list[str]:
    """フォールバックの `branch` が、取得できた値を捨てていないかを検査する。

    `when` が `is_not_empty(X)` なら `then` 側、`is_empty(X)` なら `else_branch` 側が
    「X に値があるときに採る側」である。検査は2つ。

    1. **X に値があるときに採る側が X へ辿り着くこと。** そうでないと、X に値があるのに
       別の値（フォールバックでは skip されて None）が採られる
    2. **X が空だったときに採る側が、代替候補へ辿り着くこと。** そうでないと、代替候補を
       実際に取得して値が返っているのに、その値が捨てられる（リテラルに置き換わる）

    どちらも項目値が非空になるので `item_failure_code` が付かず、取得可能性の集計では
    **充足として数えられてしまう**。片方だけを検査すると鏡像を見逃す。

    **対象はフォールバックの分岐に限る。** 判別は2つの条件で行う。

    - 空判定の対象が所在候補の取得へ辿り着くこと（候補の値を見ている分岐であること）
    - **「空だったときに走る」代替候補の取得があること。** `guard` の極性で見る。値の有無を
      ゲートに使う分岐（`when: "$has_consent"` で、文書取得の `guard` も `$has_consent`）では
      代替候補は「非空のときに走る」ので対象外になる。ここを候補への到達だけで判別すると、
      プロンプトの厳守事項3（判定 → guard 付き取得 → branch）に従った正当なプランが人の
      手待ちになる（そこでは候補の値は失われていない）
    """
    by_id = {s.id: s for s in program.steps}
    fixes: list[str] = []
    for step in program.steps:
        if step.op != OpType.BRANCH:
            continue
        # `when` は空判定の結果（`$has_a`）だけでなく、その否定（`$no_a`）や真偽ラッパ
        # （`$yes_a`）で書かれることもある。**極性を読んでどちらの側が「値があるとき」かを
        # 決める。** 直参照だけを対象にすると、否定を置いた形で検査が丸ごと飛ぶ。
        resolved = _resolve_emptiness_condition(step.when, program, closure, candidate_fetches)
        if resolved is None:
            # **`guard` 側と同じ扱いにする。** 候補の空判定に依存しているのに向きが読めない
            # ときに検査を飛ばすと、フォールバックとして正常に動くプランが2つの検査の
            # どちらも通らないまま確定する（`when` だけ fail-open にしてはいけない）。
            # 代替候補が1件も無いなら、失われる値が無いので止める理由が無い（結線の検査
            # 本体と同じ門。単一候補のプランで判定の書き方が非定型なだけで人の手待ちに
            # しない）。極性が読めないので、`guard` が同じ空判定に依存しているかで見る。
            if _has_unreadable_emptiness(
                step.when, program, closure, candidate_fetches
            ) and _has_gated_alternative(step.when, program, closure, candidate_fetches):
                step.needs_input = True
                step.reasoning = _note(
                    step.reasoning,
                    "when が所在候補の空振り判定に依存しているが、その向きを判定できないため"
                    "この分岐は確定しない（`$has_a` / `$no_a` のような定型で書き直すか、"
                    "人が確定する）",
                )
                fixes.append(f"{step.id}:needs_input(空振り判定の向き不明)")
            continue
        tester, when_true_means = resolved
        tested = _ref_id(tester.left)
        if tested is None:
            continue
        tested_reach = _reachable(tested, closure)
        # 「空だったときに走る」代替候補（＝フォールバックの後続の試行）。
        empty_side_is_true = tester.cmp == "is_empty"
        alternatives = {
            f
            for f in candidate_fetches - tested_reach
            if _guard_polarity(by_id[f].guard, tester, program, closure) is empty_side_is_true
        }
        if not alternatives:
            continue
        # `when` が真のとき tester は `when_true_means`。値があるとき tester は
        # `not empty_side_is_true` なので、両者が一致するときだけ then が「値があるとき」側。
        positive_is_then = when_true_means is not empty_side_is_true
        positive = step.then if positive_is_then else step.else_branch
        negative = step.else_branch if positive_is_then else step.then
        if tested not in _reachable(_ref_id(positive), closure):
            note = (
                f"空判定が見ている値（${tested}）に値があるとき、この分岐はその値を採らない"
                "ため確定しない（取得できた値が失われる）"
            )
        elif not (_reachable(_ref_id(negative), closure) & alternatives):
            note = (
                f"空判定が見ている値（${tested}）が空のとき、この分岐は代替候補"
                f"（{' / '.join(sorted(alternatives))}）の値を採らないため確定しない"
                "（代替候補を取得しているのにその値が失われる）"
            )
        else:
            continue
        step.needs_input = True
        step.reasoning = _note(
            step.reasoning,
            note + "。しかも項目値が入るので失敗としても記録されない。then と else_branch の"
            "どちらにどの候補を置くかを人が確定する",
        )
        fixes.append(f"{step.id}:needs_input(分岐の結線)")
    return fixes


def _block_fix(step: DSLStep, block: str, corrections: list[str]) -> str:
    """人へ回した step の是正記録。適用した書き換え（op / connection_id）も残す。

    引数を確定しなかったからといって `corrections` を捨てると、`op` や `connection_id` を
    第1候補のもので書き換えた事実が監査証跡から消える（是正一覧に残す方針に反する）。
    """
    return f"{step.id}:" + "・".join([f"needs_input({block})", *corrections])


def _value_refs(step: DSLStep) -> set[str]:
    """step が**値として**参照している前段 step の id（`guard` は含めない）。

    `llm_extract` / `llm_judge` は前段を `prompt` の `$id`（互換の `{{id}}`）で参照する。
    ここを拾わないと「取得 → llm_extract で値を取り出す」という**推奨形**（1候補が複数 step
    になる形）で参照の連鎖が切れ、フォールバックの検査が丸ごと効かなくなる。字句解析は
    検証層・実行時層と同じ `dsl` の共有関数を使う（片方だけが拾う形を作らない）。
    """
    refs: set[str] = set()
    values: list[Any] = [
        step.left,
        step.right,
        step.when,
        step.then,
        step.else_branch,
        step.input,
        step.source,
    ]
    values.extend(c.get("value") for c in (step.match or []) if isinstance(c, dict))
    for value in values:
        if isinstance(value, str) and value.startswith("$"):
            refs.add(value[1:])
    refs |= prompt_dollar_names(step.prompt) | placeholder_names(step.prompt)
    return refs


# params の値をテンプレートへ差し込めるスロット（`{{name}}` で参照する側）。
# `viz._collect_param_refs` と同じ集合である（監査図の依存エッジと検査の連鎖を揃える）。
_PARAM_SLOTS = ("sql", "fhir_query", "path_pattern", "path", "prompt")


def _param_sources(program: DSLProgram) -> dict[str, str]:
    """`params[].value` が `"$id"` のものを {パラメータ名: 参照先 step id} で返す。"""
    sources: dict[str, str] = {}
    for spec in program.params:
        ref = _ref_id(spec.value)
        if spec.name and ref:
            sources[spec.name] = ref
    return sources


def _template_param_names(step: DSLStep) -> set[str]:
    """step のテンプレートが参照している `{{name}}` の集合。"""
    names: set[str] = set()
    for field in _PARAM_SLOTS:
        value = getattr(step, field, None)
        if isinstance(value, str):
            names |= placeholder_names(value)
    for cond in step.match or []:
        if isinstance(cond, dict) and isinstance(cond.get("value"), str):
            names |= placeholder_names(cond["value"])
    return names


def _value_closure(program: DSLProgram) -> dict[str, set[str]]:
    """各 step が値として（推移的に）参照している step の id を返す（`guard` 経由は含めない）。

    フォールバックでは、次の候補の `guard` は「前段の取得が空だったか」を見る compare を
    参照する（`$no_a` → `$has_a` → `$a`）。この閉包を辿れば「その step が走る前に試した
    候補」が分かる。

    **不動点で組む。** この関数は `validate_program` より前（`enforce_derived_op_args`）で
    使うので、「参照は前方のみ」（後方参照は検証が弾く）をこの時点では前提にできない。
    1パスだと後方参照を持つ program で連鎖が切れ、検査が黙って無効になる。
    """
    # **`params[].value` の `$id` も辿る**。プロンプトは params の value に
    # `"$前段id"` を書くことを明示的に許可している（`COMPILE_SYSTEM_PROMPT`）ので、
    # 「まず区分値を取得し、それで第2候補の FHIR クエリを組み立てる」という形が書ける。
    # ここを拾わないと**その形で連鎖が切れ、2つの検査（候補の優先順・フォールバックの
    # 結線）がどちらも効かない**。step の参照ではなく program 単位の参照なので別に持つ。
    sources = _param_sources(program)
    direct = {
        step.id: _value_refs(step)
        | {sources[name] for name in _template_param_names(step) if name in sources}
        for step in program.steps
    }
    closure = {sid: set(refs) for sid, refs in direct.items()}
    changed = True
    while changed:
        changed = False
        for sid, refs in closure.items():
            grown = set(refs)
            for ref in refs:
                grown |= closure.get(ref, set())
            grown.discard(sid)  # 自己参照（循環）は辿らない
            if grown != refs:
                closure[sid] = grown
                changed = True
    return closure


def _reachable(ref: str | None, closure: dict[str, set[str]]) -> set[str]:
    """`$ref` から値として到達できる step の id（自身を含む）。"""
    if ref is None:
        return set()
    return {ref} | closure.get(ref, set())


def _normalize_generated_text(text: str | None) -> str:
    """生成物（SQL / FHIRクエリ / パス）を、同一性の比較用に正規化する。

    `DerivedOpArgs.matches` は識別子を大文字小文字を無視して照合するので、比較側も
    同じ寛容さにしないと「同じ取得を二度書いた」検出が些細な差（`/OBSERVATION?...`、
    `patient = {{patient_id}}`）で回避できてしまう。空白は**すべて落とす**（畳み込みでは
    `=` の前後に空白を入れる差が残る）。

    これは逐語コピペを捕まえる層であって、同義の書き換え（検索パラメータの並べ替え、
    意味の無い末尾の `&`）まで正規化するものではない。
    """
    return "".join((text or "").split()).lower()


def _note(reasoning: str | None, note: str) -> str:
    """reasoning へ是正内容を追記する（監査証跡として残す）。"""
    base = (reasoning or "").strip()
    return f"{base} / {note}" if base else note


def _generated_identifier_text(step: DSLStep, family: str) -> str | None:
    """検証対象となる生成物（SQL / FHIRクエリ / パスパターン）を返す。"""
    if family == FAMILY_DB:
        return step.sql
    if family == FAMILY_FHIR:
        return step.fhir_query
    return step.path_pattern


def _enforce_db(
    step: DSLStep, spec: DerivedOpArgs, matched: bool, *, block: str | None = None
) -> str | None:
    """DB取得 step を索引由来の引数で是正する。返り値は是正内容（無ければ None）。"""
    corrections: list[str] = []
    if step.op != spec.op:
        # 自アプリDB/外部DB の別は接続 scope から決まる（LLMに委ねない）。識別子系の
        # 書き換えは監査で追えるよう是正一覧に残す。
        corrections.append(f"op→{spec.op}")
    step.op = spec.op
    conn = spec.args.get("connection_id")
    if conn:
        # 既に別の接続IDが書かれていた場合のみ「訂正」として記録する（未設定からの
        # 補完は LLM 出力の否定ではないので是正一覧に載せない）。
        if step.connection_id and step.connection_id != conn:
            corrections.append(f"connection_id→{conn}")
        step.connection_id = conn
    sql_ok = bool(matched and step.sql and is_select_only_sql(step.sql))
    llm_wanted_input = bool(step.needs_input)
    if block:
        step.needs_input = True
        # 索引の識別子を踏んでいない SQL は残さない。捏造SQLを下書きとして残すと、人が
        # needs_input を外したときに「索引に無い表」への SELECT が実行される。
        if not sql_ok:
            step.sql = None
        step.reasoning = _note(step.reasoning, _BLOCK_REASONS[block].format(arg="SQL"))
        return _block_fix(step, block, corrections)
    if spec.is_complete:
        if sql_ok:
            # 索引の表・カラム・:patient_id を踏んでいる → 時間窓・並び順など臨床
            # ロジックを含むLLMのSQLをそのまま採用する。
            return f"{step.id}:{'・'.join(corrections)}" if corrections else None
        step.sql = spec.args["sql"]
        if llm_wanted_input:
            # 導出SQLは WHERE が患者キーのみで ORDER BY / LIMIT を持たない。LLM が
            # 情報不足（時間窓が決まらない等）と判断していたなら、下書きとして提示
            # するに留め needs_input は維持する（複数行の先頭を黙って採らない）。
            step.reasoning = _note(
                step.reasoning,
                f"索引から組み立てた SQL を下書きとして提示（典拠 unit_id={spec.unit_id}）。"
                "取得条件（時間窓・並び順）が未確定のため人の補完が必要",
            )
            return f"{step.id}:sql下書き(needs_input維持)"
        step.needs_input = False
        step.reasoning = _note(
            step.reasoning,
            f"SQLを索引の構造化情報から決定的に組み立て（典拠 unit_id={spec.unit_id}）",
        )
        return f"{step.id}:sql確定"
    # 索引が患者絞り込みキー列を持たない＝WHERE句を決定的に確定できない。推測のSQLを
    # 実行させず人の補完へ回す（識別子を踏んだSQLは下書きとして残す）。
    step.needs_input = True
    if not sql_ok:
        step.sql = None
    step.reasoning = _note(
        step.reasoning,
        f"索引に{'・'.join(spec.missing)}が無く、SQLを決定的に組み立てられない（人の補完が必要）",
    )
    return f"{step.id}:needs_input"


def _enforce_fhir(
    step: DSLStep, spec: DerivedOpArgs, matched: bool, *, block: str | None = None
) -> str | None:
    """FHIR取得 step を索引由来の引数で是正する。"""
    corrections: list[str] = []
    conn = spec.args.get("connection_id")
    if conn:
        if step.connection_id and step.connection_id != conn:
            corrections.append(f"connection_id→{conn}")
        step.connection_id = conn
    path = spec.args.get("fhir_path")
    if path:
        if step.fhir_path and step.fhir_path != path:
            corrections.append(f"fhir_path→{path}")
        step.fhir_path = path  # 値の取り出しパスは索引が持つ識別子
    if block:
        step.needs_input = True
        # 索引の識別子を踏んでいないクエリは残さない（_enforce_db と同じ理由）。
        step.fhir_query = (
            normalize_fhir_query(step.fhir_query) if matched and step.fhir_query else None
        )
        step.reasoning = _note(step.reasoning, _BLOCK_REASONS[block].format(arg="FHIRクエリ"))
        return _block_fix(step, block, corrections)
    if spec.is_complete:
        if matched and step.fhir_query:
            # 先頭 "/" は executor の URL 連結規約（base_url + fhir_query）。
            step.fhir_query = normalize_fhir_query(step.fhir_query)
            return f"{step.id}:{'・'.join(corrections)}" if corrections else None
        step.fhir_query = spec.args["fhir_query"]
        if step.needs_input:
            # 索引のクエリ雛形は絞り込みが患者IDのみで、並び順を持たない（executor は
            # Bundle の先頭リソースを採る）。LLM が情報不足と判断していたなら下書きに
            # 留め、needs_input を維持する（_enforce_db と同じ扱い）。
            step.reasoning = _note(
                step.reasoning,
                f"索引のクエリ雛形を下書きとして提示（典拠 unit_id={spec.unit_id}）。"
                "取得条件（対象の絞り込み・どの1件を採るか）が未確定のため人の補完が必要",
            )
            return f"{step.id}:fhir_query下書き(needs_input維持)"
        step.needs_input = False
        step.reasoning = _note(
            step.reasoning,
            f"FHIRクエリを索引のクエリ雛形から決定的に確定（典拠 unit_id={spec.unit_id}）",
        )
        return f"{step.id}:fhir_query確定"
    step.needs_input = True
    # LLM のクエリは消さず下書きとして残す。索引が患者絞り込みを宣言していない場合、
    # LLM の方が正しい絞り込みを書いていることがある（消すと人が一から書き直す）。
    if step.fhir_query:
        step.fhir_query = normalize_fhir_query(step.fhir_query)
    step.reasoning = _note(
        step.reasoning,
        f"索引に{'・'.join(spec.missing)}が無く、FHIRクエリを決定的に組み立てられない",
    )
    return f"{step.id}:needs_input"


def _enforce_file(
    step: DSLStep, spec: DerivedOpArgs, matched: bool, *, block: str | None = None
) -> str | None:
    """ファイル取得 step を索引のマニフェスト宣言で是正する。"""
    corrections: list[str] = []
    conn = spec.args.get("connection_id")
    if conn:
        if step.connection_id and step.connection_id != conn:
            corrections.append(f"connection_id→{conn}")
        step.connection_id = conn
    if block:
        step.needs_input = True
        # 索引の宣言を踏んでいないパスは残さない（_enforce_db と同じ理由）。
        step.path_pattern = (
            normalize_path_pattern(step.path_pattern) if matched and step.path_pattern else None
        )
        step.reasoning = _note(step.reasoning, _BLOCK_REASONS[block].format(arg="ファイルパス"))
        return _block_fix(step, block, corrections)
    if not spec.is_complete:
        # 索引のパターンをそのまま実行できない（ワイルドカード・`..`）。executor は
        # glob を展開しないため、焼き込むと「該当なし」と誤記録される。人へ回す。
        step.needs_input = True
        if step.path_pattern:
            step.path_pattern = normalize_path_pattern(step.path_pattern)
        step.reasoning = _note(
            step.reasoning,
            f"索引の宣言が{'・'.join(spec.missing)}のため、ファイルパスを確定できない",
        )
        return f"{step.id}:needs_input"
    pattern = spec.args["path_pattern"]
    if matched and step.path_pattern:
        # root_path 起点の相対（先頭 "/" なし）が executor の規約。
        step.path_pattern = normalize_path_pattern(step.path_pattern)
        return f"{step.id}:{'・'.join(corrections)}" if corrections else None
    step.path_pattern = pattern
    step.needs_input = False
    step.reasoning = _note(
        step.reasoning,
        f"ファイルパスを索引のマニフェスト宣言で確定（典拠 unit_id={spec.unit_id}）",
    )
    return f"{step.id}:path_pattern確定"


def _is_unconditional(guard: Any) -> bool:
    """guard が「無条件」か（未設定・空白・真偽値リテラル `true`）。

    `guard: true` はコンパイラのプロンプトが明示的に許可しており、意味は無条件実行である。
    参照（`"$id"`）だけが「前段の結果に依存して走る」を表す。
    """
    if guard is None:
        return True
    if isinstance(guard, bool):
        return guard is True
    if isinstance(guard, str):
        text = guard.strip()
        return not text or text.lower() == "true"
    return False


def _unguarded_multi_candidate_fetches(
    program: DSLProgram, derived: list[DerivedOpArgs]
) -> set[str]:
    """guard を伴わない複数候補の無条件取得の step id を返す。

    条件は2つとも満たすこと:

    1. そのファミリの所在候補が**2件以上**ある（フォールバックが成立しうる項目である）
    2. そのファミリの取得 step が**2つ以上**あり、**いずれにも guard が無い**

    1つでも guard を持つ step があれば「前段の空振りで走る」定型を書こうとしているので
    従来の検査（優先順・結線）に任せる。ここで捕まえるのは**どれも無条件**の形だけである。
    """
    by_family: dict[str, list[str]] = {}
    for step in program.steps:
        family = OP_FAMILY.get(step.op)
        if family is None:
            continue
        by_family.setdefault(family, []).append(step.id)

    flagged: set[str] = set()
    for family, step_ids in by_family.items():
        if len(step_ids) < 2:
            continue
        if len([d for d in derived if d.family == family]) < 2:
            continue
        guards = {s.id: s.guard for s in program.steps if s.id in set(step_ids)}
        if all(_is_unconditional(guards.get(sid)) for sid in step_ids):
            flagged.update(step_ids)
    return flagged


def enforce_derived_op_args(program: DSLProgram, derived: list[DerivedOpArgs]) -> list[str]:
    """LLM が生成した program の取得系 step を索引由来の引数で検証・是正する（in-place）。

    索引由来の所在（payload）が無い項目では何もしない（従来どおり LLM 出力のまま）。

    Args:
        program: LLM 生成の DSLプログラム
        derived: データ所在候補（優先順）から導出した op 引数

    Returns:
        是正した内容の一覧（ログ用。空なら LLM 出力をそのまま採用した）。
    """
    fixes: list[str] = []
    # 典拠として使った候補（同じ候補を2つ以上の step に焼き込むと、意味の異なる step が
    # 同値になるため2件目以降は人へ回す）。フォールバック取得計画は step ごとに別の候補を
    # 引くのが正しい形なので、この保護は全ファミリに掛ける。
    used: set[int] = set()
    # 既に別 step が踏んだ生成物（確定済み引数を両 step へコピペした形の検出に使う）。
    used_texts: set[str] = set()
    # 候補の配列順は優先順である（プロンプトの厳守事項3）。フォールバックで「前段が空
    # だったから走る」step が、**前段より優先順の高い候補**を引いていたら後戻りである。
    # 両候補に値がある被験者では運用者が指定した優先順と逆の値が確定保存されるのに、
    # step ごとの unit_id は「正しく」刻まれ fixes も空なので痕跡がどこにも残らない。
    #
    # 判別は guard で行う（step の並び順では行わない）。guard の無い取得 step は無条件に
    # 実行されるので並び順は優先順を表さず、条件付き取得（相互排他な guard）や
    # 多ソースのプログラムを誤って人へ回してしまう。`derive_item_failure_code` が候補を
    # guard 式で見分けるのと同じ考え方である。
    order = {id(d): i for i, d in enumerate(derived)}
    closure = _value_closure(program)
    # **guard を伴わない複数候補の無条件取得**を先に洗い出す。
    #
    # 優先順の検査（下）も分岐の結線の検査も「プロンプトが指示する定型」——`guard` で
    # 前段の空振りに依存させ、`branch.when` に空判定を置く——を前提にしている。
    # 複数候補を**無条件に**取って `branch` で選ぶ形はどちらの検査にも掛からず、
    # `then` 側に第2候補・`else_branch` 側に第1候補を書いても検出されない。両候補に値が
    # ある被験者では**運用者が指定した優先順と逆の値が確定保存される**のに、`fixes` は空、
    # `unit_id` は step ごとに正しく刻まれ、`needs_input` も立たないので痕跡が残らない。
    #
    # 静的に判定できるのは「同じファミリの候補が2件以上あり、そのファミリの取得 step が
    # 2つ以上あって**いずれにも guard が無い**」形に限る。この形は条件付き実行が「branch だけで
    # 書くと両方の取得が実行され、不要な患者データ取得が起きる」として避けたい形そのもの
    # なので、優先順の話とは独立に人へ回す価値がある。条件付き実行・多ソース・
    # 区分値による表の切替はいずれも guard を持つか別ファミリなので巻き込まない。
    unguarded = _unguarded_multi_candidate_fetches(program, derived)
    fetch_ids = {s.id for s in program.steps if OP_FAMILY.get(s.op) is not None}
    # 取得 step の id → その step までの試行で使った候補の優先順の最大（後戻りの検出用）。
    chain_max: dict[str, int] = {}
    # 所在候補を割り当てた取得 step（分岐の結線の検査で「候補の値」を見分けるのに使う）。
    candidate_fetches: set[str] = set()
    for step in program.steps:
        step.unit_id = None  # 典拠はコード側が刻む（LLM が書いた値は破棄）
        family = OP_FAMILY.get(step.op)
        if family is None:
            continue
        # この step が「前段の取得が空だったから走る」ものなら、そこまでに試した候補の
        # 優先順の最大を取る（空判定を経ない guard・無条件実行なら -1 で検査は効かない）。
        tried = max(
            (
                chain_max[f]
                for f in _emptiness_gated_fetches(step.guard, program, closure, fetch_ids)
                if f in chain_max
            ),
            default=-1,
        )
        specs = [d for d in derived if d.family == family]
        if not specs:
            # 索引由来の候補を持たない取得（手入力所在など）でも試行の連鎖は続く。
            # ここで記録を切ると、間に挟まった1 step で後続の逆転が見えなくなる。
            chain_max[step.id] = tried
            continue
        # 同ファミリの候補のうち、生成物が識別子を踏んでいるものがあればそれを典拠とする
        # （所在が複数候補＝多ソースのプログラムでも step 単位で正しく対応づけられる）。
        text = _generated_identifier_text(step, family)
        matched = next((s for s in specs if s.matches(text)), None)
        norm = _normalize_generated_text(text)
        # 別 step と同一の生成物。step ごとの意図の表明とは見なせない（「A を試して、
        # 空なら A をもう一度試す」形）ので、**どの候補のコピペかに依らず**人へ回す。
        # 「使用済みの候補と一致したときだけ」にすると、第2候補をコピペした無条件の2 step で
        # 第1候補の引数が黙って焼き込まれ、LLM が一度も書いていない取得に差し替わる。
        duplicated = bool(matched is not None and norm and norm in used_texts)
        if duplicated:
            matched = None
        if norm:
            used_texts.add(norm)
        spec = matched or specs[0]
        rank = order[id(spec)]
        if step.id in unguarded:
            # 同じ項目の複数候補を無条件に取っている。どちらの値を採るかが
            # 静的に定まらないので、優先順の検査より先にここで人へ回す。
            block = "guard無しの複数候補取得"
        elif duplicated or (matched is None and id(spec) in used):
            # 生成物が別 step と同一、または引数を焼き込もうとしている候補が既に使われている
            # ＝この step の所在は特定できない。
            block = "候補重複"
        elif tried > rank:
            block = "優先順の逆転"
        elif _has_unreadable_emptiness(step.guard, program, closure, candidate_fetches):
            block = "空振り判定の向き不明"
        else:
            block = None
        used.add(id(spec))
        # 後戻りした step も含めて連鎖に記録する（第3候補 → 第1候補 → 第2候補のように、
        # 追い越された step が後続にもある場合を漏らさない）。
        chain_max[step.id] = max(rank, tried)
        candidate_fetches.add(step.id)
        # 典拠は「その step が実際に踏んだ／引数を焼き込んだ候補」だけを刻む。特定できない
        # step に第1候補の unit_id を付けると、監査証跡に偽の典拠が残る（strip_llm_unit_ids
        # と同じ方針）。
        step.unit_id = None if block else spec.unit_id
        if family == FAMILY_DB:
            fix = _enforce_db(step, spec, matched is not None, block=block)
        elif family == FAMILY_FHIR:
            fix = _enforce_fhir(step, spec, matched is not None, block=block)
        else:
            fix = _enforce_file(step, spec, matched is not None, block=block)
        if fix:
            fixes.append(fix)
    # 取得 step の対応づけが済んでから、値の選び方（branch）の結線を検査する。
    fixes.extend(_enforce_branch_routing(program, closure, candidate_fetches))
    return fixes


def strip_llm_unit_ids(program: DSLProgram) -> None:
    """LLM が書いた `unit_id` を落とす（所在の典拠はコード側だけが刻む）。

    `enforce_derived_op_args` は索引由来の所在がある項目でのみ呼ばれるため、payload の
    無い項目（手入力所在など）では LLM 出力の unit_id がそのまま保存されうる。実在風の
    偽の典拠が監査証跡に残るのを防ぐため、コンパイル結果には必ずこれを通す。
    """
    for step in program.steps:
        step.unit_id = None


# 患者ごとに値を引く取得 op。`key_type` が決まれば実行時に resolver が登録番号から
# その名前空間の値を解決し、`physical_format` を当ててからバインドする。
_PATIENT_KEYED_OPS = frozenset(
    {
        OpType.SQL_EXTRACT,
        OpType.EXTERNAL_DB_EXTRACT,
        OpType.FHIR_EXTRACT,
        OpType.FILE_EXTRACT,
        OpType.CSV_EXTRACT,
    }
)


def _expected_key_type(
    step: DSLStep, spec: DerivedOpArgs | None, registry: ConnectionRegistry
) -> str | None:
    """宣言から決まる患者キー種別。宣言が無ければ None。

    1. step に典拠として刻まれた索引の単位が `payload.key_type` を持てばそれ（列ごとの宣言）
    2. DB 以外（file / csv / fhir）は接続の既定 `key_type`

    **DB は 1 だけ**にする。患者で絞らない表や典拠の特定できない step に接続単位の
    既定を当てると、要らない解決が走り、未登録の被験者で項目ごと飛ばされる。
    """
    if spec is not None and spec.key_type:
        return spec.key_type
    if OP_FAMILY.get(step.op) == FAMILY_DB:
        return None
    return registry.key_type_of(step.connection_id)


def _drop_patient_key(step: DSLStep, why: str, fixes: list[str]) -> None:
    """step の患者キー指定を外して人へ回す（実行時に誤った名前空間で解決させない）。"""
    if not step.key_type and not step.physical_format:
        return
    written = step.key_type
    step.key_type = None
    step.physical_format = None
    step.needs_input = True
    step.reasoning = _note(
        step.reasoning, f"患者キーの指定（key_type={written}）を外した: {why}。人の確認が必要"
    )
    fixes.append(f"{step.id}:患者キー除去(needs_input)")


def bake_patient_keys(
    program: DSLProgram,
    derived: list[DerivedOpArgs],
    registry: ConnectionRegistry | None = None,
    catalog: KeyTypeCatalog | None = None,
) -> list[str]:
    """取得 step の `key_type` / `physical_format` を索引と接続から決定的に焼き込む。

    **LLM に任せない。** 実機で回すと LLM は `key_type` を1つも書かず、
    DWH の mrn はゼロ詰めされないまま全員 not_found、ファイルのパスは登録番号で組まれた。
    どちらも値は索引（`payload.key_type`）と接続レジストリ（`key_type` / `key_formats`）に
    あり、プロンプトの臨床ロジックとは関係が無い。`enforce_derived_op_args` が表やパスを
    焼き込むのと同じ扱いにする。

    LLM が**別の値**を書いていたら、索引の値で置き換えたうえで `needs_input` にする。黙って
    上書きしないのは、食い違いが「LLM が別の所在を想定して SQL やパスを書いた」ことの徴候で
    ありうるからである（取り違えた患者の値を確定保存するより、人に一度見せる）。未記入からの
    補完は LLM 出力の否定ではないので、是正一覧に載せない（`connection_id` の補完と同じ）。

    患者キーを**付けない**のが正しい step もある。そこへ LLM が書いた指定は外して人へ回す:

    - 「患者で絞らない表」と宣言された単位（マスタ・件数表）。SQL に `:patient_id` が無い
    - 自アプリDB（`sql_extract`）で anchor（登録番号）を指すもの。登録番号をそのまま使う
    - 宣言が無く、LLM の書いた名前がキー種別カタログに無いもの（`patient_id` / `value` 等。
      評価用 DB の保存済みプランに実在した）。実行時に resolver が引けず項目ごと飛ぶ

    宣言が無く、カタログにある名前を LLM が書いた step には手を入れない（推測で否定しない）。

    `enforce_derived_op_args` の**後**に呼ぶこと（典拠の `unit_id` と接続IDが確定している）。
    """
    reg = registry if registry is not None else conn_mod.get_registry()
    cat = catalog if catalog is not None else kt_mod.get_catalog()
    anchor = settings.key_resolver_anchor
    by_unit: dict[str, DerivedOpArgs] = {}
    for d in derived:
        if d.unit_id:
            by_unit.setdefault(d.unit_id, d)
    fixes: list[str] = []
    for step in program.steps:
        if step.op not in _PATIENT_KEYED_OPS:
            continue
        spec = by_unit.get(step.unit_id) if step.unit_id else None
        if spec is not None and not spec.patient_scoped:
            _drop_patient_key(step, "索引が「患者で絞らない表」と宣言している", fixes)
            continue
        key_type = _expected_key_type(step, spec, reg)
        if step.op == OpType.SQL_EXTRACT and (
            key_type == anchor or (key_type is None and step.key_type == anchor)
        ):
            # 自アプリDBは登録番号で引く。anchor を解決させると resolver を余計に呼ぶだけで
            # なく、resolver が落ちている間は自分の DB すら読めなくなる
            if step.key_type not in (None, anchor):
                _drop_patient_key(step, "自アプリDBの表は登録番号（anchor）で引く宣言", fixes)
            step.key_type = None
            step.physical_format = None
            continue
        if key_type is None:
            if step.key_type and step.key_type != anchor and cat.is_loaded:
                if cat.get(step.key_type) is None:
                    _drop_patient_key(step, "キー種別カタログ（key_types.yaml）に無い名前", fixes)
            continue
        fmt = reg.key_format_of(step.connection_id, key_type)
        conflicts = []
        if step.key_type and step.key_type != key_type:
            conflicts.append(f"key_type {step.key_type}→{key_type}")
        if step.physical_format and step.physical_format != fmt:
            conflicts.append(f"physical_format {step.physical_format}→{fmt}")
        step.key_type = key_type
        step.physical_format = fmt
        if conflicts:
            step.needs_input = True
            step.reasoning = _note(
                step.reasoning,
                f"患者キーの指定が索引・接続の宣言と食い違うため置き換えた（{', '.join(conflicts)}）。"
                "所在の取り違えでないか人の確認が必要",
            )
            fixes.append(f"{step.id}:患者キー不一致(needs_input)")
    return fixes


class AIPlanCompiler:
    """ステップ3: 確認済み手順書を DSL プログラムへコンパイルするクラス"""

    def __init__(self) -> None:
        # プロバイダは設定で決まる。Ollama（既定）または vLLM。
        # `chat_json` が読むのは
        # `.message.content` だけなので、契約はそこに閉じている。
        self._client = make_design_llm_client()
        # link_id → データ所在から決定的に導出した op 引数（プロンプト提示と生成後の
        # 是正で同じものを使うため compile() の冒頭で一度だけ組み立てる）。
        self._derived: dict[str, list[DerivedOpArgs]] = {}

    def _derived_for(self, item: dict[str, Any]) -> list[DerivedOpArgs]:
        link_id = str(item.get("link_id"))
        if link_id not in self._derived:
            self._derived[link_id] = derive_op_args_list(candidates_of(item))
        return self._derived[link_id]

    def _build_user_message(self, items: list[dict[str, Any]]) -> str:
        lines = ["以下の各入力項目について program を生成してください:\n"]
        for d in items:
            lines.append(f"### linkId: {d.get('link_id')}")
            lines.append(f"  項目名: {d.get('item_text') or '（未設定）'}")
            lines.append(f"  ソース種別: {d.get('source_category') or 'manual'}")
            if d.get("annotation"):
                lines.append(f"  臨床的コンテキスト（分岐条件の根拠）: {d['annotation']}")
            lines.extend(_location_lines(d, self._derived_for(d)))
            steps = d.get("steps") or []
            if steps:
                lines.append("  確認済み手順書ステップ:")
                for s in steps:
                    tag = f"[{s.get('action_type')}]" if s.get("action_type") else ""
                    lines.append(
                        f"    {s.get('order')}. {tag} {s.get('title')}: {s.get('description')}"
                    )
                    if s.get("data_hint"):
                        lines.append(f"        データ補足: {s['data_hint']}")
            lines.append("")
        return "\n".join(lines)

    async def compile(
        self,
        items: list[dict[str, Any]],
        model: str | None = None,
        recorder: LLMCallRecorder | None = None,
    ) -> list[GeneratedPlanItem]:
        """項目ごとの手順書＋2役割入力から DSL プログラムを生成して返す

        生成後、データ所在が索引由来（payload あり）の項目については op 引数を
        `enforce_derived_op_args` で検証・是正する（表・カラム・クエリ・パスの捏造を
        構造的に防ぎ、典拠 unit_id を step に刻む）。

        Args:
            items: {link_id, item_text, source_category, annotation, location,
                    unit_id, connection_id, payload, candidates, steps} のリスト。
                   candidates は SystemLocation の優先順候補配列（機械可読情報を含む）。
                   steps は確認済み手順書のステップ配列。

        Raises:
            AIServiceException: Ollama呼び出し・パースエラー
        """
        if not items:
            return []

        selected_model = (model or design_llm_model()).strip()
        # **使ったモデルのダイジェストを残す**。タグだけでは実装が
        # 特定できない（同名タグの中身が差し替わっても記録は同じ文字列）。取れなくても
        # 生成は続ける——理由つきの null が残る（`model_digest_of` は例外を投げない）。
        if isinstance(recorder, DesignLlmRecorder):
            await recorder.declare_model_digest(self._client, selected_model)
        self._derived = {}
        for item in items:  # プロンプト提示前に導出しておく（提示と是正で同じ値を使う）
            self._derived_for(item)
        logger.info(f"DSLコンパイル開始: {len(items)}件, model={selected_model}")

        # 多項目を1コールに詰めると num_ctx 超過で空応答になるため分割して呼ぶ。
        try:
            plans = await chat_json_chunked(
                self._client,
                selected_model,
                items,
                system_prompt=COMPILE_SYSTEM_PROMPT,
                build_message=self._build_user_message,
                # **一様なキーの省略を許さない**。既定値つきのフィールドは
                # `required` に入らず、structured output はモデルに省略を許す。
                response_format=_plan_response_format(),
                parse=lambda d: GeneratedPlans(**d).plans,
                label="DSLコンパイル",
                chunk_size=COMPILE_CHUNK_SIZE,
                options=LARGE_CONTEXT_OPTIONS,
                recorder=recorder,
            )
        except PartialResultException as e:
            # **完了済みチャンクを捨てない**。この経路の「未試行」は
            # **プランを生成していない**（再実行すれば対象に入る）という意味で、
            # 「生成したが op 引数の是正で落ちた」とは別である。**呼び出し側は
            # 渡した link_id と返ってきた link_id の差で未試行を数える**
            # （件数ではなく項目で持つ。押し直す対象が分かる形にする）。
            plans = e.partial
            logger.warning(
                f"DSLコンパイルが途中で終わった（{e.message}）: "
                f"プランが返ったのは{len(plans)}件。"
                "未試行は「生成できなかった」ではないので、再実行して埋めること"
            )

        enforced = 0
        for plan in plans:
            derived = self._derived.get(plan.link_id) or []
            if not derived:
                # 索引由来の所在が無い項目は LLM 出力をそのまま採用する。ただし典拠だけは
                # 例外で、LLM が書いた unit_id は必ず捨てる（偽の来歴を残さない）。
                strip_llm_unit_ids(plan.program)
                fixes = []
            else:
                fixes = enforce_derived_op_args(plan.program, derived)
            # 患者キーは所在が手入力でも接続の宣言から決まる（file / csv / fhir）。
            fixes.extend(bake_patient_keys(plan.program, derived))
            if fixes:
                enforced += 1
                logger.info(f"op引数を決定的に是正: link_id={plan.link_id}, {', '.join(fixes)}")

        logger.info(f"DSLコンパイル完了: {len(plans)}件（op引数を是正: {enforced}件）")
        return plans
