#!/usr/bin/env python3
"""Request guards for the radar HTTP boundary (contract C7, lane 05).

Pure, dispatcher-agnostic checks that RadarHandler (owned by chat 07) calls
before any route handler runs. Every rejection is a controlled JSON response
and MUST happen before any stored state can change.

Trust model (documented, not aspirational):

* The server binds loopback only. The OS user boundary is the real
  authentication: any local process can already talk to the socket, so a
  loopback client without browser headers is an *intentionally authorized
  non-browser client* (curl, launchd probes, scripts).
* The threats these guards address are browser-borne: DNS rebinding (a
  hostile page whose name resolves to 127.0.0.1 — its requests arrive with
  the hostile Host), cross-origin request forgery, content-type confusion,
  and malformed/oversized/slow request bodies.
* Forwarded headers (X-Forwarded-For/Host/Proto) are NEVER consulted; there
  is no legitimate proxy in front of this server.
* The custom ``X-Radar-Action`` header stays required on mutations. It is a
  preflight-forcing measure, not authentication, and is not treated as such.

Origin policy for mutations (the documented missing-Origin rule):

1. ``Origin`` present  -> must be exactly this server's own http origin
   (loopback name + actual port). ``null``, https, or foreign origins are
   rejected: no browser page other than the served dashboard may mutate.
2. ``Origin`` absent but ``Referer`` present -> the referrer's origin must
   satisfy the same rule (legacy-browser belt; the dashboard itself sends
   ``Referrer-Policy: no-referrer``, so its own requests carry neither).
3. Both absent -> ALLOWED. This is the deliberate, documented path for
   authorized local non-browser clients. It is not a bypass of browser
   protections: browsers attach Origin to POST requests, and a cross-origin
   page cannot strip it.
4. ``Sec-Fetch-Site`` present with a value other than ``same-origin`` /
   ``none`` -> rejected regardless of Origin (defense in depth on browsers
   that send fetch metadata).

Body rules for mutations: exact ``application/json`` (optional utf-8
charset), Content-Length required within [1, MAX_BODY_BYTES], no
Transfer-Encoding, valid UTF-8, JSON *object* (list/null/scalar get a
controlled 400), bounded wall-clock read time, and scalar-only values for
the fields the verdict store persists verbatim. Guard rejections close the
connection so an unread body can never be reinterpreted as a next request.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Dict, Optional, Tuple

MAX_BODY_BYTES = 16 * 1024
# Wall-clock budget for reading one request body. Tests shrink this via
# monkeypatch; the dispatcher passes nothing.
BODY_READ_TIMEOUT = 15.0

# Exact loopback names this service may be addressed as. 127.0.0.0/8
# variants beyond 127.0.0.1 are deliberately NOT listed: the server never
# binds them, and any request carrying one is misdirected.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# Fields record_verdict copies into config/verdicts.json without coercion.
# They must be scalars so a request cannot smuggle nested containers into
# the authored store.
RAW_PERSISTED_FIELDS = ("stars", "license", "last_push")
MAX_SCALAR_STRING_LEN = 4096


class Rejection(Tuple[int, Dict[str, Any], bool]):
    """(status, json_payload, close_connection) — truthy by construction."""

    __slots__ = ()

    def __new__(cls, status: int, payload: Dict[str, Any], close: bool = True):
        return super().__new__(cls, (status, payload, close))

    @property
    def status(self) -> int:
        return self[0]

    @property
    def payload(self) -> Dict[str, Any]:
        return self[1]

    @property
    def close(self) -> bool:
        return self[2]


def _split_host_header(value: str) -> Tuple[str, Optional[str]]:
    """Return (hostname_lowercase, port_string_or_None) from a Host header.

    Handles ``[::1]:8765``, ``[::1]``, ``127.0.0.1:8765``, ``localhost`` and
    the raw unbracketed ``::1`` a non-browser client may send.
    """
    value = value.strip()
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return value.lower(), None
        host = value[1:closing]
        rest = value[closing + 1 :]
        port = rest[1:] if rest.startswith(":") else None
        return host.lower(), port
    if value.count(":") > 1:
        # Unbracketed IPv6 literal; no port can be expressed.
        return value.lower(), None
    host, sep, port = value.partition(":")
    return host.lower(), (port if sep else None)


def check_host(host_header: Optional[str], server_port: int) -> Optional[Rejection]:
    """Reject any Host that is not this server's own loopback identity.

    This is the DNS-rebinding guard: a hostile page rebound to 127.0.0.1
    still sends its own hostname here. Missing Host (HTTP/1.0 raw clients)
    is rejected too — every legitimate client of this service sends one.
    """
    if not host_header or not host_header.strip():
        return Rejection(400, {"error": "missing Host header"})
    host, port = _split_host_header(host_header)
    if host not in LOOPBACK_NAMES:
        return Rejection(400, {"error": "Host is not a loopback name for this service"})
    if port is not None:
        if not port.isdigit() or int(port) != int(server_port):
            return Rejection(400, {"error": "Host port does not match this server"})
    elif int(server_port) != 80:
        # Bare loopback names without a port only make sense on port 80;
        # anything else is a client that did not address *this* server.
        return Rejection(400, {"error": "Host must include this server's port"})
    return None


def _origin_is_self(origin: str, server_port: int) -> bool:
    origin = origin.strip().lower()
    if not origin.startswith("http://"):
        return False
    host, port = _split_host_header(origin[len("http://") :].split("/", 1)[0])
    if host not in LOOPBACK_NAMES:
        return False
    if port is None:
        return int(server_port) == 80
    return port.isdigit() and int(port) == int(server_port)


def check_origin(
    origin_header: Optional[str],
    referer_header: Optional[str],
    sec_fetch_site: Optional[str],
    server_port: int,
) -> Optional[Rejection]:
    """Apply the documented browser-mutation policy (module docstring)."""
    if sec_fetch_site:
        if sec_fetch_site.strip().lower() not in {"same-origin", "none"}:
            return Rejection(403, {"error": "cross-site requests may not mutate this service"})
    if origin_header is not None and origin_header.strip() != "":
        if origin_header.strip().lower() == "null":
            return Rejection(403, {"error": "opaque origins may not mutate this service"})
        if not _origin_is_self(origin_header, server_port):
            return Rejection(403, {"error": "cross-origin requests may not mutate this service"})
        return None
    if referer_header:
        # No Origin but a Referer: hold it to the same standard. The served
        # dashboard sends neither (Referrer-Policy: no-referrer).
        prefix = referer_header.strip().lower()
        for scheme in ("http://",):
            if prefix.startswith(scheme):
                authority = prefix[len(scheme) :].split("/", 1)[0]
                if _origin_is_self("http://" + authority, server_port):
                    return None
        return Rejection(403, {"error": "cross-origin requests may not mutate this service"})
    # Documented rule 3: no Origin, no Referer -> authorized local
    # non-browser client. See module docstring; not an undocumented bypass.
    return None


def check_content_type(content_type: Optional[str]) -> Optional[Rejection]:
    """Mutations must declare exactly application/json (utf-8 charset ok)."""
    if not content_type:
        return Rejection(400, {"error": "Content-Type must be application/json"})
    parts = [p.strip() for p in content_type.split(";")]
    media = parts[0].lower()
    if media != "application/json":
        return Rejection(400, {"error": "Content-Type must be application/json"})
    for param in parts[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset" and value.strip().strip('"').lower() not in (
            "utf-8",
            "utf8",
        ):
            return Rejection(400, {"error": "charset must be utf-8"})
    return None


def read_json_object(
    handler: Any,
    *,
    max_bytes: int = MAX_BODY_BYTES,
    read_timeout: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Rejection]]:
    """Read, bound and parse a mutation body. Returns (object, None) or (None, rejection).

    Every failure is a controlled JSON response; the connection is closed on
    failure because the body may be wholly or partially unread and must not
    be parsed as a followup request.
    """
    if handler.headers.get("Transfer-Encoding"):
        return None, Rejection(501, {"error": "transfer-encoding is not supported"})
    raw_length = handler.headers.get("Content-Length")
    try:
        length = int(str(raw_length).strip())
    except (TypeError, ValueError):
        length = 0
    if length <= 0 or length > max_bytes:
        return None, Rejection(400, {"error": "body must be 1..{} bytes".format(max_bytes)})

    budget = BODY_READ_TIMEOUT if read_timeout is None else read_timeout
    deadline = time.monotonic() + budget
    connection = getattr(handler, "connection", None)
    previous_timeout = connection.gettimeout() if connection is not None else None
    chunks = []
    received = 0
    try:
        while received < length:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                return None, Rejection(408, {"error": "body read timed out"})
            if connection is not None:
                connection.settimeout(remaining_time)
            try:
                chunk = handler.rfile.read(min(65536, length - received))
            except socket.timeout:
                return None, Rejection(408, {"error": "body read timed out"})
            except OSError:
                return None, Rejection(400, {"error": "body could not be read"})
            if not chunk:
                return None, Rejection(400, {"error": "body ended early"})
            chunks.append(chunk)
            received += len(chunk)
    finally:
        if connection is not None:
            try:
                connection.settimeout(previous_timeout)
            except OSError:
                pass

    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        return None, Rejection(400, {"error": "body must be valid UTF-8"})
    try:
        parsed = json.loads(text)
    except ValueError:
        return None, Rejection(400, {"error": "body must be JSON"})
    if not isinstance(parsed, dict):
        return None, Rejection(400, {"error": "body must be a JSON object"})
    return parsed, None


def check_body_hygiene(body: Dict[str, Any]) -> Optional[Rejection]:
    """Structural hygiene on top of the handlers' own business validation.

    Deliberately does NOT re-encode enums or key formats (record_* owns those
    rules and already enforces them before writing); it only closes the two
    structural gaps: unbounded string values, and non-scalar values reaching
    fields the verdict store persists verbatim.
    """
    for key, value in body.items():
        if isinstance(value, str) and len(value) > MAX_SCALAR_STRING_LEN:
            return Rejection(
                400, {"error": "field '{}' exceeds {} characters".format(key, MAX_SCALAR_STRING_LEN)}
            )
    for field in RAW_PERSISTED_FIELDS:
        value = body.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, (str, int, float)):
            return Rejection(400, {"error": "field '{}' must be a scalar".format(field)})
        if isinstance(value, str) and len(value) > 200:
            return Rejection(400, {"error": "field '{}' exceeds 200 characters".format(field)})
    return None


def guard_mutation(handler: Any, server_port: int) -> Tuple[Optional[Dict[str, Any]], Optional[Rejection]]:
    """Full pre-handler pipeline for a mutation request (except the action
    header, which the dispatcher checks per-route to keep its 400 semantics).

    Order: Origin policy -> content type -> bounded body read -> object shape
    -> field hygiene. The Host check runs earlier, for every verb.
    """
    rejection = check_origin(
        handler.headers.get("Origin"),
        handler.headers.get("Referer"),
        handler.headers.get("Sec-Fetch-Site"),
        server_port,
    )
    if rejection:
        return None, rejection
    rejection = check_content_type(handler.headers.get("Content-Type"))
    if rejection:
        return None, rejection
    body, rejection = read_json_object(handler)
    if rejection:
        return None, rejection
    rejection = check_body_hygiene(body)
    if rejection:
        return None, rejection
    return body, None
