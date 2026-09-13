"""valtiolle.fi — the Finnish state's own job board (ministries, agencies, the courts).

Every posting here is a public-sector job in Finland: an agency's own advert, published before
or alongside its Työmarkkinatori syndication and with the whole advert on the page. The site
runs on Grade Solutions Oy's platform, which :mod:`jobscraper.sources._grade` parses; this
module is only the host, the wording and the default search terms.

Verified live 2026-09-13: ``?desc=ohjelmisto`` → 15 hits on one page, ``?desc=harjoittelija``
→ 19, ``?desc=data`` → 41 over two pages.
"""

from __future__ import annotations

from jobscraper.sources._grade import GradeBoard, GradeSource
from jobscraper.sources.base import register

BOARD = GradeBoard(
    name="valtiolle",
    description="valtiolle.fi — Finnish state employers (Grade Solutions platform)",
    base="https://valtiolle.fi/",
    default_queries=("ohjelmisto", "kehittäjä", "software", "harjoittelija", "data"),
)


class Valtiolle(GradeSource):
    board = BOARD


register(Valtiolle())
