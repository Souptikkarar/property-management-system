"""Tests for the mock IDS Next (XML API + FIAS feed). Run from the project root: pytest"""
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "generator"))

import generate as g  # noqa: E402
from mock_ids.config import CLIENTS  # noqa: E402
from mock_ids.fias import FiasServer, FrameBuffer, decode, encode  # noqa: E402
from mock_ids.simulator import build_plan, run  # noqa: E402
from mock_ids.store import Store, StoreError, money, parse_money  # noqa: E402
from mock_ids.xmlapi import AuthService, make_http_server  # noqa: E402

TODAY = date(2026, 10, 14)
_tmp = None


def setUpModule():
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    data = g.generate(seed=42, today=TODAY, raw_path=Path("does-not-exist.csv"))
    g.write_sqlite(Path(_tmp.name), data)


def tearDownModule():
    _tmp.cleanup()


def db_path():
    return Path(_tmp.name) / "hotel.sqlite"


def xml_of(body: bytes):
    return ET.fromstring(body)


class ApiTestCase(unittest.TestCase):
    rate_limit = 0

    def setUp(self):
        self.store = Store(db_path())
        self.auth = AuthService(CLIENTS, ttl=900)
        self.http = make_http_server(self.store, self.auth, "127.0.0.1", 0, rate_limit=self.rate_limit)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()

    def call(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, data=body.encode() if body else None, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if body:
            req.add_header("Content-Type", "application/xml")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def login(self, client="demo-client", secret="demo-secret"):
        body = f"<TokenRQ><ClientId>{client}</ClientId><ClientSecret>{secret}</ClientSecret></TokenRQ>"
        status, raw, _ = self.call("POST", "/api/v1/auth/token", body)
        return status, xml_of(raw)

    def token(self, client="demo-client"):
        status, root = self.login(client)
        self.assertEqual(status, 200)
        return root.findtext("AccessToken")

    def error_code(self, raw):
        return xml_of(raw).find("Error").get("code")


class AuthTests(ApiTestCase):
    def test_login_returns_bearer_token(self):
        status, root = self.login()
        self.assertEqual(status, 200)
        self.assertEqual(root.findtext("TokenType"), "Bearer")
        self.assertEqual(root.findtext("ExpiresIn"), "900")
        self.assertTrue(root.findtext("AccessToken"))

    def test_wrong_secret_is_rejected(self):
        status, root = self.login(secret="nope")
        self.assertEqual(status, 401)
        self.assertEqual(root.find("Error").get("code"), "INVALID_CLIENT")

    def test_missing_and_garbage_tokens_are_rejected(self):
        status, raw, headers = self.call("GET", "/api/v1/reservations")
        self.assertEqual((status, self.error_code(raw)), (401, "MISSING_TOKEN"))
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")
        status, raw, _ = self.call("GET", "/api/v1/reservations", token="garbage")
        self.assertEqual((status, self.error_code(raw)), (401, "INVALID_TOKEN"))

    def test_expired_token_is_rejected_and_a_new_one_works(self):
        token = self.token()
        self.assertEqual(self.call("GET", "/api/v1/rooms/status", token=token)[0], 200)
        self.auth.expire_all()
        status, raw, _ = self.call("GET", "/api/v1/rooms/status", token=token)
        self.assertEqual((status, self.error_code(raw)), (401, "TOKEN_EXPIRED"))
        self.assertEqual(self.call("GET", "/api/v1/rooms/status", token=self.token())[0], 200)

    def test_read_only_client_cannot_write(self):
        token = self.token("readonly-client")
        self.assertEqual(self.call("GET", "/api/v1/rooms/status", token=token)[0], 200)
        body = "<RoomStatusRQ><Housekeeping>clean</Housekeeping></RoomStatusRQ>"
        status, raw, _ = self.call("PUT", "/api/v1/rooms/101/status", body, token)
        self.assertEqual((status, self.error_code(raw)), (403, "FORBIDDEN_SCOPE"))

    def test_unknown_path_and_wrong_method(self):
        token = self.token()
        self.assertEqual(self.call("GET", "/api/v1/nothing", token=token)[0], 404)
        self.assertEqual(self.call("DELETE", "/api/v1/rooms/status", token=token)[0], 405)

    def test_bad_xml_is_a_400_not_a_crash(self):
        status, raw, _ = self.call("POST", "/api/v1/auth/token", "<not xml")
        self.assertEqual((status, self.error_code(raw)), (400, "BAD_XML"))


class RateLimitTests(ApiTestCase):
    rate_limit = 3

    def test_fourth_request_in_a_minute_gets_429(self):
        token = self.token()
        codes = [self.call("GET", "/api/v1/rooms/status", token=token)[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        _, _, headers = self.call("GET", "/api/v1/rooms/status", token=token)
        self.assertTrue(int(headers["Retry-After"]) >= 1)


class ReadTests(ApiTestCase):
    def test_reservation_list_is_paged_and_complete(self):
        token = self.token()
        total = len(self.store.reservations_by_id)
        status, raw, _ = self.call("GET", "/api/v1/reservations?pageSize=100", token=token)
        root = xml_of(raw)
        self.assertEqual(status, 200)
        self.assertEqual(int(root.get("total")), total)
        self.assertEqual(len(root.findall("Reservation")), 100)
        self.assertEqual(root.get("hasMore"), "true")
        seen = 0
        page = 1
        while True:
            root = xml_of(self.call("GET", f"/api/v1/reservations?pageSize=500&page={page}", token=token)[1])
            seen += len(root.findall("Reservation"))
            if root.get("hasMore") == "false":
                break
            page += 1
        self.assertEqual(seen, total)

    def test_status_filter_and_money_format(self):
        token = self.token()
        root = xml_of(self.call("GET", "/api/v1/reservations?status=in_house", token=token)[1])
        items = root.findall("Reservation")
        self.assertTrue(items)
        self.assertTrue(all(i.get("status") == "in_house" for i in items))
        rate = items[0].find("Rate").get("amount")
        self.assertRegex(rate, r"^\d+\.\d{2}$")   # decimal euros, not cents

    def test_bad_query_values_are_400(self):
        token = self.token()
        for q in ("status=bogus", "arrivalFrom=14-10-2026", "page=0", "pageSize=9999", "modifiedSince=yesterday"):
            status, raw, _ = self.call("GET", "/api/v1/reservations?" + q, token=token)
            self.assertEqual((q, status, self.error_code(raw)), (q, 400, "BAD_REQUEST"))

    def test_single_reservation_and_404(self):
        token = self.token()
        rid = self.store.reservations(status="in_house")[0]["reservation_id"]
        root = xml_of(self.call("GET", f"/api/v1/reservations/{rid}", token=token)[1])
        self.assertEqual(root.find("Reservation").get("id"), rid)
        status, raw, _ = self.call("GET", "/api/v1/reservations/RES999999", token=token)
        self.assertEqual((status, self.error_code(raw)), (404, "RESERVATION_NOT_FOUND"))

    def test_cancelled_reservation_has_no_room_number(self):
        token = self.token()
        root = xml_of(self.call("GET", "/api/v1/reservations?status=cancelled&pageSize=5", token=token)[1])
        for res in root.findall("Reservation"):
            self.assertIsNone(res.find("Room").get("number"))
            self.assertIsNotNone(res.find("Timestamps").get("cancelled"))

    def test_guest_profile(self):
        token = self.token()
        guest = next(iter(self.store.guests.values()))
        root = xml_of(self.call("GET", f"/api/v1/guests/{guest['guest_id']}", token=token)[1])
        p = root.find("Profile")
        self.assertEqual(p.find("Name").get("last"), guest["last_name"])
        self.assertTrue(p.findtext("Email").endswith("@example.com"))

    def test_room_status_lists_all_rooms_and_filters(self):
        token = self.token()
        root = xml_of(self.call("GET", "/api/v1/rooms/status", token=token)[1])
        self.assertEqual(len(root.findall("Room")), 60)
        dirty = xml_of(self.call("GET", "/api/v1/rooms/status?housekeeping=dirty", token=token)[1])
        self.assertTrue(all(r.get("housekeeping") == "dirty" for r in dirty.findall("Room")))

    def test_folio_balance_matches_lines(self):
        token = self.token()
        rid = next(r["reservation_id"] for r in self.store.reservations(status="in_house")
                   if r["reservation_id"] in self.store.folio_by_res)
        root = xml_of(self.call("GET", f"/api/v1/reservations/{rid}/folio", token=token)[1])
        folio = root.find("Folio")
        total = sum(parse_money(ln.get("amount").lstrip("-")) * (-1 if ln.get("amount").startswith("-") else 1)
                    for ln in folio.findall("Line"))
        self.assertEqual(money(total), folio.get("balance"))

    def test_folio_missing_before_check_in(self):
        token = self.token()
        rid = self.store.reservations(status="booked")[0]["reservation_id"]
        status, raw, _ = self.call("GET", f"/api/v1/reservations/{rid}/folio", token=token)
        self.assertEqual((status, self.error_code(raw)), (404, "FOLIO_NOT_FOUND"))

    def test_modified_since_returns_only_changes(self):
        token = self.token()
        before = self.store.current_time().isoformat()
        rid = ready_arrival(self.store)["reservation_id"]
        self.store.check_in(rid)
        root = xml_of(self.call("GET", f"/api/v1/reservations?modifiedSince={before}", token=token)[1])
        self.assertEqual([r.get("id") for r in root.findall("Reservation")], [rid])


class WriteTests(ApiTestCase):
    def open_folio(self):
        return next(f for f in self.store.folios.values() if f["status"] == "open")

    def charge_body(self, ref="pos-1", amount="18.50"):
        return (f"<ChargePostRQ><ExternalRef>{ref}</ExternalRef><Code>REST</Code>"
                f"<Description>Dinner</Description><Amount>{amount}</Amount></ChargePostRQ>")

    def test_charge_posts_and_updates_balance(self):
        token = self.token()
        folio = self.open_folio()
        before = folio["balance_cents"]
        status, raw, _ = self.call("POST", f"/api/v1/folios/{folio['folio_id']}/charges", self.charge_body(), token)
        self.assertEqual(status, 201)
        self.assertEqual(xml_of(raw).get("duplicate"), "false")
        self.assertEqual(self.store.folios[folio["folio_id"]]["balance_cents"], before + 1850)

    def test_same_external_ref_never_posts_twice(self):
        token = self.token()
        folio = self.open_folio()
        path = f"/api/v1/folios/{folio['folio_id']}/charges"
        self.call("POST", path, self.charge_body(), token)
        balance = self.store.folios[folio["folio_id"]]["balance_cents"]
        status, raw, _ = self.call("POST", path, self.charge_body(), token)
        self.assertEqual(status, 200)
        self.assertEqual(xml_of(raw).get("duplicate"), "true")
        self.assertEqual(self.store.folios[folio["folio_id"]]["balance_cents"], balance)

    def test_bad_amounts_are_rejected(self):
        token = self.token()
        path = f"/api/v1/folios/{self.open_folio()['folio_id']}/charges"
        for amount in ("0", "-5", "abc", "1.234", ""):
            status, raw, _ = self.call("POST", path, self.charge_body(ref="x" + amount, amount=amount), token)
            self.assertEqual((amount, status, self.error_code(raw)), (amount, 400, "BAD_AMOUNT"))

    def test_closed_folio_refuses_charges(self):
        token = self.token()
        closed = next(f for f in self.store.folios.values() if f["status"] == "closed")
        status, raw, _ = self.call("POST", f"/api/v1/folios/{closed['folio_id']}/charges", self.charge_body(), token)
        self.assertEqual((status, self.error_code(raw)), (409, "FOLIO_CLOSED"))

    def test_room_status_can_be_changed(self):
        token = self.token()
        room = next(r for r in self.store.list_room_status() if r["occupancy"] == "vacant"
                    and r["housekeeping"] != "out_of_order")
        body = "<RoomStatusRQ><Housekeeping>dirty</Housekeeping></RoomStatusRQ>"
        status, raw, _ = self.call("PUT", f"/api/v1/rooms/{room['room_number']}/status", body, token)
        self.assertEqual(status, 200)
        self.assertEqual(xml_of(raw).find("Room").get("housekeeping"), "dirty")

    def test_occupied_room_cannot_be_inspected(self):
        token = self.token()
        room = next(r for r in self.store.list_room_status() if r["occupancy"] == "occupied")
        body = "<RoomStatusRQ><Housekeeping>inspected</Housekeeping></RoomStatusRQ>"
        status, raw, _ = self.call("PUT", f"/api/v1/rooms/{room['room_number']}/status", body, token)
        self.assertEqual((status, self.error_code(raw)), (409, "ROOM_OCCUPIED"))


def ready_arrival(store):
    """A booked reservation whose room is vacant and not dirty/out of order."""
    for r in store.reservations(status="booked"):
        if not r["room_id"]:
            continue
        hk = store.housekeeping[r["room_id"]]
        if hk["occupancy"] == "vacant" and hk["housekeeping_status"] in ("clean", "inspected"):
            return r
    raise AssertionError("no ready arrival in the sample data")


class StoreRuleTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(db_path())

    def test_check_in_needs_a_ready_room(self):
        rid = ready_arrival(self.store)["reservation_id"]
        room = self.store.rooms[self.store.reservations_by_id[rid]["room_id"]]["room_number"]
        self.store.set_room_status(room, "dirty")
        with self.assertRaises(StoreError) as cm:
            self.store.check_in(rid)
        self.assertEqual(cm.exception.code, "ROOM_NOT_READY")
        self.store.set_room_status(room, "clean")
        self.assertEqual(self.store.check_in(rid)["status"], "in_house")
        with self.assertRaises(StoreError):
            self.store.check_in(rid)   # cannot check in twice

    def test_check_in_creates_folio_and_check_out_settles_it(self):
        rid = ready_arrival(self.store)["reservation_id"]
        self.store.check_in(rid)
        folio, _ = self.store.folio_for_reservation(rid)
        self.assertEqual((folio["status"], folio["balance_cents"]), ("open", 0))
        self.store.post_charge(folio["folio_id"], "REST", "Dinner", 2500)
        self.store.check_out(rid)
        folio, lines = self.store.folio_for_reservation(rid)
        self.assertEqual((folio["status"], folio["balance_cents"]), ("closed", 0))
        self.assertEqual(sum(x["amount_cents"] for x in lines), 0)

    def test_check_out_leaves_room_dirty_and_vacant(self):
        rid = self.store.reservations(status="in_house")[0]["reservation_id"]
        room_id = self.store.reservations_by_id[rid]["room_id"]
        self.store.check_out(rid)
        hk = self.store.housekeeping[room_id]
        self.assertEqual((hk["occupancy"], hk["housekeeping_status"]), ("vacant", "dirty"))


class FiasClient:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.buf = FrameBuffer()
        self.pending = []

    def read(self, timeout=2.0):
        self.sock.settimeout(timeout)
        while not self.pending:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not data:
                return None
            self.pending.extend(self.buf.feed(data))
        return decode(self.pending.pop(0))

    def read_all(self, quiet=0.7):
        out = []
        while (frame := self.read(quiet)) is not None:
            out.append(frame)
        return out

    def send(self, record, fields=()):
        self.sock.sendall(encode(record, fields))

    def close(self):
        self.sock.close()


class FramingTests(unittest.TestCase):
    def test_round_trip(self):
        record, fields = decode(encode("GI", [("RN", "204"), ("G#", "RES1"), ("GN", "O'Brien")])[1:-1])
        self.assertEqual((record, fields), ("GI", {"RN": "204", "G#": "RES1", "GN": "O'Brien"}))

    def test_separator_characters_in_values_cannot_break_a_frame(self):
        _, fields = decode(encode("PS", [("CT", "a|b\x03c")])[1:-1])
        self.assertEqual(fields["CT"], "a b c")

    def test_split_merged_and_noisy_streams(self):
        a, b = encode("LA", [("DA", "261014")]), encode("GO", [("RN", "101")])
        buf = FrameBuffer()
        stream = b"noise" + a + b
        out = []
        for i in range(0, len(stream), 3):   # arrives in 3-byte pieces
            out += buf.feed(stream[i:i + 3])
        self.assertEqual([decode(x)[0] for x in out], ["LA", "GO"])


class FiasTestCase(unittest.TestCase):
    chaos = False

    def setUp(self):
        self.store = Store(db_path())
        self.fias = FiasServer(self.store, "127.0.0.1", 0, chaos=self.chaos, seed=3)
        self.fias.start()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.fias.stop()

    def connect(self):
        c = FiasClient(self.fias.port)
        self.clients.append(c)
        self.assertEqual(c.read()[0], "LS")
        self.assertEqual(c.read()[0], "LD")
        return c


class FiasTests(FiasTestCase):
    def test_keepalive_is_answered(self):
        c = self.connect()
        c.send("LA", [("DA", "261014"), ("TI", "120000")])
        self.assertEqual(c.read()[0], "LA")

    def test_check_in_is_pushed_as_GI(self):
        c = self.connect()
        r = ready_arrival(self.store)
        self.store.check_in(r["reservation_id"])
        record, f = c.read()
        guest = self.store.guests[r["guest_id"]]
        self.assertEqual(record, "GI")
        self.assertEqual((f["G#"], f["GN"]), (r["reservation_id"], guest["last_name"]))
        self.assertEqual(f["RN"], self.store.rooms[r["room_id"]]["room_number"])

    def test_check_out_is_GO_then_room_goes_dirty(self):
        c = self.connect()
        rid = self.store.reservations(status="in_house")[0]["reservation_id"]
        self.store.check_out(rid)
        (r1, f1), (r2, f2) = c.read(), c.read()
        self.assertEqual((r1, r2), ("GO", "RE"))
        self.assertEqual((f1["RN"], f2["RS"]), (f2["RN"], "1"))   # 1 = dirty in this mock

    def test_room_status_change_is_pushed_as_RE(self):
        c = self.connect()
        room = next(r for r in self.store.list_room_status() if r["occupancy"] == "vacant"
                    and r["housekeeping"] == "dirty")["room_number"]
        self.store.set_room_status(room, "clean")
        record, f = c.read()
        self.assertEqual((record, f["RN"], f["RS"]), ("RE", room, "2"))

    def test_posting_charge_via_PS_updates_folio(self):
        c = self.connect()
        res = self.store.reservations(status="in_house")[0]
        room = self.store.rooms[res["room_id"]]["room_number"]
        folio_id = self.store.folio_by_res[res["reservation_id"]]
        before = self.store.folios[folio_id]["balance_cents"]
        c.send("PS", [("P#", "9001"), ("RN", room), ("TA", "1850"), ("SO", "REST"), ("CT", "Dinner")])
        record, f = c.read()
        self.assertEqual((record, f["AS"], f["P#"]), ("PA", "OK", "9001"))
        self.assertEqual(self.store.folios[folio_id]["balance_cents"], before + 1850)

    def test_repeated_PS_is_acknowledged_but_posted_once(self):
        c = self.connect()
        res = self.store.reservations(status="in_house")[0]
        room = self.store.rooms[res["room_id"]]["room_number"]
        folio_id = self.store.folio_by_res[res["reservation_id"]]
        for _ in range(3):
            c.send("PS", [("P#", "42"), ("RN", room), ("TA", "500"), ("SO", "MINI")])
            self.assertEqual(c.read()[1]["AS"], "OK")
        posted = [x for x in self.store.lines[folio_id] if x["amount_cents"] == 500 and x["code"] == "MINI"]
        self.assertEqual(len(posted), 1)

    def test_PS_for_empty_room_or_bad_amount_is_refused(self):
        c = self.connect()
        empty = next(r for r in self.store.list_room_status() if r["occupancy"] == "vacant")["room_number"]
        c.send("PS", [("P#", "1"), ("RN", empty), ("TA", "500")])
        self.assertEqual(c.read()[1]["AS"], "NG")
        occupied = next(r for r in self.store.list_room_status() if r["occupancy"] == "occupied")["room_number"]
        c.send("PS", [("P#", "2"), ("RN", occupied), ("TA", "abc")])
        self.assertEqual(c.read()[1]["AS"], "IA")

    def test_charges_are_mirrored_to_other_clients_but_not_echoed(self):
        poster, listener = self.connect(), self.connect()
        res = self.store.reservations(status="in_house")[0]
        room = self.store.rooms[res["room_id"]]["room_number"]
        poster.send("PS", [("P#", "77"), ("RN", room), ("TA", "900"), ("SO", "LAUN")])
        self.assertEqual(poster.read()[0], "PA")
        self.assertIsNone(poster.read(0.7))   # no echo of its own posting
        record, f = listener.read()
        self.assertEqual((record, f["TA"], f["G#"]), ("PS", "900", res["reservation_id"]))


class ChaosTests(FiasTestCase):
    chaos = True

    def test_chaos_duplicates_and_reorders_but_loses_nothing(self):
        c = self.connect()
        res = self.store.reservations(status="in_house")[0]
        folio_id = self.store.folio_by_res[res["reservation_id"]]
        for i in range(30):
            self.store.post_charge(folio_id, "MINI", f"item {i}", 100 + i, external_ref=f"c{i}")
        frames = c.read_all()
        seqs = [f["TA"] for r, f in frames if r == "PS"]
        self.assertEqual(set(seqs), {str(100 + i) for i in range(30)})   # nothing lost
        self.assertGreater(len(seqs), 30)                                  # duplicates arrived
        self.assertNotEqual(seqs, sorted(seqs))                           # order was disturbed


class SimulatorTests(unittest.TestCase):
    def test_plan_is_repeatable(self):
        a = [s.describe() for s in build_plan(Store(db_path()), seed=5)]
        b = [s.describe() for s in build_plan(Store(db_path()), seed=5)]
        self.assertEqual(a, b)

    def check_day(self, store):
        today = store.business_date.isoformat()
        plan = build_plan(store)
        skipped = []
        run(store, plan, interval=0, log=lambda m: skipped.append(m) if "skipped" in m else None)
        self.assertEqual(skipped, [])
        self.assertEqual([r for r in store.reservations(status="in_house") if r["departure_date"] == today], [])
        arrivals_left = [r for r in store.reservations(status="booked") if r["arrival_date"] == today and r["room_id"]]
        self.assertEqual(arrivals_left, [])
        occupied_rooms = [r["room_id"] for r in store.reservations(status="in_house")]
        self.assertEqual(len(occupied_rooms), len(set(occupied_rooms)))    # one guest per room
        for room_id in occupied_rooms:
            hk = store.housekeeping[room_id]
            self.assertEqual(hk["occupancy"], "occupied")
            self.assertIn(hk["housekeeping_status"], ("clean", "dirty"))
        for room_id, hk in store.housekeeping.items():
            if room_id not in occupied_rooms:
                self.assertEqual(hk["occupancy"], "vacant")
        for f in store.folios.values():
            self.assertEqual(f["balance_cents"], sum(x["amount_cents"] for x in store.lines[f["folio_id"]]))
            if f["status"] == "closed":
                self.assertEqual(f["balance_cents"], 0)

    def test_day_plays_out_consistently(self):
        self.check_day(Store(db_path()))

    def test_day_plays_out_for_many_different_hotels(self):
        """Different seeds (and a different business date) give different hotels; all must play out cleanly."""
        for seed in range(1, 13):
            with tempfile.TemporaryDirectory() as tmp:
                data = g.generate(seed=seed, today=date(2026, 10, 15), raw_path=Path("does-not-exist.csv"))
                g.write_sqlite(Path(tmp), data)
                with self.subTest(seed=seed):
                    self.check_day(Store(Path(tmp) / "hotel.sqlite"))


if __name__ == "__main__":
    unittest.main()
