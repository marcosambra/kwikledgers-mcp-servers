"""
KwikLedgers - MCP Server: Windows Calendar & Notifications
Suporta Windows nativo e WSL (usa powershell.exe do host para notificacoes).
"""
import base64
import os
import re
import sys
import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from utils.env import load_env_file
from utils.logger import audit


load_env_file(Path(__file__))

# Detecta se esta rodando dentro do WSL
IS_WSL = "microsoft" in (open("/proc/version").read().lower() if os.path.exists("/proc/version") else "")

DEFAULT_NOTIFICATION_TIMEOUT_SECONDS = 20
DEFAULT_OUTLOOK_TIMEOUT_SECONDS = 90

server = Server("kwikledgers-windows")


@audit("kwikledgers.windows")
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="send_notification",
            description="Envia uma notificacao toast no Windows (funciona no WSL tambem)",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "urgency": {"type": "string", "enum": ["normal", "high"], "default": "normal"}
                },
                "required": ["title", "message"]
            }
        ),
        Tool(
            name="create_calendar_event",
            description="Cria um evento no calendario do Outlook",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "ISO 8601: 2025-01-15T14:00:00"},
                    "end": {"type": "string", "description": "ISO 8601: 2025-01-15T15:00:00"},
                    "description": {"type": "string"},
                    "reminder_minutes": {"type": "integer", "default": 30}
                },
                "required": ["title", "start", "end"]
            }
        ),
        Tool(
            name="get_upcoming_deadlines",
            description="Retorna eventos dos proximos N dias do calendario do Outlook",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 7}
                }
            }
        ),
        Tool(
            name="schedule_reminder",
            description="Agenda um lembrete no calendario",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "remind_at": {"type": "string", "description": "ISO 8601"},
                    "message": {"type": "string"}
                },
                "required": ["title", "remind_at", "message"]
            }
        ),
    ]


@audit("kwikledgers.windows")
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        return [TextContent(type="text", text=f"Erro: {str(e)}")]


@audit("kwikledgers.windows")
async def _dispatch(name: str, arguments: dict) -> str:
    if name == "send_notification":
        return _send_notification(arguments["title"], arguments["message"], arguments.get("urgency", "normal"))
    if name == "create_calendar_event":
        return _create_calendar_event(
            arguments["title"], arguments["start"], arguments["end"],
            arguments.get("description", ""), arguments.get("reminder_minutes", 30)
        )
    if name == "get_upcoming_deadlines":
        return _get_upcoming_deadlines(arguments.get("days", 7))
    if name == "schedule_reminder":
        return _schedule_reminder(arguments["title"], arguments["remind_at"], arguments["message"])
    return f"Ferramenta desconhecida: {name}"


# --- Helpers de ambiente ---

def _get_timeout_seconds(env_name: str, default: int) -> int:
    raw_value = os.getenv(env_name, str(default)).strip()
    try:
        parsed_value = int(raw_value)
    except ValueError:
        return default
    return parsed_value if parsed_value > 0 else default


def _powershell_literal(value: str) -> str:
    return value.replace("'", "''")


