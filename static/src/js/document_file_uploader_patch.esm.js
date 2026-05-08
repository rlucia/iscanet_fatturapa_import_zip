import {DocumentFileUploader} from "@account/components/document_file_uploader/document_file_uploader";
import {_t} from "@web/core/l10n/translation";
import {patch} from "@web/core/utils/patch";

/**
 * Show a sticky info toast as soon as a ZIP archive is uploaded, and dismiss
 * it once the server-side import finishes (success or error).
 *
 * Without this, the only feedback during a multi-minute import of a 20+
 * invoice ZIP is the browser's thin top-of-page loading bar, which is easy
 * to miss after alt-tabbing away.
 */
patch(DocumentFileUploader.prototype, {
    async onFileUploaded(file) {
        const isZip =
            (file && file.name && file.name.toLowerCase().endsWith(".zip")) ||
            file.type === "application/zip" ||
            file.type === "application/x-zip-compressed";
        if (isZip && !this._iscanetFpzipNotifClose) {
            this._iscanetFpzipNotifClose = this.notification.add(
                _t(
                    "Importing the ZIP archive — this may take a while for " +
                        "large batches. Please don't close this tab."
                ),
                {type: "info", sticky: true}
            );
        }
        return super.onFileUploaded(file);
    },

    async onUploadComplete() {
        try {
            return await super.onUploadComplete();
        } finally {
            if (this._iscanetFpzipNotifClose) {
                this._iscanetFpzipNotifClose();
                this._iscanetFpzipNotifClose = undefined;
            }
        }
    },
});
