"""Sign up, log in, log out, and the per-user settings API."""

from __future__ import annotations

import json

from django.conf import settings
from datetime import timedelta

from django.contrib.auth import get_user_model, login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import LoginView
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from yabai_search import services
from yabai_site.middleware import client_ip

from .models import UserSettings
from .throttle import login_throttle

MAX_SETTINGS_BYTES = 200_000


class ThrottledLoginView(LoginView):
    """Django's login view plus a failure counter (see accounts/throttle.py)."""

    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def _keys(self, request):
        username = (request.POST.get("username") or "").strip().lower()[:150]
        return f"ip:{client_ip(request)}", f"user:{username}"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["signups_open"] = settings.SIGNUPS_OPEN
        context["asset_version"] = services.asset_version()
        return context

    def post(self, request, *args, **kwargs):
        ip_key, user_key = self._keys(request)
        if login_throttle.blocked(ip_key, user_key):
            form = self.get_form()
            form.add_error(None, "Too many failed attempts. Try again in a few minutes.")
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        login_throttle.fail(*self._keys(self.request))
        return super().form_invalid(form)

    def form_valid(self, form):
        login_throttle.clear(*self._keys(self.request))
        return super().form_valid(form)


RETRY_WINDOW = timedelta(minutes=2)


def _repeat_of_signup(request):
    """The account just created by this same POST, if this is a repeat of it.

    The Worker can send a POST to the tablet after the laptop already handled it
    (a 502 after the app wrote). The second attempt would fail with "username
    taken" although the account exists. Same username + same password + created
    within two minutes is treated as that repeat. Wrong passwords count against the
    login throttle, so this is no easier to guess than the login form.
    """
    username = (request.POST.get("username") or "").strip()
    password = request.POST.get("password1") or ""
    if not username or not password:
        return None
    user = get_user_model().objects.filter(username=username).first()
    if user is None or not user.is_active or timezone.now() - user.date_joined > RETRY_WINDOW:
        return None
    keys = (f"ip:{client_ip(request)}", f"user:{username.lower()[:150]}")
    if login_throttle.blocked(*keys):
        return None
    if user.check_password(password):
        return user
    login_throttle.fail(*keys)
    return None


@require_http_methods(["GET", "POST"])
def signup(request):
    if not settings.SIGNUPS_OPEN:
        raise Http404
    if request.user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        repeat = _repeat_of_signup(request)
        if repeat is not None:
            login(request, repeat, backend="django.contrib.auth.backends.ModelBackend")
            return redirect("/")
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("/")
    status = 400 if request.method == "POST" else 200
    return render(
        request,
        "accounts/signup.html",
        {"form": form, "asset_version": services.asset_version()},
        status=status,
    )


def _unauthorized() -> HttpResponse:
    return HttpResponse(
        json.dumps({"detail": "Not authenticated"}),
        status=401,
        content_type="application/json",
    )


@require_http_methods(["GET", "PUT"])
def user_settings(request):
    """GET returns the signed-in user's settings; PUT replaces them.

    PUT is protected by Django's CSRF check: send the csrftoken cookie's value in
    an X-CSRFToken header (the index page sets the cookie for signed-in users).
    """
    if not request.user.is_authenticated:
        return _unauthorized()
    if request.method == "GET":
        # A read: do not create the row here (a replica may not write).
        record = UserSettings.objects.filter(user=request.user).first()
        return HttpResponse(
            json.dumps(
                {
                    "data": record.data if record else {},
                    "updated_at": record.updated_at.isoformat() if record else None,
                }
            ),
            content_type="application/json",
        )
    record, _ = UserSettings.objects.get_or_create(user=request.user)
    if len(request.body) > MAX_SETTINGS_BYTES:
        return HttpResponse(
            json.dumps({"detail": "Settings are too large."}),
            status=413,
            content_type="application/json",
        )
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        return HttpResponse(
            json.dumps({"detail": "Send a JSON object."}),
            status=400,
            content_type="application/json",
        )
    record.data = data
    record.save()
    return HttpResponse(
        json.dumps({"ok": True, "updated_at": record.updated_at.isoformat()}),
        content_type="application/json",
    )
