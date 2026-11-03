"""URL routes: the dictionary pages and API, accounts, and the admin site."""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("api/account/", include("accounts.api_urls")),
    path("", include("dictionary.urls")),
]