def _encode_powershell_script(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _decode_powershell_output(raw_output: bytes | str | None) -> str:
    if raw_output is None:
        return ""
    if isinstance(raw_output, str):
        return raw_output
    for encoding in ("utf-8", "cp1252", "cp850", "latin-1"):
        try:
            return raw_output.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_output.decode("utf-8", errors="replace")


def _sanitize_powershell_output(text: str) -> str:
    cleaned = text.replace("#< CLIXML", "").replace("_x000D__x000A_", "\n")
    error_lines = re.findall(r'<S S="Error">(.*?)</S>', cleaned, flags=re.S)
    if error_lines:
        cleaned = "\n".join(error_lines)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _compose_powershell_script(body: str, include_outlook_helper: bool = False) -> str:
    prelude = [
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "$OutputEncoding = [System.Text.Encoding]::UTF8",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
    ]
    if include_outlook_helper:
        prelude.extend([
            "function Get-OutlookApplication {",
            "    $attempts = 6",
            "    for ($attempt = 1; $attempt -le $attempts; $attempt++) {",
            "        try {",
            "            try {",
            "                return [System.Runtime.InteropServices.Marshal]::GetActiveObject('Outlook.Application')",
            "            } catch {",
            "                return New-Object -ComObject Outlook.Application",
            "            }",
            "        } catch [System.Runtime.InteropServices.COMException] {",
            "            if ($_.Exception.HResult -eq -2147418111 -and $attempt -lt $attempts) {",
            "                Start-Sleep -Seconds 2",
            "                continue",
            "            }",
            "            throw",
            "        }",
            "    }",
            "    throw 'Nao foi possivel obter a automacao do Outlook apos varias tentativas.'",
            "}",
        ])
    prelude.append(body.strip())
    return "\n".join(prelude)


def _run_powershell(script: str, timeout_seconds: int) -> subprocess.CompletedProcess:
    """Executa PowerShell usando EncodedCommand para evitar problemas de quoting no WSL."""
    shell_name = "powershell.exe" if IS_WSL else "powershell"
    encoded_script = _encode_powershell_script(script)
    cmd = [
        shell_name,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-STA",
        "-EncodedCommand",
        encoded_script,
    ]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            exc.cmd,
            124,
            _decode_powershell_output(exc.stdout),
            (
                "PowerShell expirou apos "
                f"{timeout_seconds} segundos ao executar a integracao Windows. "
                "O Outlook pode estar preso em primeiro acesso, perfil MAPI bloqueado ou COM indisponivel no host."
            ),
        )


def _format_powershell_error(action: str, result: subprocess.CompletedProcess) -> str:
    stderr_text = _decode_powershell_output(result.stderr)
    stdout_text = _decode_powershell_output(result.stdout)
    details = _sanitize_powershell_output(
        stderr_text or stdout_text or "Falha sem detalhes retornados pelo PowerShell."
    )
    return f"Erro ao {action}: {details}"


# --- Implementacoes ---

