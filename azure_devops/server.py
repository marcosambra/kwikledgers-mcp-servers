"""
KwikLedgers - MCP Server: Azure DevOps
Expoe ferramentas para o agente interagir com historias, PRs e sprints.
"""
import os
import json
import asyncio
import sys
import subprocess
import re
from html import unescape
from base64 import b64encode
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from azure.devops.connection import Connection
from azure.devops.v7_0.git.models import GitPullRequestSearchCriteria
from azure.devops.v7_0.work_item_tracking.models import TeamContext, Wiql
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


EMPTY_INPUT_SCHEMA = {"type": "object", "properties": {}}
COMPLETED_STATES = {"done", "closed", "resolved"}


def get_team_context() -> TeamContext:
    return TeamContext(project=get_project())


def get_active_pr_search_criteria() -> GitPullRequestSearchCriteria:
    return GitPullRequestSearchCriteria(status="active")


def get_age_days(created_at: Optional[datetime]) -> int:
    if created_at is None:
        return 0

    if created_at.tzinfo is not None:
        return (datetime.now(created_at.tzinfo) - created_at).days

    return (datetime.utcnow() - created_at).days


# --- Servidor MCP ---

server = Server("kwikledgers-azure-devops")


@audit("kwikledgers.azure_devops")
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
       Tool(name="get_active_user", description="Retorna o email do usuario ativo via git config", inputSchema=EMPTY_INPUT_SCHEMA),
       Tool(name="get_my_work_items", description="Lista historias, tasks e bugs atribuidos ao usuario ativo no sprint atual", inputSchema=EMPTY_INPUT_SCHEMA),
       Tool(name="get_my_blocked_items", description="Lista itens bloqueados atribuidos ao usuario ativo no sprint atual", inputSchema=EMPTY_INPUT_SCHEMA),
      Tool(name="get_my_daily_summary", description="Retorna um resumo JSON do sprint atual com itens do usuario ativo, progresso do projeto inteiro, historias do sprint, bloqueios, PRs e story points restantes", inputSchema=EMPTY_INPUT_SCHEMA),
        Tool(name="get_user_stories", description="Lista historias do Azure DevOps atribuidas ao email informado",
             inputSchema={"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}),
      Tool(name="get_sprint_stories", description="Retorna JSON estruturado com todas as historias do sprint atual e o progresso agregado do projeto em modo leve", inputSchema=EMPTY_INPUT_SCHEMA),
      Tool(name="get_sprint_stories_detailed", description="Retorna JSON estruturado com todas as historias do sprint atual incluindo description e acceptance criteria", inputSchema=EMPTY_INPUT_SCHEMA),
        Tool(name="get_story_details", description="Retorna detalhes completos de uma historia: criterios, tasks, story points",
             inputSchema={"type": "object", "properties": {"story_id": {"type": "integer"}}, "required": ["story_id"]}),
        Tool(name="get_open_prs", description="Lista PRs abertas atribuidas ao usuario",
             inputSchema={"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}),
        Tool(name="create_work_item", description="Cria um work item no Azure DevOps, opcionalmente ja direcionado para uma iteration especifica ou para a proxima sprint configurada",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "work_item_type": {"type": "string"},
                     "title": {"type": "string"},
                     "description": {"type": "string"},
                     "iteration_path": {"type": "string"},
                     "use_next_sprint": {"type": "boolean"},
                     "area_path": {"type": "string"},
                     "assigned_to": {"type": "string"},
                     "tags": {"type": "string"},
                     "acceptance_criteria": {"type": "string"},
                     "story_points": {"type": "number"},
                     "remaining_work_hours": {"type": "number"}
                 },
                 "required": ["work_item_type", "title"]
             }),
        Tool(name="move_work_item_to_next_sprint", description="Move um work item existente para a proxima sprint configurada do time no Azure DevOps",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "work_item_id": {"type": "integer"},
                     "comment": {"type": "string"}
                 },
                 "required": ["work_item_id"]
             }),
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
    if name == "get_sprint_stories_detailed":
        return _get_sprint_stories_detailed()
    if name == "get_story_details":
        return _get_story_details(arguments["story_id"])
    if name == "get_open_prs":
        return _get_open_prs(arguments["email"])
    if name == "create_work_item":
        return _create_work_item(
            work_item_type=arguments["work_item_type"],
            title=arguments["title"],
            description=arguments.get("description"),
            iteration_path=arguments.get("iteration_path"),
            use_next_sprint=bool(arguments.get("use_next_sprint", False)),
            area_path=arguments.get("area_path"),
            assigned_to=arguments.get("assigned_to"),
            tags=arguments.get("tags"),
            acceptance_criteria=arguments.get("acceptance_criteria"),
            story_points=arguments.get("story_points"),
            remaining_work_hours=arguments.get("remaining_work_hours"),
        )
    if name == "move_work_item_to_next_sprint":
        return _move_work_item_to_next_sprint(
            work_item_id=arguments["work_item_id"],
            comment=arguments.get("comment"),
        )
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


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", value)).strip()


