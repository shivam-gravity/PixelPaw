"""Developer helper: collect every t("...") string in the web app into locales/en.json -> "app".

Run after changing pixelpaw/web/app.html:  python tests/extract_strings.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
html = (ROOT / "pixelpaw" / "web" / "app.html").read_text(encoding="utf-8")
en_path = ROOT / "pixelpaw" / "locales" / "en.json"
en = json.loads(en_path.read_text(encoding="utf-8"))

found = set(re.findall(r'\bt\("((?:[^"\\]|\\.)+)"\)', html))
# Labels passed through helper functions that call t() on their first argument.
for helper in ("field", "card", "mk", "step", "sel"):
    found |= set(re.findall(helper + r'\(\s*(?:\d+\s*,\s*[\w.!]+\s*,\s*)?"((?:[^"\\]|\\.)+)"', html))
found |= set(re.findall(r'\[\s*"[\w-]+"\s*,\s*"((?:[^"\\]|\\.)+)"\s*\]', html))  # select options / tab labels
found = {s for s in found if not re.fullmatch(r"[\w-]+", s) or s[0].isupper()}
found -= {k for k in en if not k.startswith("_") and k != "app"}  # already top-level keys

app = {s: s for s in sorted(found)}
en["app"] = app
en_path.write_text(json.dumps(en, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"{len(app)} app strings written to {en_path}")
