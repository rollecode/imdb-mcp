import json

import httpx
import pytest

from imdb_mcp.client import CLIENT_NAME, ENDPOINT, ImdbClient, ImdbError


def _client(reply, status=200):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(status, json=reply)

    client = ImdbClient()
    client._http = httpx.Client(transport=httpx.MockTransport(handler), headers=client._http.headers)
    return client, seen


def test_a_query_posts_with_the_site_client_name():
    client, seen = _client({"data": {"title": {"id": "tt1"}}})
    assert client.query('{ title(id: "tt1") { id } }') == {"title": {"id": "tt1"}}
    assert seen["url"] == ENDPOINT
    assert seen["headers"]["x-imdb-client-name"] == CLIENT_NAME
    assert seen["body"] == {"query": '{ title(id: "tt1") { id } }'}


def test_graphql_errors_become_one_exception():
    client, _ = _client({"errors": [{"message": "FORBIDDEN: Authentication required but not provided"}]})
    with pytest.raises(ImdbError) as raised:
        client.query("{ x }")
    assert raised.value.needs_login


def test_a_rejected_document_still_reports_its_errors():
    client, _ = _client({"errors": [{"message": "Cannot query field \"zz\" on type \"Query\"."}]}, status=400)
    with pytest.raises(ImdbError, match="Cannot query field"):
        client.query("{ zz }")


def test_user_id_is_taken_from_the_environment(monkeypatch):
    monkeypatch.setenv("IMDB_USER_ID", "ur123")
    client, seen = _client({})
    assert client.user_id() == "ur123"
    assert "body" not in seen


def test_profile_id_is_resolved_once(monkeypatch):
    monkeypatch.delenv("IMDB_USER_ID", raising=False)
    monkeypatch.setenv("IMDB_PROFILE_ID", "p.abc")
    client, seen = _client({"data": {"userProfile": {"userId": "ur9"}}})
    assert client.user_id() == "ur9"
    assert 'profileId: "p.abc"' in seen["body"]["query"]
    seen.clear()
    assert client.user_id() == "ur9"
    assert seen == {}


def test_missing_configuration_says_what_to_set(monkeypatch):
    monkeypatch.delenv("IMDB_USER_ID", raising=False)
    monkeypatch.delenv("IMDB_PROFILE_ID", raising=False)
    client, _ = _client({})
    with pytest.raises(ImdbError, match="IMDB_USER_ID"):
        client.user_id()
