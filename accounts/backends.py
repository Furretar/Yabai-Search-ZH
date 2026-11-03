"""A login backend that never writes to the database.

Django's ModelBackend quietly re-saves a user's password hash when the hasher or its
settings changed since the hash was made. On a replica that write is forbidden (only
the global writer may write), so this backend checks the password WITHOUT the upgrade
step. Hashes are then upgraded the next time the person changes their password on
the writer. Used only when YABAI_REPL_NODE is set (see yabai_site/settings.py).
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import check_password


class ReadOnlyModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        if username is None:
            username = kwargs.get(user_model.USERNAME_FIELD)
        if username is None or password is None:
            return None
        try:
            user = user_model._default_manager.get_by_natural_key(username)
        except user_model.DoesNotExist:
            # Hash anyway so a missing user takes as long as a wrong password.
            user_model().set_password(password)
            return None
        # No `setter` argument: that is what turns off the silent re-save.
        if check_password(password, user.password) and self.user_can_authenticate(user):
            return user
        return None
