"""ApiKey не должен уходить по HTTP-redirect на другой адрес.

Проверка идёт на двух локальных HTTP-серверах: первый отвечает redirect,
второй — «внешний получатель», который не должен получить ни запроса, ни ключа.
Политика клиента — не выполнять redirect вообще, поэтому запрещены и
междоменные переходы, и смена схемы на `http://`.
"""

from __future__ import annotations

import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_renamer.core.stashdb_client import (
    StashDBClient,
    StashDBResponseError,
    _NoRedirectHandler,
)

TOKEN = "test-token-not-real"


def _handler(received: list[tuple[str, dict]], *, status: int = 200, location: str = ""):
    body = b'{"data": {"ok": true}}'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            received.append(
                (self.path, {key.lower(): value for key, value in self.headers.items()})
            )
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # тестовый шум в stderr не нужен
            pass

    return Handler


@contextmanager
def _server(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/graphql"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _client(endpoint: str) -> StashDBClient:
    return StashDBClient(
        TOKEN,
        endpoint=endpoint,
        delay=0.0,
        timeout=5.0,
        max_attempts=1,
        sleep=lambda seconds: None,
    )


def test_baseline_request_carries_the_key():
    received: list[tuple[str, dict]] = []
    with _server(_handler(received)) as endpoint:
        data = _client(endpoint).execute("{ ok }")

    assert data == {"ok": True}
    assert received[0][1]["apikey"] == TOKEN


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_not_followed_and_key_is_not_leaked(status):
    external: list[tuple[str, dict]] = []
    origin: list[tuple[str, dict]] = []
    with (
        _server(_handler(external)) as external_endpoint,
        _server(_handler(origin, status=status, location=external_endpoint)) as endpoint,
        pytest.raises(StashDBResponseError) as excinfo,
    ):
        _client(endpoint).execute("{ ok }")

    assert external == []
    assert len(origin) == 1
    assert TOKEN not in str(excinfo.value)
    assert str(status) in str(excinfo.value)


def test_same_origin_redirect_is_not_followed_either():
    origin: list[tuple[str, dict]] = []
    with (
        _server(_handler(origin, status=307, location="/moved-graphql")) as endpoint,
        pytest.raises(StashDBResponseError),
    ):
        _client(endpoint).execute("{ ok }")

    assert [path for path, _headers in origin] == ["/graphql"]


def test_redirect_error_is_not_retried():
    calls: list[tuple[str, dict]] = []
    with _server(_handler(calls, status=302, location="http://127.0.0.1:1/graphql")) as endpoint:
        client = StashDBClient(
            TOKEN, endpoint=endpoint, delay=0.0, timeout=5.0, max_attempts=3, sleep=lambda _: None
        )
        with pytest.raises(StashDBResponseError):
            client.execute("{ ok }")

    assert len(calls) == 1


def test_redirect_handler_refuses_downgrade_and_other_hosts():
    handler = _NoRedirectHandler()
    request = urllib.request.Request("https://stashdb.org/graphql", data=b"{}", method="POST")

    for target in (
        "http://stashdb.org/graphql",
        "https://evil.example/graphql",
        "https://stashdb.org:8443/graphql",
        "https://stashdb.org/graphql",
    ):
        assert handler.redirect_request(request, None, 302, "Found", {}, target) is None
