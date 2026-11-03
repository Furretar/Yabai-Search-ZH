from django.conf import settings
from django.db import models


class UserSettings(models.Model):
    """One JSON blob of per-user settings (theme, language, dictionary order...).

    The browser keeps these in local storage today; this table is where a signed-in
    user's copy lives so it can follow them between devices.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="yabai_settings",
    )
    data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"settings for {self.user}"
