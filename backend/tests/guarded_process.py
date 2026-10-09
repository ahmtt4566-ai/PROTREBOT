"""Run native spawn tests without re-importing an unguarded pytest launcher."""

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def audit(event, args):
    if event in {"open", "os.listdir", "os.scandir", "os.remove", "os.rename", "os.mkdir", "os.rmdir"}:
        for value in args[:2]:
            if not isinstance(value, (str, bytes, os.PathLike)):
                continue
            text = os.fsdecode(value).lower().replace("\\", "/")
            if any(token in text for token in (
                "donchian-test-", "test-sealed.7z", "independent-block",
                "donchian-holdout-results", "protrebot-research",
            )) or any(
                ("sealed" in part or "registry" in part or "registries" in part)
                and not part.endswith((".py", ".pyc", ".pyd"))
                for part in text.split("/")
            ):
                raise RuntimeError("FORBIDDEN_RESEARCH_PATH_ACCESS")
    if event == "socket.connect":
        # Existing offline guards wrap connect; only the actual stdlib self-pipe is permitted.
        frame = sys._getframe(1)
        while frame is not None:
            if (
                frame.f_code.co_filename == socket.__file__
                and frame.f_code.co_name == "_fallback_socketpair"
                and args[1][0] in ("127.0.0.1", "::1")
            ):
                return
            frame = frame.f_back
        raise RuntimeError("NETWORK_DISABLED_FOR_SYNTHETIC_SUITE")
    if event == "socket.getaddrinfo":
        raise RuntimeError("NETWORK_DISABLED_FOR_SYNTHETIC_SUITE")


def run_isolated_if_needed(case):
    specification = getattr(sys.modules["__main__"], "__spec__", None)
    if specification is not None and specification.name == "guarded_process":
        return False
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(root / "backend" / "tests"), str(root / "backend"))),
            "PROTREBOT_DATA_DIR": temporary,
            "DATA_DIR": temporary,
            "DATABASE_URL": "",
            "PROTREBOT_DURABLE_AUTH_REQUIRED": "0",
            "ASSISTANT_LIVE_TESTS": "0",
        }
        result = subprocess.run(
            [sys.executable, "-B", "-m", "guarded_process", case.id()],
            cwd=root, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False,
        )
    case.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return True


if __name__ in {"__main__", "__mp_main__"}:
    sys.addaudithook(audit)

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])
    if suite.countTestCases() != 1:
        raise RuntimeError("EXACTLY_ONE_NATIVE_TEST_REQUIRED")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
