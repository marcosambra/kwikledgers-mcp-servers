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
import unicodedata
from difflib import SequenceMatcher
from html import escape, unescape
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
WORK_ITEM_TYPE_ALIASES = {
    "task": ("Task",),
    "tasks": ("Task",),
    "story": ("User Story", "Story"),
    "stories": ("User Story", "Story"),
    "user story": ("User Story", "Story"),
    "user stories": ("User Story", "Story"),
    "historia": ("User Story", "Story"),
    "historias": ("User Story", "Story"),
    "bug": ("Bug",),
    "bugs": ("Bug",),
    "technical debt": ("Technical Debt", "Technical debt", "Tech Debt"),
    "technical debts": ("Technical Debt", "Technical debt", "Tech Debt"),
    "tech debt": ("Technical Debt", "Technical debt", "Tech Debt"),
    "tech debts": ("Technical Debt", "Technical debt", "Tech Debt"),
    "debito tecnico": ("Technical Debt", "Technical debt", "Tech Debt"),
    "debitos tecnicos": ("Technical Debt", "Technical debt", "Tech Debt"),
}
WORK_ITEM_FIELD_ALIASES = {
    "Microsoft.VSTS.Common.AcceptanceCriteria": "acceptance_criteria",
    "Microsoft.VSTS.Scheduling.StoryPoints": "story_points",
    "Microsoft.VSTS.Scheduling.RemainingWork": "remaining_work_hours",
    "Microsoft.VSTS.Scheduling.CompletedWork": "completed_work_hours",
    "Microsoft.VSTS.Scheduling.OriginalEstimate": "original_estimate_hours",
}
WORK_ITEM_TYPE_CACHE: dict[str, dict[str, str]] = {}
WORK_ITEM_FIELD_CACHE: dict[tuple[str, str], set[str]] = {}
TEAM_CONTEXT_CACHE: dict[str, str] = {}
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
TECHNICAL_DEBT_CONTEXT_DIR = WORKSPACE_ROOT / "AI_Tracking" / "Repo_Work_Item_Context" / "technical_debts"
SIMILARITY_STOPWORDS = {
    "a", "ao", "aos", "as", "bug", "bugs", "com", "como", "da", "das", "de", "debito",
    "debt", "do", "dos", "e", "em", "for", "generic", "historia", "issue", "it", "its",
    "na", "nas", "no", "nos", "o", "of", "os", "ou", "para", "por", "repository", "repo",
    "service", "story", "task", "tasks", "tecnica", "tecnico", "technical", "tecnicas", "tecnicos",
    "the", "to", "um", "uma", "update", "work", "item",
}


