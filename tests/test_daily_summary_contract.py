import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
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


azure_server = _load_module("azure_daily_summary_server", "azure_devops/server.py")
tracking_server = _load_module("local_tracking_server_for_summary_tests", "local_tracking/server.py")


class _FakeGitClient:
    def get_repositories(self, project=None):
        return []


class _FakeClients:
    def get_git_client(self):
        return _FakeGitClient()


class _FakeConnection:
    clients = _FakeClients()


class DailySummaryContractTests(unittest.TestCase):
    def test_count_items_by_state_sorts_and_counts_states(self):
        breakdown = azure_server._count_items_by_state([
            {"state": "Closed"},
            {"state": "Active"},
            {"state": "Closed"},
        ])

        self.assertEqual(breakdown, {"Active": 1, "Closed": 2})

    def test_is_blocked_item_accepts_compound_blocked_states(self):
        self.assertTrue(azure_server._is_blocked_item({"System.State": "Blocked Desenvolvimento"}))
        self.assertTrue(azure_server._is_blocked_item({"System.State": "Bloqueado QA"}))
        self.assertTrue(azure_server._is_blocked_item({"System.BoardColumn": "Desenvolvimento Blocked"}))
        self.assertFalse(azure_server._is_blocked_item({"System.State": "Desenvolvimento", "System.Tags": "normal"}))

    def test_build_daily_summary_counts_all_assigned_item_types(self):
        assigned_items = [
            {
                "id": 1,
                "title": "Story",
                "state": "Active",
                "work_item_type": "User Story",
                "story_points": 3,
                "remaining_work_hours": 2,
                "is_blocked": False,
            },
            {
                "id": 2,
                "title": "Debt",
                "state": "Active",
                "work_item_type": "Technical Debt",
                "story_points": 5,
                "remaining_work_hours": 1,
                "is_blocked": False,
            },
            {
                "id": 3,
                "title": "Spike",
                "state": "Blocked",
                "work_item_type": "Spike",
                "story_points": 2,
                "remaining_work_hours": 4,
                "is_blocked": True,
            },
            {
                "id": 4,
                "title": "History",
                "state": "Committed",
                "work_item_type": "History",
                "story_points": 1,
                "remaining_work_hours": 3,
                "is_blocked": False,
            },
            {
                "id": 5,
                "title": "Closed task",
                "state": "Closed",
                "work_item_type": "Task",
                "story_points": 0,
                "remaining_work_hours": 0,
                "is_blocked": False,
            },
        ]

        with (
            patch.object(azure_server, "_query_assigned_items", return_value=assigned_items),
            patch.object(
                azure_server,
                "_query_child_items_summary",
                return_value={
                    "total_children": 3,
                    "open_children": 2,
                    "blocked_children": 1,
                    "completed_children": 1,
                    "children_by_type": {"Task": 2, "Bug": 1},
                    "parents_with_children": 2,
                    "children_by_parent": [
                        {
                            "parent_id": 3,
                            "parent_title": "Spike",
                            "child_count": 2,
                            "open_child_count": 2,
                            "blocked_child_count": 1,
                            "children": [],
                        }
                    ],
                },
            ),
            patch.object(azure_server, "_query_sprint_stories", return_value=[]),
            patch.object(
                azure_server,
                "_build_project_progress",
                return_value={
                    "remaining_story_points": 34,
                    "total_stories": 0,
                    "completed_stories": 0,
                    "active_stories": 0,
                    "blocked_stories": 0,
                    "unassigned_stories": 0,
                    "unestimated_stories": 0,
                    "active_unestimated_stories": 0,
                    "total_story_points": 0,
                    "completed_story_points": 0,
                    "blocked_story_points": 0,
                    "progress_percent_by_story_count": 0,
                    "progress_percent_by_story_points": 0,
                    "progress_warning": None,
                    "state_breakdown": {},
                },
            ),
            patch.object(azure_server, "_get_current_sprint_context", return_value={"name": "Sprint 13"}),
            patch.object(azure_server, "get_client", return_value=_FakeConnection()),
            patch.object(azure_server, "get_project", return_value="Kwik Ledgers"),
        ):
            payload = azure_server._build_daily_summary("dev@kwikledgers.test")

        self.assertEqual(payload["counts"]["assigned_items"], 5)
        self.assertEqual(payload["counts"]["blocked_items"], 1)
        self.assertEqual(payload["counts"]["user_stories"], 1)
        self.assertEqual(payload["counts"]["tasks_all_statuses"], 1)
        self.assertEqual(payload["counts"]["tasks_by_state"], {"Closed": 1})
        self.assertEqual(
            payload["counts"]["assigned_items_by_type"],
            {"History": 1, "Spike": 1, "Task": 1, "Technical Debt": 1, "User Story": 1},
        )
        self.assertEqual(payload["counts"]["children_of_assigned_items"], 3)
        self.assertEqual(payload["counts"]["blocked_children_of_assigned_items"], 1)
        self.assertEqual(payload["child_items_summary"]["children_by_type"], {"Task": 2, "Bug": 1})
        self.assertEqual(payload["assigned_story_points"], 11.0)
        self.assertEqual(payload["remaining_work_hours"], 10.0)

    def test_update_task_control_renders_assigned_type_breakdown(self):
        payload = {
            "generated_at": "2026-07-02T12:00:00Z",
            "project": "Kwik Ledgers",
            "user_email": "dev@kwikledgers.test",
            "counts": {
                "assigned_items": 6,
                "blocked_items": 1,
                "open_prs": 0,
                "user_stories": 1,
                "tasks_all_statuses": 2,
                "tasks_by_state": {
                    "Closed": 1,
                    "Done": 1,
                },
                "assigned_items_by_type": {
                    "User Story": 1,
                    "Task": 2,
                    "Technical Debt": 1,
                    "Spike": 1,
                    "History": 1,
                },
                "children_of_assigned_items": 3,
                "blocked_children_of_assigned_items": 1,
                "sprint_stories": 0,
            },
            "remaining_story_points": 34,
            "assigned_story_points": 11,
            "remaining_work_hours": 10,
            "sprint_context": {"name": "Sprint 13", "path": "Kwik Ledgers\\2026\\Sprint 13"},
            "project_progress": {"total_stories": 0},
            "child_items_summary": {
                "total_children": 3,
                "open_children": 2,
                "blocked_children": 1,
                "completed_children": 1,
                "children_by_type": {"Task": 2, "Bug": 1},
                "children_by_parent": [
                    {
                        "parent_id": 3,
                        "parent_title": "Spike",
                        "child_count": 2,
                        "open_child_count": 2,
                        "blocked_child_count": 1,
                    }
                ],
            },
            "sprint_stories": [],
            "items": [],
            "blocked_items": [],
            "open_prs": [],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with (
                patch.object(tracking_server, "TASK_CONTROL_DIR", root / "Task_Control"),
                patch.object(tracking_server, "DAILY_LOG_DIR", root / "Daily_Action_Logs"),
                patch.object(tracking_server, "METRICS_DIR", root / "Metrics"),
            ):
                tracking_server._update_task_control(json.dumps(payload))

                markdown = (root / "Task_Control" / "current-sprint.md").read_text()

        self.assertIn("Tasks do usuario em todos os status: 2", markdown)
        self.assertIn("Tasks por status: Closed=1, Done=1", markdown)
        self.assertIn("Itens atribuídos por tipo: History=1, Spike=1, Task=2, Technical Debt=1, User Story=1", markdown)
        self.assertIn("Children dos itens atribuídos: 3", markdown)
        self.assertIn("Children bloqueados dos itens atribuídos: 1", markdown)
        self.assertIn("Pai KL-3: Spike | children=2 | abertas=2 | bloqueadas=1", markdown)
        self.assertIn("Story points atribuidos aos itens do usuario: 11", markdown)

    def test_build_default_log_details_mentions_child_insights(self):
        payload = {
            "user_email": "dev@kwikledgers.test",
            "counts": {
                "assigned_items": 2,
                "assigned_items_by_type": {"Technical Debt": 1, "User Story": 1},
                "tasks_all_statuses": 2,
                "tasks_by_state": {"Closed": 1, "Done": 1},
                "blocked_items": 1,
                "open_prs": 0,
                "sprint_stories": 1,
            },
            "child_items_summary": {
                "total_children": 4,
                "open_children": 3,
                "blocked_children": 2,
                "children_by_parent": [
                    {
                        "parent_id": 113875,
                        "parent_title": "Bug de status",
                        "child_count": 3,
                        "open_child_count": 3,
                        "blocked_child_count": 2,
                    }
                ],
            },
            "returned_items": [],
            "project_progress": {
                "total_stories": 1,
                "blocked_stories": 1,
                "blocked_story_points": 8,
                "progress_percent_by_story_count": 0,
                "progress_percent_by_story_points": 0,
            },
            "sprint_context": {"name": "Sprint 13", "goal": None},
            "remaining_story_points": 34,
        }

        details = tracking_server._build_default_log_details(payload)

        self.assertIn("Tasks do usuario em todos os status: 2, com breakdown Closed=1, Done=1.", details)
        self.assertIn("Children ligados aos itens atribuídos: 4, com 3 abertos e 2 bloqueados.", details)
        self.assertIn("Historias bloqueadas no sprint: 1, somando 8 story points bloqueados.", details)
        self.assertIn("Maior foco em children: KL-113875 tem 2 children bloqueados e 3 abertos.", details)


if __name__ == "__main__":
    unittest.main()