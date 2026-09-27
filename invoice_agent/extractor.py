"""Read an invoice PDF with Claude and return structured data."""

from __future__ import annotations

import base64

import anthropic

from .models import ExtractionResult

MAX_PDF_BYTES = 30 * 1024 * 1024  # API request limit is 32 MB including base64 overhead

SYSTEM_PROMPT = """You extract data from supplier invoices into a structured record that will be \
turned into an Excel sheet for accounts payable.

- Copy values exactly as printed. Do not invent numbers; use null when a field is absent or unreadable.
- Amounts are plain numbers without currency symbols or thousands separators (1,23,456.50 -> 123456.5).
- Dates as YYYY-MM-DD when the date is unambiguous; otherwise keep the printed text.
- Capture every line item row, including those that continue across pages. Skip subtotal/total rows.
- Put each tax component (CGST, SGST, IGST, VAT, cess...) in `taxes`.
- If the PDF holds several separate invoices, return one entry per invoice.
- If the PDF is not an invoice-like document, return an empty `invoices` list and explain in `warnings`.
- Add a warning when line items do not add up to the subtotal/total, or when text is illegible."""


class ExtractionError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        # True for temporary problems (network, rate limit, bad key): try again later
        # instead of telling the sender their PDF is unreadable.
        self.retryable = retryable


class InvoiceExtractor:
    def __init__(self, api_key: str | None = None, model: str = "claude-opus-5"):
        # With no key the SDK falls back to ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
        self.client = anthropic.Anthropic(api_key=api_key or None, max_retries=4)
        self.model = model

    def extract(self, pdf_bytes: bytes, filename: str = "invoice.pdf") -> ExtractionResult:
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise ExtractionError(f"{filename} is larger than 30 MB and cannot be processed.")

        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=32000,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                # On a safety-classifier decline the API retries on a fallback model automatically.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
                                },
                                "title": filename,
                            },
                            {"type": "text", "text": f"Extract all invoice data from '{filename}'."},
                        ],
                    }
                ],
                output_format=ExtractionResult,
            )
        except anthropic.BadRequestError as e:
            # Typically: encrypted/password-protected or corrupt PDF, or too many pages.
            raise ExtractionError(f"Claude could not read {filename}: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise ExtractionError("Anthropic API key is missing or invalid.", retryable=True) from e
        except anthropic.RateLimitError as e:
            raise ExtractionError("Anthropic rate limit hit; will retry on the next run.", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ExtractionError(f"Anthropic API error {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ExtractionError(f"Could not reach the Anthropic API: {e}", retryable=True) from e

        if response.stop_reason == "refusal":
            raise ExtractionError(f"Claude declined to process {filename}.")
        if response.stop_reason == "max_tokens":
            raise ExtractionError(f"{filename} is too long to extract in one pass.")
        if response.parsed_output is None:
            raise ExtractionError(f"Claude returned no structured data for {filename}.")
        return response.parsed_output
