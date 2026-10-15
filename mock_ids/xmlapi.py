"""HTNG-style XML web-service for the mock IDS Next.

ASSUMPTION: endpoint paths, element names and the token flow are invented for this demo.
They follow the general style of HTNG/OpenTravel web services (XML bodies, bearer tokens,
decimal money) but are NOT IDS Next's real schemas.

Endpoints (all under /api/v1, XML in and out):
  POST /auth/token                      client credentials -> bearer token
  GET  /reservations                    filters: status, arrivalFrom, arrivalTo, modifiedSince, page, pageSize
  GET  /reservations/{id}
  GET  /reservations/{id}/folio
  GET  /guests/{id}
  GET  /rooms/status                    filter: housekeeping
  PUT  /rooms/{number}/status           write: housekeeping status          (needs "write" scope)
  POST /folios/{folio_id}/charges       write: post a charge, duplicate-safe (needs "write" scope)
"""
from __future__ import annotations

import hmac
import math
import re
import secrets
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import RES_STATUSES, Store, StoreError, money, parse_money

MAX_BODY = 64 * 1024


# --------------------------------------------------------------------------
# Auth and rate limiting
# --------------------------------------------------------------------------
class AuthService:
    def __init__(self, clients: dict, ttl: int = 900, clock=time.time):
        self.clients, self.ttl, self.clock = clients, ttl, clock
        self._tokens: dict[str, dict] = {}
        self._lock = threading.Lock()

    def issue(self, client_id: str, secret: str):
        c = self.clients.get(client_id)
        if not c or not hmac.compare_digest(c["secret"].encode(), (secret or "").encode()):
            raise StoreError("INVALID_CLIENT", "unknown client or wrong secret", 401)
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = {"client": client_id, "scopes": set(c["scopes"]), "expires": self.clock() + self.ttl}
        return token, self.ttl, sorted(c["scopes"])

    def verify(self, token: str | None):
        if not token:
            raise StoreError("MISSING_TOKEN", "send 'Authorization: Bearer <token>'", 401)
        with self._lock:
            rec = self._tokens.get(token)
        if not rec:
            raise StoreError("INVALID_TOKEN", "token not recognised", 401)
        if self.clock() >= rec["expires"]:
            raise StoreError("TOKEN_EXPIRED", "token has expired; request a new one", 401)
        return rec["client"], rec["scopes"]

    def expire_all(self):
        """For demos and tests: make every issued token expire now."""
        with self._lock:
            for rec in self._tokens.values():
                rec["expires"] = 0


class RateLimiter:
    """Fixed one-minute window per client. limit=0 turns it off. (Real limits are unknown.)"""

    def __init__(self, limit_per_minute: int = 0, clock=time.time):
        self.limit, self.clock = limit_per_minute, clock
        self._windows: dict[str, list] = {}
        self._lock = threading.Lock()

    def check(self, key: str):
        if not self.limit:
            return None
        with self._lock:
            now = self.clock()
            w = self._windows.get(key)
            if not w or now - w[0] >= 60:
                self._windows[key] = [now, 1]
                return None
            if w[1] >= self.limit:
                return max(1, math.ceil(60 - (now - w[0])))
            w[1] += 1
            return None


# --------------------------------------------------------------------------
# XML helpers
# --------------------------------------------------------------------------
def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def to_bytes(root) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def parse_xml(body: bytes):
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise StoreError("BAD_XML", "request body is not valid XML", 400)


def error_root(err: StoreError):
    root = ET.Element("ErrorRS")
    sub(root, "Error", err.message, code=err.code, httpStatus=err.http_status)
    return root


def reservation_el(store: Store, r: dict, parent=None):
    el = ET.Element("Reservation") if parent is None else ET.SubElement(parent, "Reservation")
    el.attrib.update(id=r["reservation_id"], confirmationNo=r["confirmation_no"], status=r["status"],
                     lastModified=r["last_modified"])
    sub(el, "Stay", arrival=r["arrival_date"], departure=r["departure_date"], nights=r["nights"],
        adults=r["adults"], children=r["children"])
    room = {"type": r["room_type"]}
    if r["room_id"]:
        room["number"] = store.rooms[r["room_id"]]["room_number"]
    sub(el, "Room", **room)
    sub(el, "Rate", amount=money(r["rate_cents"]), currency=r["currency"])
    sub(el, "Guest", id=r["guest_id"])
    sub(el, "Channel", r["channel"])
    stamps = {"created": r["created_at"]}
    for attr, key in (("cancelled", "cancelled_at"), ("checkedIn", "checked_in_at"), ("checkedOut", "checked_out_at")):
        if r[key]:
            stamps[attr] = r[key]
    sub(el, "Timestamps", **stamps)
    return el


