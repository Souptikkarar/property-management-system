#!/usr/bin/env python3
"""
Sample data generator for the IDS Next integration demo.

Builds a fake 60-room hotel: rooms, guests, reservations, folio lines and
housekeeping status. Everything is fictional. Same seed + same --today
gives identical output every time.

Booking patterns (lead time, length of stay, guest count, cancellation,
daily rate) come from the Kaggle "Hotel Booking Demand" CSV if it is at
data/raw/hotel_bookings.csv. If not, built-in random rules are used so the
script still runs. Arrival dates are always placed around "today", so the
data looks current.

Usage:
    python generator/generate.py
    python generator/generate.py --seed 7 --today 2026-10-14
"""
from __future__ import annotations

import argparse
import csv
import random
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW = ROOT / "data" / "raw" / "hotel_bookings.csv"
DEFAULT_OUT = ROOT / "data" / "generated"

HOTEL = {"hotel_code": "DEMO01", "name": "Hotel Demo Lisboa", "currency": "EUR"}
TAX_RATE_PCT = 10
WINDOW_PAST_DAYS = 21     # arrivals from this many days before today...
WINDOW_FUTURE_DAYS = 45   # ...to this many days after
OUT_OF_ORDER_ROOMS = 2
MAX_NIGHTS = 14

# code, name, max guests, daily-rate band in EUR, number of rooms
ROOM_TYPES = [
    ("STD1", "Standard Single", 1, (55, 95), 12),
    ("STD2", "Standard Double", 2, (75, 130), 24),
    ("DLX", "Deluxe", 3, (120, 200), 16),
    ("STE", "Suite", 4, (190, 380), 8),
]
ROOMS_PER_FLOOR = 12

FALLBACK_FIRST = ["Ana", "João", "Maria", "Pedro", "Sofia", "Tiago", "Inês", "Rui", "Beatriz",
                  "Miguel", "Clara", "Daniel", "Emma", "Lucas", "Hannah", "Oliver", "Chloe",
                  "Marco", "Giulia", "Jonas"]
FALLBACK_LAST = ["Silva", "Santos", "Ferreira", "Pereira", "Costa", "Martins", "Rodrigues",
                 "Smith", "Taylor", "Brown", "Müller", "Schmidt", "Rossi", "Bianchi", "Dubois",
                 "Martin", "Jansen", "Novak", "Larsen", "Kowalski"]
FALLBACK_COUNTRIES = ["PRT", "GBR", "FRA", "ESP", "DEU", "ITA", "IRL", "NLD", "USA", "BRA"]
FALLBACK_CHANNELS = ["Online TA", "Direct", "Offline TA/TO", "Corporate", "Groups"]


# --------------------------------------------------------------------------
# Booking patterns: Kaggle CSV or fallback
# --------------------------------------------------------------------------
def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def load_kaggle_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            nights = _to_int(r.get("stays_in_weekend_nights")) + _to_int(r.get("stays_in_week_nights"))
            adults = _to_int(r.get("adults"))
            if nights < 1 or adults < 1:
                continue
            country = (r.get("country") or "").strip()
            rows.append({
                "lead_time": max(0, min(_to_int(r.get("lead_time")), 365)),
                "nights": min(nights, MAX_NIGHTS),
                "adults": min(adults, 4),
                "children": min(_to_int(r.get("children")) + _to_int(r.get("babies")), 3),
                "adr": float(r.get("adr") or 0),
                "canceled": (r.get("is_canceled") or "").strip().lower() in ("1", "yes", "true"),
                "country": country if 2 <= len(country) <= 3 and country.isalpha() else None,
                "channel": (r.get("distribution_channel") or "").strip() or None,
            })
    return rows


