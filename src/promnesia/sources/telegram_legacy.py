'''
Uses [[https://github.com/fabianonline/telegram_backup#readme][telegram_backup]] database for messages data
'''

from pathlib import Path
import sqlite3
from textwrap import dedent
from typing import Union, TypeVar
from urllib.parse import unquote # TODO mm, make it easier to rememember to use...

from ..common import PathIsh, Visit, get_logger, Loc, extract_urls, from_epoch, Results, echain
from ..sqlite import sqlite_connection

T = TypeVar("T")


def unwrap(res: Union[T, Exception]) -> T:
    if isinstance(res, Exception):
        raise res
    else:
        return res


def index(database: PathIsh, *, http_only: bool=False) -> Results:
    """
    :param database:
        the path of the sqlite generated by the _telegram_backup_ java program
    :param http_only:
        when true, do not collect IP-addresses and `python.py` strings
    """
    logger = get_logger()

    path = Path(database)
    assert path.is_file(), path

    def make_query(text_query: str) -> str:
        extra_criteria = "AND (M.has_media == 1 OR text LIKE '%http%')" if http_only else ""
        return dedent(
            f"""
            WITH entities AS (
            SELECT 'dialog' as type
                , id
                , coalesce(username, id) as handle
                , coalesce(first_name || " " || last_name
                    , username
                    , id
                ) as display_name FROM users
            UNION
            SELECT 'group' as type
                , id
                , id as handle
                , coalesce(name, id) as display_name FROM chats
            )
            SELECT src.display_name AS chatname
                , src.handle       AS chat
                , snd.display_name AS sender
                , M.time           AS time
                , {text_query}     AS text
                , M.message_id     AS mid
            FROM messages AS M
                                                                                /* chat types are 'dialog' (1-1), 'group' and 'supergroup' */
                                                                                /* this is abit hacky way to handle all groups in one go */
            LEFT JOIN entities AS src    ON M.source_id = src.id AND src.type = (CASE M.source_type WHEN 'supergroup' THEN 'group' ELSE M.source_type END)
            LEFT JOIN entities AS snd    ON M.sender_id = snd.id AND snd.type = 'dialog'
            WHERE
                M.message_type NOT IN ('service_message', 'empty_message')
                {extra_criteria}
            ORDER BY time;
            """)

    with sqlite_connection(path, immutable=True, row_factory='row') as db:
        # TODO yield error if chatname or chat or smth else is null?
        for row in db.execute(make_query('M.text')):
            try:
                yield from _handle_row(row)
            except Exception as ex:
                yield echain(RuntimeError(f'While handling {row}'), ex)

        # old (also 'stable') version doesn't have 'json' column yet...
        messages_columns = [d[0] for d in db.execute('SELECT * FROM messages').description]
        # todo hmm what is 'markup_json'??
        if 'json' in messages_columns:
            for row in db.execute(make_query("json_extract(json, '$.media.webpage.description')")):
                try:
                    yield from _handle_row(row)
                except Exception as ex:
                    yield echain(RuntimeError(f'While handling {row}'), ex)


def _handle_row(row: sqlite3.Row) -> Results:
    text = row['text']
    if text is None:
        return
    urls = extract_urls(text)
    if len(urls) == 0:
        return
    dt            = from_epoch(row['time'])
    mid: str      = unwrap(row['mid'])

    # TODO perhaps we could be defensive with null sender/chat etc and still emit the Visit
    sender: str   = unwrap(row['sender'])
    chatname: str = unwrap(row['chatname'])
    chat: str     = unwrap(row['chat'])

    in_context = f'https://t.me/{chat}/{mid}'
    for u in urls:
        # https://www.reddit.com/r/Telegram/comments/6ufwi3/link_to_a_specific_message_in_a_channel_possible/
        # hmm, only seems to work on mobile app, but better than nothing...
        yield Visit(
            url=unquote(u),
            dt=dt,
            context=f"{sender}: {text}",
            locator=Loc.make(
                title=f"chat with {chatname}",
                href=in_context,
            ),
        )