@audit("kwikledgers.windows")
def _send_notification(title: str, message: str, urgency: str = "normal") -> str:
    """Envia notificacao toast. Funciona em Windows nativo e WSL via powershell.exe."""
    if not IS_WSL:
        # Windows nativo: tenta winotify primeiro
        try:
            from winotify import Notification, audio
            toast = Notification(app_id="KwikLedgers Dev Agent", title=title, msg=message,
                                 duration="short" if urgency == "normal" else "long")
            if urgency == "high":
                toast.set_audio(audio.Default, loop=False)
            toast.show()
            return f"Notificacao enviada: {title}"
        except ImportError:
            pass

    # WSL ou fallback: usa powershell.exe do host Windows
    safe_title = _powershell_literal(title)
    safe_message = _powershell_literal(message)
    timeout_seconds = _get_timeout_seconds(
        "KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS",
        DEFAULT_NOTIFICATION_TIMEOUT_SECONDS,
    )
    ps_script = _compose_powershell_script(f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $template.SelectSingleNode('//text[@id=1]').InnerText = '{safe_title}'
    $template.SelectSingleNode('//text[@id=2]').InnerText = '{safe_message}'
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('KwikLedgers').Show($toast)
    Write-Output 'OK'
    """)
    result = _run_powershell(ps_script, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        # Fallback simples com BalloonTip
        fallback = _compose_powershell_script(f"""
        Add-Type -AssemblyName System.Windows.Forms
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.Visible = $true
        $n.ShowBalloonTip(5000, '{safe_title}', '{safe_message}', 'Info')
        Start-Sleep -Seconds 6
        $n.Dispose()
        Write-Output 'OK'
        """)
        fallback_result = _run_powershell(fallback, timeout_seconds=timeout_seconds)
        if fallback_result.returncode != 0:
            return _format_powershell_error("enviar notificacao", fallback_result)
    return f"Notificacao enviada: {title}"


@audit("kwikledgers.windows")
def _create_calendar_event(
    title: str, start: str, end: str,
    description: str = "", reminder_minutes: int = 30
) -> str:
    """Cria evento no Outlook. Funciona via COM no Windows nativo ou powershell.exe no WSL."""
    if not IS_WSL:
        try:
            import win32com.client
            outlook = win32com.client.Dispatch("Outlook.Application")
            appt = outlook.CreateItem(1)
            appt.Subject = title
            appt.Body = description
            appt.Start = start
            appt.End = end
            appt.ReminderMinutesBeforeStart = reminder_minutes
            appt.ReminderSet = True
            appt.Save()
            return f"Evento criado: '{title}' em {start}"
        except ImportError:
            pass

    # WSL: usa powershell.exe do host
    safe_title = _powershell_literal(title)
    safe_description = _powershell_literal(description)
    safe_start = _powershell_literal(start)
    safe_end = _powershell_literal(end)
    timeout_seconds = _get_timeout_seconds(
        "KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS",
        DEFAULT_OUTLOOK_TIMEOUT_SECONDS,
    )
    ps_script = _compose_powershell_script(f"""
    $outlook = Get-OutlookApplication
    $appt = $outlook.CreateItem(1)
    $appt.Subject = '{safe_title}'
    $appt.Body = '{safe_description}'
    $appt.Start = '{safe_start}'
    $appt.End = '{safe_end}'
    $appt.ReminderMinutesBeforeStart = {reminder_minutes}
    $appt.ReminderSet = $true
    $appt.Save()
    Write-Output 'OK'
    """, include_outlook_helper=True)
    result = _run_powershell(ps_script, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        return _format_powershell_error("criar evento no Outlook", result)
    return f"Evento criado no Outlook: '{title}' em {start}"


@audit("kwikledgers.windows")
def _get_upcoming_deadlines(days: int = 7) -> str:
    """Le eventos dos proximos N dias do Outlook via PowerShell."""
    now = datetime.now()
    end = now + timedelta(days=days)
    timeout_seconds = _get_timeout_seconds(
        "KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS",
        DEFAULT_OUTLOOK_TIMEOUT_SECONDS,
    )

    ps_script = _compose_powershell_script(f"""
    $outlook = Get-OutlookApplication
    $ns = $outlook.GetNamespace('MAPI')
    $cal = $ns.GetDefaultFolder(9)
    $items = $cal.Items
    $items.Sort('[Start]')
    $items.IncludeRecurrences = $true
    $filter = "[Start] >= '{now.strftime('%m/%d/%Y %H:%M')}' AND [Start] <= '{end.strftime('%m/%d/%Y %H:%M')}'"
    $restricted = $items.Restrict($filter)
    foreach ($item in $restricted) {{
        Write-Output "$($item.Start.ToString('dd/MM HH:mm')) | $($item.Subject)"
    }}
    """, include_outlook_helper=True)
    result = _run_powershell(ps_script, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        return _format_powershell_error("ler calendario", result)
    lines = _decode_powershell_output(result.stdout).strip().splitlines()
    if not lines:
        return f"Nenhum evento nos proximos {days} dias."
    return f"Proximos {days} dias:\n" + "\n".join(f"  {l}" for l in lines)


@audit("kwikledgers.windows")
def _schedule_reminder(title: str, remind_at: str, message: str) -> str:
    """Cria evento curto como lembrete."""
    start_dt = datetime.fromisoformat(remind_at)
    end_dt = start_dt + timedelta(minutes=15)
    return _create_calendar_event(
        title=f"[LEMBRETE] {title}",
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        description=message,
        reminder_minutes=0
    )


# --- Entry point ---

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
