"""countersigned seed-demo | worker | verify | serve"""

import argparse
import json
import os
import sys
import time

from .db import Database
from .seed import default_policy, seed_demo
from .service import Gate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="countersigned")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), help="Postgres DSN (or DATABASE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed-demo", help="create demo agents, approvers and a simulated business")
    s.add_argument("--reset", action="store_true", help="wipe everything first")
    sub.add_parser("worker", help="run the executor loop")
    sub.add_parser("verify", help="check the audit hash chain; exit 1 if it was tampered with")
    sv = sub.add_parser("serve", help="run the API")
    sv.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    sv.add_argument("--no-worker", action="store_true")
    args = ap.parse_args(argv)

    if not args.database_url:
        print("set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    gate = Gate(Database(args.database_url))

    if args.cmd == "seed-demo":
        seed_demo(gate, reset=args.reset)
        print("seeded. demo API keys are in countersigned.seed.DEMO_KEYS (local use only)")
        return 0
    if args.cmd == "verify":
        res = gate.verify_audit()
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1
    if args.cmd == "worker":
        gate.activate_policy(default_policy())
        print("worker running; Ctrl-C to stop")
        while True:
            gate.release_due()
            gate.expire_stale()
            if gate.run_worker_once() is None:
                time.sleep(0.5)
    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        gate.activate_policy(default_policy())
        uvicorn.run(create_app(gate, inline_worker=not args.no_worker), host="0.0.0.0", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
