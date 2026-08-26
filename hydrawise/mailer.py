"""Delivering one monthly report per person over SMTP.

The SMTP password is read from the environment variable named in the config,
so nothing secret is written to disk by this package. The actual send is
behind an injectable callable, which is how ``--dry-run`` and the tests avoid
touching a mail server.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, List, Optional, Sequence

from .config import EmailConfig
from .report import render_person_html, render_person_text
from .usage import UsageReport

__all__ = ["SendResult", "Mailer", "build_message", "send_reports"]

Sender = Callable[[EmailMessage], None]


@dataclass
class SendResult:
    """What happened to one person's mail."""

    person_id: str
    email: Optional[str]
    status: str  # "sent" | "skipped" | "failed"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "sent"


class Mailer:
    """Sends :class:`~email.message.EmailMessage` objects over SMTP."""

    def __init__(self, config: EmailConfig, sender: Optional[Sender] = None) -> None:
        self.config = config
        self._sender = sender or self._smtp_send

    def send(self, message: EmailMessage) -> None:
        self._sender(message)

    def _smtp_send(self, message: EmailMessage) -> None:
        config = self.config
        if not config.smtp_host:
            raise RuntimeError("email.smtp_host is not configured")
        if config.use_ssl:
            client = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30)
        else:
            client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
        with client:
            client.ehlo()
            if config.use_starttls and not config.use_ssl:
                client.starttls()
                client.ehlo()
            if config.username:
                password = config.password
                if not password:
                    raise RuntimeError(
                        f"no SMTP password in ${config.password_env}; export it before sending"
                    )
                client.login(config.username, password)
            client.send_message(message)


def build_message(
    report: UsageReport,
    person_usage,
    config: EmailConfig,
    *,
    to: Optional[str] = None,
) -> EmailMessage:
    """Compose one person's multipart (text + HTML) monthly report."""
    person = person_usage.person
    recipient = to or person.email
    if not recipient:
        raise ValueError(f"{person.id} has no email address")

    message = EmailMessage()
    message["Subject"] = config.subject_template.format(
        period=report.period, name=person.display_name, id=person.id
    )
    message["From"] = config.from_address or config.username or "hydrawise@localhost"
    message["To"] = recipient
    if config.bcc:
        message["Bcc"] = ", ".join(config.bcc)
    message.set_content(render_person_text(person_usage, report))
    message.add_alternative(render_person_html(person_usage, report), subtype="html")
    return message


def send_reports(
    report: UsageReport,
    config: EmailConfig,
    *,
    dry_run: bool = False,
    mailer: Optional[Mailer] = None,
    only: Optional[Sequence[str]] = None,
    skip_empty: bool = False,
) -> List[SendResult]:
    """Send every person their own report.

    ``skip_empty`` suppresses mail to people whose zones did not run at all.
    Failures are collected rather than raised, so one bad address does not
    stop the rest of the month's mail.
    """
    results: List[SendResult] = []
    mailer = mailer or Mailer(config)
    wanted = set(only) if only else None

    for person_usage in report.people:
        person = person_usage.person
        if wanted is not None and person.id not in wanted:
            continue
        if not person.email:
            results.append(
                SendResult(person.id, None, "skipped", "no email address configured")
            )
            continue
        if skip_empty and person_usage.seconds == 0:
            results.append(
                SendResult(person.id, person.email, "skipped", "no usage this period")
            )
            continue
        try:
            message = build_message(report, person_usage, config)
        except ValueError as exc:
            results.append(SendResult(person.id, person.email, "skipped", str(exc)))
            continue
        if dry_run:
            results.append(
                SendResult(person.id, person.email, "skipped", "dry run, not sent")
            )
            continue
        try:
            mailer.send(message)
        except Exception as exc:  # noqa: BLE001 - one bad address must not stop the run
            results.append(SendResult(person.id, person.email, "failed", str(exc)))
            continue
        results.append(SendResult(person.id, person.email, "sent"))

    return results