def fallback_row(rng: random.Random) -> dict:
    return {
        "lead_time": min(int(rng.expovariate(1 / 60)), 365),
        "nights": rng.choices([1, 2, 3, 4, 5, 7, 10], [18, 28, 22, 12, 8, 8, 4])[0],
        "adults": rng.choices([1, 2, 3], [20, 70, 10])[0],
        "children": rng.choices([0, 1, 2], [85, 10, 5])[0],
        "adr": max(30.0, rng.gauss(105, 45)),
        "canceled": rng.random() < 0.3,
        "country": rng.choice(FALLBACK_COUNTRIES),
        "channel": rng.choice(FALLBACK_CHANNELS),
    }


def pick_booking_patterns(raw_path: Path, n: int, rng: random.Random):
    if raw_path.exists():
        pool = load_kaggle_rows(raw_path)
        if pool:
            return [rng.choice(pool) for _ in range(n)], f"kaggle ({raw_path.name}, {len(pool)} usable rows)"
    return [fallback_row(rng) for _ in range(n)], "built-in fallback rules (no Kaggle CSV found)"


# --------------------------------------------------------------------------
# Fake people
# --------------------------------------------------------------------------
class NameSource:
    """Faker if installed, otherwise small built-in lists. Always seeded."""

    def __init__(self, seed: int, rng: random.Random):
        self.rng = rng
        self.fake = None
        try:
            from faker import Faker
            Faker.seed(seed)
            self.fake = Faker("en_GB")
        except ImportError:
            pass

    def person(self, n: int) -> tuple[str, str, str, str]:
        if self.fake:
            first, last = self.fake.first_name(), self.fake.last_name()
            phone = self.fake.phone_number()
        else:
            first, last = self.rng.choice(FALLBACK_FIRST), self.rng.choice(FALLBACK_LAST)
            phone = f"+44 7700 900{self.rng.randint(100, 999)}"
        slug = f"{first}.{last}".lower().replace(" ", "").replace("'", "")
        # example.com is reserved for documentation: never a real mailbox
        return first, last, f"{slug}{n}@example.com", phone


# --------------------------------------------------------------------------
# Rooms
# --------------------------------------------------------------------------
def build_rooms(rng: random.Random) -> list[dict]:
    rooms, idx = [], 0
    for code, name, max_guests, band, count in ROOM_TYPES:
        for _ in range(count):
            floor = idx // ROOMS_PER_FLOOR + 1
            number = floor * 100 + idx % ROOMS_PER_FLOOR + 1
            rooms.append({
                "room_id": f"R{number}",
                "room_number": str(number),
                "floor": floor,
                "room_type": code,
                "room_type_name": name,
                "max_guests": max_guests,
            })
            idx += 1
    return rooms


def type_index(code: str) -> int:
    return [t[0] for t in ROOM_TYPES].index(code)


def band_for(code: str) -> tuple[int, int]:
    return ROOM_TYPES[type_index(code)][3]


def preferred_type_index(adr: float, guests: int) -> tuple[int, int]:
    """Return (preferred type index, minimum type index that fits the guests)."""
    if adr < 70:
        band = 0
    elif adr < 110:
        band = 1
    elif adr < 170:
        band = 2
    else:
        band = 3
    if guests >= 4:
        minimum = 3
    elif guests == 3:
        minimum = 2
    elif guests == 2:
        minimum = 1
    else:
        minimum = 0
    return max(band, minimum), minimum


# --------------------------------------------------------------------------
# Main generation
# --------------------------------------------------------------------------
def decide_status(cancelled: bool, arr: date, dep: date, today: date, rng: random.Random) -> str:
    if cancelled:
        return "cancelled"
    if dep < today:
        return "checked_out"
    if dep == today:
        return "checked_out" if rng.random() < 0.6 else "in_house"
    if arr < today:
        return "in_house"
    if arr == today:
        return "in_house" if rng.random() < 0.25 else "booked"
    return "booked"


