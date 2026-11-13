#!/usr/bin/env python
"""빌드: 단일 실행 파일 crossreview.pyz 를 레포 최상단에 만든다 (표준 라이브러리 zipapp 만 사용).

    python build.py

결과물
    crossreview.pyz   (레포 최상단) Python 만 있으면 어디서든 실행:  python crossreview.pyz
                      실행 설정은 같은 폴더의 crossreview.ini (처음 실행 시 자동 생성)
"""

import os
import shutil
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(ROOT, "crossreview.pyz")
MAIN = "import sys\nfrom crossreview.cli import main\nsys.exit(main())\n"


def main():
    stage = tempfile.mkdtemp(prefix="crossreview-build-")
    try:
        shutil.copytree(os.path.join(ROOT, "crossreview"), os.path.join(stage, "crossreview"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with open(os.path.join(stage, "__main__.py"), "w") as fp:
            fp.write(MAIN)
        target = TARGET
        kwargs = {"compressed": True} if sys.version_info >= (3, 7) else {}
        zipapp.create_archive(stage, target, interpreter="/usr/bin/env python3", **kwargs)
        print("built: %s (%d bytes)" % (target, os.path.getsize(target)))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()
