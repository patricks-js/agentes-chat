import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("updater", Path(__file__).with_name("update.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class UpdateTests(unittest.TestCase):
    def test_failed_start_restores_dump_before_old_image_starts(self):
        previous_cwd = Path.cwd()
        calls = []
        starts = 0

        def compose(*args, **kwargs):
            nonlocal starts
            calls.append(args)
            if "pg_dump" in args:
                kwargs["stdout"].write(b"dump")
            if args[0] == "up":
                starts += 1
                if starts == 1:
                    raise RuntimeError("unhealthy")

        def output(*args):
            if args[:2] == ("docker", "inspect"):
                return json.dumps([{"Image": "sha256:old", "State": {"Health": {"Status": "healthy"}},
                    "Mounts": [{"Destination": "/data/files", "Type": "volume", "Name": "files"}]}])
            if args[:2] == ("docker", "exec"):
                return json.dumps({"version": "0.1.0"})
            return "sha256:new" if "image" in args else "container"

        try:
            with tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                (directory / ".env").write_text("SECRET=keep\nAGENTES_IMAGE=old\n")
                with patch.object(updater, "compose", side_effect=compose), patch.object(updater, "output", side_effect=output), patch.object(updater, "run"):
                    with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                        updater.update(directory, "v0.2.0", "image")
                self.assertEqual(starts, 2)
                self.assertIn("AGENTES_IMAGE=sha256:old", (directory / ".env").read_text())
                restore = next(i for i, call in enumerate(calls) if "pg_restore" in call)
                self.assertLess(restore, len(calls) - 1)
                self.assertEqual(calls[-1][0], "up")
        finally:
            os.chdir(previous_cwd)

    def test_server_versions_only(self):
        self.assertIsNone(updater.version("desktop-v0.9.0"))
        self.assertIsNone(updater.version("v1.0.0-rc1"))
        self.assertGreater(updater.version("v0.10.0"), updater.version("v0.9.0"))

    def test_release_feed_filters_other_products_and_prereleases(self):
        releases = [
            {"tag_name": tag, "draft": draft, "prerelease": pre}
            for tag, draft, pre in [("desktop-v9.0.0", False, False),
                ("v0.2.0", False, False), ("v0.10.0", False, False),
                ("v1.0.0", False, True), ("v2.0.0", True, False)]
        ]
        with patch.object(updater.urllib.request, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps(releases).encode()
            self.assertEqual(updater.latest("owner/repo"), "v0.10.0")

    def test_image_pin_preserves_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("SECRET=keep\nAGENTES_IMAGE=old\nHOST_PORT=3001\n")
            updater.write_image(path, "sha256:new")
            self.assertEqual(path.read_text(), "SECRET=keep\nAGENTES_IMAGE=sha256:new\nHOST_PORT=3001\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
