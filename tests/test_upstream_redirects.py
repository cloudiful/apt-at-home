"""The discovery client speaks HTTPS only, follows no other origin, and
follows at most three hops.

The policy is exercised with real `urllib` requests: an injected HTTPS
transport serves the chain and records every request with its headers, and a
real local HTTP server proves that a plaintext target is never contacted. The
evidence is the request that was (or was not) sent, not a list of URLs a fake
client was asked for.
"""

from __future__ import annotations

import io
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.response import addinfourl

from scripts.upstream import (
    MAX_REDIRECTS,
    HttpError,
    SameOriginRedirectHandler,
    UrllibClient,
)

GITHUB_API = "https://api.github.com/repos/x/y/releases/latest"
CODEBERG_API = "https://codeberg.org/api/v1/repos/x/y/releases/latest"


class _Transport(urllib.request.HTTPSHandler):
    """A real urllib HTTPS transport that answers from a fixed route table.

    Every request it serves is recorded with its headers, so a test can assert
    which origins were contacted, how many requests a chain sent, and which
    one carried a token.
    """

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        super().__init__()
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []

    @property
    def hits(self) -> list[str]:
        return [url for url, _ in self.requests]

    def header_for(self, url: str, name: str) -> str | None:
        """Return the header one recorded request carried, if any."""
        for requested, headers in self.requests:
            if requested != url:
                continue
            for key, value in headers.items():
                if key.lower() == name.lower():
                    return value
        return None

    def https_open(self, req):  # type: ignore[no-untyped-def]
        self.requests.append((req.full_url, dict(req.headers)))
        status, extra, body = self.routes.get(req.full_url, (404, {}, b"missing"))
        message = Message()
        for key, value in extra.items():
            message[key] = value
        response = addinfourl(io.BytesIO(body), message, req.full_url, status)
        response.msg = "Injected"
        return response


class _Server:
    """A local HTTP server that answers fixed routes and records every hit."""

    def __init__(self, routes: dict[str, tuple[int, str, bytes]]) -> None:
        self.hits: list[str] = []
        routes_ = routes
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib spells it this way
                hits.append(self.path)
                status, location, body = routes_.get(self.path, (404, "", b"missing"))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _client(transport: _Transport, *, token: str | None = None) -> UrllibClient:
    """A client whose opener serves the injected transport and the real policy."""
    client = UrllibClient(token=token)
    client._opener = urllib.request.build_opener(  # noqa: SLF001 - the transport is injected
        transport, SameOriginRedirectHandler()
    )
    return client


def _chain(redirects: int, prefix: str = "") -> dict[str, tuple[int, dict[str, str], bytes]]:
    """A route table of `redirects` redirects that ends in a 200 answer.

    `prefix` names a second chain beside the default one, so one transport can
    serve both.
    """
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {
        f"https://codeberg.org/{prefix}start": (
            302, {"Location": f"https://codeberg.org/{prefix}hop0"}, b""
        )
    }
    for index in range(redirects - 1):
        routes[f"https://codeberg.org/{prefix}hop{index}"] = (
            302, {"Location": f"https://codeberg.org/{prefix}hop{index + 1}"}, b""
        )
    routes[f"https://codeberg.org/{prefix}hop{redirects - 1}"] = (200, {}, b"done")
    return routes


