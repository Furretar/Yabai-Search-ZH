"""Optional shared-secret gate in front of the app.

Run the laptop and tablet copies as `deploy.guard:app` instead of
`yabai_search.main:app`. When YABAI_ORIGIN_SECRET is set, requests that do not
carry a matching X-Origin-Secret header are turned away. The Cloudflare Worker
always adds that header.

Requests that arrive directly from a private address (the LAN or Tailscale)
are let through without the secret, so you can open laptop. and tablet. in a
browser for debugging. Public visitors who skip the Worker are redirected to
the main site (HTML) or get a 403 (everything else). With the variable unset
this changes nothing.

It wraps the app from outside, so yabai_search/main.py stays untouched.
"""

import hmac
import ipaddress
import os

from yabai_search.main import app as _app

# Read once at start-up; changing it needs a restart.
_SECRET = os.environ.get("YABAI_ORIGIN_SECRET", "")

# Private ranges that may reach the origin directly: home LAN and Tailscale.
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


def _is_private_client(scope) -> bool:
    """Return True when the TCP peer is on the LAN or Tailscale."""
    client = scope.get("client")
    if not client:
        return False
    try:
        addr = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETS)


async def app(scope, receive, send):
    """ASGI entry point: check the secret, then hand over to the real app."""
    # Only HTTP requests are gated; "lifespan" (start-up/shutdown) must pass.
    if _SECRET and scope["type"] == "http":
        sent = dict(scope["headers"]).get(b"x-origin-secret", b"").decode("latin-1")
        # compare_digest takes the same time however many characters match.
        if not hmac.compare_digest(sent, _SECRET) and not _is_private_client(scope):
            # A public visitor who skipped the Worker should land on the main
            # site, not a bare 403.
            headers = dict(scope["headers"])
            accept = headers.get(b"accept", b"").decode("latin-1")
            if scope["method"] in ("GET", "HEAD") and "text/html" in accept:
                path = scope.get("path", "/")
                query = scope.get("query_string", b"").decode("latin-1")
                location = "https://yabaisearch.org" + path
                if query:
                    location += "?" + query
                await send(
                    {
                        "type": "http.response.start",
                        "status": 302,
                        "headers": [(b"location", location.encode("latin-1"))],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"Forbidden"})
            return
    await _app(scope, receive, send)
