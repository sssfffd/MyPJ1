"""crossreview - 횡전개(동일 기능의 타 버전/레포/브랜치 이식) 코드 리뷰용 diff 수집기.

표준 라이브러리 + git CLI 만 사용한다.
"""

from .gitcmd import GitError, Repo
from .resolve import ResolveError, resolve_side
from .collect import collect_side
from .pairing import pair_files
from .bundle import build_bundle, iter_review_units, file_patch_text

__version__ = "0.1.0"
__all__ = [
    "GitError", "Repo", "ResolveError", "resolve_side", "collect_side",
    "pair_files", "build_bundle", "iter_review_units", "file_patch_text",
]
