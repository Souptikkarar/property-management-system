"""FIAS-style TCP feed for the mock IDS Next.

ASSUMPTIONS (from general knowledge of the FIAS style, NOT confirmed for IDS Next):
  * Messages are text records framed by STX (0x02) ... ETX (0x03).
  * A record is a 2-letter id followed by fields; each field is a 2-char id plus a value,
    separated by '|' and ending with '|'.  e.g.  GI|RN204|G#RES000123|GNSilva|
  * Records used here:  LS/LD/LA/LE link control, GI check-in, GO check-out,
    RE room status, PS charge posting, PA posting answer.
  * Room-status codes come from config.HK_TO_FIAS (an invented mapping).
  * Posting records are mirrored to other connected clients so a listener sees charges.
    Whether a real IDS Next does that is UNKNOWN.
"""
from __future__ import annotations

import queue
import random
import socket
import socketserver
import threading

from .config import HK_TO_FIAS
from .store import Store, StoreError

STX, ETX = b"\x02", b"\x03"
OUTLET_CODES = {"REST", "MINI", "LAUN", "SPA"}


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------
def _clean(value) -> str:
    return str(value).translate({ord(c): " " for c in "|\x02\x03\r\n"})


def encode(record: str, fields=()) -> bytes:
    body = "|".join([record] + [f"{k}{_clean(v)}" for k, v in fields]) + "|"
    return STX + body.encode("utf-8") + ETX


def decode(payload: bytes):
    parts = [p for p in payload.decode("utf-8", errors="replace").split("|") if p]
    if not parts:
        return "", {}
    return parts[0], {p[:2]: p[2:] for p in parts[1:]}


class FrameBuffer:
    """Reassembles frames from a byte stream (TCP can split or merge messages)."""
    MAX = 8192

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames = []
        while True:
            start = self._buf.find(STX)
            if start < 0:
                self._buf.clear()
                break
            del self._buf[:start]
            end = self._buf.find(ETX, 1)
            if end < 0:
                if len(self._buf) > self.MAX:
                    self._buf.clear()
                break
            frames.append(bytes(self._buf[1:end]))
            del self._buf[:end + 1]
        return frames


def _yymmdd(iso_date: str) -> str:
    return iso_date.replace("-", "")[2:]


def frames_for_event(ev: dict) -> list[bytes]:
    at = ev["at"]
    stamp = [("DA", at.strftime("%y%m%d")), ("TI", at.strftime("%H%M%S"))]
    kind = ev["type"]
    if kind == "check_in":
        r, g = ev["reservation"], ev["guest"]
        return [encode("GI", [("RN", ev["room_number"]), ("G#", r["reservation_id"]), ("GN", g["last_name"]),
                              ("GF", g["first_name"]), ("GA", _yymmdd(r["arrival_date"])),
                              ("GD", _yymmdd(r["departure_date"]))] + stamp)]
    if kind == "check_out":
        return [encode("GO", [("RN", ev["room_number"]), ("G#", ev["reservation"]["reservation_id"])] + stamp)]
    if kind == "room_status":
        return [encode("RE", [("RN", ev["room_number"]), ("RS", HK_TO_FIAS[ev["status"]])] + stamp)]
    if kind == "charge":
        ln = ev["line"]
        return [encode("PS", [("P#", ln["line_id"][1:]), ("RN", ev["room_number"]), ("G#", ev["reservation_id"]),
                              ("TA", ln["amount_cents"]), ("SO", ln["code"]), ("CT", ln["description"])] + stamp)]
    return []


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class _Client:
    def __init__(self, sock, addr):
        self.sock, self.addr, self.lock = sock, addr, threading.Lock()

    def send(self, data: bytes) -> bool:
        with self.lock:
            try:
                self.sock.sendall(data)
                return True
            except OSError:
                return False


