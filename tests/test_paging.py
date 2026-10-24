import json

from imdb_mcp import paging


def _items(n):
    return [{"id": i, "title": f"Film {i}", "blob": "x" * 500, "meta": {"year": 1970 + i % 50}} for i in range(n)]


def _read(text):
    data = json.loads(text)
    assert len(text) <= paging.BUDGET
    return data


def test_small_results_pass_through_whole():
    data = _read(paging.shape([{"id": 1}]))
    assert data == {"status": "success", "result": [{"id": 1}]}


def test_paging_reaches_every_item_exactly_once():
    data = _read(paging.shape(_items(2000)))
    seen = [i["id"] for i in data["result"]]
    rid = data["paging"]["result_id"]
    while data["paging"]["next_offset"] is not None:
        data = _read(paging.get_result_page(rid, offset=data["paging"]["next_offset"]))
        seen += [i["id"] for i in data["result"]]
    assert seen == list(range(2000))


def test_fields_and_match_narrow_the_page():
    rid = _read(paging.shape(_items(2000)))["paging"]["result_id"]
    data = _read(paging.get_result_page(rid, fields=["title", "meta.year"], match={"meta.year": "1979"}))
    assert data["paging"]["total"] == 40
    assert data["result"][0] == {"title": "Film 9", "meta.year": 1979}
    assert data["paging"]["next_offset"] is None


def test_nested_list_keeps_its_envelope():
    result = {"MediaContainer": {"size": 2000, "Metadata": _items(2000)}}
    data = _read(paging.shape(result, {"pagination": {"page": 1}}))
    assert data["paging"]["path"] == "result.MediaContainer.Metadata"
    assert data["result"]["MediaContainer"]["size"] == 2000
    assert data["pagination"] == {"page": 1}


def test_limit_still_offers_the_rest():
    rid = _read(paging.shape(_items(2000)))["paging"]["result_id"]
    data = _read(paging.get_result_page(rid, limit=5))
    assert data["paging"]["returned"] == 5
    assert data["paging"]["next_offset"] == 5


def test_text_without_a_list_is_paged_by_characters():
    data = _read(paging.shape({"doc": "y" * 300_000}))
    rid, text = data["paging"]["result_id"], data["partial_json"]
    while data["paging"]["next_offset"] is not None:
        data = _read(paging.get_result_page(rid, offset=data["paging"]["next_offset"]))
        text += data["partial_json"]
    assert json.loads(text)["result"] == {"doc": "y" * 300_000}


def test_quoted_text_pages_stay_within_budget():
    data = _read(paging.fit({"doc": '"' * 300_000}))
    assert data["paging"]["next_offset"] is not None


def test_json_text_is_parsed_and_paged_as_a_list():
    data = _read(paging.shape(json.dumps(_items(2000))))
    assert data["paging"]["total"] == 2000


def test_every_big_list_is_reachable_by_path():
    budget = {"data": {"budget": {"name": "Home", "transactions": _items(2000), "payees": _items(1500), "flags": [1, 2]}}}
    data = _read(paging.fit(budget))
    lists = data["paging"]["lists"]
    assert lists == {"data.budget.transactions": 2000, "data.budget.payees": 1500}
    assert data["data"]["budget"]["flags"] == [1, 2]
    assert data["data"]["budget"]["name"] == "Home"
    rid = data["paging"]["result_id"]
    page = _read(paging.get_result_page(rid, path="data.budget.payees", fields=["id"]))
    assert page["paging"]["total"] == 1500
    assert page["data"]["budget"]["transactions"] == []


def test_expired_result_says_so():
    assert json.loads(paging.get_result_page("nope"))["status"] == "error"

