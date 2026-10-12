import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import server_v2


def _fake_dns(ip):
    return lambda host, port, **kw: [(2, 1, 6, "", (ip, port or 443))]


class ValidateBaseUrlTests(unittest.TestCase):
    def test_accepts_public_https_host(self):
        with mock.patch.object(server_v2.socket, "getaddrinfo", _fake_dns("104.18.0.1")):
            self.assertEqual(server_v2.validate_base_url("https://api.anthropic.com/v1"), "https://api.anthropic.com/v1")

    def test_rejects_non_https(self):
        for bad in ["http://127.0.0.1:8080/v1", "file:///etc/hosts", "ftp://example.com", "https://", "", "not a url"]:
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    server_v2.validate_base_url(bad)

    def test_rejects_private_and_internal_addresses(self):
        for bad in ["https://127.0.0.1/v1", "https://10.0.0.5/v1", "https://192.168.1.1/v1",
                    "https://169.254.169.254/latest", "https://172.16.4.2/v1", "https://[::1]/v1"]:
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    server_v2.validate_base_url(bad)

    def test_rejects_hostname_that_resolves_to_private_address(self):
        with mock.patch.object(server_v2.socket, "getaddrinfo", _fake_dns("10.1.2.3")):
            with self.assertRaises(ValueError):
                server_v2.validate_base_url("https://looks-public.example/v1")

    def test_operator_allowlist_permits_listed_private_host(self):
        original = server_v2.ALLOWED_LLM_HOSTS
        server_v2.ALLOWED_LLM_HOSTS = {"10.0.0.5"}
        try:
            self.assertEqual(server_v2.validate_base_url("https://10.0.0.5/v1"), "https://10.0.0.5/v1")
        finally:
            server_v2.ALLOWED_LLM_HOSTS = original

    def test_unresolvable_host_is_refused(self):
        with mock.patch.object(server_v2.socket, "getaddrinfo", side_effect=socket.gaierror("no such host")):
            with self.assertRaises(ValueError):
                server_v2.validate_base_url("https://does-not-exist.invalid/v1")


class _Recorder(BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        _Recorder.hits.append((self.path, self.headers.get("x-api-key")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
            return
        self.send_response(500)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"internal detail that must not reach the browser")


class RequestJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Recorder.hits = []

    def test_redirect_is_not_followed_and_key_not_resent(self):
        with self.assertRaises(RuntimeError) as ctx:
            server_v2.request_json(self.base + "/redirect", {}, {"x-api-key": "secret-key"})
        self.assertIn("HTTP 302", str(ctx.exception))
        paths = [p for p, _ in _Recorder.hits]
        self.assertEqual(paths, ["/redirect"])

    def test_upstream_body_is_not_returned_to_caller(self):
        with self.assertRaises(RuntimeError) as ctx:
            server_v2.request_json(self.base + "/fail", {}, {})
        self.assertEqual(str(ctx.exception), "LLM API returned HTTP 500")
        self.assertNotIn("internal detail", str(ctx.exception))

    def test_connection_error_is_generic(self):
        with self.assertRaises(RuntimeError) as ctx:
            server_v2.request_json("http://127.0.0.1:1/x", {}, {})
        self.assertEqual(str(ctx.exception), "LLM connection failed")


class AnalyseRejectsHttpTests(unittest.TestCase):
    def test_stored_http_url_is_refused_before_any_request(self):
        if server_v2.Fernet is None:
            self.skipTest("the cryptography package is required to build encrypted settings")
        original_key = server_v2.MASTER_KEY
        server_v2.MASTER_KEY = "test-master-key"
        try:
            settings = {
                "api_key_enc": server_v2.encrypt_secret("k"),
                "provider": "anthropic",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "m",
                "auth_mode": "x-api-key",
            }
            with self.assertRaises(ValueError):
                server_v2.analyse_with_settings(settings, {})
        finally:
            server_v2.MASTER_KEY = original_key


if __name__ == "__main__":
    unittest.main()
