"""Call real tools over a fake IMDb and check the queries they build and the shapes they return."""

import asyncio
import json

import httpx
import pytest

from imdb_mcp import server
from imdb_mcp.client import ImdbClient

TITLE = {
    "id": "tt0078748",
    "titleText": {"text": "Alien"},
    "originalTitleText": {"text": "Alien"},
    "releaseYear": {"year": 1979, "endYear": None},
    "titleType": {"id": "movie", "text": "Movie"},
    "runtime": {"seconds": 7020},
    "ratingsSummary": {"aggregateRating": 8.4, "voteCount": 1081110},
    "titleGenres": {"genres": [{"genre": {"text": "Horror"}}, {"genre": {"text": "Sci-Fi"}}]},
}


def _rating(i, value, date, year=1979, genre="Horror"):
    return {
        "node": {
            "title": {**TITLE, "id": f"tt{i:07d}", "titleText": {"text": f"Film {i}"}, "releaseYear": {"year": year}, "titleGenres": {"genres": [{"genre": {"text": genre}}]}},
            "userRating": {"value": value, "date": f"{date}T12:00:00Z"},
        }
    }


@pytest.fixture(autouse=True)
def imdb(monkeypatch):
    monkeypatch.setenv("IMDB_USER_ID", "ur12345678")
    monkeypatch.delenv("IMDB_PROFILE_ID", raising=False)
    server._client = None
    server._ratings_cache = None
    seen: dict = {"queries": []}

    def handler(request):
        query = json.loads(request.content)["query"]
        seen["queries"].append(query)
        if "mainSearch" in query:
            data = {"mainSearch": {"edges": [{"node": {"entity": TITLE}}]}}
        elif "userRatings(first: 250" in query:
            second = "after:" in query
            data = {"userRatings": {
                "total": 3,
                "pageInfo": {"hasNextPage": not second, "endCursor": "c1"},
                "edges": [_rating(3, 4, "2024-03-01", 2001, "Comedy")] if second
                else [_rating(1, 10, "2025-01-01"), _rating(2, 7, "2025-02-01", 1986)],
            }}
        elif "userProfile" in query:
            data = {"userProfile": {"username": {"text": "film-fan-7"}, "bio": {"text": {"plainText": "Films."}}, "creationDate": "2009-01-02T00:00:00Z"}}
        elif "userRatings(first: 1" in query:
            data = {"userRatings": {"total": 3}, "predefinedList": {"id": "ls1", "titleListItemSearch": {"total": 14}}}
        elif "advancedTitleSearch" in query:
            data = {"advancedTitleSearch": {"total": 1, "pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": [{"node": {"title": TITLE}}]}}
        else:
            data = {"title": {**TITLE, "principalCredits": [], "taglines": {"edges": []}}}
        return httpx.Response(200, json={"data": data})

    client = ImdbClient()
    client._http = httpx.Client(transport=httpx.MockTransport(handler), headers=client._http.headers)
    server._client = client
    yield seen
    server._client = None
    server._ratings_cache = None


def test_search_escapes_the_term_and_flattens_titles(imdb):
    out = json.loads(server.search_titles('say "hi"', title_type="movie"))
    assert 'searchTerm: "say \\"hi\\""' in imdb["queries"][0]
    assert "titleSearchOptions: {type: MOVIE}" in imdb["queries"][0]
    assert out["titles"][0] == {
        "id": "tt0078748", "title": "Alien", "original_title": "Alien", "year": 1979, "end_year": None,
        "type": "movie", "runtime_minutes": 117, "imdb_rating": 8.4, "votes": 1081110, "genres": ["Horror", "Sci-Fi"],
    }


def test_get_title_accepts_a_url_and_adds_its_own(imdb):
    out = json.loads(server.get_title("https://www.imdb.com/title/tt0078748/?ref_=x"))
    assert 'title(id: "tt0078748")' in imdb["queries"][0]
    assert out["title"]["url"] == "https://www.imdb.com/title/tt0078748/"


