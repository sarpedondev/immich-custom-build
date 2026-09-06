import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("build", Path(__file__).resolve().parents[1] / "scripts/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def release(tag, prerelease=False, draft=False):
    return dict(tag_name=tag, prerelease=prerelease, draft=draft)


class ReleaseTests(unittest.TestCase):
    def test_never_downgrade_rc_to_older_stable(self):
        self.assertEqual(build.select_release([release("v3.1.0")], "v3.2.0-rc.1", False), "v3.2.0-rc.1")

    def test_stable_supersedes_rc(self):
        self.assertEqual(build.select_release([release("v3.2.0")], "v3.2.0-rc.1", False), "v3.2.0")

    def test_prereleases_opt_in(self):
        releases = [release("v3.3.0-rc.1", True), release("v3.2.0")]
        self.assertEqual(build.select_release(releases, "v3.1.0", False), "v3.2.0")
        self.assertEqual(build.select_release(releases, "v3.1.0", True), "v3.3.0-rc.1")

    def test_numeric_order_and_ignore_drafts(self):
        releases = [release("v3.9.0"), release("v3.10.0"), release("v4.0.0", draft=True), release("nightly")]
        self.assertEqual(build.select_release(releases, "v3.8.0", True), "v3.10.0")
        self.assertGreater(build.version_key("v3.2.0-rc.10"), build.version_key("v3.2.0-rc.2"))


class GitTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.lines = [f"line {i}\n" for i in range(40)]
        (self.upstream / "file.txt").write_text("".join(self.lines))
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        patched = self.lines.copy()
        patched[10] = "custom patch\n"
        (self.upstream / "file.txt").write_text("".join(patched))
        (self.root / "custom.patch").write_text(self.git("diff", "--binary", "--full-index") + "\n")
        self.git("restore", "file.txt")
        self.config = {"patch_base": self.base, "patches": ["custom.patch"], "upstream_repository": "unused"}
        self.destination = self.root / "source"

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.upstream, stderr=subprocess.DEVNULL, text=True, timeout=20).strip()

    def prepare(self, sha):
        build.prepare_source(self.root, self.config, self.destination, sha, str(self.upstream))

    def test_applies_patch_on_base(self):
        self.prepare(self.base)
        self.assertIn("custom patch", (self.destination / "file.txt").read_text())

    def test_new_release_keeps_both_patch_and_upstream_change(self):
        self.lines[35] = "new upstream feature\n"
        (self.upstream / "file.txt").write_text("".join(self.lines))
        self.git("commit", "-am", "release")
        self.prepare(self.git("rev-parse", "HEAD"))
        text = (self.destination / "file.txt").read_text()
        self.assertIn("custom patch", text)
        self.assertIn("new upstream feature", text)

    def test_conflict_fails_instead_of_silently_dropping_patch(self):
        self.lines[10] = "upstream changed the same code\n"
        (self.upstream / "file.txt").write_text("".join(self.lines))
        self.git("commit", "-am", "conflicting release")
        with self.assertRaises(subprocess.CalledProcessError):
            self.prepare(self.git("rev-parse", "HEAD"))

    def test_existing_source_directory_is_not_overwritten(self):
        self.destination.mkdir()
        with self.assertRaises(ValueError):
            self.prepare(self.base)

    def test_build_identity_tracks_code_but_ignores_success_record(self):
        first = build.recipe_key(self.upstream, self.base)
        (self.upstream / build.STATE).write_text('{}\n')
        self.git("add", build.STATE)
        self.assertEqual(first, build.recipe_key(self.upstream, self.base))
        (self.upstream / "file.txt").write_text("changed recipe\n")
        self.assertNotEqual(first, build.recipe_key(self.upstream, self.base))
        self.assertNotEqual(first, build.recipe_key(self.upstream, "a" * 40))


class StateTests(unittest.TestCase):
    def test_daily_skip_and_failure_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"minimum_version": "v3.2.0-rc.1", "upstream_repository": "test/upstream", "include_prereleases": False, "image": "test/image"}
            state = {"version": "v3.2.0", "upstream_sha": "a" * 40, "build_key": "successful"}
            (root / build.STATE).write_text(json.dumps(state))
            with patch.object(build, "run", side_effect=["[[]]", "a" * 40] * 2), patch.object(build, "recipe_key", side_effect=["successful", "changed"]), patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "RELEASE_CHANNEL": "stable"}):
                self.assertEqual(build.select(root, config)["build"], "false")
                self.assertEqual(build.select(root, config)["build"], "true")

    def test_rejects_moved_upstream_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"minimum_version": "v3.2.0", "upstream_repository": "test/upstream", "include_prereleases": False}
            (root / build.STATE).write_text(json.dumps({"version": "v3.2.0", "upstream_sha": "a" * 40}))
            with patch.object(build, "run", side_effect=["[[]]", "b" * 40]), patch.dict(os.environ, {"RELEASE_CHANNEL": "stable"}):
                with self.assertRaisesRegex(ValueError, "moved"):
                    build.select(root, config)


if __name__ == "__main__":
    unittest.main()
