import {_} from './i18n.js';

export const PROVIDERS = ['claude', 'codex', 'grok', 'ollama'];
export const DISPLAY_NAMES = {claude: 'Claude', codex: 'Codex', grok: 'Grok', ollama: 'Ollama Cloud'};
export const CLI_NAMES = {claude: 'Claude Code', codex: 'Codex', grok: 'Grok', ollama: 'Ollama'};

export function nowSeconds() {
    return Date.now() / 1000;
}

export function hasReset(window, now) {
    return window.resetsAt !== null && window.resetsAt !== undefined && window.resetsAt <= now;
}

// Half away from zero, like the backend and the macOS app.
export function usedAt(window, now) {
    return hasReset(window, now) ? 0 : Math.floor(window.usedPercent + 0.5);
}

export function remainingAt(window, now) {
    return 100 - usedAt(window, now);
}

// Model-scoped limits do not block the whole account, so they only count when nothing else is reported.
export function tightestWindow(snapshot, now) {
    const accountWide = snapshot.windows.filter(window => window.kind !== 'weeklyModel');
    const candidates = accountWide.length ? accountWide : snapshot.windows;
    let tightest = null;
    for (const window of candidates) {
        if (!tightest || remainingAt(window, now) < remainingAt(tightest, now))
            tightest = window;
    }
    return tightest;
}

// Top bar: when an account reports both a 5-hour session and a longer window (weekly or monthly),
// it shows both, session above; otherwise the tightest account-wide window.
export function panelWindows(snapshot, now) {
    const session = snapshot.windows.find(window => window.kind === 'session');
    const longer = snapshot.windows.find(window => window.kind === 'weekly') ??
        snapshot.windows.find(window => window.kind === 'monthly');
    if (session && longer)
        return [session, longer];
    const tightest = tightestWindow(snapshot, now);
    return tightest ? [tightest] : [];
}

export function usageLevel(remainingPercent) {
    if (remainingPercent <= 5)
        return 'critical';
    if (remainingPercent <= 20)
        return 'warning';
    return 'normal';
}

export function displayValue(mode, remainingPercent) {
    return mode === 'used' ? 100 - remainingPercent : remainingPercent;
}

export function displaySuffix(mode) {
    return mode === 'used' ? _('used') : _('left');
}

export function countdown(to, now) {
    const seconds = Math.trunc(to - now);
    if (seconds <= 0)
        return _('now');
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0)
        return hours > 0 ? _('{days}d {hours}h', {days, hours}) : _('{days}d', {days});
    if (hours > 0)
        return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
    return `${Math.max(minutes, 1)}m`;
}

export function relative(date, now) {
    const seconds = Math.trunc(now - date);
    if (seconds < 60)
        return _('now');
    if (seconds < 3600)
        return _('{minutes} min ago', {minutes: Math.floor(seconds / 60)});
    return _('{hours} h ago', {hours: Math.floor(seconds / 3600)});
}
