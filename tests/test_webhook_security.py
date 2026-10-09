"""Network-free regression tests for outbound webhook SSRF and TLS hostname checks."""
import http.client
import socket
from types import SimpleNamespace

import pytest

from runledger.server import approvals


@pytest.mark.parametrize("destination", ["fec0::1", "ff02::1", "64:ff9b::7f00:1", "2002:7f00:1::1"])
def test_dns_answer_with_internal_or_translated_ipv6_is_rejected(monkeypatch, destination):
    monkeypatch.setattr(approvals.socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "", (destination, 443, 0, 0)),
    ])
    def must_not_connect(*args, **kwargs):
        raise AssertionError("non-public destination reached TCP")
    monkeypatch.setattr(approvals.socket, "create_connection", must_not_connect)
    with pytest.raises(ValueError, match="not a public IP"):
        approvals._post_json("https://hooks.example.com/post", {})


def test_https_pin_preserves_original_sni_and_certificate_name(monkeypatch):
    """Exercise HTTPSConnection.connect, not a mocked HTTPSConnection.request."""
    monkeypatch.setattr(approvals.socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.4.4", 443)),
    ])
    captured = {"addresses": []}

    class FakeSocket:
        def setsockopt(self, *args):
            pass
        def close(self):
            pass

    def create_connection(address, timeout, source_address=None):
        captured["addresses"].append(address)
        return FakeSocket()

    monkeypatch.setattr(approvals.socket, "create_connection", create_connection)

    class FakeSSLContext:
        post_handshake_auth = None
        check_hostname = True
        verify_mode = 2  # ssl.CERT_REQUIRED

        def wrap_socket(self, sock, server_hostname=None):
            captured["hostname"] = server_hostname
            captured["checked"] = self.check_hostname and self.verify_mode == 2
            return sock

    class TestHTTPSConnection(http.client.HTTPSConnection):
        def __init__(self, host, port, timeout):
            super().__init__(host, port, timeout=timeout, context=FakeSSLContext())

        def request(self, method, url, body=None, headers=None):
            captured["request"] = (method, url)
            self.connect()  # runs the real HTTPSConnection TLS/SNI path

        def getresponse(self):
            class Response:
                status = 200
                def read(self, size):
                    return b""
            return Response()

    # Swap only approvals' module binding, not http.client.HTTPSConnection itself:
    # CPython's real HTTPSConnection.connect() uses that class in super() calls.
    monkeypatch.setattr(approvals, "http", SimpleNamespace(client=SimpleNamespace(
        HTTPSConnection=TestHTTPSConnection, HTTPConnection=http.client.HTTPConnection,
    )))
    approvals._post_json("https://hooks.example.com/post", {"kind": "test"})
    assert captured == {
        "addresses": [("8.8.4.4", 443)],
        "hostname": "hooks.example.com",
        "checked": True,
        "request": ("POST", "/post"),
    }
