import base64
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "windows_calendar" / "server.py"
SERVER_SPEC = importlib.util.spec_from_file_location("windows_calendar_server", SERVER_PATH)
server = importlib.util.module_from_spec(SERVER_SPEC)
assert SERVER_SPEC.loader is not None
SERVER_SPEC.loader.exec_module(server)


class WindowsCalendarServerTests(unittest.TestCase):
    def test_run_powershell_uses_encoded_command(self):
        completed = subprocess.CompletedProcess(["powershell"], 0, "OK", "")

        with patch.object(server.subprocess, "run", return_value=completed) as mocked_run:
            result = server._run_powershell("Write-Output 'OK'", timeout_seconds=33)

        self.assertEqual(result, completed)
        command = mocked_run.call_args.args[0]
        self.assertIn("-EncodedCommand", command)
        encoded_script = command[command.index("-EncodedCommand") + 1]
        decoded_script = base64.b64decode(encoded_script).decode("utf-16le")
        self.assertEqual(decoded_script, "Write-Output 'OK'")
        self.assertEqual(mocked_run.call_args.kwargs["timeout"], 33)
        self.assertFalse(mocked_run.call_args.kwargs["text"])

    def test_powershell_literal_escapes_single_quotes(self):
        self.assertEqual(server._powershell_literal("O'Brien"), "O''Brien")

    def test_decode_powershell_output_handles_windows_code_page(self):
        self.assertEqual(server._decode_powershell_output("Reunião".encode("cp1252")), "Reunião")

    def test_sanitize_powershell_output_removes_clixml_wrapper(self):
        raw_output = (
            '#< CLIXML\n'
            '<Objs><S S="Error">Falha ao acessar o Outlook._x000D__x000A_</S></Objs>'
        )
        self.assertEqual(server._sanitize_powershell_output(raw_output), "Falha ao acessar o Outlook.")

    def test_get_upcoming_deadlines_returns_actionable_timeout_message(self):
        timeout_result = subprocess.CompletedProcess(
            ["powershell.exe"],
            124,
            "",
            "PowerShell expirou apos 90 segundos ao executar a integracao Windows.",
        )

        with patch.object(server, "_run_powershell", return_value=timeout_result):
            result = server._get_upcoming_deadlines(3)

        self.assertIn("Erro ao ler calendario:", result)
        self.assertIn("PowerShell expirou", result)


if __name__ == "__main__":
    unittest.main()