"""Replication engine tests. Standard library only: no Django, no network beyond loopback."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from accounts import replication as r

SCHEMA = """
CREATE TABLE django_migrations (id integer PRIMARY KEY AUTOINCREMENT, app varchar(255), name varchar(255));
CREATE TABLE auth_user (id integer PRIMARY KEY AUTOINCREMENT, password varchar(128), username varchar(150) UNIQUE, is_staff bool NOT NULL DEFAULT 0);
CREATE TABLE accounts_usersettings (id integer PRIMARY KEY AUTOINCREMENT, user_id integer UNIQUE REFERENCES auth_user(id), data text);
CREATE TABLE django_session (session_key varchar(40) PRIMARY KEY, session_data text);
"""


def make_db(path: Path, node: str, migrations=(("auth", "0001"), ("accounts", "0001"))) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO django_migrations (app, name) VALUES (?, ?)", migrations)
    conn.commit()
    conn.close()
    r.setup(path, node)
    return path


def write_user(path: Path, name: str, settings: str = "{}") -> None:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO auth_user (password, username) VALUES ('hash-' || ?, ?)", (name, name))
    conn.execute("INSERT INTO accounts_usersettings (user_id, data) VALUES (?, ?)", (cur.lastrowid, settings))
    conn.commit()
    conn.close()


def users(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [x[0] for x in conn.execute("SELECT username FROM auth_user ORDER BY username")]
    finally:
        conn.close()


def vec(path: Path) -> dict:
    return r.state_of(path)["vector"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SECRET = "s" * 32


@pytest.fixture
def pair(tmp_path):
    """Two machines, each with its own database and a peer server on loopback."""
    ports = {"laptop": free_port(), "tablet": free_port()}
    cfgs, servers = {}, []
    for node, other in (("laptop", "tablet"), ("tablet", "laptop")):
        d = tmp_path / node
        d.mkdir()
        cfgs[node] = r.Config(
            {
                "YABAI_REPL_NODE": node,
                "YABAI_REPL_PEER_URL": f"http://127.0.0.1:{ports[other]}",
                "YABAI_REPL_SECRET": SECRET,
                "YABAI_REPL_PORT": str(ports[node]),
                "DJANGO_DB_PATH": str(d / "accounts.sqlite3"),
            }
        )
    yield cfgs, servers, ports
    for s in servers:
        s.shutdown()
        s.server_close()


def start(cfg, servers):
    srv = r.serve(cfg)
    servers.append(srv)
    return srv


def stop(srv, servers):
    srv.shutdown()
    srv.server_close()
    servers.remove(srv)


# --- counters -------------------------------------------------------------------


def test_triggers_count_only_local_writes(tmp_path):
    db = make_db(tmp_path / "a.sqlite3", "laptop")
    assert vec(db) == {"laptop": 0}
    write_user(db, "alice")  # insert user + insert settings
    assert vec(db) == {"laptop": 2}
    conn = sqlite3.connect(db)
    conn.execute("UPDATE auth_user SET is_staff = 1")
    conn.execute("DELETE FROM django_session")  # no rows: no trigger fires
    conn.commit()
    conn.close()
    assert vec(db) == {"laptop": 3}


def test_setup_is_idempotent_and_covers_new_tables(tmp_path):
    db = make_db(tmp_path / "a.sqlite3", "laptop")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE later (x)")
    conn.commit()
    conn.close()
    r.setup(db, "laptop")
    r.setup(db, "laptop")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO later VALUES (1)")
    conn.commit()
    names = [x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
    conn.close()
    assert len(names) == len(set(names)) == 3 * 5
    assert vec(db) == {"laptop": 1}


# --- deciding -------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ({"l": 3}, {"l": 3}, "equal"),
        ({}, {}, "equal"),
        ({"l": 3}, {"l": 5}, "b_newer"),
        ({"l": 5, "t": 1}, {"l": 5}, "a_newer"),
        ({"l": 5}, {"l": 4, "t": 2}, "concurrent"),
        ({"l": 5}, {"t": 1}, "concurrent"),
    ],
)
def test_compare(a, b, expected):
    assert r.compare(a, b) == expected


def _st(v, migs=()):
    return {"vector": v, "last_write": 0, "migrations": list(migs)}


def test_decide_never_installs_a_stale_peer():
    assert r.decide(_st({"laptop": 9}), _st({"laptop": 4}), "tablet", "laptop", "laptop") == "keep"


def test_decide_conflict_more_writes_wins_then_priority():
    laptop, tablet = _st({"laptop": 7}), _st({"laptop": 5, "tablet": 4})
    # tablet has 9 writes vs laptop 7: tablet wins from both points of view
    assert r.decide(laptop, tablet, "laptop", "tablet", "laptop") == "install_conflict"
    assert r.decide(tablet, laptop, "tablet", "laptop", "laptop") == "keep"
    tie_l, tie_t = _st({"laptop": 6}), _st({"tablet": 6})
    assert r.decide(tie_l, tie_t, "laptop", "tablet", "laptop") == "keep"
    assert r.decide(tie_t, tie_l, "tablet", "laptop", "laptop") == "install_conflict"


def test_schema_rules():
    a = _st({}, ["x.1"])
    assert r.schema_ok(a, _st({}, ["x.1"]), live=True)[0]
    assert not r.schema_ok(a, _st({}, ["x.1", "x.2"]), live=True)[0]  # peer ahead
    assert not r.schema_ok(a, _st({}, ["x.1", "x.2"]), live=False)[0]
    assert not r.schema_ok(_st({}, ["x.1", "x.2"]), a, live=True)[0]  # local ahead, live
    assert r.schema_ok(_st({}, ["x.1", "x.2"]), a, live=False)[0]  # start-up: migrate follows


# --- installing -----------------------------------------------------------------


def test_catch_up_installs_newer_peer_and_rebuilds_triggers(pair):
    cfgs, servers, _ = pair
    lap, tab = cfgs["laptop"], cfgs["tablet"]
    make_db(lap.db_path, "laptop")
    make_db(tab.db_path, "tablet")
    write_user(tab.db_path, "bob")  # the tablet took a signup while the laptop was away
    # (laptop's vector knows nothing of tablet writes, and tablet knows no laptop writes)
    start(tab, servers)
    # Equalise history first: the laptop is a copy of the tablet from before the outage
    assert r.sync_once(lap, live=False).startswith("installed")
    assert users(lap.db_path) == ["bob"]
    assert r.compare(vec(lap.db_path), vec(tab.db_path)) == "equal"
    # Triggers now count for the LAPTOP, not the tablet that the snapshot came from.
    write_user(lap.db_path, "carol")
    v = vec(lap.db_path)
    assert v["tablet"] == vec(tab.db_path)["tablet"] and v["laptop"] == 2
    # sqlite_sequence came along: the next id is not reused.
    conn = sqlite3.connect(lap.db_path)
    ids = [x[0] for x in conn.execute("SELECT id FROM auth_user ORDER BY id")]
    conn.close()
    assert ids == [1, 2]


def test_stale_peer_is_never_installed(pair):
    cfgs, servers, _ = pair
    lap, tab = cfgs["laptop"], cfgs["tablet"]
    make_db(lap.db_path, "laptop")
    write_user(lap.db_path, "newer")
    make_db(tab.db_path, "tablet")
    start(tab, servers)
    assert r.sync_once(lap, live=True) == "kept-local"
    assert users(lap.db_path) == ["newer"]


def test_returning_laptop_does_not_overwrite_newer_tablet_with_its_stale_copy(pair):
    """The race from the design notes: the tablet's periodic pull fires after the
    laptop is back but before it has caught up."""
    cfgs, servers, _ = pair
    lap, tab = cfgs["laptop"], cfgs["tablet"]
    make_db(lap.db_path, "laptop")
    write_user(lap.db_path, "old")
    make_db(tab.db_path, "tablet")
    # tablet starts as a copy of the laptop...
    start(lap, servers)
    assert r.sync_once(tab, live=False).startswith("installed")
    stop(servers[-1], servers)
    # ...takes a write while the laptop is down...
    write_user(tab.db_path, "during-outage")
    # ...and the laptop comes back serving its STALE database before catching up.
    start(lap, servers)
    assert r.sync_once(tab, live=True) == "kept-local"
    assert users(tab.db_path) == ["during-outage", "old"]
    # The laptop's own catch-up then downloads the tablet's newer database.
    start(tab, servers)
    assert r.sync_once(lap, live=False) == "installed"
    assert users(lap.db_path) == ["during-outage", "old"]


def test_split_brain_archives_the_loser_and_salvage_recovers_its_accounts(pair):
    cfgs, servers, _ = pair
    lap, tab = cfgs["laptop"], cfgs["tablet"]
    make_db(lap.db_path, "laptop")
    write_user(lap.db_path, "base")
    make_db(tab.db_path, "tablet")
    start(lap, servers)
    r.sync_once(tab, live=False)
    stop(servers[-1], servers)
    # Both machines now take writes (tunnel dropped while the process kept running).
    write_user(lap.db_path, "on-laptop")  # 2 writes
    write_user(tab.db_path, "on-tablet-1")
    write_user(tab.db_path, "on-tablet-2")  # 4 writes: the tablet wins
    start(tab, servers)
    result = r.sync_once(lap, live=True)
    assert result == "installed-after-archiving"
    assert users(lap.db_path) == ["base", "on-tablet-1", "on-tablet-2"]
    archives = list(lap.archive_dir.glob("accounts-laptop-*-lost-conflict*.sqlite3"))
    assert len(archives) == 1 and users(archives[0]) == ["base", "on-laptop"]
    # And the tablet, seeing the laptop, keeps its own.
    start(lap, servers)
    assert r.sync_once(tab, live=True) in ("in-sync", "kept-local")

    report = r.salvage(lap, archives[0])
    assert report["copied"] == ["on-laptop"] and report["skipped_taken"] == ["base"]
    assert users(lap.db_path) == ["base", "on-laptop", "on-tablet-1", "on-tablet-2"]
    conn = sqlite3.connect(lap.db_path)
    n = conn.execute(
        "SELECT count(*) FROM accounts_usersettings s JOIN auth_user u ON u.id = s.user_id "
        "WHERE u.username = 'on-laptop'"
    ).fetchone()[0]
    pw = conn.execute("SELECT password FROM auth_user WHERE username='on-laptop'").fetchone()[0]
    conn.close()
    assert n == 1 and pw == "hash-on-laptop"


def test_live_install_is_refused_if_the_live_database_moved(tmp_path):
    live = make_db(tmp_path / "live.sqlite3", "laptop")
    snap = make_db(tmp_path / "snap.sqlite3", "tablet")
    write_user(snap, "x")
    expect = r.state_of(live)
    write_user(live, "raced")  # the app wrote after the decision
    out = r._install_by_copy(live, snap, "laptop", expect, lambda cur, new: "install")
    assert out == "changed" and users(live) == ["raced"]


def test_failed_install_rolls_back_completely(tmp_path):
    live = make_db(tmp_path / "live.sqlite3", "laptop")
    write_user(live, "keepme")
    snap = make_db(tmp_path / "snap.sqlite3", "tablet")
    conn = sqlite3.connect(snap)
    conn.execute("ALTER TABLE auth_user ADD COLUMN extra text")
    conn.commit()
    conn.close()
    with pytest.raises(r.ReplicationError):
        r._install_by_copy(live, snap, "laptop", None, lambda cur, new: "install")
    assert users(live) == ["keepme"]
    assert vec(live) == {"laptop": 2}


def test_app_connection_keeps_working_during_install(tmp_path):
    """A connection the app already holds sees the new data and can still write."""
    live = make_db(tmp_path / "live.sqlite3", "laptop")
    snap = make_db(tmp_path / "snap.sqlite3", "tablet")
    write_user(snap, "from-peer")
    app = sqlite3.connect(live, timeout=30)
    assert app.execute("SELECT count(*) FROM auth_user").fetchone()[0] == 0
    assert r._install_by_copy(live, snap, "laptop", None, lambda c, n: "install") == "installed"
    assert [x[0] for x in app.execute("SELECT username FROM auth_user")] == ["from-peer"]
    app.execute("INSERT INTO auth_user (password, username) VALUES ('h', 'new')")
    app.commit()
    app.close()
    assert vec(live)["laptop"] == 1


def test_missing_local_database_is_created_from_the_peer(pair):
    cfgs, servers, _ = pair
    lap, tab = cfgs["laptop"], cfgs["tablet"]
    make_db(lap.db_path, "laptop")
    write_user(lap.db_path, "alice")
    start(lap, servers)
    assert not tab.db_path.exists()
    assert r.catch_up(tab, wait=0).startswith("installed")
    assert users(tab.db_path) == ["alice"]
    write_user(tab.db_path, "bob")
    assert vec(tab.db_path)["tablet"] == 2


def test_catch_up_with_peer_down_lets_us_serve(pair):
    cfgs, _, _ = pair
    make_db(cfgs["tablet"].db_path, "tablet")
    assert r.catch_up(cfgs["tablet"], wait=0) == "unreachable"


def test_startup_install_accepts_older_peer_schema(pair):
    cfgs, servers, _ = pair
    lap, tab = cfgs["laptop"], cfgs["tablet"]
    make_db(lap.db_path, "laptop", migrations=(("auth", "0001"),))
    write_user(lap.db_path, "from-old-schema")
    make_db(tab.db_path, "tablet")  # tablet code is newer (extra migration)
    start(lap, servers)
    assert r.sync_once(tab, live=True).startswith("skipped")
    assert r.sync_once(tab, live=False) == "installed"
    assert users(tab.db_path) == ["from-old-schema"]


def test_snapshot_is_consistent_while_the_app_writes(pair):
    cfgs, servers, _ = pair
    lap, tab = cfgs["laptop"], cfgs["tablet"]
    make_db(lap.db_path, "laptop")
    start(lap, servers)
    stopped = threading.Event()

    def writer():
        i = 0
        while not stopped.is_set():
            write_user(lap.db_path, f"u{i}")
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        results = [r.sync_once(tab, live=False) for _ in range(3)]
    finally:
        stopped.set()
        t.join()
    assert all(x.startswith("installed") or x in ("in-sync", "kept-local") for x in results), results
    conn = sqlite3.connect(tab.db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()


# --- the server -----------------------------------------------------------------


def test_server_rejects_missing_or_wrong_secret_and_unknown_paths(pair):
    cfgs, servers, ports = pair
    make_db(cfgs["laptop"].db_path, "laptop")
    start(cfgs["laptop"], servers)
    base = f"http://127.0.0.1:{ports['laptop']}"

    def code(path, secret=None):
        req = urllib.request.Request(base + path, headers={r.SECRET_HEADER: secret} if secret else {})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    assert code("/state") == 403
    assert code("/state", "wrong" * 8) == 403
    assert code("/snapshot") == 403
    assert code("/state", SECRET) == 200
    assert code("/nope", SECRET) == 404
    state = json.load(urllib.request.urlopen(urllib.request.Request(base + "/state", headers={r.SECRET_HEADER: SECRET})))
    assert state["node"] == "laptop" and state["vector"] == {"laptop": 0}


def test_public_addresses_are_not_peers():
    assert r._peer_allowed("100.101.102.103")  # Tailscale
    assert r._peer_allowed("127.0.0.1") and r._peer_allowed("192.168.1.5")
    assert r._peer_allowed("::ffff:10.0.0.4")
    assert not r._peer_allowed("8.8.8.8") and not r._peer_allowed("nonsense")


def test_truncated_or_corrupt_snapshots_are_rejected(tmp_path):
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(r.ReplicationError):
        r.verify_snapshot(bad)
    good = make_db(tmp_path / "good.sqlite3", "laptop")
    cut = tmp_path / "cut.sqlite3"
    cut.write_bytes(good.read_bytes()[:700])
    with pytest.raises((r.ReplicationError, sqlite3.Error)):
        r.verify_snapshot(cut)


def test_config_validation():
    with pytest.raises(r.ReplicationError):
        r.Config({"YABAI_REPL_NODE": "laptop", "YABAI_REPL_SECRET": "short"}).validate()
    assert not r.Config({}).enabled


def test_live_installs_while_the_app_keeps_writing(tmp_path):
    """No errors, no corruption, and the vector never goes backwards for a pure install."""
    live = make_db(tmp_path / "live.sqlite3", "laptop")
    errors, stop_flag, count = [], threading.Event(), [0]

    def app():
        conn = sqlite3.connect(live, timeout=60)
        while not stop_flag.is_set():
            try:
                conn.execute("INSERT INTO django_session VALUES (?, 'x')", (f"k{count[0]}",))
                conn.commit()
                count[0] += 1
            except sqlite3.Error as exc:  # pragma: no cover - would fail the test
                errors.append(exc)
                return

    t = threading.Thread(target=app)
    t.start()
    try:
        for i in range(5):
            snap = make_db(tmp_path / f"snap{i}.sqlite3", "tablet")
            write_user(snap, f"p{i}")
            # Whatever the app did meanwhile, a changed live DB must be reported, never overwritten blindly.
            for _ in range(20):
                out = r._install_by_copy(live, snap, "laptop", r.state_of(live), lambda c, n: "install")
                if out == "installed":
                    break
            assert out in ("installed", "changed")
    finally:
        stop_flag.set()
        t.join()
    assert not errors, errors
    conn = sqlite3.connect(live)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