def generate(seed: int = 42, today: date | None = None, raw_path: Path = DEFAULT_RAW,
             candidates: int = 1400) -> dict:
    today = today or date.today()
    now = datetime.combine(today, time(12, 0))  # "now" for the demo is noon today
    rng = random.Random(seed)
    names = NameSource(seed, rng)

    rooms = build_rooms(rng)
    out_of_order = {r["room_id"] for r in rng.sample(rooms, OUT_OF_ORDER_ROOMS)}
    rooms_by_type: dict[int, list[dict]] = {i: [] for i in range(len(ROOM_TYPES))}
    for r in rooms:
        if r["room_id"] not in out_of_order:
            rooms_by_type[type_index(r["room_type"])].append(r)
    occupied_nights: dict[str, list[tuple[date, date]]] = {r["room_id"]: [] for r in rooms}

    patterns, source = pick_booking_patterns(raw_path, candidates, rng)

    # Place arrivals around today, in date order so rooms fill like real life
    span = WINDOW_PAST_DAYS + WINDOW_FUTURE_DAYS
    arrivals = sorted(today - timedelta(days=WINDOW_PAST_DAYS) + timedelta(days=rng.randint(0, span))
                      for _ in patterns)

    guests: list[dict] = []
    reservations: list[dict] = []
    departing_still_in_house: set[str] = set()  # rooms whose guest leaves today but has not yet left

    def room_free(room_id: str, arr: date, dep: date) -> bool:
        return all(dep <= a or arr >= d for a, d in occupied_nights[room_id])

    for pat, arr in zip(patterns, arrivals):
        nights = pat["nights"]
        dep = arr + timedelta(days=nights)
        total_guests = pat["adults"] + pat["children"]
        cancelled = pat["canceled"]

        # Guest: 12% are returning guests
        if guests and rng.random() < 0.12:
            guest = rng.choice(guests)
        else:
            n = len(guests) + 1
            first, last, email, phone = names.person(n)
            guest = {
                "guest_id": f"G{n:05d}", "first_name": first, "last_name": last,
                "email": email, "phone": phone,
                "country": pat["country"] or rng.choice(FALLBACK_COUNTRIES),
                "vip": 1 if rng.random() < 0.04 else 0,
            }
            guests.append(guest)

        # Room: preferred type first, then bigger types, then smaller ones that still fit
        adr = pat["adr"] if pat["adr"] > 0 else rng.uniform(60, 140)
        preferred, minimum = preferred_type_index(adr, total_guests)
        order = [preferred] + list(range(preferred + 1, len(ROOM_TYPES))) + list(range(preferred - 1, minimum - 1, -1))
        room, room_type_idx = None, preferred
        if cancelled:
            room_type_idx = preferred  # cancelled bookings hold no room
        else:
            for ti in order:
                free = [r for r in rooms_by_type[ti] if room_free(r["room_id"], arr, dep)]
                if free:
                    room, room_type_idx = rng.choice(free), ti
                    break
            if room is None:
                continue  # hotel is full for these dates: drop this booking
            occupied_nights[room["room_id"]].append((arr, dep))

        code = ROOM_TYPES[room_type_idx][0]
        lo, hi = band_for(code)
        rate_cents = int(round(min(max(adr, lo), hi) * 100))

        status = decide_status(cancelled, arr, dep, today, rng)
        if room is not None:
            if status == "in_house" and dep == today:
                departing_still_in_house.add(room["room_id"])
            elif status == "in_house" and arr == today and room["room_id"] in departing_still_in_house:
                status = "booked"  # the room is not free yet, so this guest cannot have checked in early

        created_day = min(arr - timedelta(days=pat["lead_time"]), today)
        created_hour = rng.randint(0, 11) if created_day == today else rng.randint(8, 22)
        created_at = datetime.combine(created_day, time(created_hour, rng.randint(0, 59)))
        cancelled_at = None
        if cancelled:
            window_days = max(1, (min(arr, today) - created_day).days)
            cancelled_at = min(created_at + timedelta(hours=rng.randint(1, 24 * window_days)), now)
            cancelled_at = max(cancelled_at, created_at)

        checked_in_at = checked_out_at = None
        if status in ("in_house", "checked_out"):
            if arr < today:
                checked_in_at = datetime.combine(arr, time(rng.randint(14, 19), rng.randint(0, 59)))
            else:  # early check-in today
                checked_in_at = datetime.combine(arr, time(rng.randint(9, 11), rng.randint(0, 59)))
        if status == "checked_out":
            checked_out_at = datetime.combine(dep, time(rng.randint(6, 11), rng.randint(0, 59)))

        rid = len(reservations) + 1
        reservations.append({
            "reservation_id": f"RES{rid:06d}",
            "confirmation_no": f"IDN{2600000 + rid}",
            "guest_id": guest["guest_id"],
            "room_id": room["room_id"] if room else None,
            "room_type": code,
            "arrival_date": arr.isoformat(),
            "departure_date": dep.isoformat(),
            "nights": nights,
            "adults": pat["adults"],
            "children": pat["children"],
            "rate_cents": rate_cents,
            "currency": HOTEL["currency"],
            "channel": pat["channel"] or rng.choice(FALLBACK_CHANNELS),
            "status": status,
            "created_at": created_at.isoformat(timespec="seconds"),
            "cancelled_at": cancelled_at.isoformat(timespec="seconds") if cancelled_at else None,
            "checked_in_at": checked_in_at.isoformat(timespec="seconds") if checked_in_at else None,
            "checked_out_at": checked_out_at.isoformat(timespec="seconds") if checked_out_at else None,
        })

    folios, folio_lines = build_folios(reservations, today, now, rng)
    housekeeping = build_housekeeping(rooms, out_of_order, reservations, today, now, rng)

    return {
        "hotel": [{**HOTEL, "business_date": today.isoformat(), "seed": seed, "data_source": source}],
        "rooms": rooms,
        "guests": guests,
        "reservations": reservations,
        "folios": folios,
        "folio_lines": folio_lines,
        "housekeeping": housekeeping,
    }


