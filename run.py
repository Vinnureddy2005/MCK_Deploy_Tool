"""Start the dashboard using APP_HOST / APP_PORT from .env.

    python run.py

Equivalent to `uvicorn app.main:app --host 127.0.0.1 --port 5002`, but the
host and port come from .env so they are configured in one place.
"""

from __future__ import annotations

import sys

import uvicorn

from app.config import settings


def main() -> int:
    print(f"McKesson Deployment Tool -> http://{settings.app_host}:{settings.app_port}")
    if settings.dry_run:
        print("DRY_RUN is ENABLED - no server changes will be made.")
    else:
        print("DRY_RUN is DISABLED - deployments will modify the app server.")

    try:
        uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, log_level="info")
    except OSError as exc:
        # Most common cause: the port is already taken by another local tool.
        print(f"\nCould not bind {settings.app_host}:{settings.app_port} - {exc}", file=sys.stderr)
        print("Change APP_PORT in .env and try again.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
