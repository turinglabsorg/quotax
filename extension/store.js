import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {EventEmitter} from 'resource:///org/gnome/shell/misc/signals.js';

import {_} from './i18n.js';
import {DISPLAY_NAMES, PROVIDERS, nowSeconds} from './model.js';

Gio._promisify(Gio.File.prototype, 'load_contents_async');

// The usage endpoints rate-limit aggressive clients: never poll more often than this.
const REFRESH_INTERVAL = 5 * 60;
const STALE_AFTER = 60;
const AFTER_WAKE_DELAY = 5;
const AFTER_RESET_DELAY = 20;

// Linked accounts, read from the backend's accounts.json and reloaded whenever it changes,
// including when it is changed from a terminal with the `quota` command.
export class AccountStore extends EventEmitter {
    constructor() {
        super();
        this.accounts = [];
        const directory = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_config_dir(), 'quota']));
        try {
            directory.make_directory_with_parents(null);
        } catch {
            // Already there.
        }
        this._file = directory.get_child('accounts.json');
        this._reloadId = 0;
        this._monitor = directory.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
        this._monitor.connect('changed', (monitor, file, otherFile) => {
            if ([file, otherFile].some(changed => changed?.get_basename() === 'accounts.json'))
                this._scheduleReload();
        });
    }

    async load() {
        let accounts = [];
        try {
            const [contents] = await this._file.load_contents_async(null);
            const raw = JSON.parse(new TextDecoder().decode(contents));
            accounts = (raw.accounts ?? []).filter(account =>
                PROVIDERS.includes(account.provider) && ['cli', 'managed'].includes(account.source) && account.id);
        } catch {
            // Missing or unreadable file: no linked accounts.
        }
        if (!this._monitor)
            return;
        this.accounts = accounts;
        this.emit('changed');
    }

    hasSharedLogin(provider) {
        return this.accounts.some(account => account.provider === provider && account.source === 'cli');
    }

    _scheduleReload() {
        if (this._reloadId)
            return;
        this._reloadId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
            this._reloadId = 0;
            this.load();
            return GLib.SOURCE_REMOVE;
        });
    }

    destroy() {
        if (this._reloadId)
            GLib.source_remove(this._reloadId);
        this._monitor.cancel();
        this._monitor = null;
    }
}

export class UsageStore extends EventEmitter {
    constructor(backend, accounts) {
        super();
        this._backend = backend;
        this._accounts = accounts;
        this.entries = new Map();
        this.lastRefresh = null;
        this.isRefreshing = false;
        this._needsAnotherRefresh = false;
        this._accountIds = '';
        this._sources = new Set();
        this._resetSourceId = 0;
        this._cancellable = new Gio.Cancellable();
    }

    start() {
        this._accountsChangedId = this._accounts.connect('changed', () => {
            const ids = this._accounts.accounts.map(account => account.id).sort().join();
            if (ids !== this._accountIds) {
                this._accountIds = ids;
                this.refresh();
            } else {
                this.emit('changed');
            }
        });
        this._addSource(GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, REFRESH_INTERVAL, () => {
            this.refresh();
            return GLib.SOURCE_CONTINUE;
        }));
        this._sleepSubscription = Gio.DBus.system.signal_subscribe(
            'org.freedesktop.login1', 'org.freedesktop.login1.Manager', 'PrepareForSleep',
            '/org/freedesktop/login1', null, Gio.DBusSignalFlags.NONE,
            (connection, sender, path, iface, signal, parameters) => {
                const [goingToSleep] = parameters.deepUnpack();
                if (!goingToSleep)
                    this._refreshAfter(AFTER_WAKE_DELAY);
            });
        this._accounts.load();
    }

    refreshIfStale() {
        if (this.lastRefresh !== null && nowSeconds() - this.lastRefresh < STALE_AFTER)
            return;
        this.refresh();
    }

    refresh() {
        if (this.isRefreshing) {
            this._needsAnotherRefresh = true;
            return;
        }
        const current = this._accounts.accounts;
        const ids = new Set(current.map(account => account.id));
        for (const id of [...this.entries.keys()]) {
            if (!ids.has(id))
                this.entries.delete(id);
        }
        if (!current.length) {
            this.emit('changed');
            return;
        }
        this.isRefreshing = true;
        this.emit('changed');
        this._fetch(current).catch(logError);
    }

    async _fetch(current) {
        const answered = new Set();
        try {
            await this._backend.stream(['fetch'], event => {
                if (event.type !== 'result')
                    return;
                answered.add(event.id);
                this._apply(event);
            }, this._cancellable);
        } catch (error) {
            logError(error, 'Quota: backend failed');
        }
        if (this._cancellable.is_cancelled())
            return;
        for (const account of current) {
            if (!answered.has(account.id))
                this._apply({id: account.id, issue: {kind: 'invalidResponse', message: _('Unrecognized response.'), keepsLastSnapshot: true}});
        }
        this.lastRefresh = nowSeconds();
        this.isRefreshing = false;
        this._scheduleRefreshAfterNextReset();
        this.emit('changed');
        if (this._needsAnotherRefresh) {
            this._needsAnotherRefresh = false;
            this.refresh();
        }
    }

    _apply(event) {
        if (!this._accounts.accounts.some(account => account.id === event.id))
            return;
        if (event.snapshot) {
            this.entries.set(event.id, {snapshot: event.snapshot, issue: null});
        } else {
            const previous = event.issue.keepsLastSnapshot ? this.entries.get(event.id)?.snapshot ?? null : null;
            this.entries.set(event.id, {snapshot: previous, issue: event.issue});
        }
        this.emit('changed');
    }

    _scheduleRefreshAfterNextReset() {
        if (this._resetSourceId) {
            GLib.source_remove(this._resetSourceId);
            this._sources.delete(this._resetSourceId);
            this._resetSourceId = 0;
        }
        const now = nowSeconds();
        const resets = [...this.entries.values()]
            .flatMap(entry => entry.snapshot?.windows ?? [])
            .map(window => window.resetsAt)
            .filter(resetsAt => resetsAt && resetsAt > now);
        if (!resets.length)
            return;
        const next = Math.min(...resets) - now;
        if (next < REFRESH_INTERVAL)
            this._resetSourceId = this._refreshAfter(next + AFTER_RESET_DELAY);
    }

    _refreshAfter(seconds) {
        const id = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, Math.ceil(seconds), () => {
            this._sources.delete(id);
            if (id === this._resetSourceId)
                this._resetSourceId = 0;
            this.refresh();
            return GLib.SOURCE_REMOVE;
        });
        return this._addSource(id);
    }

    _addSource(id) {
        this._sources.add(id);
        return id;
    }

    destroy() {
        this._cancellable.cancel();
        for (const id of this._sources)
            GLib.source_remove(id);
        this._sources.clear();
        if (this._sleepSubscription)
            Gio.DBus.system.signal_unsubscribe(this._sleepSubscription);
        if (this._accountsChangedId)
            this._accounts.disconnect(this._accountsChangedId);
    }
}

