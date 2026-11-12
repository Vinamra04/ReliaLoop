"""Container healthcheck: `python -m services.kit.healthcheck`.

The slim images ship neither curl nor wget, so the check uses the standard
library. Exit code 0 means `/healthz` answered 200.
"""

from __future__ import annotations

import os
import sys
import urllib.request

TIMEOUT_S = 2


def main() -> int:
    port = int(os.environ.get("PORT", "8000"))
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=TIMEOUT_S
        ) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
