"""Entry point for the desktop bundle.

The bundle ships no server logic: it starts the published package's own CLI, so there is exactly
one implementation of the tools and the bundle cannot fall behind a release.
"""

from __future__ import annotations

import sys

from uvc_ptz_mcp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
