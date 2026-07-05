from __future__ import annotations

import glob
import os
import subprocess
import shutil


def run_utf8(cmd, **kw):
    """Run a subprocess with UTF-8 text decoding by default."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, **kw)


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
