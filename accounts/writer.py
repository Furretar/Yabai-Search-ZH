"""Write fencing for the single global writer (standard library only).

Only one host, the "writer", may change the accounts database. The Cloudflare
Worker sends every write to the writer the Durable Object chose and adds two
headers:

    X-Yabai-Writer:        the name of the host it chose (must be THIS host)
    X-Yabai-Writer-Epoch:  a counter that goes up each time the writer changes

"Fencing" means refusing a write whose epoch is older than one already seen, so a
host that was replaced can never write again by accident. The highest epoch seen
is kept in a tiny file next to the database, because the replication process and
the web process both read and raise it.

If the Durable Object's storage is ever wiped, its epoch restarts at 1 and every
host refuses writes. Delete data/writer-epoch on each host to recover.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Unsafe requests under these paths change accounts. Keep in step with
# WRITE_PREFIXES in deploy/worker/wrangler.toml. Login and logout are NOT writes:
# sessions live in signed cookies, so any host can log someone in.
WRITE_PREFIXES = ("/accounts/signup/", "/api/account/")


def is_write(method: str, path: str, prefixes=WRITE_PREFIXES) -> bool:
    """True for an unsafe request on an account path."""
    if method.upper() in SAFE_METHODS:
        return False
    return any(path.startswith(p) for p in prefixes)


def highest_epoch(path) -> int:
    """The highest epoch recorded in the file at `path` (0 when there is none)."""
    try:
        return max(0, int(Path(path).read_text().strip() or 0))
    except (OSError, ValueError):
        return 0


def observe_epoch(path, epoch: int) -> bool:
    """Record `epoch` if it is higher than the one on file. Returns True if raised."""
    if epoch <= highest_epoch(path):
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".epoch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(str(int(epoch)))
        os.replace(tmp, path)  # atomic: a reader never sees half a number
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def check_write(path, node: str, writer: str, epoch_text: str) -> tuple[bool, str]:
    """May this host (`node`) take a write that names `writer` and `epoch_text`?

    Returns (ok, reason). On success the epoch is recorded.
    """
    try:
        epoch = int(epoch_text)
    except (TypeError, ValueError):
        return False, "no writer epoch (the request did not come through the Worker)"
    if writer != node:
        return False, f"the writer is {writer or 'unknown'}, not {node}"
    if epoch < highest_epoch(path):
        return False, f"stale epoch {epoch} (this host has seen {highest_epoch(path)})"
    observe_epoch(path, epoch)
    return True, ""
