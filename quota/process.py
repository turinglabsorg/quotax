"""Runs the providers' CLIs: plain pipes, or a pseudo-terminal for the ones that expect a TTY."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path


class TimedOut(Exception):
    pass


class Result:
    def __init__(self, status: int, stdout: str):
        self.status = status
        self.stdout = stdout


_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()

# Quota runs each CLI as a fresh terminal would: credentials handed down by a parent agent
# (or the Claude desktop app) must not stand in for the login stored on disk.
_INHERITED_PREFIXES = ("CLAUDE", "ANTHROPIC_")


def environment(overrides: dict[str, str | None], executable: str | Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(_INHERITED_PREFIXES)}
    home = str(Path.home())
    extra = [str(Path(executable).parent), f"{home}/.local/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    current = [entry for entry in env.get("PATH", "").split(":") if entry]
    env["PATH"] = ":".join(current + [entry for entry in extra if entry not in current])
    env.setdefault("TERM", "xterm-256color")
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run(
    executable: str | Path,
    arguments: list[str],
    *,
    env: dict[str, str | None] | None = None,
    timeout: float,
    keep_stdin_open: bool = False,
    tty: bool = False,
    on_output: Callable[[str], None] | None = None,
) -> Result:
    """Runs a command to completion, streaming its output to `on_output`. Raises TimedOut."""
    command = [str(executable), *arguments]
    environ = environment(env or {}, executable)
    cwd = tempfile.gettempdir()
    if tty:
        master, slave = os.openpty()
        _set_window_size(slave)
        process = subprocess.Popen(
            command, stdin=slave, stdout=slave, stderr=slave, env=environ, cwd=cwd, start_new_session=True
        )
        os.close(slave)
        streams = {master: True}
    else:
        master = None
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if keep_stdin_open else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environ,
            cwd=cwd,
            start_new_session=True,
        )
        streams = {process.stdout.fileno(): True, process.stderr.fileno(): False}
    _track(process)

    output = bytearray()
    deadline = time.monotonic() + timeout
    exited_at = None
    try:
        open_fds = set(streams)
        while open_fds:
            now = time.monotonic()
            if now >= deadline:
                raise TimedOut()
            if process.poll() is not None:
                # Background helpers can keep the pipes open after the CLI exits: give them a second.
                exited_at = exited_at or now
                if now - exited_at > 1:
                    break
            ready, _, _ = select.select(list(open_fds), [], [], min(deadline - now, 0.5))
            for fd in ready:
                try:
                    chunk = os.read(fd, 65_536)
                except OSError:
                    chunk = b""  # EIO: the pseudo-terminal's last writer is gone
                if not chunk:
                    open_fds.discard(fd)
                    continue
                if streams[fd]:
                    output += chunk
                if on_output:
                    on_output(chunk.decode("utf-8", "replace"))
        status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise TimedOut() from None
    finally:
        _terminate(process)
        if master is not None:
            os.close(master)
    return Result(status, output.decode("utf-8", "replace"))


@contextlib.contextmanager
def background_tty(executable: str | Path, arguments: list[str], *, env: dict[str, str | None] | None = None) -> Iterator[None]:
    """Keeps an interactive CLI running in a pseudo-terminal for the duration of the block."""
    master, slave = os.openpty()
    _set_window_size(slave)
    process = subprocess.Popen(
        [str(executable), *arguments],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment(env or {}, executable),
        cwd=tempfile.gettempdir(),
        start_new_session=True,
    )
    os.close(slave)
    _track(process)

    def drain():
        with contextlib.suppress(OSError):
            while os.read(master, 65_536):
                pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        yield
    finally:
        _terminate(process)
        reader.join(timeout=2)
        os.close(master)


def spawn_pipes(executable: str | Path, arguments: list[str], *, env: dict[str, str | None] | None = None) -> subprocess.Popen:
    process = subprocess.Popen(
        [str(executable), *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment(env or {}, executable),
        cwd=tempfile.gettempdir(),
        start_new_session=True,
    )
    _track(process)
    return process


def terminate(process: subprocess.Popen) -> None:
    _terminate(process)


def terminate_all() -> None:
    with _children_lock:
        children = list(_children)
    for process in children:
        _terminate(process)


def _track(process: subprocess.Popen) -> None:
    with _children_lock:
        _children.add(process)


def _terminate(process: subprocess.Popen) -> None:
    # Each child leads its own session, so the whole group (helpers included) goes down together.
    for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
        if process.poll() is not None:
            break
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            with contextlib.suppress(OSError):
                stream.close()
    with _children_lock:
        _children.discard(process)


def _set_window_size(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))


_located: dict[str, Path | None] = {}


def locate(name: str) -> Path | None:
    if name not in _located:
        _located[name] = _locate(name)
    return _located[name]


def _locate(name: str) -> Path | None:
    home = Path.home()
    directories = [
        home / ".local/bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        home / ".npm-global/bin",
        home / ".bun/bin",
        home / ".volta/bin",
        home / ".claude/local",
        *sorted(home.glob(".nvm/versions/node/*/bin"), key=_version_key, reverse=True),
    ]
    for directory in directories:
        candidate = directory / name
        if _is_executable(candidate):
            return candidate
    if found := shutil.which(name):
        return Path(found)
    shell = os.environ.get("SHELL") or "/bin/bash"
    with contextlib.suppress(OSError, TimedOut):
        result = run(shell, ["-lc", f"command -v {name}"], timeout=10)
        lines = result.stdout.strip().splitlines()
        if result.status == 0 and lines and _is_executable(Path(lines[-1].strip())):
            return Path(lines[-1].strip())
    if name == "claude":
        # The Claude desktop app ships its own Claude Code; use the newest one when no CLI is installed.
        bundled = sorted(home.glob(".config/Claude/claude-code/*/*/claude"), key=_version_key, reverse=True)
        return next((path for path in bundled if _is_executable(path)), None)
    return None


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _version_key(path: Path) -> tuple[int, ...]:
    for part in path.parts:
        if re.fullmatch(r"v?\d+(\.\d+)+", part):
            return tuple(int(number) for number in part.lstrip("v").split("."))
    return ()
