"""Root pytest configuration."""

import os
from pathlib import Path

# Load test env before any app imports
TEST_DIR = Path(__file__).resolve().parent
ENV_TEST = TEST_DIR / ".env.test"
if ENV_TEST.exists():
    for line in ENV_TEST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
