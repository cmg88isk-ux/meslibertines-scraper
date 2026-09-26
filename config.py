"""Reading credentials from .env and collecting a provider's key pool.

Shared by every entry point: scraper, credit checker and reachability probe all
need the same two things, and none of them should depend on a scraper module
just to find out which keys exist.
"""

from __future__ import annotations

import os


def load_dotenv(path: str) -> None:
    """Populate os.environ from a .env file without clobbering the real env.

    setdefault rather than assignment: a value already exported by the shell
    wins, so a run can be pointed at a different key set without editing the
    file.
    """
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def provider_keys(prefix: str) -> list[str]:
    """Every key declared for one provider, in declaration order.

    Recognises PREFIX, PREFIX_2, PREFIX_3 ... and a comma-separated list in each,
    because both shapes show up in practice when keys are added over time.
    Duplicates are collapsed while keeping first-seen order.
    """
    found: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        if name == prefix:
            index = 1
        elif name.startswith(f"{prefix}_") and name[len(prefix) + 1 :].isdigit():
            index = int(name[len(prefix) + 1 :])
        else:
            continue
        for key in (value or "").split(","):
            key = key.strip()
            if key:
                found.append((index, key))
    found.sort(key=lambda item: item[0])
    keys: list[str] = []
    for _, key in found:
        if key not in keys:
            keys.append(key)
    return keys
