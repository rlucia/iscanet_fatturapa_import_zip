import zipfile
from io import BytesIO
from unittest.mock import patch

from odoo import tools
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.iscanet_fatturapa_import_zip.models import account_journal
from odoo.addons.l10n_it_edi.tests.common import TestItEdi


def _read_l10n_it_fixture(filename):
    """Read a real FatturaPA fixture XML/p7m shipped with l10n_it_edi."""
    path = f"l10n_it_edi/tests/import_xmls/{filename}"
    with tools.file_open(path, mode="rb") as fd:
        return fd.read()


def _build_zip(entries):
    """Build a ZIP archive in memory.

    `entries` is an iterable of (arcname, content_bytes) tuples. Returns the
    raw ZIP bytes ready to be stored in an ir.attachment.
    """
    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in entries:
            zf.writestr(arcname, content)
    return buf.getvalue()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestFatturaPAZipImport(TestItEdi):
    """Tests for the ZIP-aware extension of the invoice/bill Upload action."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.purchase_journal = cls.company_data_2["default_journal_purchase"]
        cls.sale_journal = cls.company_data_2["default_journal_sale"]
        # Pre-load a couple of vendor-bill fixtures (our company is the buyer
        # in both, so they should land as in_invoices).
        cls.bill_xml_a = _read_l10n_it_fixture("IT01234567890_FPR01.xml")
        cls.bill_xml_b = _read_l10n_it_fixture("IT01234567891_FPR01.xml")
        cls.bill_p7m = _read_l10n_it_fixture("IT01234567890_FPR01.xml.p7m")

    def _upload(self, journal, raw_bytes, filename):
        """Create an ir.attachment with the given content and run it through
        the Upload entry point on the given journal. Returns the recordset of
        invoices that came out (matching the production frontend flow)."""
        attachment = self.env["ir.attachment"].create(
            {
                "name": filename,
                "raw": raw_bytes,
            }
        )
        # Match the context the AccountFileUploader frontend sets when the
        # user clicks Upload on a journal: it pins default_move_type to the
        # journal's "obvious" direction.
        if journal.type == "purchase":
            move_type = "in_invoice"
        elif journal.type == "sale":
            move_type = "out_invoice"
        else:
            move_type = "entry"
        return journal.with_context(
            default_move_type=move_type,
            default_journal_id=journal.id,
        )._create_document_from_attachment(attachment.ids)

    # ------------------------------------------------------------------
    # Golden paths
    # ------------------------------------------------------------------

    def test_single_xml_in_zip_creates_one_bill(self):
        zip_bytes = _build_zip([("IT01234567890_FPR01.xml", self.bill_xml_a)])
        invoices = self._upload(self.purchase_journal, zip_bytes, "batch.zip")
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices.move_type, "in_invoice")
        self.assertEqual(invoices.journal_id, self.purchase_journal)
        # Sanity-check the XML actually parsed (vs. a blank fallback move):
        self.assertTrue(
            invoices.invoice_line_ids, "invoice lines should be populated from the XML"
        )

    def test_multiple_xmls_in_zip_create_one_bill_each(self):
        zip_bytes = _build_zip(
            [
                ("IT01234567890_FPR01.xml", self.bill_xml_a),
                ("IT01234567891_FPR01.xml", self.bill_xml_b),
            ]
        )
        invoices = self._upload(self.purchase_journal, zip_bytes, "batch.zip")
        self.assertEqual(len(invoices), 2)
        self.assertEqual(set(invoices.mapped("move_type")), {"in_invoice"})
        for inv in invoices:
            self.assertTrue(
                inv.invoice_line_ids, "each invoice should have lines from its XML"
            )

    def test_nested_zip_is_recursively_exploded(self):
        inner_zip = _build_zip([("IT01234567890_FPR01.xml", self.bill_xml_a)])
        outer_zip = _build_zip([("inner.zip", inner_zip)])
        invoices = self._upload(self.purchase_journal, outer_zip, "outer.zip")
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices.move_type, "in_invoice")
        self.assertTrue(invoices.invoice_line_ids)

    def test_p7m_signed_xml_in_zip_is_decoded(self):
        zip_bytes = _build_zip([("IT01234567890_FPR01.xml.p7m", self.bill_p7m)])
        invoices = self._upload(self.purchase_journal, zip_bytes, "signed.zip")
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices.move_type, "in_invoice")
        self.assertTrue(
            invoices.invoice_line_ids,
            "p7m signature should have been stripped and the inner XML imported",
        )

    # ------------------------------------------------------------------
    # Filtering & resilience
    # ------------------------------------------------------------------

    def test_non_xml_entries_are_skipped_silently(self):
        zip_bytes = _build_zip(
            [
                ("IT01234567890_FPR01.xml", self.bill_xml_a),
                ("README.txt", b"This is not a fattura."),
                (".DS_Store", b"\x00\x01\x02"),
            ]
        )
        invoices = self._upload(self.purchase_journal, zip_bytes, "mixed.zip")
        self.assertEqual(
            len(invoices), 1, "only the XML entry should produce an invoice"
        )
        self.assertTrue(invoices.invoice_line_ids)

    def test_one_bad_xml_does_not_abort_the_batch(self):
        zip_bytes = _build_zip(
            [
                ("IT01234567890_FPR01.xml", self.bill_xml_a),
                ("broken.xml", b"<not-fatturapa/>"),
            ]
        )
        with tools.mute_logger(
            "odoo.addons.l10n_it_edi.models.ir_attachment",
            "odoo.addons.l10n_it_edi.models.account_move",
        ):
            invoices = self._upload(self.purchase_journal, zip_bytes, "mixed.zip")
        # Both attachments produce a move (each gets its own savepoint in
        # _extend_with_attachments), but only the valid one has invoice lines.
        self.assertEqual(len(invoices), 2)
        valid = invoices.filtered(lambda m: m.invoice_line_ids)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid.move_type, "in_invoice")

    # ------------------------------------------------------------------
    # Direction auto-routing
    # ------------------------------------------------------------------

    def test_vendor_bill_uploaded_via_sale_journal_is_routed_to_purchase(self):
        """The XML in the zip is a vendor bill (our company is the buyer),
        but the user clicks Upload from the customer-invoices journal.
        Without this module, l10n_it_edi would refuse because
        default_move_type='out_invoice' locks it to the seller path. With the
        module, default_move_type is dropped and the move is auto-routed to
        the purchase journal via the stored compute on journal_id."""
        zip_bytes = _build_zip([("IT01234567890_FPR01.xml", self.bill_xml_a)])
        invoices = self._upload(self.sale_journal, zip_bytes, "batch.zip")
        self.assertEqual(len(invoices), 1)
        self.assertEqual(
            invoices.move_type,
            "in_invoice",
            "direction should be detected from the XML, not from the chosen journal",
        )
        self.assertEqual(
            invoices.journal_id.type,
            "purchase",
            "the move should have been re-routed to a purchase journal",
        )

    # ------------------------------------------------------------------
    # Pass-through (regression guard)
    # ------------------------------------------------------------------

    def test_non_zip_attachment_uses_native_flow(self):
        """A bare XML upload should behave exactly as on stock Odoo 18 — same
        single invoice produced via the native code path, no extra
        attachments created."""
        attachment_count_before = self.env["ir.attachment"].search_count([])
        invoices = self._upload(
            self.purchase_journal, self.bill_xml_a, "IT01234567890_FPR01.xml"
        )
        attachment_count_after = self.env["ir.attachment"].search_count([])
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices.move_type, "in_invoice")
        # Native flow: only the one attachment we created, nothing exploded.
        self.assertEqual(attachment_count_after, attachment_count_before + 1)

    # ------------------------------------------------------------------
    # Error paths / safety guards
    # ------------------------------------------------------------------

    def test_empty_zip_raises(self):
        zip_bytes = _build_zip([])
        with self.assertRaisesRegex(UserError, "FatturaPA"):
            self._upload(self.purchase_journal, zip_bytes, "empty.zip")

    def test_zip_with_only_non_xml_entries_raises(self):
        zip_bytes = _build_zip([("README.txt", b"hi")])
        with self.assertRaisesRegex(UserError, "FatturaPA"):
            self._upload(self.purchase_journal, zip_bytes, "junk.zip")

    def test_too_many_entries_is_refused(self):
        # 3 entries, but the cap is set to 2 via a patch.
        zip_bytes = _build_zip(
            [
                ("a.xml", self.bill_xml_a),
                ("b.xml", self.bill_xml_b),
                ("c.xml", self.bill_xml_a),
            ]
        )
        with patch.object(account_journal, "MAX_ZIP_ENTRIES", 2):
            with self.assertRaisesRegex(UserError, "more than"):
                self._upload(self.purchase_journal, zip_bytes, "many.zip")

    def test_total_size_cap_is_refused(self):
        zip_bytes = _build_zip(
            [
                ("a.xml", self.bill_xml_a),
                ("b.xml", self.bill_xml_b),
            ]
        )
        with patch.object(account_journal, "MAX_ZIP_TOTAL_UNCOMPRESSED", 100):
            with self.assertRaisesRegex(UserError, "uncompressed"):
                self._upload(self.purchase_journal, zip_bytes, "big.zip")

    def test_nesting_depth_cap_is_refused(self):
        # Build a chain deeper than MAX_ZIP_DEPTH (5) → 6 nested zips.
        innermost = _build_zip([("IT01234567890_FPR01.xml", self.bill_xml_a)])
        nested = innermost
        for i in range(6):
            nested = _build_zip([(f"level_{i}.zip", nested)])
        with self.assertRaisesRegex(UserError, "depth"):
            self._upload(self.purchase_journal, nested, "deep.zip")

    # ------------------------------------------------------------------
    # User feedback: chatter provenance + end-of-import toasts
    # ------------------------------------------------------------------

    def test_each_imported_move_logs_archive_provenance_to_chatter(self):
        """Every move created from a ZIP entry must carry a chatter line that
        names the source archive, so the connection between the original
        upload and the resulting drafts is preserved on each invoice."""
        archive_name = "vendor_batch_2026.zip"
        zip_bytes = _build_zip(
            [
                ("IT01234567890_FPR01.xml", self.bill_xml_a),
                ("IT01234567891_FPR01.xml", self.bill_xml_b),
            ]
        )
        invoices = self._upload(self.purchase_journal, zip_bytes, archive_name)
        self.assertEqual(len(invoices), 2)
        for invoice in invoices:
            chatter_bodies = invoice.message_ids.mapped("body")
            self.assertTrue(
                any(archive_name in (body or "") for body in chatter_bodies),
                f"expected '{archive_name}' to appear in {invoice.display_name}'s "
                f"chatter, got bodies: {chatter_bodies}",
            )

    def test_public_action_carries_per_archive_notifications(self):
        """The frontend renders ``action.context.notifications`` as sticky
        info toasts. Confirm the public override populates that dict with one
        entry per uploaded ZIP, keyed by archive name and stating the count
        of imported documents."""
        archive_name = "feedback_loop.zip"
        zip_bytes = _build_zip(
            [
                ("IT01234567890_FPR01.xml", self.bill_xml_a),
                ("IT01234567891_FPR01.xml", self.bill_xml_b),
            ]
        )
        attachment = self.env["ir.attachment"].create(
            {"name": archive_name, "raw": zip_bytes}
        )
        # Hit the *public* method (the one the frontend calls), not the
        # underscore one we use elsewhere — this is what builds the action.
        action = self.purchase_journal.with_context(
            default_move_type="in_invoice",
            default_journal_id=self.purchase_journal.id,
        ).create_document_from_attachment(attachment.ids)

        self.assertIsInstance(action, dict)
        notifications = (action.get("context") or {}).get("notifications") or {}
        self.assertIn(archive_name, notifications)
        msg = notifications[archive_name]
        self.assertIn("2", msg, f"count should appear in the toast body: {msg!r}")

    def test_non_zip_upload_does_not_emit_zip_notifications(self):
        """A bare XML upload (regression guard for B): the action should NOT
        contain the iscanet zip-notifications key, since we only kick in for
        actual ZIP uploads."""
        attachment = self.env["ir.attachment"].create(
            {"name": "IT01234567890_FPR01.xml", "raw": self.bill_xml_a}
        )
        action = self.purchase_journal.with_context(
            default_move_type="in_invoice",
            default_journal_id=self.purchase_journal.id,
        ).create_document_from_attachment(attachment.ids)
        notifications = (action.get("context") or {}).get("notifications") or {}
        self.assertEqual(
            notifications,
            {},
            "non-ZIP uploads must not produce iscanet zip-import toasts",
        )

    # ------------------------------------------------------------------
    # List-view flow regression: no default_journal_id in context
    # ------------------------------------------------------------------

    def test_list_view_zip_upload_resolves_journal_from_default_move_type(self):
        """The Customer Invoices / Vendor Bills *list view* Upload button
        does not pin a journal: the frontend calls
        ``account.journal.create_document_from_attachment`` with an empty
        ``self`` and only ``default_move_type`` in context.

        Native code uses ``default_move_type`` to find a journal of the
        matching type. Because our underscore override strips
        ``default_move_type`` (so l10n_it_edi probes both buyer/seller), it
        must replicate that fallback before calling super, otherwise super
        raises 'The journal in which to upload the invoice is not specified.'

        Regression: this is exactly what blew up when a user clicked Upload
        on a customer-invoices list with a ZIP."""
        zip_bytes = _build_zip([("IT01234567890_FPR01.xml", self.bill_xml_a)])
        attachment = self.env["ir.attachment"].create(
            {"name": "from_list_view.zip", "raw": zip_bytes}
        )
        # Mimic the list-view RPC: empty journal recordset, only
        # default_move_type in context (no default_journal_id).
        invoices = (
            self.env["account.journal"]
            .with_context(
                default_move_type="out_invoice",
                allowed_company_ids=self.company.ids,
            )
            ._create_document_from_attachment(attachment.ids)
        )
        self.assertEqual(len(invoices), 1)
        # Our test fixture is a vendor bill (we are the buyer), so even
        # though the user uploaded from Customer Invoices, the move ends up
        # on a purchase journal via _compute_journal_id auto-routing.
        self.assertEqual(invoices.move_type, "in_invoice")
        self.assertEqual(invoices.journal_id.type, "purchase")