def get_team_context() -> TeamContext:
    team_name = _resolve_team_name()
    if team_name:
        return TeamContext(project=get_project(), team=team_name)
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
                     "repository_name": {"type": "string"},
                     "iteration_path": {"type": "string"},
                     "use_next_sprint": {"type": "boolean"},
                     "area_path": {"type": "string"},
                     "assigned_to": {"type": "string"},
                     "tags": {"type": "string"},
                     "parent_work_item_id": {"type": "integer"},
                     "acceptance_criteria": {"type": "string"},
                     "story_points": {"type": "number"},
                     "remaining_work_hours": {"type": "number"},
                     "completed_work_hours": {"type": "number"},
                     "original_estimate_hours": {"type": "number"}
                 },
                 "required": ["work_item_type", "title"]
             }),
        Tool(name="find_similar_technical_debts", description="Busca debitos tecnicos parecidos no Azure DevOps para evitar duplicidade e salva um contexto local por repositorio com os itens associados",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "repository_name": {"type": "string"},
                     "title": {"type": "string"},
                     "description": {"type": "string"},
                     "limit": {"type": "integer"}
                 },
                 "required": ["repository_name", "title"]
             }),
        Tool(name="update_work_item_content", description="Atualiza Description e Acceptance Criteria de um work item existente usando rich text HTML simples compativel com o Azure DevOps",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "work_item_id": {"type": "integer"},
                     "description": {"type": "string"},
                     "acceptance_criteria": {"type": "string"},
                     "comment": {"type": "string"}
                 },
                 "required": ["work_item_id"]
             }),
        Tool(name="update_work_item_effort", description="Atualiza esforco e horas de um work item no Azure DevOps, incluindo horas gastas, horas restantes, estimativa original e story points quando o tipo suportar esses campos",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "work_item_id": {"type": "integer"},
                     "story_points": {"type": "number"},
                     "remaining_work_hours": {"type": "number"},
                     "completed_work_hours": {"type": "number"},
                     "original_estimate_hours": {"type": "number"},
                     "comment": {"type": "string"}
                 },
                 "required": ["work_item_id"]
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
            repository_name=arguments.get("repository_name"),
            iteration_path=arguments.get("iteration_path"),
            use_next_sprint=bool(arguments.get("use_next_sprint", False)),
            area_path=arguments.get("area_path"),
            assigned_to=arguments.get("assigned_to"),
            tags=arguments.get("tags"),
            parent_work_item_id=arguments.get("parent_work_item_id"),
            acceptance_criteria=arguments.get("acceptance_criteria"),
            story_points=arguments.get("story_points"),
            remaining_work_hours=arguments.get("remaining_work_hours"),
            completed_work_hours=arguments.get("completed_work_hours"),
            original_estimate_hours=arguments.get("original_estimate_hours"),
        )
    if name == "find_similar_technical_debts":
        return _find_similar_technical_debts(
            repository_name=arguments["repository_name"],
            title=arguments["title"],
            description=arguments.get("description"),
            limit=int(arguments.get("limit", 5) or 5),
        )
    if name == "update_work_item_content":
        return _update_work_item_content(
            work_item_id=arguments["work_item_id"],
            description=arguments.get("description"),
            acceptance_criteria=arguments.get("acceptance_criteria"),
            comment=arguments.get("comment"),
        )
    if name == "update_work_item_effort":
        return _update_work_item_effort(
            work_item_id=arguments["work_item_id"],
            story_points=arguments.get("story_points"),
            remaining_work_hours=arguments.get("remaining_work_hours"),
            completed_work_hours=arguments.get("completed_work_hours"),
            original_estimate_hours=arguments.get("original_estimate_hours"),
            comment=arguments.get("comment"),
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


def _split_inline_numbered_items(line: str) -> list[str]:
    stripped_line = str(line or "").strip()
    if not stripped_line:
        return []

    if not re.search(r"(?:^|\s)1\.\s", stripped_line):
        return [stripped_line]

    numbered_matches = list(re.finditer(r"(?:^|\s)(\d+)\.\s+", stripped_line))
    if len(numbered_matches) < 2:
        return [stripped_line]

    items: list[str] = []
    for index, match in enumerate(numbered_matches):
        content_start = match.end()
        content_end = numbered_matches[index + 1].start() if index + 1 < len(numbered_matches) else len(stripped_line)
        content = stripped_line[content_start:content_end].strip(" ;")
        if content:
            items.append(f"{match.group(1)}. {content}")

    return items or [stripped_line]


def _apply_inline_code_markup(value: str) -> str:
    return re.sub(r"`([^`]+)`", lambda match: f"<code>{escape(match.group(1), quote=False)}</code>", value)


def _normalize_markdown_lines(value: str | None) -> list[str]:
    if not value:
        return []

    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    expanded_lines: list[str] = []
    for raw_line in normalized.split("\n"):
        split_lines = _split_inline_numbered_items(raw_line)
        expanded_lines.extend(split_lines or [raw_line])
    return expanded_lines


def _render_lines_as_azure_html(lines: list[str]) -> str:
    html_parts: list[str] = []
    paragraph_lines: list[str] = []
    list_type: str | None = None
    list_items: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        paragraph = " ".join(part.strip() for part in paragraph_lines if part.strip())
        paragraph_lines = []
        if paragraph:
            html_parts.append(f"<p>{_apply_inline_code_markup(escape(paragraph, quote=False))}</p>")

    def flush_list() -> None:
        nonlocal list_type, list_items
        if not list_type or not list_items:
            list_type = None
            list_items = []
            return
        items_html = "".join(
            f"<li>{_apply_inline_code_markup(escape(item.strip(), quote=False))}</li>" for item in list_items if item.strip()
        )
        if items_html:
            html_parts.append(f"<{list_type}>{items_html}</{list_type}>")
        list_type = None
        list_items = []

    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue

        heading_match = re.fullmatch(r"#{1,6}\s*(.+?)\s*", line)
        ordered_match = re.fullmatch(r"(\d+)\.\s+(.+)", line)
        unordered_match = re.fullmatch(r"[-*]\s+(.+)", line)

        if heading_match:
            flush_paragraph()
            flush_list()
            heading_text = heading_match.group(1).strip().rstrip(":")
            html_parts.append(f"<p><strong>{_apply_inline_code_markup(escape(heading_text, quote=False))}</strong></p>")
            continue

        if line.endswith(":") and not ordered_match and not unordered_match:
            flush_paragraph()
            flush_list()
            html_parts.append(f"<p><strong>{_apply_inline_code_markup(escape(line[:-1].strip(), quote=False))}</strong></p>")
            continue

        if ordered_match:
            flush_paragraph()
            if list_type != "ol":
                flush_list()
                list_type = "ol"
            list_items.append(ordered_match.group(2).strip())
            continue

        if unordered_match:
            flush_paragraph()
            if list_type != "ul":
                flush_list()
                list_type = "ul"
            list_items.append(unordered_match.group(1).strip())
            continue

        flush_list()
        paragraph_lines.append(line)

    flush_paragraph()
    flush_list()
    return "".join(html_parts)


def _format_azure_rich_text(value: str | None) -> str | None:
    lines = _normalize_markdown_lines(value)
    if not lines:
        return None

    html_value = _render_lines_as_azure_html(lines)
    return html_value or None


def _build_browser_work_item_url(work_item_id: int | None) -> str | None:
    if work_item_id is None:
        return None
    org_url = os.environ["AZURE_ORG_URL"].rstrip("/")
    project = quote(get_project(), safe="")
    return f"{org_url}/{project}/_workitems/edit/{work_item_id}"


def _normalize_repository_key(repository_name: str | None) -> str:
    normalized_name = _normalize_lookup_key(repository_name)
    return normalized_name.replace(" ", "_")


def _build_repository_aliases(repository_name: str | None) -> set[str]:
    normalized_repository_name = _normalize_lookup_key(repository_name)
    if not normalized_repository_name:
        return set()

    aliases = {
        normalized_repository_name,
        normalized_repository_name.replace(" ", "_"),
        normalized_repository_name.replace(" ", ""),
        normalized_repository_name.replace(" ", "-"),
    }
    return {alias for alias in aliases if alias}


def _get_technical_debt_context_path(repository_name: str) -> Path:
    context_file_name = f"{_normalize_repository_key(repository_name)}.json"
    return TECHNICAL_DEBT_CONTEXT_DIR / context_file_name


def _load_repo_technical_debt_context(repository_name: str) -> dict[str, Any]:
    context_path = _get_technical_debt_context_path(repository_name)
    if not context_path.exists():
        return {
            "repository_name": repository_name,
            "updated_at": None,
            "technical_debts": [],
        }

    try:
        return json.loads(context_path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "repository_name": repository_name,
            "updated_at": None,
            "technical_debts": [],
        }


def _save_repo_technical_debt_context(repository_name: str, technical_debts: list[dict[str, Any]]) -> str:
    TECHNICAL_DEBT_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    context_payload = {
        "repository_name": repository_name,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "technical_debts": technical_debts,
    }
    context_path = _get_technical_debt_context_path(repository_name)
    context_path.write_text(_format_json(context_payload) + "\n", encoding="utf-8")
    return str(context_path)


def _infer_repository_name(repository_name: str | None, title: str | None, description: str | None) -> str | None:
    normalized_repository_name = str(repository_name or "").strip()
    if normalized_repository_name:
        return normalized_repository_name

    normalized_title = str(title or "")
    title_match = re.search(r"\[\s*([A-Za-z0-9_\- ]+)\s*\]", normalized_title)
    if title_match:
        candidate = _normalize_repository_key(title_match.group(1))
        if candidate:
            return candidate

    search_space = _normalize_lookup_key(" ".join(part for part in [title or "", description or ""] if part))
    for repo_candidate in ("kl_store", "portal_backend", "portal_frontend", "admin_backend", "admin_frontend"):
        if _normalize_lookup_key(repo_candidate).replace(" ", "") in search_space.replace(" ", ""):
            return repo_candidate

    return None


def _is_technical_debt_type(work_item_type: str | None) -> bool:
    return _normalize_lookup_key(work_item_type) in {
        "technical debt",
        "technical debts",
        "tech debt",
        "tech debts",
        "debito tecnico",
        "debitos tecnicos",
    }


def _get_work_items_in_batches(wit: Any, ids: list[str], fields: list[str], batch_size: int = 75) -> list[Any]:
    if not ids:
        return []

    items: list[Any] = []
    for start_index in range(0, len(ids), batch_size):
        batch_ids = ids[start_index:start_index + batch_size]
        items.extend(wit.get_work_items(ids=batch_ids, fields=fields))
    return items


def _query_technical_debt_candidates() -> list[dict[str, Any]]:
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    query = Wiql(query="""
        SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType],
               [System.IterationPath], [System.AreaPath], [System.ChangedDate], [System.Tags]
        FROM WorkItems
        WHERE [System.WorkItemType] IN ('Technical Debt', 'Technical debt', 'Tech Debt')
          AND [System.State] <> 'Removed'
        ORDER BY [System.ChangedDate] DESC
    """)

    result = wit.query_by_wiql(query, team_context=get_team_context())
    if not result.work_items:
        return []

    ids = [str(work_item.id) for work_item in result.work_items]
    items = _get_work_items_in_batches(wit, ids, fields=[
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.IterationPath",
        "System.AreaPath",
        "System.ChangedDate",
        "System.Tags",
        "System.Description",
    ])

    candidates: list[dict[str, Any]] = []
    for item in items:
        fields = item.fields or {}
        work_item_id = fields.get("System.Id", getattr(item, "id", None))
        candidates.append(
            {
                "id": work_item_id,
                "title": fields.get("System.Title"),
                "state": fields.get("System.State"),
                "work_item_type": fields.get("System.WorkItemType"),
                "iteration_path": fields.get("System.IterationPath"),
                "area_path": fields.get("System.AreaPath"),
                "changed_at": _serialize_datetime(fields.get("System.ChangedDate")),
                "tags": fields.get("System.Tags", ""),
                "description": _strip_html(fields.get("System.Description")),
                "url": _build_browser_work_item_url(work_item_id),
            }
        )
    return candidates


def _tokenize_similarity_text(value: str | None, repository_name: str | None = None) -> set[str]:
    normalized_value = _normalize_lookup_key(value)
    if not normalized_value:
        return set()

    repository_alias_tokens: set[str] = set()
    for alias in _build_repository_aliases(repository_name):
        repository_alias_tokens.update(alias.replace("_", " ").replace("-", " ").split())

    tokens = {
        token
        for token in normalized_value.split()
        if len(token) > 2 and token not in SIMILARITY_STOPWORDS and token not in repository_alias_tokens
    }
    return tokens


def _is_candidate_associated_with_repository(candidate: dict[str, Any], repository_name: str, known_ids: set[int]) -> bool:
    candidate_id = int(candidate.get("id") or 0)
    if candidate_id and candidate_id in known_ids:
        return True

    aliases = _build_repository_aliases(repository_name)
    if not aliases:
        return False

    search_space_parts = [
        str(candidate.get("title") or ""),
        str(candidate.get("description") or ""),
        str(candidate.get("tags") or ""),
        str(candidate.get("area_path") or ""),
    ]
    normalized_search_space = _normalize_lookup_key(" ".join(search_space_parts))
    collapsed_search_space = normalized_search_space.replace(" ", "")

    for alias in aliases:
        normalized_alias = _normalize_lookup_key(alias)
        collapsed_alias = normalized_alias.replace(" ", "")
        if normalized_alias and normalized_alias in normalized_search_space:
            return True
        if collapsed_alias and collapsed_alias in collapsed_search_space:
            return True

    return False


def _score_technical_debt_similarity(repository_name: str, title: str, description: str | None, candidate: dict[str, Any]) -> dict[str, Any]:
    normalized_title = _normalize_lookup_key(title)
    candidate_title = str(candidate.get("title") or "")
    normalized_candidate_title = _normalize_lookup_key(candidate_title)
    input_tokens = _tokenize_similarity_text(" ".join(part for part in [title, description or ""] if part), repository_name)
    candidate_tokens = _tokenize_similarity_text(
        " ".join(part for part in [candidate_title, str(candidate.get("description") or "")] if part),
        repository_name,
    )

    shared_tokens = sorted(input_tokens & candidate_tokens)
    union_tokens = input_tokens | candidate_tokens
    token_score = (len(shared_tokens) / len(union_tokens)) if union_tokens else 0.0
    title_ratio = SequenceMatcher(None, normalized_title, normalized_candidate_title).ratio() if normalized_title and normalized_candidate_title else 0.0
    score = max(title_ratio if shared_tokens else 0.0, (token_score * 0.7) + (title_ratio * 0.3))

    return {
        "exact_title_match": bool(normalized_title and normalized_title == normalized_candidate_title),
        "token_overlap": round(token_score, 3),
        "title_ratio": round(title_ratio, 3),
        "similarity_score": round(score, 3),
        "matched_terms": shared_tokens,
    }


def _build_technical_debt_similarity_analysis(
    repository_name: str,
    title: str,
    description: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    normalized_limit = max(1, min(int(limit or 5), 10))
    existing_context = _load_repo_technical_debt_context(repository_name)
    known_ids = {
        int(item.get("id"))
        for item in existing_context.get("technical_debts", [])
        if str(item.get("id") or "").isdigit()
    }

    candidates = _query_technical_debt_candidates()
    associated_items: list[dict[str, Any]] = []
    similar_items: list[dict[str, Any]] = []

    for candidate in candidates:
        if _is_candidate_associated_with_repository(candidate, repository_name, known_ids):
            associated_items.append(candidate)

        similarity = _score_technical_debt_similarity(repository_name, title, description, candidate)
        if similarity["exact_title_match"] or similarity["token_overlap"] >= 0.12 or similarity["similarity_score"] >= 0.55:
            similar_items.append({**candidate, **similarity})

    associated_items.sort(key=lambda item: ((item.get("state") or "") in COMPLETED_STATES, item.get("changed_at") or ""), reverse=True)
    similar_items.sort(
        key=lambda item: (
            item.get("exact_title_match", False),
            item.get("similarity_score", 0.0),
            item.get("changed_at") or "",
        ),
        reverse=True,
    )

    serialized_associated_items = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "state": item.get("state"),
            "iteration_path": item.get("iteration_path"),
            "url": item.get("url"),
        }
        for item in associated_items
    ]
    context_path = _save_repo_technical_debt_context(repository_name, serialized_associated_items)

    return {
        "repository_name": repository_name,
        "query_title": title,
        "duplicate_found": any(item.get("exact_title_match") for item in similar_items),
        "similar_found": bool(similar_items),
        "suggested_matches": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "state": item.get("state"),
                "iteration_path": item.get("iteration_path"),
                "similarity_score": item.get("similarity_score"),
                "token_overlap": item.get("token_overlap"),
                "title_ratio": item.get("title_ratio"),
                "exact_title_match": item.get("exact_title_match"),
                "matched_terms": item.get("matched_terms"),
                "url": item.get("url"),
            }
            for item in similar_items[:normalized_limit]
        ],
        "associated_repository_items": serialized_associated_items,
        "local_context_path": context_path,
    }


