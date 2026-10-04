"""Run the sandbox: python -m sandbox [--reset] [--chaos on|off] [--port 8000]"""
import argparse

import uvicorn

from . import seed
from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated company intranet for the Alfred worker")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="wipe the database back to the seed data first")
    ap.add_argument("--chaos", choices=["on", "off"], default="on",
                    help="inject a one-off 503 on save and a one-off session expiry (default on)")
    args = ap.parse_args()
    if args.reset:
        seed.seed(reset=True)
    print(f"Sandbox on http://127.0.0.1:{args.port}  (chaos {args.chaos})")
    uvicorn.run(create_app(chaos=args.chaos == "on"), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
