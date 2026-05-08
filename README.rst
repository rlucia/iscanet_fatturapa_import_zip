==================================
Iscanet - FatturaPA ZIP Import
==================================

Extends the standard Customer Invoices / Vendor Bills **Upload** action of the
Accounting application so that a single ``.zip`` archive containing one or
more Italian electronic-invoice (FatturaPA) XML documents produces *one
``account.move`` per inner XML file*, instead of a single blank invoice with
the ZIP attached as the source document.

Background
==========

Odoo 18 natively supports importing Italian FatturaPA XML files through
``l10n_it_edi``. However, when a user uploads a ZIP archive — which is the
shape in which invoices arrive from the Sistema di Interscambio (SDI) and from
several intermediary platforms — the standard flow only stores the archive as
an attachment on a freshly-created blank invoice. The OCA module
``l10n_it_fatturapa_import_zip`` (Odoo 16) used to address this, but it has
not been ported to Odoo 18 because the underlying OCA FatturaPA stack it
depended on has been superseded by the official ``l10n_it_edi``.

This module fills that gap: it plugs into the Upload entry point provided by
``l10n_it_edi``, transparently expanding ZIP archives so each inner XML is
imported through the standard pipeline.

Features
========

* Upload of ``.zip`` archives containing FatturaPA XML files via the existing
  *Upload* button on Customer Invoices, Vendor Bills, and the journal
  dashboards — no new menu, no new wizard.
* Automatic per-document direction detection: each XML is parsed individually
  and routed to a customer invoice or a vendor bill based on which party
  (CedentePrestatore / CessionarioCommittente) matches the company's VAT or
  Codice Fiscale. A single ZIP may legitimately contain a mix of both.
* Automatic journal re-routing: when the detected direction does not match
  the journal the user clicked Upload from, the move is moved onto a journal
  of the appropriate type within the same company (via Odoo's stored compute
  on ``account.move.journal_id``).
* Support for signed ``.xml.p7m`` envelopes (CAdES) — the existing
  ``l10n_it_edi`` signature-stripping logic is reused.
* Recursive expansion of nested ZIP archives, up to a configurable depth.
* Silent skipping of non-XML/P7M entries (READMEs, ``.DS_Store``, directory
  entries, etc.).
* Per-file error isolation: a malformed XML inside a batch does not abort the
  rest of the import (each entry is parsed inside its own savepoint, as in
  the native flow).
* Defensive limits against zip-bomb-style payloads: caps on entry count
  (1000), total uncompressed size (200 MB), and nesting depth (5).

Requirements
============

* Odoo 18.0
* The ``l10n_it_edi`` module must be installed (it is a hard dependency).

Installation
============

Place this module in your add-ons path and install it as usual::

    odoo-bin -d <database> -i iscanet_fatturapa_import_zip

There is no configuration required: once installed, the existing Upload
button in Accounting → Vendors → Bills (or Customers → Invoices) will
transparently accept ZIP archives.

Usage
=====

1. Open *Accounting → Vendors → Bills* (or *Customers → Invoices*).
2. Click **Upload** and select a ``.zip`` archive.
3. Each FatturaPA XML inside the archive becomes a separate draft invoice
   or bill, ready to be reviewed and posted.

Non-FatturaPA-shaped uploads (a single bare ``.xml`` or ``.xml.p7m``,
PDFs, etc.) are not affected: they continue to use the native Odoo flow.

Compatibility with ``l10n_it_edi_extension`` (OCA)
===================================================

Both this module and the OCA ``l10n_it_edi_extension`` override
``account.journal._create_document_from_attachment`` and chain via
``super()``, so they compose cleanly under MRO when installed together.

Their scope is broader and largely orthogonal to this module:

* additional FatturaPA fields on the export side (``RiferimentoAmministrazione``,
  ``Art73``, ``StabileOrganizzazione``, ``AltriDatiGestionali``, ``IndirizzoResa``\ …);
* per-line import enrichment (``ScontoMaggiorazione``, ``CodiceArticolo``,
  ``DatiRiepilogo`` / ``Natura`` / ``EsigibilitaIVA``\ …);
* multi-body XML splitting (one ``.xml`` containing several
  ``<FatturaElettronicaBody>`` blocks);
* a separate menu-driven import wizard with deduplication.

This module focuses narrowly on extending the native **Upload** button to
accept ZIP archives. The two together cover the full matrix of
"FatturaPA delivered as something other than one bare XML."

Note that ``l10n_it_edi_extension`` is licensed AGPL-3 (this module is
Apache-2.0) and ships with heavier dependencies (``codicefiscale``,
``openupgradelib``, ``partner_firstname``\ …) and DB-mutating init hooks;
evaluate accordingly before installing.

Limitations
===========

* The cap of 200 MB total uncompressed payload and 1000 entries per archive
  is hard-coded; if you legitimately need to import larger batches, edit the
  ``MAX_ZIP_*`` constants in ``models/account_journal.py``.
* Files that are neither valid FatturaPA XML nor ``.p7m`` are skipped; if
  you also need to ingest other EDI dialects from the same archive, that
  would require additional decoders (out of scope for this module).

Credits
=======

* **Author**: Rocco Lucia, Iscanet Internet Services S.r.l.
* **License**: Apache 2.0 (see ``LICENSE`` at the repository root)
* Inspired by the Odoo 16 OCA module ``l10n_it_fatturapa_import_zip``
  (TAKOBI / OCA), re-implemented from scratch on top of Odoo 18's native
  ``l10n_it_edi`` import pipeline.
