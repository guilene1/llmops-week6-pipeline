"""The pipeline scripts import each other by name (they are run as python pipeline/x.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
