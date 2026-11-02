"""FormGeneratorのテスト（enableWhen機能含む）"""

from kawasemi.web.form_generator import FormGenerator


class TestFormGenerator:
    """FormGenerator基本機能のテスト"""

    def test_generate_simple_fields(self):
        """シンプルなフィールド生成のテスト"""
        questionnaire = {
            "title": "テスト質問票",
            "item": [
                {"linkId": "q1", "type": "string", "text": "氏名", "required": True},
                {"linkId": "q2", "type": "integer", "text": "年齢"},
            ],
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        assert len(fields) == 2
        assert fields[0]["link_id"] == "q1"
        assert fields[0]["type"] == "string"
        assert fields[0]["required"] is True
        assert fields[1]["link_id"] == "q2"
        assert fields[1]["type"] == "integer"
        assert fields[1]["required"] is False

    def test_generate_choice_field(self):
        """選択肢フィールド生成のテスト"""
        questionnaire = {
            "item": [
                {
                    "linkId": "gender",
                    "type": "choice",
                    "text": "性別",
                    "answerOption": [
                        {"valueCoding": {"code": "male", "display": "男性"}},
                        {"valueCoding": {"code": "female", "display": "女性"}},
                    ],
                }
            ]
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        assert len(fields) == 1
        assert fields[0]["type"] == "choice"
        assert len(fields[0]["options"]) == 2
        assert fields[0]["options"][0]["value"] == "male"
        assert fields[0]["options"][0]["display"] == "男性"

    def test_get_title(self):
        """タイトル取得のテスト"""
        questionnaire = {"title": "テスト質問票"}
        generator = FormGenerator(questionnaire)
        assert generator.get_title() == "テスト質問票"

    def test_get_description(self):
        """説明取得のテスト"""
        questionnaire = {"description": "これはテスト用の質問票です"}
        generator = FormGenerator(questionnaire)
        assert generator.get_description() == "これはテスト用の質問票です"


class TestEnableWhenParsing:
    """enableWhenパース機能のテスト"""

    def test_parse_enable_when_boolean(self):
        """boolean型enableWhenのパーステスト"""
        questionnaire = {
            "item": [
                {"linkId": "q1", "type": "boolean", "text": "症状がありますか？"},
                {
                    "linkId": "q2",
                    "type": "string",
                    "text": "症状の詳細",
                    "enableWhen": [{"question": "q1", "operator": "=", "answerBoolean": True}],
                },
            ]
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        # q2にenableWhenが設定されていることを確認
        assert "enable_when" in fields[1]
        enable_when = fields[1]["enable_when"]
        assert len(enable_when["conditions"]) == 1
        assert enable_when["conditions"][0]["question"] == "q1"
        assert enable_when["conditions"][0]["operator"] == "="
        assert enable_when["conditions"][0]["answer_type"] == "boolean"
        assert enable_when["conditions"][0]["answer_value"] is True
        assert enable_when["behavior"] == "all"

    def test_parse_enable_when_string(self):
        """string型enableWhenのパーステスト"""
        questionnaire = {
            "item": [
                {
                    "linkId": "q1",
                    "type": "choice",
                    "text": "種別",
                    "answerOption": [
                        {"valueString": "A"},
                        {"valueString": "B"},
                        {"valueString": "other"},
                    ],
                },
                {
                    "linkId": "q2",
                    "type": "string",
                    "text": "その他の詳細",
                    "enableWhen": [{"question": "q1", "operator": "=", "answerString": "other"}],
                },
            ]
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        enable_when = fields[1]["enable_when"]
        assert enable_when["conditions"][0]["answer_type"] == "string"
        assert enable_when["conditions"][0]["answer_value"] == "other"

    def test_parse_enable_when_integer(self):
        """integer型enableWhenのパーステスト"""
        questionnaire = {
            "item": [
                {"linkId": "age", "type": "integer", "text": "年齢"},
                {
                    "linkId": "q2",
                    "type": "string",
                    "text": "高齢者向け質問",
                    "enableWhen": [{"question": "age", "operator": ">=", "answerInteger": 65}],
                },
            ]
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        enable_when = fields[1]["enable_when"]
        assert enable_when["conditions"][0]["operator"] == ">="
        assert enable_when["conditions"][0]["answer_type"] == "integer"
        assert enable_when["conditions"][0]["answer_value"] == 65

    def test_parse_enable_when_coding(self):
        """coding型enableWhenのパーステスト"""
        questionnaire = {
            "item": [
                {
                    "linkId": "q1",
                    "type": "choice",
                    "text": "診断",
                    "answerOption": [
                        {"valueCoding": {"code": "positive", "display": "陽性"}},
                        {"valueCoding": {"code": "negative", "display": "陰性"}},
                    ],
                },
                {
                    "linkId": "q2",
                    "type": "string",
                    "text": "治療計画",
                    "enableWhen": [
                        {"question": "q1", "operator": "=", "answerCoding": {"code": "positive"}}
                    ],
                },
            ]
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        enable_when = fields[1]["enable_when"]
        assert enable_when["conditions"][0]["answer_type"] == "coding"
        assert enable_when["conditions"][0]["answer_value"] == "positive"

    def test_parse_enable_when_multiple_conditions_all(self):
        """複数条件（all）のパーステスト"""
        questionnaire = {
            "item": [
                {"linkId": "q1", "type": "boolean", "text": "条件1"},
                {"linkId": "q2", "type": "boolean", "text": "条件2"},
                {
                    "linkId": "q3",
                    "type": "string",
                    "text": "両方の条件を満たす場合に表示",
                    "enableWhen": [
                        {"question": "q1", "operator": "=", "answerBoolean": True},
                        {"question": "q2", "operator": "=", "answerBoolean": True},
                    ],
                    "enableBehavior": "all",
                },
            ]
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        enable_when = fields[2]["enable_when"]
        assert len(enable_when["conditions"]) == 2
        assert enable_when["behavior"] == "all"

    def test_parse_enable_when_multiple_conditions_any(self):
        """複数条件（any）のパーステスト"""
        questionnaire = {
            "item": [
                {"linkId": "q1", "type": "boolean", "text": "条件1"},
                {"linkId": "q2", "type": "boolean", "text": "条件2"},
                {
                    "linkId": "q3",
                    "type": "string",
                    "text": "いずれかの条件を満たす場合に表示",
                    "enableWhen": [
                        {"question": "q1", "operator": "=", "answerBoolean": True},
                        {"question": "q2", "operator": "=", "answerBoolean": True},
                    ],
                    "enableBehavior": "any",
                },
            ]
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        enable_when = fields[2]["enable_when"]
        assert enable_when["behavior"] == "any"

    def test_get_enable_when_config(self):
        """enableWhen設定のJSON出力テスト"""
        questionnaire = {
            "item": [
                {"linkId": "q1", "type": "boolean", "text": "チェック"},
                {
                    "linkId": "q2",
                    "type": "string",
                    "text": "詳細",
                    "enableWhen": [{"question": "q1", "operator": "=", "answerBoolean": True}],
                },
            ]
        }
        generator = FormGenerator(questionnaire)
        config = generator.get_enable_when_config()

        # dict として返ることを確認（テンプレート側で | tojson する）
        assert isinstance(config, dict)

        # q2のみが含まれていることを確認（enableWhenがあるフィールドのみ）
        assert "q2" in config
        assert "q1" not in config
        assert config["q2"]["conditions"][0]["question"] == "q1"

    def test_no_enable_when(self):
        """enableWhenがない場合のテスト"""
        questionnaire = {
            "item": [
                {"linkId": "q1", "type": "string", "text": "質問1"},
                {"linkId": "q2", "type": "string", "text": "質問2"},
            ]
        }
        generator = FormGenerator(questionnaire)
        fields = generator.generate_form_fields()

        # enableWhenがないフィールドには enable_when キーがない
        assert "enable_when" not in fields[0]
        assert "enable_when" not in fields[1]

        # 空のconfigが返される
        config = generator.get_enable_when_config()
        assert config == {}


class TestParentText:
    """最も近い祖先の text を項目に持たせる（所在探索のクエリが使う）"""

    def test_nearest_ancestor_text_is_kept(self):
        questionnaire = {
            "resourceType": "Questionnaire",
            "item": [
                {
                    "linkId": "lab",
                    "type": "group",
                    "text": "臨床検査",
                    "item": [
                        {
                            "linkId": "crp",
                            "type": "group",
                            "text": "CRP",
                            "item": [{"linkId": "crp_v", "type": "decimal", "text": "数値(mg/dL)"}],
                        },
                        {
                            "linkId": "untitled",
                            "type": "group",
                            "item": [{"linkId": "u_v", "type": "decimal", "text": "数値"}],
                        },
                    ],
                },
                {"linkId": "top", "type": "string", "text": "氏名"},
            ],
        }
        fields = {f["link_id"]: f for f in FormGenerator(questionnaire).generate_form_fields()}
        assert fields["crp_v"]["parent_text"] == "CRP"
        # text の無い group は飛ばして、その上の text を使う
        assert fields["u_v"]["parent_text"] == "臨床検査"
        assert "parent_text" not in fields["top"]


class TestNestedItemsAndOptions:
    """質問項目の下の子と、code の無い選択肢（感染症の様式で見つかった形）"""

    def test_children_of_a_question_item_are_shown(self):
        """choice の下に置いた子も項目になる（FHIR は group 以外の下にも子を置ける）"""
        questionnaire = {
            "item": [
                {
                    "linkId": "fever",
                    "type": "choice",
                    "text": "発熱",
                    "answerOption": [{"valueCoding": {"code": "1", "display": "あり"}}],
                    "item": [{"linkId": "fever_max", "type": "string", "text": "最高体温"}],
                }
            ]
        }
        fields = FormGenerator(questionnaire).generate_form_fields()
        assert [f["link_id"] for f in fields] == ["fever", "fever_max"]

    def test_an_option_without_a_code_uses_its_display_as_the_value(self):
        """display だけの Coding は display を値にする（None だと選んでも回答に残らない）"""
        questionnaire = {
            "item": [
                {
                    "linkId": "vaccine",
                    "type": "choice",
                    "answerOption": [
                        {"valueCoding": {"code": "1", "display": "A"}},
                        {"valueCoding": {"display": "その他"}},
                    ],
                }
            ]
        }
        opts = FormGenerator(questionnaire).generate_form_fields()[0]["options"]
        assert opts == [{"value": "1", "display": "A"}, {"value": "その他", "display": "その他"}]

    def test_an_integer_option_of_zero_is_kept(self):
        """valueInteger の 0 を値として残す（None かどうかで選ぶ形に変えても落とさない）"""
        questionnaire = {
            "item": [{"linkId": "n", "type": "choice", "answerOption": [{"valueInteger": 0}]}]
        }
        opts = FormGenerator(questionnaire).generate_form_fields()[0]["options"]
        assert opts == [{"value": 0, "display": 0}]


class TestAncestorEnableWhen:
    """条件付き group の子孫は、祖先の条件にも従う（FHIR R4: 無効な item の子孫も無効）"""

    _Q = {
        "item": [
            {
                "linkId": "pathogen",
                "type": "choice",
                "answerOption": [
                    {"valueCoding": {"code": "3", "display": "A"}},
                    {"valueCoding": {"code": "6", "display": "その他"}},
                ],
                "item": [
                    {
                        "linkId": "pathogen_other",
                        "type": "group",
                        "enableWhen": [
                            {"question": "pathogen", "operator": "=", "answerCoding": {"code": "6"}}
                        ],
                        "item": [
                            {"linkId": "pathogen_other_name", "type": "string"},
                            {
                                "linkId": "detail",
                                "type": "group",
                                "enableWhen": [
                                    {
                                        "question": "pathogen_other_name",
                                        "operator": "exists",
                                        "answerBoolean": True,
                                    }
                                ],
                                "item": [{"linkId": "detail_text", "type": "text"}],
                            },
                        ],
                    }
                ],
            }
        ]
    }

    def test_descendants_carry_every_conditional_ancestor(self):
        fields = {f["link_id"]: f for f in FormGenerator(self._Q).generate_form_fields()}
        assert "enable_when_ancestors" not in fields["pathogen"]
        assert "enable_when_ancestors" not in fields["pathogen_other"]  # 自身の条件だけ
        assert fields["pathogen_other_name"]["enable_when_ancestors"] == ["pathogen_other"]
        assert fields["detail"]["enable_when_ancestors"] == ["pathogen_other"]
        assert fields["detail_text"]["enable_when_ancestors"] == ["pathogen_other", "detail"]

    def test_the_form_hides_a_descendant_and_does_not_require_it_up_front(self):
        """画面の初期状態で子孫も隠し、祖先の linkId を JS へ渡す"""
        from jinja2 import Environment, FileSystemLoader

        q = {
            "item": [
                {
                    "linkId": "g",
                    "type": "group",
                    "enableWhen": [{"question": "x", "operator": "exists", "answerBoolean": True}],
                    "item": [{"linkId": "child id", "type": "string", "required": True}],
                }
            ]
        }
        fields = FormGenerator(q).generate_form_fields()
        env = Environment(loader=FileSystemLoader("kawasemi/web/templates"), autoescape=True)
        src = env.loader.get_source(env, "form.html")[0]
        start = src.index("{% for field in fields %}")
        # 項目ループの閉じタグ（行頭インデント8桁。内側の選択肢ループは深い）
        end = src.index("\n        {% endfor %}", start) + len("\n        {% endfor %}")
        html = env.from_string(src[start:end]).render(fields=fields, prefill_data={})
        child = html[html.index('data-link-id="child id"') :]
        child = child[: child.index("</div>")]
        assert 'data-enable-when="true"' in child
        assert "data-enable-ancestors='[\"g\"]'" in child
        tag = child[child.index("<input") :]
        assert "required" not in tag[: tag.index(">")]  # 祖先が無効なうちは必須にしない
