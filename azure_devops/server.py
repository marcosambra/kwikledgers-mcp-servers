"""
KwikLedgers - MCP Server: Azure DevOps
Expoe ferramentas para o agente interagir com historias, PRs e sprints.
"""
import os
import json
import asyncio
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from azure.devops.connection import Connection
from azure.devops.v7_0.work_item_tracking.models import Wiql
from msrest.authentication import BasicAuthentication
from utils.env import load_env_file
from utils.logger import audit


load_env_file(Path(__file__))


# --- Conexao com Azure DevOps ---

def get_client():
    org_url = os.environ["AZURE_ORG_URL"]
    pat = os.environ["AZURE_PAT"]
    credentials = BasicAuthentication("", pat)
    connection = Connection(base_url=org_url, creds=credentials)
    return connection


def get_project():
    return os.environ.get("AZURE_PROJECT", "KwikLedgers")


# --- Servidor MCP ---

server = Server("kwikledgers-azure-devops")


@audit("kwikledgers.azure_devops")
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="get_active_user", description="Retorna o email do usuario ativo via git config"),
        Tool(name="get_my_work_items", description="Lista historias, tasks e bugs atribuidos ao usuario ativo no sprint atual"),
        Tool(name="get_my_blocked_items", description="Lista itens bloqueados atribuidos ao usuario ativo no sprint atual"),
        Tool(name="get_my_daily_summary", description="Retorna um resumo JSON do sprint atual com itens do usuario ativo, bloqueios, PRs e story points restantes"),
        Tool(name="get_user_stories", description="Lista historias do Azure DevOps atribuidas ao email informado",
             inputSchema={"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}),
        Tool(name="get_sprint_stories", description="Lista todas as historias do sprint atual do projeto"),
        Tool(name="get_story_details", description="Retorna detalhes completos de uma historia: criterios, tasks, story points",
             inputSchema={"type": "object", "properties": {"story_id": {"type": "integer"}}, "required": ["story_id"]}),
        Tool(name="get_open_prs", description="Lista PRs abertas atribuidas ao usuario",
             inputSchema={"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}),
        Tool(name="update_story_status", description="Atualiza o status de uma historia no Azure DevOps",
             inputSchema={"type": "object",
                          "properties": {"story_id": {"type": "integer"}, "status": {"type": "string"}},
                          "required": ["story_id", "status"]}),
        Tool(name="add_story_comment", description="Adiciona um comentario a uma historia",
             inputSchema={"type": "object",
                          "properties": {"story_id": {"type": "integer"}, "comment": {"type": "string"}},
                          "required": ["story_id", "comment"]}),
    ]


@audit("kwikledgers.azure_devops")
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        return [TextContent(type="text", text=f"Erro: {str(e)}")]


@audit("kwikledgers.azure_devops")
async def _dispatch(name: str, arguments: dict) -> str:
    if name == "get_active_user":
        return _get_active_user()
    if name == "get_my_work_items":
        return _get_my_work_items()
    if name == "get_my_blocked_items":
        return _get_my_blocked_items()
    if name == "get_my_daily_summary":
        return _get_my_daily_summary()
    if name == "get_user_stories":
        return _get_user_stories(arguments["email"])
    if name == "get_sprint_stories":
        return _get_sprint_stories()
    if name == "get_story_details":
        return _get_story_details(arguments["story_id"])
    if name == "get_open_prs":
        return _get_open_prs(arguments["email"])
    if name == "update_story_status":
        return _update_story_status(arguments["story_id"], arguments["status"])
    if name == "add_story_comment":
        return _add_story_comment(arguments["story_id"], arguments["comment"])
    return f"Ferramenta desconhecida: {name}"


# --- Implementacoes das ferramentas ---

def _resolve_active_user_email() -> Optional[str]:
    configured_email = os.environ.get("AZURE_USER_EMAIL", "").strip()
    if configured_email:
        return configured_email

    for command in (["git", "config", "user.email"], ["git", "config", "--global", "user.email"]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        except Exception:
            continue

        email = result.stdout.strip()
        if email:
            return email

    return None


def _format_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=True)


def _is_blocked_item(fields: dict[str, Any]) -> bool:
    state = str(fields.get("System.State", "")).strip().lower()
    tags = str(fields.get("System.Tags", "")).strip().lower()
    return state in {"blocked", "impeded"} or "blocked" in tags or "impediment" in tags


def _query_assigned_items(email: str) -> list[dict[str, Any]]:
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    query = Wiql(query=f"""
        SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType],
               [System.IterationPath], [System.ChangedDate],
               [Microsoft.VSTS.Scheduling.StoryPoints], [Microsoft.VSTS.Scheduling.RemainingWork],
               [System.Tags]
        FROM WorkItems
        WHERE [System.AssignedTo] = '{email}'
          AND [System.IterationPath] = @CurrentIteration
          AND [System.WorkItemType] IN ('User Story', 'Task', 'Bug')
          AND [System.State] NOT IN ('Closed', 'Removed', 'Done')
        ORDER BY [System.ChangedDate] DESC
    """)

    result = wit.query_by_wiql(query, project=get_project())
    if not result.work_items:
        return []

    ids = [str(work_item.id) for work_item in result.work_items]
    items = wit.get_work_items(ids=ids, fields=[
        "System.Id", "System.Title", "System.State", "System.WorkItemType",
        "System.IterationPath", "System.ChangedDate", "System.Tags",
        "Microsoft.VSTS.Scheduling.StoryPoints", "Microsoft.VSTS.Scheduling.RemainingWork"
    ])

    serialized_items: list[dict[str, Any]] = []
    for item in items:
        fields = item.fields
        serialized_items.append({
            "id": fields.get("System.Id"),
            "title": fields.get("System.Title"),
            "state": fields.get("System.State"),
            "work_item_type": fields.get("System.WorkItemType"),
            "iteration_path": fields.get("System.IterationPath"),
            "changed_at": str(fields.get("System.ChangedDate", "")),
            "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints") or 0,
            "remaining_work_hours": fields.get("Microsoft.VSTS.Scheduling.RemainingWork") or 0,
            "tags": fields.get("System.Tags", ""),
            "is_blocked": _is_blocked_item(fields),
        })
    return serialized_items


def _build_daily_summary(email: str) -> dict[str, Any]:
    items = _query_assigned_items(email)
    blocked_items = [item for item in items if item["is_blocked"]]
    story_items = [item for item in items if item["work_item_type"] == "User Story"]
    total_story_points = sum(float(item["story_points"] or 0) for item in story_items)
    remaining_work_hours = sum(float(item["remaining_work_hours"] or 0) for item in items)

    open_prs: list[dict[str, Any]] = []
    connection = get_client()
    git = connection.clients.get_git_client()
    repos = git.get_repositories(project=get_project())
    for repo in repos:
        prs = git.get_pull_requests(repository_id=repo.id, search_criteria={"status": "active"})
        for pr in prs:
            creator_email = getattr(pr.created_by, "unique_name", "")
            if email.lower() not in creator_email.lower():
                continue

            age_days = (datetime.utcnow() - pr.creation_date).days
            open_prs.append({
                "pull_request_id": pr.pull_request_id,
                "title": pr.title,
                "repository": repo.name,
                "source_branch": pr.source_ref_name,
                "target_branch": pr.target_ref_name,
                "age_days": age_days,
            })

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "project": get_project(),
        "user_email": email,
        "counts": {
            "assigned_items": len(items),
            "blocked_items": len(blocked_items),
            "open_prs": len(open_prs),
            "user_stories": len(story_items),
        },
        "remaining_story_points": total_story_points,
        "remaining_work_hours": remaining_work_hours,
        "items": items,
        "blocked_items": blocked_items,
        "open_prs": open_prs,
    }

@audit("kwikledgers.azure_devops")
def _get_active_user() -> str:
    """Le o email do git config local ou global."""
    email = _resolve_active_user_email()
    if email:
        return f"Usuario ativo: {email}"
    return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"


@audit("kwikledgers.azure_devops")
def _get_my_work_items() -> str:
    email = _resolve_active_user_email()
    if not email:
        return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"

    payload = _build_daily_summary(email)
    payload.pop("open_prs", None)
    return _format_json(payload)


@audit("kwikledgers.azure_devops")
def _get_my_blocked_items() -> str:
    email = _resolve_active_user_email()
    if not email:
        return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"

    payload = _build_daily_summary(email)
    return _format_json({
        "generated_at": payload["generated_at"],
        "project": payload["project"],
        "user_email": payload["user_email"],
        "blocked_count": payload["counts"]["blocked_items"],
        "blocked_items": payload["blocked_items"],
    })


@audit("kwikledgers.azure_devops")
def _get_my_daily_summary() -> str:
    email = _resolve_active_user_email()
    if not email:
        return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"

    return _format_json(_build_daily_summary(email))


@audit("kwikledgers.azure_devops")
def _get_user_stories(email: str) -> str:
    """Busca historias atribuidas ao email no Azure DevOps."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    query = Wiql(query=f"""
        SELECT [System.Id], [System.Title], [System.State],
               [Microsoft.VSTS.Scheduling.StoryPoints], [System.IterationPath]
        FROM WorkItems
        WHERE [System.AssignedTo] = '{email}'
          AND [System.WorkItemType] = 'User Story'
          AND [System.State] NOT IN ('Closed', 'Resolved', 'Done')
        ORDER BY [Microsoft.VSTS.Common.Priority] ASC
    """)

    result = wit.query_by_wiql(query, project=get_project())
    if not result.work_items:
        return "Nenhuma historia encontrada para este usuario."

    ids = [str(wi.id) for wi in result.work_items]
    items = wit.get_work_items(ids=ids, fields=[
        "System.Id", "System.Title", "System.State",
        "Microsoft.VSTS.Scheduling.StoryPoints", "System.IterationPath"
    ])

    lines = ["Historias atribuidas:\n"]
    for item in items:
        f = item.fields
        lines.append(
            f"KL-{f['System.Id']}: {f['System.Title']}\n"
            f"  Status: {f['System.State']} | "
            f"Story Points: {f.get('Microsoft.VSTS.Scheduling.StoryPoints', '?')} | "
            f"Sprint: {f.get('System.IterationPath', '?')}\n"
        )
    return "\n".join(lines)


@audit("kwikledgers.azure_devops")
def _get_sprint_stories() -> str:
    """Busca historias do sprint atual."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    query = Wiql(query="""
        SELECT [System.Id], [System.Title], [System.State],
               [System.AssignedTo], [Microsoft.VSTS.Scheduling.StoryPoints]
        FROM WorkItems
        WHERE [System.WorkItemType] = 'User Story'
          AND [System.IterationPath] = @CurrentIteration
          AND [System.State] NOT IN ('Closed', 'Done')
        ORDER BY [Microsoft.VSTS.Common.Priority] ASC
    """)

    result = wit.query_by_wiql(query, project=get_project())
    if not result.work_items:
        return "Nenhuma historia no sprint atual."

    ids = [str(wi.id) for wi in result.work_items]
    items = wit.get_work_items(ids=ids, fields=[
        "System.Id", "System.Title", "System.State",
        "System.AssignedTo", "Microsoft.VSTS.Scheduling.StoryPoints"
    ])

    lines = ["Sprint atual:\n"]
    for item in items:
        f = item.fields
        assigned = f.get("System.AssignedTo", {})
        assigned_name = assigned.get("displayName", "Nao atribuido") if isinstance(assigned, dict) else str(assigned)
        lines.append(
            f"KL-{f['System.Id']}: {f['System.Title']}\n"
            f"  Status: {f['System.State']} | "
            f"Points: {f.get('Microsoft.VSTS.Scheduling.StoryPoints', '?')} | "
            f"Responsavel: {assigned_name}\n"
        )
    return "\n".join(lines)


@audit("kwikledgers.azure_devops")
def _get_story_details(story_id: int) -> str:
    """Retorna detalhes completos da historia incluindo criterios de aceite e tasks."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    item = wit.get_work_item(id=story_id, expand="Relations", fields=[
        "System.Id", "System.Title", "System.Description", "System.State",
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        "Microsoft.VSTS.Scheduling.StoryPoints",
        "Microsoft.VSTS.Common.Priority",
        "System.IterationPath", "System.AssignedTo",
        "System.Tags"
    ])

    f = item.fields
    lines = [
        f"=== KL-{story_id}: {f['System.Title']} ===",
        f"Status: {f['System.State']}",
        f"Story Points: {f.get('Microsoft.VSTS.Scheduling.StoryPoints', '?')}",
        f"Prioridade: {f.get('Microsoft.VSTS.Common.Priority', '?')}",
        f"Sprint: {f.get('System.IterationPath', '?')}",
        "",
        "Descricao:",
        f.get("System.Description", "Sem descricao"),
        "",
        "Criterios de Aceite:",
        f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "Sem criterios definidos"),
        "",
    ]

    # Busca tasks filhas
    if item.relations:
        task_ids = [
            rel.url.split("/")[-1]
            for rel in item.relations
            if rel.rel == "System.LinkTypes.Hierarchy-Forward"
        ]
        if task_ids:
            tasks = wit.get_work_items(ids=task_ids, fields=[
                "System.Id", "System.Title", "System.State",
                "Microsoft.VSTS.Scheduling.RemainingWork"
            ])
            lines.append("Tasks:")
            for task in tasks:
                tf = task.fields
                lines.append(
                    f"  - [{tf['System.State']}] {tf['System.Title']} "
                    f"(horas restantes: {tf.get('Microsoft.VSTS.Scheduling.RemainingWork', '?')})"
                )

    return "\n".join(lines)


