import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tests  # noqa: F401
from scripts.configure_office import main
from scripts.office_connection import OfficeUpdateError


class ConfigureOfficeTests(unittest.TestCase):
    # Fictional input only. The MDA subprocess is always mocked.
    answers = [
        "",
        "Demo",
        "Recipient",
        "10 Fixture Road",
        "",
        "Boston",
        "ma",
        "02110",
        "6175550100",
        "",
    ]

    def invoke_setup(self, answers, *, returncode=0):
        output = io.StringIO()
        with (
            patch("sys.argv", ["configure_office.py", "--project", ".local/app"]),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=answers) as prompt,
            patch(
                "scripts.configure_office.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=returncode,
                    stdout="private provider receipt",
                    stderr="private address",
                ),
            ) as save,
            contextlib.redirect_stdout(output),
        ):
            code = main()
        return code, output.getvalue(), prompt, save

    def test_blank_required_field_reprompts_and_preserves_other_answers(self):
        for index in (1, 2, 3, 5, 6, 7, 8):
            with self.subTest(field=index):
                answers = self.answers.copy()
                answers.insert(index, "   ")
                code, output, prompt, save = self.invoke_setup(answers)
                self.assertEqual(code, 0)
                self.assertIn("is required", output)
                self.assertEqual(prompt.call_args_list[index], prompt.call_args_list[index + 1])
                save.assert_called_once()
                value = json.loads(save.call_args.kwargs["input"])
                self.assertEqual(value["label"], "Office")
                self.assertEqual(value["shipping_address"]["address_line1"], "10 Fixture Road")
                self.assertEqual(value["shipping_address"]["state"], "MA")
                self.assertNotIn("address_line2", value["shipping_address"])
                self.assertNotIn("10 Fixture Road", output)
                self.assertNotIn("6175550100", output)
                self.assertNotIn("10 Fixture Road", str(save.call_args.args))

    def test_long_field_error_does_not_repeat_input(self):
        answers = self.answers.copy()
        too_long = "private-fixture-" * 12
        answers.insert(3, too_long)
        code, output, _, save = self.invoke_setup(answers)
        self.assertEqual(code, 0)
        self.assertIn("Street address must be 150 characters or fewer", output)
        self.assertNotIn(too_long, output)
        save.assert_called_once()

    def test_cancel_does_not_save_or_print_a_traceback(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                code, output, _, save = self.invoke_setup(["", "Demo", error])
                self.assertEqual(code, 130)
                self.assertIn("No office Connection was saved", output)
                self.assertNotIn("Traceback", output)
                save.assert_not_called()

    def test_provider_failure_does_not_dump_address_or_receipt(self):
        code, output, _, save = self.invoke_setup(self.answers, returncode=1)
        self.assertEqual(code, 1)
        self.assertIn("Connection was not saved", output)
        self.assertNotIn("private provider receipt", output)
        self.assertNotIn("private address", output)
        self.assertNotIn("10 Fixture Road", output)
        save.assert_called_once()

    def test_optional_email_is_saved_privately_and_invalid_input_reprompts(self):
        answers = self.answers[:-1] + ["not an email", "office@example.invalid"]
        code, output, _, save = self.invoke_setup(answers)
        self.assertEqual(code, 0)
        self.assertIn("one valid email address", output)
        self.assertIn("extra fee", output)
        self.assertEqual(
            json.loads(save.call_args.kwargs["input"])["notification_email"],
            "office@example.invalid",
        )
        self.assertNotIn("office@example.invalid", output)
        self.assertNotIn("office@example.invalid", str(save.call_args.args))

    def invoke_update(self, answers, *, resolve_error=None, replace_error=None):
        output = io.StringIO()
        updater = MagicMock()
        updater.__enter__.return_value = updater
        updater.slug, updater.deployment = "restock-office", "office-b"
        updater.resolve.side_effect = resolve_error
        updater.replace.side_effect = replace_error
        with (
            patch("sys.argv", ["configure_office.py", "--update", "--deployment", "office-b"]),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=answers) as prompt,
            patch("scripts.configure_office.OfficeUpdater.from_project", return_value=updater),
            patch("scripts.configure_office.subprocess.run") as create,
            contextlib.redirect_stdout(output),
        ):
            code = main()
        create.assert_not_called()
        updater.__exit__.assert_called_once()
        return code, output.getvalue(), updater, prompt

    def test_update_reenters_details_and_does_not_recreate_connection(self):
        code, output, updater, _ = self.invoke_update(self.answers)
        self.assertEqual(code, 0)
        updater.resolve.assert_called_once()
        updater.replace.assert_called_once()
        value = updater.replace.call_args.args[0]
        self.assertEqual(value["shipping_address"]["first_name"], "Demo")
        self.assertEqual(value["shipping_address"]["address_line1"], "10 Fixture Road")
        self.assertNotIn("address_line2", value["shipping_address"])
        self.assertNotIn("notification_email", value)
        self.assertIn("no redeploy", output)
        self.assertNotIn("10 Fixture Road", output)
        self.assertNotIn("6175550100", output)

    def test_update_cancellation_and_metadata_failure_make_no_write(self):
        code, output, updater, _ = self.invoke_update([KeyboardInterrupt()])
        self.assertEqual(code, 130)
        updater.replace.assert_not_called()
        self.assertIn("No office Connection was saved", output)
        code, _, updater, prompt = self.invoke_update(
            [], resolve_error=OfficeUpdateError("No matching office credential.")
        )
        self.assertEqual(code, 2)
        prompt.assert_not_called()
        updater.replace.assert_not_called()

    def test_uncertain_update_and_interrupted_write_never_claim_no_change(self):
        for error in (OfficeUpdateError("The update was not confirmed."), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                code, output, updater, _ = self.invoke_update(self.answers, replace_error=error)
                self.assertNotEqual(code, 0)
                updater.replace.assert_called_once()
                self.assertNotIn("No office Connection was saved", output)
                self.assertNotIn("10 Fixture Road", output)
