from pathlib import Path
from typing import  Optional, Union, Sequence, Tuple, Mapping

import pytest

from promnesia.common import _is_windows
from promnesia.database.load import get_all_db_visits


def run_index(cfg: Path, *, update=False) -> None:
    from promnesia.__main__ import do_index
    do_index(cfg, overwrite_db=not update)


def test_example_config(tmp_path: Path) -> None:
    if _is_windows:
        pytest.skip("doesn't work on Windows: example config references /usr/include paths")

    from promnesia.__main__ import read_example_config
    ex = read_example_config()
    cfg = tmp_path / 'test_config.py'
    cfg.write_text(ex)
    run_index(cfg)


# TODO a bit shit... why did I make it dict at first??
Urls = Union[
           Mapping[str, Optional[str]],
    Sequence[Tuple[str, Optional[str]]],
]

def index_urls(urls: Urls, *, source_name: str='test'):
    uuu = list(urls.items()) if isinstance(urls, dict) else urls

    def idx(tmp_path: Path):
        cfg = tmp_path / 'test_config.py'
        cfg.write_text(f"""
OUTPUT_DIR = r'{tmp_path}'

from promnesia.common import Source, Visit, Loc
from datetime import datetime, timedelta
# todo reuse demo indexer?
indexer = Source(
    lambda: [Visit(
        url=url,
        dt=datetime.min + timedelta(days=5000) + timedelta(hours=i),
        locator=Loc.make('test'),
        context=ctx,
    ) for i, (url, ctx) in enumerate({uuu})],
    name=f'{source_name}',
)

SOURCES = [indexer]
""")
        run_index(cfg)
    return idx


def test_comparison(tmp_path: Path) -> None:
    from promnesia.compare import compare_files

    idx = index_urls({
        'https://example.com': None,
        'https://en.wikipedia.org/wiki/Saturn_V': None,
        'https://plato.stanford.edu/entries/qualia': None,
    })
    idx(tmp_path)
    db     = tmp_path / 'promnesia.sqlite'
    old_db = tmp_path / 'promnesia-old.sqlite'
    import shutil
    shutil.move(str(db), str(old_db))

    idx2 = index_urls({
        'https://example.com': None,
        'https://www.reddit.com/r/explainlikeimfive/comments/1ev6e0/eli5entropy': None,
        'https://en.wikipedia.org/wiki/Saturn_V': None,
        'https://plato.stanford.edu/entries/qualia': None,
    })
    idx2(tmp_path)

    # should not crash, as there are more links in the new database
    assert len(list(compare_files(old_db, db))) == 0

    assert len(list(compare_files(db, old_db))) == 1


def test_index_many(tmp_path: Path) -> None:
    # NOTE [20200521] experimenting with promnesia.dump._CHUNK_BY
    # inserting 100K visits
    # value=1000: 9 seconds
    # value=10  : 9 seconds
    # value=1   : 18 seconds
    # ok, I guess it's acceptable considering the alternative is crashing (too many sql variables on some systems)
    # kinda makes sense -- I guess most overhead is coming from creating temporary lists etc?
    cfg = tmp_path / 'test_config.py'
    cfg.write_text(f"""
from datetime import datetime, timedelta
from promnesia.common import Source, Visit, Loc
# TODO def need to allow taking in index function without having to wrap in source?
def index():
    for i in range(100000):
        yield Visit(
            url='http://whatever/page' + str(i),
            dt=datetime.min + timedelta(days=5000) + timedelta(hours=i),
            locator=Loc.make('test'),
        )

SOURCES = [Source(index)]
OUTPUT_DIR = r'{tmp_path}'
    """)
    run_index(cfg)
    visits = get_all_db_visits(tmp_path / 'promnesia.sqlite')

    assert len(visits) == 100000


def test_indexing_error(tmp_path: Path) -> None:
    cfg = tmp_path / 'test_config.py'
    cfg.write_text(f'''
def bad_index():
    import bad_import
    yield from [] # dummy
from promnesia.common import Source
from promnesia.sources import demo

SOURCES = [
    bad_index,
    Source(demo, count=3),
]
OUTPUT_DIR = r'{tmp_path}'
''')
    run_index(cfg)
    # TODO hmm, this is more of a cli test, actually need to run as binary? not sure
    # with pytest.raises(SystemExit):
    #     run_index(cfg)
        # should exit(1)

    # yet save the database
    visits = get_all_db_visits(tmp_path / 'promnesia.sqlite')

    [e, _, _, _] = visits
    assert e.src == 'error'
