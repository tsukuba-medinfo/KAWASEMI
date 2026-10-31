"""モデルごとの options の例外（`model_options.py`）の読み込みのテスト。値はすべて架空のもの。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kawasemi.ai_input.model_options import ModelOptionsError, load_model_options
from kawasemi.core.config import PROJECT_ROOT, Settings


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "model_options.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_default_path_is_under_site_dir() -> None:
    assert Settings(site_dir="/x/site").model_options_file == "/x/site/model_options.yaml"


def test_a_missing_file_is_an_empty_table(tmp_path: Path) -> None:
    assert load_model_options(str(tmp_path / "absent.yaml")) == {}


def test_an_empty_file_is_an_empty_table(tmp_path: Path) -> None:
    assert load_model_options(_write(tmp_path, "")) == {}


def test_reads_model_name_to_options(tmp_path: Path) -> None:
    path = _write(tmp_path, 'models:\n  "model-a:7b":\n    num_batch: 512\n')
    assert load_model_options(path) == {"model-a:7b": {"num_batch": 512}}


def test_the_template_in_site_example_is_valid() -> None:
    table = load_model_options(str(PROJECT_ROOT / "site.example" / "model_options.yaml"))
    assert table == {"example-model:7b": {"num_batch": 512}}


def test_a_changed_file_is_read_again(tmp_path: Path) -> None:
    path = _write(tmp_path, 'models:\n  "model-a:7b":\n    num_batch: 512\n')
    assert load_model_options(path)["model-a:7b"] == {"num_batch": 512}
    p = Path(path)
    p.write_text('models:\n  "model-a:7b":\n    num_batch: 256\n', encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert load_model_options(path)["model-a:7b"] == {"num_batch": 256}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("- a\n- b\n", "models:"),
        ("unknown: {}\n", "models:"),
        ("models: [a]\n", "対応表"),
        ('models:\n  "": {num_batch: 1}\n', "モデル名"),
        ('models:\n  "model-a:7b": 512\n', "対応表"),
        ('models:\n  "model-a:7b": {}\n', "対応表"),
        ('models:\n  "model-a:7b": {num_batch: [1, 2]}\n', "num_batch"),
        ('models:\n  "model-a:7b": {num_batch: {x: 1}}\n', "num_batch"),
        ("models: [\n", "YAML"),
    ],
)
def test_a_malformed_file_is_an_error(tmp_path: Path, text: str, fragment: str) -> None:
    """**形が正しくなければ止める。** 黙って空に落とすと、例外が効いていないことに気づけない。"""
    with pytest.raises(ModelOptionsError, match=fragment):
        load_model_options(_write(tmp_path, text))
