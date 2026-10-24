"""MCP server for IMDb: public title data plus one user's own ratings and watchlist."""

import importlib.metadata
import json
import logging
import os
import re
import time
from collections import Counter

from mcp.server.fastmcp import FastMCP
from mcp.types import Icon

from . import paging
from .client import ImdbClient, ImdbError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    __version__ = importlib.metadata.version("imdb-mcp")
except importlib.metadata.PackageNotFoundError:  # running from a source tree
    __version__ = "0.0.0"

_ICON_BASE = os.getenv("MCP_PUBLIC_URL", "").rstrip("/")
_ICON_SIZES = (48, 96, 256)

# The SDK's 30-minute default kills sessions of chats left idle, and their next call fails.
SESSION_IDLE_TIMEOUT = 24 * 3600

mcp = FastMCP(
    "imdb",
    session_idle_timeout=SESSION_IDLE_TIMEOUT,
    icons=(
        [
            Icon(
                src=f"{_ICON_BASE}/icon.png"
                if size == 256
                else f"{_ICON_BASE}/icon-{size}.png",
                mimeType="image/png",
                sizes=[f"{size}x{size}"],
            )
            for size in _ICON_SIZES
        ]
        if _ICON_BASE
        else None
    ),
    website_url=_ICON_BASE or None,
    instructions=(
        "Read IMDb: titles, people, credits, episodes, reviews, box office, "
        "charts and search, plus the configured user's own ratings and "
        "watchlist. Ratings here are that user's own 1-10 scores; "
        "imdb_rating is the public average and is never their opinion. "
        "Resolve a title with search_titles before asking for details. For "
        "'have I seen', 'what did I rate' or 'my best films of 1999' use "
        "my_rating_for and find_my_ratings, which read the whole ratings "
        "history. IMDb has no public write API, so nothing here can rate, "
        "review or change a watchlist."
    ),
)

mcp._mcp_server.version = __version__
paging.register(mcp)

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

RATINGS_PAGE = 250
"""The largest page userRatings hands out."""
RATINGS_TTL = 15 * 60
"""Seconds a fetched ratings history stays good for."""

CHARTS = {
    "top_rated_movies": "TOP_RATED_MOVIES",
    "lowest_rated_movies": "LOWEST_RATED_MOVIES",
    "top_rated_tv_shows": "TOP_RATED_TV_SHOWS",
    "top_rated_english_movies": "TOP_RATED_ENGLISH_MOVIES",
    "most_popular_movies": "MOST_POPULAR_MOVIES",
    "most_popular_tv_shows": "MOST_POPULAR_TV_SHOWS",
}
SEARCH_SORTS = {
    "popularity": "POPULARITY",
    "rating": "USER_RATING",
    "votes": "USER_RATING_COUNT",
    "release_date": "RELEASE_DATE",
    "alphabetical": "TITLE_REGIONAL",
    "runtime": "RUNTIME",
    "metascore": "METACRITIC_SCORE",
    "box_office": "BOX_OFFICE_GROSS_DOMESTIC",
}
RATING_SORTS = {"recent": "MOST_RECENT", "top": "TOP_RATED"}
TITLE_TYPES = ("movie", "tvSeries", "tvMiniSeries", "tvMovie", "tvEpisode", "short", "video", "videoGame")

_TITLE = """
  id titleText { text } originalTitleText { text } releaseYear { year endYear }
  titleType { id text } runtime { seconds }
  ratingsSummary { aggregateRating voteCount }
  titleGenres { genres { genre { text } } }
"""
_NAME = "id nameText { text } primaryProfessions { category { text } }"

_client: ImdbClient | None = None
_ratings_cache: tuple[float, list[dict]] | None = None


def _get_client() -> ImdbClient:
    global _client
    if _client is None:
        _client = ImdbClient()
    return _client


def _q(query: str) -> dict:
    return _get_client().query(query)


def _s(value) -> str:
    """A GraphQL string literal."""
    return json.dumps(str(value))


def _ok(data: dict) -> str:
    return paging.fit({"status": "success", **data})


def _err(e: Exception) -> str:
    import httpx

    if isinstance(e, ImdbError) and e.needs_login:
        msg = (
            "IMDb only shows that to a signed-in user, and this server has no "
            "IMDb login. Your own public ratings and watchlist still work when "
            "IMDB_USER_ID or IMDB_PROFILE_ID is set."
        )
    elif isinstance(e, ImdbError):
        msg = f"IMDb refused the request: {e}"
    elif isinstance(e, httpx.TimeoutException):
        msg = "Request timed out. IMDb may be slow -- try again."
    elif isinstance(e, httpx.ConnectError):
        msg = "Could not connect to IMDb. Check network connectivity."
    elif isinstance(e, httpx.HTTPStatusError):
        msg = f"IMDb API error (HTTP {e.response.status_code})."
    else:
        msg = f"{type(e).__name__}: {e}"

    return json.dumps({"status": "error", "message": msg})