def _register_technical_debt_in_repo_context(repository_name: str, created_item: Any) -> str:
    current_context = _load_repo_technical_debt_context(repository_name)
    current_items = current_context.get("technical_debts", []) or []
    created_fields = getattr(created_item, "fields", {}) or {}
    created_id = created_fields.get("System.Id", getattr(created_item, "id", None))
    created_entry = {
        "id": created_id,
        "title": created_fields.get("System.Title"),
        "state": created_fields.get("System.State"),
        "iteration_path": created_fields.get("System.IterationPath"),
        "url": _build_browser_work_item_url(created_id),
    }
    merged_items = [item for item in current_items if item.get("id") != created_id]
    merged_items.insert(0, created_entry)
    return _save_repo_technical_debt_context(repository_name, merged_items)


@audit("kwikledgers.azure_devops")
def _find_similar_technical_debts(
    repository_name: str,
    title: str,
    description: str | None = None,
    limit: int = 5,
) -> str:
    normalized_repository_name = str(repository_name or "").strip()
    normalized_title = str(title or "").strip()
    if not normalized_repository_name:
        raise ValueError("repository_name e obrigatorio para analisar debitos tecnicos parecidos.")
    if not normalized_title:
        raise ValueError("title e obrigatorio para analisar debitos tecnicos parecidos.")

    return _format_json(
        _build_technical_debt_similarity_analysis(
            repository_name=normalized_repository_name,
            title=normalized_title,
            description=description,
            limit=limit,
        )
    )


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


