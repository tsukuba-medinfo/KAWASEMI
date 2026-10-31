"""DSLInterpreter の LLM op（llm_extract / llm_judge）を実 Ollama 無しで決定的に検証する。

実 Ollama 依存の e2e/vision テストは CI で skip されるため、LLM 関与パスは
`_ollama_client.chat` をモックしてこの統合テストで CI 必須に固定する。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import executor as executor_mod
from kawasemi.ai_input.compiler import COMPILE_SYSTEM_PROMPT
from kawasemi.ai_input.executor import (
    _JUDGE_INSTRUCTION,
    _JUDGE_NO,
    _JUDGE_SCHEMA,
    _JUDGE_UNKNOWN,
    _JUDGE_YES,
    DSLInterpreter,
    _exc_msg,
    _judge_answer,
    _truthy,
)
from kawasemi.ai_input.models import FailureCode, InputPlan


def _plan(link_id: str, program: dict) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id=link_id, program=program, status="confirmed")


def _chat_response(content: str) -> SimpleNamespace:
    """ollama AsyncClient.chat の戻り値形（参照されるのは message.content のみ）。"""
    return SimpleNamespace(message=SimpleNamespace(content=content))


@pytest.mark.asyncio
async def test_llm_extract_fills_value_and_records_trace(db_session: AsyncSession) -> None:
    """llm_extract: モデル出力が prefill に入り、trace に llm.prompt/raw_output が確定保存される。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("男性"))
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "患者 {{patient_id}} の性別を答えよ"}],
        "return": "$v",
    }
    prefill, evidence = await interp.execute([_plan("gender", program)], "P1", db_session)

    assert prefill["gender"] == "男性"
    trace = evidence["gender"]["trace"]
    llm_step = next(t for t in trace if t.get("op") == "llm_extract")
    assert llm_step["llm"]["raw_output"] == "男性"
    # プロンプトに patient_id が展開されている（GxP 証跡）
    assert "P1" in llm_step["llm"]["prompt"]
    interp._ollama_client.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_trace_records_the_provider(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """実行ロールの trace に推論プロバイダが残ること。

    設計ロールは `ai_input_design_llm_calls.provider` に残す。実行ロールは DB ではなく
    evidence trace に残すので、**同じ粒度の記録をこちらにも持つ**。モデル名だけでは実装が
    特定できず（`OLLAMA_MODEL` は環境変数で上書きでき、モデルのダイジェストにも現れない）、
    差し替え後の判定が「どのエンジンで得られたか」を答えられなくなる。

    **値は client から取り、`__init__` で確定する。** op ごとに再計算すると、生成が成功した
    後の trace 組み立てに失敗の芽を持ち込む（実測: 例外が成功した抽出値と生出力を
    捨てた）。したがって「client 由来である」ことは**構築時に別の client を渡して**確かめる。
    """
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "患者 {{patient_id}} の性別を答えよ"}],
        "return": "$v",
    }

    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("男性"))
    _, evidence = await interp.execute([_plan("gender", program)], "P1", db_session)
    step = next(t for t in evidence["gender"]["trace"] if t.get("op") == "llm_extract")
    assert step["llm"]["provider"] == "ollama"

    # 名乗る client を**構築時に**渡す（プロバイダの選択は client に閉じている）。
    class _Named:
        provider_name = "stub"

        def __init__(self, **_: object) -> None:
            self.chat = AsyncMock(return_value=_chat_response("男性"))

    monkeypatch.setattr(executor_mod.ollama, "AsyncClient", _Named)
    named = DSLInterpreter()
    _, evidence = await named.execute([_plan("gender2", program)], "P1", db_session)
    step = next(t for t in evidence["gender2"]["trace"] if t.get("op") == "llm_extract")
    assert step["llm"]["provider"] == "stub"


@pytest.mark.asyncio
async def test_llm_extract_empty_output_not_prefilled(db_session: AsyncSession) -> None:
    """llm_extract: モデルが空応答なら値は出さず（prefill にキーを入れない）trace は残す。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response(""))
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "所見は？"}],
        "return": "$v",
    }
    prefill, evidence = await interp.execute([_plan("empty", program)], "P1", db_session)
    assert "empty" not in prefill
    assert evidence["empty"]["trace"]  # trace 自体は記録される
    # **空応答には失敗コードが付く**。`undetermined = not raw_text` を
    # `False` へ潰す退行は、これが無いと**挙動テストでは1件も落ちない**。
    # 向きは自動充足率ではなく**失敗理由内訳が `unclassified` へ落ちる**方向である
    step = next(t for t in evidence["empty"]["trace"] if t.get("op") == "llm_extract")
    assert step["failure_code"] == FailureCode.EXTRACTION_LOW_CONFIDENCE


@pytest.mark.asyncio
async def test_llm_judge_normalizes_truthy_to_bool(db_session: AsyncSession) -> None:
    """llm_judge: 真値トークン（「はい」）が bool True に正規化される。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("はい"))
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    prefill, _ = await interp.execute([_plan("flag", program)], "P1", db_session)
    assert prefill["flag"] is True


