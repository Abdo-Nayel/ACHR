"""The general ledger's public interface.

Every module outside ``apps.accounting`` that needs the ledger imports from
*here* — ``from apps.accounting.services import post_entry, JournalEntryDraft`` —
never from the private modules (`posting`, `sequences`, `fx`) underneath. That
keeps the ledger's internals free to move (the posting engine is being
decomposed) without a ripple through the nine modules that post into it, and it
makes the seam greppable: one import path is the whole contract.

Exposed:

* the posting choke point — ``post_entry`` (the only sanctioned ledger write),
  plus ``void_entry`` / ``reverse_entry`` for corrections and
  ``assert_ledger_balanced`` for the nightly integrity check;
* the inert draft types callers build — ``JournalEntryDraft`` / ``LineDraft``
  (they have no ``save()``; the only way to make a draft real is ``post_entry``);
* the domain exceptions those raise — ``UnbalancedEntry`` / ``PeriodClosed`` /
  ``DuplicatePosting``;
* gapless document numbering — ``allocate_document_number``.
"""

from __future__ import annotations

from apps.accounting.services.posting import (
    DuplicatePosting,
    JournalEntryDraft,
    LineDraft,
    PeriodClosed,
    UnbalancedEntry,
    assert_ledger_balanced,
    post_entry,
    reverse_entry,
    void_entry,
)
from apps.accounting.services.sequences import allocate_document_number

__all__ = [
    "JournalEntryDraft",
    "LineDraft",
    "UnbalancedEntry",
    "PeriodClosed",
    "DuplicatePosting",
    "post_entry",
    "void_entry",
    "reverse_entry",
    "assert_ledger_balanced",
    "allocate_document_number",
]