def _extract_iteration_year(value: str | None) -> int | None:
    match = re.search(r"\\(20\d{2})(?:\\|$)", str(value or "").replace("/", "\\"))
    if not match:
        return None
    return int(match.group(1))


def _resolve_team_name() -> str | None:
    configured_team = os.environ.get("AZURE_TEAM", "").strip()
    if configured_team:
        return configured_team

    project = get_project()
    cached_team = TEAM_CONTEXT_CACHE.get(project)
    if cached_team:
        return cached_team

    connection = get_client()
    core_client = connection.clients.get_core_client()
    work_client = connection.clients.get_work_client()
    current_year = datetime.now().year
    team_candidates: list[dict[str, Any]] = []

    for team in core_client.get_teams(project_id=project):
        team_name = str(getattr(team, "name", "")).strip()
        if not team_name:
            continue

        team_context = TeamContext(project=project, team=team_name)
        try:
            current_iterations = work_client.get_team_iterations(team_context, timeframe="current")
        except Exception:
            continue

        for iteration in current_iterations or []:
            iteration_name = str(getattr(iteration, "name", "")).strip()
            iteration_path = str(getattr(iteration, "path", "")).strip()
            if not iteration_path:
                continue

            team_candidates.append(
                {
                    "team_name": team_name,
                    "iteration_name": iteration_name,
                    "iteration_path": iteration_path,
                    "iteration_year": _extract_iteration_year(iteration_path),
                    "sprint_number": _extract_sprint_number(iteration_name or iteration_path),
                }
            )

    if not team_candidates:
        return None

    selected_team = max(
        team_candidates,
        key=lambda candidate: (
            candidate["iteration_year"] == current_year,
            candidate["iteration_year"] or 0,
            candidate["sprint_number"] or -1,
            _is_plain_sprint_name(candidate["iteration_name"]),
            candidate["iteration_path"],
            candidate["team_name"],
        ),
    )
    TEAM_CONTEXT_CACHE[project] = selected_team["team_name"]
    return selected_team["team_name"]


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


