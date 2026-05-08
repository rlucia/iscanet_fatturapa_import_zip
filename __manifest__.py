{
    "name": "Iscanet - FatturaPA ZIP Import",
    "version": "18.0.1.0.0",
    "category": "Accounting/Localizations/EDI",
    "summary": "Import many FatturaPA XML files at once from a ZIP archive.",
    "author": "Rocco Lucia, Iscanet Internet Services S.r.l.",
    "website": "https://github.com/rlucia/iscanet_fatturapa_import_zip",
    "license": "Other OSI approved licence",
    "depends": ["account", "l10n_it_edi"],
    "assets": {
        "web.assets_backend": [
            "iscanet_fatturapa_import_zip/static/src/js/document_file_uploader_patch.esm.js",
        ],
    },
    "installable": True,
    "auto_install": False,
    "application": False,
}