@pytest.mark.asyncio
async def test_llm_judge_negative_token_is_false(db_session: AsyncSession) -> None:
    """llm_judge: 既知の否定トークン（「いいえ」）は False。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("いいえ"))
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    prefill, _ = await interp.execute([_plan("flag2", program)], "P1", db_session)
    assert prefill["flag2"] is False


@pytest.mark.asyncio
async def test_llm_judge_empty_output_is_not_a_false_judgement(db_session: AsyncSession) -> None:
    """llm_judge: **空応答は「False という判定」ではなく「判定が得られなかった」**。

    以前は `_truthy(None)` の False が出力になり、`is_empty_value(False)` は偽なので
    「値あり＝充足」として自動充足率の分子に入り、失敗コードも付かなかった。取得困難度を
    **低く見せる**バイアスで、分母を過大に数えるバイアスと同じ向きなので相殺しない。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response(""))
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    prefill, evidence = await interp.execute([_plan("judge_empty", program)], "P1", db_session)

    # 値は出さない（False を「判定できた」と数えない）
    assert "judge_empty" not in prefill
    trace = evidence["judge_empty"]["trace"]
    judge_step = next(t for t in trace if t.get("op") == "llm_judge")
    assert judge_step["output"] is None
    assert judge_step["failure_code"] == FailureCode.EXTRACTION_LOW_CONFIDENCE
    # 項目単位の失敗理由も低信頼になる（unclassified に落ちない）
    assert evidence["judge_empty"]["failure_code"] == FailureCode.EXTRACTION_LOW_CONFIDENCE


@pytest.mark.asyncio
async def test_llm_judge_false_is_still_a_value(db_session: AsyncSession) -> None:
    """**「いいえ」による False は従来どおり「判定できた」**こと（空応答の扱いの修正で壊さない）。

    空応答と False を区別できていることを、同じテストで両方向から見る。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("いいえ"))
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    prefill, evidence = await interp.execute([_plan("judge_false", program)], "P1", db_session)
    assert prefill["judge_false"] is False
    judge_step = next(t for t in evidence["judge_false"]["trace"] if t.get("op") == "llm_judge")
    assert "failure_code" not in judge_step


@pytest.mark.asyncio
async def test_llm_judge_empty_output_still_takes_the_negative_branch(
    db_session: AsyncSession,
) -> None:
    """空応答でも**実行の分岐は従来どおり偽側**に進むこと（空応答の扱いの修正で挙動を変えていない）。

    変えたのは「充足したか」の数え方だけで、guard / branch から見た真偽ではない。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response(""))
    program = {
        "steps": [
            {"op": "llm_judge", "id": "j", "prompt": "入院症例か？"},
            {"op": "branch", "id": "v", "when": "$j", "then": "1", "else_branch": "2"},
        ],
        "return": "$v",
    }
    prefill, _ = await interp.execute([_plan("branchy", program)], "P1", db_session)
    assert prefill["branchy"] == "2"


@pytest.mark.asyncio
async def test_llm_judge_empty_output_is_empty_for_compare(db_session: AsyncSession) -> None:
    """空応答は `compare is_empty` で**真**になること（空応答の扱いの修正で変わる経路）。

    guard / branch から見た真偽は変わらないが、`compare` は変わる（False は空ではないが
    None は空）。フォールバック判別（`is_empty` → guard 付き取得）に判定 op の出力を
    通すプランでは、空応答時に代替候補が走るようになる。**意味としてはこちらが正しい**
    （判定できていないのだから「空」）。変わること自体を固定しておく。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response(""))
    program = {
        "steps": [
            {"op": "llm_judge", "id": "j", "prompt": "入院症例か？"},
            {"op": "compare", "id": "empty", "left": "$j", "cmp": "is_empty"},
            {
                "op": "branch",
                "id": "v",
                "when": "$empty",
                "then": "代替候補",
                "else_branch": "本命",
            },
        ],
        "return": "$v",
    }
    prefill, _ = await interp.execute([_plan("cmp", program)], "P1", db_session)
    assert prefill["cmp"] == "代替候補"


@pytest.mark.asyncio
async def test_llm_judge_false_is_not_empty_for_compare(db_session: AsyncSession) -> None:
    """「いいえ」による False は `compare is_empty` で**偽**のままであること。

    空応答と False の区別が `compare` の側でも保たれていることを見る。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("いいえ"))
    program = {
        "steps": [
            {"op": "llm_judge", "id": "j", "prompt": "入院症例か？"},
            {"op": "compare", "id": "empty", "left": "$j", "cmp": "is_empty"},
            {
                "op": "branch",
                "id": "v",
                "when": "$empty",
                "then": "代替候補",
                "else_branch": "本命",
            },
        ],
        "return": "$v",
    }
    prefill, _ = await interp.execute([_plan("cmp2", program)], "P1", db_session)
    assert prefill["cmp2"] == "本命"