class _TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        fs: FiasServer = self.server.fias
        client = _Client(self.request, self.client_address)
        self.request.settimeout(0.5)
        fs._register(client)   # register first so no event is missed after the handshake
        try:
            client.send(encode("LS", fs._stamp()))
            client.send(encode("LD", fs._stamp() + [("V#", "IDSNEXT-MOCK-0.1"), ("IF", "PMS")]))
            buf = FrameBuffer()
            while not fs._stop.is_set():
                try:
                    data = self.request.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                for payload in buf.feed(data):
                    if not fs._handle_frame(client, payload):
                        return
        finally:
            fs._unregister(client)


class FiasServer:
    def __init__(self, store: Store, host: str = "127.0.0.1", port: int = 5100, chaos: bool = False, seed: int = 1):
        self.store, self.chaos = store, chaos
        self._rng = random.Random(seed)
        self._clients: list[_Client] = []
        self._clients_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._srv = _TcpServer((host, port), _Handler)
        self._srv.fias = self
        self._threads: list[threading.Thread] = []
        store.add_listener(self._on_event)

    @property
    def port(self) -> int:
        return self._srv.server_address[1]

    def start(self):
        for target in (self._srv.serve_forever, self._sender):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._srv.shutdown()
        self._srv.server_close()
        with self._clients_lock:
            for c in self._clients:
                try:
                    c.sock.close()
                except OSError:
                    pass

    # ---- bookkeeping ----
    def _stamp(self):
        t = self.store.current_time()
        return [("DA", t.strftime("%y%m%d")), ("TI", t.strftime("%H%M%S"))]

    def _register(self, c):
        with self._clients_lock:
            self._clients.append(c)

    def _unregister(self, c):
        with self._clients_lock:
            if c in self._clients:
                self._clients.remove(c)

    # ---- outbound events ----
    def _on_event(self, ev: dict):
        frames = frames_for_event(ev)
        if frames:
            self._queue.put((frames, ev.get("origin")))

    def _send_all(self, frame: bytes, exclude):
        with self._clients_lock:
            targets = [c for c in self._clients if c is not exclude]
        for c in targets:
            c.send(frame)

    def _sender(self):
        """Delivers events in order. With chaos on, it duplicates and swaps some frames,
        to mimic a flaky link and let the adapter prove it copes."""
        held = None
        while not self._stop.is_set():
            try:
                frames, exclude = self._queue.get(timeout=0.2)
            except queue.Empty:
                if held:
                    self._send_all(*held)
                    held = None
                continue
            for frame in frames:
                to_send = [frame]
                if self.chaos:
                    roll = self._rng.random()
                    if roll < 0.15:
                        to_send = [frame, frame]          # duplicate
                    elif roll < 0.30 and held is None:
                        held = (frame, exclude)           # delay behind the next frame
                        continue
                for f in to_send:
                    self._send_all(f, exclude)
                if held and held[0] is not frame:
                    self._send_all(*held)
                    held = None

    # ---- inbound ----
    def _handle_frame(self, client: _Client, payload: bytes) -> bool:
        record, f = decode(payload)
        if record == "LE":
            return False
        if record == "LA":
            client.send(encode("LA", self._stamp()))
        elif record == "PS":
            self._handle_ps(client, f)
        # unknown records are ignored, as a tolerant interface would
        return True

    def _handle_ps(self, client: _Client, f: dict):
        seq, room = f.get("P#", ""), f.get("RN", "")

        def answer(status: str):
            fields = [("AS", status), ("P#", seq), ("RN", room)] + self._stamp()
            client.send(encode("PA", fields))

        try:
            cents = int(f.get("TA", ""))
        except ValueError:
            return answer("IA")   # invalid amount
        res = self.store.find_in_house_by_room(room)
        if not res:
            return answer("NG")   # no guest in that room
        outlet = f.get("SO", "")
        try:
            folio_id = self.store.folio_by_res[res["reservation_id"]]
            self.store.post_charge(folio_id, outlet if outlet in OUTLET_CODES else "OTHER",
                                   f.get("CT", "POS charge"), cents,
                                   external_ref=f"fias:{seq}" if seq else None, source="fias", origin=client)
        except StoreError:
            return answer("IA")
        answer("OK")