class HttpsOnlyRequestTest(unittest.TestCase):
    """A plaintext target is refused before the transport is touched."""

    def test_a_plaintext_target_is_never_contacted(self) -> None:
        server = _Server({"/plain": (200, "", b"accepted plaintext")})
        self.addCleanup(server.close)

        client = UrllibClient(timeout=5)
        with self.assertRaises(HttpError) as ctx:
            client.get_bytes(server.url("/plain"), max_bytes=100)
        self.assertIn("non-HTTPS", str(ctx.exception))
        self.assertEqual(server.hits, [])

    def test_every_other_scheme_is_refused(self) -> None:
        client = UrllibClient(timeout=5)
        for url in (
            "http://api.github.com/repos/x/y/releases/latest",
            "http://codeberg.org/api/v1/repos/x/y/releases/latest",
            "ftp://codeberg.org/api/v1/repos/x/y/releases/latest",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                with self.assertRaises(HttpError):
                    client.get_bytes(url, max_bytes=100)

    def test_a_refused_scheme_sends_no_request(self) -> None:
        transport = _Transport({GITHUB_API: (200, {}, b"{}")})
        client = _client(transport)
        with self.assertRaises(HttpError):
            client.get_json("http://api.github.com/repos/x/y/releases/latest")
        self.assertEqual(transport.requests, [])


class RedirectBudgetTest(unittest.TestCase):
    """A chain follows at most three hops, whatever the destinations are."""

    def test_three_hops_reach_the_endpoint(self) -> None:
        transport = _Transport(_chain(3))
        client = _client(transport)

        body = client.get_bytes("https://codeberg.org/start", max_bytes=100)

        self.assertEqual(body, b"done")
        self.assertEqual(
            transport.hits,
            [
                "https://codeberg.org/start",
                "https://codeberg.org/hop0",
                "https://codeberg.org/hop1",
                "https://codeberg.org/hop2",
            ],
        )

    def test_a_fourth_hop_is_refused_before_it_is_sent(self) -> None:
        transport = _Transport(_chain(4))
        client = _client(transport)

        with self.assertRaises(HttpError) as ctx:
            client.get_bytes("https://codeberg.org/start", max_bytes=100)

        self.assertIn("redirect budget", str(ctx.exception))
        # The initial request and the three followed hops, and nothing more.
        self.assertEqual(len(transport.requests), MAX_REDIRECTS + 1)
        self.assertNotIn("https://codeberg.org/hop3", transport.hits)

    def test_a_repeated_destination_is_capped(self) -> None:
        transport = _Transport({
            "https://codeberg.org/start": (
                302, {"Location": "https://codeberg.org/loop"}, b""
            ),
            "https://codeberg.org/loop": (
                302, {"Location": "https://codeberg.org/loop"}, b""
            ),
        })
        client = _client(transport)

        with self.assertRaises(HttpError) as ctx:
            client.get_bytes("https://codeberg.org/start", max_bytes=100)

        self.assertIn("redirect budget", str(ctx.exception))
        self.assertEqual(len(transport.requests), MAX_REDIRECTS + 1)
        self.assertEqual(transport.hits.count("https://codeberg.org/loop"), MAX_REDIRECTS)

    def test_an_independent_request_starts_with_a_fresh_budget(self) -> None:
        routes = _chain(4)
        routes.update(_chain(3, prefix="short/"))
        transport = _Transport(routes)
        client = _client(transport)

        with self.assertRaises(HttpError):
            client.get_bytes("https://codeberg.org/start", max_bytes=100)
        # The next request is a chain of its own with a full allowance.
        body = client.get_bytes("https://codeberg.org/short/start", max_bytes=100)

        self.assertEqual(body, b"done")
        self.assertEqual(len(transport.requests), 2 * (MAX_REDIRECTS + 1))

    def test_concurrent_chains_keep_their_own_budget(self) -> None:
        routes = _chain(3, prefix="a/")
        routes.update(_chain(3, prefix="b/"))
        transport = _Transport(routes)
        client = _client(transport)
        bodies: list[bytes] = []
        failures: list[Exception] = []

        def run(path: str) -> None:
            try:
                bodies.append(
                    client.get_bytes(f"https://codeberg.org/{path}", max_bytes=100)
                )
            except Exception as exc:  # noqa: BLE001 - the assertion reports it
                failures.append(exc)

        threads = [
            threading.Thread(target=run, args=("a/start",)),
            threading.Thread(target=run, args=("b/start",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(failures, [])
        self.assertEqual(bodies, [b"done", b"done"])

    def test_a_downgrade_and_a_foreign_origin_are_never_contacted(self) -> None:
        transport = _Transport({
            "https://codeberg.org/plain": (302, {"Location": "http://codeberg.org/plain"}, b""),
            "https://codeberg.org/foreign": (
                302, {"Location": "https://evil.example/take"}, b""
            ),
        })
        client = _client(transport)

        with self.assertRaises(HttpError) as ctx:
            client.get_bytes("https://codeberg.org/plain", max_bytes=100)
        self.assertIn("non-HTTPS", str(ctx.exception))
        with self.assertRaises(HttpError) as ctx:
            client.get_bytes("https://codeberg.org/foreign", max_bytes=100)
        self.assertIn("cross-origin", str(ctx.exception))

        # Neither the plaintext target nor the foreign origin was contacted.
        self.assertEqual(
            transport.hits,
            ["https://codeberg.org/plain", "https://codeberg.org/foreign"],
        )

    def test_the_token_reaches_only_the_github_api(self) -> None:
        transport = _Transport({
            GITHUB_API: (200, {}, b"{}"),
            CODEBERG_API: (200, {}, b"{}"),
        })
        client = _client(transport, token="secret-value")

        client.get_json(GITHUB_API)
        client.get_json(CODEBERG_API)

        self.assertEqual(
            transport.header_for(GITHUB_API, "Authorization"), "Bearer secret-value"
        )
        self.assertIsNone(transport.header_for(CODEBERG_API, "Authorization"))


class RedirectPolicyTest(unittest.TestCase):
    """The handler refuses a redirect that leaves the origin it was given."""

    def _request(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(url, headers={"Authorization": "Bearer secret"})

    def test_a_same_origin_redirect_is_followed(self) -> None:
        handler = SameOriginRedirectHandler()
        request = self._request(GITHUB_API)
        new = handler.redirect_request(
            request, None, 302, "Found", {}, "https://api.github.com/repos/x/y/releases/2"
        )
        self.assertIsNotNone(new)
        assert new is not None
        self.assertEqual(new.full_url, "https://api.github.com/repos/x/y/releases/2")
        # The token stays with the origin it was scoped to.
        self.assertEqual(new.get_header("Authorization"), "Bearer secret")

    def test_a_redirect_to_another_origin_is_refused(self) -> None:
        handler = SameOriginRedirectHandler()
        request = self._request(GITHUB_API)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(
                request, None, 302, "Found", {}, CODEBERG_API
            )
        ctx.exception.close()

    def test_a_redirect_that_downgrades_to_http_is_refused(self) -> None:
        handler = SameOriginRedirectHandler()
        request = self._request(GITHUB_API)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(
                request, None, 302, "Found", {},
                "http://api.github.com/repos/x/y/releases/latest",
            )
        ctx.exception.close()


if __name__ == "__main__":
    unittest.main()