@pytest.mark.asyncio
async def test_llm_judge_empty_output_changes_value_consuming_paths(
    db_session: AsyncSession,
) -> None:
    """空応答は**値として消費する経路**では「値が無い」になること（空応答の扱いの修正の影響範囲）。

    `guard` / `branch` の真偽は変わらないが、`aggregate count`（1→0）・`transform`（写像を
    通らない）・`compare` の数値比較は変わる。**変わること自体を固定する**——申告と実装が
    ずれると、どの実行記録を混ぜてよいかの判断ができない。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response(""))

    # aggregate count: False なら1件、None なら0件
    count_program = {
        "steps": [
            {"op": "llm_judge", "id": "j", "prompt": "所見あり？"},
            {"op": "aggregate", "id": "n", "source": "$j", "func": "count"},
        ],
        "return": "$n",
    }
    prefill, _ = await interp.execute([_plan("cnt", count_program)], "P1", db_session)
    assert prefill["cnt"] == 0

    # transform: None は写像を通らない（False なら "False" キーを引ける形）
    map_program = {
        "steps": [
            {"op": "llm_judge", "id": "j", "prompt": "所見あり？"},
            {
                "op": "transform",
                "id": "v",
                "input": "$j",
                "kind": "code_map",
                "map": {"False": "2", "True": "1"},
            },
        ],
        "return": "$v",
    }
    prefill, _ = await interp.execute([_plan("mapped", map_program)], "P1", db_session)
    assert "mapped" not in prefill, "写像を通って値が出ている（None のはず）"

    # compare の数値比較: False は 0.0 として比較されるが None は文字列比較へ縮退する
    gt_program = {
        "steps": [
            {"op": "llm_judge", "id": "j", "prompt": "所見あり？"},
            {"op": "compare", "id": "gt", "left": "$j", "cmp": ">", "right": 0},
        ],
        "return": "$gt",
    }
    prefill, _ = await interp.execute([_plan("gt", gt_program)], "P1", db_session)
    assert prefill["gt"] is True, "数値比較の縮退（False なら偽、None なら真）が起きていない"


@pytest.mark.asyncio
async def test_llm_judge_whitespace_only_output_is_empty(db_session: AsyncSession) -> None:
    """空白のみの応答（`"   "`）も空扱いであること（strip 後に空になる）。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("   "))
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    prefill, evidence = await interp.execute([_plan("ws", program)], "P1", db_session)
    assert "ws" not in prefill
    judge_step = next(t for t in evidence["ws"]["trace"] if t.get("op") == "llm_judge")
    assert judge_step["failure_code"] == FailureCode.EXTRACTION_LOW_CONFIDENCE


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("はい", True),
        ("あり", True),
        ("該当", True),
        ("陽性", True),
        ("yes", True),
        ("1", True),
        ("いいえ", False),
        ("なし", False),
        ("非該当", False),
        ("陰性", False),
        ("no", False),
        ("0", False),
        # **structured output の語彙**。`format` を渡しているのでこちらが本経路。
        ('{"answer": "yes"}', True),
        ('{"answer": "no"}', False),
        ('{"answer": "YES"}', True),  # 大小は問わない
        ("YES", True),  # 副経路（トークン表）でも大小を問わない
    ],
)
@pytest.mark.asyncio
async def test_llm_judge_token_table(answer: str, expected: bool, db_session: AsyncSession) -> None:
    """**判定 op の答えを決める語彙**を挙動で固定する。

    空応答の扱いの修正が直したのは「空応答」の1ケースで、「どの文字列が『はい』か」は
    `_TRUE_TOKENS` / `_FALSE_TOKENS` が決めている。表から1語落とす退行はここで止める。

    structured output（`_JUDGE_SCHEMA`）を付けたので、**本経路は JSON の
    `answer`** である。トークン表は **`format` を無視するモデルのため**に残してあり、
    **そちらも同じ真偽を返す**ことをここで固定する（「過去の生応答の読み直し」も理由に
    挙げていたが、その呼び出し側は実装に無い）。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response(answer))
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    prefill, _ = await interp.execute([_plan(f"tok_{answer}", program)], "P1", db_session)
    assert prefill[f"tok_{answer}"] is expected


@pytest.mark.parametrize(
    "answer",
    [
        # 以前は `_truthy` の `bool(s)` で**すべて True（該当あり）**になっていた
        "不明",
        "判定できません",
        "情報が不足しています",
        "たぶん該当",
        # structured output に従った「判定不能」
        '{"answer": "unknown"}',
        # スキーマに従っているが `answer` が無い／型が違う（キーの省略）
        '{"answer": null}',
        # **非文字列の `answer` でも推測しない**。
        # 「None だけ None、他は `_truthy(answer)`」へ緩めると `{"answer": true}` が
        # **充足に戻る**（閉じた fail-open の再開。向きは困難度を過小に見せる）。
        # この行が無いと、その変更は挙動テストで1件も落ちない
        '{"answer": true}',
        '{"answer": 1}',
        "{}",
        # 語彙外の値（列挙を増やす前でも fail-closed であること）
        '{"answer": "maybe"}',
        # **JSON 経路の語彙外に、トークン表の肯定語を置いた形**。
        # 「モデルが日本語で返したときも読めるように」とトークン表へフォールバックさせる
        # 変更は、**`{"answer": "該当"}` を充足に戻す**——判定不能を該当ありにしない修正が閉じた向きそのものである。
        # この1件が無いと、その変更が挙動テストで1件も落ちない（変異が実測で生き残った）
        '{"answer": "該当"}',
        '{"answer": "はい"}',
        # JSON として壊れている（**肯定の語を含んでいても推測しない**）
        '{"answer": "yes"',
    ],
)
@pytest.mark.asyncio
async def test_llm_judge_undeterminable_answer_is_not_a_positive_judgement(
    answer: str, db_session: AsyncSession
) -> None:
    """**判定できない応答は「該当あり」ではない**。

    以前は既知トークンに無い非空応答を `_truthy` の `bool(s)` で True に倒していた。
    コメントは「医療判定で曖昧語を陽性側へ寄せる」＝臨床的な安全側と説明していたが、
    **失敗コードも付かないので人にも回らず、自動充足率の分子に静かに入る**。空応答の扱いで直した
    「空応答を False という判定として数える」のと**同じ向き**（取得困難度を低く見せる）の
    バイアスである。

    直した後は空応答と同じ扱いになる——値を出さず、`extraction_low_confidence` を付けて
    人へ回す。**「判定できない」ことが分かる状態が、臨床的にも測定としても安全側である。**
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response(answer))
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    link_id = f"und_{answer}"
    prefill, evidence = await interp.execute([_plan(link_id, program)], "P1", db_session)

    # 値は出さない（True を「判定できた」と数えない）
    assert link_id not in prefill
    judge_step = next(t for t in evidence[link_id]["trace"] if t.get("op") == "llm_judge")
    assert judge_step["output"] is None
    # **非空の応答でも失敗コードが付く**（`not raw_text` 条件では拾えない）
    assert judge_step["failure_code"] == FailureCode.EXTRACTION_LOW_CONFIDENCE
    assert evidence[link_id]["failure_code"] == FailureCode.EXTRACTION_LOW_CONFIDENCE
    # 生応答は監査のために残る（何を返したモデルを判定不能と数えたかが追える）
    assert judge_step["llm"]["raw_output"] == answer


