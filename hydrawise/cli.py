"""Command line entry point: ``python -m hydrawise`` (or ``hydrawise``).

Two families of commands:

*live control* — ``controllers``, ``status``, ``zones``, ``run``, ``stop``,
``suspend``, ``resume`` — talk to the Hydrawise cloud right now.

*reporting* — ``poll``, ``runs``, ``report``, ``send-reports`` — build and use
the local run log that the monthly per-person bills are computed from.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .client import DEFAULT_BASE_URL, HydrawiseClient
from .config import EXAMPLE_CONFIG, Config, ConfigError
from .errors import HydrawiseError
from .models import StatusSchedule
from .poller import PollOutcome, poll_forever, poll_once
from .report import format_duration, render_csv, render_person_html, render_summary, to_dict
from .storage import RunStore
from .usage import build_report, month_bounds, previous_month

__all__ = ["main", "build_parser"]

DEFAULT_DB = "hydrawise.db"
DEFAULT_CONFIG = "hydrawise.config.json"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ----------------------------------------------------------------------
# argument parsing
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hydrawise",
        description="Control a Hydrawise irrigation controller and bill its water and power per person.",
    )
    parser.add_argument(
        "--api-key",
        help="Hydrawise API key (default: the environment variable named by the config, or $HYDRAWISE_API_KEY)",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG, help=f"site config file (default: {DEFAULT_CONFIG})"
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"run log database (default: {DEFAULT_DB})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument("--controller-id", type=int, help="controller to address, for multi-controller accounts")
    parser.add_argument("--json", action="store_true", help="print raw JSON instead of a table")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-config", help="write a starter config file")
    sub.add_parser("controllers", help="list the controllers on the account")
    sub.add_parser("status", help="show controller status, zones and live runs")
    sub.add_parser("zones", help="list zones with their next scheduled run")

    run = sub.add_parser("run", help="start a zone (or every zone)")
    run.add_argument("zone", nargs="?", help="zone number, relay id, or name")
    run.add_argument("--all", action="store_true", help="run every zone in sequence")
    run.add_argument("--minutes", type=float, help="run time in minutes")
    run.add_argument("--seconds", type=int, help="run time in seconds")

    stop = sub.add_parser("stop", help="stop a zone (or every zone)")
    stop.add_argument("zone", nargs="?", help="zone number, relay id, or name")
    stop.add_argument("--all", action="store_true", help="stop every zone")

    suspend = sub.add_parser("suspend", help="suspend a zone (or every zone)")
    suspend.add_argument("zone", nargs="?", help="zone number, relay id, or name")
    suspend.add_argument("--all", action="store_true", help="suspend every zone")
    suspend.add_argument("--days", type=float, help="suspend for this many days")
    suspend.add_argument("--until", help="suspend until an ISO 8601 timestamp")

    resume = sub.add_parser("resume", help="clear a suspension")
    resume.add_argument("zone", nargs="?", help="zone number, relay id, or name")
    resume.add_argument("--all", action="store_true", help="resume every zone")

    poll = sub.add_parser("poll", help="record watering into the local run log")
    poll.add_argument("--interval", type=float, default=60.0, help="seconds between polls")
    poll.add_argument("--once", action="store_true", help="poll a single time and exit")
    poll.add_argument("--iterations", type=int, help="stop after this many polls")
    poll.add_argument("--quiet", action="store_true", help="only print run starts and finishes")

    runs = sub.add_parser("runs", help="list runs from the local log")
    runs.add_argument("--month", help="limit to a month (YYYY-MM)")
    runs.add_argument("--limit", type=int, default=50, help="most recent N runs (default: 50)")

    report = sub.add_parser("report", help="build the per-person usage report")
    report.add_argument("--month", help="month to report (YYYY-MM, default: last month)")
    report.add_argument(
        "--format",
        choices=["text", "json", "csv", "html"],
        default="text",
        help="output format (html renders one person's mail body)",
    )
    report.add_argument("--person", help="limit to one person id")
    report.add_argument("--output", help="write to this file instead of stdout")

    send = sub.add_parser("send-reports", help="email each person their monthly report")
    send.add_argument("--month", help="month to send (YYYY-MM, default: last month)")
    send.add_argument("--dry-run", action="store_true", help="compose but do not send")
    send.add_argument("--person", action="append", help="only this person id (repeatable)")
    send.add_argument("--skip-empty", action="store_true", help="skip people with no usage")

    return parser


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _load_config(args: argparse.Namespace, *, required: bool) -> Config:
    path = Path(args.config).expanduser()
    if path.exists():
        return Config.load(path)
    if required:
        raise ConfigError(
            f"no config file at {path}; run `hydrawise init-config` and fill it in"
        )
    return Config()


def _resolve_api_key(args: argparse.Namespace, config: Config) -> str:
    key = args.api_key or config.api_key or os.environ.get("HYDRAWISE_API_KEY")
    if not key:
        raise HydrawiseError(
            "no API key: pass --api-key or export "
            f"${config.api_key_env} (find it in app.hydrawise.com → My Account → Account Details)"
        )
    return key


def _client(args: argparse.Namespace, config: Config) -> HydrawiseClient:
    return HydrawiseClient(
        _resolve_api_key(args, config),
        base_url=args.base_url,
        timeout=args.timeout,
    )


def _controller_id(args: argparse.Namespace, config: Config) -> Optional[int]:
    return args.controller_id if args.controller_id is not None else config.controller_id


def _resolve_zone(status: StatusSchedule, identifier: str) -> int:
    zone = status.zone(identifier)
    if zone is None or zone.relay_id is None:
        known = ", ".join(
            f"{item.number}:{item.name}" for item in status.zones if item.name
        )
        raise HydrawiseError(f"no zone matches {identifier!r}. Known zones: {known}")
    return zone.relay_id


def _dump(payload: Any, out) -> None:
    json.dump(payload, out, indent=2, ensure_ascii=False, default=str)
    out.write("\n")


def _month(args: argparse.Namespace, config: Config) -> str:
    if getattr(args, "month", None):
        return args.month
    return previous_month(_utcnow(), config.timezone)


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def _cmd_init_config(args: argparse.Namespace, out) -> int:
    path = Path(args.config).expanduser()
    if path.exists():
        out.write(f"{path} already exists; not overwriting it.\n")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(EXAMPLE_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    out.write(
        f"Wrote {path}.\n"
        "Next: set each zone's flow_rate_lpm and owner, then export your API key:\n"
        "  export HYDRAWISE_API_KEY=...\n"
    )
    return 0


def _cmd_controllers(args: argparse.Namespace, config: Config, out) -> int:
    details = _client(args, config).customer_details()
    if args.json:
        _dump(details.raw, out)
        return 0
    out.write(f"customer_id: {details.customer_id}\n")
    for controller in details.controllers:
        last = controller.last_contact.isoformat() if controller.last_contact else "never"
        out.write(
            f"  [{controller.controller_id}] {controller.name or '?'} "
            f"— {controller.status or 'unknown'} — last contact {last}\n"
        )
    if not details.controllers:
        out.write("  (no controllers on this account)\n")
    return 0


def _cmd_status(args: argparse.Namespace, config: Config, out) -> int:
    client = _client(args, config)
    status = client.status_schedule(_controller_id(args, config))
    if args.json:
        _dump(status.raw, out)
        return 0
    out.write(f"{status.name or 'controller'} — {status.status or 'unknown'}\n")
    if status.message:
        out.write(f"{status.message}\n")
    out.write("\n")
    _write_zone_table(status, out)
    if status.running:
        out.write("\nRunning now:\n")
        for item in status.running:
            out.write(
                f"  {item.name or item.relay_id}: "
                f"{format_duration(item.time_left or 0)} left of "
                f"{format_duration(item.run_seconds or 0)}\n"
            )
    else:
        out.write("\nNothing is watering right now.\n")
    for sensor in status.sensors:
        out.write(f"\nSensor {sensor.name or sensor.input}: type={sensor.type} mode={sensor.mode}\n")
    return 0


def _cmd_zones(args: argparse.Namespace, config: Config, out) -> int:
    client = _client(args, config)
    status = client.status_schedule(_controller_id(args, config))
    if args.json:
        _dump([zone.raw for zone in status.zones], out)
        return 0
    _write_zone_table(status, out)
    return 0


def _write_zone_table(status: StatusSchedule, out) -> None:
    out.write(f"{'#':>3}  {'relay_id':>9}  {'zone':<24}  {'next run':<26}  {'last water'}\n")
    out.write(f"{'-'*3}  {'-'*9}  {'-'*24}  {'-'*26}  {'-'*10}\n")
    for zone in status.zones:
        if status.is_running(zone.relay_id or -1):
            next_run = "watering now"
        elif zone.is_scheduled:
            next_run = zone.nice_time or zone.time_string or "scheduled"
        else:
            next_run = "not scheduled"
        out.write(
            f"{zone.number if zone.number is not None else '?':>3}  "
            f"{zone.relay_id if zone.relay_id is not None else '?':>9}  "
            f"{(zone.name or '')[:24]:<24}  {next_run[:26]:<26}  {zone.last_water or ''}\n"
        )


def _cmd_run(args: argparse.Namespace, config: Config, out) -> int:
    client = _client(args, config)
    seconds: Optional[int] = None
    if args.seconds is not None:
        seconds = args.seconds
    elif args.minutes is not None:
        seconds = int(args.minutes * 60)
    if args.all:
        result = client.run_all_zones(seconds)
    else:
        if not args.zone:
            raise HydrawiseError("name a zone, or pass --all")
        status = client.status_schedule(_controller_id(args, config))
        result = client.run_zone(_resolve_zone(status, args.zone), seconds)
    return _write_command_result(result, args, out)


def _cmd_stop(args: argparse.Namespace, config: Config, out) -> int:
    client = _client(args, config)
    if args.all:
        result = client.stop_all_zones()
    else:
        if not args.zone:
            raise HydrawiseError("name a zone, or pass --all")
        status = client.status_schedule(_controller_id(args, config))
        result = client.stop_zone(_resolve_zone(status, args.zone))
    return _write_command_result(result, args, out)


def _cmd_suspend(args: argparse.Namespace, config: Config, out) -> int:
    client = _client(args, config)
    if args.until:
        try:
            until: Any = datetime.fromisoformat(args.until)
        except ValueError as exc:
            raise HydrawiseError(f"--until must be an ISO 8601 timestamp: {exc}") from exc
    elif args.days is not None:
        until = timedelta(days=args.days)
    else:
        raise HydrawiseError("pass --days or --until")
    if args.all:
        result = client.suspend_all_zones(until)
    else:
        if not args.zone:
            raise HydrawiseError("name a zone, or pass --all")
        status = client.status_schedule(_controller_id(args, config))
        result = client.suspend_zone(_resolve_zone(status, args.zone), until)
    return _write_command_result(result, args, out)


def _cmd_resume(args: argparse.Namespace, config: Config, out) -> int:
    client = _client(args, config)
    if args.all:
        result = client.resume_all_zones()
    else:
        if not args.zone:
            raise HydrawiseError("name a zone, or pass --all")
        status = client.status_schedule(_controller_id(args, config))
        result = client.resume_zone(_resolve_zone(status, args.zone))
    return _write_command_result(result, args, out)


def _write_command_result(result, args: argparse.Namespace, out) -> int:
    if args.json:
        _dump(result.raw, out)
    else:
        out.write((result.message or "done").strip() + "\n")
    return 0 if result.ok else 1


def _cmd_poll(args: argparse.Namespace, config: Config, out) -> int:
    client = _client(args, config)
    controller_id = _controller_id(args, config)
    with RunStore(args.db) as store:
        if args.once:
            events = poll_once(client, store, controller_id=controller_id)
            _write_events(PollOutcome(_utcnow(), events), args, out)
            return 0

        def report(outcome: PollOutcome) -> None:
            _write_events(outcome, args, out)

        iterations = args.iterations
        out.write(
            f"Polling every {args.interval:g}s into {args.db}. Ctrl-C to stop.\n"
        )
        out.flush()
        try:
            poll_forever(
                client,
                store,
                interval=args.interval,
                controller_id=controller_id,
                on_outcome=report,
                max_iterations=iterations,
            )
        except KeyboardInterrupt:
            out.write("\nStopped.\n")
    return 0


def _write_events(outcome: PollOutcome, args: argparse.Namespace, out) -> None:
    stamp = outcome.polled_at.isoformat(timespec="seconds")
    if outcome.error is not None:
        out.write(f"{stamp}  ! {outcome.error}\n")
        out.flush()
        return
    interesting = [event for event in outcome.events if event.kind != "updated"]
    if not interesting and args.quiet:
        return
    if not outcome.events and not args.quiet:
        out.write(f"{stamp}  idle\n")
    for event in outcome.events:
        if event.kind == "updated" and args.quiet:
            continue
        run = event.run
        detail = f"{run.zone_name or run.relay_id}"
        if event.kind == "finished":
            detail += f" ({format_duration(run.seconds)})"
        out.write(f"{stamp}  {event.kind:<8} {detail}\n")
    out.flush()


def _cmd_runs(args: argparse.Namespace, config: Config, out) -> int:
    with RunStore(args.db) as store:
        if args.month:
            start, end = month_bounds(args.month, config.timezone)
            records = store.runs_between(start, end)
        else:
            records = store.all_runs()
    records = records[-args.limit :] if args.limit else records
    if args.json:
        _dump(
            [
                {
                    "relay_id": record.relay_id,
                    "zone": record.zone_number,
                    "name": record.zone_name,
                    "started_at": record.started_at.isoformat(),
                    "ended_at": record.ended_at.isoformat() if record.ended_at else None,
                    "seconds": record.seconds,
                }
                for record in records
            ],
            out,
        )
        return 0
    if not records:
        out.write("No runs logged yet. Start `hydrawise poll` and let it watch.\n")
        return 0
    out.write(f"{'started (UTC)':<20}  {'zone':<24}  {'duration':>10}\n")
    out.write(f"{'-'*20}  {'-'*24}  {'-'*10}\n")
    for record in records:
        label = record.zone_name or f"relay {record.relay_id}"
        marker = "" if record.ended_at else "  (running)"
        out.write(
            f"{record.started_at.strftime('%Y-%m-%d %H:%M:%S'):<20}  "
            f"{label[:24]:<24}  {format_duration(record.seconds):>10}{marker}\n"
        )
    return 0


def _cmd_report(args: argparse.Namespace, config: Config, out) -> int:
    month = _month(args, config)
    start, end = month_bounds(month, config.timezone)
    with RunStore(args.db) as store:
        records = store.runs_between(start, end)
    report = build_report(
        records, config, period=month, start=start, end=end, generated_at=_utcnow()
    )

    if args.format == "json" or args.json:
        text = json.dumps(to_dict(report), indent=2, ensure_ascii=False)
    elif args.format == "csv":
        text = render_csv(report)
    elif args.format == "html":
        if not args.person:
            raise HydrawiseError("--format html needs --person, since the page is one person's mail")
        person = report.person(args.person)
        if person is None:
            raise HydrawiseError(f"no person with id {args.person!r} in {config.path or 'the config'}")
        text = render_person_html(person, report)
    else:
        text = render_summary(report)

    if args.output:
        Path(args.output).expanduser().write_text(text, encoding="utf-8")
        out.write(f"Wrote {args.output}\n")
    else:
        out.write(text if text.endswith("\n") else text + "\n")
    return 0


def _cmd_send_reports(args: argparse.Namespace, config: Config, out) -> int:
    from .mailer import send_reports  # imported late: smtplib is only needed here

    month = _month(args, config)
    start, end = month_bounds(month, config.timezone)
    with RunStore(args.db) as store:
        records = store.runs_between(start, end)
    report = build_report(
        records, config, period=month, start=start, end=end, generated_at=_utcnow()
    )
    if not args.dry_run and not config.email.is_configured:
        raise HydrawiseError(
            "email.smtp_host and email.from_address must be set before sending"
        )
    results = send_reports(
        report,
        config.email,
        dry_run=args.dry_run,
        only=args.person,
        skip_empty=args.skip_empty,
    )
    failures = 0
    for result in results:
        out.write(f"{result.status:<8} {result.person_id:<16} {result.email or '-'}")
        if result.detail:
            out.write(f"  ({result.detail})")
        out.write("\n")
        if result.status == "failed":
            failures += 1
    if not results:
        out.write("Nobody to send to.\n")
    return 1 if failures else 0


_LIVE_COMMANDS = {
    "controllers": _cmd_controllers,
    "status": _cmd_status,
    "zones": _cmd_zones,
    "run": _cmd_run,
    "stop": _cmd_stop,
    "suspend": _cmd_suspend,
    "resume": _cmd_resume,
    "poll": _cmd_poll,
}
_LOCAL_COMMANDS = {
    "runs": _cmd_runs,
    "report": _cmd_report,
    "send-reports": _cmd_send_reports,
}


def main(argv: Optional[Sequence[str]] = None, out=None) -> int:
    """Run one CLI invocation and return its exit status."""
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init-config":
            return _cmd_init_config(args, out)
        needs_config = args.command in {"report", "send-reports"}
        config = _load_config(args, required=needs_config)
        handler = _LIVE_COMMANDS.get(args.command) or _LOCAL_COMMANDS[args.command]
        return handler(args, config, out)
    except (HydrawiseError, ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
