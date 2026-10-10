"""Makes the module under test importable from the workspace root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
