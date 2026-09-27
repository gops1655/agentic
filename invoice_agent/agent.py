"""The pipeline: email -> PDFs -> Claude extraction -> Excel -> reply."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import OUTPUT_DIR, Settings
from .excel_writer import SourceInvoice, reconcile, save_workbook
from .extractor import ExtractionError, InvoiceExtractor
from .mailer import IncomingEmail, build_reply, send_email
from .store import Store


@dataclass
class ConversionResult:
    items: list[SourceInvoice] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)
    excel_path: Path | None = None
    retry_later: bool = False  # a temporary API problem stopped at least one PDF

    @property
    def ok(self) -> bool:
        return bool(self.items)


def safe_name(text: str, fallback: str = "invoice") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    return (cleaned or fallback)[:60]


def email_key(mail: IncomingEmail) -> str:
    return mail.message_id or f"uid:{mail.uid}:{mail.sender_email}:{mail.subject}"


class InvoiceAgent:
    def __init__(self, settings: Settings, store: Store | None = None, log=print):
        self.settings = settings
        self.extractor = InvoiceExtractor(settings.anthropic_api_key, settings.claude_model)
        self.store = store or Store()
        self.log = log

    # ---- conversion ---------------------------------------------------
    def convert_pdfs(self, pdfs: list[tuple[str, bytes]], out_name: str) -> ConversionResult:
        result = ConversionResult()
        for filename, data in pdfs:
            self.log(f"  Reading [bold]{filename}[/bold] with Claude...")
            try:
                extracted = self.extractor.extract(data, filename)
            except ExtractionError as e:
                self.log(f"  [red]Failed:[/red] {e}")
                result.retry_later |= e.retryable
                if not e.retryable:
                    result.failed_files.append(filename)
                result.warnings.append(f"{filename}: {e}")
                continue
            if not extracted.invoices:
                result.failed_files.append(filename)
            result.items.extend(SourceInvoice(filename, inv) for inv in extracted.invoices)
            result.warnings.extend(f"{filename}: {w}" for w in extracted.warnings)
            self.log(f"  Found {len(extracted.invoices)} invoice(s) in {filename}")

        if result.items:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = OUTPUT_DIR / f"{safe_name(out_name)}_{stamp}.xlsx"
            result.excel_path = save_workbook(result.items, path, result.warnings)
            self.log(f"  Excel saved: {path}")
        return result

    def convert_email(self, mail: IncomingEmail) -> ConversionResult:
        first_pdf = Path(mail.pdfs[0].filename).stem if mail.pdfs else "invoice"
        name = first_pdf if len(mail.pdfs) == 1 else f"invoices_{safe_name(mail.sender_email)}"
        return self.convert_pdfs([(p.filename, p.data) for p in mail.pdfs], name)

    # ---- reply --------------------------------------------------------
    def reply_body(self, mail: IncomingEmail, result: ConversionResult) -> str:
        greeting = f"Hello {mail.sender_name}," if mail.sender_name else "Hello,"
        lines = [greeting, "", "Thank you for your invoice. Attached is the Excel version of the PDF(s) you sent.", ""]
        for item in result.items:
            inv = item.invoice
            total = f"{inv.currency or ''} {inv.total_amount:,.2f}".strip() if inv.total_amount is not None else "n/a"
            _, check = reconcile(inv)
            lines.append(
                f"  - {inv.invoice_number or '(no number)'} | {inv.invoice_date or '-'} | "
                f"{inv.vendor_name or '-'} | Total: {total} | {len(inv.line_items)} line(s) | Check: {check}"
            )
        if result.failed_files:
            lines += ["", "We could not read the following file(s); please resend them as text-based PDFs:"]
            lines += [f"  - {f}" for f in result.failed_files]
        lines += [
            "",
            "Sheets in the workbook: Summary, Line Items, Taxes.",
            "This file was generated automatically - please verify figures before posting.",
            "",
            "Regards,",
            "Invoice Agent",
        ]
        return "\n".join(lines)

    def failure_body(self, mail: IncomingEmail, result: ConversionResult) -> str:
        greeting = f"Hello {mail.sender_name}," if mail.sender_name else "Hello,"
        reasons = "\n".join(f"  - {w}" for w in result.warnings) or "  - No invoice data was found."
        return (
            f"{greeting}\n\nWe received your email but could not convert the attached PDF(s) to Excel:\n"
            f"{reasons}\n\nPlease check the file (not password-protected, readable scan) and resend.\n\n"
            "Regards,\nInvoice Agent"
        )

    def send_reply(self, mail: IncomingEmail, result: ConversionResult, body: str | None = None) -> None:
        attachments = [result.excel_path] if result.excel_path else []
        body = body or (self.reply_body(mail, result) if result.ok else self.failure_body(mail, result))
        send_email(self.settings, build_reply(self.settings, mail, body, attachments))
        self.log(f"  [green]Reply sent to {mail.reply_to}[/green]")

    def record(self, mail: IncomingEmail, result: ConversionResult, status: str, detail: str = "") -> None:
        self.store.record(
            email_key(mail),
            sender=mail.sender_email,
            subject=mail.subject,
            pdf_count=len(mail.pdfs),
            invoice_count=len(result.items),
            status=status,
            detail=detail or "; ".join(result.warnings)[:500],
            excel_path=str(result.excel_path or ""),
        )
