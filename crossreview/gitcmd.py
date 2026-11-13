"""git CLI 를 subprocess 로 호출하는 얇은 래퍼."""

import os
import subprocess


class GitError(Exception):
    pass


class Repo(object):
    def __init__(self, path, git="git"):
        self.path = os.path.abspath(path)
        self.git = git
        if not os.path.isdir(self.path):
            raise GitError("레포 경로가 없습니다: %s" % self.path)
        if not self.ok(["rev-parse", "--git-dir"]):
            raise GitError("git 레포가 아닙니다: %s" % self.path)

    def _env(self, extra):
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        env["GIT_OPTIONAL_LOCKS"] = "0"   # 읽기 전용 동작: index lock 을 잡지 않는다
        env["GIT_PAGER"] = "cat"
        if extra:
            env.update(extra)
        return env

    def _cmd(self, args):
        return [self.git, "-C", self.path,
                "-c", "core.quotepath=false", "-c", "color.ui=false"] + list(args)

    def run_raw(self, args, input=None, env=None):
        """(returncode, stdout bytes, stderr str) 반환. 예외를 던지지 않는다."""
        try:
            p = subprocess.Popen(self._cmd(args), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=self._env(env))
        except OSError as e:
            raise GitError("git 실행 실패 (%s): %s" % (self.git, e))
        out, err = p.communicate(input)
        return p.returncode, out, err.decode("utf-8", "replace")

    def run(self, args, input=None, env=None):
        code, out, err = self.run_raw(args, input, env)
        if code != 0:
            raise GitError("git %s 실패 (exit %d): %s" % (" ".join(args), code, err.strip()))
        return out

    def text(self, args, env=None):
        return self.run(args, env=env).decode("utf-8", "replace").strip()

    def lines(self, args, env=None):
        t = self.text(args, env=env)
        return t.split("\n") if t else []

    def ok(self, args, env=None):
        return self.run_raw(args, env=env)[0] == 0
