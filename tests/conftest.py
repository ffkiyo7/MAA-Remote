class FakePopen:
    """脚本化 Popen 替身。实例本身可作为 popen 工厂传入。"""

    def __init__(self, lines, returncode=0, boom=None):
        self._lines = lines
        self._returncode = returncode
        self._boom = boom
        self.killed = False
        self.cmd = None
        self.kw = None

    def __call__(self, cmd, **kw):
        if self._boom is not None:
            raise self._boom
        self.cmd = cmd
        self.kw = kw
        return self

    @property
    def stdout(self):
        return iter(line + "\n" for line in self._lines)

    @property
    def returncode(self):
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self.killed = True
