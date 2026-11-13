"""Request middleware: the Django replacement for the old FastAPI guard."""

from __future__ import annotations

import hmac
import ipaddress
import os
import threading
import time
import uuid

from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse

from accounts import writer
from yabai_search.debug import perf as pd

# Private ranges that may reach the origin without the shared secret: the home
# LAN and Tailscale.
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
    )
)


def _parse_ip(text: str):
    try:
        return ipaddress.ip_address(text.strip().strip("[]"))
    except ValueError:
        return None


def client_ip(request) -> str:
    """The visitor's address as best the origin can tell.

    gunicorn only sees cloudflared (127.0.0.1) as the peer. When the peer is
    loopback, use X-Forwarded-For, reading from the RIGHT and skipping loopback
    entries: Cloudflare appends the real address, so a forged value a visitor
    put at the left of the header is never reached (this is what uvicorn's
    proxy-headers option did for the old guard).
    """
    peer = request.META.get("REMOTE_ADDR", "")
    peer_ip = _parse_ip(peer)
    if peer_ip is not None and peer_ip.is_loopback:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        for part in reversed(forwarded.split(",")):
            candidate = _parse_ip(part)
            if candidate is not None and not candidate.is_loopback:
                return str(candidate)
    return peer


def _is_private(address: str) -> bool:
    ip = _parse_ip(address)
    return ip is not None and any(ip in net for net in _PRIVATE_NETS)


_PUBLIC_DOMAIN = "yabaisearch.org"


def _is_own_host_page(request) -> bool:
    """True for a read of this machine's own <node>.yabaisearch.org name.

    YABAI_REPL_NODE is the role from deploy/run.sh (laptop, pc, tabs, tabs2).
    Each host tunnel is reachable directly at that name, but it has no Worker
    secret. Allowing GET/HEAD lets you open and pin a host page directly;
    everything else still needs the Worker or a private-network connection.
    """
    node = os.environ.get("YABAI_REPL_NODE", "").strip().lower()
    if not node:
        return False
    host = request.get_host().split(":", 1)[0].strip().lower().rstrip(".")
    return host == f"{node}.{_PUBLIC_DOMAIN}"


class OriginSecretMiddleware:
    """Turn away requests that skipped the Cloudflare Worker.

    Active only when YABAI_ORIGIN_SECRET is set. The Worker adds the matching
    X-Origin-Secret header. Visitors on the LAN or Tailscale may connect directly.
    A host's own public name may serve GET/HEAD without the secret, so a host page
    can be opened directly. Other public requests without the secret are redirected
    to the main site (HTML page loads) or get a 403 (everything else).
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Read once at start-up; changing it needs a restart.
        self.secret = os.environ.get("YABAI_ORIGIN_SECRET", "")

    def __call__(self, request):
        if self.secret:
            sent = request.META.get("HTTP_X_ORIGIN_SECRET", "")
            own_page = request.method in ("GET", "HEAD") and _is_own_host_page(request)
            if not (
                hmac.compare_digest(sent, self.secret)
                or _is_private(client_ip(request))
                or own_page
            ):
                accept = request.META.get("HTTP_ACCEPT", "")
                if request.method in ("GET", "HEAD") and "text/html" in accept:
                    return HttpResponseRedirect(
                        "https://yabaisearch.org" + request.get_full_path()
                    )
                return HttpResponse("Forbidden", status=403, content_type="text/plain")
        return self.get_response(request)


def _query_only(on: bool) -> None:
    """Switch this thread's SQLite connection between read-only and read-write."""
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute(f"PRAGMA query_only = {'ON' if on else 'OFF'}")