@pytest.mark.asyncio
async def test_llm_judge_determined_answer_gets_no_failure_code(db_session: AsyncSession) -> None:
    """**正常系では判定不能の扱いが発火しない**こと（`yes` / `no` は「判定できた」）。

    判定不能へ倒す規則は、正常な応答まで巻き込むと「判定できる項目が1つも無い」状態を
    作る（取得困難度を**過大に**見せる。判定不能を該当ありにしない修正が直そうとしている向きの逆）。両方向を測る。
    """
    interp = DSLInterpreter()
    for link_id, answer, expected in (
        ("det_yes", '{"answer": "yes"}', True),
        ("det_no", '{"answer": "no"}', False),
    ):
        interp._ollama_client.chat = AsyncMock(return_value=_chat_response(answer))
        program = {
            "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
            "return": "$j",
        }
        prefill, evidence = await interp.execute([_plan(link_id, program)], "P1", db_session)
        assert prefill[link_id] is expected
        judge_step = next(t for t in evidence[link_id]["trace"] if t.get("op") == "llm_judge")
        assert "failure_code" not in judge_step
        assert evidence[link_id].get("failure_code") is None


@pytest.mark.asyncio
async def test_llm_judge_constrains_the_answer_vocabulary(db_session: AsyncSession) -> None:
    """判定 op が**語彙を閉じた状態で**呼ばれ、それが trace に残る。

    スキーマも指示も「付けたつもり」で結線が切れていると、モデルは自由文で答え続け、
    その全部が判定不能になる（判定不能を該当ありにしない修正が**取得困難度を過大に見せる**方向へ反転する）。
    **結線そのものを測る**。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response('{"answer": "yes"}'))
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    _prefill, evidence = await interp.execute([_plan("wired", program)], "P1", db_session)

    kwargs = interp._ollama_client.chat.await_args.kwargs
    # ① structured output が渡っている（語彙は3値・`answer` は必須）
    assert kwargs["format"] == _JUDGE_SCHEMA
    assert kwargs["format"]["properties"]["answer"]["enum"] == ["yes", "no", "unknown"]
    assert kwargs["format"]["required"] == ["answer"]
    # **`type` もリテラルで pin する**。`kwargs["format"] ==
    # _JUDGE_SCHEMA` は**同一オブジェクト比較**なので何も固定していなかった。
    # `"object"` でなければ `{"answer": ...}` を強制できず、**すべての応答が判定不能になる**
    # （困難度を過大に見せる向き）。この assert が無いと挙動テストでは止まらなかった
    assert kwargs["format"]["type"] == "object"
    assert kwargs["format"]["properties"]["answer"]["type"] == "string"
    # ② 指示が本文に付いている（選択肢を与えるだけでは「判定不能」が使われない）
    sent_prompt = kwargs["messages"][0]["content"]
    assert "所見あり？" in sent_prompt  # 元のプロンプトを消していない
    assert sent_prompt.endswith(_JUDGE_INSTRUCTION)
    assert "unknown" in _JUDGE_INSTRUCTION and "推測" in _JUDGE_INSTRUCTION
    # ③ スキーマが trace に確定保存される（測定パラメータなので options と同じ扱い）
    judge_step = next(t for t in evidence["wired"]["trace"] if t.get("op") == "llm_judge")
    assert judge_step["llm"]["format"] == _JUDGE_SCHEMA
    assert judge_step["llm"]["prompt"] == sent_prompt


@pytest.mark.asyncio
async def test_llm_extract_answer_vocabulary_is_not_constrained(db_session: AsyncSession) -> None:
    """**値を取り出す op には語彙を付けない**。

    `llm_extract` に判定の語彙を付けると、取り出せる値が `yes` / `no` / `unknown` だけに
    なる。判定 op だけに付いていることを、両方の op を同じ形で見て固定する。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("男性"))
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "性別を答えよ"}],
        "return": "$v",
    }
    prefill, evidence = await interp.execute([_plan("free", program)], "P1", db_session)

    assert prefill["free"] == "男性"  # 語彙外の値がそのまま取れる
    kwargs = interp._ollama_client.chat.await_args.kwargs
    assert kwargs["format"] is None
    assert _JUDGE_INSTRUCTION not in kwargs["messages"][0]["content"]
    step = next(t for t in evidence["free"]["trace"] if t.get("op") == "llm_extract")
    assert "format" not in step["llm"]
    # **抽出できた値に失敗コードを付けない**。判定不能の判定を
    # `llm_extract` にも当てる形（`undetermined = not raw_text or _judge_answer(...) is None`）は
    # **正常に抽出できた値へ `extraction_low_confidence` を付ける**＝困難度を**過大**に見せる
    # 向きの退行だが、この assert が無いと挙動テストでは1件も落ちなかった
    assert "failure_code" not in step, step


