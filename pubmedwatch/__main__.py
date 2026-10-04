"""Command line entry point: python -m pubmedwatch <command>."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler

from .config import load_config
from .mailer import send
from .pubmed import PubMed
from .runner import make_http, run_once
from .scheduler import daemon


def _setup_logging(cfg, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(cfg.data_dir / "pubmedwatch.log", maxBytes=1_000_000,
                                            backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="pubmedwatch", description="PubMed-figyelő")
    parser.add_argument("-c", "--config", help="config.yaml útvonala")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon", help="folyamatos futás: API + napi ütemezés (a konténer alapparancsa)")
    run = sub.add_parser("run", help="egyszeri futás most")
    run.add_argument("--no-mail", action="store_true", help="ne küldjön levelet, csak mentse a jelentést")
    counts = sub.add_parser("counts", help="témánkénti találatszám az elmúlt napokban (lekérdezések hangolásához)")
    counts.add_argument("--days", type=int, default=30)
    sub.add_parser("serve", help="csak az API futtatása")
    sub.add_parser("test-mail", help="próbalevél küldése az SMTP-beállítások ellenőrzéséhez")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    _setup_logging(cfg, args.verbose)

    if args.command == "daemon":
        daemon(args.config)
    elif args.command == "run":
        result = run_once(cfg, send_mail=not args.no_mail)
        print(result.digest.text)
    elif args.command == "counts":
        pubmed = PubMed(make_http(cfg), cfg.sources.ncbi_api_key, cfg.sources.ncbi_email)
        until = date.today()
        since = until - timedelta(days=args.days)
        for topic in cfg.topics:
            n = pubmed.count(topic.query, since, until)
            print(f"{topic.id:<30} {n:6}  ~{n / args.days:6.1f}/nap")
    elif args.command == "serve":
        from .api import make_server
        server = make_server(cfg)
        print(f"API: http://0.0.0.0:{cfg.api.port}/health")
        server.serve_forever()
    elif args.command == "test-mail":
        send(cfg.mail, "PubMed-figyelő – próbalevél",
             "<p>Ez egy próbalevél a PubMed-figyelőtől. Az SMTP-beállítás működik.</p>",
             "Ez egy próbalevél a PubMed-figyelőtől. Az SMTP-beállítás működik.")
        print(f"próbalevél elküldve: {', '.join(cfg.mail.recipients)} ({cfg.mail.host}:{cfg.mail.port})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