class WriterGateMiddleware:
    """Only the global writer may change the accounts database.

    Active only when YABAI_REPL_NODE (this host's name) is set. The Worker sends every
    account write to the writer it chose and adds X-Yabai-Writer and
    X-Yabai-Writer-Epoch (see accounts/writer.py). A write that names another host or
    an old epoch gets 421 ("misdirected"), and the Worker asks again.

    Every other request runs with SQLite's query_only switched ON, so a stray write
    (anything this code forgot about) fails loudly here instead of silently making
    this copy diverge from the writer's. List this AFTER OriginSecretMiddleware: the
    headers are only trusted once the request is known to come from the Worker.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.node = os.environ.get("YABAI_REPL_NODE", "").strip()
        db = settings.DATABASES["default"]
        self.epoch_file = Path(str(db.get("NAME", "."))).parent / "writer-epoch"
        # The admin path can be moved (DJANGO_ADMIN_PATH); it is a write path too.
        self.prefixes = writer.WRITE_PREFIXES + ("/" + settings.ADMIN_URL_PATH.lstrip("/"),)

    def __call__(self, request):
        if not self.node:
            return self.get_response(request)
        allowed = False
        if writer.is_write(request.method, request.path, self.prefixes):
            ok, why = writer.check_write(
                self.epoch_file,
                self.node,
                request.META.get("HTTP_X_YABAI_WRITER", ""),
                request.META.get("HTTP_X_YABAI_WRITER_EPOCH", ""),
            )
            if not ok:
                response = HttpResponse(f"Not the writer: {why}", status=421, content_type="text/plain")
                response["Cache-Control"] = "no-store"
                return response
            allowed = True
        _query_only(not allowed)
        try:
            return self.get_response(request)
        finally:
            # Back to the default. Each request sets its own mode first, so a request
            # can never inherit write access from the one before it.
            _query_only(False)


_inflight = 0
_inflight_lock = threading.Lock()
# Optional load shedder: when this many searches are already running, answer new
# ones with 503 + Retry-After instead of queueing them. Off by default (0).
_ADMISSION_LIMIT = int(os.environ.get("YABAI_ADMISSION", "0") or 0)


def _served_by() -> str:
    return os.environ.get("YABAI_SERVED_BY", "unknown")


class RequestStampMiddleware:
    """Record arrival time and in-flight count; add the X-Yabai-* headers.

    With YABAI_TRACE=1 it also starts a perf trace per request (yabai_search/debug/perf.py). (The old
    event-loop lag sampler is gone: there is no event loop under gunicorn.)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        global _inflight
        request.yabai_arrived = time.perf_counter()
        client_sent = request.META.get("HTTP_X_CLIENT_SENT")
        lag_ms = None
        if client_sent:
            try:
                lag_ms = max(0, round(time.time() * 1000 - float(client_sent)))
            except ValueError:
                pass
        trace = None
        debug_id = None
        if pd.ENABLED:
            debug_id = request.META.get("HTTP_CF_RAY") or uuid.uuid4().hex[:12]
            trace = pd.start(debug_id, request.META.get("QUERY_STRING", ""))
            if client_sent and trace is not None:
                try:
                    trace.client_sent_ms = float(client_sent)
                except ValueError:
                    pass
            pd.mark("arrived")
        if (
            _ADMISSION_LIMIT > 0
            and request.path == "/api/search"
            and _inflight >= _ADMISSION_LIMIT
        ):
            headers = {"Retry-After": "1"}
            if lag_ms is not None:
                headers["X-Yabai-Lag"] = str(lag_ms)
            response = JsonResponse({"detail": "busy", "retry_after": 1}, status=503)
            for name, value in headers.items():
                response[name] = value
            return response
        with _inflight_lock:
            _inflight += 1
            seen = _inflight
        try:
            response = self.get_response(request)
        except BaseException:
            if trace is not None:
                pd.finish(
                    trace,
                    {"path": request.path, "status": 500, "error": "unhandled"},
                )
            raise
        finally:
            with _inflight_lock:
                _inflight -= 1
        response["X-Yabai-Inflight"] = str(seen)
        response["X-Served-By"] = _served_by()
        if lag_ms is not None:
            response["X-Yabai-Lag"] = str(lag_ms)
        if trace is not None:
            timing = pd.server_timing(trace)
            if timing:
                old = response.get("Server-Timing", "")
                response["Server-Timing"] = ", ".join(v for v in (old, timing) if v)
            if debug_id:
                response["X-Debug-ID"] = debug_id
            pd.finish(
                trace,
                {
                    "path": request.path,
                    "status": response.status_code,
                    "inflight_seen": seen,
                },
            )
        return response


class CacheHeadersMiddleware:
    """Cache rules from the old FastAPI app (static assets reload only on change).

    HTML pages are never cached, because the theme must apply before paint.
    Versioned static assets are immutable: asset_version() changes every ?v=
    URL on any edit. This sits outside WhiteNoise so it also covers static files.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        content_type = response.get("Content-Type", "")
        if content_type.startswith("text/html"):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate"
        elif (
            path == "/api/search"
            and response.status_code == 200
            and "dv" in request.GET
        ):
            # Results are immutable for a given data version + query + page.
            response["Cache-Control"] = (
                "public, max-age=86400, stale-while-revalidate=604800"
            )
        elif path == "/sw.js":
            response["Cache-Control"] = "no-cache"
        elif path.startswith("/static/") and response.status_code in (200, 304):
            if "v" in request.GET or path.startswith("/static/ids_images/"):
                response["Cache-Control"] = "public, max-age=31536000, immutable"
            elif path.startswith("/static/vendor/"):
                response["Cache-Control"] = "public, max-age=604800"
            else:
                response["Cache-Control"] = "no-cache"
        elif path.startswith("/moe-glyphs/") and response.status_code == 200:
            response["Cache-Control"] = "public, max-age=604800"
        return response