@pytest.mark.asyncio
async def test_branch_truthiness_is_unchanged_by_the_judge_rule(db_session: AsyncSession) -> None:
    """`branch.when` / `guard` の真偽化は判定不能の導入で**変えていない**こと。

    `_truthy` は2値でなければ分岐が決まらないので、判定不能を持ち込めない（`_judge_answer`
    を別関数にした理由）。`_truthy` に判定不能を混ぜ込む変更が入れば、ここが落ちる。
    """
    assert _truthy("たぶん該当") is True  # 未知の文字列は truthy（branch の既定は変えない）
    assert _truthy("いいえ") is False
    # 判定 op の応答は別の規則で読む（同じ入力で違う答えになることを明示する）
    assert _judge_answer("たぶん該当") is None


def test_exc_msg_falls_back_to_type_name_for_empty_message() -> None:
    """str(e) が空になる例外（httpx.ReadTimeout 等）でも型名が残る。"""
    assert str(TimeoutError()) == ""  # 前提: メッセージ無し例外は str が空
    assert _exc_msg(TimeoutError()) == "TimeoutError"
    # 非空メッセージは型名を前置して保持する
    assert _exc_msg(ValueError("boom")) == "ValueError: boom"


@pytest.mark.asyncio
async def test_llm_step_records_nonempty_error_on_empty_message_exception(
    db_session: AsyncSession,
) -> None:
    """LLM 呼び出しが空メッセージ例外で失敗しても evidence の error が無言にならない。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(side_effect=TimeoutError())
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "所見を要約せよ"}],
        "return": "$v",
    }
    prefill, evidence = await interp.execute([_plan("summary", program)], "P1", db_session)

    assert "summary" not in prefill  # 失敗項目は補完されない
    errors = [t.get("error") for t in evidence["summary"]["trace"] if t.get("error")]
    assert errors and all(e for e in errors)  # error は非空
    assert any("TimeoutError" in e for e in errors)


def test_the_compile_prompt_does_not_invite_classification_judges() -> None:
    """コンパイラのプロンプトが `llm_judge` を**真偽専用**として説明していること。

    「誤った使い方を勧めるプロンプトを残すと、判定不能を該当ありにしない改訂が困難度を
    **過大に**見せる向きへ反転する」。説明を以前の形
    （「真偽/分類を判定（true/false 等）。」）へ戻すミューテーションが**挙動テストを素通り**
    していたので、ここで性質として固定する。

    **語の在否ではなく対応の方向と構造を見る**（`test_runbook_branch.py` と同じ手法）。

    **上限は「語の羅列」より広い**。この4つの assert を
    **すべて満たしたまま「分類にも使ってよい」と勧める通る日本語**が書ける
    （「真偽または分類を判定する（yes / no / unknown 等）。『分類には使えない』という制限は無い」）。
    つまり**位置関係で固定できるのは語の並びだけで、意味の反転は止まらない**。
    そこから先はテストでは支えられず、プロンプトの変更を読むレビューに委ねる。
    """
    line = next(
        ln for ln in COMPILE_SYSTEM_PROMPT.splitlines() if ln.lstrip().startswith("- llm_judge:")
    )
    # ① 「分類も判定できる」と読める書き方に戻っていない
    assert "真偽/分類" not in COMPILE_SYSTEM_PROMPT
    assert "分類には使えない" in COMPILE_SYSTEM_PROMPT
    # ② `llm_judge` の説明の**中**で真偽に限定している（別の節に書いても効かない）
    i_judge = COMPILE_SYSTEM_PROMPT.index("- llm_judge:")
    i_compare = COMPILE_SYSTEM_PROMPT.index("- compare:", i_judge)
    judge_block = COMPILE_SYSTEM_PROMPT[i_judge:i_compare]
    assert "真偽" in judge_block and "分類には使えない" in judge_block, judge_block
    # ③ **区分や値が欲しいときの代替**（llm_extract）を同じブロックで指している。
    #    代替を書かないと、モデルは分類を llm_judge で書くしかない
    assert judge_block.index("分類には使えない") < judge_block.index("llm_extract"), judge_block
    # ④ 応答語彙が3値であることをブロックの中で述べている（実装と説明の対応）
    for token in (_JUDGE_YES, _JUDGE_NO, _JUDGE_UNKNOWN):
        assert token in judge_block, (token, judge_block)
    assert line in judge_block


def test_the_judge_instruction_points_undeterminable_at_unknown() -> None:
    """判定の指示が **3値の対応の向き**を固定していること。

    以前の assert は `"unknown" in _JUDGE_INSTRUCTION and "推測" in ...` という
    **語の在否だけ**だったので、「迷ったら推測で yes / no を選び、unknown は使わないこと」へ
    **意味を反転**させても素通りした（語は両方残る）。それは判定不能を該当ありにしない修正が閉じた fail-open を
    プロンプト側から開け直す変更である。

    **どの語がどの選択肢に結び付いているか**を、行の対応で固定する。

    **これも上限がある**。「unknown: 与えられた情報では判断できないときでも
    使わない（『推測で yes / no を選ばない』という原則は適用しない）」は、bullets の対応と
    「推測で yes / no を選ばない」の部分文字列を**満たしたまま意味が反転する**。
    そこから先はテストでは支えられず、`_JUDGE_INSTRUCTION` の変更を読むレビューに委ねる。
    """
    lines = [ln.strip() for ln in _JUDGE_INSTRUCTION.splitlines() if ln.strip()]
    bullets = {ln.split(":", 1)[0].lstrip("- "): ln.split(":", 1)[1] for ln in lines if ":" in ln}
    # 3値それぞれに説明が付いている（1つでも欠けるとモデルはその選択肢を使わない）
    assert set(bullets) >= {_JUDGE_YES, _JUDGE_NO, _JUDGE_UNKNOWN}, bullets
    # **向き**: 該当する→yes / 該当しない→no / 判断できない→unknown
    assert "該当する" in bullets[_JUDGE_YES] and "判断できない" not in bullets[_JUDGE_YES]
    assert "該当しない" in bullets[_JUDGE_NO] and "判断できない" not in bullets[_JUDGE_NO]
    assert "判断できない" in bullets[_JUDGE_UNKNOWN]
    # **推測の禁止は unknown 側に付いている**（yes / no へ寄せる指示に反転していない）
    assert "推測" in bullets[_JUDGE_UNKNOWN]
    assert "推測で yes / no を選ばない" in bullets[_JUDGE_UNKNOWN]
    # 指示の本文が JSON の形も示している（スキーマだけでは形を外すモデルがある）
    assert '{"answer": ...}' in _JUDGE_INSTRUCTION


@pytest.mark.asyncio
async def test_llm_extract_whitespace_only_output_gets_a_failure_code(
    db_session: AsyncSession,
) -> None:
    """**空白のみの応答にも失敗コードが付く**こと。

    空応答のテストが閉じたのは `content=""` 側だけで、**空白のみ側の対照が無かった**——
    `undetermined = not raw_text` を `raw_text is None` へ緩める形が挙動テストで1件も落ちず、
    `"   "` の応答で `extraction_low_confidence` が消えて失敗理由内訳が `unclassified` へ移る。
    判定 op には同型のテストがあるので、非判定側にも置いて対称にする。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("   "))
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "所見は？"}],
        "return": "$v",
    }
    prefill, evidence = await interp.execute([_plan("ws_extract", program)], "P1", db_session)

    # 値は空（`is_empty_value` が不充足と数える）。**空白のみは strip 後に空文字になる**ので、
    # `prefill` にはキーが残る（`content=""` の場合と挙動が違う点も固定する）
    assert prefill.get("ws_extract") == ""
    step = next(t for t in evidence["ws_extract"]["trace"] if t.get("op") == "llm_extract")
    assert step["failure_code"] == FailureCode.EXTRACTION_LOW_CONFIDENCE
    assert evidence["ws_extract"]["failure_code"] == FailureCode.EXTRACTION_LOW_CONFIDENCE


