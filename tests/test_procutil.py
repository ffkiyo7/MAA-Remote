import sys

from maa_remote.procutil import resolve_executable, run_utf8


def test_run_utf8_decodes_chinese_output():
    r = run_utf8(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('理智药中文输出'.encode('utf-8'))",
        ]
    )
    assert r.returncode == 0
    assert r.stdout == "理智药中文输出"


def test_run_utf8_kwargs_passthrough():
    r = run_utf8([sys.executable, "-c", "print('x')"], timeout=30)
    assert r.stdout.strip() == "x"


def test_resolve_executable_keeps_unknown_command_name():
    assert resolve_executable("definitely-not-a-real-maa-remote-command") == (
        "definitely-not-a-real-maa-remote-command"
    )


def test_resolve_executable_prefers_native_exe_over_npm_cmd_shim(tmp_path, monkeypatch):
    # npm 全局目录布局:垫片在顶层,真身在 node_modules/<scope>/<pkg>/bin/
    shim = tmp_path / "lark-cli.CMD"
    shim.write_text("@echo off\n", encoding="utf-8")
    native = tmp_path / "node_modules" / "@larksuite" / "cli" / "bin" / "lark-cli.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")

    monkeypatch.setattr("maa_remote.procutil.shutil.which", lambda name: str(shim))
    assert resolve_executable("lark-cli") == str(native)


def test_resolve_executable_falls_back_to_shim_without_native_exe(tmp_path, monkeypatch):
    shim = tmp_path / "lark-cli.CMD"
    shim.write_text("@echo off\n", encoding="utf-8")

    monkeypatch.setattr("maa_remote.procutil.shutil.which", lambda name: str(shim))
    assert resolve_executable("lark-cli") == str(shim)


def test_resolve_executable_returns_real_exe_untouched(tmp_path, monkeypatch):
    exe = tmp_path / "lark-cli.exe"
    exe.write_bytes(b"MZ")

    monkeypatch.setattr("maa_remote.procutil.shutil.which", lambda name: str(exe))
    assert resolve_executable("lark-cli") == str(exe)
