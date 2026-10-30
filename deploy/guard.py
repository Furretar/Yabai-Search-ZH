"""Optional shared-secret gate in front of the app.

Run the laptop and tablet copies as `deploy.guard:app` instead of
`yabai_search.main:app`. When YABAI_ORIGIN_SECRET is set, any request that does
not carry a matching X-Origin-Secret header gets a 403. The Cloudflare Worker
adds that header, so visitors who go straight to laptop. or tablet. (skipping
the Worker) are turned away. With the variable unset this changes nothing.

It wraps the app from outside, so yabai_search/main.py stays untouched.
"""

import hmac
import os

from yabai_search.main import app as _app

# Read once at start-up; changing it needs a restart.
_SECRET = os.environ.get("YABAI_ORIGIN_SECRET", "")


async def app(scope, receive, send):
    """ASGI entry point: check the secret, then hand over to the real app."""
    # Only HTTP requests are gated; "lifespan" (start-up/shutdown) must pass.
    if _SECRET and scope["type"] == "http":
        sent = dict(scope["headers"]).get(b"x-origin-secret", b"").decode("latin-1")
        # compare_digest takes the same time however many characters match.
        if not hmac.compare_digest(sent, _SECRET):
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
