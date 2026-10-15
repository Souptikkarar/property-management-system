"""Tests for generator/generate.py. Run from the project root with: pytest"""
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "generator"))
import generate as g  # noqa: E402

TODAY = date(2026, 10, 14)
NO_KAGGLE = Path("does-not-exist.csv")  # forces the built-in fallback rules


@pytest.fixture(scope="module")
def data():
    return g.generate(seed=42, today=TODAY, raw_path=NO_KAGGLE)


def test_same_seed_gives_identical_output(data):
    again = g.generate(seed=42, today=TODAY, raw_path=NO_KAGGLE)
    assert again == data


def test_different_seed_gives_different_output(data):
    other = g.generate(seed=7, today=TODAY, raw_path=NO_KAGGLE)
    assert other["reservations"] != data["reservations"]


def test_hotel_has_60_rooms_with_unique_ids(data):
    ids = [r["room_id"] for r in data["rooms"]]
    assert len(ids) == 60
    assert len(set(ids)) == 60


def test_no_room_is_double_booked(data):
    stays = defaultdict(list)
    for r in data["reservations"]:
        if r["room_id"] and r["status"] != "cancelled":
            stays[r["room_id"]].append((r["arrival_date"], r["departure_date"]))
    for room_id, items in stays.items():
        items.sort()
        for (_, dep), (arr, _) in zip(items, items[1:]):
            assert arr >= dep, f"{room_id} is double-booked"


def test_cancelled_reservations_hold_no_room(data):
    for r in data["reservations"]:
        if r["status"] == "cancelled":
            assert r["room_id"] is None
            assert r["cancelled_at"] is not None


def test_folio_balance_equals_sum_of_lines(data):
    sums = defaultdict(int)
    for line in data["folio_lines"]:
        sums[line["folio_id"]] += line["amount_cents"]
    assert data["folios"], "expected at least one folio"
    for f in data["folios"]:
        assert f["balance_cents"] == sums[f["folio_id"]]


def test_closed_folios_are_settled(data):
    closed = [f for f in data["folios"] if f["status"] == "closed"]
    assert closed
    assert all(f["balance_cents"] == 0 for f in closed)


def test_references_point_to_existing_rows(data):
    rooms = {r["room_id"] for r in data["rooms"]}
    guests = {x["guest_id"] for x in data["guests"]}
    reservations = {r["reservation_id"] for r in data["reservations"]}
    folios = {f["folio_id"] for f in data["folios"]}
    for r in data["reservations"]:
        assert r["guest_id"] in guests
        assert r["room_id"] is None or r["room_id"] in rooms
    for f in data["folios"]:
        assert f["reservation_id"] in reservations
        assert f["guest_id"] in guests
        assert f["room_id"] in rooms
    for line in data["folio_lines"]:
        assert line["folio_id"] in folios


def test_only_stays_that_started_have_folios(data):
    started = {r["reservation_id"] for r in data["reservations"] if r["status"] in ("in_house", "checked_out")}
    assert {f["reservation_id"] for f in data["folios"]} == started


def test_nothing_is_posted_in_the_future(data):
    now = "2026-10-14T12:00:00"
    assert all(line["posted_at"] <= now for line in data["folio_lines"])


def test_occupied_rooms_are_never_inspected_or_out_of_order(data):
    hk = {h["room_id"]: h for h in data["housekeeping"]}
    in_house = {r["room_id"] for r in data["reservations"] if r["status"] == "in_house"}
    assert in_house
    for room_id in in_house:
        assert hk[room_id]["occupancy"] == "occupied"
        assert hk[room_id]["housekeeping_status"] in ("clean", "dirty")


def test_housekeeping_covers_every_room_with_valid_status(data):
    assert {h["room_id"] for h in data["housekeeping"]} == {r["room_id"] for r in data["rooms"]}
    assert {h["housekeeping_status"] for h in data["housekeeping"]} <= {"clean", "dirty", "inspected", "out_of_order"}


def test_guest_emails_are_never_real_addresses(data):
    assert all(x["email"].endswith("@example.com") for x in data["guests"])


def test_built_in_validation_finds_no_problems(data):
    assert g.validate(data) == []


def test_validation_catches_a_double_booking(data):
    broken = {k: [dict(row) for row in v] for k, v in data.items()}
    stay = next(r for r in broken["reservations"] if r["room_id"] and r["status"] != "cancelled")
    clone = dict(stay, reservation_id="RES999999")
    broken["reservations"].append(clone)
    assert any("double-booked" in p for p in g.validate(broken))


def test_validation_catches_a_wrong_folio_balance(data):
    broken = {k: [dict(row) for row in v] for k, v in data.items()}
    broken["folios"][0]["balance_cents"] += 1
    assert any("balance" in p for p in g.validate(broken))


def test_never_two_in_house_guests_in_one_room(data):
    in_house = [r["room_id"] for r in data["reservations"] if r["status"] == "in_house"]
    assert len(in_house) == len(set(in_house))


def test_validation_catches_two_guests_in_one_room(data):
    broken = {k: [dict(row) for row in v] for k, v in data.items()}
    stay = next(r for r in broken["reservations"] if r["status"] == "in_house")
    broken["reservations"].append(dict(stay, reservation_id="RES999999"))
    assert any("more than one in-house guest" in p for p in g.validate(broken))


@pytest.mark.parametrize("seed", range(1, 26))
def test_many_seeds_pass_validation(seed):
    """The generator must be consistent for any seed, not only the default one."""
    data = g.generate(seed=seed, today=TODAY, raw_path=NO_KAGGLE)
    assert g.validate(data) == []
