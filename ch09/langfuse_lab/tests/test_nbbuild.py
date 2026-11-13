from langfuse_mastery._nbbuild import parse_percent

SRC = '''# %% [markdown]
# # Title
# Some *text*
#
# %%
x = 1
print(x)

# %% [markdown]
# Done
'''


def test_parse_percent():
    cells = parse_percent(SRC)
    assert [c["cell_type"] for c in cells] == ["markdown", "code", "markdown"]
    assert "".join(cells[0]["source"]) == "# Title\nSome *text*\n"
    assert "".join(cells[1]["source"]) == "x = 1\nprint(x)"


import json

import pytest

from langfuse_mastery import _nbbuild


@pytest.fixture
def lab(tmp_path, monkeypatch):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    monkeypatch.setattr(_nbbuild, "HERE", tmp_path)
    monkeypatch.setattr(_nbbuild, "SRC_DIR", src_dir)
    src = src_dir / "01_demo.py"
    src.write_text("# %%\nx = 1\n", encoding="utf-8")
    return src


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path, nb):
    path.write_text(json.dumps(nb), encoding="utf-8")


def test_build_stores_fingerprint(lab):
    out = _nbbuild.build(lab)
    meta = _load(out)["metadata"]["langfuse_mastery"]
    assert meta["source"] == "01_demo.py"
    assert meta["built_sha256"] == _nbbuild.fingerprint(_load(out)["cells"])


def test_rebuild_untouched_notebook_picks_up_source_change(lab):
    out = _nbbuild.build(lab)
    lab.write_text("# %%\nx = 2\n", encoding="utf-8")
    _nbbuild.build(lab)
    assert "".join(_load(out)["cells"][0]["source"]) == "x = 2"


def test_rebuild_skips_edited_notebook_without_outputs(lab, capsys):
    out = _nbbuild.build(lab)
    nb = _load(out)
    nb["cells"][0]["source"] = ["x = 'mine'"]
    _save(out, nb)
    lab.write_text("# %%\nx = 2\n", encoding="utf-8")
    _nbbuild.build(lab)
    assert "".join(_load(out)["cells"][0]["source"]) == "x = 'mine'"
    assert "edited by you" in capsys.readouterr().out


def test_rebuild_skips_notebook_with_outputs(lab, capsys):
    out = _nbbuild.build(lab)
    nb = _load(out)
    nb["cells"][0]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": ["1\n"]}]
    _save(out, nb)
    lab.write_text("# %%\nx = 2\n", encoding="utf-8")
    _nbbuild.build(lab)
    assert "".join(_load(out)["cells"][0]["source"]) == "x = 1"
    assert "edited by you" in capsys.readouterr().out


def test_rebuild_skips_notebook_without_fingerprint(lab):
    out = _nbbuild.build(lab)
    nb = _load(out)
    del nb["metadata"]["langfuse_mastery"]
    _save(out, nb)
    lab.write_text("# %%\nx = 2\n", encoding="utf-8")
    _nbbuild.build(lab)
    assert "".join(_load(out)["cells"][0]["source"]) == "x = 1"


def test_force_overwrites_edited_notebook(lab):
    out = _nbbuild.build(lab)
    nb = _load(out)
    nb["cells"][0]["source"] = ["x = 'mine'"]
    _save(out, nb)
    lab.write_text("# %%\nx = 2\n", encoding="utf-8")
    _nbbuild.build(lab, force=True)
    assert "".join(_load(out)["cells"][0]["source"]) == "x = 2"


def test_built_cells_have_unique_ids(lab):
    lab.write_text("# %% [markdown]\n# Hi\n# %%\nx = 1\n# %%\ny = 2\n", encoding="utf-8")
    ids = [c.get("id") for c in _load(_nbbuild.build(lab))["cells"]]
    assert len(ids) == 3 and all(ids) and len(set(ids)) == 3


def test_warns_on_content_before_first_marker(lab, capsys):
    lab.write_text("import os\n# %%\nx = 1\n", encoding="utf-8")
    out = _nbbuild.build(lab)
    assert f"ignored content before first # %% in {lab.name}" in capsys.readouterr().out
    assert len(_load(out)["cells"]) == 1
