import os
from pathlib import Path


def find_env_file(start_path: Path) -> Path | None:
    explicit_env = os.environ.get("KWIKLEDGERS_ENV_FILE") or os.environ.get("MCP_ENV_FILE")
    if explicit_env:
        candidate = Path(explicit_env).expanduser()
        if candidate.exists():
            return candidate.resolve()

    resolved_start = start_path.resolve()
    search_roots = [resolved_start, *resolved_start.parents]
    for root in search_roots:
        candidate = root / ".env"
        if candidate.exists():
            return candidate

    return None


def load_env_file(start_path: Path) -> Path | None:
    env_file = find_env_file(start_path)
    if env_file is None:
        return None

    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
        return env_file
    except ImportError:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
        return env_file


def discover_workspace_root(start_path: Path) -> Path:
    explicit_root = os.environ.get("KWIKLEDGERS_ROOT_DIR")
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()

    resolved_start = start_path.resolve()
    markers = ["AGENTS.md", ".vscode", "agent", "AI_Tracking"]
    for root in [resolved_start, *resolved_start.parents]:
        if all((root / marker).exists() for marker in ["agent", ".vscode"]):
            return root
        if any((root / marker).exists() for marker in markers):
            candidate = root
            if (candidate / "agent").exists() and (candidate / ".vscode").exists():
                return candidate

    return resolved_start.parents[3]