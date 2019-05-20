#!/usr/bin/env python3
import argparse
from datetime import datetime
import logging
import tempfile
from os.path import lexists
from typing import Optional, NamedTuple, Sequence, Tuple, Union
from pathlib import Path
import subprocess
from subprocess import check_call, DEVNULL, check_output

# pip3 install python-magic
import magic # type: ignore
mime = magic.Magic(mime=True)

from kython.py37 import nullcontext
from kython.klogging import setup_logzero

from browser_history import Browser, backup_history, CHROME, FIREFOX, guess_db_date


def get_logger():
    return logging.getLogger('populate-browser-history')


def sqlite(db: Path, script, method=check_call, **kwargs):
    return method(['sqlite3', str(db), script], **kwargs)


def entries(db: Path) -> Optional[int]:
    if not db.exists():
        return None
    return int(sqlite(db, 'SELECT COUNT(*) FROM visits', method=check_output).decode('utf8'))


Col = Union[str, Tuple[str, Optional[str]]] # tuple is renaming
ColType = str


class Schema(NamedTuple):
    cols: Sequence[Tuple[Col, ColType]]
    key: Sequence[str]


SchemaCheck = Tuple[str, str]

def create(db: Path, table: str, schema: Schema):
    things = []
    for cc, tp in schema.cols:
        from_: str
        to: Optional[str]
        if isinstance(cc, str):
            from_ = cc
            to = cc
        else:
            (from_, to) = cc
        if to is not None:
            to = to.split('.')[-1] # strip off table alias
            things.append(f'{to} {tp}')

    query = f"""
CREATE TABLE {table}(
    {', '.join(things)},
    PRIMARY KEY ({', '.join(schema.key)})
);
    """
    sqlite(db, query)


# at first, I was merging urls and visits tables separately... but it's kinda messy when you e.g. reinstall OS and table ids reset
# so joining before inserting makes a bit more sense.. we're throwing id anyway since it's fairly useless for the same reasons
# TODO it's a bit slow now because of the repeating joins presumably... could pass last handled visit date or something... not sure if it's safe
# TODO not sure how to make chunk read only?
def merge_browser(
        merged: Path,
        chunk: Path,
        schema: Schema,
        schema_check: SchemaCheck,
        query: str,
):
    check_table, expected = schema_check
    # ugh. a bit ugly but kinda works
    res = sqlite(chunk, f"select group_concat(name, ', ') from pragma_table_info('{check_table}')", method=check_output).decode('utf8').strip()
    if res != expected:
        raise AssertionError(f'expected schema {expected}, got {res}')


    if not merged.exists():
        create(merged, 'visits', schema)

    proj = ', '.join(c for c, _ in schema.cols) # type: ignore
    query = f"""
ATTACH '{chunk}' AS chunk;

INSERT OR IGNORE INTO main.visits
    SELECT {proj}
    {query};

DETACH chunk;
    """
    sqlite(merged, query)


class Extr(NamedTuple):
    detector: str
    schema_check: SchemaCheck
    schema: Schema
    query: str


chrome = Extr(
    detector='keyword_search_terms',
    schema_check=('visits', "id, url, visit_time, from_visit, transition, segment_id, visit_duration, incremented_omnibox_typed_score"),
    schema=Schema(cols=[
        ('U.url'                                  , 'TEXT'),

        # ('V.id'                                   , 'INTEGER'),
        # V.url is quite useless
        ('V.visit_time'                             , 'INTEGER NOT NULL'),
        ('V.from_visit'                             , 'INTEGER'),
        ('V.transition'                             , 'INTEGER NOT NULL'),
        # V.segment_id looks useless
        ('V.visit_duration'                         , 'INTEGER NOT NULL'),
        # V.omnibox thing looks useless
    ], key=('url', 'visit_time')),
    query='FROM chunk.visits as V, chunk.urls as U WHERE V.url = U.id',
)