# --------------------------------------------------------------------------- #
# モデルのダイジェスト（設計ロールの側は別に記録している）
# --------------------------------------------------------------------------- #

_PROGRAM_ONE_EXTRACT = {
    "steps": [{"op": "llm_extract", "id": "v", "prompt": "患者 {{patient_id}} の性別を答えよ"}],
    "return": "$v",
}


def _llm_trace(evidence: dict, link_id: str) -> dict:
    step = next(t for t in evidence[link_id]["trace"] if t.get("op") == "llm_extract")
    return step["llm"]


@pytest.mark.asyncio
async def test_llm_trace_records_the_model_digest(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """実行のたびに evidence trace にダイジェストが残る。

    名前（タグ）は同じでも中身が差し替わる（`ollama pull`、同名タグの作り直し）ので、
    `llm.model` だけでは「どの重みで判定したか」を後から答えられない。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("男性"))
    seen: list[tuple[object, str]] = []

    async def fake_digest(client: object, model: str) -> tuple[str | None, str | None]:
        seen.append((client, model))
        return "ab" * 32, None

    monkeypatch.setattr(executor_mod, "model_digest_of", fake_digest)
    _, evidence = await interp.execute([_plan("g", _PROGRAM_ONE_EXTRACT)], "P1", db_session)

    llm = _llm_trace(evidence, "g")
    assert llm["model_digest"] == "ab" * 32
    assert llm["model_digest_unavailable"] is None
    # 引いたのは**実際に呼んだモデル名**（記録した `llm.model` と同じ）
    assert seen == [(interp._ollama_client, llm["model"])]


@pytest.mark.asyncio
async def test_llm_trace_records_why_the_digest_is_missing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取れなかったときは理由つきの null。「記録していない」とは区別できる。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("男性"))

    async def no_digest(client: object, model: str) -> tuple[str | None, str | None]:
        return None, "ollama の一覧に無かった"

    monkeypatch.setattr(executor_mod, "model_digest_of", no_digest)
    _, evidence = await interp.execute([_plan("g", _PROGRAM_ONE_EXTRACT)], "P1", db_session)

    llm = _llm_trace(evidence, "g")
    assert llm["model_digest"] is None
    assert llm["model_digest_unavailable"] == "ollama の一覧に無かった"


@pytest.mark.asyncio
async def test_a_failing_digest_lookup_does_not_break_the_execution(
    db_session: AsyncSession,
) -> None:
    """ダイジェストの取得が壊れ・固まる形でも、**抽出値と生出力は捨てない**。

    実際の `model_digest_of` を使う（差し替えない）。一覧を返さない client と、
    投げ続ける client の2形で、実行が成功し、理由つきの null が残ることを確かめる。
    """
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("男性"))

    async def broken_list() -> None:
        raise RuntimeError("推論サーバに繋がらない")

    interp._ollama_client.list = broken_list  # type: ignore[method-assign]
    prefill, evidence = await interp.execute([_plan("g", _PROGRAM_ONE_EXTRACT)], "P1", db_session)

    assert prefill["g"] == "男性", "ダイジェストの失敗が抽出値を捨てた"
    llm = _llm_trace(evidence, "g")
    assert llm["raw_output"] == "男性"
    assert llm["model_digest"] is None
    assert llm["model_digest_unavailable"], "理由が残っていない"


@pytest.mark.asyncio
async def test_vision_trace_records_the_model_digest_of_the_vision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vision OCR の trace にもダイジェストが残る。

    vision のモデルはテキスト側と別のタグでありうる（`OLLAMA_VISION_MODEL`）ので、
    引くのは**実際に呼んだ vision のモデル名**である。
    """
    from pathlib import Path

    from kawasemi.core.config import settings

    monkeypatch.setattr(settings, "ollama_vision_model", "vision-model:1b")
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("読み取った文字"))
    interp._render_pdf_to_png = lambda _path: ["aW1hZ2U="]  # type: ignore[method-assign]
    seen: list[str] = []

    async def fake_digest(client: object, model: str) -> tuple[str | None, str | None]:
        seen.append(model)
        return "cd" * 32, None

    monkeypatch.setattr(executor_mod, "model_digest_of", fake_digest)
    raw, meta = await interp._vision_read_pdf(Path("scan.pdf"))

    assert raw == "読み取った文字"
    assert meta["model"] == "vision-model:1b"
    assert meta["model_digest"] == "cd" * 32
    assert meta["model_digest_unavailable"] is None
    assert seen == ["vision-model:1b"]