def _title(node: dict | None) -> dict | None:
    if not node:
        return None
    summary = node.get("ratingsSummary") or {}
    runtime = (node.get("runtime") or {}).get("seconds")
    year = node.get("releaseYear") or {}
    return {
        "id": node.get("id"),
        "title": (node.get("titleText") or {}).get("text"),
        "original_title": (node.get("originalTitleText") or {}).get("text"),
        "year": year.get("year"),
        "end_year": year.get("endYear"),
        "type": (node.get("titleType") or {}).get("id"),
        "runtime_minutes": runtime // 60 if runtime else None,
        "imdb_rating": summary.get("aggregateRating"),
        "votes": summary.get("voteCount"),
        "genres": [g["genre"]["text"] for g in (node.get("titleGenres") or {}).get("genres", [])],
    }


def _name(node: dict | None) -> dict | None:
    if not node:
        return None
    return {
        "id": node.get("id"),
        "name": (node.get("nameText") or {}).get("text"),
        "professions": [p["category"]["text"] for p in node.get("primaryProfessions") or []],
    }


def _edges(connection: dict | None) -> list[dict]:
    return [e["node"] for e in (connection or {}).get("edges", [])]


def _page_info(connection: dict | None) -> dict:
    info = (connection or {}).get("pageInfo") or {}
    return {"total": (connection or {}).get("total"), "next_cursor": info.get("endCursor") if info.get("hasNextPage") else None}


def _after(cursor: str | None) -> str:
    return f", after: {_s(cursor)}" if cursor else ""


def _check(value: str, allowed, what: str) -> str:
    if value not in allowed:
        raise ValueError(f"{what} must be one of: {', '.join(allowed)}")
    return value


def _tt(value: str) -> str:
    match = re.search(r"tt\d+", value or "")
    if not match:
        raise ValueError(f"Not a title id: {value!r}. Expected tt followed by digits, as in tt0078748.")
    return match.group(0)


def _nm(value: str) -> str:
    match = re.search(r"nm\d+", value or "")
    if not match:
        raise ValueError(f"Not a name id: {value!r}. Expected nm followed by digits, as in nm0000244.")
    return match.group(0)


# ------------------------------------------------------------------
# Titles
# ------------------------------------------------------------------


