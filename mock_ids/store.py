"""In-memory hotel state for the mock, loaded from the generator's SQLite file.

One Store is shared by the XML API and the FIAS server, so a check-in made on one side
is visible on the other. All changes go through methods here, which enforce the rules
and notify listeners (the FIAS server listens, to push events to connected clients).
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

HK_STATUSES = ("clean", "dirty", "inspected", "out_of_order")
RES_STATUSES = ("booked", "in_house", "checked_out", "cancelled")
CODE_RE = re.compile(r"^[A-Z]{2,6}$")
MONEY_RE = re.compile(r"^\d{1,7}(\.\d{1,2})?$")


class StoreError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400, headers: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status
        self.headers = headers or {}


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}{c // 100}.{c % 100:02d}"


def parse_money(text) -> int:
    if text is None or not MONEY_RE.match(text.strip()):
        raise StoreError("BAD_AMOUNT", "amount must be a positive decimal with at most 2 decimals, e.g. 18.50")
    return int(Decimal(text.strip()) * 100)


class Store:
    def __init__(self, db_path):
        path = Path(db_path)
        if not path.exists():
            raise FileNotFoundError(f"{path} not found. Run: python generator/generate.py")
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            rows = lambda t: [dict(r) for r in con.execute(f"SELECT * FROM {t}")]
            self.hotel = rows("hotel")[0]
            self.rooms = {r["room_id"]: r for r in rows("rooms")}
            self.guests = {g["guest_id"]: g for g in rows("guests")}
            self.reservations_by_id = {r["reservation_id"]: r for r in rows("reservations")}
            self.folios = {f["folio_id"]: f for f in rows("folios")}
            self.housekeeping = {h["room_id"]: h for h in rows("housekeeping")}
            lines = rows("folio_lines")
        finally:
            con.close()

        self.business_date = date.fromisoformat(self.hotel["business_date"])
        self.rooms_by_number = {r["room_number"]: r for r in self.rooms.values()}
        self.folio_by_res = {f["reservation_id"]: f["folio_id"] for f in self.folios.values()}
        self.lines: dict[str, list[dict]] = {fid: [] for fid in self.folios}
        for ln in lines:
            self.lines[ln["folio_id"]].append(ln)
        self._next_line = max((int(ln["line_id"][1:]) for ln in lines), default=0) + 1
        self._refs: dict[str, dict] = {}   # external_ref -> line, for duplicate protection
        for r in self.reservations_by_id.values():
            stamps = [r[k] for k in ("created_at", "cancelled_at", "checked_in_at", "checked_out_at") if r[k]]
            r["last_modified"] = max(stamps)
        self._clock = datetime.combine(self.business_date, time(12, 0))
        self._lock = threading.RLock()
        self._listeners = []

    # ---- plumbing -------------------------------------------------------
    def add_listener(self, fn):
        self._listeners.append(fn)

    def _emit(self, event: dict):
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # a broken listener must never break the PMS
                pass

    def _tick(self) -> datetime:
        self._clock += timedelta(minutes=1)
        return self._clock

    def current_time(self) -> datetime:
        return self._clock

    # ---- reads ----------------------------------------------------------
    def reservations(self, status=None, arrival_from=None, arrival_to=None, modified_since=None) -> list[dict]:
        with self._lock:
            out = []
            for r in sorted(self.reservations_by_id.values(), key=lambda x: x["reservation_id"]):
                if status and r["status"] != status:
                    continue
                if arrival_from and r["arrival_date"] < arrival_from:
                    continue
                if arrival_to and r["arrival_date"] > arrival_to:
                    continue
                if modified_since and datetime.fromisoformat(r["last_modified"]) <= modified_since:
                    continue
                out.append(dict(r))
            return out

    def get_reservation(self, reservation_id: str) -> dict:
        with self._lock:
            r = self.reservations_by_id.get(reservation_id)
            if not r:
                raise StoreError("RESERVATION_NOT_FOUND", f"no reservation {reservation_id}", 404)
            return dict(r)

    def get_guest(self, guest_id: str) -> dict:
        with self._lock:
            g = self.guests.get(guest_id)
            if not g:
                raise StoreError("GUEST_NOT_FOUND", f"no guest {guest_id}", 404)
            return dict(g)

    def list_room_status(self, housekeeping=None) -> list[dict]:
        with self._lock:
            out = []
            for room in sorted(self.rooms.values(), key=lambda r: r["room_number"]):
                hk = self.housekeeping[room["room_id"]]
                if housekeeping and hk["housekeeping_status"] != housekeeping:
                    continue
                out.append({"room_number": room["room_number"], "room_type": room["room_type"],
                            "floor": room["floor"], "housekeeping": hk["housekeeping_status"],
                            "occupancy": hk["occupancy"], "updated_at": hk["updated_at"]})
            return out

    def folio_for_reservation(self, reservation_id: str):
        with self._lock:
            self.get_reservation(reservation_id)
            fid = self.folio_by_res.get(reservation_id)
            if not fid:
                raise StoreError("FOLIO_NOT_FOUND", f"{reservation_id} has no folio (guest has not checked in)", 404)
            return dict(self.folios[fid]), [dict(x) for x in self.lines[fid]]

    def find_in_house_by_room(self, room_number: str):
        with self._lock:
            room = self.rooms_by_number.get(room_number)
            if not room:
                return None
            for r in self.reservations_by_id.values():
                if r["room_id"] == room["room_id"] and r["status"] == "in_house":
                    return dict(r)
            return None

    # ---- writes ---------------------------------------------------------
    def check_in(self, reservation_id: str, source: str = "api") -> dict:
        with self._lock:
            r = self.reservations_by_id.get(reservation_id)
            if not r:
                raise StoreError("RESERVATION_NOT_FOUND", f"no reservation {reservation_id}", 404)
            if r["status"] != "booked":
                raise StoreError("INVALID_STATE", f"cannot check in a {r['status']} reservation", 409)
            if not r["room_id"]:
                raise StoreError("NO_ROOM", "reservation has no room assigned", 409)
            hk = self.housekeeping[r["room_id"]]
            if hk["occupancy"] == "occupied":
                raise StoreError("ROOM_OCCUPIED", "room is occupied", 409)
            if hk["housekeeping_status"] in ("dirty", "out_of_order"):
                raise StoreError("ROOM_NOT_READY", f"room is {hk['housekeeping_status']}", 409)
            ts = self._tick()
            r.update(status="in_house", checked_in_at=ts.isoformat(timespec="seconds"),
                     last_modified=ts.isoformat(timespec="seconds"))
            hk.update(occupancy="occupied", updated_at=ts.isoformat(timespec="seconds"))
            if hk["housekeeping_status"] == "inspected":
                hk["housekeeping_status"] = "clean"   # occupied rooms are never "inspected"
            self._ensure_folio(r)
            room_number = self.rooms[r["room_id"]]["room_number"]
            event = {"type": "check_in", "at": ts, "source": source, "reservation": dict(r),
                     "guest": dict(self.guests[r["guest_id"]]), "room_number": room_number}
        self._emit(event)
        return dict(r)

    def check_out(self, reservation_id: str, source: str = "api") -> dict:
        with self._lock:
            r = self.reservations_by_id.get(reservation_id)
            if not r:
                raise StoreError("RESERVATION_NOT_FOUND", f"no reservation {reservation_id}", 404)
            if r["status"] != "in_house":
                raise StoreError("INVALID_STATE", f"cannot check out a {r['status']} reservation", 409)
            ts = self._tick()
            stamp = ts.isoformat(timespec="seconds")
            folio = self.folios[self.folio_by_res[reservation_id]]
            if folio["balance_cents"] != 0:   # guest settles the bill at the desk
                self._add_line(folio, "PAY", "Card payment", -folio["balance_cents"], ts)
            folio["status"] = "closed"
            r.update(status="checked_out", checked_out_at=stamp, last_modified=stamp)
            hk = self.housekeeping[r["room_id"]]
            hk.update(occupancy="vacant", housekeeping_status="dirty", updated_at=stamp)
            room_number = self.rooms[r["room_id"]]["room_number"]
            events = [
                {"type": "check_out", "at": ts, "source": source, "reservation": dict(r), "room_number": room_number},
                {"type": "room_status", "at": ts, "source": source, "room_number": room_number, "status": "dirty"},
            ]
        for e in events:
            self._emit(e)
        return dict(r)

    def set_room_status(self, room_number: str, status: str, source: str = "api") -> dict:
        if status not in HK_STATUSES:
            raise StoreError("BAD_STATUS", f"housekeeping must be one of {', '.join(HK_STATUSES)}", 400)
        with self._lock:
            room = self.rooms_by_number.get(room_number)
            if not room:
                raise StoreError("ROOM_NOT_FOUND", f"no room {room_number}", 404)
            hk = self.housekeeping[room["room_id"]]
            if hk["occupancy"] == "occupied" and status in ("inspected", "out_of_order"):
                raise StoreError("ROOM_OCCUPIED", f"an occupied room cannot be set to {status}", 409)
            if hk["housekeeping_status"] == status:
                return dict(hk)
            ts = self._tick()
            hk.update(housekeeping_status=status, updated_at=ts.isoformat(timespec="seconds"))
            event = {"type": "room_status", "at": ts, "source": source, "room_number": room_number, "status": status}
        self._emit(event)
        return dict(hk)

    def post_charge(self, folio_id, code, description, amount_cents, external_ref=None,
                    source="api", origin=None):
        """Returns (line, duplicate). A repeated external_ref never posts twice."""
        if not CODE_RE.match(code or ""):
            raise StoreError("BAD_CODE", "charge code must be 2-6 capital letters", 400)
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise StoreError("BAD_AMOUNT", "amount must be greater than zero", 400)
        description = (description or code).strip()[:60]
        with self._lock:
            folio = self.folios.get(folio_id)
            if not folio:
                raise StoreError("FOLIO_NOT_FOUND", f"no folio {folio_id}", 404)
            if external_ref and external_ref in self._refs:
                return dict(self._refs[external_ref]), True
            if folio["status"] != "open":
                raise StoreError("FOLIO_CLOSED", "folio is closed", 409)
            ts = self._tick()
            line = self._add_line(folio, code, description, amount_cents, ts)
            if external_ref:
                self._refs[external_ref] = line
            res = self.reservations_by_id[folio["reservation_id"]]
            res["last_modified"] = ts.isoformat(timespec="seconds")
            event = {"type": "charge", "at": ts, "source": source, "origin": origin, "line": dict(line),
                     "reservation_id": res["reservation_id"], "room_number": self.rooms[folio["room_id"]]["room_number"]}
        self._emit(event)
        return dict(line), False

    # ---- internals ------------------------------------------------------
    def _ensure_folio(self, r: dict):
        if r["reservation_id"] in self.folio_by_res:
            return
        fid = "F" + r["reservation_id"][3:]
        self.folios[fid] = {"folio_id": fid, "reservation_id": r["reservation_id"], "guest_id": r["guest_id"],
                            "room_id": r["room_id"], "status": "open", "balance_cents": 0, "currency": r["currency"]}
        self.folio_by_res[r["reservation_id"]] = fid
        self.lines[fid] = []

    def _add_line(self, folio: dict, code: str, description: str, cents: int, ts: datetime) -> dict:
        line = {"line_id": f"L{self._next_line:07d}", "folio_id": folio["folio_id"],
                "posted_at": ts.isoformat(timespec="seconds"), "post_date": ts.date().isoformat(),
                "code": code, "description": description, "amount_cents": cents, "currency": folio["currency"]}
        self._next_line += 1
        self.lines[folio["folio_id"]].append(line)
        folio["balance_cents"] += cents
        return line
