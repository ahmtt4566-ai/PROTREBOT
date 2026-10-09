import socket
import unittest

from guarded_process import audit, run_isolated_if_needed


class GuardedProcessTests(unittest.TestCase):
    def test_arbitrary_connections_and_dns_remain_blocked(self):
        for host in ("127.0.0.1", "::1", "203.0.113.1"):
            with self.subTest(host=host), self.assertRaisesRegex(RuntimeError, "NETWORK_DISABLED"):
                audit("socket.connect", (None, (host, 443)))
        with self.assertRaisesRegex(RuntimeError, "NETWORK_DISABLED"):
            audit("socket.getaddrinfo", ("localhost", 443))

    def test_protected_paths_remain_blocked_without_opening_them(self):
        for path in ("donchian-test-data", "test-sealed.7z", "independent-block",
                     "donchian-holdout-results", "protrebot-research", "sealed", "registry"):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "FORBIDDEN_RESEARCH_PATH"):
                audit("open", (path, "r", 0))

    def test_native_socketpair_works_under_the_installed_guard(self):
        if run_isolated_if_needed(self):
            return
        first, second = socket.socketpair()
        with first, second:
            first.sendall(b"native-self-pipe")
            self.assertEqual(second.recv(32), b"native-self-pipe")
