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
- installs its own requirements before startup

This supports a future where the server folders are split into submodules while still reading configuration from the parent `agent/` repo.

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
- `windows_calendar/server.py`
- `utils/env.py`
- `utils/logger.py`
