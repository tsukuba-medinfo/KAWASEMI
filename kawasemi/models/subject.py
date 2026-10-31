"""被験者（Subject＝Hub）モデル定義

データ自動収集における識別の基点（Hub）。登録番号を試験内で一意に識別する
仮名IDのみを保持し、氏名・外部システムの名寄せ値（MRN 等）は一切保持しない。
それらは実行時に resolver 経由で都度解決する（KAWASEMI は名寄せ値を保持も
受領もしない＝取り違えの構造的防止）。
"""

from __future__ import annotations

from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from kawasemi.models.base import Base, TimestampMixin


class Subject(Base, TimestampMixin):
    """被験者（Hub）。

    ``study_id × registration_no`` で一意。study は当面、試験識別子の文字列で
    持つ（Study 第一級化は後続）。氏名・MRN 等の PHI はここに持たない。
    """

    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    study_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="試験識別子（文字列。Study 第一級化は後続）",
    )
    registration_no: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True, comment="登録番号（試験内一意の仮名ID）"
    )

    __table_args__ = (
        UniqueConstraint("study_id", "registration_no", name="uq_subject_study_registration"),
    )


async def get_or_create_subject(db: AsyncSession, study_id: str, registration_no: str) -> Subject:
    """``(study_id, registration_no)`` で被験者を取得し、無ければ作成する（冪等）。

    既存 ``patient_id`` 文字列からの橋渡しに使う。``commit`` は行わず ``flush`` まで
    で id を確定し、呼び出し側のトランザクションに参加する。
    """
    study_id = study_id.strip()
    registration_no = registration_no.strip()
    result = await db.execute(
        select(Subject).where(
            Subject.study_id == study_id,
            Subject.registration_no == registration_no,
        )
    )
    subject = result.scalar_one_or_none()
    if subject is None:
        subject = Subject(study_id=study_id, registration_no=registration_no)
        db.add(subject)
        await db.flush()
    return subject
