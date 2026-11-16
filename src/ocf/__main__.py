"""``python -m ocf`` (used by the app's autostart shortcut)."""

import sys

from ocf.cli import main

sys.exit(main())
