"""Primary/standby replication of the accounts SQLite database (standard library only).

Only one machine, the global writer, takes writes (the Cloudflare Worker sends them
there; see accounts/writer.py), so there is nothing to merge. Every other machine is a
replica: it asks the Worker who the writer is (YABAI_CONTROL_URL), then pulls the
writer's database through its tunnel hostname at /__repl/*. A replica always converges
to the writer's copy; if it holds writes the writer lacks, they are archived first.

How "newer" is decided: a version vector. Every write to any table bumps a counter
that belongs to the machine that made it (SQLite triggers do this, so raw SQL and
admin edits are counted too). Two databases compare as:

    equal       same counters
    peer newer  peer has every write we have, plus more   -> install the peer's copy
    we're newer we have every write the peer has, plus more -> keep ours, never pull
    concurrent  each side has writes the other lacks (split brain: a tunnel dropped
                while the process kept running, or a crash before replication)

Clocks are never compared, so a wrong tablet clock cannot cause a bad decision. A
concurrent pair is resolved deterministically (more writes wins; a tie goes to
YABAI_REPL_PRIORITY) and the losing database is copied to data/replication-archive/
first, so `salvage` can recover its accounts.

Usage (run from the project root, deploy/run.sh does this for you):
    python -m accounts.replication catch-up [--wait SECONDS]   before serving
    python -m accounts.replication serve                       peer-facing server only
    python -m accounts.replication pull                        pull loop only
    python -m accounts.replication daemon                      serve + pull in one process
    python -m accounts.replication status
    python -m accounts.replication salvage ARCHIVE.sqlite3     re-add lost accounts
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import writer

log = logging.getLogger("yabai.replication")

BASE_DIR = Path(__file__).resolve().parent.parent
VECTOR_TABLE = "replication_vector"
TRIGGER_PREFIX = "yabai_repl_"
SECRET_HEADER = "X-Replication-Secret"
ORIGIN_HEADER = "X-Origin-Secret"  # the same shared secret the Worker sends to the app
# Cloudflare's WAF blocks the default Python-urllib User-Agent with a 403, so
# every replication request identifies itself explicitly.
USER_AGENT = "Yabai-Replication/1"
REPL_PREFIX = "/__repl"  # cloudflared sends this path to the replication port
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
_NOW_SQL = "(julianday('now') - 2440587.5) * 86400.0"
_NODE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

# Peers may only be on loopback, the LAN or Tailscale (same list as the origin gate).
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fd7a:115c:a1e0::/48",
    )
)


class ReplicationError(Exception):
    """Something that should stop an install (the live database is left untouched)."""


class Config:
    """Settings read from the environment (deploy/env.local feeds them in)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.node = (env.get("YABAI_REPL_NODE") or "").strip()
        # Normal mode: the Worker route that names the current writer.
        self.control_url = (env.get("YABAI_CONTROL_URL") or "").strip()
        # Fixed peer (tests and manual use only); ignored when control_url is set.
        self.peer_url = (env.get("YABAI_REPL_PEER_URL") or "").strip().rstrip("/")
        self.origin_secret = env.get("YABAI_ORIGIN_SECRET") or ""
        self.secret = env.get("YABAI_REPL_SECRET") or ""
        self.bind = env.get("YABAI_REPL_BIND") or "127.0.0.1"
        self.port = int(env.get("YABAI_REPL_PORT") or 8765)
        self.priority = (env.get("YABAI_REPL_PRIORITY") or "laptop").strip()
        self.interval = float(env.get("YABAI_REPL_INTERVAL") or 10)
        self.db_path = Path(
            env.get("DJANGO_DB_PATH") or (BASE_DIR / "data" / "accounts.sqlite3")
        )
        self.archive_dir = Path(
            env.get("YABAI_REPL_ARCHIVE") or (self.db_path.parent / "replication-archive")
        )
        self.epoch_path = self.db_path.parent / "writer-epoch"

    @property
    def enabled(self) -> bool:
        return bool(self.node)

    def validate(self, need_peer: bool = True) -> None:
        if not _NODE_RE.match(self.node):
            raise ReplicationError("YABAI_REPL_NODE must be lowercase letters/digits, e.g. laptop")
        if not self.secret or len(self.secret) < 16:
            raise ReplicationError("YABAI_REPL_SECRET must be set (16+ characters)")
        if need_peer and not (self.control_url or self.peer_url):
            raise ReplicationError("YABAI_CONTROL_URL must be set")


