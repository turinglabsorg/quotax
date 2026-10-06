import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

Gio._promisify(Gio.DataInputStream.prototype, 'read_line_async');
Gio._promisify(Gio.Subprocess.prototype, 'wait_async');

const SIGTERM = 15;

// Runs the bundled Python backend (`quotax <command> --json`) and streams its JSON events.
// Nothing here blocks the shell: every command is a subprocess read asynchronously.
export class Backend {
    constructor(extensionPath) {
        this._pythonPath = `${extensionPath}/backend`;
        this._processes = new Set();
    }

    async stream(args, onEvent = () => {}, cancellable = null) {
        if (cancellable?.is_cancelled())
            return null;
        const launcher = new Gio.SubprocessLauncher({
            flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
        });
        launcher.setenv('PYTHONPATH', this._pythonPath, true);
        launcher.set_cwd(GLib.get_home_dir());
        const proc = launcher.spawnv(['python3', '-B', '-m', 'quotax', ...args, '--json']);
        this._processes.add(proc);
        const cancelId = cancellable?.connect(() => proc.send_signal(SIGTERM)) ?? 0;
        const input = new Gio.DataInputStream({base_stream: proc.get_stdout_pipe(), close_base_stream: true});
        const decoder = new TextDecoder();
        try {
            for (;;) {
                const [bytes] = await input.read_line_async(GLib.PRIORITY_DEFAULT, null);
                if (bytes === null)
                    break;
                let event;
                try {
                    event = JSON.parse(decoder.decode(bytes));
                } catch {
                    continue;
                }
                onEvent(event);
            }
            await proc.wait_async(null);
            return proc.get_if_exited() ? proc.get_exit_status() : null;
        } finally {
            if (cancelId)
                cancellable.disconnect(cancelId);
            this._processes.delete(proc);
        }
    }

    destroy() {
        // The backend cleans up after itself on SIGTERM (child CLIs, half-finished sign-ins).
        for (const proc of this._processes)
            proc.send_signal(SIGTERM);
        this._processes.clear();
    }
}