@audit("kwikledgers.azure_devops")
def _get_open_prs(email: str) -> str:
    """Lista PRs abertas criadas pelo usuario."""
    connection = get_client()
    git = connection.clients.get_git_client()

    repos = git.get_repositories(project=get_project())
    open_prs = []

    for repo in repos:
        prs = git.get_pull_requests(
            repository_id=repo.id,
            search_criteria={"status": "active"}
        )
        for pr in prs:
            creator_email = getattr(pr.created_by, "unique_name", "")
            if email.lower() in creator_email.lower():
                age_days = (datetime.utcnow() - pr.creation_date).days
                open_prs.append(
                    f"PR #{pr.pull_request_id}: {pr.title}\n"
                    f"  Repo: {repo.name} | Branch: {pr.source_ref_name} -> {pr.target_ref_name}\n"
                    f"  Criada ha {age_days} dias"
                )

    if not open_prs:
        return "Nenhuma PR aberta encontrada para este usuario."
    return "PRs abertas:\n\n" + "\n\n".join(open_prs)


@audit("kwikledgers.azure_devops")
def _update_story_status(story_id: int, status: str) -> str:
    """Atualiza o estado de uma historia."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    patch = [{"op": "add", "path": "/fields/System.State", "value": status}]
    wit.update_work_item(document=patch, id=story_id)
    return f"Historia KL-{story_id} atualizada para: {status}"


@audit("kwikledgers.azure_devops")
def _add_story_comment(story_id: int, comment: str) -> str:
    """Adiciona comentario a uma historia."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    patch = [{"op": "add", "path": "/fields/System.History", "value": comment}]
    wit.update_work_item(document=patch, id=story_id)
    return f"Comentario adicionado na historia KL-{story_id}"


# --- Entry point ---

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
