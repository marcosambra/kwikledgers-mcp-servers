# KwikLedgers MCP Servers

Repository name: `kwikledgers-mcp-servers`

Folder: `/home/ambra/Kwikledgers/agent/mcp_servers`

Description: shared Python MCP runtime for the KwikLedgers agent, including Azure DevOps access, local sprint tracking, audit logging, environment discovery, and optional Windows notification support.

## Purpose

This repository owns the MCP execution layer used by the agent. It is the best boundary for the server code because the servers share runtime packaging and utility modules.

Current components:

- `azure_devops/`: Azure DevOps queries and updates
- `local_tracking/`: AI tracking files, sprint snapshots, metrics, and logs
- `windows_calendar/`: notifications and calendar integration
- `utils/`: shared logger and environment discovery helpers
- `tests/`: focused MCP regression coverage
- `.venv/`: local development virtualenv, ignored

## Why Keep This As One Repo

The MCP servers currently share:

- `utils/env.py`
- `utils/logger.py`
- shared Python dependency management via `pyproject.toml`
- a common virtualenv bootstrap model

Splitting each MCP server into its own repository now would force an additional shared package or duplicated utility code.

## Recommended Structure

```text
mcp_servers/
  pyproject.toml
  poetry.lock
  azure_devops/
  local_tracking/
  windows_calendar/
  utils/
```

## Runtime Model

Each server launcher:

- locates the nearest `.env` in a parent folder
- uses the shared local virtualenv under `.venv/`
- validates that the shared runtime was prepared by `agent/setup.sh` or `agent/setup.ps1`
- checks a per-server requirements stamp before startup instead of installing dependencies on demand

This supports a future where the server folders are split into submodules while still reading configuration from the parent `agent/` repo.

## Server Responsibilities

- `azure_devops/`: daily summary, work-item reads, PR context, and controlled Azure write actions
- `local_tracking/`: `Task_Control`, `Daily_Action_Logs`, `Metrics`, and tracking snapshot reads under `AI_Tracking/`
- `windows_calendar/`: toast notifications, Outlook event creation, calendar reads, and reminder scheduling from Windows or WSL

The local tracking server is designed so the agent can refresh only the sprint
snapshot when needed or persist the full daily operational bundle in one call.

## Windows Bridge Notes

The Windows MCP uses an encoded PowerShell bridge so WSL calls do not depend on
fragile shell quoting. Current behavior:

- notifications are sent through `powershell.exe` when the agent runs inside WSL
- Outlook access retries transient COM rejections before failing
- notification and Outlook operations use separate timeout knobs from `agent/.env`

Relevant environment variables:

- `KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS`
- `KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS`

## Validation

Focused regression tests live under `tests/`, including coverage for the
Windows calendar bridge and PowerShell invocation behavior.

## Recommended License Structure

For internal use:

```text
LICENSE.md
README.md
SECURITY.md
CONTRIBUTING.md
```

Suggested license label:

- `Proprietary - Internal Use Only`

## First Files To Keep Versioned

- `pyproject.toml`
- `poetry.lock`
- `azure_devops/server.py`
- `local_tracking/server.py`
- `tests/test_windows_calendar.py`
- `windows_calendar/server.py`
- `windows_calendar/run.sh`
- `utils/env.py`
- `utils/logger.py`
