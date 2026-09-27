"""Structured invoice schema that Claude fills in from each PDF."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    line_no: Optional[int] = Field(description="Serial / line number as printed, if any")
    item_code: Optional[str] = Field(description="SKU, part number or item code")
    description: str = Field(description="Item or service description")
    hsn_sac: Optional[str] = Field(description="HSN or SAC code (Indian GST invoices)")
    batch_no: Optional[str] = Field(description="Batch / lot number (pharma & medical supplies)")
    expiry_date: Optional[str] = Field(description="Expiry date as YYYY-MM-DD or as printed")
    quantity: Optional[float]
    unit: Optional[str] = Field(description="Unit of measure, e.g. Nos, Box, Kg, Hrs")
    unit_price: Optional[float] = Field(description="Rate per unit before tax")
    discount: Optional[float] = Field(description="Discount amount on this line")
    tax_rate_percent: Optional[float] = Field(description="Total tax rate for the line, e.g. 18 for 18%")
    tax_amount: Optional[float]
    amount: Optional[float] = Field(description="Line total exactly as printed on the invoice")


class TaxLine(BaseModel):
    name: str = Field(description="Tax name, e.g. CGST, SGST, IGST, VAT, Cess")
    rate_percent: Optional[float]
    taxable_amount: Optional[float]
    amount: Optional[float]


class Invoice(BaseModel):
    document_type: str = Field(description="invoice, tax invoice, credit note, proforma, receipt, etc.")
    invoice_number: Optional[str]
    invoice_date: Optional[str] = Field(description="YYYY-MM-DD when determinable")
    due_date: Optional[str] = Field(description="YYYY-MM-DD when determinable")
    po_number: Optional[str] = Field(description="Purchase order number")
    vendor_name: Optional[str] = Field(description="Seller / supplier who issued the invoice")
    vendor_address: Optional[str]
    vendor_tax_id: Optional[str] = Field(description="Seller GSTIN / VAT / tax ID")
    vendor_pan: Optional[str]
    bill_to_name: Optional[str] = Field(description="Buyer / customer name")
    bill_to_address: Optional[str]
    bill_to_tax_id: Optional[str] = Field(description="Buyer GSTIN / VAT / tax ID")
    place_of_supply: Optional[str]
    currency: Optional[str] = Field(description="ISO code such as INR or USD")
    subtotal: Optional[float] = Field(description="Total before tax")
    discount_total: Optional[float]
    tax_total: Optional[float]
    shipping: Optional[float] = Field(description="Freight / shipping / other charges")
    round_off: Optional[float]
    total_amount: Optional[float] = Field(description="Grand total payable")
    amount_paid: Optional[float]
    balance_due: Optional[float]
    payment_terms: Optional[str]
    bank_details: Optional[str] = Field(description="Bank name, account number, IFSC if printed")
    notes: Optional[str]
    line_items: list[LineItem]
    taxes: list[TaxLine]


class ExtractionResult(BaseModel):
    invoices: list[Invoice] = Field(description="One entry per distinct invoice found in the PDF")
    warnings: list[str] = Field(
        description="Anything unreadable, ambiguous, or totals that do not reconcile"
    )
