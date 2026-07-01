"""
KwikLedgers - MCP Server: Local Tracking
Gera e atualiza arquivos locais de controle do sprint, logs diarios e metricas.
"""
import os
import json
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from utils.env import discover_workspace_root, load_env_file
from utils.logger import audit


load_env_file(Path(__file__))

ROOT_DIR = discover_workspace_root(Path(__file__))
TRACKING_DIR = ROOT_DIR / "AI_Tracking"
TASK_CONTROL_DIR = TRACKING_DIR / "Task_Control"
DAILY_LOG_DIR = TRACKING_DIR / "Daily_Action_Logs"
METRICS_DIR = TRACKING_DIR / "Metrics"

server = Server("kwikledgers-local-tracking")


@audit("kwikledgers.local_tracking")
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="sync_daily_tracking",
            description="Atualiza controle do sprint, log diario e metricas operacionais em uma unica chamada",
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "JSON serializado retornado pelo resumo do Azure"},
                    "log_title": {"type": "string"},
                    "log_details": {"type": "string"},
                    "metrics_title": {"type": "string"},
                    "metrics_summary": {"type": "string"},
                    "actions_taken": {"type": "array", "items": {"type": "string"}},
                    "source": {"type": "string", "default": "agent"},
                },
                "required": ["summary", "actions_taken"],
            },
        ),
        Tool(
            name="update_task_control",
            description="Cria ou atualiza os arquivos de controle do sprint atual em JSON e Markdown",
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "JSON serializado retornado pelo resumo do Azure"},
                },
                "required": ["summary"],
            },
        ),
        Tool(
            name="append_daily_action_log",
            description="Acrescenta uma entrada estruturada no log diario de acoes",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                    "source": {"type": "string", "default": "agent"},
                },
                "required": ["title", "details"],
            },
        ),
        Tool(
            name="record_ai_metrics",
            description="Registra metricas operacionais do uso de IA em um arquivo Markdown acumulado",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_title": {"type": "string"},
                    "summary": {"type": "string"},
                    "remaining_story_points": {"type": "number"},
                    "blocked_items": {"type": "integer"},
                    "actions_taken": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["entry_title", "summary", "actions_taken"],
            },
        ),
        Tool(
            name="read_tracking_snapshot",
            description="Retorna o estado atual dos arquivos principais em AI_Tracking",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@audit("kwikledgers.local_tracking")
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as error:
        return [TextContent(type="text", text=f"Erro: {str(error)}")]


@audit("kwikledgers.local_tracking")
async def _dispatch(name: str, arguments: dict) -> str:
    if name == "sync_daily_tracking":
        return _sync_daily_tracking(
            arguments["summary"],
            arguments["actions_taken"],
            arguments.get("log_title"),
            arguments.get("log_details"),
            arguments.get("metrics_title"),
            arguments.get("metrics_summary"),
            arguments.get("source", "agent"),
        )
    if name == "update_task_control":
        return _update_task_control(arguments["summary"])
    if name == "append_daily_action_log":
        return _append_daily_action_log(arguments["title"], arguments["details"], arguments.get("source", "agent"))
    if name == "record_ai_metrics":
        return _record_ai_metrics(
            arguments["entry_title"],
            arguments["summary"],
            arguments.get("remaining_story_points"),
            arguments.get("blocked_items"),
            arguments["actions_taken"],
        )
    if name == "read_tracking_snapshot":
        return _read_tracking_snapshot()
    return f"Ferramenta desconhecida: {name}"


def _ensure_directories() -> None:
    TASK_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    METRICS_DIR.mkdir(parents=True, exist_ok=True)


def _today_stamp() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _parse_summary(summary: str) -> dict[str, Any]:
    payload = json.loads(summary)
    if not isinstance(payload, dict):
        raise ValueError("Resumo invalido: esperado objeto JSON")
    return payload


def _load_existing_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    existing = json.loads(path.read_text())
    if not isinstance(existing, dict):
        return {}
    return existing


def _detect_returned_items(previous_snapshot: dict[str, Any], current_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    previous_items = {
        item.get("id"): item
        for item in previous_snapshot.get("items", [])
        if isinstance(item, dict) and item.get("id") is not None
    }

    returned_items: list[dict[str, Any]] = []
    for item in current_snapshot.get("items", []):
        if not isinstance(item, dict):
            continue

        item_id = item.get("id")
        if item_id is None or item_id not in previous_items:
            continue

        previous_item = previous_items[item_id]
        previous_state = str(previous_item.get("state", "")).strip()
        current_state = str(item.get("state", "")).strip()
        if previous_state and current_state and previous_state != current_state:
            returned_items.append({
                "id": item_id,
                "title": item.get("title"),
                "previous_state": previous_state,
                "current_state": current_state,
            })

    return returned_items


@audit("kwikledgers.local_tracking")
def _update_task_control(summary: str) -> str:
    _ensure_directories()
    payload = _parse_summary(summary)
    json_path = TASK_CONTROL_DIR / "current-sprint.json"
    md_path = TASK_CONTROL_DIR / "current-sprint.md"

    previous_snapshot = _load_existing_snapshot(json_path)
    returned_items = _detect_returned_items(previous_snapshot, payload)
    payload["returned_items"] = returned_items

    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")

    items = payload.get("items", [])
    blocked_items = payload.get("blocked_items", [])
    returned_items = payload.get("returned_items", [])
    open_prs = payload.get("open_prs", [])
    counts = payload.get("counts", {})

    lines = [
        "# Sprint Control Snapshot",
        "",
        f"Data: {_today_stamp()}",
        f"Usuario: {payload.get('user_email', 'desconhecido')}",
        f"Projeto: {payload.get('project', 'desconhecido')}",
        "",
        "## Resumo",
        "",
        f"- Itens atribuídos: {counts.get('assigned_items', 0)}",
        f"- Itens bloqueados: {counts.get('blocked_items', 0)}",
        f"- Itens devolvidos ao usuario: {len(returned_items)}",
        f"- User stories no sprint: {counts.get('user_stories', 0)}",
        f"- PRs abertas: {counts.get('open_prs', 0)}",
        f"- Story points restantes: {payload.get('remaining_story_points', 0)}",
        f"- Horas restantes estimadas: {payload.get('remaining_work_hours', 0)}",
        "",
        "## Itens atribuídos",
        "",
    ]

    if items:
        for item in items:
            lines.append(
                f"- KL-{item.get('id')}: {item.get('title')} | {item.get('work_item_type')} | {item.get('state')} | blocked={item.get('is_blocked')}"
            )
    else:
        lines.append("- Nenhum item atribuído no sprint atual.")

    lines.extend(["", "## Itens bloqueados", ""])
    if blocked_items:
        for item in blocked_items:
            lines.append(f"- KL-{item.get('id')}: {item.get('title')} | {item.get('state')}")
    else:
        lines.append("- Nenhum item bloqueado.")

    lines.extend(["", "## Itens devolvidos ao usuario", ""])
    if returned_items:
        for item in returned_items:
            lines.append(
                f"- KL-{item.get('id')}: {item.get('title')} | {item.get('previous_state')} -> {item.get('current_state')}"
            )
    else:
        lines.append("- Nenhum item devolvido detectado desde o ultimo snapshot.")

    lines.extend(["", "## PRs abertas", ""])
    if open_prs:
        for pr in open_prs:
            lines.append(
                f"- PR #{pr.get('pull_request_id')}: {pr.get('title')} | {pr.get('repository')} | {pr.get('age_days')} dias"
            )
    else:
        lines.append("- Nenhuma PR aberta.")

    md_path.write_text("\n".join(lines) + "\n")
    return (
        f"Arquivos atualizados: {json_path} e {md_path}. "
        f"Itens devolvidos detectados: {len(returned_items)}"
    )


def _build_default_log_details(payload: dict[str, Any]) -> str:
    counts = payload.get("counts", {})
    returned_items = payload.get("returned_items", [])
    lines = [
        f"Resumo do sprint para {payload.get('user_email', 'desconhecido')}.",
        f"Itens atribuídos: {counts.get('assigned_items', 0)}.",
        f"Itens bloqueados: {counts.get('blocked_items', 0)}.",
        f"Itens devolvidos detectados: {len(returned_items)}.",
        f"Story points restantes: {payload.get('remaining_story_points', 0)}.",
        f"PRs abertas: {counts.get('open_prs', 0)}.",
    ]
    return "\n".join(lines)


@audit("kwikledgers.local_tracking")
def _append_daily_action_log(title: str, details: str, source: str) -> str:
    _ensure_directories()
    log_path = DAILY_LOG_DIR / f"{_today_stamp()}.md"
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")

    if not log_path.exists():
        log_path.write_text(
            "# Daily Action Log\n\n"
            f"Data: {_today_stamp()}\n"
            "Escopo: atividade operacional do agente e do desenvolvedor\n\n"
            "## Entradas\n\n"
        )

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"### {timestamp} - {title}\n")
        handle.write(f"Fonte: {source}\n\n")
        handle.write(f"{details}\n\n")

    return f"Log atualizado: {log_path}"


@audit("kwikledgers.local_tracking")
def _record_ai_metrics(
    entry_title: str,
    summary: str,
    remaining_story_points: Any,
    blocked_items: Any,
    actions_taken: list[str],
) -> str:
    _ensure_directories()
    metrics_path = METRICS_DIR / "ai-usage-log.md"
    sprint_metrics_path = METRICS_DIR / "sprint-metrics.md"
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")

    if not metrics_path.exists():
        metrics_path.write_text(
            "# AI Usage Metrics Log\n\n"
            "Registro operacional das interacoes com IA relacionadas ao sprint atual.\n\n"
        )

    lines = [
        f"## {timestamp} - {entry_title}",
        "",
        f"Resumo: {summary}",
    ]
    if remaining_story_points is not None:
        lines.append(f"Story points restantes: {remaining_story_points}")
    if blocked_items is not None:
        lines.append(f"Itens bloqueados: {blocked_items}")
    lines.append("Acoes executadas:")
    for action in actions_taken:
        lines.append(f"- {action}")
    lines.append("")

    with metrics_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    if not sprint_metrics_path.exists():
        sprint_metrics_path.write_text(
            "# Sprint Metrics Snapshot\n\n"
            "| Data | Story Points Restantes | Itens Bloqueados | Resumo |\n"
            "|---|---:|---:|---|\n"
        )

    sprint_summary = summary.replace("|", "/")
    with sprint_metrics_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"| {timestamp} | {remaining_story_points if remaining_story_points is not None else '-'} | "
            f"{blocked_items if blocked_items is not None else '-'} | {sprint_summary} |\n"
        )

    return f"Metricas atualizadas: {metrics_path}"