// Linking: the logins the CLIs already have, and isolated sign-ins in the browser.
export class LinkController extends EventEmitter {
    constructor(backend, accounts) {
        super();
        this._backend = backend;
        this._accounts = accounts;
        this.sharedLogins = {};
        this.isDetecting = false;
        this._signIns = {};
        this._destroyed = false;
    }

    signInState(provider) {
        return this._signIns[provider] ?? {state: 'idle'};
    }

    async detectSharedLogins() {
        if (this.isDetecting)
            return;
        this.isDetecting = true;
        this.emit('changed');
        const found = {};
        try {
            await this._backend.stream(['detect'], event => {
                if (event.type === 'login' && event.identity)
                    found[event.provider] = event.identity;
            });
        } catch (error) {
            logError(error, 'Quota: detection failed');
        }
        if (this._destroyed)
            return;
        this.sharedLogins = found;
        this.isDetecting = false;
        this.emit('changed');
    }

    async linkSharedLogin(provider) {
        if (this._accounts.hasSharedLogin(provider))
            return;
        const identity = this.sharedLogins[provider];
        const args = ['link', provider];
        if (identity?.email)
            args.push('--email', identity.email);
        if (identity?.plan)
            args.push('--plan', identity.plan);
        let linked = null;
        await this._backend.stream(args, event => {
            if (event.type === 'linked')
                linked = event.account;
        });
        await this._accounts.load();
        if (linked && !this._destroyed)
            this.emit('linked', linked.id);
    }

    startSignIn(provider) {
        this._signIns[provider]?.cancellable?.cancel();
        const cancellable = new Gio.Cancellable();
        const attempt = {state: 'waiting', url: null, cancellable};
        this._signIns[provider] = attempt;
        this.emit('changed');

        let outcome = null;
        this._backend.stream(['signin', provider], event => {
            if (event.type === 'url' && this._signIns[provider] === attempt) {
                attempt.url = event.url;
                this.emit('changed');
            } else if (event.type === 'linked' || event.type === 'error') {
                outcome = event;
            }
        }, cancellable).catch(error => logError(error, 'Quota: sign-in failed')).finally(async () => {
            if (this._destroyed || this._signIns[provider] !== attempt)
                return;
            if (cancellable.is_cancelled()) {
                this._signIns[provider] = {state: 'idle'};
            } else if (outcome?.type === 'linked') {
                this._signIns[provider] = {state: 'idle'};
                await this._accounts.load();
                this.emit('linked', outcome.account.id);
            } else {
                this._signIns[provider] = {
                    state: 'failed',
                    message: outcome?.message ?? _('{name} sign-in was not completed.', {name: DISPLAY_NAMES[provider]}),
                };
            }
            this.emit('changed');
        });
    }

    cancelSignIn(provider) {
        this._signIns[provider]?.cancellable?.cancel();
    }

    async unlink(account) {
        await this._backend.stream(['unlink', account.id]);
        await this._accounts.load();
    }

    destroy() {
        this._destroyed = true;
        for (const attempt of Object.values(this._signIns))
            attempt.cancellable?.cancel();
    }
}
