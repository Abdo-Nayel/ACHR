"""Invoice attachments: the first upload path in the product.

Nothing here had ever accepted a file. ``ExpenseReceipt`` models an object-key
on the assumption that something else writes the bytes, and nothing ever did —
so every check below is new ground rather than a regression, and the ones that
matter most are the refusals.

The two failures worth designing against:

* **An attachment that executes.** A ``.svg`` or ``.html`` served from the
  application's own origin runs script against every user of the tenant. That
  is stored XSS with an upload button on the front of it, and the
  browser-declared content type cannot be trusted to prevent it.
* **A path built from a user-supplied name.** ``../../etc/passwd`` as a
  filename is not hypothetical. The stored key is a UUID; the original name is
  kept as *data*, never as a path component.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.sales.models import Customer, Invoice, InvoiceAttachment, InvoiceLine
from apps.sales.models import invoice_attachment_path
from apps.sales.serializers import InvoiceAttachmentUploadSerializer
from tests.conftest import TEST_CURRENCY

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer(tenant, chart_of_accounts) -> Customer:
    return Customer.objects.create(
        tenant=tenant, code="C-4001", name="Nile Retail",
        currency=TEST_CURRENCY,
        receivable_account=chart_of_accounts["ar_control"],
    )


@pytest.fixture
def invoice(tenant, customer, chart_of_accounts) -> Invoice:
    today = date.today()
    inv = Invoice.objects.create(
        tenant=tenant, customer=customer, issue_date=today,
        due_date=today + timedelta(days=30), currency=TEST_CURRENCY,
        exchange_rate=Decimal("1"), subtotal_amount=Decimal("100.00"),
        discount_amount=Decimal("0"), tax_amount=Decimal("0"),
        total_amount=Decimal("100.00"), amount_paid=Decimal("0"),
        amount_due=Decimal("100.00"), status=Invoice.Status.DRAFT,
    )
    InvoiceLine.objects.create(
        tenant=tenant, invoice=inv, line_number=1, description="Work",
        quantity=Decimal("1"), unit_price=Decimal("100.00"),
        discount_rate=Decimal("0"), line_subtotal=Decimal("100.00"),
        line_tax=Decimal("0"), line_total=Decimal("100.00"),
        income_account=chart_of_accounts["service_revenue"],
    )
    return inv


def _upload(name, content=b"%PDF-1.4 hello", content_type="application/pdf"):
    return SimpleUploadedFile(name, content, content_type=content_type)


def _validate(upload):
    s = InvoiceAttachmentUploadSerializer(data={"file": upload})
    return s.is_valid(), s.errors


# ---------------------------------------------------------------------------
# Validation — the refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["logo.svg", "page.html", "x.htm", "a.js"])
def test_types_that_execute_from_our_origin_are_refused(name):
    """Stored XSS with an upload button on the front of it.

    Refused on the *extension*, whatever content type the browser declares,
    because the declared type is trivially forged and the extension is what a
    static file server will honour when the file is opened later.
    """
    ok, errors = _validate(_upload(name, b"<svg/>", "image/png"))
    assert not ok


def test_executables_are_refused():
    ok, _ = _validate(_upload("setup.exe", b"MZ", "application/octet-stream"))
    assert not ok


def test_an_unlisted_content_type_is_refused():
    """An allowlist, not a blocklist: the dangerous set is open-ended and
    grows, the useful set is short and stable."""
    ok, _ = _validate(_upload("clip.mp4", b"\x00\x00", "video/mp4"))
    assert not ok


def test_an_empty_file_is_refused():
    """Almost always a drag-and-drop that did not complete. Stored, it becomes
    an attachment that looks present and opens to nothing."""
    ok, _ = _validate(_upload("empty.pdf", b""))
    assert not ok


def test_a_file_over_the_limit_is_refused():
    ok, errors = _validate(
        _upload("huge.pdf", b"x" * (InvoiceAttachment.MAX_BYTES + 1))
    )
    assert not ok
    assert "10 MB" in str(errors)


@pytest.mark.parametrize("name,ctype", [
    ("po.pdf", "application/pdf"),
    ("scan.png", "image/png"),
    ("note.jpg", "image/jpeg"),
    ("data.csv", "text/csv"),
    ("terms.docx",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
])
def test_the_types_a_finance_team_actually_attaches_are_accepted(name, ctype):
    ok, errors = _validate(_upload(name, b"payload", ctype))
    assert ok, errors


# ---------------------------------------------------------------------------
# Storage key
# ---------------------------------------------------------------------------

def test_the_stored_key_is_a_uuid_not_the_uploaded_name(tenant, invoice):
    """The original name is data, never a path component."""
    attachment = InvoiceAttachment(tenant_id=tenant.id, invoice=invoice)

    key = invoice_attachment_path(attachment, "quarterly report.pdf")

    assert "quarterly" not in key
    assert key.endswith(".pdf")
    assert key.startswith(f"invoices/{tenant.id}/{invoice.id}/")


@pytest.mark.parametrize("hostile", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32\\config",
    "file\n.pdf",
    "con.pdf",
    "x" * 400 + ".pdf",
])
def test_a_hostile_filename_cannot_escape_the_key(tenant, invoice, hostile):
    attachment = InvoiceAttachment(tenant_id=tenant.id, invoice=invoice)

    key = invoice_attachment_path(attachment, hostile)

    prefix = f"invoices/{tenant.id}/{invoice.id}/"
    assert key.startswith(prefix)
    # Exactly one path segment after the prefix, and nothing that walks up.
    assert "/" not in key[len(prefix):]
    assert ".." not in key
    assert "\n" not in key and "\\" not in key


def test_the_key_is_tenant_first(tenant, invoice):
    """So a customer's whole file set can be exported or erased with one
    prefix operation — which is what a deletion request actually looks like."""
    attachment = InvoiceAttachment(tenant_id=tenant.id, invoice=invoice)

    assert invoice_attachment_path(attachment, "a.pdf").split("/")[1] == str(tenant.id)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _attach(tenant, invoice, name="po.pdf", content=b"%PDF-1.4 hello"):
    return InvoiceAttachment.objects.create(
        tenant_id=tenant.id, invoice=invoice,
        file=SimpleUploadedFile(name, content, content_type="application/pdf"),
        original_filename=name, content_type="application/pdf",
        size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
    )


def test_the_same_file_cannot_be_attached_twice_to_one_invoice(tenant, invoice):
    """Almost always a repeated click rather than a second document."""
    from django.db.utils import IntegrityError  # noqa: PLC0415

    _attach(tenant, invoice)

    with pytest.raises(IntegrityError):
        _attach(tenant, invoice, name="same-again.pdf")


def test_the_same_file_may_be_attached_to_two_different_invoices(
    tenant, customer, invoice, chart_of_accounts
):
    """Unlike ``ExpenseReceipt``, where a repeat is fraud. One signed framework
    agreement legitimately belongs on every invoice raised under it."""
    other = Invoice.objects.create(
        tenant=tenant, customer=customer, issue_date=date.today(),
        due_date=date.today() + timedelta(days=30), currency=TEST_CURRENCY,
        exchange_rate=Decimal("1"), subtotal_amount=Decimal("50.00"),
        discount_amount=Decimal("0"), tax_amount=Decimal("0"),
        total_amount=Decimal("50.00"), amount_paid=Decimal("0"),
        amount_due=Decimal("50.00"), status=Invoice.Status.DRAFT,
    )
    _attach(tenant, invoice)
    _attach(tenant, other)

    assert InvoiceAttachment.objects.count() == 2


def test_a_zero_byte_row_is_refused_by_the_database(tenant, invoice):
    """The serializer refuses it too; this is the guard a future importer that
    skips the serializer still meets."""
    from django.db.utils import IntegrityError  # noqa: PLC0415

    with pytest.raises(IntegrityError):
        InvoiceAttachment.objects.create(
            tenant_id=tenant.id, invoice=invoice,
            file=SimpleUploadedFile("e.pdf", b"x", content_type="application/pdf"),
            original_filename="e.pdf", content_type="application/pdf",
            size_bytes=0, sha256="0" * 64,
        )


def test_attachments_do_not_leak_between_tenants(tenant, other_tenant, invoice):
    _attach(tenant, invoice)

    from apps.core.tenancy_context import tenant_context  # noqa: PLC0415

    with tenant_context(other_tenant.id):
        assert InvoiceAttachment.objects.count() == 0


def test_deleting_an_invoice_takes_its_attachments(tenant, invoice):
    """CASCADE, not PROTECT: an attachment has no meaning without its invoice,
    and a row pointing at a document that is gone is unusable."""
    _attach(tenant, invoice)
    assert InvoiceAttachment.objects.filter(invoice=invoice).count() == 1

    from django.db.models.query import QuerySet  # noqa: PLC0415

    QuerySet(model=Invoice).filter(pk=invoice.pk).delete()

    assert InvoiceAttachment.objects.count() == 0