def _strip_html(value: str | None) -> str:
    if not value:
        return ""

    cleaned = unescape(value)
    replacements = {
        "<br>": "\n",
        "<br/>": "\n",
        "<br />": "\n",
        "</p>": "\n\n",
        "</div>": "\n",
        "</li>": "\n",
        "<li>": "- ",
        "</ul>": "\n",
        "</ol>": "\n",
    }
    for source, target in replacements.items():
        cleaned = cleaned.replace(source, target)
        cleaned = cleaned.replace(source.upper(), target)

    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return _normalize_whitespace(cleaned)


def _build_story_summary(description: str, acceptance_criteria: str, story: dict[str, Any]) -> str:
    summary_parts: list[str] = []

    if description and description != "Sem descricao":
        first_sentence = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0].strip()
        if first_sentence:
            summary_parts.append(first_sentence)

    if acceptance_criteria and acceptance_criteria != "Sem criterios definidos":
        criteria_line = acceptance_criteria.splitlines()[0].strip()
        if criteria_line:
            if len(criteria_line) > 140:
                criteria_line = criteria_line[:137].rstrip() + "..."
            summary_parts.append(f"Criterio-chave: {criteria_line}")

    if not summary_parts:
        summary_parts.append(
            f"Historia {story.get('state', 'sem status')} com {story.get('story_points', 0)} story points."
        )

    summary = " ".join(summary_parts)
    if len(summary) > 280:
        summary = summary[:277].rstrip() + "..."
    return summary


def _serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    raw_value = str(value).strip()
    return raw_value or None


def _get_current_sprint_context() -> dict[str, Any]:
    connection = get_client()
    work_client = connection.clients.get_work_client()
    current_iterations = work_client.get_team_iterations(get_team_context(), timeframe="current")

    if not current_iterations:
        return {
            "name": None,
            "path": None,
            "start_date": None,
            "finish_date": None,
            "timeframe": None,
            "goal": None,
            "goal_available": False,
            "goal_note": "Nenhum sprint atual retornado pela API de iteracoes do Azure DevOps.",
        }

    current_iteration = current_iterations[0]
    attributes = getattr(current_iteration, "attributes", None)
    context = {
        "name": getattr(current_iteration, "name", None),
        "path": getattr(current_iteration, "path", None),
        "start_date": _serialize_datetime(getattr(attributes, "start_date", None)),
        "finish_date": _serialize_datetime(getattr(attributes, "finish_date", None)),
        "timeframe": getattr(attributes, "time_frame", None),
        "goal": None,
        "goal_available": False,
        "goal_note": "A API de iteracoes do Azure DevOps usada por esta integracao nao expoe o sprint goal no payload atual.",
    }

    try:
        org_url = os.environ["AZURE_ORG_URL"].rstrip("/")
        project = quote(get_project(), safe="")
        pat = os.environ["AZURE_PAT"]
        detail_url = f"{org_url}/{project}/_apis/work/teamsettings/iterations?$timeframe=current&api-version=7.1-preview.1"
        auth = b64encode(f":{pat}".encode()).decode()
        request = Request(detail_url, headers={"Authorization": f"Basic {auth}"})
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode())

        values = payload.get("value") or []
        if values:
            goal = values[0].get("goal")
            if isinstance(goal, str) and goal.strip():
                context["goal"] = _normalize_whitespace(goal)
                context["goal_available"] = True
                context["goal_note"] = None
    except Exception:
        pass

    return context


