import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import _hermetic_environment


_ROOT = Path(__file__).resolve().parent.parent


def _repo_log_stat():
    path = _ROOT / "filesharingbot.log"
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


class HermeticLoggingTests(unittest.TestCase):
    """Prove the test bootstrap redirects production logging away from the repo."""

    BASE_ENV = {
        "TG_BOT_TOKEN": "123:TESTTOKEN",
        "APP_ID": "123456",
        "API_HASH": "0" * 32,
        "CHANNEL_ID": "-100123",
        "OWNER_ID": "1",
        "DATABASE_URL": "mongodb://localhost:27017/test_teledrop",
    }

    def test_bootstrap_activated_and_log_path_is_outside_repository(self):
        # The package initializer already ran the bootstrap before this test
        # module was imported, so the shared helper must be active.
        self.assertIsNotNone(_hermetic_environment._TEMP_ROOT)
        log_name = os.environ.get("LOG_FILE_NAME")
        self.assertTrue(log_name, "LOG_FILE_NAME must be redirected by the bootstrap")
        log_path = Path(log_name).resolve()
        self.assertNotEqual(log_path, _ROOT)
        self.assertNotIn(_ROOT, log_path.parents, "test log must not live inside the repository")
        temp_root = Path(tempfile.gettempdir()).resolve()
        self.assertIn(temp_root, log_path.parents, "test log must live under the system temp dir")
        self.assertTrue(log_path.name.endswith(".log"))

    def test_cleanup_target_is_self_created_temp_directory_only(self):
        # The helper's cleanup target must be a temp directory it created, never
        # a repository path.
        temp_root = _hermetic_environment._TEMP_ROOT
        self.assertIsNotNone(temp_root)
        temp_root = Path(temp_root).resolve()
        self.assertTrue(temp_root.name.startswith("teledrop-test-logs-"))
        sys_temp = Path(tempfile.gettempdir()).resolve()
        self.assertIn(sys_temp, temp_root.parents, "cleanup target must be under the system temp dir")
        self.assertNotIn(_ROOT, temp_root.parents, "cleanup must never target a repository directory")

    def test_config_import_in_subprocess_does_not_create_repository_log(self):
        # Strongest-case scenario: the subprocess runs with the repository root as
        # the working directory (exactly what the canonical discovery commands do)
        # and does NOT pre-seed LOG_FILE_NAME. Only the hermetic bootstrap redirects
        # it. Without the bootstrap this would create filesharingbot.log at repo root.
        repository_log_before = _repo_log_stat()
        environment = {
            name: os.environ[name]
            for name in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "PYTHONIOENCODING")
            if name in os.environ
        }
        environment.update(self.BASE_ENV)
        environment["PYTHONPATH"] = str(_ROOT)
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c",
             "import tests._hermetic_environment; import config"],
            cwd=str(_ROOT),
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            repository_log_before, _repo_log_stat(),
            "importing config must not create or modify the repository filesharingbot.log",
        )


class RootConfigRetryValidationTests(unittest.TestCase):
    BASE_ENV = {
        "TG_BOT_TOKEN": "123:TESTTOKEN",
        "APP_ID": "123456",
        "API_HASH": "0" * 32,
        "CHANNEL_ID": "-100123",
        "OWNER_ID": "1",
        "DATABASE_URL": "mongodb://localhost:27017/test_teledrop",
    }

    def load_with(self, **values):
        environment = dict(self.BASE_ENV)
        environment.update(values)
        root = Path(__file__).resolve().parent.parent
        repository_log = root / "filesharingbot.log"
        before = repository_log.stat() if repository_log.exists() else None
        with tempfile.TemporaryDirectory() as cwd:
            environment = {
                name: os.environ[name]
                for name in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "PYTHONIOENCODING")
                if name in os.environ
            }
            environment.update(self.BASE_ENV)
            environment.update(values)
            environment.update({
                "PYTHONPATH": str(root),
                "LOG_FILE_NAME": str(Path(cwd) / "config-test.log"),
            })
            result = subprocess.run(
                [
                    sys.executable, "-W", "error::ResourceWarning", "-c",
                    "import dotenv; dotenv.load_dotenv = lambda *args, **kwargs: None; import config",
                ],
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
            )
        after = repository_log.stat() if repository_log.exists() else None
        self.assertEqual(before, after, "config subprocess changed repository filesharingbot.log")
        return result

    def test_max_below_base_is_rejected_at_startup(self):
        result = self.load_with(CONTROL_PLANE_METRICS_RETRY_BASE_SECONDS="60", CONTROL_PLANE_METRICS_RETRY_MAX_SECONDS="30")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MAX_SECONDS must be >=", result.stderr)

    def test_equal_and_greater_max_are_accepted(self):
        self.assertEqual(self.load_with(CONTROL_PLANE_METRICS_RETRY_BASE_SECONDS="60", CONTROL_PLANE_METRICS_RETRY_MAX_SECONDS="60").returncode, 0)
        self.assertEqual(self.load_with(CONTROL_PLANE_METRICS_RETRY_BASE_SECONDS="60", CONTROL_PLANE_METRICS_RETRY_MAX_SECONDS="61").returncode, 0)

    def test_non_positive_and_non_integer_retry_values_are_rejected(self):
        for name, value in (("CONTROL_PLANE_METRICS_RETRY_BASE_SECONDS", "0"), ("CONTROL_PLANE_METRICS_RETRY_MAX_SECONDS", "0"), ("CONTROL_PLANE_METRICS_RETRY_BASE_SECONDS", "bad")):
            with self.subTest(name=name, value=value):
                self.assertNotEqual(self.load_with(**{name: value}).returncode, 0)


if __name__ == "__main__":
    unittest.main()