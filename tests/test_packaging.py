import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from scripts.export_app import DIRECTORIES, FILES, export
from scripts.package_source import source_files
from scripts.preflight import problems, settings_from_file
from scripts.setup import initialize


class SetupTests(unittest.TestCase):
    def test_slack_is_opt_in_and_preserved_when_exporting_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, dest = root / "source", root / "app"
            src.mkdir()
            for name in FILES:
                (src / name).write_text("fixture")
            for name in DIRECTORIES:
                (src / name).mkdir()
            (src / "channels/slack.py").write_text("# fixture channel")
            with contextlib.redirect_stdout(io.StringIO()):
                export(dest, src)
                self.assertFalse((dest / "channels/slack.py").exists())
                export(dest, src, slack=True)
                self.assertTrue((dest / "channels/slack.py").exists())
                export(dest, src)
                self.assertTrue((dest / "channels/slack.py").exists())

    def test_export_removes_retired_source_but_preserves_private_settings_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, dest = root / "source", root / "app"
            src.mkdir()
            for name in FILES:
                (src / name).write_text("fixture")
            for name in DIRECTORIES:
                (src / name).mkdir()
            old = src / "tools/removed.py"
            old.write_text("# fixture")
            with contextlib.redirect_stdout(io.StringIO()):
                export(dest, src)
                (dest / ".env").write_text("FAKE_SETTING=preserve")
                (dest / ".mda").mkdir()
                (dest / ".mda/deployment.json").write_text('{"id":"fixture"}')
                old.unlink()
                export(dest, src)
            self.assertFalse((dest / "tools/removed.py").exists())
            self.assertEqual((dest / ".env").read_text(), "FAKE_SETTING=preserve")
            self.assertEqual(
                json.loads((dest / ".mda/deployment.json").read_text())["id"], "fixture"
            )

    def test_settings_initialization_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            (app / ".env.example").write_text("RESTOCK_MODE=rehearsal\n")
            self.assertTrue(initialize(app))
            (app / ".env").write_text("FAKE_SETTING=keep\n")
            self.assertFalse(initialize(app))
            self.assertEqual((app / ".env").read_text(), "FAKE_SETTING=keep\n")

    def test_preflight_reports_missing_fields_without_values(self):
        issues = problems(
            {"OPENAI_API_KEY": "synthetic-private-value", "RESTOCK_MODE": "live"}, True
        )
        self.assertTrue(any("LANGSMITH_API_KEY" in issue for issue in issues))
        self.assertTrue(any("rehearsal" in issue for issue in issues))
        self.assertNotIn("synthetic-private-value", str(issues))

    def test_preflight_quotes_and_shell_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.fixture"
            path.write_text('OPENAI_API_KEY="fixture#value" # comment\nRESTOCK_MODE=rehearsal\n')
            values = settings_from_file(path, {"RESTOCK_MODE": "link-test"})
            self.assertEqual(values["OPENAI_API_KEY"], "fixture#value")
            self.assertEqual(values["RESTOCK_MODE"], "link-test")

    def test_release_manifest_has_no_generated_or_private_files(self):
        root = Path(__file__).resolve().parents[1]
        names = [str(path.relative_to(root)) for path in source_files(root)]
        self.assertIn(".env.example", names)
        for name in names:
            self.assertFalse(
                set(Path(name).parts) & {".local", ".git", ".mda", ".venv", "node_modules", ".env"},
                name,
            )
