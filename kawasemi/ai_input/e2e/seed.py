"""E2E のアプリDB投入: 様式・利用者・データセット。

投入するもの:

- Questionnaire（`examples/e2e/questionnaire_e2e_infection.json`）
- 利用者 3 名（設計者・収集者・閲覧検証者。パスワードは `demo_password()`。管理者はアカウント
  運用だけの役割なので E2E には要らない）
- Dataset ＋ 10 InputTarget（全員が確定待ち。設計成果物は投入しない——所在探索・手順書・
  DSL は E2E の試験が索引から作る）

最小の投入だけを持つ。冪等（再実行で入れ直す）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.e2e import (
    DATASET_DESCRIPTION,
    DATASET_NAME,
    E2E_STUDY_ID,
    QUESTIONNAIRE_JSON,
    SUBJECTS,
    E2ESubject,
)
from kawasemi.core.auth import get_password_hash
from kawasemi.core.database import AsyncSessionLocal
from kawasemi.core.demo_secrets import demo_password
from kawasemi.core.logging_config import get_logger
from kawasemi.models import (
    Dataset,
    DatasetQuestionnaire,
    InputTarget,
    QuestionnaireResponse,
    Role,
    RoleType,
    User,
    UserRole,
    get_or_create_subject,
)
from kawasemi.models.input_target import InputTargetStatus
from kawasemi.models.questionnaire import Questionnaire

logger = get_logger(__name__)

USER_DESIGNER = "e2e_designer"
USER_COLLECTOR = "e2e_collector"
USER_REVIEWER = "e2e_reviewer"


def seed_users() -> dict[str, tuple[str, list[str]]]:
    """投入する利用者 {username: (表示名, ロール名の列)}。

    **ロール名に依存する箇所はこの関数だけにする。** 4 役割のうち、設計の全段階を
    通せる設計者、自動収集と回答送信をする収集者、回答を検証する閲覧・検証者の 3 名を作る。
    管理者はアカウント運用だけで患者データの画面に入らないので、E2E の動線には要らない。
    """
    return {
        USER_DESIGNER: ("E2E 設計者（試験用）", [RoleType.DESIGNER]),
        USER_COLLECTOR: ("E2E 収集者（試験用）", [RoleType.COLLECTOR]),
        USER_REVIEWER: ("E2E 閲覧・検証者（試験用）", [RoleType.REVIEWER]),
    }


async def _register_questionnaire(db: AsyncSession) -> int:
    content = json.loads(QUESTIONNAIRE_JSON.read_text("utf-8"))
    fhir_id = content["id"]
    existing = (
        await db.execute(select(Questionnaire).where(Questionnaire.fhir_id == fhir_id))
    ).scalar_one_or_none()
    if existing:
        return int(existing.id)
    q = Questionnaire(
        fhir_id=fhir_id,
        title=content.get("title"),
        status=content.get("status", "active"),
        version=content.get("version"),
        url=content.get("url"),
        content=content,
        is_deleted=False,
    )
    db.add(q)
    await db.flush()
    logger.info(f"Questionnaire 登録: {q.title} (id={q.id})")
    return int(q.id)


async def _ensure_user(db: AsyncSession, username: str, full_name: str, roles: list[str]) -> User:
    role_rows: list[Role] = []
    for role in roles:
        role_row = (await db.execute(select(Role).where(Role.name == role))).scalar_one_or_none()
        if not role_row:
            role_row = Role(name=role, description=role)
            db.add(role_row)
            await db.flush()
        role_rows.append(role_row)
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not user:
        password = demo_password()
        user = User(
            username=username,
            email=f"{username}@e2e.example",
            full_name=full_name,
            password_hash=get_password_hash(password),
            is_active=True,
        )
        db.add(user)
        await db.flush()
        for role_row in role_rows:
            db.add(UserRole(user_id=user.id, role_id=role_row.id, assigned_by_id=None))
        logger.info(f"ユーザー '{username}' を作成（ロール: {', '.join(roles)}）")
    return user


async def _seed_dataset(
    db: AsyncSession, questionnaire_id: int, owner: User, subjects: list[E2ESubject]
) -> None:
    """Dataset と InputTarget（全員 pending）を入れ直す。"""
    existing = (
        (await db.execute(select(Dataset).where(Dataset.name == DATASET_NAME))).scalars().all()
    )
    for ds in existing:
        await db.delete(ds)  # input_targets / dataset_questionnaires は cascade
    await db.execute(
        delete(QuestionnaireResponse).where(
            QuestionnaireResponse.questionnaire_id == questionnaire_id
        )
    )
    await db.flush()

    dataset = Dataset(name=DATASET_NAME, description=DATASET_DESCRIPTION, created_by_id=owner.id)
    db.add(dataset)
    await db.flush()
    db.add(
        DatasetQuestionnaire(
            dataset_id=dataset.id, questionnaire_id=questionnaire_id, display_order=0
        )
    )
    for s in subjects:
        subject = await get_or_create_subject(db, E2E_STUDY_ID, s.reg_no)
        db.add(
            InputTarget(
                dataset_id=dataset.id,
                questionnaire_id=questionnaire_id,
                patient_id=s.reg_no,
                subject_id=subject.id,
                status=InputTargetStatus.PENDING,
            )
        )
    logger.info(f"Dataset '{DATASET_NAME}' を seed: InputTarget {len(subjects)}件（全員 pending）")


async def seed_all(
    session_factory: Callable[[], Any] = AsyncSessionLocal,
    subjects: list[E2ESubject] = SUBJECTS,
) -> int:
    """様式・利用者・データセットをアプリDBへ投入し、Questionnaire ID を返す。

    `session_factory` はテストが差し替える（本番は `AsyncSessionLocal`）。
    """
    async with session_factory() as db:
        questionnaire_id = await _register_questionnaire(db)
        users = {
            name: await _ensure_user(db, name, full_name, roles)
            for name, (full_name, roles) in seed_users().items()
        }
        await _seed_dataset(db, questionnaire_id, users[USER_COLLECTOR], subjects)
        await db.commit()
    return questionnaire_id
