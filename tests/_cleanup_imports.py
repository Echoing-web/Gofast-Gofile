"""One-off: strip unused imports flagged by pyflakes in test files."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (file, old_line, new_line_or_None)  None = delete the line
edits = [
    (r"tests\test_atomicio.py", "import os\n\n", None),
    (r"tests\test_cli.py", "import pytest\n", None),
    (r"tests\test_cli.py", "from gofast52.engine import EngineConfig\n", None),
    (r"tests\test_crosscheck.py",
     "from gofast52.recover import recover_partial, scan_segment_prefix\n",
     "from gofast52.recover import recover_partial\n"),
    (r"tests\test_engine.py",
     "        from gofast52.api import GoFileApi\n        from gofast52.engine import _LinkRefresher\n",
     "        from gofast52.engine import _LinkRefresher\n"),
    (r"tests\test_memory.py",
     "from gofast52.engine import DownloadEngine, EngineConfig\n", None),
    (r"tests\test_ratelimit.py", "from gofast52.timeutil import FakeClock\n", None),
    (r"tests\test_stress.py",
     "from tests.test_engine import SMALL, ScriptedServer, _ctx, allow_all_guard\n",
     "from tests.test_engine import ScriptedServer, allow_all_guard\n"),
    (r"tests\test_stress.py", "import threading\n", None),
    (r"tests\test_stress.py", "import pytest\n", None),
    (r"tests\test_transport.py", "from gofast52.timeutil import FakeClock\n", None),
    (r"tests\_exe_smoke.py", "import struct\n", None),
    (r"tests\_exe_smoke.py", "import sys\n", None),
    (r"tests\bdd\conftest.py", "from gofast52.errors import FatalError\n", None),
    (r"tests\bdd\conftest.py", "    eng = _ensure_engine(ctx)\n", None),
    (r"tests\bdd\test_admission.py", "from pathlib import Path\n", None),
    (r"tests\bdd\test_security.py", "import pytest\n", None),
]

for rel, old, new in edits:
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    if old in text:
        p.write_text(text.replace(old, new if new else "", 1), encoding="utf-8")
        print(f"fixed {rel}: {old.strip()!r}")
    else:
        print(f"MISS  {rel}: {old.strip()!r}")

# test_recovery.py: inline unused import inside a function
p = ROOT / r"tests\bdd\test_recovery.py"
text = p.read_text(encoding="utf-8")
old = "    from gofast52.recover import recover_partial  # noqa: F401  (import sanity)\n"
if old in text:
    p.write_text(text.replace(old, "", 1), encoding="utf-8")
    print("fixed bdd/test_recovery inline import")
