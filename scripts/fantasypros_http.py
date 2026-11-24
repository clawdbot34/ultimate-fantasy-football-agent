"""Build requests that keep the FantasyPros key on its intended endpoint."""

from urllib.parse import urlsplit
from urllib.request import Request


def fantasypros_request(url: str, api_key: str, *, user_agent: str) -> Request:
    endpoint = urlsplit(url)
    if endpoint.scheme != "https" or endpoint.netloc != "api.fantasypros.com":
        raise ValueError("FantasyPros requests require the official HTTPS API host")
    if not api_key.isascii() or not api_key.isprintable():
        raise ValueError("FantasyPros API key has an invalid format")

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
            "Cache-Control": "no-cache",
        },
    )
    request.add_unredirected_header("x-api-key", api_key)
    return request
