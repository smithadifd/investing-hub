"""Ensure pytest imports ``hub`` from this worktree, not from an editable install elsewhere."""

import sys
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
if str(WORKTREE) not in sys.path:
    sys.path.insert(0, str(WORKTREE))
