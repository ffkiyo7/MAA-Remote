from __future__ import annotations

import subprocess


def run_utf8(cmd, **kw):
    """Run a subprocess with UTF-8 text decoding by default."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, **kw)