# https://www.forensicswiki.org/wiki/Mozilla_Firefox_3_History_File_Format#moz_historyvisits
firefox = Extr(
    detector='moz_meta',
    schema_check=('moz_historyvisits', "id, from_visit, place_id, visit_date, visit_type, session"),
    schema=Schema(cols=[
        ('P.url'       , 'TEXT'),

        # ('H.id'        , 'INTEGER'),
        ('H.from_visit', 'INTEGER'),
        # ('H.place_id'  , 'INTEGER'),
        ('H.visit_date', 'INTEGER'),
        ('H.visit_type', 'INTEGER'),
        # not sure what session is form but could be useful?..
        ('H.session'   , 'INTEGER'),
    ], key=('url', 'visit_date')),
    query='FROM chunk.moz_historyvisits as H, chunk.moz_places as P WHERE H.place_id = P.id',
)


firefox_phone = Extr(
    detector='remote_devices',
    schema_check=('visits', "_id, history_guid, visit_type, date, is_local"),
    schema=Schema(cols=[
        ('H.url'         , 'TEXT NOT NULL'),

        # primary key in orig table, but here could be non unuque
        # ('_id'         , 'INTEGER NOT NULL'),
        # ('history_guid', 'TEXT NOT NULL'),
        ('V.visit_type'  , 'INTEGER NOT NULL'),
        ('V.date'        , 'INTEGER NOT NULL'),
        # ('is_local'    , 'INTEGER NOT NULL'),
    ], key=('url', 'date')),
    query='FROM chunk.history as H, chunk.visits as V WHERE H.guid = V.history_guid',
)

def merge(merged: Path, chunk: Path):
    logger = get_logger()
    logger.info(f"Merging {chunk} into {merged}")
    if lexists(merged):
        logger.info("DB size before: %s items %d bytes", entries(merged), merged.stat().st_size)
    else:
        logger.info(f"DB doesn't exist yet: {merged}")

    candidates = []
    for ff in [chrome, firefox, firefox_phone]:
        res = sqlite(chunk, f"SELECT * FROM {ff.detector}", method=subprocess.run, stdout=DEVNULL, stderr=DEVNULL)
        if res.returncode == 0:
            candidates.append(ff)
    assert len(candidates) == 1
    merger = candidates[0]

    merge_browser(merged=merged, chunk=chunk, schema=merger.schema, schema_check=merger.schema_check, query=merger.query)
    logger.info("DB size after : %s items %d bytes", entries(merged), merged.stat().st_size)


def merge_from(browser: Optional[Browser], from_: Optional[Path], to: Path):
    assert not to.is_dir()

    logger = get_logger()
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)

        if from_ is None:
            assert browser is not None
            backup_history(browser, tdir)
            from_ = tdir

        for dbfile in sorted(x for x in from_.rglob('*') if x.is_file() and mime.from_file(str(x)) in ['application/x-sqlite3']):
            logger.info('merging %s', dbfile)
            merge(merged=to, chunk=dbfile)


def _helper(tmp_path, browser):
    logger = get_logger()
    setup_logzero(logger, level=logging.DEBUG)

    tdir = Path(tmp_path)
    merged = tdir / 'merged.sqlite'

    entr = entries(merged)
    assert entr is None

    merge_from(browser, None, merged)
    merge_from(browser, None, merged)

    entr = entries(merged)
    assert entr is not None
    assert entr > 100 # quite arbitrary, but ok for now

def test_merge_chrome(tmp_path):
    _helper(tmp_path, CHROME)

def test_merge_firefox(tmp_path):
    _helper(tmp_path, FIREFOX)


def main():
    logger = get_logger()
    setup_logzero(logger, level=logging.DEBUG)

    p = argparse.ArgumentParser()
    p.add_argument('--browser', type=Browser, required=False)
    p.add_argument('--to', type=Path, required=True)
    p.add_argument('--from', type=Path, default=None)
    args = p.parse_args()

    from_ = getattr(args, 'from')

    # TODO need to mark already handled? although it's farily quick as it s
    # maybe should use the DB thing to handle merged??
    merge_from(browser=args.browser, from_=from_, to=args.to)


if __name__ == '__main__':
    main()