def test_a_bad_id_is_refused_before_any_request(imdb):
    out = json.loads(server.get_title("Alien"))
    assert out["status"] == "error" and "tt0078748" in out["message"]
    assert imdb["queries"] == []


def test_the_ratings_history_is_fetched_once_and_reused(imdb):
    first = json.loads(server.my_rating_for("tt0000001"))
    second = json.loads(server.my_rating_for("tt0000009"))
    assert first["rated"] and first["rating"]["my_rating"] == 10
    assert not second["rated"]
    assert sum("userRatings(first: 250" in q for q in imdb["queries"]) == 2


def test_find_my_ratings_combines_filters_and_sorts(imdb):
    out = json.loads(server.find_my_ratings(min_rating=5, year_to=1990, sort="my_rating", order="ASC"))
    assert [r["my_rating"] for r in out["ratings"]] == [7, 10]
    assert out["total"] == 2
    assert json.loads(server.find_my_ratings(genre="comedy"))["ratings"][0]["title"] == "Film 3"
    assert json.loads(server.find_my_ratings(query="film 2"))["total"] == 1


def test_my_rating_stats_summarises_the_history(imdb):
    out = json.loads(server.my_rating_stats())
    assert out["total"] == 3
    assert out["average"] == 7.0
    assert out["spread"]["10"] == 1 and out["spread"]["5"] == 0
    assert out["per_year_rated"] == {"2024": 1, "2025": 2}
    assert out["per_decade_released"] == {"1970s": 1, "1980s": 1, "2000s": 1}
    assert out["genres"][0] == {"genre": "Horror", "count": 2, "average": 8.5}
    assert out["first_rated"] == "2024-03-01" and out["last_rated"] == "2025-02-01"


def test_advanced_search_builds_every_constraint(imdb):
    out = json.loads(server.advanced_title_search(
        genres=["Horror"], title_types=["movie"], year_from=1970, year_to=1979,
        min_rating=7, min_votes=10000, keywords=["space"], text="alien", sort="rating",
    ))
    q = imdb["queries"][0]
    assert 'genreConstraint: {allGenreIds: ["Horror"]}' in q
    assert 'titleTypeConstraint: {anyTitleTypeIds: ["movie"]}' in q
    assert 'releaseDateRange: {start: "1970-01-01", end: "1979-12-31"}' in q
    assert "userRatingsConstraint: {aggregateRatingRange: {min: 7.0}, ratingsCountRange: {min: 10000}}" in q
    assert 'keywordConstraint: {allKeywords: ["space"]}' in q
    assert "sort: {sortBy: USER_RATING, sortOrder: DESC}" in q
    assert out["titles"][0]["title"] == "Alien" and out["next_cursor"] is None


def test_unknown_choices_are_named(imdb):
    assert "chart must be one of" in json.loads(server.get_chart("top_500"))["message"]
    assert "title_types" in json.loads(server.advanced_title_search(title_types=["film"]))["message"]


def test_the_profile_needs_no_profile_id(imdb):
    out = json.loads(server.get_user_profile())
    assert out["profile"] == {"user_id": "ur12345678", "ratings": 3, "watchlist": 14, "watchlist_id": "ls1"}


def test_the_profile_adds_who_when_a_profile_id_is_set(imdb, monkeypatch):
    monkeypatch.setenv("IMDB_PROFILE_ID", "p.abc")
    out = json.loads(server.get_user_profile())["profile"]
    assert out["username"] == "film-fan-7" and out["bio"] == "Films." and out["member_since"] == "2009-01-02"
    assert out["url"] == "https://www.imdb.com/user/p.abc/"


def test_every_tool_registers():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert len(names) == 21
    assert {"my_rating_for", "find_my_ratings", "my_watchlist", "get_chart", "get_result_page"} <= names
