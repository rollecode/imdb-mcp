"""Client for the GraphQL API behind imdb.com.

It is the same API IMDb's own pages call. Public title, name, chart and search
data needs no login, and so do another user's public ratings and watchlist
when their user id is known. Anything about the signed-in user is refused
without IMDb's own session cookies, which this client never has.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.graphql.imdb.com/"

# Requests without the client name the site itself sends are answered 403.
CLIENT_NAME = "imdb-web-next-localized"

ENV_USER_ID = "IMDB_USER_ID"
ENV_PROFILE_ID = "IMDB_PROFILE_ID"

# A profile URL carries p.<letters>; the API wants the ur<digits> behind it.
PROFILE_PREFIX = "p."
USER_PREFIX = "ur"


class ImdbError(RuntimeError):
    """One or more errors in a GraphQL response."""

    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages))
        self.messages = messages

    @property
    def needs_login(self) -> bool:
        return any("Authentication required" in m for m in self.messages)


class ImdbClient:
    def __init__(self, timeout: float = 30.0):
        self._http = httpx.Client(
            headers={
                "Content-Type": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "x-imdb-client-name": CLIENT_NAME,
            },
            timeout=timeout,
        )
        self._user_id: str | None = None

    def query(self, query: str) -> dict:
        """Run one GraphQL document and return its data."""
        response = self._http.post(ENDPOINT, json={"query": query})
        if response.is_error:
            # A rejected document still carries GraphQL errors worth reading.
            try:
                errors = response.json().get("errors") or []
            except ValueError:
                errors = []
            if errors:
                raise ImdbError([e.get("message", "unknown error") for e in errors])
        response.raise_for_status()
        body = response.json()

        errors = body.get("errors") or []
        if errors:
            raise ImdbError([e.get("message", "unknown error") for e in errors])
        return body["data"]

    def user_id(self) -> str:
        """The ur... id of the configured user, resolved from a profile id if needed."""
        if self._user_id:
            return self._user_id

        configured = (os.getenv(ENV_USER_ID) or "").strip()
        profile = (os.getenv(ENV_PROFILE_ID) or "").strip()
        if not configured and profile.startswith(USER_PREFIX):
            configured = profile
        if configured.startswith(USER_PREFIX):
            self._user_id = configured
            return configured

        if not profile:
            raise ImdbError([
                (
                    f"No IMDb user configured. Set {ENV_USER_ID} (ur...) or "
                    f"{ENV_PROFILE_ID} (the p.... part of your profile URL)."
                )
            ])

        data = self.query(
            f"{{ userProfile(input: {{profileId: {json.dumps(profile)}}}) {{ userId }} }}"
        )
        user = (data.get("userProfile") or {}).get("userId")
        if not user:
            raise ImdbError([f"IMDb knows no profile {profile!r}."])
        self._user_id = user
        return user
