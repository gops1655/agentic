"""IMAP (reading) and SMTP (replying) helpers."""

from __future__ import annotations

import imaplib
import mimetypes
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path

from .config import Settings


@dataclass
class PdfAttachment:
    filename: str
    data: bytes


@dataclass
class IncomingEmail:
    uid: str
    message_id: str
    sender_name: str
    sender_email: str
    reply_to: str
    subject: str
    date: str
    references: str
    body_preview: str
    pdfs: list[PdfAttachment] = field(default_factory=list)


def _is_pdf(part) -> bool:
    filename = part.get_filename() or ""
    return part.get_content_type() == "application/pdf" or filename.lower().endswith(".pdf")


def parse_email(uid: str, raw: bytes) -> IncomingEmail:
    msg = message_from_bytes(raw, policy=policy.default)
    sender_name, sender_email = parseaddr(str(msg.get("From", "")))
    reply_to_addrs = getaddresses([str(msg.get("Reply-To", ""))])
    reply_to = next((a for _, a in reply_to_addrs if a), "") or sender_email

    pdfs: list[PdfAttachment] = []
    for i, part in enumerate(msg.walk()):
        if part.is_multipart() or not _is_pdf(part):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        name = part.get_filename() or f"attachment_{i}.pdf"
        pdfs.append(PdfAttachment(filename=name, data=payload))

    body = msg.get_body(preferencelist=("plain", "html"))
    preview = ""
    if body is not None:
        try:
            preview = " ".join(body.get_content().split())[:200]
        except (LookupError, UnicodeDecodeError):
            preview = ""

    return IncomingEmail(
        uid=uid,
        message_id=str(msg.get("Message-ID", "")).strip(),
        sender_name=sender_name,
        sender_email=sender_email.lower(),
        reply_to=reply_to,
        subject=str(msg.get("Subject", "")).strip(),
        date=str(msg.get("Date", "")),
        references=" ".join(str(msg.get("References", "")).split()),
        body_preview=preview,
        pdfs=pdfs,
    )


def matches_filters(mail: IncomingEmail, settings: Settings) -> bool:
    sender = mail.sender_email
    if settings.email_address and sender == settings.email_address.lower():
        return False  # never process our own replies
    if settings.allowed_senders and not sender_allowed(sender, settings.allowed_senders):
        return False
    if settings.search_since_days <= 0 and not received_today(mail):
        return False
    if settings.subject_keywords:
        subject = mail.subject.lower()
        if not any(k in subject for k in settings.subject_keywords):
            return False
    return True


def sender_allowed(sender: str, allowed: list[str]) -> bool:
    """Match an exact address, or a domain (with or without '@') including its subdomains."""
    domain = sender.rsplit("@", 1)[-1]
    for entry in allowed:
        if "@" in entry.lstrip("@"):
            if entry == sender:
                return True
        else:
            d = entry.lstrip("@")
            if domain == d or domain.endswith("." + d):
                return True
    return False


def received_today(mail: IncomingEmail, today: date | None = None) -> bool:
    """True when the email's Date header falls on today's date in this PC's timezone."""
    try:
        sent = parsedate_to_datetime(mail.date)
    except (TypeError, ValueError, IndexError):
        return True  # unreadable date: rely on the server's SINCE filter
    if sent is None:
        return True
    local = sent.astimezone() if sent.tzinfo else sent
    return local.date() == (today or date.today())


def _from_criteria(allowed: list[str]) -> list[str]:
    """IMAP FROM search terms so the server only returns mail from allowed senders."""
    terms = [["FROM", f'"{a.lstrip("@")}"'] for a in allowed]
    if not terms:
        return []
    criteria = terms[-1]
    for term in reversed(terms[:-1]):
        criteria = ["OR", *term, *criteria]
    return criteria


class Mailbox:
    """Context manager around an IMAP connection."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.imap: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "Mailbox":
        self.imap = imaplib.IMAP4_SSL(self.s.imap_host, self.s.imap_port, ssl_context=ssl.create_default_context())
        self.imap.login(self.s.email_address, self.s.email_password)
        status, _ = self.imap.select(_quote(self.s.imap_folder))
        if status != "OK":
            raise RuntimeError(f"Cannot open IMAP folder {self.s.imap_folder!r}")
        return self

    def __exit__(self, *exc) -> None:
        if self.imap is None:
            return
        try:
            self.imap.close()
        except imaplib.IMAP4.error:
            pass
        try:
            self.imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def search_uids(self) -> list[str]:
        # One extra day of margin for timezone differences; received_today() does the exact check.
        days = self.s.search_since_days if self.s.search_since_days > 0 else 1
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        criteria = ["SINCE", since, *_from_criteria(self.s.allowed_senders)]
        if self.s.only_unread:
            criteria.insert(0, "UNSEEN")
        status, data = self.imap.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        return data[0].decode().split() if data and data[0] else []

    def fetch(self, uid: str) -> IncomingEmail | None:
        # BODY.PEEK keeps the message unread until we have actually processed it.
        status, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return parse_email(uid, data[0][1])

    def fetch_invoice_emails(self) -> list[IncomingEmail]:
        mails = []
        for uid in self.search_uids():
            mail = self.fetch(uid)
            if mail and mail.pdfs and matches_filters(mail, self.s):
                mails.append(mail)
        return mails

    def mark_seen(self, uid: str) -> None:
        self.imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")


def _quote(folder: str) -> str:
    return folder if folder.startswith('"') else f'"{folder}"'


def build_reply(
    settings: Settings,
    original: IncomingEmail,
    body: str,
    attachments: list[Path],
) -> EmailMessage:
    reply = EmailMessage()
    reply["From"] = formataddr(("Invoice Agent", settings.email_address))
    reply["To"] = original.reply_to
    if settings.cc_address:
        reply["Cc"] = settings.cc_address
    subject = original.subject or "your invoice"
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    reply["Message-ID"] = make_msgid(domain=settings.email_address.rsplit("@", 1)[-1] or None)
    if original.message_id:
        reply["In-Reply-To"] = original.message_id
        reply["References"] = f"{original.references} {original.message_id}".strip()
    reply.set_content(body)

    for path in attachments:
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        reply.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return reply


def send_email(settings: Settings, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    if settings.smtp_security == "starttls":
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.email_address, settings.email_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=60) as smtp:
            smtp.login(settings.email_address, settings.email_password)
            smtp.send_message(message)


def test_imap(settings: Settings) -> str:
    with Mailbox(settings) as box:
        return f"IMAP OK - {len(box.search_uids())} matching message(s) in {settings.imap_folder}"


def test_smtp(settings: Settings) -> str:
    context = ssl.create_default_context()
    if settings.smtp_security == "starttls":
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.email_address, settings.email_password)
    else:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=30) as smtp:
            smtp.login(settings.email_address, settings.email_password)
    return "SMTP OK - login succeeded"
