import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {Backend} from './backend.js';
import {loadCatalog} from './i18n.js';
import {QuotaIndicator} from './indicator.js';
import {AccountStore, LinkController, UsageStore} from './store.js';

export default class QuotaExtension extends Extension {
    enable() {
        loadCatalog(`${this.path}/backend/quota/locale`);
        this._settings = this.getSettings();
        this._backend = new Backend(this.path);
        this._accounts = new AccountStore();
        this._store = new UsageStore(this._backend, this._accounts);
        this._linker = new LinkController(this._backend, this._accounts);
        this._indicator = new QuotaIndicator({
            store: this._store,
            accounts: this._accounts,
            linker: this._linker,
            settings: this._settings,
        });
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._store.start();
    }

    disable() {
        this._indicator?.destroy();
        this._linker?.destroy();
        this._store?.destroy();
        this._accounts?.destroy();
        this._backend?.destroy();
        this._indicator = this._linker = this._store = this._accounts = this._backend = this._settings = null;
    }
}
