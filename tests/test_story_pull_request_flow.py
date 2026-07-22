import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent


def _load_module(name: str, relative_path: str):
    module_path = MCP_DIR / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


azure_server = _load_module("azure_story_pr_server", "azure_devops/server.py")


class _FakeRelation:
    def __init__(self, rel: str, url: str, name: str | None = None):
        self.rel = rel
        self.url = url
        self.attributes = {"name": name} if name else {}


class _FakeWorkItem:
    def __init__(
        self,
        work_item_id: int,
        title: str,
        state: str,
        work_item_type: str,
        iteration_path: str = "Kwik Ledgers\\2026\\Sprint 13",
        relations: list[_FakeRelation] | None = None,
    ):
        self.id = work_item_id
        self.fields = {
            "System.Id": work_item_id,
            "System.Title": title,
            "System.State": state,
            "System.WorkItemType": work_item_type,
            "System.IterationPath": iteration_path,
        }
        self.url = f"https://example.test/_apis/wit/workItems/{work_item_id}"
        self.relations = relations or []


class _FakeWorkItemTrackingClient:
    def __init__(self, items: dict[int, _FakeWorkItem]):
        self.items = items

    def get_work_item(self, id, fields=None, expand=None):
        return self.items[int(id)]

    def update_work_item(self, document, id):
        item = self.items[int(id)]
        for operation in document:
            if operation.get("path") == "/relations/-":
                value = operation["value"]
                attributes = value.get("attributes") or {}
                item.relations.append(
                    _FakeRelation(
                        rel=value["rel"],
                        url=value["url"],
                        name=attributes.get("name"),
                    )
                )
            elif operation.get("path") == "/fields/System.State":
                item.fields["System.State"] = operation["value"]
        return item


class _FakeGitClient:
    def __init__(self, repository, active_pull_requests=None):
        self.repository = repository
        self.active_pull_requests = list(active_pull_requests or [])
        self.created_pull_requests: list[dict[str, object]] = []

    def get_repositories(self, project=None):
        return [self.repository]

    def get_pull_requests(self, repository_id, search_criteria, project=None, max_comment_length=None, skip=None, top=None):
        return list(self.active_pull_requests)

    def create_pull_request(self, git_pull_request_to_create, repository_id, project=None, supports_iterations=None):
        self.created_pull_requests.append(
            {
                "repository_id": repository_id,
                "project": project,
                "supports_iterations": supports_iterations,
                "title": git_pull_request_to_create.title,
                "description": git_pull_request_to_create.description,
                "source_ref_name": git_pull_request_to_create.source_ref_name,
                "target_ref_name": git_pull_request_to_create.target_ref_name,
                "is_draft": git_pull_request_to_create.is_draft,
            }
        )
        return SimpleNamespace(
            pull_request_id=77,
            title=git_pull_request_to_create.title,
            source_ref_name=git_pull_request_to_create.source_ref_name,
            target_ref_name=git_pull_request_to_create.target_ref_name,
            is_draft=git_pull_request_to_create.is_draft,
            artifact_id="vstfs:///Git/PullRequestId/proj-1%2frepo-1%2f77",
            remote_url="https://example.test/_git/portal_backend/pullrequest/77",
        )

    def get_pull_request_work_item_refs(self, repository_id, pull_request_id, project=None):
        return [
            SimpleNamespace(id="100", url="https://example.test/_apis/wit/workItems/100"),
            SimpleNamespace(id="101", url="https://example.test/_apis/wit/workItems/101"),
        ]


class _FakeClients:
    def __init__(self, git_client, work_item_tracking_client):
        self._git_client = git_client
        self._work_item_tracking_client = work_item_tracking_client

    def get_git_client(self):
        return self._git_client

    def get_work_item_tracking_client(self):
        return self._work_item_tracking_client


class _FakeConnection:
    def __init__(self, git_client, work_item_tracking_client):
        self.clients = _FakeClients(git_client, work_item_tracking_client)


