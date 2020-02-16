# TODO eh. confusing how guess and auto are different...
from typing import Iterable, Any

from ..common import Extraction, PathIsh


def is_git_repo(p: str) -> bool:
    if '://github.com/' in p:
        return True
    return False


def index(path: PathIsh, *args, **kwargs) -> Iterable[Extraction]:
    ps = str(path)
    # TODO better url detection

    index_: Any # meh
    if is_git_repo(ps):
        from . import vcs
        index_ = vcs.index
    else:
        from . import auto
        index_ = auto.index
    return index_(path, *args, **kwargs)