@pytest.mark.asyncio
async def test_a_hanging_digest_lookup_does_not_stall_the_execution(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一覧が**固まる**形でも、上限時間で戻って実行は成功する（抽出値は残る）。

    例外で落ちる形（上のテスト）とは別の失敗の形である。実際の `model_digest_of` と
    上限時間（`DIGEST_TIMEOUT_SECONDS`）を使い、待ちを短くして確かめる。
    """
    import asyncio
    import time

    from kawasemi.ai_input import llm_provider as provider_mod

    monkeypatch.setattr(provider_mod, "DIGEST_TIMEOUT_SECONDS", 0.05)
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("判定: はい"))

    async def hanging_list() -> None:
        await asyncio.sleep(3600)

    interp._ollama_client.list = hanging_list  # type: ignore[method-assign]
    program = {
        "steps": [{"op": "llm_judge", "id": "j", "prompt": "所見あり？"}],
        "return": "$j",
    }
    started = time.monotonic()
    _, evidence = await interp.execute([_plan("j", program)], "P1", db_session)
    elapsed = time.monotonic() - started

    assert elapsed < 5, "固まった一覧に実行が付き合っている"
    step = next(t for t in evidence["j"]["trace"] if t.get("op") == "llm_judge")
    assert step["llm"]["raw_output"] == "判定: はい"
    assert step["llm"]["model_digest"] is None
    assert step["llm"]["model_digest_unavailable"]
