import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from .models import ProviderIssue

TIMEOUT = 15
USER_AGENT = "Quota/1.0 (Linux)"

# No cookie jar and no cache: every request carries only the headers given here.
_opener = urllib.request.build_opener()


def get(url: str, headers: dict[str, str]) -> bytes:
    return _send(urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers}))


def post_form(url: str, fields: dict[str, str]) -> bytes:
    body = urllib.parse.urlencode(fields).encode()
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    return _send(urllib.request.Request(url, data=body, headers=headers, method="POST"))


def _send(request: urllib.request.Request) -> bytes:
    try:
        with _opener.open(request, timeout=TIMEOUT) as response:
            status, data = response.status, response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            data = error.read()
        except OSError:
            data = b""
    except (urllib.error.URLError, OSError, ValueError):
        raise ProviderIssue("network") from None

    debug = os.environ.get("QUOTA_DEBUG")
    if debug and (debug == "verbose" or not 200 <= status < 300):
        body = data[: 4_000 if debug == "verbose" else 300].decode("utf-8", "replace")
        host = urllib.parse.urlsplit(request.full_url).hostname or ""
        print(f"[{host}] HTTP {status} body={body}", file=sys.stderr)

    if 200 <= status < 300:
        return data
    if status == 429:
        raise ProviderIssue("rateLimited")
    raise ProviderIssue("http", status=status)
