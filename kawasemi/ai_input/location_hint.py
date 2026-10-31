"""ステップ1(役割2): 所在検索のクエリ拡張と型ヒント

所在提案の検索クエリは項目名＋臨床的意味、つまり**マニュアルの言葉**である。索引側の
DB カラムは略号と短い論理名しか持たない。項目名で embedding 検索すると、項目名をそのまま
含むマニュアルの定義文が必ず上位に来て、**略号の列名しか持たない DB 側が負けていた**。
規約文書を所在の候補から外したので負けていた相手はいなくなったが、**DB 側の候補が弱い
こと自体は変わっていない**。

そこで検索の**前に**、LLM に2つを書かせる。

- `terms`: 院内システムでこの値が入っているとしたら、どんな表名・カラム名・文書名で
  呼ばれているか。これをクエリ文へ連結して埋め込む（いわゆる HyDE）。
- `expected_types`: 想定される型と桁。候補の `payload.type_raw` との一致で加点する。
  期待される型はマニュアルの【項目の書式】に書いてあり、LLM は臨床的意味を書くときに
  既に読んでいる。

## 捏造防止との関係

**生成した文字列はクエリベクトルにしかならない。** 所在文字列は従来どおり
`derive.build_location()` が選ばれたユニットの payload から決定的に組み立て、LLM が
返すのは候補番号の整数だけである（`resource_drafter` の設計方針は不変）。

## 失敗したとき

**施策抜きで縮退する。** 生成が失敗した項目は拡張なし・加点なしになり、現行と同じ
動作に戻る。索引の rewriter が None のモードが施策抜きで縮退するのと同じ扱いで、
所在提案そのものを止めない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from kawasemi.ai_input.llm_json import (
    LARGE_CONTEXT_OPTIONS,
    LLMCallRecorder,
    PartialResultException,
    chat_json_chunked,
    merge_by_link_id,
    strict_response_format,
)
from kawasemi.ai_input.schemas import LLMLocationHints
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# ヒント生成1コールあたりの項目数。
#
# **選択の刻み幅（`DEFAULT_CHUNK_SIZE=4`）と分ける。** ヒントのプロンプトには候補本文
# （snippet）が載らず、項目名と臨床的意味だけなので、選択のプロンプトより桁違いに短い
# （選択側は候補本文を載せるので1コールが長い）。4件のままだと対象項目数を4で割ったコール数になり、
# **選択のコールに加えて所要が倍近くへ伸びる**。ヒントは1コールが軽いので束ねる。
#
# **測定に効く**（所要時間と、1コールに載る項目の組み合わせが変わる）。
_HINT_CHUNK_SIZE = 12

# 1項目あたりクエリへ足す語の上限。
#
# **測定に効く**（クエリ文が変われば検索順位が変わる）。上限を置くのは、
# モデルが語を大量に並べると元の項目名・臨床的意味が薄まり、拡張が逆効果になるためで
# ある。索引側のチャンク長とは無関係に、こちら側で持つ。
_MAX_TERMS = 8

# 1項目あたり受け取る型ヒントの上限。
#
# 型は加点の条件なので、多すぎると「どれかに当たる」状態になって加点が効かなくなる。
_MAX_TYPES = 3

# 型ヒント生成の system プロンプト。
HINT_SYSTEM_PROMPT = """あなたは病院情報システムのデータベースに詳しいアシスタントです。
登録様式（がん登録、感染症の症例報告など）の入力項目について、その値が院内システムの
どこに入っているかを検索するための手がかりを書いてください。所在そのものを答えるのではなく、
**検索語**を出す作業です。様式の領域は項目名と臨床的コンテキストから読み取り、決めつけないこと。

各項目には項目名（item_text）と臨床的コンテキスト（annotation, 任意）が与えられます。

terms には、その値が院内システムに格納されているとしたら、表・カラム・文書が
**どんな名前で呼ばれていそうか**を挙げてください。
- 実際のシステムで使われる言い方を挙げる（例「病理検査」「手術記録」「経過表」「検査結果」）。
- 日本語の略記やローマ字略号も挙げてよい（例「診断日」に対し「SHINDANBI」「DIAG_DATE」）。
- 一般名詞だけを並べない。その項目に特有の語を選ぶ。
- 最大8語。思いつかなければ空配列でよい。

