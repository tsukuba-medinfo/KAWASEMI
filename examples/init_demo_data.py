"""デモデータ初期化スクリプト

開発・デモ用途で使用するサンプルデータを一括登録します。

実行方法:
    uv run python examples/init_demo_data.py
    または
    uv run python -m kawasemi.cli init_demo

注意:
    このスクリプトは既存のデータベースを完全にリセットします。
    本番環境では絶対に実行しないでください。
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.auth import get_password_hash
from kawasemi.core.database import AsyncSessionLocal, drop_db, init_db
from kawasemi.core.demo_secrets import demo_password, was_generated
from kawasemi.models import (
    Dataset,
    DatasetQuestionnaire,
    InputTarget,
    InputTargetStatus,
    Questionnaire,
    Role,
    RoleType,
    User,
    UserRole,
)


async def create_roles(db: AsyncSession) -> dict[str, Role]:
    """4つのロール（RoleType.ALL）を揃える

    マイグレーションが roles 表へ4役割を入れるので、在るものは取得し、無いものだけ作る。
    """
    roles = {}
    for name in RoleType.ALL:
        role = (await db.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
        if role is None:
            role = Role(name=name, description=RoleType.DESCRIPTIONS[name])
            db.add(role)
            await db.flush()  # IDを取得するため
        roles[name] = role

    await db.commit()
    return roles


async def create_users(db: AsyncSession, roles: dict[str, Role]) -> dict[str, User]:
    """4名のデモユーザーを作成（4役割各1名）

    **パスワードはコミットしない**。`DEMO_PASSWORD` が与えられていれば
    それを、無ければ起動ごとに生成した値を全員に設定する。以前は各ユーザー名と同じ
    文字列（`admin` / `admin`）を焼き込んでおり、このデータでシードされて到達可能な
    デプロイでは管理者権限を丸ごと奪われた（CWE-798）。

    収集者の username は "user1" とする。
    """
    password = demo_password()
    users_data = [
        ("admin", "admin@example.com", "管理者", [RoleType.ADMIN]),
        ("designer", "designer@example.com", "設計者", [RoleType.DESIGNER]),
        ("user1", "user1@example.com", "収集者", [RoleType.COLLECTOR]),
        ("reviewer", "reviewer@example.com", "閲覧・検証者", [RoleType.REVIEWER]),
    ]

    users = {}
    for username, email, full_name, role_names in users_data:
        # ユーザー作成
        user = User(
            username=username,
            email=email,
            full_name=full_name,
            password_hash=get_password_hash(password),
            is_active=True,
        )
        db.add(user)
        await db.flush()

        # ロール割り当て
        for role_name in role_names:
            user_role = UserRole(
                user_id=user.id,
                role_id=roles[role_name].id,
                assigned_by_id=None,  # 初期データなので割り当て者なし
            )
            db.add(user_role)

        users[username] = user

    await db.commit()
    return users


async def create_questionnaires(db: AsyncSession) -> dict[str, Questionnaire]:
    """3種類のQuestionnaireを作成"""

    questionnaires_data = [
        {
            "fhir_id": "patient-basic-info",
            "title": "患者基本情報入力フォーム",
            "status": "active",
            "version": "1.0.0",
            "url": "http://example.org/fhir/Questionnaire/patient-basic-info",
            "content": {
                "resourceType": "Questionnaire",
                "id": "patient-basic-info",
                "meta": {
                    "profile": [
                        "http://www.hosp.ncgm.go.jp/JASPEHR/fhir/StructureDefinition/jaspehr-questionnaire"
                    ]
                },
                "url": "http://example.org/fhir/Questionnaire/patient-basic-info",
                "version": "1.0.0",
                "title": "患者基本情報入力フォーム",
                "status": "active",
                "item": [
                    {
                        "linkId": "patient_name",
                        "type": "string",
                        "text": "患者氏名",
                        "required": True,
                    },
                    {
                        "linkId": "gender",
                        "type": "choice",
                        "text": "性別",
                        "required": True,
                        "answerOption": [
                            {"valueCoding": {"code": "male", "display": "男性"}},
                            {"valueCoding": {"code": "female", "display": "女性"}},
                        ],
                    },
                    {
                        "linkId": "birthdate",
                        "type": "date",
                        "text": "生年月日",
                        "required": True,
                    },
                ],
            },
        },
        {
            "fhir_id": "surgery-record",
            "title": "手術記録入力フォーム",
            "status": "active",
            "version": "1.0.0",
            "url": "http://example.org/fhir/Questionnaire/surgery-record",
            "content": {
                "resourceType": "Questionnaire",
                "id": "surgery-record",
                "meta": {
                    "profile": [
                        "http://www.hosp.ncgm.go.jp/JASPEHR/fhir/StructureDefinition/jaspehr-questionnaire"
                    ]
                },
                "url": "http://example.org/fhir/Questionnaire/surgery-record",
                "version": "1.0.0",
                "title": "手術記録入力フォーム",
                "status": "active",
                "item": [
                    {
                        "linkId": "surgery_date",
                        "type": "date",
                        "text": "手術日",
                        "required": True,
                    },
                    {
                        "linkId": "surgery_type",
                        "type": "string",
                        "text": "手術名",
                        "required": True,
                    },
                    {
                        "linkId": "surgeon",
                        "type": "string",
                        "text": "執刀医",
                        "required": True,
                    },
                ],
            },
        },
        {
            "fhir_id": "research-survey",
            "title": "研究用アンケート",
            "status": "draft",  # 未登録扱い
            "version": "0.1.0",
            "url": "http://example.org/fhir/Questionnaire/research-survey",
            "content": {
                "resourceType": "Questionnaire",
                "id": "research-survey",
                "meta": {
                    "profile": [
                        "http://www.hosp.ncgm.go.jp/JASPEHR/fhir/StructureDefinition/jaspehr-questionnaire"
                    ]
                },
                "url": "http://example.org/fhir/Questionnaire/research-survey",
                "version": "0.1.0",
                "title": "研究用アンケート",
                "status": "draft",
                "item": [
                    {
                        "linkId": "satisfaction",
                        "type": "choice",
                        "text": "満足度",
                        "required": False,
                        "answerOption": [
                            {"valueCoding": {"code": "1", "display": "不満"}},
                            {"valueCoding": {"code": "2", "display": "やや不満"}},
                            {"valueCoding": {"code": "3", "display": "普通"}},
                            {"valueCoding": {"code": "4", "display": "やや満足"}},
                            {"valueCoding": {"code": "5", "display": "満足"}},
                        ],
                    }
                ],
            },
        },
    ]

    questionnaires = {}
    for data in questionnaires_data:
        questionnaire = Questionnaire(
            fhir_id=data["fhir_id"],
            title=data["title"],
            status=data["status"],
            version=data["version"],
            url=data["url"],
            content=data["content"],
            is_deleted=False,
        )
        db.add(questionnaire)
        await db.flush()
        questionnaires[data["fhir_id"]] = questionnaire

    await db.commit()
    return questionnaires


async def create_target_list(
    db: AsyncSession, users: dict[str, User], questionnaires: dict[str, Questionnaire]
) -> int:
    """対象患者リスト（Dataset と InputTarget）を作成する。

    誰を収集するかは管理者が登録するリストで決まり、収集者は「次の患者を受け取る」で
    先着の自動割り当てを受ける（患者を自分で選ぶことはできない）。
    """
    dataset = Dataset(
        name="デモ対象患者リスト",
        description="デモ用の対象患者（架空）。収集者は入力対象一覧から次の患者を受け取る。",
        created_by_id=users["admin"].id,
    )
    db.add(dataset)
    await db.flush()
    qs = [questionnaires["patient-basic-info"], questionnaires["surgery-record"]]
    for order, q in enumerate(qs):
        db.add(
            DatasetQuestionnaire(dataset_id=dataset.id, questionnaire_id=q.id, display_order=order)
        )
    count = 0
    for q in qs:
        for i in range(1, 11):
            db.add(
                InputTarget(
                    dataset_id=dataset.id,
                    questionnaire_id=q.id,
                    patient_id=f"patient-{i:03d}",
                    status=InputTargetStatus.PENDING,
                )
            )
            count += 1
    await db.commit()
    return count


async def init_demo_data() -> None:
    """デモデータの初期化"""
    print("=" * 60)
    print("デモデータ初期化を開始します")
    print("=" * 60)

    # データベースのリセット
    print("\n[1/5] データベースをリセット中...")
    await drop_db()
    await init_db()
    print("✓ データベースをリセットしました")

    async with AsyncSessionLocal() as db:
        # ロール作成
        print("\n[2/5] ロールを作成中...")
        roles = await create_roles(db)
        print(f"✓ {len(roles)}件のロールを作成しました")

        # ユーザー作成
        print("\n[3/5] ユーザーを作成中...")
        users = await create_users(db, roles)
        print(f"✓ {len(users)}名のユーザーを作成しました")
        for username, user in users.items():
            print(f"  - {username} ({user.full_name})")
        # **起動した人が見つけられること**。起動ごとに変わる値
        # なので README にも compose にも書けない。ここが唯一の見せ場である。
        if was_generated():
            print("\n" + "-" * 60)
            print(f"  全員のパスワード（起動ごとに生成）: {demo_password()}")
            print("  固定したいときは DEMO_PASSWORD=... を渡してください。")
            print("-" * 60)
        else:
            print("  パスワードは DEMO_PASSWORD で与えられた値です。")

        # Questionnaire作成
        print("\n[4/5] Questionnaireを作成中...")
        questionnaires = await create_questionnaires(db)
        print(f"✓ {len(questionnaires)}件のQuestionnaireを作成しました")
        for fhir_id, q in questionnaires.items():
            print(f"  - {q.title} (status: {q.status})")

        # 対象患者リスト作成
        print("\n[5/5] 対象患者リストを作成中...")
        n_targets = await create_target_list(db, users, questionnaires)
        print(f"✓ {n_targets}件の入力対象を作成しました")

    print("\n" + "=" * 60)
    print("デモデータ初期化が完了しました！")
    print("=" * 60)
    print("\nログイン情報（4役割各1名 / パスワードは DEMO_PASSWORD か起動時に生成した値）:")
    print("  - 管理者: admin")
    print("  - 設計者: designer")
    print("  - 収集者: user1")
    print("  - 閲覧・検証者: reviewer")
    print("\nサーバーを起動してアクセスしてください:")
    print("  uv run kawasemi serve")
    print("  http://localhost:8000/")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(init_demo_data())
