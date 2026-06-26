import sys
from pathlib import Path

_here   = Path(__file__).parent         # dmrlink3/tests/
_root   = _here.parent                   # dmrlink3/

# dmrlink3 source (const, dmrlink, bridge, …)
sys.path.insert(0, str(_root))
# test helpers (helpers.py lives alongside the test files)
sys.path.insert(0, str(_here))