def _serialize_iteration(iteration: Any) -> dict[str, Any]:
    attributes = getattr(iteration, "attributes", None)
    return {
        "name": getattr(iteration, "name", None),
        "path": getattr(iteration, "path", None),
        "start_date": _serialize_datetime(getattr(attributes, "start_date", None)),
        "finish_date": _serialize_datetime(getattr(attributes, "finish_date", None)),
        "timeframe": getattr(attributes, "time_frame", None),
    }


def _get_future_iterations_context() -> list[dict[str, Any]]:
    connection = get_client()
    work_client = connection.clients.get_work_client()
    iterations = work_client.get_team_iterations(get_team_context(), timeframe="future")
    serialized_iterations = [_serialize_iteration(iteration) for iteration in iterations]
    serialized_iterations.sort(
        key=lambda iteration: (
            iteration["start_date"] is None,
            iteration["start_date"] or "",
            iteration["path"] or "",
        )
    )
    return serialized_iterations


def _get_next_sprint_context() -> dict[str, Any]:
    future_iterations = _get_future_iterations_context()
    if not future_iterations:
        raise ValueError("Nenhuma proxima sprint foi encontrada no Azure DevOps para este time.")

    next_sprint = future_iterations[0]
    if not str(next_sprint.get("path") or "").strip():
        raise ValueError("A proxima sprint encontrada nao possui iteration path utilizavel.")

    return next_sprint


def _append_work_item_field_patch(document: list[dict[str, Any]], field_name: str, value: Any) -> None:
    if value is None:
        return

    normalized_value = value.strip() if isinstance(value, str) else value
    if normalized_value == "":
        return

    document.append({"op": "add", "path": f"/fields/{field_name}", "value": normalized_value})


def _serialize_work_item_payload(item: Any, extra_payload: Optional[dict[str, Any]] = None) -> str:
    fields = getattr(item, "fields", {}) or {}
    payload = {
        "id": fields.get("System.Id", getattr(item, "id", None)),
        "title": fields.get("System.Title"),
        "work_item_type": fields.get("System.WorkItemType"),
        "state": fields.get("System.State"),
        "assigned_to": _serialize_assigned_to(fields.get("System.AssignedTo")),
        "iteration_path": fields.get("System.IterationPath"),
        "url": getattr(item, "url", None),
    }
    if extra_payload:
        payload.update(extra_payload)
    return _format_json(payload)


def _is_blocked_item(fields: dict[str, Any]) -> bool:
    state = str(fields.get("System.State", "")).strip().lower()
    tags = str(fields.get("System.Tags", "")).strip().lower()
    return state in {"blocked", "impeded"} or "blocked" in tags or "impediment" in tags


def _is_completed_state(state: Any) -> bool:
    return str(state or "").strip().lower() in COMPLETED_STATES