def _get_all_iterations_context() -> list[dict[str, Any]]:
    connection = get_client()
    work_client = connection.clients.get_work_client()
    iterations = work_client.get_team_iterations(get_team_context())
    serialized_iterations = [_serialize_iteration(iteration) for iteration in iterations]
    serialized_iterations.sort(
        key=lambda iteration: (
            iteration["start_date"] is None,
            iteration["start_date"] or "",
            iteration["path"] or "",
        )
    )
    return serialized_iterations


def _get_future_iterations_context(include_undated: bool = False) -> list[dict[str, Any]]:
    future_iterations = [
        iteration
        for iteration in _get_all_iterations_context()
        if str(iteration.get("timeframe") or "").strip().lower() == "future"
    ]

    if include_undated:
        return future_iterations

    return [
        iteration
        for iteration in future_iterations
        if iteration.get("start_date") or iteration.get("finish_date")
    ]


def _normalize_classification_iteration_path(path: str | None) -> str | None:
    normalized_path = str(path or "").strip().replace("/", "\\")
    if not normalized_path:
        return None

    if normalized_path.startswith("\\"):
        normalized_path = normalized_path[1:]

    iteration_segment = "\\Iteration\\"
    if iteration_segment in normalized_path:
        normalized_path = normalized_path.replace(iteration_segment, "\\", 1)
    elif normalized_path.endswith("\\Iteration"):
        normalized_path = normalized_path[: -len("\\Iteration")]

    return normalized_path or None