# --- database helpers ---------------------------------------------------------


def _q(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def connect(path, timeout: float = 30.0, readonly: bool = False) -> sqlite3.Connection:
    """A connection with manual transaction control and foreign keys off.

    Foreign keys are off on purpose: tables are replaced one after another inside a
    single transaction, so the order must not matter.
    """
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout, isolation_level=None)
    else:
        conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def _tables(conn, schema: str = "main") -> list[str]:
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' "
        f"AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND name <> ? ORDER BY name",
        (VECTOR_TABLE,),
    ).fetchall()
    return [r[0] for r in rows]


def _has_table(conn, name: str, schema: str = "main") -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _columns(conn, table: str, schema: str = "main") -> list[tuple]:
    return [tuple(r[1:3]) for r in conn.execute(f"PRAGMA {schema}.table_info({_q(table)})")]


def ensure_vector_table(conn) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} ("
        "node TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 0, last_write REAL NOT NULL DEFAULT 0)"
    )


def drop_triggers(conn) -> None:
    for (name,) in conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type = 'trigger' AND name LIKE ? ESCAPE '\\'",
        (TRIGGER_PREFIX.replace("_", "\\_") + "%",),
    ).fetchall():
        conn.execute(f"DROP TRIGGER IF EXISTS {_q(name)}")


def install_triggers(conn, node: str) -> None:
    """(Re)create the write counters for `node`. Safe to call any number of times.

    Must run inside a transaction or on a private file: it drops and recreates
    every trigger, and a snapshot from the peer carries the PEER's triggers, so this
    is always run again after an install.
    """
    if not _NODE_RE.match(node):
        raise ReplicationError(f"bad node name {node!r}")
    ensure_vector_table(conn)
    conn.execute(
        f"INSERT OR IGNORE INTO {VECTOR_TABLE} (node, seq, last_write) VALUES (?, 0, 0)", (node,)
    )
    drop_triggers(conn)
    bump = (
        f"UPDATE {VECTOR_TABLE} SET seq = seq + 1, last_write = {_NOW_SQL} "
        f"WHERE node = '{node}';"
    )
    for table in _tables(conn):
        for event in ("INSERT", "UPDATE", "DELETE"):
            name = f"{TRIGGER_PREFIX}{event.lower()}_{table}"
            conn.execute(
                f"CREATE TRIGGER {_q(name)} AFTER {event} ON {_q(table)} "
                f"FOR EACH ROW BEGIN {bump} END"
            )


