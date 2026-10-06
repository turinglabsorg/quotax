import GLib from 'gi://GLib';

// Same catalog as the Python backend (backend/quota/locale/<language>.json): keys are the English
// strings and `{name}` placeholders are filled after translation.
let catalog = {};

export function loadCatalog(directory) {
    catalog = {};
    for (const name of GLib.get_language_names()) {
        const code = name.split(/[._@]/)[0];
        if (code === 'C' || code === 'en')
            return;
        try {
            const [, contents] = GLib.file_get_contents(`${directory}/${code}.json`);
            catalog = JSON.parse(new TextDecoder().decode(contents));
            return;
        } catch {
            // No catalog for this language: try the next preference.
        }
    }
}

export function _(text, values = {}) {
    const translated = catalog[text] ?? text;
    return translated.replace(/\{(\w+)\}/g, (match, key) => key in values ? String(values[key]) : match);
}
