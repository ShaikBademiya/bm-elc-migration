"""Make the repository root importable so tests can exercise the source tree.

``amf`` ships as an implicit namespace package (no ``__init__.py``), so without
this the source tree is only importable after an install.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
