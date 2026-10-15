"""Coordinator helper: set a package's status and note in docs/v2/data/packages.py.

usage: python scripts/plan_done.py <ID> <status> "<note>"
"""

import re
import sys
from pathlib import Path

pid, status, note = sys.argv[1], sys.argv[2], sys.argv[3]
p = Path(__file__).resolve().parents[1] / "docs" / "v2" / "data" / "packages.py"
s = p.read_text(encoding="utf-8")
start = s.index(f'("{pid}", ')
# the tuple ends at the first `),` followed by a newline after start
end = s.index("),\n", start)
body = s[start:end]
m = list(re.finditer(r'"((?:[^"\\]|\\.)*)"', body))
# last two string literals are status and note
st, nt = m[-2], m[-1]
new = (
    body[: st.start()]
    + f'"{status}"'
    + body[st.end() : nt.start()]
    + '"'
    + note.replace('"', "'")
    + '"'
    + body[nt.end() :]
)
p.write_text(s[:start] + new + s[end:], encoding="utf-8")
print(f"{pid} -> {status}")
