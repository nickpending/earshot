"""The `earshot` command: `voice add <name> --from <ms> --to <ms>`, `import-limitless`, `redecode`, `rebuild`."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from collections.abc import Callable

from . import importer, limitless, lock, pipeline, store
from .config import Config, load


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="earshot")
    parser.add_argument(
        "--config", help="path to earshotd.toml (default: $EARSHOTD_CONFIG)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    voice = sub.add_parser("voice", help="enrolled voices").add_subparsers(
        dest="action", required=True
    )
    add = voice.add_parser(
        "add", help="enroll the voice that speaks in a range of stored audio"
    )
    add.add_argument("name")
    add.add_argument("--from", dest="from_ms", type=int, required=True, help="epoch ms")
    add.add_argument("--to", dest="to_ms", type=int, required=True, help="epoch ms")
    add.add_argument(
        "--replace", action="store_true", help="overwrite a voice already enrolled"
    )
    imp = sub.add_parser(
        "import-limitless",
        help="pull the Limitless archive (lifelogs and pendant audio) into the store",
    )
    imp.add_argument(
        "--api-base", default=limitless.BASE_URL, help=argparse.SUPPRESS
    )  # a stand-in server for tests
    sub.add_parser(
        "redecode",
        help="rebuild pendant records and audio pages from every stored raw page",
    )
    sub.add_parser(
        "rebuild",
        help="clear every transcript and speaker so the worker redoes all stored audio",
    )
    return parser


def _read_key() -> str:
    """The Limitless key: a hidden prompt on a terminal, else the first line of stdin."""
    if sys.stdin.isatty():
        return getpass.getpass("Limitless API key: ").strip()
    return sys.stdin.readline().strip()


def import_limitless(
    config: Config,
    api_base: str,
    sleep: Callable[[float], None],
    read_key: Callable[[], str],
) -> int:
    key = read_key()
    if not key:
        print("earshot: no Limitless API key was given", file=sys.stderr)
        return 1
    store.init(config.db_path)
    try:
        client = limitless.Client(key, api_base, sleep=sleep)
        report = importer.run(config, client)
    except limitless.LimitlessError as e:
        print(
            f"earshot: import failed, nothing more was imported: {e}", file=sys.stderr
        )
        return 1
    print(importer.summary(report))
    return 0 if report.ok else 1


def redecode(config: Config) -> int:
    store.init(config.db_path)
    # Its one write transaction holds the store for tens of seconds on a real archive, beside a
    # worker whose commits time out: refuse instead, as rebuild does.
    try:
        held = lock.acquire(config.data_dir)
    except lock.Held:
        print(
            "earshot: an earshotd worker is running; stop earshotd, then redecode",
            file=sys.stderr,
        )
        return 1
    try:
        count, failed = store.redecode(config.db_path)
    finally:
        held.close()
    print(f"redecoded {count} raw pages")
    for message in failed:
        print(f"earshot: could not decode {message}", file=sys.stderr)
    return 1 if failed else 0


def rebuild(config: Config) -> int:
    store.init(config.db_path)
    try:
        held = lock.acquire(config.data_dir)
    except lock.Held:
        print(
            "earshot: an earshotd worker is running; stop earshotd, then rebuild",
            file=sys.stderr,
        )
        return 1
    try:
        cleared = store.rebuild(config.db_path)
    finally:
        held.close()
    print(
        f"cleared {cleared['segments']} segments, {cleared['day_speakers']} day_speakers; "
        f"{cleared['pages']} pages and {cleared['limitless_audio']} imported chunks "
        "will be transcribed again"
    )
    return 0


def main(
    argv: list[str] | None = None,
    config: Config | None = None,
    models: pipeline.Models | None = None,
    limitless_sleep: Callable[[float], None] = time.sleep,
    read_key: Callable[[], str] = _read_key,
) -> int:
    os.umask(0o077)  # first: everything this process creates is private
    args = build_parser().parse_args(argv)
    config = config or load(args.config)
    if args.command == "import-limitless":
        return import_limitless(config, args.api_base, limitless_sleep, read_key)
    if args.command == "redecode":
        return redecode(config)
    if args.command == "rebuild":
        return rebuild(config)
    try:
        pipeline.enroll(
            config,
            models or pipeline.LocalModels(config),
            args.name,
            args.from_ms,
            args.to_ms,
            args.replace,
        )
    except pipeline.EnrollError as e:
        print(f"earshot: {e}", file=sys.stderr)
        return 1
    print(f"enrolled {args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
