# Invoice Agent: email PDF → Excel → reply

An agent that watches your mailbox for emails with **invoice PDFs**, uses **Claude** to read each
invoice (including scanned ones), builds an **Excel workbook**, and **replies to the sender** in the
same email thread with the Excel file attached.

```
 Inbox (IMAP) ──► PDF attachments ──► Claude reads invoice ──► Excel (.xlsx) ──► Reply (SMTP) to sender
                                            │
                                   structured fields, line items,
                                   GST/VAT taxes, totals check
```

## What you get in the Excel file

| Sheet | Contents |
|---|---|
| **Summary** | One row per invoice: invoice no, dates, PO, vendor, GSTIN/PAN, bill-to, subtotal, tax, total, balance, payment terms, bank details, a **Check** column that shows whether the line items add up to the total, plus a totals row |
| **Line Items** | Every line from every invoice: item code, description, HSN/SAC, batch, expiry, qty, unit, rate, discount, tax %, tax amount, amount |
| **Taxes** | CGST / SGST / IGST / VAT / cess breakdown per invoice |
| **Extraction Notes** | Anything Claude found unreadable or inconsistent (only added when needed) |

If one email has several PDFs, or one PDF holds several invoices, they all go into one workbook.

## Quick start on a Windows PC

1. Install **Python** from <https://www.python.org/downloads/>. On the first installer screen, tick
   **"Add python.exe to PATH"**.
2. Download this project (GitHub → **Code** → **Download ZIP**) and unzip it, e.g. to `C:\InvoiceAgent`.
3. Double-click **`start.bat`**. The first run installs everything, then the setup wizard asks for
   your email, app password and Anthropic API key.
4. Use the menu. Excel files are saved in the `output` folder.

On macOS/Linux run `./start.sh` instead.

## Everyday use: the tray icon (no black window)

After the first `start.bat` run there is an **Invoice Agent** icon on your desktop. Double-click it
and a small icon appears near the clock (look under the **^** arrow). The agent then runs in the
background:

| Icon colour | Meaning |
|---|---|
| Green | ON: checking the inbox and replying automatically |
| Grey | OFF (paused) |
| Yellow | Checking right now |
| Red | Problem; hover to see it, or right-click → View log |

**Left-click** the icon to turn it ON/OFF. **Right-click** for the menu: *Check inbox now*,
*Open Excel folder*, *Review inbox manually*, *History*, *View log*, *Settings*,
**Start with Windows** (tick it once and it starts on every boot), and *Quit*. The ON/OFF choice
is remembered across restarts.

In tray mode replies are sent automatically. To approve each reply, use *Review inbox manually*
instead of turning the agent ON.

## Setup (manual)

1. **Python 3.10+**, then install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. **Anthropic API key**: create one at <https://console.anthropic.com>.
3. **Mailbox access**:
   - **Gmail**: turn on 2-Step Verification, then create an **App Password**
     (Google Account → Security → App passwords). Use it in place of your normal password.
   - **Outlook / Microsoft 365**: IMAP/SMTP basic auth has to be allowed for the mailbox (ask IT),
     or use an app password.
   - **Zoho / Yahoo / others**: pick the preset or enter the IMAP/SMTP hosts.
4. Run the setup wizard. It saves everything to `.env`:
   ```bash
   python main.py setup
   ```
   Or copy `.env.example` to `.env` and fill it in by hand.
5. Check that everything connects:
   ```bash
   python main.py test
   ```

## Using it

### Interactive menu (recommended to start)

```bash
python main.py
```

```
1  Check inbox now (review each invoice before replying)
2  Start auto-pilot (keep watching the inbox)
3  Convert a PDF on this computer to Excel
4  View history
5  Test connections
6  Setup / change settings
0  Exit
```

With **Check inbox now**, you see the list of invoice emails and pick which ones to process. For
each email the agent shows the extracted invoices and line items, then asks:

- **s**: send the reply with the Excel attached
- **p**: preview the reply message
- **n**: add a personal note to the reply
- **k**: skip for now (it shows up again next time)
- **i**: ignore it permanently

### Commands

```bash
python main.py once            # check inbox once, asking before each reply
python main.py once --auto     # check inbox once, reply automatically
python main.py watch           # auto-pilot: check every POLL_INTERVAL_MINUTES
python main.py convert a.pdf b.pdf   # convert local files only, no email
python main.py history         # what was processed and when
```

To keep auto-pilot running on a server, run `python main.py watch` under `systemd`, `pm2`, `nohup`,
or Windows Task Scheduler. You can also run `python main.py once --auto` from cron every few minutes.

## Settings (`.env`)

| Key | Meaning |
|---|---|
| `ALLOWED_SENDERS` | Comma-separated senders or domains (`acme.in,billing@xyz.com`). Only these get processed. Blank means everyone. |
| `SUBJECT_KEYWORDS` | Only process subjects containing one of these words (`invoice,bill`). Blank means any subject. |
| `ONLY_UNREAD` / `SEARCH_SINCE_DAYS` | Which mails to look at. |
| `REQUIRE_APPROVAL` | In auto-pilot, ask before each reply (`true`) or send automatically (`false`). |
| `CC_ADDRESS` | Also send every reply to this address, e.g. your accounts team. |
| `CLAUDE_MODEL` | Defaults to `claude-opus-5`. |

## How it behaves

- **Never processes the same email twice.** Processed Message-IDs are kept in `data/state.db`, and
  the email is marked as read after the reply goes out.
- **Replies in the same thread**, using `In-Reply-To`/`References` and `Re:` on the subject. It
  honours the sender's `Reply-To` header.
- **Never replies to itself**, so mail sent from your own address is ignored and cannot loop.
- **Temporary failures** (network, rate limit, invalid API key) leave the email unread so it is
  retried on the next run. The sender is not told anything in that case.
- **Unreadable PDFs** (password-protected or corrupt) get a polite reply asking the sender to
  resend.
- **Totals check**: if the line items don't add up to the subtotal or total, the Check cell turns
  orange and the reply mentions it.
- Excel files are also kept locally in `output/`.

## Project layout

```
main.py                     entry point
invoice_agent/
  cli.py                    interactive menu, setup wizard, watch loop, commands
  agent.py                  pipeline: PDFs → Claude → Excel → reply
  extractor.py              Claude PDF reading with structured output
  models.py                 invoice schema (fields, line items, taxes)
  excel_writer.py           formatted workbook + totals reconciliation
  mailer.py                 IMAP fetch/parse, filters, SMTP reply
  store.py                  SQLite history
  config.py                 .env settings
tests/test_pipeline.py      offline tests (pytest)
```

Run the tests with `pip install pytest && python -m pytest -q`.

## Privacy note

Invoice PDFs are sent to the Anthropic API to be read. `.env` holds your passwords, so it is in
`.gitignore`. Never commit it.
