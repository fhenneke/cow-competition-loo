"""Assemble a solver-value artifact page: the house stylesheet from the skill's
template, the page body, the render script, and one run's DATA inlined.

    uv run python scripts/build_page.py out/jul2026/artifact-data.json \
        artifacts/jul2026-mainnet-quotes.html ["Page Title"]

The page must be self-contained (the Artifact CSP blocks external requests), so DATA is
inlined rather than fetched. `render.js` is the skill template's script with the quote
table added; `body.html` is the prose, with the run's figures already filled in.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SKILL = ROOT.parent / ".claude/skills/solver-value-artifact/reference/template.html"
_tpl = SKILL.read_text()
if _tpl.count("<style>") != 1:
    raise SystemExit("the skill template must carry exactly one <style> block")
STYLE = _tpl[_tpl.index("<style>"):_tpl.index("</style>") + len("</style>")]
DATA = pathlib.Path(sys.argv[1]).read_text().strip()
BODY = (ROOT / "body.html").read_text()
SCRIPT = (ROOT / "render.js").read_text()

TITLE = sys.argv[3] if len(sys.argv) > 3 else "July 2026 Mainnet Solver Value"
out = pathlib.Path(sys.argv[2])
out.write_text(
    f"<title>{TITLE}</title>\n{STYLE}\n{BODY}\n<script>\nconst DATA = {DATA};\n{SCRIPT}\n</script>\n"
)
print(f"wrote {out} ({out.stat().st_size:,} bytes)")
