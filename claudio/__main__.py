"""Allow running as: python -m claudio

Delegates to the unified `claudio` entry point so `python -m claudio [...]`
behaves identically to the installed `claudio` script.
"""

import sys
from claudio.repl import main

sys.exit(main())
