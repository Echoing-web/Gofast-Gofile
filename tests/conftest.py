"""Shared fixtures for gofast52 tests. No network, no real sleep."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gofast52.timeutil import FakeClock  # noqa: E402


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1000.0)
