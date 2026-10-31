"""動的フォーム生成エンジン"""

from typing import Any


class FormGenerator:
    """Questionnaire から HTML フォームを生成するクラス"""

    def __init__(
        self,
        questionnaire: dict[str, Any],
        valueset_map: dict[str, list[dict[str, str]]] | None = None,
    ):
        self.questionnaire = questionnaire
        self.items = questionnaire.get("item", [])
        # key: answerValueSet URL, value: [{value, display}, ...]
        self.valueset_map = valueset_map or {}

    def generate_form_fields(self) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        self._collect_fields(self.items, fields)
        return fields

    def _collect_fields(
        self,
        items: list[dict[str, Any]],
        fields: list[dict[str, Any]],
        ancestors: tuple[str, ...] = (),
        parent_text: str | None = None,
    ) -> None:
        for item in items:
            field = self._generate_field(item)
            if field:
                if parent_text:
                    # **最も近い祖先の text を持たせる。** 「数値(mg/dL)」のように、項目の text だけでは
                    # 何の値か分からない様式がある（感染症の様式の検査値は親グループ名が「CRP」）。
                    # 所在探索のクエリと LLM への提示が使う（画面の表示は変えない）
                    field["parent_text"] = parent_text
                if ancestors:
                    # **祖先の enableWhen を子孫へ伝える。** FHIR R4 では無効な item の子孫も
                    # 無効である。画面は平坦に並べるので、伝えないと「〜の場合の詳細」欄が
                    # 条件と無関係に出続ける（入れ子の深い様式では大量の欄になる）
                    field["enable_when_ancestors"] = list(ancestors)
                fields.append(field)
            # 子アイテムを再帰的に展開する。**group に限らない**——FHIR は質問項目（choice
            # など）の下にも子を置ける（「はい」のときに詳細を聞く形）。group だけを辿ると、
            # 感染症の様式で項目の大半が画面から落ちた
            if "item" in item:
                child_ancestors = (
                    (*ancestors, item["linkId"]) if "enableWhen" in item else ancestors
                )
                child_parent = (item.get("text") or "").strip() or parent_text
                self._collect_fields(item["item"], fields, child_ancestors, child_parent)

    def _generate_field(self, item: dict[str, Any]) -> dict[str, Any] | None:
        link_id = item.get("linkId")
        text = item.get("text", "")
        item_type = item.get("type", "string")
        required = item.get("required", False)

        field: dict[str, Any] = {
            "link_id": link_id,
            "text": text,
            "type": item_type,
            "required": required,
            "input_type": self._map_type_to_input(item_type),
        }

        if "answerOption" in item:
            field["options"] = [_option(opt) for opt in item.get("answerOption", [])]
        elif "answerValueSet" in item:
            vs_url = item["answerValueSet"]
            field["options"] = self.valueset_map.get(vs_url, [])

        if "enableWhen" in item:
            field["enable_when"] = self._parse_enable_when(item)

        return field

    def _parse_enable_when(self, item: dict[str, Any]) -> dict[str, Any]:
        enable_when = item.get("enableWhen", [])
        enable_behavior = item.get("enableBehavior", "all")

        conditions = []
        for condition in enable_when:
            parsed: dict[str, Any] = {
                "question": condition.get("question"),
                "operator": condition.get("operator", "="),
            }

            if "answerBoolean" in condition:
                parsed["answer_type"] = "boolean"
                parsed["answer_value"] = condition["answerBoolean"]
            elif "answerString" in condition:
                parsed["answer_type"] = "string"
                parsed["answer_value"] = condition["answerString"]
            elif "answerInteger" in condition:
                parsed["answer_type"] = "integer"
                parsed["answer_value"] = condition["answerInteger"]
            elif "answerDecimal" in condition:
                parsed["answer_type"] = "decimal"
                parsed["answer_value"] = condition["answerDecimal"]
            elif "answerDate" in condition:
                parsed["answer_type"] = "date"
                parsed["answer_value"] = condition["answerDate"]
            elif "answerCoding" in condition:
                parsed["answer_type"] = "coding"
                parsed["answer_value"] = condition["answerCoding"].get("code")
            else:
                parsed["answer_type"] = "exists"
                parsed["answer_value"] = True

            conditions.append(parsed)

        return {
            "conditions": conditions,
            "behavior": enable_behavior,
        }

    def _map_type_to_input(self, fhir_type: str) -> str:
        mapping = {
            "boolean": "checkbox",
            "decimal": "number",
            "integer": "number",
            "date": "date",
            "dateTime": "datetime-local",
            "time": "time",
            "string": "text",
            "text": "textarea",
            "choice": "select",
            "open-choice": "select",
            "display": "display",
            "group": "group",
        }
        return mapping.get(fhir_type, "text")

    def get_title(self) -> str:
        return str(self.questionnaire.get("title", "質問票"))

    def get_description(self) -> str:
        return str(self.questionnaire.get("description", ""))

    def get_enable_when_config(self) -> dict[str, Any]:
        config = {}
        fields = self.generate_form_fields()
        for field in fields:
            if "enable_when" in field:
                config[field["link_id"]] = field["enable_when"]
        return config


def _option(opt: dict[str, Any]) -> dict[str, Any]:
    """answerOption を {value, display} にする。

    **code の無い Coding は display を値にする。** display だけの Coding は FHIR として有効で
    （感染症の様式の「その他」「不明」など）、値が None だと `value="None"` の選択肢に
    なり、"None" という文字列が回答に残る。値は None と空文字列を飛ばして先頭から選ぶ。
    """
    coding = opt.get("valueCoding") or {}

    def first(*xs: Any) -> Any:
        return next((x for x in xs if x is not None and x != ""), None)

    value = first(coding.get("code"), opt.get("valueString"), opt.get("valueInteger"))
    display = first(coding.get("display"), opt.get("valueString"), opt.get("valueInteger"))
    return {"value": first(value, display), "display": first(display, value)}
