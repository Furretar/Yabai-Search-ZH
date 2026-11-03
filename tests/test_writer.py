"""Write fencing (accounts/writer.py). Standard library only."""

from accounts import writer as w


def test_only_unsafe_requests_on_account_paths_are_writes():
    assert w.is_write("POST", "/accounts/signup/")
    assert w.is_write("put", "/api/account/settings")
    assert not w.is_write("GET", "/accounts/signup/")
    assert not w.is_write("POST", "/accounts/login/")  # logins work on every host
    assert not w.is_write("POST", "/api/convert")  # stateless, stays on the local host
    assert w.is_write("POST", "/admin/login/", prefixes=("/admin/",))


def test_a_write_must_name_this_host_and_a_current_epoch(tmp_path):
    f = tmp_path / "writer-epoch"
    assert w.check_write(f, "laptop", "laptop", "4") == (True, "")
    assert w.highest_epoch(f) == 4
    assert w.check_write(f, "laptop", "laptop", "5")[0]  # newer is fine
    ok, why = w.check_write(f, "laptop", "laptop", "4")
    assert not ok and "stale" in why
    ok, why = w.check_write(f, "laptop", "tablet", "9")
    assert not ok and "tablet" in why
    assert w.highest_epoch(f) == 5  # a refused write records nothing


def test_missing_or_garbage_headers_are_refused(tmp_path):
    f = tmp_path / "writer-epoch"
    for bad in ("", "x", None):
        assert not w.check_write(f, "laptop", "laptop", bad)[0]
    assert not f.exists()


def test_a_replaced_writer_learns_the_new_epoch_from_replication(tmp_path):
    f = tmp_path / "writer-epoch"
    w.check_write(f, "laptop", "laptop", "1")
    assert w.observe_epoch(f, 2)  # the replication process saw tablet become writer
    assert not w.observe_epoch(f, 2) and not w.observe_epoch(f, 1)  # never goes down
    assert not w.check_write(f, "laptop", "laptop", "1")[0]
