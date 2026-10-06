import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Animation from 'resource:///org/gnome/shell/ui/animation.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

import {Glyph, UsageBar} from './glyphs.js';
import {_} from './i18n.js';
import {
    CLI_NAMES, DISPLAY_NAMES, PROVIDERS, countdown, displaySuffix, displayValue, hasReset, nowSeconds,
    relative, remainingAt, tightestWindow, usageLevel,
} from './model.js';

const CLOCK_INTERVAL = 30;
const VERTICAL = Clutter.Orientation.VERTICAL;
// Logical pixels around the scrolled list: header, footer, menu padding and arrow.
const RESERVED_HEIGHT = {usage: 170, addAccount: 250};

// A scrolled St.Viewport sizes its content from its layout's minimum height, which squeezes
// buttons and labels. Inside the viewport, this list reports its natural height as its minimum,
// so the cards keep their size and the viewport scrolls instead.
const NaturalHeightList = GObject.registerClass(
class QuotaNaturalHeightList extends St.BoxLayout {
    _init() {
        super._init({style_class: 'quota-list', orientation: VERTICAL});
    }

    vfunc_get_preferred_height(forWidth) {
        const [, natural] = super.vfunc_get_preferred_height(forWidth);
        return [natural, natural];
    }
});

export const QuotaIndicator = GObject.registerClass(
class QuotaIndicator extends PanelMenu.Button {
    _init({store, accounts, linker, settings}) {
        super._init(0.5, 'Quotax');
        this._store = store;
        this._accounts = accounts;
        this._linker = linker;
        this._settings = settings;
        this._page = 'usage';
        this._showSettings = false;
        this._expanded = new Set();
        this._scroll = null;
        this._scrolledPage = null;

        this.accessible_name = 'Quotax';
        this._panelBox = new St.BoxLayout({style_class: 'quota-panel-box', y_align: Clutter.ActorAlign.CENTER});
        this.add_child(this._panelBox);

        this._content = new St.BoxLayout({style_class: 'quota-content', orientation: VERTICAL});
        this.menu.box.add_child(this._content);
        this.menu.connect('open-state-changed', (menu, open) => {
            if (open) {
                this._store.refreshIfStale();
                this._renderMenu();
            }
        });

        const update = () => this._update();
        this._connections = [
            [this._store, this._store.connect('changed', update)],
            [this._accounts, this._accounts.connect('changed', update)],
            [this._linker, this._linker.connect('changed', update)],
            [this._linker, this._linker.connect('linked', () => this._onLinked())],
        ];
        this._settingsChangedId = this._settings.connect('changed::display-mode', update);
        this._clockId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, CLOCK_INTERVAL, () => {
            this._update();
            return GLib.SOURCE_CONTINUE;
        });
        this._update();
    }

    _onDestroy() {
        super._onDestroy();
        GLib.source_remove(this._clockId);
        this._settings.disconnect(this._settingsChangedId);
        for (const [emitter, id] of this._connections)
            emitter.disconnect(id);
    }

    _onLinked() {
        this._page = 'usage';
        this._update();
        if (!this.menu.isOpen)
            this.menu.open();
    }

    get _displayMode() {
        return this._settings.get_string('display-mode');
    }

    _update() {
        this._renderPanel();
        if (this.menu.isOpen)
            this._renderMenu();
    }

    // Top bar: one value per linked account, the tightest account-wide window.
    _renderPanel() {
        this._panelBox.destroy_all_children();
        const now = nowSeconds();
        let shown = 0;
        for (const account of this._accounts.accounts) {
            const entry = this._store.entries.get(account.id);
            const window = entry?.snapshot ? tightestWindow(entry.snapshot, now) : null;
            if (!window)
                continue;
            const remaining = remainingAt(window, now);
            const item = new St.BoxLayout({style_class: `quota-panel-item level-${usageLevel(remaining)}`});
            if (entry.issue)
                item.opacity = 140;
            item.add_child(new Glyph(account.provider, 'quota-panel-glyph'));
            item.add_child(new St.Label({
                text: `${displayValue(this._displayMode, remaining)}%`,
                style_class: 'quota-panel-label',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            this._panelBox.add_child(item);
            shown++;
        }
        if (!shown)
            this._panelBox.add_child(new Glyph('gauge', 'quota-panel-glyph quota-panel-empty'));
    }

    _renderMenu() {
        // Rebuilt on every change; the list keeps its scroll position across rebuilds.
        const scrolled = this._scroll?.vadjustment.value ?? 0;
        const page = this._page;
        this._content.destroy_all_children();
        this._scroll = null;
        if (page === 'addAccount')
            this._renderAddAccount();
        else
            this._renderUsage();
        if (scrolled && page === this._scrolledPage) {
            const adjustment = this._scroll.vadjustment;
            const id = adjustment.connect('changed', () => {
                adjustment.disconnect(id);
                adjustment.value = scrolled;
            });
        }
        this._scrolledPage = page;
    }

    // Many accounts can be taller than the screen: the cards scroll, the header stays.
    _addScrollableList(list) {
        const workArea = Main.layoutManager.getWorkAreaForMonitor(Main.layoutManager.primaryIndex);
        const {scaleFactor} = St.ThemeContext.get_for_stage(global.stage);
        const maxHeight = Math.max(200, Math.floor(workArea.height / scaleFactor) - RESERVED_HEIGHT[this._page]);
        const viewport = new St.BoxLayout({orientation: VERTICAL});
        viewport.add_child(list);
        this._scroll = new St.ScrollView({
            style_class: 'quota-scroll',
            style: `max-height: ${maxHeight}px;`,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: true,
            child: viewport,
        });
        this._content.add_child(this._scroll);
    }

    _renderUsage() {
        const now = nowSeconds();
        const accounts = this._accounts.accounts;

        const header = new St.BoxLayout({style_class: 'quota-header'});
        const titles = new St.BoxLayout({orientation: VERTICAL, x_expand: true});
        titles.add_child(new St.Label({text: 'Quotax', style_class: 'quota-title'}));
        titles.add_child(secondary(this._subtitle(now)));
        header.add_child(titles);
        if (this._store.isRefreshing) {
            const spinner = new Animation.Spinner(16, {animate: true});
            const box = new St.Bin({style_class: 'quota-icon-button', child: spinner});
            header.add_child(box);
            spinner.play();
        } else {
            const refresh = iconButton('view-refresh-symbolic', _('Refresh now'), () => this._store.refresh());
            refresh.reactive = accounts.length > 0;
            header.add_child(refresh);
        }
        const gear = iconButton('emblem-system-symbolic', _('Settings'), () => {
            this._showSettings = !this._showSettings;
            this._renderMenu();
        });
        gear.checked = this._showSettings;
        header.add_child(gear);
        this._content.add_child(header);

        const list = new NaturalHeightList();
        if (this._showSettings)
            list.add_child(this._settingsCard());
        if (!accounts.length)
            list.add_child(this._emptyState());
        for (const account of accounts)
            list.add_child(this._accountCard(account, this._store.entries.get(account.id), now));
        this._addScrollableList(list);

        if (accounts.length) {
            const add = new St.Button({style_class: 'quota-text-button', x_align: Clutter.ActorAlign.START, can_focus: true});
            const label = new St.BoxLayout({style_class: 'quota-text-button-box'});
            label.add_child(new St.Icon({icon_name: 'list-add-symbolic', style_class: 'quota-text-button-icon'}));
            label.add_child(new St.Label({text: _('Add account'), y_align: Clutter.ActorAlign.CENTER}));
            add.set_child(label);
            add.connect('clicked', () => this._showAddAccount());
            this._content.add_child(add);
        }
    }

    _subtitle(now) {
        if (!this._accounts.accounts.length)
            return _('No accounts');
        if (this._store.isRefreshing)
            return _('Updating…');
        if (this._store.lastRefresh === null)
            return _('Waiting for data…');
        return _('Updated {time}', {time: relative(this._store.lastRefresh, now)});
    }

    _settingsCard() {
        const card = new St.BoxLayout({style_class: 'quota-card quota-settings', orientation: VERTICAL});
        card.add_child(new St.Label({text: _('Show'), style_class: 'quota-card-title'}));
        const segments = new St.BoxLayout({style_class: 'quota-segments'});
        for (const [mode, title] of [['remaining', _('Percentage left')], ['used', _('Percentage used')]]) {
            const segment = new St.Button({
                label: title,
                style_class: 'button quota-segment',
                toggle_mode: true,
                checked: this._displayMode === mode,
                x_expand: true,
                can_focus: true,
            });
            segment.connect('clicked', () => this._settings.set_string('display-mode', mode));
            segments.add_child(segment);
        }
        card.add_child(segments);
        return card;
    }

    _emptyState() {
        const card = new St.BoxLayout({style_class: 'quota-card', orientation: VERTICAL});
        card.add_child(new St.Label({text: _('No linked accounts'), style_class: 'quota-card-title'}));
        card.add_child(wrapped(_('Choose which Claude, Codex, Grok and Ollama Cloud accounts to monitor.'), 'quota-body'));
        const add = new St.Button({
            label: _('Add account'),
            style_class: 'button default quota-small-button',
            x_align: Clutter.ActorAlign.START,
            can_focus: true,
        });
        add.connect('clicked', () => this._showAddAccount());
        card.add_child(add);
        return card;
    }

    _accountCard(account, entry, now) {
        const card = new St.BoxLayout({style_class: 'quota-card', orientation: VERTICAL});

        const header = new St.BoxLayout({style_class: 'quota-card-header'});
        const glyph = new Glyph(account.provider, `quota-card-glyph quota-accent-${account.provider}`);
        glyph.y_align = Clutter.ActorAlign.START;
        header.add_child(glyph);
        const titles = new St.BoxLayout({orientation: VERTICAL, x_expand: true});
        titles.add_child(new St.Label({text: DISPLAY_NAMES[account.provider], style_class: 'quota-card-title'}));
        const subtitle = secondary(entry?.snapshot?.account ?? account.email ?? sourceLabel(account));
        subtitle.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        titles.add_child(subtitle);
        header.add_child(titles);
        const plan = entry?.snapshot?.plan ?? account.plan;
        if (plan) {
            header.add_child(new St.Label({
                text: plan,
                style_class: 'quota-badge',
                y_align: Clutter.ActorAlign.START,
            }));
        }
        const more = iconButton('view-more-symbolic', _('More'), () => {
            if (this._expanded.has(account.id))
                this._expanded.delete(account.id);
            else
                this._expanded.add(account.id);
            this._renderMenu();
        });
        more.y_align = Clutter.ActorAlign.START;
        more.checked = this._expanded.has(account.id);
        header.add_child(more);
        card.add_child(header);

        if (this._expanded.has(account.id)) {
            const actions = new St.BoxLayout({style_class: 'quota-actions'});
            const source = account.source === 'cli'
                ? _('Uses the {name} login', {name: CLI_NAMES[account.provider]})
                : _('Linked by Quotax');
            const sourceText = secondary(source);
            sourceText.x_expand = true;
            sourceText.y_align = Clutter.ActorAlign.CENTER;
            actions.add_child(sourceText);
            const unlink = new St.Button({
                label: _('Unlink account'),
                style_class: 'button quota-small-button quota-destructive',
                can_focus: true,
            });
            unlink.connect('clicked', () => {
                this._expanded.delete(account.id);
                this._linker.unlink(account).catch(logError);
            });
            actions.add_child(unlink);
            card.add_child(actions);
        }

        for (const window of entry?.snapshot?.windows ?? [])
            card.add_child(this._windowRow(window, now));

        if (entry?.issue) {
            const text = entry.snapshot
                ? _('Data from {time}. {message}', {time: relative(entry.snapshot.fetchedAt, now), message: entry.issue.message})
                : entry.issue.message;
            card.add_child(issueLine(entry.issue.kind, text));
        } else if (!entry) {
            card.add_child(secondary(_('Loading…')));
        }
        return card;
    }

    _windowRow(window, now) {
        const remaining = remainingAt(window, now);
        const value = displayValue(this._displayMode, remaining);
        const level = usageLevel(remaining);
        const row = new St.BoxLayout({style_class: 'quota-window', orientation: VERTICAL});

        const line = new St.BoxLayout({style_class: 'quota-window-line'});
        line.add_child(new St.Label({text: window.label, style_class: 'quota-window-label', x_expand: true}));
        line.add_child(new St.Label({text: `${value}%`, style_class: `quota-window-value level-${level}`}));
        const suffix = secondary(displaySuffix(this._displayMode));
        suffix.y_align = Clutter.ActorAlign.END;
        line.add_child(suffix);
        row.add_child(line);

        row.add_child(new UsageBar(value / 100, level));
        if (window.resetsAt) {
            row.add_child(secondary(hasReset(window, now)
                ? _('Reset, updating')
                : _('Resets in {countdown}', {countdown: countdown(window.resetsAt, now)})));
        }
        return row;
    }

    _showAddAccount() {
        this._page = 'addAccount';
        this._linker.detectSharedLogins();
        this._renderMenu();
    }

    _renderAddAccount() {
        const header = new St.BoxLayout({style_class: 'quota-header'});
        const back = iconButton('go-previous-symbolic', _('Back'), () => {
            this._page = 'usage';
            this._renderMenu();
        });
        header.add_child(back);
        header.add_child(new St.Label({text: _('Add account'), style_class: 'quota-title', y_align: Clutter.ActorAlign.CENTER}));
        this._content.add_child(header);

        const list = new NaturalHeightList();
        for (const provider of PROVIDERS)
            list.add_child(this._providerLinkCard(provider));
        this._addScrollableList(list);

        this._content.add_child(wrapped(
            _("Sign-in happens in your browser, on the service's official page: Quotax never sees your password. Each new account stays separate from your CLI logins."),
            'quota-footnote'));
    }

    _providerLinkCard(provider) {
        const card = new St.BoxLayout({style_class: 'quota-card', orientation: VERTICAL});
        const header = new St.BoxLayout({style_class: 'quota-card-header'});
        header.add_child(new Glyph(provider, `quota-card-glyph quota-accent-${provider}`));
        header.add_child(new St.Label({text: DISPLAY_NAMES[provider], style_class: 'quota-card-title', y_align: Clutter.ActorAlign.CENTER}));
        card.add_child(header);
        card.add_child(this._sharedLoginRow(provider));
        card.add_child(this._signInRow(provider));
        return card;
    }

    _sharedLoginRow(provider) {
        const cliName = CLI_NAMES[provider];
        const identity = this._linker.sharedLogins[provider];
        if (identity) {
            const row = new St.BoxLayout({style_class: 'quota-row'});
            const titles = new St.BoxLayout({orientation: VERTICAL, x_expand: true, y_align: Clutter.ActorAlign.CENTER});
            titles.add_child(secondary(_('Current {name} login', {name: cliName})));
            const name = new St.Label({
                text: [identity.email ?? _('Active account'), identity.plan].filter(Boolean).join(' · '),
                style_class: 'quota-body',
            });
            name.clutter_text.ellipsize = Pango.EllipsizeMode.MIDDLE;
            titles.add_child(name);
            row.add_child(titles);
            if (this._accounts.hasSharedLogin(provider)) {
                const linked = new St.BoxLayout({style_class: 'quota-linked', y_align: Clutter.ActorAlign.CENTER});
                linked.add_child(new St.Icon({icon_name: 'object-select-symbolic', style_class: 'quota-issue-icon'}));
                linked.add_child(secondary(_('Linked')));
                row.add_child(linked);
            } else {
                const link = new St.Button({
                    label: _('Link'),
                    style_class: 'button quota-small-button',
                    y_align: Clutter.ActorAlign.CENTER,
                    can_focus: true,
                });
                link.connect('clicked', () => this._linker.linkSharedLogin(provider).catch(logError));
                row.add_child(link);
            }
            return row;
        }
        if (this._linker.isDetecting)
            return spinnerRow(_('Looking for the {name} login…', {name: cliName}));
        return secondary(_('No {name} login on this computer.', {name: cliName}));
    }

    _signInRow(provider) {
        const attempt = this._linker.signInState(provider);
        if (attempt.state === 'waiting') {
            const row = new St.BoxLayout({style_class: 'quota-row'});
            const spinner = new Animation.Spinner(16, {animate: true});
            spinner.y_align = Clutter.ActorAlign.START;
            row.add_child(spinner);
            spinner.play();
            const texts = new St.BoxLayout({orientation: VERTICAL, x_expand: true});
            texts.add_child(wrapped(_('Finish signing in in your browser…'), 'quota-body'));
            if (attempt.url) {
                const open = new St.Button({
                    label: _('Open the sign-in page'),
                    style_class: 'quota-link',
                    x_align: Clutter.ActorAlign.START,
                    can_focus: true,
                });
                open.connect('clicked', () => {
                    Gio.AppInfo.launch_default_for_uri(attempt.url, global.create_app_launch_context(0, -1));
                    this.menu.close();
                });
                texts.add_child(open);
            }
            row.add_child(texts);
            const cancel = new St.Button({
                label: _('Cancel'),
                style_class: 'button quota-small-button',
                y_align: Clutter.ActorAlign.START,
                can_focus: true,
            });
            cancel.connect('clicked', () => this._linker.cancelSignIn(provider));
            row.add_child(cancel);
            return row;
        }

        const box = new St.BoxLayout({style_class: 'quota-sign-in', orientation: VERTICAL});
        if (attempt.state === 'failed')
            box.add_child(issueLine('error', attempt.message));
        const button = new St.Button({
            style_class: 'button quota-small-button',
            x_align: Clutter.ActorAlign.START,
            can_focus: true,
        });
        const label = new St.BoxLayout({style_class: 'quota-button-box'});
        if (attempt.state !== 'failed')
            label.add_child(new St.Icon({icon_name: 'contact-new-symbolic', style_class: 'quota-button-icon'}));
        label.add_child(new St.Label({
            text: attempt.state === 'failed' ? _('Try again') : _('Sign in to another account…'),
            y_align: Clutter.ActorAlign.CENTER,
        }));
        button.set_child(label);
        button.connect('clicked', () => this._linker.startSignIn(provider));
        box.add_child(button);
        return box;
    }
});

function sourceLabel(account) {
    return account.source === 'cli'
        ? _('{name} login', {name: CLI_NAMES[account.provider]})
        : _('linked by Quotax');
}

function secondary(text) {
    return new St.Label({text, style_class: 'quota-secondary'});
}

function wrapped(text, styleClass) {
    const label = new St.Label({text, style_class: styleClass, x_expand: true});
    label.clutter_text.line_wrap = true;
    label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    return label;
}

function iconButton(iconName, accessibleName, onClicked) {
    const button = new St.Button({
        style_class: 'quota-icon-button',
        child: new St.Icon({icon_name: iconName, icon_size: 16}),
        accessible_name: accessibleName,
        toggle_mode: false,
        can_focus: true,
        y_align: Clutter.ActorAlign.CENTER,
    });
    button.connect('clicked', onClicked);
    return button;
}

function spinnerRow(text) {
    const row = new St.BoxLayout({style_class: 'quota-row'});
    const spinner = new Animation.Spinner(16, {animate: true});
    row.add_child(spinner);
    spinner.play();
    const label = secondary(text);
    label.y_align = Clutter.ActorAlign.CENTER;
    row.add_child(label);
    return row;
}

const ISSUE_ICONS = {
    signedOut: ['avatar-default-symbolic', 'quota-issue-icon'],
    noQuota: ['dialog-information-symbolic', 'quota-issue-icon'],
    sessionExpired: ['dialog-password-symbolic', 'quota-issue-icon level-warning'],
};

// Icon + secondary text; `code` between backticks renders in monospace.
function issueLine(kind, text) {
    const [iconName, iconClass] = ISSUE_ICONS[kind] ?? ['dialog-warning-symbolic', 'quota-issue-icon level-warning'];
    const row = new St.BoxLayout({style_class: 'quota-issue'});
    const icon = new St.Icon({icon_name: iconName, style_class: iconClass, y_align: Clutter.ActorAlign.START});
    row.add_child(icon);
    const label = wrapped('', 'quota-secondary');
    const markup = text.split('`')
        .map((part, index) => {
            const escaped = GLib.markup_escape_text(part, -1);
            return index % 2 ? `<tt>${escaped}</tt>` : escaped;
        })
        .join('');
    label.clutter_text.set_markup(markup);
    row.add_child(label);
    return row;
}
