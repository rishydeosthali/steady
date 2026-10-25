"""Configure or control the external biller integration, without printing keys."""

import argparse
import json
import os
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from dotenv import dotenv_values, load_dotenv, set_key

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("init","status","check","advance","receipts"))
    parser.add_argument("--days",type=int,default=1)
    parser.add_argument("--steady-url",default="http://127.0.0.1:5000")
    args = parser.parse_args()
    path = ROOT/".env"
    if args.command == "init":
        ignored = subprocess.run(["git","check-ignore","-q","--",".env"],cwd=ROOT).returncode == 0
        tracked = subprocess.run(["git","ls-files","--error-unmatch",".env"],cwd=ROOT,
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode == 0
        if not ignored or tracked:
            parser.error("Keep .env gitignored and untracked before generating local settings")
        path.touch(mode=0o600,exist_ok=True)
        values = dotenv_values(path)
        defaults = {"BILLER_API_TOKEN":secrets.token_urlsafe(24),
                    "STEADY_PAYMENT_CONTROL_TOKEN":secrets.token_urlsafe(24),
                    "BILLER_BASE_URL":"http://127.0.0.1:5011", "BILLER_CUSTOMER_REF":"demo-margaret",
                    "STEADY_EXTERNAL_BILLER":"1", "STEADY_PAYMENT_DB":"work/external-payments.sqlite3",
                    "STEADY_BILLER_DEMO_DATE":datetime.now(ZoneInfo("America/Chicago")).date().isoformat()}
        for key,value in defaults.items():
            if not values.get(key):
                set_key(str(path),key,value,quote_mode="always")
        os.chmod(path,0o600)
        print("Saved local biller configuration. Existing account credentials were preserved; secret values are not displayed.")
        print("Start terminal 1: python -m biller_simulator")
        print("Start terminal 2: python -m backend.app")
        return
    load_dotenv(path,override=False)
    if args.command == "receipts":
        from backend.payments.client import BillerClient, BillerError
        try:
            client=BillerClient(os.environ.get("BILLER_BASE_URL",""),os.environ.get("BILLER_API_TOKEN",""))
            print(json.dumps(client._request("GET","/v1/payments"),indent=2))
        except (ValueError,BillerError) as exc:
            parser.error(str(exc))
        return
    parsed=urlsplit(args.steady_url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or (
        parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1","localhost"})):
        parser.error("Use an HTTPS Steady URL or HTTP loopback")
    token=os.environ.get("STEADY_PAYMENT_CONTROL_TOKEN","")
    if not token:
        parser.error("Run init or configure STEADY_PAYMENT_CONTROL_TOKEN")
    suffix={"status":"","check":"/check","advance":"/advance"}[args.command]
    kwargs={"headers":{"Authorization":"Bearer "+token},"timeout":45,"allow_redirects":False}
    if args.command != "status":
        kwargs["json"]={"days":args.days} if args.command == "advance" else {}
    try:
        response=requests.request("GET" if args.command == "status" else "POST",
                                  args.steady_url.rstrip("/")+"/demo/payments"+suffix,**kwargs)
        if not response.ok:
            parser.error(f"Steady returned HTTP {response.status_code}; check configuration and auth")
        print(json.dumps(response.json(),indent=2))
    except requests.RequestException as exc:
        parser.error("Steady connection failed: "+type(exc).__name__)


if __name__ == "__main__":
    main()
