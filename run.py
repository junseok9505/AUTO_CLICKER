"""실행 진입점.

    python run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from allowclicker.ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
