"""計算プロパティの単体テスト。DB 不要の純粋検証。"""

from datetime import date, timedelta

from kawasemi.models.batch_job import BatchJob, BatchJobStatus
from kawasemi.models.case_assignment import AssignmentStatus, CaseAssignment
from kawasemi.models.input_target import InputTarget, InputTargetStatus


def _assignment(status: str, due_date: date | None) -> CaseAssignment:
    return CaseAssignment(
        questionnaire_id=1, patient_id="P1", assigned_to_id=1, status=status, due_date=due_date
    )


def test_is_overdue_none_due_date() -> None:
    assert _assignment(AssignmentStatus.PENDING, None).is_overdue is False


def test_is_overdue_past_due_active() -> None:
    a = _assignment(AssignmentStatus.PENDING, date.today() - timedelta(days=1))
    assert a.is_overdue is True


def test_is_overdue_future_due() -> None:
    a = _assignment(AssignmentStatus.IN_PROGRESS, date.today() + timedelta(days=1))
    assert a.is_overdue is False


def test_is_overdue_completed_or_cancelled_never_overdue() -> None:
    past = date.today() - timedelta(days=10)
    assert _assignment(AssignmentStatus.COMPLETED, past).is_overdue is False
    assert _assignment(AssignmentStatus.CANCELLED, past).is_overdue is False


def test_assignment_status_label() -> None:
    a = _assignment(AssignmentStatus.PENDING, None)
    assert a.status_label == AssignmentStatus.LABELS[AssignmentStatus.PENDING]


def test_batch_job_is_active() -> None:
    assert BatchJob(status=BatchJobStatus.RUNNING).is_active is True
    assert BatchJob(status=BatchJobStatus.COMPLETED).is_active is False


def test_input_target_status_label() -> None:
    t = InputTarget(
        dataset_id=1, questionnaire_id=1, patient_id="P1", status=InputTargetStatus.AI_DRAFTED
    )
    assert t.status_label == InputTargetStatus.LABELS[InputTargetStatus.AI_DRAFTED]
