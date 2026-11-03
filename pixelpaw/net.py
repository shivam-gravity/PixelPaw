"""Tiny JSON-over-HTTPS client on the Python standard library (no requests, no SDKs)."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "PixelPaw/1.0 (+https://github.com/shivam-srivastava-031/PixelPaw)"


def _ssl_context() -> ssl.SSLContext:
    try:  # use certifi's CA bundle when it happens to be installed (helps some macOS/Linux setups)
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_CTX = _ssl_context()


class ApiError(Exception):
    """HTTP/API failure. `status` 0 means a network problem; `reason` is the API's error reason."""

    def __init__(self, status: int, reason: str = "", message: str = ""):
        self.status, self.reason, self.message = status, reason, message
        super().__init__(f"{status} {reason}: {message}".strip())


def _parse(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw": raw[:500].decode("utf-8", "replace")}


def request(method: str, url: str, *, params: dict | None = None, json_body=None, form: dict | None = None,
            headers: dict | None = None, timeout: float = 60):
    if params:
        clean = {k: ("true" if v is True else "false" if v is False else v) for k, v in params.items() if v is not None}
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif method in ("POST", "PUT", "PATCH"):
        data = b""  # Google APIs reject body-less POSTs without Content-Length
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            return _parse(resp.read())
    except urllib.error.HTTPError as exc:
        body = _parse(exc.read())
        err = body.get("error") if isinstance(body, dict) else None
        reason, message = "", ""
        if isinstance(err, dict):
            errs = err.get("errors") or []
            reason = (errs[0].get("reason") if errs else "") or err.get("status", "")
            message = err.get("message", "")
        elif isinstance(err, str):  # OAuth token endpoint style
            reason, message = err, body.get("error_description", "")
        else:
            message = str(body)[:300]
        raise ApiError(exc.code, reason, message) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise ApiError(0, "network", str(getattr(exc, "reason", exc))) from None
