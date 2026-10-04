"""Report delivery through the (Synology) SMTP server."""
from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .config import MailConfig

log = logging.getLogger(__name__)


def build_message(mail: MailConfig, subject: str, html: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((mail.sender_name, mail.sender))
    msg["To"] = ", ".join(mail.recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=mail.sender.rsplit("@", 1)[-1])
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send(mail: MailConfig, subject: str, html: str, text: str) -> None:
    msg = build_message(mail, subject, html, text)
    context = ssl.create_default_context()
    if not mail.verify_tls:  # NAS certificates are usually self-signed
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    if mail.security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(mail.host, mail.port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(mail.host, mail.port, timeout=30)
    with server:
        server.ehlo()
        if mail.security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if mail.user:
            server.login(mail.user, mail.password)
        server.send_message(msg, from_addr=mail.sender, to_addrs=mail.recipients)


def send_with_retry(mail: MailConfig, subject: str, html: str, text: str,
                    attempts: int = 3, wait_seconds: float = 120.0) -> None:
    """The mail server runs on the same NAS and may still be starting after a reboot."""
    for attempt in range(1, attempts + 1):
        try:
            send(mail, subject, html, text)
            return
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("levélküldés sikertelen (%d/%d): %s", attempt, attempts, exc)
            if attempt == attempts:
                raise
            time.sleep(wait_seconds)
