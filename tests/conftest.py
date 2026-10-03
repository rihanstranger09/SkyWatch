"""Pytest bootstrap.

Adds the repository root (and ``tests/``) to ``sys.path`` so tests can import the
Lambda sources as ``src.*`` regardless of where pytest is invoked from.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
