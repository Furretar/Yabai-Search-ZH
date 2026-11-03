import os

from django.apps import AppConfig, apps
from django.conf import settings
from django.db.models.signals import post_migrate, pre_migrate


def _replication_target():
    """(path, node) when replication is on and the accounts DB is a SQLite file."""
    node = (os.environ.get("YABAI_REPL_NODE") or "").strip()
    db = settings.DATABASES.get("default", {})
    if not node or not db.get("ENGINE", "").endswith("sqlite3"):
        return None
    path = str(db.get("NAME", ""))
    if not path or path == ":memory:" or path.startswith("file:"):
        return None
    return path, node


def _before_migrate(sender, using="default", **kwargs):
    """Turn the write counters off while migrations run.

    Schema rebuilds drop tables (and their triggers) anyway, and migration-time
    writes (content types, permissions) must not count: both machines run the
    same migrations and would otherwise look like they had diverged.
    """
    target = _replication_target()
    if target is None or not os.path.exists(target[0]) or using != "default":
        return
    from . import replication

    conn = replication.connect(target[0])
    try:
        conn.execute("BEGIN IMMEDIATE")
        replication.drop_triggers(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()


def _after_migrate(sender, using="default", **kwargs):
    """Turn the counters back on, once, after the LAST app's post_migrate.

    Earlier apps' receivers (auth permissions, content types) still have writes to
    make; those run before this and so are not counted.
    """
    last = [c for c in apps.get_app_configs() if c.models_module is not None][-1]
    target = _replication_target()
    if target is None or sender is not last or using != "default":
        return
    from . import replication

    replication.setup(target[0], target[1])


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        pre_migrate.connect(_before_migrate, dispatch_uid="yabai_repl_pre")
        post_migrate.connect(_after_migrate, dispatch_uid="yabai_repl_post")
