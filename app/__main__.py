"""Production entrypoint: ``python -m app``.

Applies pending Alembic migrations, then serves on ``0.0.0.0:{PORT}`` (PORT from env/.env,
default 8000). Dokploy/Railpack runs this via railpack.json; the same command works locally.
"""

from __future__ import annotations

import subprocess
import sys

import uvicorn

from app.core.config import settings


def main() -> None:
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - container binding
        port=settings.port,
        workers=max(1, settings.web_concurrency),
        loop="uvloop" if sys.platform != "win32" else "asyncio",
        http="httptools" if sys.platform != "win32" else "auto",
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=not settings.is_production,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