def setup(path, node: str) -> None:
    """Create the vector table and triggers on the database at `path`."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        install_triggers(conn, node)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def read_state(conn, schema: str = "main") -> dict:
    vector, last_write = {}, 0.0
    if _has_table(conn, VECTOR_TABLE, schema):
        for node, seq, when in conn.execute(f"SELECT node, seq, last_write FROM {schema}.{VECTOR_TABLE}"):
            vector[node] = int(seq)
            last_write = max(last_write, float(when))
    migrations = []
    if _has_table(conn, "django_migrations", schema):
        migrations = sorted(
            f"{app}.{name}"
            for app, name in conn.execute(f"SELECT app, name FROM {schema}.django_migrations")
        )
    return {"vector": vector, "last_write": last_write, "migrations": migrations}


def state_of(path) -> dict:
    """State of the database file at `path`; an empty state if it does not exist."""
    if not Path(path).exists():
        return {"vector": {}, "last_write": 0.0, "migrations": []}
    conn = connect(path, readonly=True)
    try:
        return read_state(conn)
    finally:
        conn.close()


# --- deciding -----------------------------------------------------------------


def compare(a: dict, b: dict) -> str:
    """'equal' | 'a_newer' | 'b_newer' | 'concurrent' for two version vectors."""
    nodes = set(a) | set(b)
    a_ahead = any(a.get(n, 0) > b.get(n, 0) for n in nodes)
    b_ahead = any(b.get(n, 0) > a.get(n, 0) for n in nodes)
    if a_ahead and b_ahead:
        return "concurrent"
    if a_ahead:
        return "a_newer"
    if b_ahead:
        return "b_newer"
    return "equal"


def decide(
    local: dict, peer: dict, local_node: str, peer_node: str, priority: str, follow: bool = False
) -> str:
    """What this machine should do: 'noop' | 'keep' | 'install' | 'install_conflict'.

    'install_conflict' means both sides have writes the other lacks and the peer
    wins; the local copy must be archived first. When the local side wins the answer
    is 'keep' (the peer reaches the opposite conclusion and archives its own copy).

    follow=True is for a replica pulling from the current writer: the writer is always
    right, so anything but "equal" or "peer newer" means this copy holds writes the
    writer lacks (lost in a failover); they are archived and the writer's copy wins.
    """
    result = compare(local["vector"], peer["vector"])
    if result == "equal":
        return "noop"
    if follow:
        return "install" if result == "b_newer" else "install_conflict"
    if result == "a_newer":
        return "keep"
    if result == "b_newer":
        return "install"
    mine, theirs = sum(local["vector"].values()), sum(peer["vector"].values())
    if mine != theirs:
        return "keep" if mine > theirs else "install_conflict"
    # Exactly tied: the preferred node wins; with no preference, the lower name.
    winner = priority if priority in (local_node, peer_node) else min(local_node, peer_node)
    return "keep" if winner == local_node else "install_conflict"


def schema_ok(local: dict, peer: dict, live: bool) -> tuple[bool, str]:
    """May the peer's database be installed here, given what each has migrated?

    A machine with no migrations recorded has no database yet and takes any schema.
    """
    lm, pm = set(local["migrations"]), set(peer["migrations"])
    if not lm:
        return True, ""
    if not pm - lm and (not lm - pm or not live):
        return True, ""
    if pm - lm:
        return False, "the peer's schema is newer than this machine's code; update this machine first"
    return False, "the schemas differ; it will be reconciled at the next restart (migrate)"


# --- archive and install ------------------------------------------------------


def archive_database(path, archive_dir: Path, node: str, reason: str) -> tuple[Path, dict]:
    """Copy the live database aside (consistent, via the backup API). Returns file + its state."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    dest = archive_dir / f"accounts-{node}-{stamp}-{reason}.sqlite3"
    n = 1
    while dest.exists():
        n += 1
        dest = archive_dir / f"accounts-{node}-{stamp}-{reason}-{n}.sqlite3"
    src = connect(path, readonly=True)
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dest, state_of(dest)