def _extract_sprint_number(value: str | None) -> int | None:
    match = re.search(r"\bsprint\s*0*(\d+)\b", str(value or ""), flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _is_plain_sprint_name(value: str | None) -> bool:
    return bool(re.fullmatch(r"sprint\s*0*\d+", str(value or "").strip(), flags=re.IGNORECASE))


def _get_current_year_sprint_contexts() -> list[dict[str, Any]]:
    current_year = datetime.now().year
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    root = wit.get_classification_node(project=get_project(), structure_group="iterations", depth=4)

    current_year_node = next(
        (
            child
            for child in getattr(root, "children", None) or []
            if str(getattr(child, "name", "")).strip() == str(current_year)
        ),
        None,
    )

    if current_year_node is None:
        available_years = ", ".join(
            str(getattr(child, "name", "")).strip() or "Sem nome"
            for child in getattr(root, "children", None) or []
        )
        raise ValueError(
            "Nenhum ramo de iteracao do ano atual foi encontrado no Azure DevOps. "
            f"Ano esperado: {current_year}. Ramos disponiveis: {available_years}"
        )

    sprint_candidates: list[dict[str, Any]] = []
    for child in getattr(current_year_node, "children", None) or []:
        sprint_number = _extract_sprint_number(getattr(child, "name", None))
        normalized_path = _normalize_classification_iteration_path(getattr(child, "path", None))
        if sprint_number is None or not normalized_path:
            continue

        attributes = getattr(child, "attributes", None)
        sprint_candidates.append(
            {
                "name": getattr(child, "name", None),
                "path": normalized_path,
                "start_date": _serialize_datetime(getattr(attributes, "start_date", None)),
                "finish_date": _serialize_datetime(getattr(attributes, "finish_date", None)),
                "timeframe": "future",
                "year": current_year,
                "sprint_number": sprint_number,
                "resolution_strategy": "current_year_highest_sprint",
            }
        )

    if not sprint_candidates:
        raise ValueError(
            "Nenhuma sprint numerada foi encontrada no ramo de iteracoes do ano atual no Azure DevOps. "
            f"Ano esperado: {current_year}"
        )

    sprint_candidates.sort(
        key=lambda candidate: (
            candidate["sprint_number"],
            _is_plain_sprint_name(candidate.get("name")),
            candidate["path"],
        )
    )
    return sprint_candidates


def _get_current_year_highest_sprint_context() -> dict[str, Any]:
    return _get_current_year_sprint_contexts()[-1]


def _get_next_sprint_from_current_context() -> dict[str, Any]:
    current_sprint_context = _get_current_sprint_context()
    current_sprint_number = _extract_sprint_number(current_sprint_context.get("name") or current_sprint_context.get("path"))
    if current_sprint_number is None:
        raise ValueError(
            "Nao foi possivel identificar o numero da sprint atual para resolver a proxima sprint. "
            f"Sprint atual retornada: {current_sprint_context.get('path') or current_sprint_context.get('name') or 'sem identificador'}"
        )

    next_sprint_number = current_sprint_number + 1
    sprint_candidates = _get_current_year_sprint_contexts()
    matching_candidates = [
        candidate for candidate in sprint_candidates if candidate["sprint_number"] == next_sprint_number
    ]
    next_sprint = None
    if matching_candidates:
        next_sprint = max(
            matching_candidates,
            key=lambda candidate: (
                _is_plain_sprint_name(candidate.get("name")),
                candidate.get("path") or "",
            ),
        )
    if next_sprint is None:
        available_sprints = ", ".join(
            str(candidate.get("name") or candidate.get("path") or "Sem identificador")
            for candidate in sprint_candidates
        )
        raise ValueError(
            "Nao foi encontrada uma sprint correspondente a sprint atual + 1 no ramo do ano atual. "
            f"Sprint atual: {current_sprint_context.get('path') or current_sprint_context.get('name')}. "
            f"Esperada: Sprint {next_sprint_number}. Disponiveis: {available_sprints}"
        )

    next_sprint["resolution_strategy"] = "current_sprint_plus_one"
    next_sprint["based_on_current_sprint"] = current_sprint_context.get("path") or current_sprint_context.get("name")
    return next_sprint


def _get_next_sprint_context() -> dict[str, Any]:
    next_sprint = _get_next_sprint_from_current_context()
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


def _normalize_lookup_key(value: str | None) -> str:
    normalized_value = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized_value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def _get_available_work_item_types(wit: Any) -> dict[str, str]:
    project = get_project()
    cached_types = WORK_ITEM_TYPE_CACHE.get(project)
    if cached_types is not None:
        return cached_types

    available_types: dict[str, str] = {}
    for work_item_type in wit.get_work_item_types(project):
        type_name = str(getattr(work_item_type, "name", "")).strip()
        if not type_name:
            continue
        available_types[_normalize_lookup_key(type_name)] = type_name

    WORK_ITEM_TYPE_CACHE[project] = available_types
    return available_types


def _resolve_work_item_type_name(wit: Any, requested_work_item_type: str) -> str:
    normalized_requested_type = _normalize_lookup_key(requested_work_item_type)
    if not normalized_requested_type:
        raise ValueError("work_item_type e obrigatorio para criar um work item.")

    available_types = _get_available_work_item_types(wit)

    direct_match = available_types.get(normalized_requested_type)
    if direct_match:
        return direct_match

    for alias_candidate in WORK_ITEM_TYPE_ALIASES.get(normalized_requested_type, ()):
        resolved_candidate = available_types.get(_normalize_lookup_key(alias_candidate))
        if resolved_candidate:
            return resolved_candidate

    supported_type_names = sorted(set(available_types.values()))
    raise ValueError(
        "Tipo de work item nao encontrado neste projeto. "
        f"Recebido: {requested_work_item_type}. Disponiveis: {', '.join(supported_type_names)}"
    )


def _extract_reference_name(field_definition: Any) -> str | None:
    reference_name = getattr(field_definition, "reference_name", None)
    if isinstance(reference_name, str) and reference_name.strip():
        return reference_name.strip()

    field_reference = getattr(field_definition, "field", None)
    reference_name = getattr(field_reference, "reference_name", None)
    if isinstance(reference_name, str) and reference_name.strip():
        return reference_name.strip()

    return None


def _get_supported_work_item_fields(wit: Any, work_item_type: str) -> set[str]:
    project = get_project()
    cache_key = (project, work_item_type)
    cached_fields = WORK_ITEM_FIELD_CACHE.get(cache_key)
    if cached_fields is not None:
        return cached_fields

    supported_fields: set[str] = set()
    try:
        field_definitions = wit.get_work_item_type_fields_with_references(project, work_item_type)
    except Exception:
        field_definitions = []

    for field_definition in field_definitions or []:
        reference_name = _extract_reference_name(field_definition)
        if reference_name:
            supported_fields.add(reference_name)

    if not supported_fields:
        supported_fields = set(WORK_ITEM_FIELD_ALIASES)

    WORK_ITEM_FIELD_CACHE[cache_key] = supported_fields
    return supported_fields


def _append_supported_work_item_field_patch(
    document: list[dict[str, Any]],
    supported_fields: set[str],
    field_name: str,
    value: Any,
    applied_fields: list[str],
    skipped_fields: list[str],
) -> None:
    if value is None:
        return

    normalized_value = value.strip() if isinstance(value, str) else value
    if normalized_value == "":
        return

    logical_field_name = WORK_ITEM_FIELD_ALIASES.get(field_name, field_name)
    if field_name not in supported_fields:
        skipped_fields.append(logical_field_name)
        return

    _append_work_item_field_patch(document, field_name, normalized_value)
    applied_fields.append(logical_field_name)


def _build_work_item_relation_url(work_item_id: int) -> str:
    return f"{os.environ['AZURE_ORG_URL'].rstrip('/')}/_apis/wit/workItems/{work_item_id}"


def _append_parent_relation_patch(document: list[dict[str, Any]], parent_work_item_id: int | None) -> None:
    if parent_work_item_id is None:
        return

    document.append(
        {
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": _build_work_item_relation_url(parent_work_item_id),
            },
        }
    )


