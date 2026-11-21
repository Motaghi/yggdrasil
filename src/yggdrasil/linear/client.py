"""Linear GraphQL client: one transport, error classification on extensions.code, Retry-After, deadlines.

Transports:
- real: HTTPS to `linear_api_url` (default https://api.linear.app), which must be an https URL on a
  linear.app host;
- `YGG_LINEAR_FAKE=<dir>` with `<dir>/url` (A5.8): plain HTTP to the suite's fake server named there, honoured
  only under `YGG_TEST_MODE=1` and only for a loopback address, so tests never share the credential path of a
  real installation.

No request follows a redirect: a credential or an Authorization header is never resent to another origin.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .. import config, util
from ..util import YggError

DEFAULT_API = "https://api.linear.app"


@dataclass
class Response:
    status: int
    headers: dict
    body: object


class LinearError(YggError):
    """A classified Linear failure. `kind` is auth, forbidden, ratelimited, transient, invalid or unreachable."""

    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(util.EXIT_TRACKER, "linear " + kind + ": " + message)
        self.kind = kind
        self.retry_after = retry_after
        self.status: int | None = None  # the HTTP status, when Linear answered


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def fake_dir():
    """The suite's fake directory (A5.8), honoured in test mode only."""
    from pathlib import Path
    value = util.env("YGG_LINEAR_FAKE")
    return Path(value) if value and util.test_mode() else None


def _origin_problem(url: str, *, fake: bool) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if fake:
        if parsed.scheme != "http" or host not in LOOPBACK:
            return "the test fake must be a loopback http address, not " + url
        return None
    if parsed.scheme != "https":
        return "linear_api_url must use https, not " + url
    if host != "linear.app" and not host.endswith(".linear.app"):
        return "linear_api_url must name a linear.app host, not " + (host or url)
    if parsed.username or parsed.password:
        return "linear_api_url must not carry credentials"
    return None


def api_url() -> str:
    """The approved destination: A5.8's local fake server in test mode, else an https linear.app origin."""
    directory = fake_dir()
    if directory is not None and (directory / "url").is_file():
        url, fake = (directory / "url").read_text().strip().rstrip("/"), True
    else:
        url, fake = str(config.get("linear_api_url") or DEFAULT_API).rstrip("/"), False
    problem = _origin_problem(url, fake=fake)
    if problem:
        raise LinearError("invalid", problem)
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect is refused: an authenticated request is never replayed to another location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused: " + str(newurl), headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def deadline_seconds() -> float:
    return float(config.get("linear_deadline_seconds", 30))


class Transport:
    """Sends one HTTP POST and returns a Response. Network failures raise LinearError('unreachable')."""

    def post(self, path: str, body, *, form: bool = False, headers: dict | None = None) -> Response:
        directory = fake_dir()
        if directory is not None and not (directory / "url").is_file():
            raise LinearError("unreachable", "YGG_LINEAR_FAKE names no fake server (" + str(directory) + "/url)")
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        request = urllib.request.Request(api_url() + path, data=data, method="POST", headers=dict(
            {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
             "User-Agent": "yggdrasil"}, **(headers or {})))
        try:
            with _OPENER.open(request, timeout=deadline_seconds()) as response:
                status, raw_headers, payload = response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            if 300 <= int(error.code) < 400:
                raise LinearError("invalid", "Linear answered with a redirect (HTTP " + str(error.code) +
                                  "), which is never followed with credentials")
            status, raw_headers, payload = error.code, dict(error.headers or {}), error.read()
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise LinearError("unreachable", str(error))
        try:
            parsed = json.loads(payload or b"{}")
        except ValueError:
            parsed = {"_raw": payload.decode(errors="replace")[:500]}
        return Response(status, {k.lower(): v for k, v in raw_headers.items()}, parsed)


def classify(response: Response) -> LinearError | None:
    """Classify on `errors[].extensions.code` first, then on the HTTP status (probe facts: a forbidden
    operation arrives as HTTP 400 with statusCode 403 inside; a bad token is 401 AUTHENTICATION_ERROR). The
    error keeps the HTTP status, so a caller can tell a request refused before it ran (4xx) from an error
    reported with HTTP 200, which a mutation may have committed."""
    error = _classify(response)
    if error is not None:
        error.status = response.status
    return error


def _classify(response: Response) -> LinearError | None:
    body = response.body if isinstance(response.body, dict) else {}
    errors = body.get("errors") or []
    codes = [str((e.get("extensions") or {}).get("code") or "") for e in errors if isinstance(e, dict)]
    message = "; ".join(str(e.get("message")) for e in errors if isinstance(e, dict)) or ("HTTP " + str(response.status))
    retry = response.headers.get("retry-after")
    retry_after = float(retry) if retry and str(retry).replace(".", "", 1).isdigit() else None
    if "AUTHENTICATION_ERROR" in codes or response.status == 401:
        return LinearError("auth", message)
    if "RATELIMITED" in codes or response.status == 429:
        return LinearError("ratelimited", message, retry_after if retry_after is not None else 1.0)
    if "FORBIDDEN" in codes or response.status == 403:
        return LinearError("forbidden", message)
    if response.status >= 500 or response.status == 503:
        return LinearError("transient", message, retry_after)
    if errors or response.status >= 400:
        return LinearError("invalid", message)
    return None


class Client:
    """GraphQL client. `token_source()` returns a token; `on_auth_failure()` renews it once."""

    def __init__(self, token_source, on_auth_failure=None, transport: Transport | None = None, sleep=time.sleep):
        self.token_source = token_source
        self.on_auth_failure = on_auth_failure
        self.transport = transport or Transport()
        self.sleep = sleep
        self.waits: list[float] = []
        self.last_rate_limit: dict = {}

    def execute(self, operation: str, query: str, variables: dict | None = None) -> dict:
        body = {"operationName": operation, "query": query, "variables": variables or {}}
        renewed = False
        retried = False
        while True:
            response = self.transport.post("/graphql", body, headers={"Authorization": "Bearer " + self.token_source()})
            self.last_rate_limit = {k: v for k, v in response.headers.items() if k.startswith("x-ratelimit")}
            error = classify(response)
            if error is None:
                return (response.body or {}).get("data") or {}
            if error.kind == "auth" and not renewed and self.on_auth_failure is not None:
                renewed = True
                self.on_auth_failure()
                continue
            mutation = query.lstrip().startswith("mutation")
            if error.kind == "transient" and mutation:
                # A mutation may have committed before the failure: never repeat it blindly. The caller's next
                # attempt looks the object up by its Yggdrasil-owned identifier before creating it again.
                raise LinearError("ambiguous", operation + " may or may not have been applied: " + error.message)
            if error.kind in ("ratelimited", "transient") and not retried:
                retried = True
                wait = max(error.retry_after if error.retry_after is not None else 1.0, 0.0)
                if wait > deadline_seconds():
                    raise LinearError("ratelimited", operation + ": Retry-After " + str(int(wait)) +
                                      " s exceeds the " + str(int(deadline_seconds())) + " s deadline")
                self.waits.append(wait)
                self.sleep(wait)
                continue
            raise error

    def pages(self, operation: str, query: str, variables: dict, path: list[str]) -> list:
        """Read every page of a connection at `path` (the query must take `$after` and select pageInfo)."""
        nodes, after = [], None
        for _ in range(10_000):
            data = self.execute(operation, query, dict(variables, after=after))
            connection = data
            for key in path:
                connection = (connection or {}).get(key) or {}
            nodes.extend(connection.get("nodes") or [])
            info = connection.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return nodes
            after = info.get("endCursor")
        raise LinearError("invalid", "pagination did not end for " + operation)
