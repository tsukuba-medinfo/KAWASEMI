"""取得フロー図(Mermaid)の依存エッジが実データフローに忠実であることの検証

エッジは「実行時に実際に前段の値が渡る依存」だけを描くべき。特に `input` フィールドは
transform op のみが消費し、llm_extract/llm_judge は無視する（prompt 内 $id で受け取る）。
llm op の input から幻エッジを引かないことを固定する。
"""

from kawasemi.ai_input.viz import program_to_mermaid


def test_llm_input_field_does_not_draw_phantom_edge() -> None:
    """llm_extract の input:$x は（prompt に $x が無ければ）依存エッジを描かない。"""
    program = {
        "steps": [
            {"op": "file_extract", "id": "note", "path_pattern": "{{patient_id}}/note.txt"},
            {"op": "llm_extract", "id": "v", "input": "$note", "prompt": "所見を要約せよ"},
        ],
        "return": "$v",
    }
    mermaid = program_to_mermaid(program)
    # 実行時に note の値は llm へ渡らない（_op_llm は input を無視）ため、幻エッジを描かない
    assert "n_note --> n_v" not in mermaid
    # ノード自体は両方描かれる
    assert "n_note" in mermaid
    assert "n_v" in mermaid


def test_transform_input_field_draws_edge() -> None:
    """transform の input:$x は実際に消費されるため依存エッジを描く。"""
    program = {
        "steps": [
            {"op": "sql_extract", "id": "src", "sql": "SELECT 1"},
            {
                "op": "transform",
                "id": "v",
                "input": "$src",
                "rule": {"type": "code_map", "map": {}},
            },
        ],
        "return": "$v",
    }
    assert "n_src --> n_v" in program_to_mermaid(program)


def test_llm_prompt_ref_draws_edge() -> None:
    """llm_extract は prompt 内 $id からは依存エッジを描く（executor が実際に注入する経路）。"""
    program = {
        "steps": [
            {"op": "file_extract", "id": "doc", "path_pattern": "{{patient_id}}/doc.txt"},
            {"op": "llm_extract", "id": "v", "prompt": "本文: $doc"},
        ],
        "return": "$v",
    }
    assert "n_doc --> n_v" in program_to_mermaid(program)


def test_llm_judge_input_field_does_not_draw_phantom_edge() -> None:
    """llm_judge も input を消費しないため、input:$x から幻エッジを描かない。"""
    program = {
        "steps": [
            {"op": "file_extract", "id": "note", "path_pattern": "{{patient_id}}/note.txt"},
            {"op": "llm_judge", "id": "j", "input": "$note", "prompt": "所見はあるか？"},
        ],
        "return": "$j",
    }
    assert "n_note --> n_j" not in program_to_mermaid(program)


def test_csv_match_value_param_draws_edge() -> None:
    """csv_extract の match の value が params 経由で前段を参照するとき依存エッジを描く。

    同じ依存を `"$kubun"` 直書きで表現すればエッジが出るのに、params 経由（`{{form_code}}`）
    だと1本も出ない、という非対称を防ぐ。監査図から「前段 step が行の絞り込みを決めている」
    という依存が消えると、値の由来を図から追えなくなる。

    エッジの様式は記法で変わる（params 経由はパラメータ名を載せた点線、直書きは実線）。
    固定するのは「どちらの記法でも kubun → c の依存が図に現れること」である。
    """
    program = {
        "params": [{"name": "form_code", "value": "$kubun"}],
        "steps": [
            {"op": "sql_extract", "id": "kubun", "sql": "SELECT k FROM t WHERE id = :patient_id"},
            {
                "op": "csv_extract",
                "id": "c",
                "connection_id": "files",
                "path": "records.csv",
                "key_column": "患者ID",
                "column": "値",
                "match": [{"column": "様式", "value": "{{form_code}}"}],
            },
        ],
        "return": "$c",
    }
    assert 'n_kubun -. "form_code" .-> n_c' in program_to_mermaid(program)

    # 直書き `"$kubun"` でも依存は図に出る（実線のデータ辺）
    direct = {
        "steps": [
            program["steps"][0],
            {**program["steps"][1], "match": [{"column": "様式", "value": "$kubun"}]},
        ],
        "return": "$c",
    }
    assert "n_kubun --> n_c" in program_to_mermaid(direct)


def test_source_field_still_draws_edge() -> None:
    """select/aggregate の source は従来どおり依存エッジを描く（input 除去の巻き添え退行が無い）。"""
    program = {
        "steps": [
            {"op": "external_db_extract", "id": "rows", "sql": "SELECT 1", "multi": True},
            {"op": "aggregate", "id": "n", "source": "$rows", "func": "count"},
        ],
        "return": "$n",
    }
    assert "n_rows --> n_n" in program_to_mermaid(program)