def _serialize_assigned_to(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("displayName") or value.get("uniqueName") or "Nao atribuido"

    display_name = getattr(value, "display_name", None) or getattr(value, "displayName", None)
    unique_name = getattr(value, "unique_name", None) or getattr(value, "uniqueName", None)
    if display_name:
        return str(display_name)
    if unique_name:
        return str(unique_name)

    raw_value = str(value or "").strip()
    return raw_value or "Nao atribuido"


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

    result = wit.query_by_wiql(query, team_context=get_team_context())
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


def _query_sprint_stories(include_details: bool = False) -> list[dict[str, Any]]:
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    query = Wiql(query="""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo],
               [System.IterationPath], [System.ChangedDate],
               [Microsoft.VSTS.Scheduling.StoryPoints], [System.Tags]
        FROM WorkItems
        WHERE [System.WorkItemType] = 'User Story'
          AND [System.IterationPath] = @CurrentIteration
          AND [System.State] <> 'Removed'
        ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.ChangedDate] DESC
    """)

    result = wit.query_by_wiql(query, team_context=get_team_context())
    if not result.work_items:
        return []

    ids = [str(work_item.id) for work_item in result.work_items]
    fields = [
        "System.Id", "System.Title", "System.State", "System.AssignedTo",
        "System.IterationPath", "System.ChangedDate", "System.Tags",
        "Microsoft.VSTS.Scheduling.StoryPoints"
    ]
    if include_details:
        fields.extend([
            "System.Description",
            "Microsoft.VSTS.Common.AcceptanceCriteria",
        ])

    items = wit.get_work_items(ids=ids, fields=fields)

    serialized_stories: list[dict[str, Any]] = []
    for item in items:
        fields = item.fields
        state = fields.get("System.State")
        serialized_stories.append({
            "id": fields.get("System.Id"),
            "title": fields.get("System.Title"),
            "state": state,
            "assigned_to": _serialize_assigned_to(fields.get("System.AssignedTo")),
            "iteration_path": fields.get("System.IterationPath"),
            "changed_at": str(fields.get("System.ChangedDate", "")),
            "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints") or 0,
            "tags": fields.get("System.Tags", ""),
            "is_blocked": _is_blocked_item(fields),
            "is_completed": _is_completed_state(state),
        })
        if include_details:
            description = _strip_html(fields.get("System.Description")) or "Sem descricao"
            acceptance_criteria = _strip_html(
                fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
            ) or "Sem criterios definidos"
            serialized_stories[-1]["description"] = description
            serialized_stories[-1]["acceptance_criteria"] = acceptance_criteria
            serialized_stories[-1]["summary"] = _build_story_summary(
                description,
                acceptance_criteria,
                serialized_stories[-1],
            )
    return serialized_stories


def _build_sprint_stories_payload(
    sprint_stories: list[dict[str, Any]],
    detail_mode: str,
    sprint_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "project": get_project(),
        "detail_mode": detail_mode,
        "sprint_context": sprint_context or _get_current_sprint_context(),
        "counts": {
            "sprint_stories": len(sprint_stories),
        },
        "project_progress": _build_project_progress(sprint_stories),
        "sprint_stories": sprint_stories,
    }


def _build_project_progress(sprint_stories: list[dict[str, Any]]) -> dict[str, Any]:
    state_breakdown: dict[str, int] = {}
    completed_stories = 0
    blocked_stories = 0
    unassigned_stories = 0
    unestimated_stories = 0
    active_unestimated_stories = 0
    total_story_points = 0.0
    completed_story_points = 0.0
    blocked_story_points = 0.0

    for story in sprint_stories:
        state = str(story.get("state") or "Desconhecido").strip() or "Desconhecido"
        state_breakdown[state] = state_breakdown.get(state, 0) + 1

        story_points = float(story.get("story_points") or 0)
        total_story_points += story_points

        if story_points <= 0:
            unestimated_stories += 1
            if not story.get("is_completed"):
                active_unestimated_stories += 1

        if story.get("is_completed"):
            completed_stories += 1
            completed_story_points += story_points

        if story.get("is_blocked") and not story.get("is_completed"):
            blocked_stories += 1
            blocked_story_points += story_points

        if story.get("assigned_to") in {"", "Nao atribuido"}:
            unassigned_stories += 1

    total_stories = len(sprint_stories)
    active_stories = total_stories - completed_stories
    remaining_story_points = max(total_story_points - completed_story_points, 0.0)
    progress_percent_by_story_count = round((completed_stories / total_stories) * 100, 2) if total_stories else 0.0
    progress_percent_by_story_points = round((completed_story_points / total_story_points) * 100, 2) if total_story_points else 0.0
    progress_warning = None
    if active_unestimated_stories:
        progress_warning = (
            f"Existem {active_unestimated_stories} historias ativas sem story points; "
            "o progresso por story points pode superestimar o andamento do sprint."
        )

    return {
        "total_stories": total_stories,
        "completed_stories": completed_stories,
        "active_stories": active_stories,
        "blocked_stories": blocked_stories,
        "unassigned_stories": unassigned_stories,
        "unestimated_stories": unestimated_stories,
        "active_unestimated_stories": active_unestimated_stories,
        "total_story_points": round(total_story_points, 2),
        "completed_story_points": round(completed_story_points, 2),
        "blocked_story_points": round(blocked_story_points, 2),
        "remaining_story_points": round(remaining_story_points, 2),
        "progress_percent_by_story_count": progress_percent_by_story_count,
        "progress_percent_by_story_points": progress_percent_by_story_points,
        "progress_warning": progress_warning,
        "state_breakdown": state_breakdown,
    }