@mcp.tool(annotations=_READ)
def search_titles(query: str, title_type: str = "any", limit: int = 10) -> str:
    """Find films, series and episodes by name.

    Start here to get the tt id every other title tool needs.

    Args:
        query: Title, or part of one.
        title_type: any, movie or tv.
        limit: How many matches to return.
    """
    try:
        kind = _check(title_type, ("any", "movie", "tv"), "title_type")
        option = "" if kind == "any" else f", titleSearchOptions: {{type: {kind.upper()}}}"
        data = _q(
            f"{{ mainSearch(first: {int(limit)}, options: {{searchTerm: {_s(query)}, type: TITLE{option}}}) "
            f"{{ edges {{ node {{ entity {{ ... on Title {{ {_TITLE} }} }} }} }} }} }}"
        )
        return _ok({"titles": [_title(n["entity"]) for n in _edges(data["mainSearch"])]})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_title(id: str) -> str:
    """Get one title in full: plot, ratings, cast and crew, genres, release,
    certificate, awards, taglines and poster.

    Args:
        id: Title id such as tt0078748, or an IMDb URL containing one.
    """
    try:
        tid = _tt(id)
        data = _q(
            f"""{{ title(id: {_s(tid)}) {{ {_TITLE}
              releaseDate {{ day month year }} certificate {{ rating }}
              metacritic {{ metascore {{ score }} }}
              ratingsSummary {{ topRanking {{ rank }} }} meterRanking {{ currentRank }}
              plot {{ plotText {{ plainText }} }} primaryImage {{ url }}
              principalCredits {{ category {{ text }} credits {{ name {{ id nameText {{ text }} }} ... on Cast {{ characters {{ name }} }} }} }}
              countriesOfOrigin {{ countries {{ text }} }} spokenLanguages {{ spokenLanguages {{ text }} }}
              series {{ series {{ id titleText {{ text }} }} episodeNumber {{ seasonNumber episodeNumber }} }}
              episodes {{ episodes(first: 1) {{ total }} }}
              taglines(first: 3) {{ edges {{ node {{ text }} }} }}
              awardNominations(first: 1) {{ total }} prestigiousAwardSummary {{ wins nominations }}
              reviews(first: 1) {{ total }}
            }} }}"""
        )
        t = data["title"]
        if not t:
            raise ValueError(f"IMDb has no title {tid}.")
        out = _title(t)
        rd = t.get("releaseDate") or {}
        series = t.get("series") or {}
        out.update({
            "release_date": f"{rd['year']}-{rd['month']:02d}-{rd['day']:02d}" if rd.get("day") else None,
            "certificate": (t.get("certificate") or {}).get("rating"),
            "metascore": ((t.get("metacritic") or {}).get("metascore") or {}).get("score"),
            "top_250_rank": ((t.get("ratingsSummary") or {}).get("topRanking") or {}).get("rank"),
            "popularity_rank": (t.get("meterRanking") or {}).get("currentRank"),
            "plot": ((t.get("plot") or {}).get("plotText") or {}).get("plainText"),
            "poster": (t.get("primaryImage") or {}).get("url"),
            "credits": {
                group["category"]["text"]: [
                    {
                        "id": c["name"]["id"],
                        "name": c["name"]["nameText"]["text"],
                        **({"characters": [ch["name"] for ch in c["characters"]]} if c.get("characters") else {}),
                    }
                    for c in group["credits"]
                ]
                for group in t.get("principalCredits") or []
            },
            "countries": [c["text"] for c in (t.get("countriesOfOrigin") or {}).get("countries", [])],
            "languages": [lang["text"] for lang in (t.get("spokenLanguages") or {}).get("spokenLanguages", [])],
            "series": _title(series.get("series")) if series.get("series") else None,
            "episode": series.get("episodeNumber"),
            "episode_count": ((t.get("episodes") or {}).get("episodes") or {}).get("total"),
            "taglines": [n["text"] for n in _edges(t.get("taglines"))],
            "award_nominations": (t.get("awardNominations") or {}).get("total"),
            "major_awards": t.get("prestigiousAwardSummary"),
            "review_count": (t.get("reviews") or {}).get("total"),
            "url": f"https://www.imdb.com/title/{tid}/",
        })
        return _ok({"title": out})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_title_credits(id: str, category: str | None = None, limit: int = 50, cursor: str | None = None) -> str:
    """List who worked on a title, with characters for the cast.

    Args:
        id: Title id.
        category: One of director, writer, actor, actress, producer, composer,
            cinematographer, editor. Omit for everyone, cast first.
        limit: How many credits per page.
        cursor: next_cursor from the previous page.
    """
    try:
        tid = _tt(id)
        where = f", filter: {{categories: [{_s(category)}]}}" if category else ""
        data = _q(
            f"{{ title(id: {_s(tid)}) {{ credits(first: {int(limit)}{where}{_after(cursor)}) {{ total pageInfo {{ hasNextPage endCursor }} "
            f"edges {{ node {{ category {{ text }} name {{ {_NAME} }} ... on Cast {{ characters {{ name }} episodeCredits(first: 1) {{ total }} }} }} }} }} }} }}"
        )
        conn = data["title"]["credits"]
        credits = [
            {
                **_name(n["name"]),
                "category": n["category"]["text"],
                "characters": [c["name"] for c in n.get("characters") or []],
                "episodes": (n.get("episodeCredits") or {}).get("total"),
            }
            for n in _edges(conn)
        ]
        return _ok({"credits": credits, **_page_info(conn)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_title_episodes(id: str, season: int | None = None, limit: int = 50, cursor: str | None = None) -> str:
    """List a series' seasons and episodes with their ratings.

    Args:
        id: The series' title id.
        season: Only this season. Omit for all, in order.
        limit: How many episodes per page.
        cursor: next_cursor from the previous page.
    """
    try:
        tid = _tt(id)
        where = f", filter: {{includeSeasons: [{_s(season)}]}}" if season is not None else ""
        data = _q(
            f"{{ title(id: {_s(tid)}) {{ episodes {{ displayableSeasons(first: 100) {{ edges {{ node {{ season }} }} }} "
            f"episodes(first: {int(limit)}{where}{_after(cursor)}) {{ total pageInfo {{ hasNextPage endCursor }} "
            f"edges {{ node {{ {_TITLE} releaseDate {{ day month year }} series {{ episodeNumber {{ seasonNumber episodeNumber }} }} plot {{ plotText {{ plainText }} }} }} }} }} }} }} }}"
        )
        block = data["title"]["episodes"]
        if not block:
            raise ValueError(f"{tid} has no episodes. Pass the series, not a film or an episode.")
        conn = block["episodes"]
        episodes = []
        for n in _edges(conn):
            number = (n.get("series") or {}).get("episodeNumber") or {}
            rd = n.get("releaseDate") or {}
            episodes.append({
                **_title(n),
                "season": number.get("seasonNumber"),
                "episode": number.get("episodeNumber"),
                "aired": f"{rd['year']}-{rd['month']:02d}-{rd['day']:02d}" if rd.get("day") else None,
                "plot": ((n.get("plot") or {}).get("plotText") or {}).get("plainText"),
            })
        seasons = [n["season"] for n in _edges(block.get("displayableSeasons"))]
        return _ok({"seasons": seasons, "episodes": episodes, **_page_info(conn)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_title_reviews(id: str, limit: int = 10, cursor: str | None = None) -> str:
    """Read user reviews of a title, most helpful first.

    Args:
        id: Title id.
        limit: How many reviews per page.
        cursor: next_cursor from the previous page.
    """
    try:
        tid = _tt(id)
        data = _q(
            f"{{ title(id: {_s(tid)}) {{ reviews(first: {int(limit)}, sort: {{by: HELPFULNESS_SCORE, order: DESC}}{_after(cursor)}) {{ total pageInfo {{ hasNextPage endCursor }} "
            f"edges {{ node {{ author {{ nickName }} authorRating summary {{ originalText }} text {{ originalText {{ plainText }} }} submissionDate helpfulness {{ upVotes downVotes }} }} }} }} }} }}"
        )
        conn = data["title"]["reviews"]
        reviews = [
            {
                "author": (n.get("author") or {}).get("nickName"),
                "rating": n.get("authorRating"),
                "summary": (n.get("summary") or {}).get("originalText"),
                "text": ((n.get("text") or {}).get("originalText") or {}).get("plainText"),
                "date": n.get("submissionDate"),
                "helpful": n.get("helpfulness"),
            }
            for n in _edges(conn)
        ]
        return _ok({"reviews": reviews, **_page_info(conn)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_similar_titles(id: str, limit: int = 12) -> str:
    """Titles IMDb lists as more like this one.

    Args:
        id: Title id.
        limit: How many to return.
    """
    try:
        tid = _tt(id)
        data = _q(f"{{ title(id: {_s(tid)}) {{ moreLikeThisTitles(first: {int(limit)}) {{ edges {{ node {{ {_TITLE} }} }} }} }} }}")
        return _ok({"titles": [_title(n) for n in _edges(data["title"]["moreLikeThisTitles"])]})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_box_office(id: str) -> str:
    """Budget, opening weekend and lifetime grosses by area for a title.

    Args:
        id: Title id.
    """
    try:
        tid = _tt(id)
        data = _q(
            f"""{{ title(id: {_s(tid)}) {{ titleText {{ text }}
              productionBudget {{ budget {{ amount currency }} }}
              opening: openingWeekendGross(boxOfficeArea: DOMESTIC) {{ gross {{ total {{ amount currency }} }} weekendEndDate }}
              rankedLifetimeGrosses(first: 20) {{ edges {{ node {{ boxOfficeAreaType {{ code text }} rank total {{ amount currency }} }} }} }}
            }} }}"""
        )
        t = data["title"]
        if not t:
            raise ValueError(f"IMDb has no title {tid}.")
        opening = t.get("opening") or {}
        return _ok({
            "title": t["titleText"]["text"],
            "budget": (t.get("productionBudget") or {}).get("budget"),
            "opening_weekend_domestic": {**((opening.get("gross") or {}).get("total") or {}), "weekend_end": opening.get("weekendEndDate")} if opening else None,
            "lifetime_gross": [
                {"area": n["boxOfficeAreaType"]["text"], "code": n["boxOfficeAreaType"]["code"], "rank": n.get("rank"), **n["total"]}
                for n in _edges(t.get("rankedLifetimeGrosses"))
            ],
        })
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# People
# ------------------------------------------------------------------


@mcp.tool(annotations=_READ)
def search_names(query: str, limit: int = 10) -> str:
    """Find people: actors, directors, writers and crew.

    Args:
        query: Name, or part of one.
        limit: How many matches to return.
    """
    try:
        data = _q(
            f"{{ mainSearch(first: {int(limit)}, options: {{searchTerm: {_s(query)}, type: NAME, includeAdult: false}}) "
            f"{{ edges {{ node {{ entity {{ ... on Name {{ {_NAME} knownFor(first: 3) {{ edges {{ node {{ title {{ id titleText {{ text }} releaseYear {{ year }} }} }} }} }} }} }} }} }} }} }}"
        )
        names = []
        for n in _edges(data["mainSearch"]):
            entity = n["entity"]
            names.append({
                **_name(entity),
                "known_for": [
                    f"{k['title']['titleText']['text']} ({(k['title'].get('releaseYear') or {}).get('year')})"
                    for k in _edges(entity.get("knownFor"))
                ],
            })
        return _ok({"names": names})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_name(id: str) -> str:
    """Get one person: biography, dates, professions, known-for titles.

    Args:
        id: Name id such as nm0000244, or an IMDb URL containing one.
    """
    try:
        nid = _nm(id)
        data = _q(
            f"""{{ name(id: {_s(nid)}) {{ {_NAME}
              birthDate {{ date }} deathDate {{ date }} birthLocation {{ text }}
              bio {{ text {{ plainText }} }} primaryImage {{ url }} meterRanking {{ currentRank }}
              height {{ measurement {{ value unit }} }}
              knownFor(first: 8) {{ edges {{ node {{ title {{ {_TITLE} }} summary {{ principalCharacters {{ name }} }} }} }} }}
              credits(first: 1) {{ total }}
            }} }}"""
        )
        n = data["name"]
        if not n:
            raise ValueError(f"IMDb has no name {nid}.")
        return _ok({
            "name": {
                **_name(n),
                "born": (n.get("birthDate") or {}).get("date"),
                "died": (n.get("deathDate") or {}).get("date"),
                "birthplace": (n.get("birthLocation") or {}).get("text"),
                "bio": ((n.get("bio") or {}).get("text") or {}).get("plainText"),
                "photo": (n.get("primaryImage") or {}).get("url"),
                "popularity_rank": (n.get("meterRanking") or {}).get("currentRank"),
                "known_for": [
                    {**_title(k["title"]), "as": [c["name"] for c in (k.get("summary") or {}).get("principalCharacters") or []]}
                    for k in _edges(n.get("knownFor"))
                ],
                "credit_count": (n.get("credits") or {}).get("total"),
                "url": f"https://www.imdb.com/name/{nid}/",
            }
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_name_credits(id: str, category: str | None = None, limit: int = 50, cursor: str | None = None) -> str:
    """A person's filmography, newest first.

    Args:
        id: Name id.
        category: One of actor, actress, director, writer, producer, composer,
            cinematographer, editor. Omit for everything.
        limit: How many credits per page.
        cursor: next_cursor from the previous page.
    """
    try:
        nid = _nm(id)
        where = f", filter: {{categories: [{_s(category)}]}}" if category else ""
        data = _q(
            f"{{ name(id: {_s(nid)}) {{ credits(first: {int(limit)}{where}{_after(cursor)}) {{ total pageInfo {{ hasNextPage endCursor }} "
            f"edges {{ node {{ category {{ text }} title {{ {_TITLE} }} ... on Cast {{ characters {{ name }} }} }} }} }} }} }}"
        )
        conn = data["name"]["credits"]
        credits = [
            {**_title(n["title"]), "category": n["category"]["text"], "characters": [c["name"] for c in n.get("characters") or []]}
            for n in _edges(conn)
        ]
        return _ok({"credits": credits, **_page_info(conn)})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Discovery
# ------------------------------------------------------------------


@mcp.tool(annotations=_READ)
def advanced_title_search(
    genres: list[str] | None = None,
    title_types: list[str] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    min_votes: int | None = None,
    keywords: list[str] | None = None,
    text: str | None = None,
    sort: str = "popularity",
    order: str = "DESC",
    limit: int = 25,
    cursor: str | None = None,
) -> str:
    """Search titles by genre, type, year, rating, vote count and keywords.

    This is IMDb's advanced search: "best rated horror films of the 1970s with
    over 10 000 votes" is one call.

    Args:
        genres: Genre names as IMDb spells them: Horror, Sci-Fi, Film-Noir, Drama.
        title_types: Any of movie, tvSeries, tvMiniSeries, tvMovie, tvEpisode, short, video, videoGame.
        year_from: Earliest release year.
        year_to: Latest release year.
        min_rating: Lowest IMDb average, 1 to 10.
        max_rating: Highest IMDb average.
        min_votes: Fewest votes; 10000 keeps out the obscure.
        keywords: Plot keywords, all required, such as ["space", "alien"].
        text: Words in the title.
        sort: popularity, rating, votes, release_date, alphabetical, runtime, metascore or box_office.
        order: ASC or DESC.
        limit: How many per page.
        cursor: next_cursor from the previous page.
    """
    try:
        parts = []
        if genres:
            parts.append(f"genreConstraint: {{allGenreIds: [{', '.join(_s(g) for g in genres)}]}}")
        if title_types:
            for t in title_types:
                _check(t, TITLE_TYPES, "title_types")
            parts.append(f"titleTypeConstraint: {{anyTitleTypeIds: [{', '.join(_s(t) for t in title_types)}]}}")
        if year_from or year_to:
            span = (f"start: {_s(f'{year_from}-01-01')}" if year_from else "") + (", " if year_from and year_to else "") + (f"end: {_s(f'{year_to}-12-31')}" if year_to else "")
            parts.append(f"releaseDateConstraint: {{releaseDateRange: {{{span}}}}}")
        ratings = []
        if min_rating is not None or max_rating is not None:
            bounds = (f"min: {float(min_rating)}" if min_rating is not None else "") + (", " if min_rating is not None and max_rating is not None else "") + (f"max: {float(max_rating)}" if max_rating is not None else "")
            ratings.append(f"aggregateRatingRange: {{{bounds}}}")
        if min_votes:
            ratings.append(f"ratingsCountRange: {{min: {int(min_votes)}}}")
        if ratings:
            parts.append(f"userRatingsConstraint: {{{', '.join(ratings)}}}")
        if keywords:
            parts.append(f"keywordConstraint: {{allKeywords: [{', '.join(_s(k) for k in keywords)}]}}")
        if text:
            parts.append(f"titleTextConstraint: {{searchTerm: {_s(text)}}}")
        by = SEARCH_SORTS[_check(sort, tuple(SEARCH_SORTS), "sort")]
        direction = _check(order.upper(), ("ASC", "DESC"), "order")
        data = _q(
            f"{{ advancedTitleSearch(first: {int(limit)}{_after(cursor)}, sort: {{sortBy: {by}, sortOrder: {direction}}}, constraints: {{{', '.join(parts)}}}) "
            f"{{ total pageInfo {{ hasNextPage endCursor }} edges {{ node {{ title {{ {_TITLE} metacritic {{ metascore {{ score }} }} plot {{ plotText {{ plainText }} }} }} }} }} }} }}"
        )
        conn = data["advancedTitleSearch"]
        titles = [
            {**_title(n["title"]), "metascore": ((n["title"].get("metacritic") or {}).get("metascore") or {}).get("score"), "plot": ((n["title"].get("plot") or {}).get("plotText") or {}).get("plainText")}
            for n in _edges(conn)
        ]
        return _ok({"titles": titles, **_page_info(conn)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_chart(chart: str = "top_rated_movies", limit: int = 25, cursor: str | None = None) -> str:
    """IMDb's charts: the Top 250, lowest rated, top TV, most popular.

    Args:
        chart: top_rated_movies, lowest_rated_movies, top_rated_tv_shows,
            top_rated_english_movies, most_popular_movies or most_popular_tv_shows.
        limit: How many entries per page.
        cursor: next_cursor from the previous page.
    """
    try:
        kind = CHARTS[_check(chart, tuple(CHARTS), "chart")]
        data = _q(
            f"{{ chartTitles(first: {int(limit)}{_after(cursor)}, chart: {{chartType: {kind}}}) {{ total pageInfo {{ hasNextPage endCursor }} "
            f"edges {{ node {{ {_TITLE} ratingsSummary {{ topRanking {{ rank }} }} meterRanking {{ currentRank }} }} }} }} }}"
        )
        conn = data["chartTitles"]
        titles = [
            {**_title(n), "top_250_rank": ((n.get("ratingsSummary") or {}).get("topRanking") or {}).get("rank"), "popularity_rank": (n.get("meterRanking") or {}).get("currentRank")}
            for n in _edges(conn)
        ]
        return _ok({"chart": chart, "titles": titles, **_page_info(conn)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_coming_soon(kind: str = "movie", from_date: str | None = None, limit: int = 25, cursor: str | None = None) -> str:
    """Upcoming releases, soonest first.

    Args:
        kind: movie or tv.
        from_date: YYYY-MM-DD. Defaults to today.
        limit: How many per page.
        cursor: next_cursor from the previous page.
    """
    try:
        what = _check(kind, ("movie", "tv"), "kind").upper()
        start = from_date or time.strftime("%Y-%m-%d")
        data = _q(
            f"{{ comingSoon(first: {int(limit)}{_after(cursor)}, comingSoonType: {what}, releasingOnOrAfter: {_s(start)}) {{ pageInfo {{ hasNextPage endCursor }} "
            f"edges {{ node {{ {_TITLE} releaseDate {{ day month year }} plot {{ plotText {{ plainText }} }} }} }} }} }}"
        )
        conn = data["comingSoon"]
        titles = []
        for n in _edges(conn):
            rd = n.get("releaseDate") or {}
            titles.append({**_title(n), "release_date": f"{rd['year']}-{rd['month']:02d}-{rd['day']:02d}" if rd.get("day") else None, "plot": ((n.get("plot") or {}).get("plotText") or {}).get("plainText")})
        return _ok({"titles": titles, **_page_info(conn)})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# The configured user
# ------------------------------------------------------------------

_RATING_NODE = f"title {{ {_TITLE} }} userRating {{ value date }}"


def _rating_row(node: dict) -> dict:
    rating = node.get("userRating") or {}
    return {**_title(node.get("title")), "my_rating": rating.get("value"), "rated_on": (rating.get("date") or "")[:10] or None}


def _ratings_page(first: int, sort: str, order: str, cursor: str | None) -> dict:
    user = _get_client().user_id()
    data = _q(
        f"{{ userRatings(first: {int(first)}, userId: {_s(user)}, sort: {{by: {sort}, order: {order}}}{_after(cursor)}) "
        f"{{ total pageInfo {{ hasNextPage endCursor }} edges {{ node {{ {_RATING_NODE} }} }} }} }}"
    )
    return data["userRatings"]


def _all_ratings() -> list[dict]:
    """Every rating the user has given, fetched once and kept for RATINGS_TTL."""
    global _ratings_cache
    now = time.monotonic()
    if _ratings_cache and now - _ratings_cache[0] < RATINGS_TTL:
        return _ratings_cache[1]

    rows, cursor = [], None
    while True:
        conn = _ratings_page(RATINGS_PAGE, "MOST_RECENT", "DESC", cursor)
        rows.extend(_rating_row(n) for n in _edges(conn))
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info["endCursor"]
    _ratings_cache = (now, rows)
    return rows


@mcp.tool(annotations=_READ)
def my_ratings(sort: str = "recent", order: str = "DESC", limit: int = 50, cursor: str | None = None) -> str:
    """The configured user's own ratings, one page at a time.

    For questions over the whole history use find_my_ratings or my_rating_stats.

    Args:
        sort: recent (by date rated) or top (by the rating given).
        order: DESC or ASC.
        limit: How many per page, at most 250.
        cursor: next_cursor from the previous page.
    """
    try:
        by = RATING_SORTS[_check(sort, tuple(RATING_SORTS), "sort")]
        direction = _check(order.upper(), ("ASC", "DESC"), "order")
        conn = _ratings_page(min(int(limit), RATINGS_PAGE), by, direction, cursor)
        return _ok({"ratings": [_rating_row(n) for n in _edges(conn)], **_page_info(conn)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def my_rating_for(id: str) -> str:
    """What the configured user rated one title, if anything.

    Args:
        id: Title id, from search_titles.
    """
    try:
        tid = _tt(id)
        row = next((r for r in _all_ratings() if r["id"] == tid), None)
        return _ok({"id": tid, "rated": row is not None, **({"rating": row} if row else {})})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def find_my_ratings(
    query: str | None = None,
    year: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    rating: int | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    genre: str | None = None,
    title_type: str | None = None,
    rated_from: str | None = None,
    rated_to: str | None = None,
    sort: str = "rated_on",
    order: str = "DESC",
    limit: int = 100,
) -> str:
    """Search the configured user's whole ratings history.

    Every filter is optional and they combine: "my 9s and 10s from the 1990s",
    "horror I rated below 4", "everything I rated in 2024".

    Args:
        query: Words in the title.
        year: Release year.
        year_from: Earliest release year.
        year_to: Latest release year.
        rating: Exactly this rating, 1 to 10.
        min_rating: At least this rating.
        max_rating: At most this rating.
        genre: Genre name such as Horror or Comedy.
        title_type: movie, tvSeries, tvEpisode and so on.
        rated_from: Earliest date rated, YYYY-MM-DD.
        rated_to: Latest date rated, YYYY-MM-DD.
        sort: rated_on, my_rating, imdb_rating, year or title.
        order: DESC or ASC.
        limit: How many to return.
    """
    try:
        key = _check(sort, ("rated_on", "my_rating", "imdb_rating", "year", "title"), "sort")
        reverse = _check(order.upper(), ("ASC", "DESC"), "order") == "DESC"
        words = (query or "").lower().split()
        rows = [
            r for r in _all_ratings()
            if all(w in f"{r['title']} {r['original_title']}".lower() for w in words)
            and (year is None or r["year"] == year)
            and (year_from is None or (r["year"] or 0) >= year_from)
            and (year_to is None or (r["year"] or 9999) <= year_to)
            and (rating is None or r["my_rating"] == rating)
            and (min_rating is None or (r["my_rating"] or 0) >= min_rating)
            and (max_rating is None or (r["my_rating"] or 0) <= max_rating)
            and (genre is None or genre.lower() in [g.lower() for g in r["genres"]])
            and (title_type is None or r["type"] == title_type)
            and (rated_from is None or (r["rated_on"] or "") >= rated_from)
            and (rated_to is None or (r["rated_on"] or "") <= rated_to)
        ]
        rows.sort(key=lambda r: (r[key] is None, r[key] if r[key] is not None else 0), reverse=reverse)
        return _ok({"total": len(rows), "ratings": rows[: int(limit)]})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def my_rating_stats() -> str:
    """Numbers over the configured user's whole ratings history: how many,
    the average, the spread from 1 to 10, ratings per year, favourite genres
    and decades, and where they sit against IMDb's averages."""
    try:
        rows = _all_ratings()
        rated = [r for r in rows if r["my_rating"]]
        spread = Counter(r["my_rating"] for r in rated)
        per_year = Counter((r["rated_on"] or "")[:4] for r in rated if r["rated_on"])
        decades = Counter(f"{r['year'] // 10 * 10}s" for r in rated if r["year"])
        genres: Counter = Counter()
        genre_sum: Counter = Counter()
        for r in rated:
            for g in r["genres"]:
                genres[g] += 1
                genre_sum[g] += r["my_rating"]
        with_imdb = [r for r in rated if r["imdb_rating"]]
        diff = sum(r["my_rating"] - r["imdb_rating"] for r in with_imdb) / len(with_imdb) if with_imdb else None
        return _ok({
            "total": len(rated),
            "average": round(sum(r["my_rating"] for r in rated) / len(rated), 2) if rated else None,
            "spread": {str(v): spread.get(v, 0) for v in range(1, 11)},
            "per_year_rated": dict(sorted(per_year.items())),
            "per_decade_released": dict(sorted(decades.items())),
            "genres": [
                {"genre": g, "count": n, "average": round(genre_sum[g] / n, 2)}
                for g, n in genres.most_common()
            ],
            "types": dict(Counter(r["type"] for r in rated).most_common()),
            "average_difference_to_imdb": round(diff, 2) if diff is not None else None,
            "first_rated": min((r["rated_on"] for r in rated if r["rated_on"]), default=None),
            "last_rated": max((r["rated_on"] for r in rated if r["rated_on"]), default=None),
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def my_watchlist(limit: int = 50, cursor: str | None = None) -> str:
    """Titles on the configured user's watchlist, in list order.

    Args:
        limit: How many per page.
        cursor: next_cursor from the previous page.
    """
    try:
        user = _get_client().user_id()
        data = _q(f"{{ predefinedList(classType: WATCH_LIST, userId: {_s(user)}) {{ id }} }}")
        list_id = (data.get("predefinedList") or {}).get("id")
        if not list_id:
            raise ValueError("IMDb shows no public watchlist for this user. The watchlist is private unless set public on imdb.com.")
        return _list_page(list_id, limit, cursor)
    except Exception as e:
        return _err(e)


def _list_page(list_id: str, limit: int, cursor: str | None) -> str:
    data = _q(
        f"{{ list(id: {_s(list_id)}) {{ id name {{ originalText }} description {{ originalText {{ plainText }} }} author {{ userId username {{ text }} }} "
        f"items(first: {int(limit)}{_after(cursor)}) {{ total pageInfo {{ hasNextPage endCursor }} edges {{ node {{ absolutePosition createdDate listItem {{ ... on Title {{ {_TITLE} }} ... on Name {{ {_NAME} }} }} }} }} }} }} }}"
    )
    lst = data.get("list")
    if not lst:
        raise ValueError(f"IMDb has no public list {list_id}.")
    items = []
    for n in _edges(lst["items"]):
        entity = n.get("listItem") or {}
        row = _title(entity) if "titleText" in entity else _name(entity)
        items.append({**(row or {}), "position": n.get("absolutePosition"), "added_on": (n.get("createdDate") or "")[:10] or None})
    return _ok({
        "list": {
            "id": lst["id"],
            "name": (lst.get("name") or {}).get("originalText"),
            "description": ((lst.get("description") or {}).get("originalText") or {}).get("plainText"),
            "author": ((lst.get("author") or {}).get("username") or {}).get("text"),
        },
        "items": items,
        **_page_info(lst["items"]),
    })


@mcp.tool(annotations=_READ)
def get_list(id: str, limit: int = 50, cursor: str | None = None) -> str:
    """Any public IMDb list by its ls id, titles or people.

    Args:
        id: List id such as ls055592025, or a URL containing one.
        limit: How many per page.
        cursor: next_cursor from the previous page.
    """
    try:
        match = re.search(r"ls\d+", id or "")
        if not match:
            raise ValueError(f"Not a list id: {id!r}. Expected ls followed by digits.")
        return _list_page(match.group(0), limit, cursor)
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=_READ)
def get_user_profile() -> str:
    """Who the configured user is on IMDb, with their ratings and watchlist counts."""
    try:
        client = _get_client()
        user = client.user_id()
        data = _q(
            f"{{ userRatings(first: 1, userId: {_s(user)}) {{ total }} "
            f"predefinedList(classType: WATCH_LIST, userId: {_s(user)}) {{ id titleListItemSearch(first: 1) {{ total }} }} }}"
        )
        profile = {"user_id": user}
        profile_id = os.getenv("IMDB_PROFILE_ID", "")
        if profile_id.startswith("p."):
            who = _q(f"{{ userProfile(input: {{profileId: {_s(profile_id)}}}) {{ username {{ text }} bio {{ text {{ plainText }} }} creationDate }} }}")["userProfile"] or {}
            profile.update({
                "username": (who.get("username") or {}).get("text"),
                "bio": ((who.get("bio") or {}).get("text") or {}).get("plainText"),
                "member_since": (who.get("creationDate") or "")[:10] or None,
                "url": f"https://www.imdb.com/user/{profile_id}/",
            })
        watchlist = data.get("predefinedList") or {}
        profile["ratings"] = (data.get("userRatings") or {}).get("total")
        profile["watchlist"] = (watchlist.get("titleListItemSearch") or {}).get("total")
        profile["watchlist_id"] = watchlist.get("id")
        return _ok({"profile": profile})
    except Exception as e:
        return _err(e)


def main() -> None:
    import argparse

    from dotenv import find_dotenv, load_dotenv

    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path and load_dotenv(dotenv_path, override=False):
        logger.info("Loaded .env from %s", dotenv_path)

    parser = argparse.ArgumentParser(prog="imdb-mcp")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.getenv("MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8620")))
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    if args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit(
            f"refusing to listen on {args.host}: this server has no login of "
            "its own. Keep it on the local machine and put a proxy in front."
        )

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    logger.info("Listening on http://%s:%d/mcp", args.host, args.port)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
