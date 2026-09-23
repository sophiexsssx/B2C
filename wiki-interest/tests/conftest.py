"""
Make `wiki_interest` importable when running pytest from the repo root
(`python -m pytest wiki-interest/tests`), given the src-layout of the
wiki-interest package (src/wiki_interest/...) which isn't pip-installed.
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