def _build_daily_summary(email: str) -> dict[str, Any]:
    items = _query_assigned_items(email)
    blocked_items = [item for item in items if item["is_blocked"]]
    story_items = [item for item in items if item["work_item_type"] == "User Story"]
    assigned_story_points = sum(float(item["story_points"] or 0) for item in story_items)
    remaining_work_hours = sum(float(item["remaining_work_hours"] or 0) for item in items)
    sprint_stories = _query_sprint_stories()
    project_progress = _build_project_progress(sprint_stories)
    sprint_context = _get_current_sprint_context()

    open_prs: list[dict[str, Any]] = []
    connection = get_client()
    git = connection.clients.get_git_client()
    repos = git.get_repositories(project=get_project())
    for repo in repos:
        prs = git.get_pull_requests(
            repository_id=repo.id,
            search_criteria=get_active_pr_search_criteria(),
        )
        for pr in prs:
            creator_email = getattr(pr.created_by, "unique_name", "")
            if email.lower() not in creator_email.lower():
                continue

            age_days = get_age_days(pr.creation_date)
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
            "sprint_stories": len(sprint_stories),
        },
        "remaining_story_points": project_progress["remaining_story_points"],
        "assigned_story_points": round(assigned_story_points, 2),
        "remaining_work_hours": remaining_work_hours,
        "sprint_context": sprint_context,
        "project_progress": project_progress,
        "sprint_stories": sprint_stories,
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

    result = wit.query_by_wiql(query, team_context=get_team_context())
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
    """Busca historias do sprint atual com progresso agregado do projeto."""
    sprint_stories = _query_sprint_stories(include_details=False)
    return _format_json(
        _build_sprint_stories_payload(
            sprint_stories,
            detail_mode="light",
            sprint_context=_get_current_sprint_context(),
        )
    )


@audit("kwikledgers.azure_devops")
def _get_sprint_stories_detailed() -> str:
    """Busca historias do sprint atual com description e acceptance criteria."""
    sprint_stories = _query_sprint_stories(include_details=True)
    return _format_json(
        _build_sprint_stories_payload(
            sprint_stories,
            detail_mode="detailed",
            sprint_context=_get_current_sprint_context(),
        )
    )


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
            search_criteria=get_active_pr_search_criteria()
        )
        for pr in prs:
            creator_email = getattr(pr.created_by, "unique_name", "")
            if email.lower() in creator_email.lower():
                age_days = get_age_days(pr.creation_date)
                open_prs.append(
                    f"PR #{pr.pull_request_id}: {pr.title}\n"
                    f"  Repo: {repo.name} | Branch: {pr.source_ref_name} -> {pr.target_ref_name}\n"
                    f"  Criada ha {age_days} dias"
                )

    if not open_prs:
        return "Nenhuma PR aberta encontrada para este usuario."
    return "PRs abertas:\n\n" + "\n\n".join(open_prs)


