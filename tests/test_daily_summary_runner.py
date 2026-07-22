import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_daily_summary.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("daily_summary_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(RUNNER_SPEC)
assert RUNNER_SPEC.loader is not None
RUNNER_SPEC.loader.exec_module(runner)


class DailySummaryRunnerTests(unittest.TestCase):
    def test_build_identity_context_message_includes_env_file_when_present(self):
        message = runner._build_identity_context_message(
            {
                "user_email": "dev@kwikledgers.test",
                "user_identity_context": {
                    "resolved_email": "marcos.pereira@ambrait.com",
                    "email_source": "AZURE_USER_EMAIL",
                    "env_file_path": "/workspace/agent/.env",
                },
            }
        )

        self.assertIn("marcos.pereira@ambrait.com", message)
        self.assertIn("AZURE_USER_EMAIL", message)
        self.assertIn("/workspace/agent/.env", message)

    def test_build_notification_message_uses_summary_counts(self):
        message = runner._build_notification_message(
            {
                "user_email": "dev@kwikledgers.test",
                "user_identity_context": {
                    "resolved_email": "dev@kwikledgers.test",
                    "email_source": "git config user.email",
                    "env_file_path": None,
                },
                "remaining_story_points": 13,
                "counts": {"assigned_items": 4, "blocked_items": 1, "open_prs": 2},
            }
        )

        self.assertIn("Itens atribuidos: 4", message)
        self.assertIn("Bloqueados: 1", message)
        self.assertIn("PRs abertas: 2", message)
        self.assertIn("Story points restantes: 13", message)
        self.assertIn("git config user.email", message)

    def test_run_daily_summary_syncs_tracking_and_notifies(self):
        summary_payload = {
            "user_email": "dev@kwikledgers.test",
            "user_identity_context": {
                "resolved_email": "dev@kwikledgers.test",
                "email_source": "git config user.email",
                "env_file_path": None,
            },
            "remaining_story_points": 21,
            "counts": {"assigned_items": 3, "blocked_items": 1, "open_prs": 2},
        }

        with (
            patch.object(runner.azure_server, "_get_my_daily_summary", return_value=json.dumps(summary_payload)),
            patch.object(runner.tracking_server, "_sync_daily_tracking", return_value="tracking-ok") as sync_mock,
            patch.object(runner.windows_server, "_send_notification", return_value="notification-ok") as notify_mock,
        ):
            result = runner.run_daily_summary()

        self.assertEqual(result["tracking_result"], "tracking-ok")
        self.assertEqual(result["notification_result"], "notification-ok")
        self.assertEqual(result["summary"], summary_payload)

        sync_mock.assert_called_once()
        notify_mock.assert_called_once_with(
            "Resumo diario disponivel",
            runner._build_notification_message(summary_payload),
            "high",
        )

    def test_run_daily_summary_raises_on_non_json_response(self):
        with patch.object(runner.azure_server, "_get_my_daily_summary", return_value="Email nao configurado"):
            with self.assertRaises(RuntimeError):
                runner.run_daily_summary(notify=False)


if __name__ == "__main__":
    unittest.main()