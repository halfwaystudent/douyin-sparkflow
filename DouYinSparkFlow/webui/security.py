"""Request boundaries shared by the console and login desktop proxy."""

from urllib.parse import urlsplit
import time
from anyio import fail_after

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse


MAX_REQUEST_BYTES = 1024 * 1024


def same_origin(scope, public_scheme=None):
    headers = Headers(scope=scope)
    origin = headers.get("origin")
    if not origin:
        return headers.get("sec-fetch-site") != "cross-site"
    try:
        parsed = urlsplit(origin)
        expected_scheme = public_scheme or ("https" if scope["scheme"] in {"https", "wss"} else "http")
        expected = urlsplit(f"{expected_scheme}://{headers.get('host', '')}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        expected_port = expected.port or (443 if expected_scheme == "https" else 80)
        return (
            parsed.scheme == expected_scheme
            and parsed.hostname == expected.hostname
            and port == expected_port
            and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False


class SecurityMiddleware:
    def __init__(self, app, *, shared=False, max_request_bytes=MAX_REQUEST_BYTES, public_scheme=None, body_timeout=30):
        self.app = app
        self.shared = shared
        self.max_request_bytes = max_request_bytes
        self.public_scheme = public_scheme
        self.body_timeout = body_timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if scope["type"] == "websocket":
            if not same_origin(scope, self.public_scheme) or (self.shared and not headers.get("origin")):
                await send({"type": "websocket.close", "code": 4403})
                return
            return await self.app(scope, receive, send)

        async def secured_send(message):
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["X-Frame-Options"] = "SAMEORIGIN"
                response_headers["Content-Security-Policy"] = "frame-ancestors 'self'; base-uri 'self'; object-src 'none'"
                response_headers["Referrer-Policy"] = "same-origin"
                response_headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                if not scope["path"].startswith("/static/") and scope["path"] != "/favicon.ico":
                    if "no-store" not in response_headers.get("Cache-Control", "").lower():
                        response_headers["Cache-Control"] = "no-store"
            await send(message)

        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            if not same_origin(scope, self.public_scheme):
                return await PlainTextResponse("Cross-origin request denied", status_code=403)(scope, receive, secured_send)
            try:
                length = int(headers.get("content-length", "0"))
                if length < 0:
                    raise ValueError
            except ValueError:
                return await PlainTextResponse("Invalid Content-Length", status_code=400)(scope, receive, secured_send)
            if length > self.max_request_bytes:
                return await PlainTextResponse("Request body too large", status_code=413)(scope, receive, secured_send)
            # Bound chunked requests too, before the multipart/form parser runs.
            body = bytearray()
            deadline = time.monotonic() + self.body_timeout
            while True:
                try:
                    with fail_after(max(0, deadline - time.monotonic())):
                        message = await receive()
                except TimeoutError:
                    return await PlainTextResponse("Request body timed out", status_code=408)(scope, receive, secured_send)
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.max_request_bytes:
                    return await PlainTextResponse("Request body too large", status_code=413)(scope, receive, secured_send)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            return await self.app(scope, buffered_receive, secured_send)
        return await self.app(scope, receive, secured_send)
