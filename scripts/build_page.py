"""Assemble a solver-value artifact page: the house stylesheet from the skill's
template, the page body, the render script, and one run's DATA inlined.

    uv run python scripts/build_page.py out/<run>/artifact-data.json \
        artifacts/<window>-<slug>.body.html artifacts/<window>-<slug>.html "Page Title"

The page must be self-contained (the Artifact CSP blocks external requests), so DATA is
inlined rather than fetched. `render.js` is the skill template's script with the quote
table added; the body is the page's prose, with the run's figures already filled in.
Bodies and pages are run output: they live in the gitignored `artifacts/`, not in git.
"""

import pathlib
import sys

if len(sys.argv) != 5:
    raise SystemExit(__doc__)
ROOT = pathlib.Path(__file__).resolve().parent
SKILL = ROOT.parent / ".claude/skills/solver-value-artifact/reference/template.html"
_tpl = SKILL.read_text()
if _tpl.count("<style>") != 1:
    raise SystemExit("the skill template must carry exactly one <style> block")
STYLE = _tpl[_tpl.index("<style>"):_tpl.index("</style>") + len("</style>")]
data_path, body_path, out_path, TITLE = sys.argv[1:]
DATA = pathlib.Path(data_path).read_text().strip()
BODY = pathlib.Path(body_path).read_text()
SCRIPT = (ROOT / "render.js").read_text()

out = pathlib.Path(out_path)
out.write_text(
    f"<title>{TITLE}</title>\n{STYLE}\n{BODY}\n<script>\nconst DATA = {DATA};\n{SCRIPT}\n</script>\n"
)
print(f"wrote {out} ({out.stat().st_size:,} bytes)")
