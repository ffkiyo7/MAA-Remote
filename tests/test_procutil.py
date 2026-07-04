import sys

from maa_remote.procutil import run_utf8


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
