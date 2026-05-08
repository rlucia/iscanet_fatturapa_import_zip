// ESLint flat config for the Iscanet FatturaPA ZIP Import module.
//
// The module ships a single JS file (`document_file_uploader_patch.esm.js`)
// that runs in Odoo's web client. The config below is intentionally narrow:
// it covers what is actually present rather than every possible Odoo JS
// pattern. Files matching `*.esm.js` are parsed as ES modules; everything
// else is treated as classic script (Odoo's older convention). Globals
// injected by the Odoo runtime are declared so they don't trigger no-undef.

const jsdoc = require("eslint-plugin-jsdoc");

const odooGlobals = {
    odoo: "readonly",
    owl: "readonly",
    luxon: "readonly",
};

module.exports = [
    {
        plugins: {jsdoc},
        languageOptions: {
            ecmaVersion: 2024,
            sourceType: "script",
            globals: odooGlobals,
        },
        rules: {
            eqeqeq: ["warn", "always"],
            "no-unused-vars": ["warn", {argsIgnorePattern: "^_"}],
            "no-var": "warn",
            "prefer-const": "warn",
            "no-implicit-globals": "error",
            "jsdoc/check-tag-names": [
                "warn",
                {definedTags: ["odoo-module"]},
            ],
        },
    },
    {
        files: ["**/*.esm.js"],
        languageOptions: {
            ecmaVersion: 2024,
            sourceType: "module",
            globals: odooGlobals,
        },
    },
];
