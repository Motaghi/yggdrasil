import sys
from pathlib import Path

# The volume test drives the real CLI through the frozen acceptance driver and its Linear fake (read-only use).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
