import argparse
import asyncio
import logging
import secrets
import sys
import threading
from datetime import date
from pathlib import Path

from vdb.config import Settings, User, load_settings

log = logging.getLogger("vdb")


def api_users(settings: Settings, allow_temporary: bool) -> list[User]:
    """Users from config; for local runs without any, a temporary admin key is generated and printed once."""
    if settings.users:
        return settings.users
    if not allow_temporary:
        sys.exit("no users configured: add some with `vdb new-user` before starting the API")
    from vdb.api import hash_key

    key = secrets.token_urlsafe(32)
    print(f"\nNo users configured; temporary admin key for this run:\n  {key}\n")
    return [User(name="local-admin", key_sha256=hash_key(key))]


def threadsafe_publisher(bus, loop: asyncio.AbstractEventLoop):
    def report_failure(fut) -> None:
        if fut.exception():
            log.error("publish failed: %s", fut.exception())

    def publish(subject: str, msg) -> None:
        fut = asyncio.run_coroutine_threadsafe(bus.publish(subject, msg.model_dump_json().encode()), loop)
        fut.add_done_callback(report_failure)

    return publish


async def serve_api(settings: Settings, bus, users: list[User], host: str, port: int, after_start=None) -> None:
    import uvicorn

    from vdb.api import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(settings, bus, users), host=host, port=port, log_level="info"))
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.1)
    if after_start and server.started:
        after_start()
    await task


async def cmd_local(settings: Settings, args) -> None:
    """Everything in one process: worker + API, connected by an in-process bus (or NATS if configured)."""
    from vdb.bus import make_bus
    from vdb.worker import build_worker

    bus = await make_bus(settings.bus_url)
    publish = threadsafe_publisher(bus, asyncio.get_running_loop())
    stop = threading.Event()

    def start_worker() -> None:
        def run() -> None:
            try:
                build_worker(settings, settings.sites, publish).run_live(stop)
            except Exception:
                log.exception("worker stopped: monitoring is DOWN")

        threading.Thread(target=run, daemon=True, name="worker").start()

    try:
        await serve_api(settings, bus, api_users(settings, allow_temporary=True), args.host, args.port,
                        after_start=start_worker)
    finally:
        stop.set()
        await bus.close()


async def cmd_worker(settings: Settings, args) -> None:
    from vdb.bus import NatsBus
    from vdb.worker import build_worker

    if not settings.bus_url:
        sys.exit("bus_url must point at NATS for a standalone worker")
    sites = [s for s in settings.sites if not args.sites or s.id in args.sites.split(",")]
    bus = await NatsBus.connect(settings.bus_url)
    stop = threading.Event()
    worker = build_worker(settings, sites, threadsafe_publisher(bus, asyncio.get_running_loop()))
    try:
        await asyncio.to_thread(worker.run_live, stop)
    finally:
        stop.set()
        await bus.close()


async def cmd_api(settings: Settings, args) -> None:
    from vdb.bus import NatsBus

    if not settings.bus_url:
        sys.exit("bus_url must point at NATS for a standalone API")
    users = api_users(settings, allow_temporary=False)
    bus = await NatsBus.connect(settings.bus_url)
    try:
        await serve_api(settings, bus, users, args.host, args.port)
    finally:
        await bus.close()


async def cmd_report(settings: Settings, args) -> None:
    from vdb.db import init_db
    from vdb.reports import daily_report, render_report_html

    site = next((s for s in settings.sites if s.id == args.site), None)
    if site is None:
        sys.exit(f"unknown site {args.site}")
    sessions = await init_db(settings.database_url)
    async with sessions() as s:
        report = await daily_report(s, settings, site, date.fromisoformat(args.date))
    out = Path(args.out or f"report-{site.id}-{args.date}.html")
    out.write_text(render_report_html(report), encoding="utf-8")
    print(f"wrote {out}")


def cmd_new_user(args) -> None:
    from vdb.api import hash_key

    key = secrets.token_urlsafe(32)
    sites = ", ".join(f'"{x.strip()}"' for x in args.sites.split(","))
    print(f"API key for {args.name} (hand it over securely; it is not stored anywhere):\n  {key}\n")
    print(f"Add to the `users:` list in your config:\n"
          f"  - name: {args.name}\n    key_sha256: {hash_key(key)}\n    sites: [{sites}]")


def main() -> None:
    p = argparse.ArgumentParser(prog="vdb", description="Daycare vision safety monitoring")
    p.add_argument("--config", default="configs/local.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("local", "api"):
        sp = sub.add_parser(name)
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=8000)
    sw = sub.add_parser("worker")
    sw.add_argument("--sites", help="comma-separated site ids (default: all)")
    se = sub.add_parser("eval", help="score alerts against labelled videos")
    se.add_argument("--labels", required=True)
    se.add_argument("--target", type=float, default=0.9)
    se.add_argument("--tolerance", type=float, default=10.0, help="seconds of slack around each labelled event")
    se.add_argument("--min-events", type=int, default=20, help="labelled events needed per alert type to pass")
    sr = sub.add_parser("report", help="write a daily HTML report")
    sr.add_argument("--site", required=True)
    sr.add_argument("--date", default=date.today().isoformat())
    sr.add_argument("--out")
    sn = sub.add_parser("new-user", help="create an API key for a staff member (printed once)")
    sn.add_argument("--name", required=True)
    sn.add_argument("--sites", default="*", help="comma-separated site ids, or * for all")

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "new-user":
        cmd_new_user(args)
        return
    settings = load_settings(args.config)

    if args.cmd == "eval":
        from vdb.evaluate import run_eval

        sys.exit(0 if run_eval(settings, args.labels, args.target, args.tolerance, args.min_events) else 1)
    commands = {"local": cmd_local, "worker": cmd_worker, "api": cmd_api, "report": cmd_report}
    asyncio.run(commands[args.cmd](settings, args))


if __name__ == "__main__":
    main()
