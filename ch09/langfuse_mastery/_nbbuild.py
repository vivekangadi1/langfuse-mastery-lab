"""Build module notebooks from percent-format sources.

    $PY -m langfuse_mastery._nbbuild            # builds every src/*.py -> NN_name.ipynb
    $PY -m langfuse_mastery._nbbuild 01         # builds only sources starting with 01

Each built notebook records a fingerprint in metadata["langfuse_mastery"]:
{"source": <src file name>, "built_sha256": <sha256 of the generated cells' types + sources>}.

On rebuild (without --force) an existing notebook is left untouched -- printing
"skip <name> (edited by you; use --force to overwrite)" -- if it has any code-cell
outputs, has no stored fingerprint, or its current cells no longer match the stored
fingerprint. A notebook unchanged since its last build is regenerated, so source
updates flow into notebooks you haven't touched. --force always overwrites.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
SRC_DIR = HERE / "src"
KERNEL = {"name": "ch09-langfuse-v5", "display_name": "Python (ch09 Langfuse v5)", "language": "python"}


def _cell(kind: str, lines: list[str]) -> dict:
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines)
    if kind == "markdown":
        text += "\n"
    cell = {"cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True)}
    if kind == "code":
        cell.update(execution_count=None, outputs=[])
    return cell


def parse_percent(source: str, name: str = "<source>") -> list[dict]:
    cells, kind, lines = [], None, []
    for line in source.splitlines():
        if line.startswith("# %%"):
            if kind:
                cells.append(_cell(kind, lines))
            elif any(l.strip() for l in lines):
                print(f"warning: ignored content before first # %% in {name}")
            kind, lines = ("markdown" if "[markdown]" in line else "code"), []
            continue
        if kind == "markdown":
            line = line[2:] if line.startswith("# ") else line.lstrip("#")
        lines.append(line)
    if kind:
        cells.append(_cell(kind, lines))
    elif any(l.strip() for l in lines):
        print(f"warning: ignored content before first # %% in {name}")
    cells = [c for c in cells if "".join(c["source"]).strip()]
    for i, c in enumerate(cells):  # stable positional ids (nbformat 4.5); not part of the fingerprint
        c["id"] = f"c{i:03d}"
    return cells


def fingerprint(cells: list[dict]) -> str:
    h = hashlib.sha256()
    for c in cells:
        src = c.get("source", "")
        h.update(c["cell_type"].encode() + b"\0" + ("".join(src) if isinstance(src, list) else src).encode() + b"\0")
    return h.hexdigest()


def _user_edited(nb: dict) -> bool:
    cells = nb.get("cells", [])
    if any(c.get("outputs") for c in cells if c.get("cell_type") == "code"):
        return True
    stored = nb.get("metadata", {}).get("langfuse_mastery", {}).get("built_sha256")
    return stored is None or fingerprint(cells) != stored


def build(src: Path, force: bool = False) -> Path:
    out = HERE / f"{src.stem}.ipynb"
    if out.exists() and not force and _user_edited(json.loads(out.read_text(encoding="utf-8"))):
        print(f"skip {out.name} (edited by you; use --force to overwrite)")
        return out
    cells = parse_percent(src.read_text(encoding="utf-8"), name=src.name)
    nb = {"cells": cells,
          "metadata": {"kernelspec": KERNEL,
                       "langfuse_mastery": {"source": src.name, "built_sha256": fingerprint(cells)}},
          "nbformat": 4, "nbformat_minor": 5}
    out.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"built {out.name} ({len(nb['cells'])} cells)")
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for path in sorted(SRC_DIR.glob("*.py")):
        if not args or any(path.stem.startswith(a) for a in args):
            build(path, force="--force" in sys.argv)