def _build_effort_payload(
    story_points: float | None = None,
    remaining_work_hours: float | None = None,
    completed_work_hours: float | None = None,
    original_estimate_hours: float | None = None,
) -> dict[str, float]:
    payload: dict[str, float] = {}
    if story_points is not None:
        payload["story_points"] = story_points
    if remaining_work_hours is not None:
        payload["remaining_work_hours"] = remaining_work_hours
    if completed_work_hours is not None:
        payload["completed_work_hours"] = completed_work_hours
    if original_estimate_hours is not None:
        payload["original_estimate_hours"] = original_estimate_hours
    return payload


def _serialize_work_item_payload(item: Any, extra_payload: Optional[dict[str, Any]] = None) -> str:
    fields = getattr(item, "fields", {}) or {}
    payload = {
        "id": fields.get("System.Id", getattr(item, "id", None)),
        "title": fields.get("System.Title"),
        "work_item_type": fields.get("System.WorkItemType"),
        "state": fields.get("System.State"),
        "assigned_to": _serialize_assigned_to(fields.get("System.AssignedTo")),
        "iteration_path": fields.get("System.IterationPath"),
        "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints"),
        "remaining_work_hours": fields.get("Microsoft.VSTS.Scheduling.RemainingWork"),
        "completed_work_hours": fields.get("Microsoft.VSTS.Scheduling.CompletedWork"),
        "original_estimate_hours": fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate"),
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
    repository_name: str | None = None,
    iteration_path: str | None = None,
    use_next_sprint: bool = False,
    area_path: str | None = None,
    assigned_to: str | None = None,
    tags: str | None = None,
    parent_work_item_id: int | None = None,
    acceptance_criteria: str | None = None,
    story_points: float | None = None,
    remaining_work_hours: float | None = None,
    completed_work_hours: float | None = None,
    original_estimate_hours: float | None = None,
) -> str:
    normalized_work_item_type = str(work_item_type or "").strip()
    normalized_title = str(title or "").strip()

    if not normalized_work_item_type:
        raise ValueError("work_item_type e obrigatorio para criar um work item.")
    if not normalized_title:
        raise ValueError("title e obrigatorio para criar um work item.")

    resolved_repository_name = _infer_repository_name(repository_name, normalized_title, description)
    formatted_description = _format_azure_rich_text(description)
    formatted_acceptance_criteria = _format_azure_rich_text(acceptance_criteria)

    next_sprint_context = None
    target_iteration_path = str(iteration_path or "").strip()
    if not target_iteration_path and use_next_sprint:
        next_sprint_context = _get_next_sprint_context()
        target_iteration_path = str(next_sprint_context.get("path") or "").strip()

    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    resolved_work_item_type = _resolve_work_item_type_name(wit, normalized_work_item_type)
    supported_fields = _get_supported_work_item_fields(wit, resolved_work_item_type)

    document: list[dict[str, Any]] = []
    applied_fields: list[str] = []
    skipped_fields: list[str] = []
    _append_work_item_field_patch(document, "System.Title", normalized_title)
    _append_work_item_field_patch(document, "System.Description", formatted_description)
    _append_work_item_field_patch(document, "System.IterationPath", target_iteration_path)
    _append_work_item_field_patch(document, "System.AreaPath", area_path)
    _append_work_item_field_patch(document, "System.AssignedTo", assigned_to)
    _append_work_item_field_patch(document, "System.Tags", tags)
    _append_parent_relation_patch(document, parent_work_item_id)
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        formatted_acceptance_criteria,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.StoryPoints",
        story_points,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.RemainingWork",
        remaining_work_hours,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.CompletedWork",
        completed_work_hours,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.OriginalEstimate",
        original_estimate_hours,
        applied_fields,
        skipped_fields,
    )

    created_item = wit.create_work_item(
        document=document,
        project=get_project(),
        type=resolved_work_item_type,
    )

    local_context_path = None
    similarity_analysis = None
    if _is_technical_debt_type(resolved_work_item_type) and resolved_repository_name:
        local_context_path = _register_technical_debt_in_repo_context(resolved_repository_name, created_item)
        similarity_analysis = _build_technical_debt_similarity_analysis(
            repository_name=resolved_repository_name,
            title=normalized_title,
            description=description,
            limit=5,
        )

    return _serialize_work_item_payload(
        created_item,
        {
            "requested_work_item_type": normalized_work_item_type,
            "resolved_work_item_type": resolved_work_item_type,
            "repository_name": resolved_repository_name,
            "requested_iteration_path": str(iteration_path or "").strip() or None,
            "resolved_iteration_path": target_iteration_path or None,
            "used_next_sprint": bool(next_sprint_context),
            "next_sprint_context": next_sprint_context,
            "parent_work_item_id": parent_work_item_id,
            "requested_effort": _build_effort_payload(
                story_points=story_points,
                remaining_work_hours=remaining_work_hours,
                completed_work_hours=completed_work_hours,
                original_estimate_hours=original_estimate_hours,
            ),
            "applied_fields": applied_fields,
            "skipped_fields": skipped_fields,
            "local_context_path": local_context_path,
            "similarity_analysis": similarity_analysis,
        },
    )