def line_el(parent, ln: dict):
    return sub(parent, "Line", id=ln["line_id"], postedAt=ln["posted_at"], code=ln["code"],
               description=ln["description"], amount=money(ln["amount_cents"]), currency=ln["currency"])


def folio_root(store: Store, folio: dict, lines: list[dict], tag="FolioRS"):
    root = ET.Element(tag)
    f = sub(root, "Folio", id=folio["folio_id"], reservationId=folio["reservation_id"], status=folio["status"],
            balance=money(folio["balance_cents"]), currency=folio["currency"])
    sub(f, "Guest", id=folio["guest_id"])
    sub(f, "Room", number=store.rooms[folio["room_id"]]["room_number"])
    for ln in lines:
        line_el(f, ln)
    return root


# --------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------
@dataclass
class Req:
    method: str
    query: dict
    body: bytes
    params: dict
    client: str | None
    scopes: set


def qs(req: Req, name: str):
    v = req.query.get(name)
    return v[0] if v else None


def qint(req: Req, name: str, default: int, low: int, high: int) -> int:
    raw = qs(req, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise StoreError("BAD_REQUEST", f"{name} must be a whole number", 400)
    if not low <= value <= high:
        raise StoreError("BAD_REQUEST", f"{name} must be between {low} and {high}", 400)
    return value


def qdate(req: Req, name: str):
    raw = qs(req, name)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise StoreError("BAD_REQUEST", f"{name} must be YYYY-MM-DD", 400)


def h_token(ctx, req: Req):
    root = parse_xml(req.body)
    token, ttl, scopes = ctx.auth.issue(root.findtext("ClientId", "").strip(), root.findtext("ClientSecret", "").strip())
    out = ET.Element("TokenRS")
    sub(out, "AccessToken", token)
    sub(out, "TokenType", "Bearer")
    sub(out, "ExpiresIn", ttl)
    sub(out, "Scope", " ".join(scopes))
    return 200, out


def h_reservations(ctx, req: Req):
    status = qs(req, "status")
    if status and status not in RES_STATUSES:
        raise StoreError("BAD_REQUEST", f"status must be one of {', '.join(RES_STATUSES)}", 400)
    since = None
    if qs(req, "modifiedSince"):
        try:
            since = datetime.fromisoformat(qs(req, "modifiedSince"))
        except ValueError:
            raise StoreError("BAD_REQUEST", "modifiedSince must be an ISO date-time", 400)
    page = qint(req, "page", 1, 1, 100000)
    size = qint(req, "pageSize", 100, 1, 500)
    items = ctx.store.reservations(status, qdate(req, "arrivalFrom"), qdate(req, "arrivalTo"), since)
    chunk = items[(page - 1) * size: page * size]
    root = ET.Element("ReservationListRS", page=str(page), pageSize=str(size), total=str(len(items)),
                      hasMore="true" if page * size < len(items) else "false")
    for r in chunk:
        reservation_el(ctx.store, r, root)
    return 200, root


def h_reservation(ctx, req: Req):
    root = ET.Element("ReservationRS")
    reservation_el(ctx.store, ctx.store.get_reservation(req.params["id"]), root)
    return 200, root


def h_res_folio(ctx, req: Req):
    folio, lines = ctx.store.folio_for_reservation(req.params["id"])
    return 200, folio_root(ctx.store, folio, lines)


def h_guest(ctx, req: Req):
    g = ctx.store.get_guest(req.params["id"])
    root = ET.Element("ProfileRS")
    p = sub(root, "Profile", id=g["guest_id"], vip="true" if g["vip"] else "false")
    sub(p, "Name", first=g["first_name"], last=g["last_name"])
    sub(p, "Email", g["email"])
    sub(p, "Phone", g["phone"])
    sub(p, "Country", g["country"])
    return 200, root


def h_rooms(ctx, req: Req):
    hk = qs(req, "housekeeping")
    root = ET.Element("RoomStatusRS", businessDate=ctx.store.business_date.isoformat())
    for r in ctx.store.list_room_status(hk):
        sub(root, "Room", number=r["room_number"], type=r["room_type"], floor=r["floor"],
            housekeeping=r["housekeeping"], occupancy=r["occupancy"], updated=r["updated_at"])
    return 200, root


def h_room_put(ctx, req: Req):
    status = parse_xml(req.body).findtext("Housekeeping", "").strip()
    hk = ctx.store.set_room_status(req.params["number"], status, source="api")
    root = ET.Element("RoomStatusRS")
    sub(root, "Room", number=req.params["number"], housekeeping=hk["housekeeping_status"],
        occupancy=hk["occupancy"], updated=hk["updated_at"])
    return 200, root


def h_charge(ctx, req: Req):
    body = parse_xml(req.body)
    ref = body.findtext("ExternalRef", "").strip()
    line, dup = ctx.store.post_charge(
        req.params["folio_id"], body.findtext("Code", "").strip(), body.findtext("Description", ""),
        parse_money(body.findtext("Amount")), external_ref=f"{req.client}:{ref}" if ref else None, source="api")
    root = ET.Element("ChargePostRS", duplicate="true" if dup else "false")
    line_el(root, line)
    return (200 if dup else 201), root


ID = r"(?P<id>[A-Za-z0-9_-]+)"
ROUTES = [
    ("POST", r"/api/v1/auth/token", h_token, True),
    ("GET", r"/api/v1/reservations", h_reservations, False),
    ("GET", rf"/api/v1/reservations/{ID}", h_reservation, False),
    ("GET", rf"/api/v1/reservations/{ID}/folio", h_res_folio, False),
    ("GET", rf"/api/v1/guests/{ID}", h_guest, False),
    ("GET", r"/api/v1/rooms/status", h_rooms, False),
    ("PUT", r"/api/v1/rooms/(?P<number>[0-9]{1,5})/status", h_room_put, False),
    ("POST", r"/api/v1/folios/(?P<folio_id>[A-Za-z0-9_-]+)/charges", h_charge, False),
]


@dataclass
class Context:
    store: Store
    auth: AuthService
    limiter: RateLimiter
    verbose: bool = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "IDSNextMock/0.1"

    def log_message(self, fmt, *args):
        if self.server.ctx.verbose:  # never log headers: they carry tokens
            sys.stderr.write("[http] %s\n" % (fmt % args))

    do_GET = lambda self: self._dispatch("GET")
    do_POST = lambda self: self._dispatch("POST")
    do_PUT = lambda self: self._dispatch("PUT")
    do_DELETE = lambda self: self._dispatch("DELETE")

    def _send(self, status: int, root, headers: dict | None = None):
        body = to_bytes(root)
        self.send_response(status)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, err: StoreError):
        headers = dict(err.headers)
        if err.http_status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        self._send(err.http_status, error_root(err), headers)

    def _dispatch(self, method: str):
        ctx = self.server.ctx
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return self._send_error(StoreError("BAD_REQUEST", "bad or oversized body", 413 if length > 0 else 400))
        body = self.rfile.read(length) if length else b""

        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        route, path_known = None, False
        for m, pattern, fn, public in ROUTES:
            match = re.fullmatch(pattern, path)
            if match:
                path_known = True
                if m == method:
                    route = (fn, public, match)
                    break
        try:
            if not route:
                if path_known:
                    raise StoreError("METHOD_NOT_ALLOWED", f"{method} not supported here", 405)
                raise StoreError("NOT_FOUND", f"no such endpoint: {path}", 404)
            fn, public, match = route
            client, scopes = None, set()
            if not public:
                auth = self.headers.get("Authorization", "")
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
                client, scopes = ctx.auth.verify(token)
                need = "read" if method == "GET" else "write"
                if need not in scopes:
                    raise StoreError("FORBIDDEN_SCOPE", f"this client lacks the '{need}' scope", 403)
                retry = ctx.limiter.check(client)
                if retry:
                    raise StoreError("RATE_LIMITED", "too many requests", 429, {"Retry-After": str(retry)})
            status, root = fn(ctx, Req(method, parse_qs(parsed.query), body, match.groupdict(), client, scopes))
            self._send(status, root)
        except StoreError as err:
            self._send_error(err)
        except Exception:
            traceback.print_exc()
            self._send_error(StoreError("INTERNAL", "unexpected server error", 500))


class MockHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    ctx: Context


def make_http_server(store: Store, auth: AuthService, host: str, port: int,
                     rate_limit: int = 0, verbose: bool = False) -> MockHttpServer:
    server = MockHttpServer((host, port), Handler)
    server.ctx = Context(store, auth, RateLimiter(rate_limit), verbose)
    return server
