"""サンプルQuestionnaireを登録するスクリプト"""

import asyncio
import json
from pathlib import Path

from kawasemi.core.database import AsyncSessionLocal
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.utils.fhir_utils import (
    extract_questionnaire_metadata,
    validate_fhir_questionnaire,
)


async def load_sample_questionnaire():
    """サンプルQuestionnaireをデータベースに登録"""
    # サンプルファイルの読み込み
    sample_file = Path(__file__).parent / "sample_questionnaire.json"

    with open(sample_file, encoding="utf-8") as f:
        questionnaire_data = json.load(f)

    print(f"サンプルQuestionnaire '{questionnaire_data.get('title')}' を読み込みました")

    # FHIRバリデーション
    fhir_questionnaire = validate_fhir_questionnaire(questionnaire_data)
    print("✓ FHIRバリデーション成功")

    # メタデータ抽出
    metadata = extract_questionnaire_metadata(fhir_questionnaire)

    # データベースに保存
    async with AsyncSessionLocal() as session:
        # 既存のQuestionnaireをチェック
        from sqlalchemy import select

        result = await session.execute(
            select(Questionnaire).where(Questionnaire.fhir_id == questionnaire_data.get("id"))
        )
        existing = result.scalar_one_or_none()

        if existing:
            print(f"既に登録されています (ID: {existing.id})")
            return existing.id

        # 新規作成
        questionnaire = Questionnaire(
            fhir_id=questionnaire_data.get("id"),
            content=questionnaire_data,
            title=metadata.get("title"),
            status=metadata.get("status"),
            version=metadata.get("version"),
            url=metadata.get("url"),
        )

        session.add(questionnaire)
        await session.commit()
        await session.refresh(questionnaire)

        print(f"✓ Questionnaireを登録しました (ID: {questionnaire.id})")
        print(f"  FHIR ID: {questionnaire.fhir_id}")
        print(f"  タイトル: {questionnaire.title}")
        print(f"  ステータス: {questionnaire.status}")
        print(f"\nWeb UIでアクセス: http://localhost:8000/forms/{questionnaire.id}")

        return questionnaire.id


if __name__ == "__main__":
    asyncio.run(load_sample_questionnaire())