@audit("kwikledgers.azure_devops")
def _update_work_item_content(
    work_item_id: int,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    comment: str | None = None,
) -> str:
    normalized_comment = str(comment or "").strip()
    formatted_description = _format_azure_rich_text(description) if description is not None else None
    formatted_acceptance_criteria = (
        _format_azure_rich_text(acceptance_criteria) if acceptance_criteria is not None else None
    )

    if description is None and acceptance_criteria is None and not normalized_comment:
        raise ValueError(
            "Informe description, acceptance_criteria ou comment para atualizar o conteudo do work item."
        )

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
    work_item_type = str(current_item.fields.get("System.WorkItemType") or "").strip()
    supported_fields = _get_supported_work_item_fields(wit, work_item_type)

    document: list[dict[str, Any]] = []
    applied_fields: list[str] = []
    skipped_fields: list[str] = []
    requested_fields: list[str] = []

    if description is not None:
        requested_fields.append("description")
        _append_work_item_field_patch(document, "System.Description", formatted_description)
        if formatted_description:
            applied_fields.append("description")

    if acceptance_criteria is not None:
        requested_fields.append("acceptance_criteria")
        _append_supported_work_item_field_patch(
            document,
            supported_fields,
            "Microsoft.VSTS.Common.AcceptanceCriteria",
            formatted_acceptance_criteria,
            applied_fields,
            skipped_fields,
        )

    if normalized_comment:
        document.append({"op": "add", "path": "/fields/System.History", "value": normalized_comment})

    if not document:
        raise ValueError(
            "Nenhum campo de conteudo resultou em atualizacao. "
            f"Campos solicitados: {', '.join(requested_fields) or 'nenhum'}. "
            f"Campos ignorados: {', '.join(skipped_fields) or 'nenhum'}"
        )

    updated_item = wit.update_work_item(document=document, id=work_item_id)
    return _serialize_work_item_payload(
        updated_item,
        {
            "requested_fields": requested_fields,
            "applied_fields": applied_fields,
            "skipped_fields": skipped_fields,
            "comment_added": bool(normalized_comment),
            "description_format": "azure_simple_html" if description is not None else None,
            "acceptance_criteria_format": "azure_simple_html" if acceptance_criteria is not None else None,
        },
    )


@audit("kwikledgers.azure_devops")
def _update_work_item_effort(
    work_item_id: int,
    story_points: float | None = None,
    remaining_work_hours: float | None = None,
    completed_work_hours: float | None = None,
    original_estimate_hours: float | None = None,
    comment: str | None = None,
) -> str:
    requested_effort = _build_effort_payload(
        story_points=story_points,
        remaining_work_hours=remaining_work_hours,
        completed_work_hours=completed_work_hours,
        original_estimate_hours=original_estimate_hours,
    )
    normalized_comment = str(comment or "").strip()

    if not requested_effort:
        raise ValueError(
            "Informe ao menos um campo de esforco para atualizar: story_points, "
            "remaining_work_hours, completed_work_hours ou original_estimate_hours."
        )

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
    work_item_type = str(current_item.fields.get("System.WorkItemType") or "").strip()
    supported_fields = _get_supported_work_item_fields(wit, work_item_type)

    document: list[dict[str, Any]] = []
    applied_fields: list[str] = []
    skipped_fields: list[str] = []
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.StoryPoints",
        story_points,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.RemainingWork",
        remaining_work_hours,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.CompletedWork",
        completed_work_hours,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.OriginalEstimate",
        original_estimate_hours,
        applied_fields,
        skipped_fields,
    )

    if normalized_comment:
        document.append({"op": "add", "path": "/fields/System.History", "value": normalized_comment})

    if not document:
        raise ValueError(
            f"Nenhum dos campos solicitados e suportado pelo tipo {work_item_type}. "
            f"Campos ignorados: {', '.join(skipped_fields) or 'nenhum'}"
        )

    updated_item = wit.update_work_item(document=document, id=work_item_id)
    return _serialize_work_item_payload(
        updated_item,
        {
            "requested_effort": requested_effort,
            "applied_fields": applied_fields,
            "skipped_fields": skipped_fields,
            "comment_added": bool(normalized_comment),
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
