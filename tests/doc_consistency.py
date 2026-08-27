"""Audit the documentation against the codebase, treating the codebase as truth.

    python3 tests/doc_consistency.py

Prose drifts silently. A rejection code gets renamed, a seed value changes, a
capability is added -- the code keeps working and the document quietly becomes a
lie that reads exactly like the truth. Every check here compares something a
document ASSERTS against the thing it describes, mechanically, so drift is a
failing line rather than something a reader has to notice.

What it cannot check is reasoning: whether an explanation is still the right
explanation. Those are listed at the end as MANUAL so they are not mistaken for
covered ground.
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import re
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

problems: list[tuple[str, str]] = []
checks = 0


def check(ok: bool, area: str, detail: str) -> None:
    global checks
    checks += 1
    if not ok:
        problems.append((area, detail))


def read(path: str) -> str:
    return io.open(path, encoding="utf-8", errors="replace").read()


DOCS = {name: read(f"docs/{name}") for name in os.listdir("docs") if name.endswith(".md")}
ALL_DOCS = "\n".join(DOCS.values())
MMD = {name: read(f"visualizations/{name}") for name in os.listdir("visualizations")}
ALL_MMD = "\n".join(MMD.values())

db = sqlite3.connect(":memory:")
db.executescript(read("schema/schema.sql"))
db.row_factory = sqlite3.Row

# ---------------------------------------------------------------- schema shape
real_tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
real_triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}

ref = DOCS["02-reference.md"]
sec4 = ref.split("## 4. Schema reference")[1].split("## 5.")[0]
documented_tables = set(re.findall(r"^### (\w+)$", sec4, re.M))
check(real_tables == documented_tables, "schema/tables",
      f"docs/02 §4 documents {sorted(documented_tables)}; schema has {sorted(real_tables)}")

for t in sorted(real_tables & documented_tables):
    cols = {c["name"] for c in db.execute(f"PRAGMA table_info({t})")}
    block = sec4.split(f"### {t}\n", 1)[1].split("\n### ", 1)[0]
    shown = set(re.findall(r"^\| `(\w+)` \| `", block, re.M))
    check(cols == shown, f"schema/{t}",
          f"columns differ: missing from docs {sorted(cols - shown)}, "
          f"documented but absent {sorted(shown - cols)}")

er = MMD["03-schema-er.mmd"]
drawn = set(re.findall(r"^    ([A-Z_]+) \{", er, re.M))
check({t.upper() for t in real_tables} == drawn, "schema/ER-diagram",
      f"diagram draws {sorted(drawn)}; schema has {sorted(t.upper() for t in real_tables)}")
for t in sorted(real_tables):
    if t.upper() not in drawn:
        continue
    cols = {c["name"] for c in db.execute(f"PRAGMA table_info({t})")}
    block = er.split(f"    {t.upper()} {{", 1)[1].split("    }", 1)[0]
    shown = set(re.findall(r"^\s+\w+ (\w+)", block, re.M))
    check(cols == shown, f"ER/{t}",
          f"missing {sorted(cols - shown)}, extra {sorted(shown - cols)}")

for trig in sorted(real_triggers):
    check(trig in ALL_DOCS, "schema/triggers", f"trigger {trig!r} is documented nowhere")

# ------------------------------------------------------------------ seed data
caps = {r["behavior"]: r for r in db.execute("SELECT * FROM label_caps")}
for behavior, row in sorted(caps.items(), key=lambda kv: kv[1]["priority"]):
    check(behavior in ALL_DOCS, "label_caps", f"{behavior} appears in no document")
    if row["max_outstanding"] is not None:
        pattern = rf"{re.escape(behavior)}[^\n]*{row['max_outstanding']}|{row['max_outstanding']}[^\n]*{re.escape(behavior)}"
        check(re.search(pattern, ALL_DOCS) is not None, "label_caps",
              f"{behavior}'s cap of {row['max_outstanding']} is stated in no document")
    if row["reply_behavior"]:
        pattern = rf"{re.escape(behavior)}[^\n]*{re.escape(row['reply_behavior'])}"
        check(re.search(pattern, ALL_DOCS) is not None, "label_caps/reply",
              f"{behavior} -> {row['reply_behavior']} is stated in no document")

stored = {b for b, r in caps.items() if r["stored"]}
for b in sorted(stored):
    check(b in ALL_DOCS, "label_caps/stored", f"stored label {b} undocumented")

layers = {(r["source_prefix"], r["orchestrator_type"]): r["layer"] for r in db.execute("SELECT * FROM agent_layers")}
for (src, role), layer in sorted(layers.items()):
    label = role if role != "*" else src
    check(re.search(rf"{re.escape(label)}[^\n]*{layer}\b", ALL_DOCS) is not None,
          "agent_layers", f"{src}/{role} = {layer} is stated in no document")

sc = {r["source_prefix"]: r for r in db.execute("SELECT * FROM source_caps")}
for flag in ("can_execute", "needs_handshake", "can_send", "accepts_research"):
    check(flag in ALL_DOCS, "source_caps", f"column {flag} documented nowhere")
    off = sorted(p for p, r in sc.items() if not r[flag])
    if off:
        check(re.search(rf"{flag}[^\n]{{0,80}}0|0[^\n]{{0,40}}{flag}", ALL_DOCS) is not None,
              "source_caps", f"{flag}=0 (for {off}) is stated in no document")

# ------------------------------------------------------------- rejection codes
def codes_in(path: str) -> set[str]:
    return set(re.findall(r'Rejected\(\s*\n?\s*"([a-z_]+)"', read(path)))

# A docstring's `Raises:` list is documentation an agent reads at tool-listing
# time. A code named there that no longer exists sends the caller looking for a
# branch that cannot fire; one that exists under a different name is worse,
# because the caller writes a check against a string that never matches. Both
# are invisible to the section-3 check below, which compares the code to the
# reference doc and never to what the docstring beside it claims.
#
# Scoped to the `Rejected:` sentence itself, and stopping at its closing
# period: the rest of a Raises: block is prose, and prose backticks parameter
# names too.
_ALL_CODES = codes_in("messaging_core/core.py") | codes_in("polling/server.py")
_CLAIMED: set[str] = set()
for _block in re.findall(r"Raises:\n(.*?)(?=\n\n|\n +\"\"\")",
                         read("messaging_core/core.py"), re.S):
    for _sentence in re.findall(r"Rejected:(.*?)\.", _block, re.S):
        # Cut at the first qualifier: "`not_reportable` if `behavior` is ..."
        # names a code and then a PARAMETER, and only the part before the
        # qualifier is the list of codes this method can raise.
        _sentence = re.split(r"\b(?:if|when|unless|for|where)\b", _sentence)[0]
        _CLAIMED |= set(re.findall(r"`([a-z][a-z_]{3,})`", _sentence))
for _claimed in sorted(_CLAIMED):
    check(_claimed in _ALL_CODES, "rejections/docstrings",
          f"a docstring's Raises: names {_claimed!r}, which no Rejected(...) raises")

core_codes = codes_in("messaging_core/core.py")
sec3 = ref.split("## 3. Rejection code index")[1].split("Three further code families")[0]
indexed = set(re.findall(r"^\| `([a-z_]+)` \|", sec3, re.M))
check(core_codes == indexed, "rejections/core",
      f"undocumented {sorted(core_codes - indexed)}; documented-but-absent {sorted(indexed - core_codes)}")

adapter_codes: set[str] = set()
for root, _dirs, files in os.walk("adapters"):
    for f in files:
        if f.endswith(".py"):
            adapter_codes |= codes_in(os.path.join(root, f))
adapter_codes |= codes_in("extension/base.py") | codes_in("polling/server.py")
tail = ref.split("Three further code families")[1]
for c in sorted(adapter_codes - core_codes):
    check(c in tail, "rejections/adapters", f"{c} is not in the non-core code families list")

# ------------------------------------------------------------- capability list
from messaging_core.core import MessagingCore  # noqa: E402
from messaging_core.db import Database  # noqa: E402
from mcp_server.server import build_server  # noqa: E402

tmp = Database(os.path.join(tempfile.mkdtemp(), "d.db"), schema="schema/schema.sql")

# TWO surfaces, and conflating them was this script's second false positive.
# `tools` is what an agent can call. `all_tools` additionally includes the
# Polling Server's own endpoint, which is registered only when a PollingServer
# is passed -- it is not a client capability, but it IS a real tool and it has
# a note in the vault.
from polling.server import PollingServer  # noqa: E402
tools = {t.name for t in asyncio.run(build_server(name="t", core=MessagingCore(tmp)).list_tools())}
all_tools = {t.name for t in asyncio.run(build_server(
    name="t", core=MessagingCore(tmp),
    polling=PollingServer(tmp, extensions={})).list_tools())}
sec2 = ref.split("## 2. The")[1].split("## 3.")[0]
documented_caps = set(re.findall(r"^### (\w+)", sec2, re.M))
check(tools == documented_caps, "capabilities",
      f"tools not documented {sorted(tools - documented_caps)}; "
      f"documented but not tools {sorted(documented_caps - tools)}")

NUMS = {"eleven": 11, "twelve": 12, "fifteen": 15, "sixteen": 16, "seventeen": 17,
        "eighteen": 18, "nineteen": 19, "twenty": 20}
# How many capabilities take a requester_uuid -- a SUBSET claim the reference
# makes, and a different number from the total. Checking it against the total
# was this script's own first false positive.
core_sig = ast.parse(read("messaging_core/core.py"))
core_cls = next(n for n in core_sig.body if isinstance(n, ast.ClassDef) and n.name == "MessagingCore")
with_requester = {
    n.name for n in core_cls.body
    if isinstance(n, ast.FunctionDef) and n.name in tools
    and any(a.arg == "requester_uuid" for a in n.args.kwonlyargs + n.args.args)
}

for doc_name, text in DOCS.items():
    for m in re.finditer(r"\b(" + "|".join(NUMS) + r")\b\s+capabilit(?:y|ies)(\s+that\b|\s+which\b)?", text):
        claimed, qualified = NUMS[m.group(1)], m.group(2)
        if qualified:                      # "... capabilities THAT accept a requester_uuid"
            check(claimed == len(with_requester), f"counts/{doc_name}",
                  f"says {m.group(1)} capabilities take a requester_uuid; "
                  f"{len(with_requester)} do")
        else:
            check(claimed == len(tools), f"counts/{doc_name}",
                  f"says {m.group(1)} capabilities; there are {len(tools)}")
    for m in re.finditer(r"\b(?:all )?(\d+) tables\b", text):
        check(int(m.group(1)) == len(real_tables), f"counts/{doc_name}",
              f"says {m.group(1)} tables; schema has {len(real_tables)}")

# --------------------------------------------------------- extension interface
ext_src = read("extension/base.py")
tree = ast.parse(ext_src)
remote = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RemoteExtension")
abstract = {n.name for n in remote.body if isinstance(n, ast.FunctionDef)
            and any(getattr(d, "id", "") == "abstractmethod" for d in n.decorator_list)}
concrete = {n.name for n in remote.body if isinstance(n, ast.FunctionDef)} - abstract
sec6 = ref.split("## 6. Extension interface")[1].split("## 7.")[0]
for m in abstract:
    check(m in sec6, "extension", f"abstract method {m} missing from docs/02 §6")
for m in concrete:
    check(m in sec6, "extension", f"concrete method {m} missing from docs/02 §6")
for m in re.finditer(r"\*\*(\w+) abstract methods\*\*", sec6):
    word = m.group(1).lower()
    check(NUMS.get(word, {"three": 3, "four": 4, "five": 5}.get(word)) == len(abstract),
          "extension", f"docs say {word} abstract methods; there are {len(abstract)}")

# -------------------------------------------------------------- prompt shapes
tmpl = {n.name for n in ast.parse(read("messaging_core/templates.py")).body
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
for fn in sorted(tmpl):
    check(fn in ALL_DOCS, "templates", f"template {fn}() is documented nowhere")

# ------------------------------------------------------------ dead references
GONE = ["forward_queue", "backward_queue", "polling_tasks", "open_issues",
        "resume_partner", "resume_remote_execution", "CausalRole", "queue_for",
        "max_queue", "notify_targets", "no_remote_permission_removal"]
for name in GONE:
    live = name in read("messaging_core/core.py") or name in read("schema/schema.sql")
    if live:
        continue
    for doc_name, text in DOCS.items():
        hits = [ln for ln in text.splitlines() if name in ln]
        # A line that explicitly frames it as removed/historical is fine.
        stale = [ln for ln in hits if not re.search(
            r"\bno\b|not\b|gone|removed|gained|replaced|gone|gap|gain|deliberately|gapped|earlier|gap|"
            r"used to|gapp|no longer|dropped|obsolete|was |were |never\b|history", ln, re.I)]
        for ln in stale:
            check(False, f"dead-ref/{doc_name}", f"{name!r} still described as live: {ln.strip()[:90]}")

# ------------------------------------------------------------------ the vault
# The project wiki is a second documentation surface and drifts the same way.
# vault_check.py checks its LINKS; this checks its CLAIMS against the code.
VAULT = "/mnt/c/Data/Books/Brains/polling-mechanism"
if os.path.isdir(VAULT):
    live = "".join(read(f) for f in (
        "messaging_core/core.py", "schema/schema.sql", "polling/server.py",
        "adapters/antigravity/adapter.py", "adapters/claude_science/adapter.py",
        "adapters/notebooklm/adapter.py", "extension/base.py"))
    vault_md = {}
    for root, _dirs, files in os.walk(VAULT):
        if ".obsidian" in root or "Development" in root:
            continue
        for f in files:
            if f.endswith(".md"):
                path = os.path.join(root, f)
                vault_md[os.path.relpath(path, VAULT)] = read(path)

    # A name the code no longer contains must not be described as current.
    RETIRED = ["forward_queue", "backward_queue", "polling_tasks", "open_issues",
               "resume_partner", "resume_remote_execution", "CausalRole", "queue_for",
               "max_queue", "notify_targets", "no_remote_permission_removal",
               "gemini_orchestrator_requires_science_project"]
    HISTORICAL = re.compile(
        r"no longer|removed|used to|earlier|gone|dropped|replaced|never|was |were |"
        r"history|obsolete|there is no", re.I)
    for name in RETIRED:
        if name in live:
            continue
        for rel, text in vault_md.items():
            for line in text.splitlines():
                if name in line and not HISTORICAL.search(line):
                    check(False, f"vault/{rel}",
                          f"{name!r} described as current: {line.strip()[:80]}")

    # Every tool note must name a capability that exists.
    for rel in vault_md:
        if not rel.startswith("Modules/Tools/"):
            continue
        name = os.path.basename(rel)[:-3]
        check(name in all_tools, f"vault/{rel}",
              f"tool note {name!r} has no matching MCP tool")
    for name in sorted(all_tools):
        check(os.path.exists(os.path.join(VAULT, "Modules/Tools", f"{name}.md")),
              "vault/tool-notes", f"tool {name!r} has no note in Modules/Tools")

    # The schema copy in the vault must be byte-identical to the shipped one.
    vault_schema = os.path.join(VAULT, "Concepts/Schemas/Messaging/schema.sql")
    if os.path.exists(vault_schema):
        check(read(vault_schema) == read("schema/schema.sql"), "vault/schema",
              "the vault's schema.sql copy has drifted from schema/schema.sql")

print("=" * 90)
print("DOCUMENTATION CONSISTENCY AUDIT -- codebase is the source of truth")
print("=" * 90)
print(f"{checks} checks run")
if not problems:
    print("\nNo inconsistency found.")
else:
    print(f"\n{len(problems)} INCONSISTENCIES:\n")
    for area, detail in problems:
        print(f"  [{area}] {detail}")
print("""
NOT MECHANICALLY CHECKED -- these need a reader:
  - whether an explanation is still the RIGHT explanation for a behaviour
  - whether a rationale describes the design as it now stands
  - whether a vault note's REASONING still fits (its claims are checked above,
    its links by vault_check.py)
""")
raise SystemExit(1 if problems else 0)
