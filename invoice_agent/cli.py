"""Interactive console for the invoice agent."""

from __future__ import annotations

import argparse
import imaplib
import smtplib
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from .agent import ConversionResult, InvoiceAgent, email_key
from .config import ENV_FILE, Settings, write_env
from .excel_writer import reconcile
from .mailer import IncomingEmail, Mailbox, test_imap, test_smtp

console = Console()

MAIL_ERRORS = (imaplib.IMAP4.error, smtplib.SMTPException, socket.error, OSError, RuntimeError)

PRESETS = {
    "gmail": ("imap.gmail.com", "993", "smtp.gmail.com", "465", "ssl"),
    "outlook": ("outlook.office365.com", "993", "smtp.office365.com", "587", "starttls"),
    "zoho": ("imap.zoho.in", "993", "smtp.zoho.in", "465", "ssl"),
    "yahoo": ("imap.mail.yahoo.com", "993", "smtp.mail.yahoo.com", "465", "ssl"),
}


# ---------------------------------------------------------------- display
def show_result(result: ConversionResult) -> None:
    if not result.items:
        console.print("[yellow]No invoice data extracted.[/yellow]")
    else:
        table = Table(title="Extracted invoices", show_lines=False)
        for col in ("PDF", "Invoice No", "Date", "Vendor", "Total", "Lines", "Check"):
            table.add_column(col, overflow="fold")
        for item in result.items:
            inv = item.invoice
            _, check = reconcile(inv)
            total = f"{inv.currency or ''} {inv.total_amount:,.2f}" if inv.total_amount is not None else "-"
            style = "red" if check.startswith("Mismatch") else "green" if check == "OK" else "yellow"
            table.add_row(item.source_file, inv.invoice_number or "-", inv.invoice_date or "-",
                          inv.vendor_name or "-", total, str(len(inv.line_items)), f"[{style}]{check}[/{style}]")
        console.print(table)

        first = result.items[0].invoice
        if first.line_items:
            lines = Table(title=f"Line items of {first.invoice_number or 'first invoice'} (first 10)")
            for col in ("Description", "Qty", "Rate", "Tax %", "Amount"):
                lines.add_column(col, overflow="fold")
            for li in first.line_items[:10]:
                lines.add_row(li.description, _num(li.quantity), _num(li.unit_price),
                              _num(li.tax_rate_percent), _num(li.amount))
            console.print(lines)

    for w in result.warnings:
        console.print(f"[yellow]! {w}[/yellow]")
    if result.excel_path:
        console.print(f"Excel file: [cyan]{result.excel_path}[/cyan]")


def _num(v) -> str:
    return "-" if v is None else f"{v:,.2f}"


def show_mail(mail: IncomingEmail) -> None:
    pdf_names = ", ".join(p.filename for p in mail.pdfs)
    console.print(Panel(
        f"[bold]From:[/bold] {mail.sender_name} <{mail.sender_email}>\n"
        f"[bold]Subject:[/bold] {mail.subject}\n[bold]Date:[/bold] {mail.date}\n"
        f"[bold]PDFs:[/bold] {pdf_names}\n[dim]{mail.body_preview}[/dim]",
        title="Email", expand=False,
    ))


# ---------------------------------------------------------------- core flow
def handle_email(agent: InvoiceAgent, box: Mailbox, mail: IncomingEmail, ask: bool) -> str:
    """Convert one email's PDFs and reply. Returns the final status."""
    show_mail(mail)
    result = agent.convert_email(mail)

    if result.retry_later and not result.items:
        agent.record(mail, result, "error")
        console.print("[yellow]Temporary problem - leaving this email unread to retry later.[/yellow]")
        return "error"

    show_result(result)
    body = agent.reply_body(mail, result) if result.ok else agent.failure_body(mail, result)

    if ask:
        while True:
            choice = Prompt.ask(
                f"Reply to [cyan]{mail.reply_to}[/cyan]? "
                "[bold]s[/bold]end / [bold]p[/bold]review message / add [bold]n[/bold]ote / "
                "s[bold]k[/bold]ip for now / [bold]i[/bold]gnore forever",
                choices=["s", "p", "n", "k", "i"], default="s",
            )
            if choice == "p":
                console.print(Panel(body, title="Reply message"))
            elif choice == "n":
                note = Prompt.ask("Note to add at the top of the reply")
                if note.strip():
                    first, _, rest = body.partition("\n")
                    body = f"{first}\n\n{note.strip()}\n{rest}"
            elif choice == "k":
                console.print("Skipped; it will show up again next time.")
                return "pending"
            elif choice == "i":
                agent.record(mail, result, "skipped", "Ignored by user")
                box.mark_seen(mail.uid)
                return "skipped"
            else:
                break

    try:
        agent.send_reply(mail, result, body)
    except MAIL_ERRORS as e:
        console.print(f"[red]Could not send reply: {e}[/red]")
        agent.record(mail, result, "error", f"send failed: {e}")
        return "error"
    box.mark_seen(mail.uid)
    agent.record(mail, result, "sent")
    return "sent"


