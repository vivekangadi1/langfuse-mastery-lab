"""Export the `cases` list from the ch09 Langfuse notebook into data/cases.json (run once).

One-shot export; data/cases.json is canonical — do not re-run (the source notebook is edited independently).
"""
import json
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parents[1] / "ch09_example_external_evaluation_pipelines_Langfuse.ipynb"
OUT = Path(__file__).parent / "data" / "cases.json"


def main():
    cells = json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
    source = next("".join(c["source"]) for c in cells if c["cell_type"] == "code" and "base_cases = [" in "".join(c["source"]))
    namespace: dict = {}
    exec(source, namespace)  # the cell only builds plain dicts
    OUT.write_text(json.dumps(namespace["cases"], indent=1, default=str), encoding="utf-8")
    print(f"wrote {len(namespace['cases'])} cases")


if __name__ == "__main__":
    main()
