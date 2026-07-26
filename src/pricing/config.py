"""Environment-driven settings. Local dev reads .env (gitignored)."""

import os
from pathlib import Path


def _load_dotenv() -> None:
    env_file = Path(".env")
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

USER_AGENT = (
    "sa-pricing-tracker/0.1 "
    "(public new-home pricing research; contact: markus@mavectra.com)"
)
RATE_LIMIT_MS = 1500


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (env or .env)")
    return url
