from __future__ import annotations

import glob
import os
import subprocess
import shutil


_AGENT_CONTEXT_ENV = ("HERMES_HOME", "HERMES_GIT_BASH_PATH", "OPENCLAW_HOME", "LARK_CHANNEL")


def run_utf8(cmd, **kw):
    """Run a subprocess with UTF-8 text decoding by default."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    if _is_lark_cli_cmd(cmd):
        kw.setdefault("env", lark_subprocess_env())
    return subprocess.run(cmd, **kw)


def lark_profile_args(profile: str) -> list[str]:
    """Return lark-cli profile args; empty profile keeps the current default config."""
    return ["--profile", profile] if profile else []


def lark_subprocess_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Run MAA's lark-cli calls outside agent-specific workspace auto-detection."""
    clean = dict(os.environ if env is None else env)
    for key in _AGENT_CONTEXT_ENV:
        clean.pop(key, None)
    return clean


def _is_lark_cli_cmd(cmd) -> bool:
    if not cmd:
        return False
    first = cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd).split()[0]
    return os.path.basename(str(first)).lower().startswith("lark-cli")


def resolve_executable(name: str) -> str:
    """Resolve command names for direct subprocess calls on Windows.

    npm 的 .cmd 垫片会把参数再过一遍 cmd.exe:含 &、引号或换行的参数会被
    截断/吞掉(实测连 --as 都会丢,导致身份回退)。垫片旁 node_modules 里
    若有包自带的原生 exe,优先直接调它,绕开 cmd.exe 这一层。
    """
    found = shutil.which(name)
    if not found:
        return name
    if found.lower().endswith((".cmd", ".bat")):
        shim_dir = os.path.dirname(found)
        for pattern in (
            os.path.join(shim_dir, "node_modules", "*", "bin", name + ".exe"),
            os.path.join(shim_dir, "node_modules", "*", "*", "bin", name + ".exe"),
        ):
            matches = glob.glob(pattern)
            if matches:
                return matches[0]
    return found