class StoryPullRequestFlowTests(unittest.TestCase):
    def test_build_branch_artifact_uri_matches_real_azure_format(self):
        artifact_uri = azure_server._build_branch_artifact_uri(
            "proj-1",
            "repo-1",
            "dbt/123-ajuste-fluxo",
        )

        self.assertEqual(
            artifact_uri,
            "vstfs:///Git/Ref/proj-1%2frepo-1%2fGBdbt%2f123-ajuste-fluxo",
        )

    def test_build_pull_request_body_markdown_appends_mermaid_and_checklist(self):
        body = azure_server._build_pull_request_body_markdown(
            what_was_changed="Ajuste do fluxo de cadastro.",
            affected_processes="- API\n- Fila de notificacao",
            expected_impacts="Cadastro validado sem regressao.",
            important_points="Manter compatibilidade com o payload atual.",
            mermaid_diagram="graph TD\nA[Inicio] --> B[Fim]",
            tests_updated=True,
            tests_unchanged=False,
            new_library_name="httpx",
            env_changes="FEATURE_FLAG=true",
            generated_migration_or_seed="20260703_add_feature_flag_table",
            new_queue_or_command="php artisan feature:sync",
        )

        self.assertIn("# O que foi modificado", body)
        self.assertIn("```mermaid", body)
        self.assertIn("graph TD", body)
        self.assertIn("- [x] Voce adicionou ou ajustou testes unitarios", body)
        self.assertIn("httpx", body)
        self.assertIn("FEATURE_FLAG=true", body)

    def test_preview_story_pull_request_returns_formal_preview(self):
        repository = SimpleNamespace(id="repo-1", name="portal_backend", project=SimpleNamespace(id="proj-1"))
        story = _FakeWorkItem(
            100,
            "Ajuste do fluxo de cadastro",
            "Active",
            "User Story",
            relations=[
                _FakeRelation(
                    "System.LinkTypes.Hierarchy-Forward",
                    "https://example.test/_apis/wit/workItems/101",
                )
            ],
        )
        child = _FakeWorkItem(101, "Task filha", "Active", "Task")
        wit = _FakeWorkItemTrackingClient({100: story, 101: child})
        git = _FakeGitClient(repository)

        with (
            patch.object(azure_server, "get_client", return_value=_FakeConnection(git, wit)),
            patch.object(azure_server, "get_project", return_value="Kwik Ledgers"),
            patch.object(
                azure_server,
                "_load_pull_request_template_context",
                return_value={
                    "found": True,
                    "path": "/workspace/projects/portal_backend/pull_request_template.md",
                    "source": "repository",
                },
            ),
        ):
            payload = json.loads(
                azure_server._preview_story_pull_request(
                    story_id=100,
                    repository_name="portal_backend",
                    source_branch="feature/KL-100-ajuste-fluxo",
                    what_was_changed="Ajustei o cadastro do usuario.",
                    affected_processes="- API",
                    expected_impacts="Fluxo de cadastro estabilizado.",
                    important_points="Sem alteracao contratual no endpoint.",
                    mermaid_diagram="graph TD\nA --> B",
                )
            )

        self.assertEqual(payload["target_branch"], "stage-pre-prod")
        self.assertTrue(payload["is_draft"])
        self.assertTrue(payload["can_open_pull_request"])
        self.assertEqual(payload["recommended_story_status_transition"]["status"], "Aguardando PR")
        self.assertEqual(payload["branch_association_preview"]["counts"]["pending_link"], 2)
        self.assertIn("```mermaid", payload["body_markdown"])

    def test_create_story_pull_request_links_branch_and_pr_for_parent_and_child(self):
        repository = SimpleNamespace(id="repo-1", name="portal_backend", project=SimpleNamespace(id="proj-1"))
        story = _FakeWorkItem(
            100,
            "Ajuste do fluxo de cadastro",
            "Active",
            "User Story",
            relations=[
                _FakeRelation(
                    "System.LinkTypes.Hierarchy-Forward",
                    "https://example.test/_apis/wit/workItems/101",
                )
            ],
        )
        child = _FakeWorkItem(101, "Task filha", "Active", "Task")
        wit = _FakeWorkItemTrackingClient({100: story, 101: child})
        git = _FakeGitClient(repository)

        with (
            patch.object(azure_server, "get_client", return_value=_FakeConnection(git, wit)),
            patch.object(azure_server, "get_project", return_value="Kwik Ledgers"),
            patch.object(
                azure_server,
                "_load_pull_request_template_context",
                return_value={
                    "found": True,
                    "path": "/workspace/projects/portal_backend/pull_request_template.md",
                    "source": "repository",
                },
            ),
        ):
            payload = json.loads(
                azure_server._create_story_pull_request(
                    story_id=100,
                    repository_name="portal_backend",
                    source_branch="feature/KL-100-ajuste-fluxo",
                    what_was_changed="Ajustei o cadastro do usuario.",
                    affected_processes="- API",
                    expected_impacts="Fluxo de cadastro estabilizado.",
                    important_points="Sem alteracao contratual no endpoint.",
                    mermaid_diagram="graph TD\nA --> B",
                )
            )

        self.assertTrue(payload["created"])
        self.assertEqual(payload["pull_request"]["pull_request_id"], 77)
        self.assertTrue(payload["pull_request"]["is_draft"])
        self.assertEqual(payload["branch_association_result"]["associated_count"], 2)
        self.assertEqual(payload["pull_request_association_result"]["associated_count"], 2)
        self.assertEqual(len(git.created_pull_requests), 1)
        created_pull_request = git.created_pull_requests[0]
        self.assertEqual(created_pull_request["source_ref_name"], "refs/heads/feature/KL-100-ajuste-fluxo")
        self.assertEqual(created_pull_request["target_ref_name"], "refs/heads/stage-pre-prod")
        self.assertTrue(created_pull_request["is_draft"])

        parent_links = azure_server._extract_artifact_links(story.relations, allowed_names={"Branch", "Pull Request"})
        child_links = azure_server._extract_artifact_links(child.relations, allowed_names={"Branch", "Pull Request"})
        self.assertTrue(any(link["name"] == "Branch" for link in parent_links))
        self.assertTrue(any(link["name"] == "Pull Request" for link in parent_links))
        self.assertTrue(any(link["name"] == "Branch" for link in child_links))
        self.assertTrue(any(link["name"] == "Pull Request" for link in child_links))


if __name__ == "__main__":
    unittest.main()