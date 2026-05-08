import logging
import os
import zipfile
from io import BytesIO

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Safety guards against zip-bombs and runaway nesting.
MAX_ZIP_DEPTH = 5
MAX_ZIP_ENTRIES = 1000
MAX_ZIP_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB

# File suffixes (lower-cased) that the FatturaPA decoder in l10n_it_edi can handle.
FATTURAPA_SUFFIXES = (".xml", ".xml.p7m", ".p7m")


class AccountJournal(models.Model):
    _inherit = "account.journal"

    # Context key used to thread per-zip summary data between this module's
    # `create_document_from_attachment` (public) override and its
    # `_create_document_from_attachment` (underscore) override. The value is a
    # mutable list of (zip_name, count) tuples; the underscore impl appends to
    # it and the public impl reads it after super() returns to populate the
    # action's ``notifications`` dict for the frontend.
    _ISCANET_ZIP_SUMMARY_CTX_KEY = "_iscanet_fpzip_summary"

    def create_document_from_attachment(self, attachment_ids):
        """Override the public Upload entry point to surface per-archive
        feedback in the action returned to the frontend.

        Concretely: if any uploaded attachment is a ZIP, we wrap the call so
        the underscore override can build a list of ``(zip_name, count)``
        tuples; we then translate those into ``action.context.notifications``
        entries, which the ``DocumentFileUploader`` JS component renders as
        sticky info toasts ("Imported N documents from <zip>").
        """
        attachments = self.env["ir.attachment"].browse(attachment_ids)
        if not any(self._iscanet_fatturapa_zip_is_zip(att) for att in attachments):
            return super().create_document_from_attachment(attachment_ids)

        summary = []  # populated by the underscore override
        new_context = dict(self.env.context)
        new_context[self._ISCANET_ZIP_SUMMARY_CTX_KEY] = summary
        rebound = self.with_env(self.env(context=new_context))
        action_vals = super(AccountJournal, rebound).create_document_from_attachment(
            attachment_ids
        )

        if summary:
            # The native action's "context" is built from ``self._context``,
            # which is a frozendict; copy to a mutable one before adding our
            # notifications dict.
            ctx = dict(action_vals.get("context") or {})
            notifications = dict(ctx.get("notifications") or {})
            for zip_name, count in summary:
                notifications[zip_name] = _(
                    "Imported %(count)d FatturaPA documents from this archive.",
                    count=count,
                )
            ctx["notifications"] = notifications
            action_vals["context"] = ctx
        return action_vals

    def _create_document_from_attachment(self, attachment_ids):
        """Extend the native upload flow so that a ZIP archive of FatturaPA
        documents produces one ``account.move`` per inner XML/P7M file rather
        than a single blank invoice with the ZIP attached.

        Strategy:
        * If none of the uploaded attachments is a ZIP, defer to ``super()``.
        * Otherwise, recursively explode each ZIP into fresh ``ir.attachment``
          records (one per FatturaPA-looking entry), and call ``super()`` with
          the flattened list.
        * ``default_move_type`` is dropped from the context for the super-call
          so that l10n_it_edi probes both buyer and seller sections of each
          XML; the stored compute on ``account.move.journal_id`` then
          re-routes each move to a journal whose type matches the detected
          direction.
        * Each created move gets a chatter line identifying the source archive
          so the connection to the original ZIP is preserved on every invoice.
        """
        attachments = self.env["ir.attachment"].browse(attachment_ids)
        if not any(self._iscanet_fatturapa_zip_is_zip(att) for att in attachments):
            return super()._create_document_from_attachment(attachment_ids)

        # Resolve the journal _before_ calling super, because we are about to
        # strip ``default_move_type`` from context (so l10n_it_edi probes both
        # directions per file). Native ``_create_document_from_attachment``
        # uses ``default_move_type`` as a fallback to look up a journal when
        # ``self`` is empty (e.g. the list-view Upload button on Customer
        # Invoices / Vendor Bills, which doesn't pin a journal). If we strip
        # without resolving, that fallback breaks with "The journal in which
        # to upload the invoice is not specified."
        journal = self._iscanet_fatturapa_zip_resolve_journal()
        if not journal:
            # No journal could be resolved; defer to super so the user gets
            # the same standard error Odoo would normally raise here.
            return super()._create_document_from_attachment(attachment_ids)

        expanded_ids = []
        # inner ir.attachment id -> name of the outer ZIP it came from
        inner_to_zip = {}
        # zip_name -> count of inner FatturaPA documents extracted
        per_zip_counts = {}
        for att in attachments:
            if self._iscanet_fatturapa_zip_is_zip(att):
                inner_ids = self._iscanet_fatturapa_zip_explode(att)
                expanded_ids.extend(inner_ids)
                per_zip_counts[att.name] = per_zip_counts.get(att.name, 0) + len(
                    inner_ids
                )
                for inner_id in inner_ids:
                    inner_to_zip[inner_id] = att.name
            else:
                expanded_ids.append(att.id)

        if not expanded_ids:
            raise UserError(
                _(
                    "The uploaded ZIP does not contain any FatturaPA XML "
                    "(or signed .xml.p7m) file."
                )
            )

        # Strip default_move_type so l10n_it_edi can decide direction per file
        # from the XML's CedentePrestatore / CessionarioCommittente sections.
        # `with_context(**kwargs)` only merges, so we cannot use it to remove a
        # key. Going through `with_env` to set a freshly-built context dict.
        # We call super on the resolved journal (not on `self`, which may have
        # been an empty recordset on entry) so super can skip its own journal
        # lookup that would otherwise fail without default_move_type.
        new_context = dict(self.env.context)
        new_context.pop("default_move_type", None)
        rebound = journal.with_env(journal.env(context=new_context))
        invoices = super(AccountJournal, rebound)._create_document_from_attachment(
            expanded_ids
        )

        # Provenance: stamp each move's chatter with the source archive name.
        # Each move has exactly one inbound attachment after the native flow
        # (the inner XML/P7M); look it up and post a single chatter line.
        for move in invoices:
            for att in move.attachment_ids:
                zip_name = inner_to_zip.get(att.id)
                if zip_name:
                    move.with_context(
                        no_new_invoice=True,
                        account_predictive_bills_disable_prediction=True,
                    ).message_post(
                        body=_(
                            "Imported from ZIP archive %(name)s",
                            name=zip_name,
                        )
                    )
                    break

        # Stash per-zip counts so the public override can turn them into
        # frontend notifications. No-op if called outside the public path
        # (e.g. from a unit test that hits the underscore method directly).
        summary = self.env.context.get(self._ISCANET_ZIP_SUMMARY_CTX_KEY)
        if summary is not None:
            for zip_name, count in per_zip_counts.items():
                summary.append((zip_name, count))

        return invoices

    def _iscanet_fatturapa_zip_resolve_journal(self):
        """Pick the journal under which the ZIP-imported moves will be
        created, mirroring the fallbacks of native
        ``_create_document_from_attachment`` so we can call super with a
        non-empty ``self`` after stripping ``default_move_type``.

        Resolution order:
        1. ``self`` if non-empty.
        2. ``default_journal_id`` from context (set by the dashboard tile
           uploader).
        3. First sale or purchase journal in the current company, picked
           from ``default_move_type`` (set by the list-view uploader on
           Customer Invoices / Vendor Bills).

        If none of the above yields a journal, ``self`` (empty) is returned
        and the caller defers to super, which will raise the same
        user-facing error native Odoo raises.
        """
        if self:
            return self
        journal = self.env["account.journal"].browse(
            self.env.context.get("default_journal_id")
        )
        if journal:
            return journal
        move = self.env["account.move"]
        move_type = self.env.context.get("default_move_type", "entry")
        if move_type in move.get_sale_types(include_receipts=True):
            journal_type = "sale"
        elif move_type in move.get_purchase_types(include_receipts=True):
            journal_type = "purchase"
        else:
            return self
        return self.env["account.journal"].search(
            [
                *self.env["account.journal"]._check_company_domain(self.env.company),
                ("type", "=", journal_type),
            ],
            limit=1,
        )

    @staticmethod
    def _iscanet_fatturapa_zip_is_zip(attachment):
        """Return True if the attachment's content is a ZIP archive.

        Detection is by magic bytes (PK\\x03\\x04 / PK\\x05\\x06 / PK\\x07\\x08)
        rather than filename or mimetype, so renamed or mis-typed files are
        still recognised.
        """
        raw = attachment.raw
        if not raw:
            return False
        return zipfile.is_zipfile(BytesIO(raw))

    def _iscanet_fatturapa_zip_explode(self, attachment, depth=0):
        """Recursively explode a ZIP attachment into per-file ``ir.attachment``
        records, one per FatturaPA XML or signed P7M entry.

        Returns the list of newly-created attachment IDs in archive order.
        Non-XML/P7M entries are skipped silently (with a debug log). Bad
        nested ZIPs raise ``UserError`` so the user gets a clear message
        instead of half-imported data.
        """
        if depth > MAX_ZIP_DEPTH:
            raise UserError(
                _(
                    "Nested ZIP archives exceed the maximum supported depth (%d).",
                    MAX_ZIP_DEPTH,
                )
            )

        try:
            zf = zipfile.ZipFile(BytesIO(attachment.raw))
        except zipfile.BadZipFile as exc:
            raise UserError(
                _(
                    "The uploaded file %(name)s could not be opened as a ZIP "
                    "archive: %(err)s",
                    name=attachment.name,
                    err=exc,
                )
            ) from exc

        new_ids = []
        total_uncompressed = 0
        entries_seen = 0
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue

                entries_seen += 1
                if entries_seen > MAX_ZIP_ENTRIES:
                    raise UserError(
                        _(
                            "ZIP archive %(name)s contains more than %(max)d "
                            "entries; refusing to import.",
                            name=attachment.name,
                            max=MAX_ZIP_ENTRIES,
                        )
                    )

                total_uncompressed += info.file_size
                if total_uncompressed > MAX_ZIP_TOTAL_UNCOMPRESSED:
                    raise UserError(
                        _(
                            "ZIP archive %(name)s exceeds the maximum total "
                            "uncompressed size (%(mb)d MB); refusing to import.",
                            name=attachment.name,
                            mb=MAX_ZIP_TOTAL_UNCOMPRESSED // (1024 * 1024),
                        )
                    )

                entry_name = os.path.basename(info.filename) or info.filename
                lower = entry_name.lower()

                if lower.endswith(".zip"):
                    inner_raw = zf.read(info)
                    inner_att = self.env["ir.attachment"].create(
                        {
                            "name": entry_name,
                            "raw": inner_raw,
                        }
                    )
                    new_ids.extend(
                        self._iscanet_fatturapa_zip_explode(inner_att, depth=depth + 1)
                    )
                elif lower.endswith(FATTURAPA_SUFFIXES):
                    inner_raw = zf.read(info)
                    inner_att = self.env["ir.attachment"].create(
                        {
                            "name": entry_name,
                            "raw": inner_raw,
                        }
                    )
                    new_ids.append(inner_att.id)
                else:
                    _logger.debug(
                        "Skipping non-FatturaPA entry %s inside ZIP %s",
                        info.filename,
                        attachment.name,
                    )

        return new_ids
