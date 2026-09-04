from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from sec_inline_financials.errors import DiscoveryError

_CONFIG_NAMES = ("config.env", "config.txt")
_SEC_USER_AGENT = "SEC_USER_AGENT"


def _read_assignment(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise DiscoveryError(f"Could not read configuration file {path}: {exc}") from exc

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, raw_value = line.partition("=")
        if not separator or name.strip() != key:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value.strip()
    return ""


def load_sec_user_agent(*, working_directory: Path, environment: Mapping[str, str]) -> str:
    """Read only SEC_USER_AGENT, preferring the process environment over local files."""
    environment_value = environment.get(_SEC_USER_AGENT, "").strip()
    if environment_value:
        return environment_value
    for name in _CONFIG_NAMES:
        value = _read_assignment(working_directory / name, _SEC_USER_AGENT)
        if value:
            return value
    return ""