def build_folios(reservations, today, now, rng):
    folios, all_lines = [], []
    for res in reservations:
        if res["status"] not in ("in_house", "checked_out"):
            continue
        arr = date.fromisoformat(res["arrival_date"])
        dep = date.fromisoformat(res["departure_date"])
        checked_in = datetime.fromisoformat(res["checked_in_at"])
        checked_out = datetime.fromisoformat(res["checked_out_at"]) if res["checked_out_at"] else None
        upper = checked_out or now
        lines = []  # (posted_at, code, description, cents)

        # Night audit posts each night's room charge and tax at 23:30 of that night
        for i in range(res["nights"]):
            night = arr + timedelta(days=i)
            if night >= today:
                break
            posted = datetime.combine(night, time(23, 30))
            tax = (res["rate_cents"] * TAX_RATE_PCT + 50) // 100
            lines.append((posted, "ROOM", f"Room charge {night.isoformat()}", res["rate_cents"]))
            lines.append((posted, "TAX", f"City tax {TAX_RATE_PCT}% {night.isoformat()}", tax))

        # Outlet charges (restaurant, minibar, laundry) during the stay
        guests_n = res["adults"] + res["children"]
        day = arr
        while day <= min(dep, today):
            extras = []
            if rng.random() < 0.55:
                extras.append(("REST", "Restaurant", rng.randint(1800, 5500) * guests_n // 2 + 500, rng.randint(8, 21)))
            if rng.random() < 0.15:
                extras.append(("MINI", "Minibar", rng.randint(400, 1800), rng.randint(9, 22)))
            if rng.random() < 0.06:
                extras.append(("LAUN", "Laundry", rng.randint(1500, 3500), rng.randint(9, 18)))
            for code, desc, cents, hour in extras:
                posted = datetime.combine(day, time(hour, rng.randint(0, 59)))
                if checked_in <= posted <= upper:
                    lines.append((posted, code, desc, cents))
            day += timedelta(days=1)

        # Guests who left have settled their bill
        if checked_out:
            charges = sum(c for _, _, _, c in lines)
            lines.append((checked_out, "PAY", "Card payment", -charges))

        lines.sort(key=lambda x: (x[0], x[1]))
        fid = "F" + res["reservation_id"][3:]
        balance = 0
        for posted, code, desc, cents in lines:
            n = len(all_lines) + 1
            all_lines.append({
                "line_id": f"L{n:07d}", "folio_id": fid, "posted_at": posted.isoformat(timespec="seconds"),
                "post_date": posted.date().isoformat(), "code": code, "description": desc,
                "amount_cents": cents, "currency": res["currency"],
            })
            balance += cents
        folios.append({
            "folio_id": fid, "reservation_id": res["reservation_id"], "guest_id": res["guest_id"],
            "room_id": res["room_id"], "status": "closed" if checked_out else "open",
            "balance_cents": balance, "currency": res["currency"],
        })
    return folios, all_lines


def build_housekeeping(rooms, out_of_order, reservations, today, now, rng):
    """Status as of 'now'. Rules: occupied rooms are never inspected or out of order."""
    today_s = today.isoformat()
    occupied, departed_today, arriving_today = set(), set(), set()
    for res in reservations:
        if not res["room_id"]:
            continue
        if res["status"] == "in_house":
            occupied.add(res["room_id"])
        elif res["status"] == "checked_out" and res["departure_date"] == today_s:
            departed_today.add(res["room_id"])
        elif res["status"] == "booked" and res["arrival_date"] == today_s:
            arriving_today.add(res["room_id"])

    rows = []
    for room in rooms:
        rid = room["room_id"]
        if rid in out_of_order:
            status, occ = "out_of_order", "vacant"
        elif rid in occupied:
            status, occ = rng.choices(["clean", "dirty"], [60, 40])[0], "occupied"
        elif rid in arriving_today:
            status, occ = rng.choices(["inspected", "clean", "dirty"], [55, 30, 15])[0], "vacant"
        elif rid in departed_today:
            status, occ = rng.choices(["dirty", "clean", "inspected"], [60, 30, 10])[0], "vacant"
        else:
            status, occ = rng.choices(["inspected", "clean", "dirty"], [55, 35, 10])[0], "vacant"
        rows.append({
            "room_id": rid, "room_number": room["room_number"],
            "housekeeping_status": status, "occupancy": occ,
            "updated_at": datetime.combine(today, time(rng.randint(6, 11), rng.randint(0, 59))).isoformat(timespec="seconds"),
        })
    return rows


# --------------------------------------------------------------------------
# Self-checks (the same rules the tests rely on)
# --------------------------------------------------------------------------
def validate(data: dict) -> list[str]:
    problems = []
    room_ids = {r["room_id"] for r in data["rooms"]}
    guest_ids = {g["guest_id"] for g in data["guests"]}
    res_ids = {r["reservation_id"] for r in data["reservations"]}
    folio_ids = {f["folio_id"] for f in data["folios"]}

    # No double-booking
    by_room: dict[str, list[tuple[str, str, str]]] = {}
    for r in data["reservations"]:
        if r["room_id"] and r["status"] != "cancelled":
            by_room.setdefault(r["room_id"], []).append((r["arrival_date"], r["departure_date"], r["reservation_id"]))
    for room_id, stays in by_room.items():
        stays.sort()
        for (a1, d1, id1), (a2, d2, id2) in zip(stays, stays[1:]):
            if a2 < d1:
                problems.append(f"double-booked {room_id}: {id1} overlaps {id2}")

    # One in-house guest per room
    in_house = [r["room_id"] for r in data["reservations"] if r["status"] == "in_house"]
    for room_id in sorted({x for x in in_house if in_house.count(x) > 1}):
        problems.append(f"{room_id}: more than one in-house guest")

    # Foreign keys
    for r in data["reservations"]:
        if r["guest_id"] not in guest_ids:
            problems.append(f"{r['reservation_id']}: unknown guest")
        if r["room_id"] and r["room_id"] not in room_ids:
            problems.append(f"{r['reservation_id']}: unknown room")
    for f in data["folios"]:
        if f["reservation_id"] not in res_ids or f["guest_id"] not in guest_ids or f["room_id"] not in room_ids:
            problems.append(f"{f['folio_id']}: broken reference")
    for line in data["folio_lines"]:
        if line["folio_id"] not in folio_ids:
            problems.append(f"{line['line_id']}: unknown folio")

    # Folio balances equal the sum of their lines
    sums: dict[str, int] = {}
    for line in data["folio_lines"]:
        sums[line["folio_id"]] = sums.get(line["folio_id"], 0) + line["amount_cents"]
    for f in data["folios"]:
        if f["balance_cents"] != sums.get(f["folio_id"], 0):
            problems.append(f"{f['folio_id']}: balance does not match lines")
        if f["status"] == "closed" and f["balance_cents"] != 0:
            problems.append(f"{f['folio_id']}: closed folio has a balance")

    # Housekeeping matches reservations
    hk = {h["room_id"]: h for h in data["housekeeping"]}
    in_house_rooms = {r["room_id"] for r in data["reservations"] if r["status"] == "in_house"}
    for room_id in in_house_rooms:
        h = hk[room_id]
        if h["occupancy"] != "occupied" or h["housekeeping_status"] in ("inspected", "out_of_order"):
            problems.append(f"{room_id}: housekeeping contradicts in-house guest")
    for room_id, h in hk.items():
        if h["housekeeping_status"] == "out_of_order" and room_id in by_room:
            problems.append(f"{room_id}: out of order but has reservations")
    return problems


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def write_csv(out_dir: Path, data: dict) -> None:
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    for table, rows in data.items():
        if not rows:
            continue
        with (csv_dir / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


def write_sqlite(out_dir: Path, data: dict) -> Path:
    path = out_dir / "hotel.sqlite"
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    for table, rows in data.items():
        if not rows:
            continue
        cols = list(rows[0].keys())
        types = []
        for c in cols:
            sample = next((r[c] for r in rows if r[c] is not None), "")
            types.append("INTEGER" if isinstance(sample, int) else "TEXT")
        con.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in zip(cols, types))})")
        con.executemany(
            f"INSERT INTO {table} VALUES ({', '.join('?' for _ in cols)})",
            [tuple(r[c] for c in cols) for r in rows],
        )
    con.commit()
    con.close()
    return path


