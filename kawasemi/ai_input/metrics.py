"""取得困難度メトリクスの集計サービス

InputExecution（本番 autofill の永続実行ログ）を一次データとして、
Questionnaire × 期間 × 項目（link_id）の軸で以下を集計する:

- 自動充足率: **分母**（実行数 − `not_applicable`）のうち値が得られた
  （prefill された）割合
- 失敗理由内訳: FailureCode 別の件数。failure_code を持たない旧レコードの失敗は
  unclassified として扱う（受け入れ条件: 旧レコードで集計が壊れない）
- 人手介入率: 同じ questionnaire × patient の最新回答（QuestionnaireResponse）との
  突合。autofill が値を出せず人が値を入れた（manual_entered）、または autofill 値と
  回答値が異なる（manual_corrected）を人手介入とみなす

PHI 非含有: 集計結果は件数・率のみで構成し、患者値・patient_id を含めない
（既存の PHI レダクション方針に準拠）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.dsl import is_empty_program
from kawasemi.ai_input.executor import is_empty_value
from kawasemi.ai_input.models import ExecutionStatus, FailureCode, InputExecution, InputPlan
from kawasemi.models.questionnaire_response import QuestionnaireResponse

# evidence 内の予約キー（監査刻印 _meta は link_id ではない）
_META_KEY = "_meta"

# **取得困難度の分母から除く失敗コード**。「該当しないので取得しなくて正しい」を
# 困難度の分母に入れると困難度が過小に見える。除いた件数は `ItemMetrics.not_applicable` で
# 別掲するので、除外が黙って起きることはない（会計: `filled + Σfailure_counts == executions`
# かつ `denominator == executions - not_applicable`）。
_DENOMINATOR_EXCLUDED_CODES = frozenset({FailureCode.NOT_APPLICABLE})


@dataclass
class ItemMetrics:
    """項目（link_id）単位の集計値。件数・率のみで PHI を含まない。"""

    link_id: str
    item_text: str | None = None
    executions: int = 0  # この項目が実行された回数（evidence に現れた回数）
    filled: int = 0  # 値が得られた回数
    failure_counts: dict[str, int] = field(default_factory=dict)  # FailureCode 別件数
    responses_compared: int = 0  # 回答と突合できた回数
    manual_entered: int = 0  # autofill 空振り → 人が値を入力
    manual_corrected: int = 0  # autofill 値 → 人が別の値へ修正

    @property
    def not_applicable(self) -> int:
        """**分母から除いた件数**（患者の文脈上そもそも該当しない）。

        `failure_counts` には残す（内訳としては見える）。分母から除くだけである。
        """
        return sum(self.failure_counts.get(code, 0) for code in _DENOMINATOR_EXCLUDED_CODES)

    @property
    def denominator(self) -> int:
        """自動充足率の**分母**（実行回数から「該当しない」を除いた数）。"""
        return self.executions - self.not_applicable

    @property
    def fill_rate(self) -> float | None:
        """自動充足率（分母が 0 なら None）。

        **分母は `executions` ではなく `denominator`**（「該当しないので取得しなくて正しい」を
        分母に入れると困難度が過小に見える）。除いた件数は `not_applicable` で
        別掲する。
        """
        if self.denominator == 0:
            return None
        return self.filled / self.denominator

    @property
    def interventions(self) -> int:
        return self.manual_entered + self.manual_corrected

    @property
    def intervention_rate(self) -> float | None:
        """人手介入率（回答と突合できた実行が無ければ None）。"""
        if self.responses_compared == 0:
            return None
        return self.interventions / self.responses_compared


@dataclass
class AcquisitionSummary:
    """集計結果のルート。"""

    questionnaire_id: int
    total_executions: int = 0  # 期間内の本番実行（InputExecution）件数
    compared_responses: int = 0  # 突合できた（実行×最新回答）ペア数
    # 項目 evidence を残さず実行全体が失敗した件数（resolver 不通など実行前提の障害。
    # 項目統計のどこにも現れないため別掲する）
    failed_without_evidence: int = 0
    # **母集団の縮み**。取得困難度は `InputExecution`（本番 autofill の実行ログ）
    # を一次データにするが、autofill を実行できるのは **`confirmed` になったプランだけ**
    # である。プランを確定できなかった項目——所在が分からない・SQL を書けない・所在候補が
    # 無い項目——は母集団から落ちる。**それらは本来「取得困難度が最も高い項目」**なので、
    # 集計は困難度を**過小に見せる**。
    #
    # 測定器（この関数の集計規則）は変えない（測定の途中で測定器を変えない原則）。代わりに **母集団の縮みを数で併記する**——
    # 「対象項目のうち confirmed に至った項目数」を常に一緒に出し、取得困難度が
    # **「confirmed に至った項目に限った条件付きの値」**であることを読み手に示す。
    # 所在の確からしさの段階分布（所在判定の `coverage`）は対象項目全部を分母にするので、**2つの分母が
    # 違うことが出力から分かる形**にする。バイアスの向きは**過小**である。
    target_item_count: int = 0  # 対象項目数（集計の分母。問診票から数える）
    confirmed_item_count: int = 0  # うち confirmed なプランがある項目数（集計の母集団）
    # **母集団のうち step が1つも無いプランの項目数**。空のプランは手入力への
    # 縮退という正当な形だが、確定できてしまうので `confirmed_item_count` に入る。実行すると
    # 必ず `needs_input` になり（分母には入り、`filled` には入らない）、取得困難度の誤計上は
    # 起きない。ただし**「取得の設計ができた項目」と読むと母集団を実態より広く見せる**ので、
    # 測定器の規則は変えず、内数として別掲する。
    confirmed_empty_program_count: int = 0
    items: list[ItemMetrics] = field(default_factory=list)

    @property
    def population_shrink(self) -> int:
        """母集団から落ちた項目数（`target_item_count - confirmed_item_count`）。"""
        return max(0, self.target_item_count - self.confirmed_item_count)


def _to_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _loose_eq(a: Any, b: Any) -> bool:
    """autofill 値と回答値の緩い等値（数値化できれば数値比較。executor と同義）。"""
    an, bn = _to_number(a), _to_number(b)
    if an is not None and bn is not None:
        return an == bn
    return str(a) == str(b)


def _subject_matches(subject: str | None, patient_id: str) -> bool:
    """QuestionnaireResponse.subject（FHIR参照のフリーテキスト）と患者IDの突合。

    subject は "Patient/123" 形式が想定だが、生ID・URL付き参照の揺れを許容する。
    """
    if not subject:
        return False
    return subject == patient_id or subject.endswith(f"/{patient_id}")


def _collect_answers(items: list[dict[str, Any]] | None, out: dict[str, Any]) -> None:
    """FHIR QuestionnaireResponse の item ツリーから {linkId: 値} を再帰的に集める。

    answer は先頭要素の value* フィールドを採用する（フォームは単一回答が基本）。
    """
    for item in items or []:
        if not isinstance(item, dict):
            continue
        link_id = item.get("linkId")
        answers = item.get("answer") or []
        if link_id and answers and isinstance(answers[0], dict):
            for key, val in answers[0].items():
                if key.startswith("value"):
                    out[str(link_id)] = val
                    break
        _collect_answers(item.get("item"), out)
        for ans in answers:
            if isinstance(ans, dict):
                _collect_answers(ans.get("item"), out)


def _iter_item_evidence(evidence: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """evidence から (link_id, 項目evidence) を列挙する（予約キー _meta を除く）。"""
    pairs: list[tuple[str, dict[str, Any]]] = []
    for link_id, item_ev in (evidence or {}).items():
        if link_id == _META_KEY or not isinstance(item_ev, dict):
            continue
        pairs.append((str(link_id), item_ev))
    return pairs


def _count_item_evidence(m: ItemMetrics, item_ev: dict[str, Any]) -> None:
    """1件の項目 evidence を `ItemMetrics` に数え入れる（**分母と分子を作る規則**）。

    `executions` が分母の第1項、`filled` が分子、`failure_counts` が内訳である。
    **会計**: `filled + Σfailure_counts == executions`（値が空なら必ず何らかのコードが載る）。
    分母から `not_applicable` を除くのは `ItemMetrics.denominator` 側の仕事で、ここでは除かない
    （**除いた件数を別掲できるよう、内訳には残す**）。

    集計の呼び出し側（`acquisition_summary`）は DB からの読み出しと期間絞りだけを行う。規則を
    こちらに置くのは、**分母を作る規則を1か所に固定する**ため（呼び出し側に置くと、
    分母から何を除くかが集計の規則の外で黙って変わりうる）。
    """
    m.executions += 1
    code = item_failure_code(item_ev)
    if code is None:
        m.filled += 1
    else:
        m.failure_counts[code] = m.failure_counts.get(code, 0) + 1


def item_failure_code(item_ev: dict[str, Any]) -> str | None:
    """項目 evidence を成否分類する。値ありなら None（成功）、失敗なら FailureCode。

    failure_code を持たない旧レコードの失敗は unclassified に落とす
    （旧 evidence の構造差で集計が壊れないことを最優先する）。

    空値の定義（None・空文字・空リスト）は executor.is_empty_value と共有し、
    step トレース側の分類（0件一致＝not_found、LLM空出力＝low_confidence）と
    項目単位の成否が矛盾しないようにする。
    """
    if not is_empty_value(item_ev.get("value")):
        return None
    code = item_ev.get("failure_code")
    if isinstance(code, str) and code:
        return code
    return FailureCode.UNCLASSIFIED


async def _population(db: AsyncSession, questionnaire_id: int) -> tuple[int, int, int]:
    """（対象項目数, うち confirmed プランがある項目数, そのうち step が空のプラン数）を返す。

    **分母の正本は問診票の対象項目**（`target_items.resolve_targets`）で、プランの件数から
    数え上げない——プランが無い項目こそ落ちている項目なので、プラン側から数えると縮みが
    見えなくなる。
    """
    from kawasemi.ai_input.target_items import resolve_targets
    from kawasemi.models.questionnaire import Questionnaire

    q = await db.execute(select(Questionnaire).where(Questionnaire.id == questionnaire_id))
    questionnaire = q.scalar_one_or_none()
    if questionnaire is None:
        return 0, 0, 0
    from kawasemi.ai_input.routes import _editable_fields

    target_ids = set(resolve_targets(questionnaire.url, _editable_fields(questionnaire)).link_ids)
    if not target_ids:
        return 0, 0, 0
    rows = await db.execute(
        select(InputPlan.link_id, InputPlan.program).where(
            InputPlan.questionnaire_id == questionnaire_id,
            InputPlan.status == "confirmed",
        )
    )
    confirmed_rows = [(link_id, program) for link_id, program in rows.all()]
    confirmed = {link_id for link_id, _ in confirmed_rows} & target_ids
    empty = {
        link_id
        for link_id, program in confirmed_rows
        if link_id in target_ids and is_empty_program(program or {})
    }
    return len(target_ids), len(confirmed), len(empty)


async def acquisition_summary(
    db: AsyncSession,
    questionnaire_id: int,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> AcquisitionSummary:
    """Questionnaire × 期間の取得困難度メトリクスを集計する。

    - 実行ログは InputExecution（本番のみ。試行 InputTrialRun は evidence 非永続のため
      対象外）を created_at で期間絞りする。
    - 人手介入は「各患者の期間内最新の実行」×「同患者の最新回答」で突合する
      （実行と回答に直接の FK は無く、questionnaire_id + subject 参照で対応付ける）。
    """
    conds = [InputExecution.questionnaire_id == questionnaire_id]
    if date_from is not None:
        conds.append(InputExecution.created_at >= date_from)
    if date_to is not None:
        conds.append(InputExecution.created_at < date_to)
    result = await db.execute(
        select(InputExecution).where(*conds).order_by(InputExecution.created_at)
    )
    executions = list(result.scalars().all())

    # 表示用ラベル: link_id → item_text（プランに無い項目は evidence 由来のみで無名）
    plan_rows = await db.execute(
        select(InputPlan.link_id, InputPlan.item_text).where(
            InputPlan.questionnaire_id == questionnaire_id
        )
    )
    item_texts = {link_id: text for link_id, text in plan_rows.all()}

    summary = AcquisitionSummary(questionnaire_id=questionnaire_id)
    summary.total_executions = len(executions)
    # **母集団の縮みを併記する**。分母（対象項目）は問診票から数え、
    # そのうち confirmed なプランがある項目数を数える。**取得困難度はこの部分集合に限った
    # 条件付きの値**であることを、出力そのものが示す形にする。
    (
        summary.target_item_count,
        summary.confirmed_item_count,
        summary.confirmed_empty_program_count,
    ) = await _population(db, questionnaire_id)
    items: dict[str, ItemMetrics] = {}

    def _item(link_id: str) -> ItemMetrics:
        if link_id not in items:
            items[link_id] = ItemMetrics(link_id=link_id, item_text=item_texts.get(link_id))
        return items[link_id]

    for execution in executions:
        pairs = _iter_item_evidence(execution.evidence)
        if not pairs and execution.status == ExecutionStatus.FAILED:
            # resolver 不通など実行前提の障害: 項目 evidence が残らないため
            # 項目統計に現れない。消えないよう実行単位で別掲する。
            summary.failed_without_evidence += 1
            continue
        for link_id, item_ev in pairs:
            # 分母と分子を作る規則は `_count_item_evidence` に置く。ここは読み出しだけ
            _count_item_evidence(_item(link_id), item_ev)

    # --- 人手介入の突合（患者ごとの最新実行 × 最新回答） ---------------------
    latest_by_patient: dict[str, InputExecution] = {}
    for execution in executions:  # created_at 昇順なので後勝ち＝最新
        latest_by_patient[execution.patient_id] = execution

    if latest_by_patient:
        qr_rows = await db.execute(
            select(QuestionnaireResponse)
            .where(QuestionnaireResponse.questionnaire_id == questionnaire_id)
            .order_by(QuestionnaireResponse.created_at)
        )
        responses = list(qr_rows.scalars().all())

        for patient_id, execution in latest_by_patient.items():
            latest_qr = None
            for qr in responses:  # created_at 昇順なので後勝ち＝最新
                if _subject_matches(qr.subject, patient_id):
                    latest_qr = qr
            if latest_qr is None:
                continue
            answers: dict[str, Any] = {}
            content = latest_qr.content or {}
            _collect_answers(content.get("item"), answers)

            summary.compared_responses += 1
            prefill = execution.prefill_data or {}
            for link_id, item_ev in _iter_item_evidence(execution.evidence):
                m = _item(link_id)
                m.responses_compared += 1
                answered = link_id in answers
                auto_value = prefill.get(link_id)
                if auto_value is None and answered:
                    m.manual_entered += 1
                elif (
                    auto_value is not None
                    and answered
                    and not _loose_eq(auto_value, answers[link_id])
                ):
                    m.manual_corrected += 1

    summary.items = sorted(items.values(), key=lambda m: m.link_id)
    return summary
