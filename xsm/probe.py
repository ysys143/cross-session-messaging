"""Looking at a project folder without being held by it.

A session's folder is not xsm's: it can sit under ~/Documents, which macOS guards,
or on a mount that does not answer. An open() or an opendir() there blocks in the
kernel and cannot be interrupted, and `subprocess.run(timeout=)` waits for the child
it killed, which never leaves that read. `xsm list` asked about each session's folder
(its settings files, `git rev-parse`; 42 git calls for 8 folders, measured
2026-10-02) and one stalled folder froze it for good on the Mac mini. A listing that
a person is waiting on must not wait for a folder.

- `bounded` reads with a deadline, in a thread that is left behind when time runs out;
  a timeout is "unknown", never an error.
- `git` is `git -C <folder>` with the same deadline, and a child that does not answer
  is killed and let go, not waited for.
- `quick` is for the commands that must not wait (xsm list): within it every probe
  gets at most a second, an answer for a folder is asked once (`remember`), and a
  folder that did not answer is not asked again.

Outside `quick` nothing is remembered (the MCP server runs for days, and a folder
that stalled once may answer later), and the deadlines are the ordinary ones.
"""
from __future__ import annotations

import contextlib
import subprocess
import threading

GIT_TIMEOUT = 5.0           # what a git call has always been allowed
READ_TIMEOUT = 2.0          # a file read is microseconds when the folder answers
QUICK_TIMEOUT = 1.0

_quick = None               # the deadline in force inside `quick`, else None
_memo: dict = {}
_stalled: set = set()


@contextlib.contextmanager
def quick(timeout: float | None = None):
    """Within the block: at most `timeout` seconds (QUICK_TIMEOUT) for any probe, one
    answer per question (`remember`), and no second question to a folder that did not
    answer."""
    global _quick
    if _quick is not None:
        yield
        return
    _quick = QUICK_TIMEOUT if timeout is None else timeout
    _memo.clear()
    _stalled.clear()
    try:
        yield
    finally:
        _quick = None
        _memo.clear()
        _stalled.clear()


def limit(timeout: float) -> float:
    return timeout if _quick is None else min(timeout, _quick)


def remember(key, compute):
    """`compute()`, once per `key` inside `quick`, every time outside it."""
    if _quick is None:
        return compute()
    if key not in _memo:
        _memo[key] = compute()
    return _memo[key]


def _gave_up(folder) -> None:
    if _quick is not None and folder is not None:
        _stalled.add(folder)


def bounded(fn, *args, folder: str | None = None, timeout: float | None = None, default=None):
    """`fn(*args)`, or `default` when it raises or takes longer than the deadline. A
    read in a stalled folder cannot be interrupted, so it runs in a daemon thread that
    is left behind. `folder` names what is being asked about: one that did not answer
    inside `quick` is not asked again."""
    if folder is not None and folder in _stalled:
        return default
    box: list = []

    def work():
        try:
            box.append(fn(*args))
        except Exception:               # noqa: BLE001 - what cannot be read is unknown
            box.append(default)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(limit(READ_TIMEOUT if timeout is None else timeout))
    if thread.is_alive() or not box:
        _gave_up(folder)
        return default
    return box[0]


def git(folder: str, *args: str, timeout: float | None = None) -> str | None:
    """stdout of `git -C <folder> <args>`, or None when git failed, is missing, or did
    not answer in time (the folder is then not asked again inside `quick`)."""
    if folder in _stalled:
        return None
    try:
        proc = subprocess.Popen(["git", "-C", folder] + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=limit(GIT_TIMEOUT if timeout is None else timeout))
    except subprocess.TimeoutExpired:
        proc.kill()                     # and not waited for: it may never leave the folder
        proc.stdout.close()
        _gave_up(folder)
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    return out if proc.returncode == 0 else None
