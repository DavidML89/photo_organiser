"""Netscape cookies.txt loader for authenticated Google Photos CDN fetches."""

from __future__ import annotations

from http.cookiejar import Cookie, CookieJar
from pathlib import Path


def load_netscape_cookies(path: Path) -> CookieJar:
    """Load a Netscape / cookies.txt export into a CookieJar.

    Compatible with the "Get cookies.txt LOCALLY" Chrome extension format.
    """
    jar = CookieJar()
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            # Keep #HttpOnly_ lines (some exporters use that prefix).
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_") :]
            else:
                continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _include_sub, cookie_path, secure, expires, name, value = parts
        try:
            expires_i = int(expires) if expires not in ("0", "") else None
        except ValueError:
            expires_i = None
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain.lstrip("."),
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path=cookie_path,
            path_specified=True,
            secure=secure.upper() == "TRUE",
            expires=expires_i,
            discard=expires_i is None,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
    return jar


def cookies_look_usable(jar: CookieJar) -> list[str]:
    """Return names of important Google session cookies that are present."""
    wanted = {"SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"}
    found = {c.name for c in jar}
    return sorted(wanted & found)