@audit("kwikledgers.local_tracking")
def _sync_daily_tracking(
    summary: str,
    actions_taken: list[str],
    log_title: str | None,
    log_details: str | None,
    metrics_title: str | None,
    metrics_summary: str | None,
    source: str,
) -> str:
    control_result = _update_task_control(summary)
    payload = _parse_summary(summary)

    log_result = _append_daily_action_log(
        log_title or "Sincronizacao diaria do sprint",
        log_details or _build_default_log_details(payload),
        source,
    )
    metrics_result = _record_ai_metrics(
        metrics_title or "Sincronizacao diaria",
        metrics_summary or _build_default_log_details(payload).replace("\n", " "),
        payload.get("remaining_story_points"),
        payload.get("counts", {}).get("blocked_items"),
        actions_taken,
    )

    return "\n".join([control_result, log_result, metrics_result])


@audit("kwikledgers.local_tracking")
def _read_tracking_snapshot() -> str:
    _ensure_directories()
    snapshot = {
        "task_control": str(TASK_CONTROL_DIR / "current-sprint.json"),
        "task_control_markdown": str(TASK_CONTROL_DIR / "current-sprint.md"),
        "daily_log": str(DAILY_LOG_DIR / f"{_today_stamp()}.md"),
        "metrics_log": str(METRICS_DIR / "ai-usage-log.md"),
        "sprint_metrics": str(METRICS_DIR / "sprint-metrics.md"),
        "exists": {
            "task_control": (TASK_CONTROL_DIR / "current-sprint.json").exists(),
            "task_control_markdown": (TASK_CONTROL_DIR / "current-sprint.md").exists(),
            "daily_log": (DAILY_LOG_DIR / f"{_today_stamp()}.md").exists(),
            "metrics_log": (METRICS_DIR / "ai-usage-log.md").exists(),
            "sprint_metrics": (METRICS_DIR / "sprint-metrics.md").exists(),
        },
    }
    return json.dumps(snapshot, indent=2, ensure_ascii=True)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())