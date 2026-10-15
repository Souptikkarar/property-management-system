"""Start the mock IDS Next:  python -m mock_ids [--simulate]"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from .config import CLIENTS, FIAS_PORT, HOST, HTTP_PORT, TOKEN_TTL_SECONDS
from .fias import FiasServer
from .simulator import build_plan, run
from .store import Store
from .xmlapi import AuthService, make_http_server

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "generated" / "hotel.sqlite"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m mock_ids", description="Mock IDS Next (demo only, fake data).")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--host", default=HOST)
    p.add_argument("--http-port", type=int, default=HTTP_PORT)
    p.add_argument("--fias-port", type=int, default=FIAS_PORT)
    p.add_argument("--token-ttl", type=int, default=TOKEN_TTL_SECONDS, help="seconds a token stays valid")
    p.add_argument("--rate-limit", type=int, default=0, help="requests per minute per client (0 = off)")
    p.add_argument("--simulate", action="store_true", help="play the day's check-ins, check-outs, charges")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between simulated events")
    p.add_argument("--start-delay", type=float, default=5.0, help="seconds to wait before simulating")
    p.add_argument("--chaos", action="store_true", help="FIAS feed duplicates and reorders some messages")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    try:
        store = Store(args.db)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    http = make_http_server(store, AuthService(CLIENTS, args.token_ttl), args.host, args.http_port,
                            args.rate_limit, args.verbose)
    fias = FiasServer(store, args.host, args.fias_port, args.chaos, args.seed)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    fias.start()

    print("Mock IDS Next is running (fake data, demo credentials, local only)")
    print(f"  XML API : http://{args.host}:{http.server_address[1]}/api/v1/   token TTL {args.token_ttl}s")
    print(f"  FIAS    : tcp://{args.host}:{fias.port}   chaos={'on' if args.chaos else 'off'}")
    print(f"  Business date {store.business_date}; clients: " + ", ".join(
        f"{c} / {v['secret']} ({'+'.join(sorted(v['scopes']))})" for c, v in CLIENTS.items()))
    print("  Ctrl+C to stop")

    stop = threading.Event()
    if args.simulate:
        plan = build_plan(store, args.seed)
        print(f"  Simulation: {len(plan)} events, starting in {args.start_delay:g}s")

        def _sim():
            if not stop.wait(args.start_delay):
                run(store, plan, args.interval, stop)
                print("[sim] finished")
        threading.Thread(target=_sim, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop.set()
    fias.stop()
    http.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