def run_once(settings: Settings, ask: bool) -> dict[str, int]:
    agent = InvoiceAgent(settings, log=console.print)
    counts = {"sent": 0, "skipped": 0, "pending": 0, "error": 0}
    with Mailbox(settings) as box:
        with console.status("Checking inbox..."):
            mails = [m for m in box.fetch_invoice_emails() if not agent.store.seen(email_key(m))]
        if not mails:
            console.print("No new invoice emails with PDF attachments.")
            return counts
        console.print(f"[bold]{len(mails)}[/bold] email(s) with PDF invoices found.")

        if ask:
            table = Table()
            for col in ("#", "From", "Subject", "PDFs"):
                table.add_column(col, overflow="fold")
            for i, m in enumerate(mails, 1):
                table.add_row(str(i), m.sender_email, m.subject, str(len(m.pdfs)))
            console.print(table)
            pick = Prompt.ask("Process which? ('all', or numbers like 1,3)", default="all")
            if pick.strip().lower() != "all":
                wanted = {int(x) for x in pick.replace(" ", "").split(",") if x.isdigit()}
                mails = [m for i, m in enumerate(mails, 1) if i in wanted]

        for mail in mails:
            counts[handle_email(agent, box, mail, ask)] += 1
    return counts


def watch(settings: Settings) -> None:
    interval = max(1, settings.poll_interval_minutes)
    mode = "asks before each reply" if settings.require_approval else "replies automatically"
    console.print(Panel(f"Watching {settings.email_address} every {interval} min - {mode}.\nPress Ctrl+C to stop.",
                        title="Auto-pilot", expand=False))
    try:
        while True:
            console.rule(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            try:
                counts = run_once(settings, ask=settings.require_approval)
                console.print(f"Cycle done: {counts}")
            except MAIL_ERRORS as e:
                console.print(f"[red]Mailbox error: {e}. Retrying next cycle.[/red]")
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        console.print("\nStopped watching.")


def convert_local(settings: Settings, paths: list[Path]) -> None:
    agent = InvoiceAgent(settings, log=console.print)
    pdfs = []
    for p in paths:
        if not p.is_file():
            console.print(f"[red]Not found: {p}[/red]")
            continue
        pdfs.append((p.name, p.read_bytes()))
    if not pdfs:
        return
    name = Path(pdfs[0][0]).stem if len(pdfs) == 1 else "invoices"
    show_result(agent.convert_pdfs(pdfs, name))


def show_history(settings: Settings) -> None:
    from .store import Store
    rows = Store().history(30)
    if not rows:
        console.print("Nothing processed yet.")
        return
    table = Table(title="Recent activity")
    for col in ("When", "From", "Subject", "PDFs", "Invoices", "Status", "Excel"):
        table.add_column(col, overflow="fold")
    for when, sender, subject, pdfs, invs, status, _detail, excel in rows:
        colour = {"sent": "green", "error": "red"}.get(status, "yellow")
        table.add_row(when, sender, subject, str(pdfs), str(invs), f"[{colour}]{status}[/{colour}]",
                      Path(excel).name if excel else "")
    console.print(table)


# ---------------------------------------------------------------- setup
def setup_wizard() -> Settings:
    current = Settings.load()
    console.print(Panel("Answers are saved to .env in the project folder (keep it private).", title="Setup"))

    provider = Prompt.ask("Email provider", choices=[*PRESETS, "other"], default="gmail")
    if provider == "gmail":
        console.print("[dim]Gmail needs 2-Step Verification and an App Password "
                      "(myaccount.google.com > Security > App passwords). IMAP must be enabled.[/dim]")
    if provider == "outlook":
        console.print("[dim]Microsoft 365 tenants may block basic-auth IMAP/SMTP; ask IT to allow it "
                      "for this mailbox or use an app password.[/dim]")
    imap_host, imap_port, smtp_host, smtp_port, security = PRESETS.get(
        provider, (current.imap_host, str(current.imap_port), current.smtp_host, str(current.smtp_port),
                   current.smtp_security))
    if provider == "other":
        imap_host = Prompt.ask("IMAP host", default=imap_host)
        imap_port = Prompt.ask("IMAP port", default=imap_port)
        smtp_host = Prompt.ask("SMTP host", default=smtp_host)
        smtp_port = Prompt.ask("SMTP port", default=smtp_port)
        security = Prompt.ask("SMTP security", choices=["ssl", "starttls"], default=security)

    values = {
        "EMAIL_ADDRESS": Prompt.ask("Email address", default=current.email_address or None),
        "EMAIL_PASSWORD": _ask_secret("Email (app) password", current.email_password, strip_spaces=True),
        "IMAP_HOST": imap_host, "IMAP_PORT": imap_port,
        "SMTP_HOST": smtp_host, "SMTP_PORT": smtp_port, "SMTP_SECURITY": security,
        "IMAP_FOLDER": Prompt.ask("Folder to watch", default=current.imap_folder),
        "ANTHROPIC_API_KEY": _ask_secret("Anthropic API key (starts with sk-ant-)", current.anthropic_api_key),
        "ALLOWED_SENDERS": Prompt.ask("Only process mail from (comma-separated senders/domains, blank = all)",
                                      default=",".join(current.allowed_senders)),
        "SUBJECT_KEYWORDS": Prompt.ask("Only subjects containing (comma-separated, blank = any)",
                                       default=",".join(current.subject_keywords)),
        "POLL_INTERVAL_MINUTES": str(IntPrompt.ask("Check inbox every N minutes",
                                                   default=current.poll_interval_minutes)),
        "REQUIRE_APPROVAL": str(Confirm.ask("In auto-pilot, ask me before sending each reply?",
                                            default=current.require_approval)).lower(),
        "CC_ADDRESS": Prompt.ask("CC every reply to (blank = none)", default=current.cc_address),
    }
    write_env({k: v or "" for k, v in values.items()})
    console.print(f"[green]Saved to {ENV_FILE}[/green]")
    return Settings.load()


def _ask_secret(label: str, current: str, strip_spaces: bool = False) -> str:
    """Hidden prompt that keeps the saved value on Enter and rejects non-ASCII typos."""
    hint = " (press Enter to keep the saved one)" if current else ""
    while True:
        value = Prompt.ask(f"{label}{hint}", password=True, default=current or "", show_default=False).strip()
        if strip_spaces:
            value = value.replace(" ", "")  # Gmail shows app passwords as 'abcd efgh ijkl mnop'
        if not value.isascii():
            console.print("[red]That contains a non-English character (check keyboard language / "
                          "copy-paste). Please type it again.[/red]")
            continue
        return value


def test_connections(settings: Settings) -> None:
    checks = [("IMAP", lambda: test_imap(settings)), ("SMTP", lambda: test_smtp(settings)),
              ("Claude", lambda: _test_claude(settings))]
    for name, fn in checks:
        try:
            with console.status(f"Testing {name}..."):
                msg = fn()
            console.print(f"[green]✔ {msg}[/green]")
        except Exception as e:  # show any failure to the user rather than crash the menu
            console.print(f"[red]✘ {name} failed: {e}[/red]")


def _test_claude(settings: Settings) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
    model = client.models.retrieve(settings.claude_model)
    return f"Claude OK - model {model.display_name} available"


def start_tray_detached() -> None:
    """Launch the tray app as its own windowless process."""
    import subprocess
    from .config import ROOT
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if sys.platform == "win32" and pythonw.exists():
        subprocess.Popen([str(pythonw), "main.py", "tray"], cwd=ROOT,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        subprocess.Popen([str(exe), "main.py", "tray"], cwd=ROOT, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    console.print("[green]Invoice Agent is now running in the tray (bottom-right, near the clock; "
                  "check the ^ arrow if you don't see it). You can close this window.[/green]")


# ---------------------------------------------------------------- menu
MENU = """[bold]1[/bold]  Check inbox now (review each invoice before replying)
[bold]2[/bold]  Start auto-pilot (keep watching the inbox)
[bold]3[/bold]  Convert a PDF on this computer to Excel
[bold]4[/bold]  View history
[bold]5[/bold]  Test connections
[bold]6[/bold]  Setup / change settings
[bold]7[/bold]  Run in background (tray icon near the clock) and close this window
[bold]0[/bold]  Exit"""


def interactive() -> None:
    settings = Settings.load()
    console.print(Panel.fit("[bold]Invoice Agent[/bold]\nReads invoice PDFs from your email, converts them "
                            "to Excel and replies to the sender.", border_style="blue"))
    if settings.missing():
        console.print(f"[yellow]Missing settings: {', '.join(settings.missing())}. Let's set things up.[/yellow]")
        settings = setup_wizard()

    while True:
        console.print(Panel(MENU, title="Menu", expand=False))
        choice = Prompt.ask("Choose", choices=["1", "2", "3", "4", "5", "6", "7", "0"], default="1")
        try:
            if choice == "1":
                console.print(f"Done: {run_once(settings, ask=True)}")
            elif choice == "2":
                watch(settings)
            elif choice == "3":
                raw = Prompt.ask("Path to PDF file(s), comma-separated")
                convert_local(settings, [Path(p.strip().strip('"')).expanduser() for p in raw.split(",") if p.strip()])
            elif choice == "4":
                show_history(settings)
            elif choice == "5":
                test_connections(settings)
            elif choice == "6":
                settings = setup_wizard()
            elif choice == "7":
                start_tray_detached()
                return
            else:
                return
        except MAIL_ERRORS as e:
            console.print(f"[red]Mailbox error: {e}[/red]")
        except KeyboardInterrupt:
            console.print("\nCancelled.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Email invoice PDF -> Excel agent")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="Interactive menu (default)")
    sub.add_parser("setup", help="Run the setup wizard")
    sub.add_parser("test", help="Test IMAP, SMTP and Claude connections")
    once = sub.add_parser("once", help="Check the inbox once")
    once.add_argument("--auto", action="store_true", help="Reply without asking")
    sub.add_parser("watch", help="Keep checking the inbox (auto-pilot)")
    conv = sub.add_parser("convert", help="Convert local PDF file(s) to Excel")
    conv.add_argument("pdfs", nargs="+", type=Path)
    sub.add_parser("history", help="Show processed emails")
    sub.add_parser("tray", help="Run in the system tray with an On/Off switch")
    sub.add_parser("install-shortcut", help="Create the desktop icon (Windows)")
    args = parser.parse_args(argv)

    if args.command in (None, "menu"):
        interactive()
        return
    if args.command == "setup":
        setup_wizard()
        return
    if args.command == "tray":
        from .tray import main as tray_main
        tray_main()
        return
    if args.command == "install-shortcut":
        from .tray import create_desktop_shortcut
        console.print(create_desktop_shortcut())
        return

    settings = Settings.load()
    needs_mail = args.command in {"test", "once", "watch"}
    missing = [m for m in settings.missing() if needs_mail or m == "ANTHROPIC_API_KEY"]
    if missing and args.command != "history":
        console.print(f"[red]Missing settings: {', '.join(missing)}. Run: python main.py setup[/red]")
        sys.exit(1)

    if args.command == "test":
        test_connections(settings)
    elif args.command == "once":
        console.print(run_once(settings, ask=not args.auto))
    elif args.command == "watch":
        watch(settings)
    elif args.command == "convert":
        convert_local(settings, args.pdfs)
    elif args.command == "history":
        show_history(settings)
