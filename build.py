#!/usr/bin/env python
"""빌드: 단일 실행 파일 dist/crossreview.pyz 를 만든다 (표준 라이브러리 zipapp 만 사용).

    python build.py

결과물
    dist/crossreview.pyz   Python 만 있으면 어디서든 실행:  python crossreview.pyz
    dist/crossreview.bat   Windows 용 실행 스크립트 (더블클릭 -> UI)
"""

import os
import shutil
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
MAIN = "import sys\nfrom crossreview.cli import main\nsys.exit(main())\n"
BAT = '@echo off\r\npython "%~dp0crossreview.pyz" %*\r\nif errorlevel 1 pause\r\n'


def main():
    stage = tempfile.mkdtemp(prefix="crossreview-build-")
    try:
        shutil.copytree(os.path.join(ROOT, "crossreview"), os.path.join(stage, "crossreview"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with open(os.path.join(stage, "__main__.py"), "w") as fp:
            fp.write(MAIN)
        if not os.path.isdir(DIST):
            os.makedirs(DIST)
        target = os.path.join(DIST, "crossreview.pyz")
        kwargs = {"compressed": True} if sys.version_info >= (3, 7) else {}
        zipapp.create_archive(stage, target, interpreter="/usr/bin/env python3", **kwargs)
        with open(os.path.join(DIST, "crossreview.bat"), "w", newline="") as fp:
            fp.write(BAT)
        print("built: %s (%d bytes)" % (target, os.path.getsize(target)))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()
