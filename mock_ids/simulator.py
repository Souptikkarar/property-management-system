"""Plays 'the rest of the day' at the mock hotel so the feed has live events.

The plan is built from the sample data: today's departures check out, today's
arrivals check in (after their room is cleaned), in-house guests run up charges,
and housekeeping works through dirty rooms. Same seed = same plan.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

from .store import Store, StoreError

CHARGE_MENU = [("REST", "Restaurant dinner", 2500, 9000), ("MINI", "Minibar", 400, 1800),
               ("LAUN", "Laundry", 1500, 3500), ("SPA", "Spa treatment", 4000, 9500)]


@dataclass
class Step:
    kind: str       # check_in | check_out | room_status | charge
    args: tuple

    def describe(self) -> str:
        if self.kind == "charge":
            rid, code, _desc, cents = self.args
            return f"charge {code} {cents / 100:.2f} to {rid}"
        return f"{self.kind} " + " ".join(str(a) for a in self.args)


def build_plan(store: Store, seed: int = 1, max_charges: int = 12, max_housekeeping: int = 8) -> list[Step]:
    rng = random.Random(seed)
    today = store.business_date.isoformat()
    room_no = lambda r: store.rooms[r["room_id"]]["room_number"]
    rooms = {x["room_number"]: x for x in store.list_room_status()}

    in_house = store.reservations(status="in_house")
    departing = [r for r in in_house if r["departure_date"] == today]
    staying = [r for r in in_house if r["departure_date"] != today]
    arriving = [r for r in store.reservations(status="booked") if r["arrival_date"] == today and r["room_id"]]

    steps: list[Step] = []
    just_vacated = set()
    for r in departing:
        steps.append(Step("check_out", (r["reservation_id"],)))
        just_vacated.add(room_no(r))
    arriving_rooms = set()
    for r in arriving:
        rn = room_no(r)
        arriving_rooms.add(rn)
        if rn in just_vacated or rooms[rn]["housekeeping"] == "dirty":
            steps.append(Step("room_status", (rn, "clean")))
        steps.append(Step("check_in", (r["reservation_id"],)))

    guests = staying + arriving
    for r in rng.sample(guests, min(max_charges, len(guests))):
        code, desc, lo, hi = rng.choice(CHARGE_MENU)
        steps.append(Step("charge", (r["reservation_id"], code, desc, rng.randint(lo, hi))))

    dirty = [rn for rn, x in sorted(rooms.items())
             if x["occupancy"] == "vacant" and x["housekeeping"] == "dirty" and rn not in arriving_rooms]
    dirty += sorted(just_vacated - arriving_rooms)   # rooms getting a new guest are not touched again
    for rn in rng.sample(dirty, min(max_housekeeping, len(dirty))):
        steps.append(Step("room_status", (rn, "clean")))
        if rng.random() < 0.5:
            steps.append(Step("room_status", (rn, "inspected")))
    return steps


def apply_step(store: Store, step: Step, index: int):
    if step.kind == "check_out":
        store.check_out(step.args[0], source="sim")
    elif step.kind == "check_in":
        store.check_in(step.args[0], source="sim")
    elif step.kind == "room_status":
        store.set_room_status(step.args[0], step.args[1], source="sim")
    elif step.kind == "charge":
        rid, code, desc, cents = step.args
        folio, _ = store.folio_for_reservation(rid)
        store.post_charge(folio["folio_id"], code, desc, cents, external_ref=f"sim-{index}", source="pos")


def run(store: Store, plan: list[Step], interval: float = 2.0, stop: threading.Event | None = None, log=print):
    for i, step in enumerate(plan):
        if stop and stop.is_set():
            return
        try:
            apply_step(store, step, i)
            log(f"[sim {i + 1}/{len(plan)}] {step.describe()}")
        except StoreError as e:
            log(f"[sim {i + 1}/{len(plan)}] skipped {step.describe()}: {e.message}")
        if interval:
            time.sleep(interval)