expected_types には、その値を保持するカラムの型と桁を挙げてください。
- 索引の型の語彙に合わせる: CHAR(n) / VARCHAR / NUMERIC(n) / INT / TIMESTAMP。
- **日付は施設によって持ち方が違う。** 同じ「診断日」でも TIMESTAMP、NUMERIC(14)、
  CHAR(8)、CHAR(14) のいずれもありうるので、**日付は複数挙げる**（1つに絞らない）。
- コードや区分のように桁が定まるものは桁まで書く（例 1桁コードなら CHAR(1)）。
- 桁が定まらなければ型だけでよい（例 自由記載なら VARCHAR）。
- 最大3件。定まらなければ空配列でよい。**当てずっぽうで挙げるより空のほうがよい。**

必ず全ての項目を hints 配列に含めてください。"""


@dataclass(frozen=True)
class LocationHint:
    """1項目ぶんの検索ヒント。`terms` はクエリへ、`expected_types` は加点へ使う。"""

    terms: tuple[str, ...] = ()
    expected_types: tuple[str, ...] = ()

    @property
    def type_keys(self) -> frozenset[tuple[str, int | None]]:
        """加点の照合に使う正規化済みの型集合。"""
        return frozenset(k for k in (normalize_type(t) for t in self.expected_types) if k)


EMPTY_HINT = LocationHint()


@dataclass
class HintResult:
    """生成の結果。`hints` に無い項目は施策抜きで縮退する。"""

    hints: dict[str, LocationHint] = field(default_factory=dict)
    llm_model: str | None = None
    failed: bool = False

    def of(self, link_id: str) -> LocationHint:
        return self.hints.get(link_id, EMPTY_HINT)

    @property
    def effective_count(self) -> int:
        """**実際に検索を変えたヒントの数**。

        応答が返った項目の数ではない。プロンプトは「思いつかなければ空配列でよい」と
        言っているので、`terms` も `expected_types` も空のヒントは正当な応答だが、
        その項目の検索はこの版より前と1バイトも変わらない。**施策が効いたかを見る
        指標なので、効かなかった項目は数えない。**
        """
        return sum(1 for h in self.hints.values() if h.terms or h.expected_types)


_TYPE_PATTERN = re.compile(r"^([A-Z]+)(?:\s*\(\s*(\d+)\s*(?:,\s*\d+\s*)?\))?$")


def normalize_type(raw: str | None) -> tuple[str, int | None] | None:
    """型の表記を `(基本型, 桁)` へ正規化する。照合できない表記は None を返す。

    索引側は `CHAR(8)` と `CHAR(08)` のように**同じ型を違う字面で持つ**ので、桁は int
    にして比較する。`NUMERIC(06,02)` のような小数点以下つきは整数部の桁だけを見る
    （桁数の意味が「文字数」ではなくなるため、一致を要求すると取りこぼす）。
    """
    if not raw:
        return None
    m = _TYPE_PATTERN.match(str(raw).strip().upper())
    if not m:
        return None
    return (m.group(1), int(m.group(2)) if m.group(2) else None)


def type_matches(hint: LocationHint, type_raw: str | None) -> bool:
    """候補の型がヒントに合うか。

    **桁は片方が省略されていれば無視する。** ヒントが `CHAR` だけなら `CHAR(8)` にも
    当たり、ヒントが `CHAR(8)` なら桁なしの `CHAR` にも当たる。桁まで一致することを
    要求すると、索引側が桁を持たない列（`VARCHAR` の列の大半）に一切当たらなくなる。
    """
    key = normalize_type(type_raw)
    if key is None:
        return False
    for base, length in hint.type_keys:
        if base != key[0]:
            continue
        if length is None or key[1] is None or length == key[1]:
            return True
    return False


def item_label(d: dict[str, Any]) -> str | None:
    """所在探索のクエリと LLM への提示に使う項目名（親の text を前に付ける）。

    `item_text` だけでは何の値か分からない項目がある。感染症の様式の検査値は、項目が
    「数値(mg/dL)」で、どの検査かは親グループの text（「CRP」）にしかない。親の text を
    付けないと、こうした項目が無関係な検査値の列（別の検査の数値欄）に当たる。

    親の text が項目の text に含まれるとき（「既往歴」の下の「既往歴」等）は付けない。
    項目の text が無ければ親の text だけを返す（無題の入力欄も何を問うかが分かる）。
    保存する `item_text` は変えない——これは検索と提示のためだけの名前である。
    """
    text = (d.get("item_text") or "").strip()
    parent = (d.get("parent_text") or "").strip()
    if parent and parent not in text:
        return f"{parent} / {text}" if text else parent
    return text or None


def _build_hint_message(items: list[dict[str, Any]]) -> str:
    lines = ["以下の各入力項目について、検索語と想定される型を挙げてください:\n"]
    for d in items:
        lines.append(f"### linkId: {d['link_id']}")
        lines.append(f"  項目名: {item_label(d) or '（未設定）'}")
        if d.get("annotation"):
            lines.append(f"  臨床的コンテキスト: {d['annotation']}")
        lines.append("")
    return "\n".join(lines)


async def generate_hints(
    client: Any,
    model: str,
    items: list[dict[str, Any]],
    recorder: LLMCallRecorder | None = None,
) -> HintResult:
    """項目ごとの検索ヒントを生成する。**失敗しても例外を投げない。**

    所在提案そのものを止めないため、例外は握って `failed=True` を返す。呼び出し側は
    `HintResult.of()` で項目ごとに引き、無い項目は拡張なしで検索する。

    **選択と同じモデルを渡すこと**。モデルどうしを比べるとき、
    語の生成を1つのモデルに固定して選択だけ別のモデルにさせると比較が交絡する。
    """
    if not items:
        return HintResult()
    try:
        raw = await chat_json_chunked(
            client,
            model,
            items,
            system_prompt=HINT_SYSTEM_PROMPT,
            build_message=_build_hint_message,
            # `terms` / `expected_types` は既定値つきなので `required` に入らない。
            # 空配列は正当な応答（「思いつかなければ空でよい」）なので、必須にするのは
            # `link_id` の対応づけだけでよい——ここが欠けると項目に紐づかない。
            response_format=strict_response_format(LLMLocationHints, "link_id"),
            parse=lambda d: LLMLocationHints(**d).hints,
            label="所在検索ヒント",
            chunk_size=_HINT_CHUNK_SIZE,
            options=LARGE_CONTEXT_OPTIONS,
            recorder=recorder,
        )
    except PartialResultException as e:
        # 途中まででも使う。返らなかった項目は施策抜きで縮退する。
        raw = e.partial
        logger.warning(f"所在検索ヒントが途中で終わった（{e.message}）: {len(raw)}件は使う")
    except Exception as e:  # noqa: BLE001 — 所在提案を止めないための握り
        logger.warning(f"所在検索ヒントの生成に失敗した（{type(e).__name__}: {e}）: 拡張なしで続行")
        return HintResult(llm_model=model, failed=True)

    merged = merge_by_link_id(raw, value=lambda h: h)
    hints = {
        link_id: LocationHint(
            # **空白を除いてから上限を掛ける。** 先に切ると、モデルが空文字を混ぜたときに
            # 枠を食われて後ろの使える語が落ち、定めた上限が実効値と一致しなくなる。
            terms=tuple(t.strip() for t in (h.terms or []) if t and t.strip())[:_MAX_TERMS],
            expected_types=tuple(t.strip() for t in (h.expected_types or []) if t and t.strip())[
                :_MAX_TYPES
            ],
        )
        for link_id, h in merged.values.items()
        if h is not None
    }
    logger.info(f"所在検索ヒント生成完了: {len(hints)}件（対象{len(items)}件）, model={model}")
    return HintResult(hints=hints, llm_model=model)
