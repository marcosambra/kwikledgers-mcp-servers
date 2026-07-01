"""
KwikLedgers - MCP Server: Windows Calendar & Notifications
Suporta Windows nativo e WSL (usa powershell.exe do host para notificacoes).
"""
import os
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

def _run_powershell(script: str) -> subprocess.CompletedProcess:
    """Executa PowerShell — usa powershell.exe do Windows host quando em WSL."""
    cmd = ["powershell.exe"] if IS_WSL else ["powershell", "-NoProfile", "-Command"]
    if IS_WSL:
        return subprocess.run([*cmd, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=15)
    return subprocess.run([*cmd, script], capture_output=True, text=True, timeout=15)


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
    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $template.SelectSingleNode('//text[@id=1]').InnerText = '{title}'
    $template.SelectSingleNode('//text[@id=2]').InnerText = '{message}'
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('KwikLedgers').Show($toast)
    """
    result = _run_powershell(ps_script)
    if result.returncode != 0:
        # Fallback simples com BalloonTip
        fallback = f"""
        Add-Type -AssemblyName System.Windows.Forms
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.Visible = $true
        $n.ShowBalloonTip(5000, '{title}', '{message}', 'Info')
        Start-Sleep -Seconds 6
        $n.Dispose()
        """
        _run_powershell(fallback)
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
    ps_script = f"""
    $outlook = New-Object -ComObject Outlook.Application
    $appt = $outlook.CreateItem(1)
    $appt.Subject = '{title}'
    $appt.Body = '{description}'
    $appt.Start = '{start}'
    $appt.End = '{end}'
    $appt.ReminderMinutesBeforeStart = {reminder_minutes}
    $appt.ReminderSet = $true
    $appt.Save()
    Write-Output 'OK'
    """
    result = _run_powershell(ps_script)
    if result.returncode != 0:
        return f"Erro ao criar evento: {result.stderr}"
    return f"Evento criado no Outlook: '{title}' em {start}"


@audit("kwikledgers.windows")
def _get_upcoming_deadlines(days: int = 7) -> str:
    """Le eventos dos proximos N dias do Outlook via PowerShell."""
    now = datetime.now()
    end = now + timedelta(days=days)

    ps_script = f"""
    $outlook = New-Object -ComObject Outlook.Application
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
    """
    result = _run_powershell(ps_script)
    if result.returncode != 0:
        return f"Erro ao ler calendario: {result.stderr}"
    lines = result.stdout.strip().splitlines()
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
