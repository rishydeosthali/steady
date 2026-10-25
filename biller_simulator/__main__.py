import argparse
import json
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from werkzeug.serving import make_server

from .app import create_biller


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    parser = argparse.ArgumentParser(description="Independent simulated biller: HTTP invoices and payment receipts")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5011)
    parser.add_argument("--db", default="work/external-biller.sqlite3")
    parser.add_argument("--as-of", type=date.fromisoformat)
    args = parser.parse_args()
    token = os.environ.get("BILLER_API_TOKEN", "").strip()
    try:
        configured = os.environ.get("STEADY_BILLER_DEMO_DATE", "").strip()
        anchor = args.as_of or (date.fromisoformat(configured) if configured else None)
        app = create_biller(args.db, token, as_of=anchor)
    except ValueError as exc:
        parser.error(str(exc))
    server = make_server(args.host, args.port, app, threaded=True)
    # A machine-readable ready line lets the process integration test use port 0.
    print(json.dumps({"service":"demo-biller", "url":f"http://{args.host}:{server.server_port}", "simulation":True}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
