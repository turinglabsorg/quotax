// Development helper for scripts/preview.sh, only ever loaded in a throwaway headless shell:
// drives Quota's menu through its states, saves screenshots and quits.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot_area');

const QUOTA = 'quota@turinglabs.org';
const MARGIN = 24;

function sleep(milliseconds) {
    return new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, milliseconds, () => {
        resolve();
        return GLib.SOURCE_REMOVE;
    }));
}

async function until(condition, timeout = 15000) {
    for (let waited = 0; waited < timeout && !condition(); waited += 100)
        await sleep(100);
}

export default class QuotaPreviewExtension extends Extension {
    enable() {
        this._run()
            .catch(error => logError(error, 'Quota preview failed'))
            .finally(() => global.context.terminate());
    }

    disable() {}

    async _run() {
        const directory = GLib.getenv('QUOTA_PREVIEW_DIR');
        const variant = GLib.getenv('QUOTA_PREVIEW_VARIANT') ?? 'dark';
        await until(() => Main.panel.statusArea[QUOTA]);
        const indicator = Main.panel.statusArea[QUOTA];
        await until(() => indicator._store.lastRefresh !== null && !indicator._store.isRefreshing);
        await sleep(500);

        const shoot = async name => {
            await sleep(700);
            const menu = indicator.menu.actor.get_transformed_extents();
            const button = indicator.get_transformed_extents();
            const x = Math.max(0, Math.min(menu.origin.x, button.origin.x) - MARGIN);
            const width = global.stage.width - x;
            const height = Math.min(global.stage.height, menu.origin.y + menu.size.height + MARGIN);
            const file = Gio.File.new_for_path(`${directory}/${name}-${variant}.png`);
            const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
            await new Shell.Screenshot().screenshot_area(x, 0, width, height, stream);
            stream.close(null);
        };

        indicator.menu.open();
        await shoot('usage');

        indicator._showSettings = true;
        indicator._expanded.add(indicator._accounts.accounts[0].id);
        indicator._renderMenu();
        await shoot('settings');
        indicator._showSettings = false;
        indicator._expanded.clear();

        indicator._showAddAccount();
        await until(() => !indicator._linker.isDetecting);
        await shoot('add-account');

        indicator._linker._signIns.claude = {state: 'waiting', url: 'https://claude.ai/oauth/authorize'};
        indicator._linker._signIns.grok = {state: 'failed', message: 'Grok sign-in was not completed.'};
        indicator._renderMenu();
        await shoot('sign-in');
        indicator.menu.close();
    }
}