def verify_snapshot(path) -> dict:
    """Refuse a damaged or foreign file. Returns its replication state."""
    size = Path(path).stat().st_size
    if size < 512 or size > MAX_SNAPSHOT_BYTES:
        raise ReplicationError(f"snapshot has an implausible size ({size} bytes)")
    with open(path, "rb") as fh:
        if fh.read(16) != b"SQLite format 3\x00":
            raise ReplicationError("snapshot is not an SQLite database")
    conn = connect(path, readonly=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise ReplicationError(f"snapshot failed integrity_check: {result[:3]}")
        if not _has_table(conn, VECTOR_TABLE):
            raise ReplicationError("snapshot has no replication_vector table")
        return read_state(conn)
    finally:
        conn.close()


def _install_by_copy(db_path, staged, node: str, expect: dict | None, expect_decision) -> str:
    """Replace the live data inside ONE exclusive transaction (the app may be running).

    Returns 'installed', or 'changed' when the live database moved since the caller
    decided (the caller starts again). Other connections just wait on the lock.
    """
    conn = connect(db_path, timeout=60)
    try:
        conn.execute("ATTACH DATABASE ? AS stage", (str(staged),))
        conn.execute("BEGIN EXCLUSIVE")
        try:
            local = read_state(conn)
            peer = read_state(conn, "stage")
            if expect is not None and local["vector"] != expect["vector"]:
                conn.execute("ROLLBACK")
                return "changed"
            if expect_decision(local, peer) is None:
                conn.execute("ROLLBACK")
                return "changed"
            main_tables, stage_tables = _tables(conn), _tables(conn, "stage")
            if main_tables != stage_tables:
                raise ReplicationError("table lists differ between the databases")
            for table in main_tables:
                if _columns(conn, table) != _columns(conn, table, "stage"):
                    raise ReplicationError(f"columns of {table} differ between the databases")
            drop_triggers(conn)  # so the copy below does not count as local writes
            for table in main_tables:
                conn.execute(f"DELETE FROM main.{_q(table)}")
                conn.execute(f"INSERT INTO main.{_q(table)} SELECT * FROM stage.{_q(table)}")
            if _has_table(conn, "sqlite_sequence") and _has_table(conn, "sqlite_sequence", "stage"):
                conn.execute("DELETE FROM main.sqlite_sequence")
                conn.execute("INSERT INTO main.sqlite_sequence SELECT * FROM stage.sqlite_sequence")
            ensure_vector_table(conn)
            conn.execute(f"DELETE FROM main.{VECTOR_TABLE}")
            conn.execute(f"INSERT INTO main.{VECTOR_TABLE} SELECT * FROM stage.{VECTOR_TABLE}")
            install_triggers(conn, node)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return "installed"
    finally:
        conn.close()


def _install_by_file(db_path, staged, node: str) -> str:
    """Swap the whole file (only when nothing has the database open: start-up)."""
    conn = connect(staged)
    try:
        conn.execute("BEGIN IMMEDIATE")
        install_triggers(conn, node)
        conn.execute("COMMIT")
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()
    db_path = Path(db_path)
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            os.unlink(str(db_path) + suffix)
        except FileNotFoundError:
            pass
    os.replace(staged, db_path)
    return "installed"


def _headers(cfg: Config) -> dict:
    """Both secrets: the origin secret (proves it came from us, not the public) and
    the replication secret (proves it is allowed to read accounts)."""
    headers = {"User-Agent": USER_AGENT, SECRET_HEADER: cfg.secret}
    if cfg.origin_secret:
        headers[ORIGIN_HEADER] = cfg.origin_secret
    return headers


def download_snapshot(cfg: Config, dest_dir: Path, base: str | None = None) -> Path:
    req = urllib.request.Request((base or cfg.peer_url) + REPL_PREFIX + "/snapshot", headers=_headers(cfg))
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".repl-", suffix=".sqlite3", dir=dest_dir)
    total = 0
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=60) as resp:
            expected = resp.headers.get("Content-Length")
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SNAPSHOT_BYTES:
                    raise ReplicationError("snapshot is too large")
                out.write(chunk)
        if expected is not None and int(expected) != total:
            raise ReplicationError("snapshot download was cut short")
        return Path(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def fetch_peer_state(cfg: Config, timeout: float = 5.0, base: str | None = None) -> dict | None:
    req = urllib.request.Request((base or cfg.peer_url) + REPL_PREFIX + "/state", headers=_headers(cfg))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or "vector" not in data or "node" not in data:
        return None
    data.setdefault("migrations", [])
    return data


def fetch_control(cfg: Config, timeout: float = 5.0) -> dict | None:
    """Ask the Worker who the writer is: {"writer", "epoch", "url"}, or None."""
    req = urllib.request.Request(
        cfg.control_url,
        headers={"User-Agent": USER_AGENT, ORIGIN_HEADER: cfg.origin_secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    ok = isinstance(data, dict) and all(k in data for k in ("writer", "epoch", "url"))
    if not ok or not isinstance(data["epoch"], int):
        return None
    data["url"] = str(data["url"]).rstrip("/")
    return data


# --- one synchronisation step -------------------------------------------------


def sync_once(cfg: Config, live: bool, source: dict | None = None) -> str:
    """Compare with the peer and install its database if the rules say so.

    live=True  : the app may be running; only data is replaced, in one transaction.
    live=False : nothing has the database open (start-up); the file is swapped, so
                 an older peer schema is fine (migrate runs next).
    source (from fetch_control) names the writer to follow; without it cfg.peer_url is used
    and the old two-machine rules apply.
    Returns a short status word, also logged: unreachable, in-sync, kept-local,
    installed, installed-after-archiving, skipped:<why>.
    """
    base = source["url"] if source else None
    follow = source is not None
    peer = fetch_peer_state(cfg, base=base)
    if peer is None:
        return "unreachable"
    peer_node = peer["node"]
    if follow and peer_node != source["writer"]:
        return f"skipped: {source['url']} answered as {peer_node}, not {source['writer']}"
    db_path = cfg.db_path
    local = state_of(db_path)
    action = decide(local, peer, cfg.node, peer_node, cfg.priority, follow)
    if action == "noop":
        return "in-sync"
    if action == "keep":
        return "kept-local"
    ok, why = schema_ok(local, peer, live)
    if not ok:
        return f"skipped: {why}"

    # The listing is only a hint; the downloaded file is the authority.
    staged = download_snapshot(cfg, db_path.parent, base)
    try:
        snap = verify_snapshot(staged)
        snap_node = peer_node
        for attempt in range(5):
            local = state_of(db_path)
            action = decide(local, snap, cfg.node, snap_node, cfg.priority, follow)
            if action in ("noop", "keep"):
                return "in-sync" if action == "noop" else "kept-local"
            ok, why = schema_ok(local, snap, live)
            if not ok:
                return f"skipped: {why}"
            archived = None
            if action == "install_conflict" and db_path.exists():
                archived, arch_state = archive_database(db_path, cfg.archive_dir, cfg.node, "lost-conflict")
                log.warning(
                    "LOST WRITES: this machine holds writes the writer never received. Keeping the writer's database; "
                    "this machine's copy was saved to %s (recover accounts with: "
                    "python -m accounts.replication salvage %s)",
                    archived,
                    archived,
                )
                expect = arch_state
            else:
                expect = local if db_path.exists() else None

            def still_valid(cur, new, _snap_node=snap_node):
                act = decide(cur, new, cfg.node, _snap_node, cfg.priority, follow)
                if act == "install" or (act == "install_conflict" and archived is not None):
                    return act
                return None

            if not db_path.exists():
                result = _install_by_file(db_path, staged, cfg.node)
            elif live:
                result = _install_by_copy(db_path, staged, cfg.node, expect, still_valid)
            else:
                result = _install_by_file(db_path, staged, cfg.node)
            if result == "installed":
                return "installed-after-archiving" if archived else "installed"
            # 'changed': the live database moved while we prepared; look again.
        return "skipped: the live database kept changing; will retry"
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass


def pull_once(cfg: Config, live: bool) -> str:
    """One step: find out who the writer is, then follow it (or do nothing if it is us)."""
    if not cfg.control_url:
        return sync_once(cfg, live)  # fixed peer
    ctl = fetch_control(cfg)
    if ctl is None:
        return "unreachable"
    writer.observe_epoch(cfg.epoch_path, ctl["epoch"])  # so a replaced writer refuses old epochs
    if ctl["writer"] == cfg.node:
        return "i-am-writer"
    return sync_once(cfg, live, source=ctl)


def catch_up(cfg: Config, wait: float = 0.0) -> str:
    """The start-up gate: do not serve until we have the peer's newer data (if reachable)."""
    deadline = time.monotonic() + wait
    while True:
        try:
            result = pull_once(cfg, live=False)
        except (ReplicationError, sqlite3.Error, OSError, urllib.error.URLError) as exc:
            result = f"failed: {exc}"
        if result != "unreachable" and not result.startswith("failed"):
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(2)


# --- the peer-facing server ---------------------------------------------------


def _peer_allowed(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in net for net in _PRIVATE_NETS)


def make_handler(cfg: Config):
    snapshots = threading.BoundedSemaphore(2)

    class Handler(BaseHTTPRequestHandler):
        server_version = "yabai-repl"

        def log_message(self, fmt, *args):  # quiet: the daemon logs what matters
            log.debug("%s %s", self.address_string(), fmt % args)

        def _deny(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _authorised(self) -> bool:
            # Through cloudflared every caller looks like 127.0.0.1, so the secrets are
            # the real gate: the origin secret (when set) AND the replication secret.
            if not _peer_allowed(self.client_address[0]):
                return False
            if cfg.origin_secret:
                origin = self.headers.get(ORIGIN_HEADER, "")
                if not hmac.compare_digest(origin.encode(), cfg.origin_secret.encode()):
                    return False
            sent = self.headers.get(SECRET_HEADER, "")
            return hmac.compare_digest(sent.encode(), cfg.secret.encode())

        def do_GET(self):
            if not self._authorised():
                return self._deny(403)
            # cloudflared forwards the full path; the plain /state form still works too.
            path = self.path[len(REPL_PREFIX):] if self.path.startswith(REPL_PREFIX + "/") else self.path
            try:
                if path == "/state":
                    state = state_of(cfg.db_path)
                    state["node"] = cfg.node
                    body = json.dumps(state).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/snapshot":
                    self._send_snapshot()
                else:
                    self._deny(404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.exception("replication request failed")
                try:
                    self._deny(500)
                except OSError:
                    pass

        def _send_snapshot(self):
            if not cfg.db_path.exists():
                return self._deny(404)
            if not snapshots.acquire(blocking=False):
                return self._deny(503)
            tmpdir = tempfile.mkdtemp(prefix="repl-out-", dir=cfg.db_path.parent)
            try:
                copy = Path(tmpdir) / "snapshot.sqlite3"
                src = connect(cfg.db_path, readonly=True)
                dst = sqlite3.connect(str(copy))
                try:
                    src.backup(dst)  # a consistent copy even while the app writes
                finally:
                    dst.close()
                    src.close()
                size = copy.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(copy, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, 1 << 20)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                snapshots.release()

    return Handler


def serve(cfg: Config) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((cfg.bind, cfg.port), make_handler(cfg))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="repl-server", daemon=True).start()
    return server


def pull_loop(cfg: Config) -> None:
    """Pull the peer's database whenever it is newer. Runs until stopped."""
    last = None
    while True:
        try:
            if cfg.db_path.exists():
                result = pull_once(cfg, live=True)
            else:
                result = "waiting for the database to be created"
        except (ReplicationError, sqlite3.Error, OSError, urllib.error.URLError) as exc:
            result = f"failed: {exc}"
        if result != last or result.startswith(("installed", "failed")):
            log.info("sync: %s", result)
            last = result
        time.sleep(cfg.interval)


def daemon(cfg: Config) -> None:
    """Serve our state to the peer and keep pulling theirs when it is newer."""
    cfg.validate()
    serve(cfg)
    log.info("replication node %s serving on %s:%s, control %s", cfg.node, cfg.bind, cfg.port, cfg.control_url or cfg.peer_url)
    pull_loop(cfg)


# --- recovering accounts from an archived (losing) database -------------------


def salvage(cfg: Config, archive: Path, dry_run: bool = False) -> dict:
    """Copy accounts that exist only in `archive` into the live database.

    A user is copied (with its password hash and settings, under a NEW id) only when
    the username is not already taken. Taken usernames are reported, not touched.
    Sessions are not copied: those people just log in again.
    """
    conn = connect(cfg.db_path, timeout=60)
    report = {"copied": [], "skipped_taken": []}
    try:
        conn.execute("ATTACH DATABASE ? AS lost", (str(archive),))
        conn.execute("BEGIN IMMEDIATE")
        try:
            user_cols = [c[0] for c in _columns(conn, "auth_user") if c[0] != "id"]
            lost_cols = {c[0] for c in _columns(conn, "auth_user", "lost")}
            cols = [c for c in user_cols if c in lost_cols]
            rows = conn.execute(
                f"SELECT id, username FROM lost.auth_user WHERE username NOT IN "
                f"(SELECT username FROM main.auth_user)"
            ).fetchall()
            taken = conn.execute(
                "SELECT username FROM lost.auth_user WHERE username IN (SELECT username FROM main.auth_user)"
            ).fetchall()
            report["skipped_taken"] = [r[0] for r in taken]
            col_sql = ", ".join(_q(c) for c in cols)
            has_settings = _has_table(conn, "accounts_usersettings") and _has_table(
                conn, "accounts_usersettings", "lost"
            )
            for old_id, username in rows:
                conn.execute(
                    f"INSERT INTO main.auth_user ({col_sql}) SELECT {col_sql} FROM lost.auth_user WHERE id = ?",
                    (old_id,),
                )
                new_id = conn.execute("SELECT id FROM main.auth_user WHERE username = ?", (username,)).fetchone()[0]
                if has_settings:
                    scols = [c[0] for c in _columns(conn, "accounts_usersettings") if c[0] not in ("id", "user_id")]
                    slost = {c[0] for c in _columns(conn, "accounts_usersettings", "lost")}
                    scols = [c for c in scols if c in slost]
                    ssql = ", ".join(_q(c) for c in scols)
                    conn.execute(
                        f"INSERT INTO main.accounts_usersettings (user_id, {ssql}) "
                        f"SELECT ?, {ssql} FROM lost.accounts_usersettings WHERE user_id = ?",
                        (new_id, old_id),
                    )
                report["copied"].append(username)
            if dry_run:
                conn.execute("ROLLBACK")
            else:
                conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return report


# --- command line -------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m accounts.replication")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("catch-up", help="download the peer's newer database before serving")
    p.add_argument("--wait", type=float, default=0, help="seconds to keep retrying if the peer is down")
    sub.add_parser("daemon", help="serve our state to the peer and pull theirs")
    sub.add_parser("serve", help="only serve our state to the peer (start before catch-up)")
    sub.add_parser("pull", help="only run the pull loop (start after catch-up and migrate)")
    sub.add_parser("status", help="show this machine's version vector and the peer's")
    p = sub.add_parser("setup", help="add the vector table and triggers (also done by migrate)")
    p = sub.add_parser("salvage", help="re-add accounts that only exist in an archived database")
    p.add_argument("archive")
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="[repl] %(message)s", stream=sys.stderr)
    cfg = Config()
    if not cfg.enabled:
        print("replication is off (YABAI_REPL_NODE is not set)", file=sys.stderr)
        return 0
    try:
        cfg.validate(need_peer=args.cmd in ("catch-up", "daemon", "pull"))
        if args.cmd == "catch-up":
            result = catch_up(cfg, wait=args.wait)
            log.info("catch-up: %s", result)
            return 0
        if args.cmd == "daemon":
            daemon(cfg)
        elif args.cmd == "serve":
            serve(cfg)
            log.info("serving replication state on %s:%s", cfg.bind, cfg.port)
            threading.Event().wait()
        elif args.cmd == "pull":
            pull_loop(cfg)
        elif args.cmd == "setup":
            setup(cfg.db_path, cfg.node)
        elif args.cmd == "status":
            print("local:", json.dumps(state_of(cfg.db_path)["vector"]))
            ctl = fetch_control(cfg) if cfg.control_url else None
            if cfg.control_url:
                print("writer:", f"{ctl['writer']} (epoch {ctl['epoch']})" if ctl else "unknown (Worker unreachable)")
            base = ctl["url"] if ctl else None
            peer = fetch_peer_state(cfg, base=base) if (base or cfg.peer_url) else None
            print("writer state:", json.dumps(peer["vector"]) if peer else "unreachable")
            if peer:
                print("verdict:", decide(state_of(cfg.db_path), peer, cfg.node, peer["node"], cfg.priority, bool(ctl)))
        elif args.cmd == "salvage":
            report = salvage(cfg, Path(args.archive), args.dry_run)
            print(json.dumps(report, indent=2))
    except ReplicationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
