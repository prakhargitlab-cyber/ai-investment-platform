from __future__ import annotations

import sys
from pathlib import Path


SERVICE = Path(__file__).resolve().parents[1]
RESEARCH = SERVICE.parent / "research-engine"
for path in (str(SERVICE), str(RESEARCH)):
    if path not in sys.path:
        sys.path.insert(0, path)