@audit("kwikledgers.azure_devops")
def _create_work_item(
    work_item_type: str,
    title: str,
    description: str | None = None,
    iteration_path: str | None = None,
    use_next_sprint: bool = False,
    area_path: str | None = None,
    assigned_to: str | None = None,
    tags: str | None = None,
    acceptance_criteria: str | None = None,
    story_points: float | None = None,
    remaining_work_hours: float | None = None,
) -> str:
    normalized_work_item_type = str(work_item_type or "").strip()
    normalized_title = str(title or "").strip()

    if not normalized_work_item_type:
        raise ValueError("work_item_type e obrigatorio para criar um work item.")
    if not normalized_title:
        raise ValueError("title e obrigatorio para criar um work item.")

    next_sprint_context = None
    target_iteration_path = str(iteration_path or "").strip()
    if not target_iteration_path and use_next_sprint:
        next_sprint_context = _get_next_sprint_context()
        target_iteration_path = str(next_sprint_context.get("path") or "").strip()

    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    document: list[dict[str, Any]] = []
    _append_work_item_field_patch(document, "System.Title", normalized_title)
    _append_work_item_field_patch(document, "System.Description", description)
    _append_work_item_field_patch(document, "System.IterationPath", target_iteration_path)
    _append_work_item_field_patch(document, "System.AreaPath", area_path)
    _append_work_item_field_patch(document, "System.AssignedTo", assigned_to)
    _append_work_item_field_patch(document, "System.Tags", tags)
    _append_work_item_field_patch(document, "Microsoft.VSTS.Common.AcceptanceCriteria", acceptance_criteria)
    _append_work_item_field_patch(document, "Microsoft.VSTS.Scheduling.StoryPoints", story_points)
    _append_work_item_field_patch(document, "Microsoft.VSTS.Scheduling.RemainingWork", remaining_work_hours)

    created_item = wit.create_work_item(
        document=document,
        project=get_project(),
        type=normalized_work_item_type,
    )

    return _serialize_work_item_payload(
        created_item,
        {
            "requested_iteration_path": str(iteration_path or "").strip() or None,
            "resolved_iteration_path": target_iteration_path or None,
            "used_next_sprint": bool(next_sprint_context),
            "next_sprint_context": next_sprint_context,
        },
    )


@audit("kwikledgers.azure_devops")
def _move_work_item_to_next_sprint(work_item_id: int, comment: str | None = None) -> str:
    next_sprint_context = _get_next_sprint_context()
    target_iteration_path = str(next_sprint_context.get("path") or "").strip()

    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    current_item = wit.get_work_item(id=work_item_id, fields=[
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.AssignedTo",
        "System.IterationPath",
    ])
    current_fields = current_item.fields
    previous_iteration_path = str(current_fields.get("System.IterationPath") or "").strip()

    if previous_iteration_path == target_iteration_path:
        return _serialize_work_item_payload(
            current_item,
            {
                "previous_iteration_path": previous_iteration_path,
                "resolved_iteration_path": target_iteration_path,
                "moved": False,
                "next_sprint_context": next_sprint_context,
                "message": "O work item ja esta na proxima sprint configurada.",
            },
        )

    document = [{"op": "add", "path": "/fields/System.IterationPath", "value": target_iteration_path}]
    normalized_comment = str(comment or "").strip()
    if normalized_comment:
        document.append({"op": "add", "path": "/fields/System.History", "value": normalized_comment})

    updated_item = wit.update_work_item(document=document, id=work_item_id)
    return _serialize_work_item_payload(
        updated_item,
        {
            "previous_iteration_path": previous_iteration_path or None,
            "resolved_iteration_path": target_iteration_path,
            "moved": True,
            "next_sprint_context": next_sprint_context,
            "comment_added": bool(normalized_comment),
        },
    )


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
