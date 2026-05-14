import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_HOME = REPO_ROOT / ".tmp-home"
TMP_HOME.mkdir(exist_ok=True)
os.environ["HOME"] = str(TMP_HOME)