def summarize(data: dict, today: date) -> str:
    res = data["reservations"]
    count = lambda s: sum(1 for r in res if r["status"] == s)
    occupied = sum(1 for h in data["housekeeping"] if h["occupancy"] == "occupied")
    hk: dict[str, int] = {}
    for h in data["housekeeping"]:
        hk[h["housekeeping_status"]] = hk.get(h["housekeeping_status"], 0) + 1
    return "\n".join([
        f"Business date: {today.isoformat()}   Data source: {data['hotel'][0]['data_source']}",
        f"Rooms: {len(data['rooms'])}   Guests: {len(data['guests'])}   Reservations: {len(res)}",
        f"  booked {count('booked')} | in-house {count('in_house')} | "
        f"checked-out {count('checked_out')} | cancelled {count('cancelled')}",
        f"Folios: {len(data['folios'])}   Folio lines: {len(data['folio_lines'])}",
        f"Occupied tonight: {occupied}/{len(data['rooms'])}   Housekeeping: {hk}",
    ])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Generate fake IDS Next demo data.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--today", type=date.fromisoformat, default=date.today(),
                   help="business date, YYYY-MM-DD (default: today)")
    p.add_argument("--raw", type=Path, default=DEFAULT_RAW, help="Kaggle hotel_bookings.csv (optional)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--candidates", type=int, default=1400, help="booking attempts (more = fuller hotel)")
    args = p.parse_args(argv)

    data = generate(args.seed, args.today, args.raw, args.candidates)
    problems = validate(data)
    if problems:
        print("Generated data failed its own checks:", *problems[:20], sep="\n  ", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out, data)
    db = write_sqlite(args.out, data)
    print(summarize(data, args.today))
    print(f"Wrote {db} and CSVs in {args.out / 'csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
