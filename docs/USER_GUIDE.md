# KAWASEMI ユーザーガイド

## はじめに

KAWASEMIは、JASPEHR Implementation Guideに準拠したQuestionnaire/QuestionnaireResponseを扱うためのテンプレート入力システムです。

## 基本的な使い方

### 1. Questionnaireの登録

#### Web APIを使用

```bash
curl -X POST http://localhost:8000/api/v100/questionnaires \
  -H "Content-Type: application/json" \
  -d '{
    "resource": {
      "resourceType": "Questionnaire",
      "status": "active",
      "title": "患者基本情報",
      "item": [
        {
          "linkId": "name",
          "type": "string",
          "text": "患者氏名",
          "required": true
        },
        {
          "linkId": "gender",
          "type": "choice",
          "text": "性別",
          "required": true,
          "answerOption": [
            {
              "valueCoding": {
                "code": "male",
                "display": "男性"
              }
            },
            {
              "valueCoding": {
                "code": "female",
                "display": "女性"
              }
            }
          ]
        }
      ]
    }
  }'
```

#### サンプルを使用

```bash
uv run python examples/load_sample.py
```

### 2. Web UIでの入力

1. ブラウザで http://localhost:8000/ にアクセス
2. 「フォーム一覧」から使用したいQuestionnaireを選択
3. フォームに必要事項を入力
4. 「送信」ボタンをクリック
5. QuestionnaireResponseが自動生成されます

### 3. QuestionnaireResponseの取得

#### 特定のQuestionnaireResponseを取得

```bash
curl http://localhost:8000/api/v100/questionnaire-responses/1
```

#### 一覧を取得

```bash
# 全件取得
curl http://localhost:8000/api/v100/questionnaire-responses

# 患者IDでフィルタ
curl "http://localhost:8000/api/v100/questionnaire-responses?subject=Patient/123"

# Questionnaire IDでフィルタ
curl "http://localhost:8000/api/v100/questionnaire-responses?questionnaire_id=1"
```

### 4. ファイルへのエクスポート

#### QuestionnaireのエクスポートQuestionnaireのエクスポート

```bash
curl -X POST http://localhost:8000/api/v100/questionnaires/1/export \
  -o questionnaire.json
```

#### QuestionnaireResponseのエクスポート

```bash
curl -X POST http://localhost:8000/api/v100/questionnaire-responses/1/export \
  -o response.json
```

エクスポートされたファイルは `./output/` ディレクトリにも保存されます。

## Questionnaire の作成ガイド

### 対応している item type

| Type | HTML Input | 説明 |
|------|------------|------|
| string | text | 短いテキスト入力 |
| text | textarea | 長いテキスト入力 |
| boolean | checkbox | はい/いいえ |
| integer | number | 整数 |
| decimal | number | 小数 |
| date | date | 日付 |
| dateTime | datetime-local | 日時 |
| time | time | 時刻 |
| choice | select | 単一選択 |
| open-choice | select | 単一選択（その他入力可） |

### サンプルQuestionnaire

詳細なサンプルは `examples/sample_questionnaire.json` を参照してください。

```json
{
  "resourceType": "Questionnaire",
  "status": "active",
  "title": "患者情報",
  "item": [
    {
      "linkId": "q1",
      "type": "string",
      "text": "氏名",
      "required": true
    },
    {
      "linkId": "q2",
      "type": "choice",
      "text": "性別",
      "required": true,
      "answerOption": [
        {
          "valueCoding": {
            "code": "male",
            "display": "男性"
          }
        },
        {
          "valueCoding": {
            "code": "female",
            "display": "女性"
          }
        }
      ]
    }
  ]
}
```

## API ドキュメント

詳細なAPI仕様は http://localhost:8000/docs で確認できます（Swagger UI）。

### 主要エンドポイント

| メソッド | エンドポイント | 説明 |
|---------|--------------|------|
| GET | `/health` | ヘルスチェック |
| GET | `/` | Web UIホーム |
| GET | `/forms` | フォーム一覧 |
| GET | `/forms/{id}` | フォーム表示 |
| POST | `/api/v100/questionnaires` | Questionnaire登録 |
| GET | `/api/v100/questionnaires/{id}` | Questionnaire取得 |
| POST | `/api/v100/questionnaires/{id}/export` | Questionnaireエクスポート |
| POST | `/api/v100/questionnaire-responses` | QuestionnaireResponse作成 |
| GET | `/api/v100/questionnaire-responses/{id}` | QuestionnaireResponse取得 |
| POST | `/api/v100/questionnaire-responses/{id}/export` | QuestionnaireResponseエクスポート |

## トラブルシューティング

### Questionnaireが表示されない

- Questionnaireのstatusが "active" になっているか確認
- データベースに正しく登録されているか確認:
  ```bash
  curl http://localhost:8000/api/v100/questionnaires
  ```

### フォーム送信エラー

- 必須項目がすべて入力されているか確認
- ブラウザの開発者ツールでエラーメッセージを確認

### ファイルが出力されない

- `output/` ディレクトリの権限を確認
- 環境変数 `QUESTIONNAIRE_OUTPUT_DIR` / `QUESTIONNAIRE_RESPONSE_OUTPUT_DIR` が正しく設定されているか確認

## よくある質問（FAQ）

**Q: PostgreSQLを使用できますか？**
A: はい。`.env` ファイルで `DATABASE_URL` をPostgreSQLのURLに変更してください。

**Q: 複数のQuestionnaireを同時に管理できますか？**
A: はい。必要なだけQuestionnaireを登録できます。

**Q: QuestionnaireResponseを編集できますか？**
A: 現在のバージョンでは編集機能はありません。新しいQuestionnaireResponseを作成してください。

**Q: JASPEHR以外のプロファイルに対応していますか？**
A: 基本的なFHIR R4には対応していますが、JASPEHR固有の検証に最適化されています。
