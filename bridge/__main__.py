"""Command line: run (poller and web interface), once, or bootstrap."""

import argparse
import getpass
import logging
import signal
import sys
import threading
import time

from waitress import serve

from .config import ConfigError, load_dotenv, load_settings
from .connect import TokenStore, Tokens, clean_token, jwt_expiry, parse_login_json
from .service import Service
from .web import create_app

log = logging.getLogger("bridge")


def bootstrap(settings) -> int:
    print("Paste the login JSON or the access token. Nothing is shown as you paste.")
    raw = getpass.getpass("Login: ").strip()
    parsed = parse_login_json(raw)
    if parsed:
        access, refresh = parsed
    else:
        access = clean_token(raw)
        refresh = clean_token(getpass.getpass("Refresh token: "))
    expiry = jwt_expiry(access)
    if expiry is None:
        print("That access token does not look like a JWT; nothing was saved.", file=sys.stderr)
        return 1
    if not refresh:
        print("Warning: no refresh token, so the bridge stops working when the access token expires.", file=sys.stderr)
    TokenStore(settings.tokens_path).save(Tokens(access, refresh))
    print(f"Saved to {settings.tokens_path}. The access token expires in {int(expiry - time.time())} s.")
    return 0


def once(service: Service) -> int:
    return 0 if service.poll_once() else 1


def run(service: Service) -> int:
    service.start()
    app = create_app(service)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    port = service.settings.web_port
    threading.Thread(
        target=lambda: serve(app, host="0.0.0.0", port=port, threads=4, _quiet=True),
        name="web",
        daemon=True,
    ).start()
    log.info("Web interface on port %d", port)
    stop.wait()
    service.stop()
    log.info("Stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bridge", description="Prusa Connect to Spoolman bridge")
    parser.add_argument("command", choices=("bootstrap", "once", "run"), nargs="?", default="run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    try:
        if args.command == "bootstrap":
            return bootstrap(load_settings())
        service = Service()
        logging.getLogger().addHandler(service.logs)
        if args.command == "once":
            return 2 if service.config_error else once(service)
        return run(service)
    except ConfigError as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
