"""Explicit host-only secret loading.

``load_openrouter_key`` is called only by the paid paths (runtime.run_pilot via
scripts/run_pilot.py, the compatibility check, the behavior judge) and by unpaid account
checks (scripts/check_provider_limit.py, and scripts/reconcile_ledger.py when a hold has a
generation id to look up). Preflight
and launch gates never call it; they use ``credential_status`` (presence and file
permissions only, never contents). The key never enters containers, prompts or logs.
"""
from pathlib import Path
import os
import stat

KEY_NAME = "OPENROUTER_API_KEY"


def _env_assignment(line, name):
    """Value assigned to ``name`` on one .env line, or None if the line is not one.

    Accepts ``NAME=value`` with an optional ``export `` prefix, single or double
    quotes (taken literally up to the closing quote) and an inline comment that
    starts with whitespace + ``#``. No expansion, substitution or escapes. Errors
    never include the value.
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export") and text[6:7] in (" ", "\t"):
        text = text[6:].lstrip()
    key, separator, value = text.partition("=")
    if not separator or key.strip() != name:
        return None
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        rest = value[end + 1:].strip() if end > 0 else None
        if end < 0 or (rest and not rest.startswith("#")):
            raise RuntimeError(f"Host .env has a malformed quoted {name} line")
        return value[1:end]
    if value.startswith("#"):  # `NAME= # comment` assigns nothing
        return ""
    for marker in (" #", "\t#"):
        if marker in value:
            value = value.split(marker, 1)[0]
    return value.strip()


def credential_status(repo_root):
    """Presence/permission facts only; never reads the .env contents or a key value."""
    path = Path(repo_root) / ".env"
    status = {"environment_variable_set": bool(os.environ.get(KEY_NAME)), "env_file_present": path.exists() or path.is_symlink(),
              "env_file_regular": None, "env_file_private": None, "credentials_read": False}
    if status["env_file_present"]:
        status["env_file_regular"] = path.is_file() and not path.is_symlink()
        status["env_file_private"] = (not path.is_symlink()) and not (stat.S_IMODE(path.lstat().st_mode) & 0o077)
    return status


def load_openrouter_key(repo_root):
    """Read only at authorized paid launch / approved checks; never log the result."""
    key = os.environ.get(KEY_NAME)
    if key:
        return key
    path = Path(repo_root) / ".env"
    if path.is_symlink():
        raise RuntimeError("Host .env must be a regular private file (chmod 600), not a symlink")
    if not path.exists():
        raise RuntimeError("Set OPENROUTER_API_KEY in the host environment or private .env")
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("Host .env must be a regular private file (chmod 600)")
    # Parse only the expected key; deliberately no arbitrary environment expansion.
    values = [v for v in (_env_assignment(line, KEY_NAME) for line in path.read_text().splitlines()) if v]
    if len(set(values)) > 1:
        raise RuntimeError(f"Host .env assigns {KEY_NAME} more than once with different values")
    if values:
        return values[0]
    raise RuntimeError("Host key is absent")
