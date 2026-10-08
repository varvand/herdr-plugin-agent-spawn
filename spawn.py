#!/usr/bin/env python3
"""Open several Herdr agent panes from one prompt.

The popup is a small filterable list: type "claude 3" or "claud 4" and that
many panes start in a new tab, in the directory you were focused on.
"""

import fcntl
import json
import os
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

PLUGIN_ID = "local.agent-spawn"
MAX_COUNT = 8
SHELLS = {"zsh", "bash", "fish", "sh", "nu", "dash", "pwsh", "powershell"}

# Kind is Herdr's canonical agent id. The label is what the popup shows.
CATALOG = (
    ("claude", "Claude Code"),
    ("codex", "Codex"),
    ("grok", "Grok"),
    ("agy", "Antigravity"),
    ("gemini", "Gemini"),
    ("cursor", "Cursor"),
    ("opencode", "OpenCode"),
    ("copilot", "Copilot"),
    ("droid", "Droid"),
    ("pi", "Pi"),
    ("omp", "OMP"),
    ("kimi", "Kimi"),
    ("kiro", "Kiro"),
    ("amp", "Amp"),
    ("hermes", "Hermes"),
    ("kilo", "Kilo"),
    ("qwen", "Qwen"),
    ("letta", "Letta"),
    ("devin", "Devin"),
    ("mastracode", "Mastra Code"),
    ("cline", "Cline"),
    ("qodercli", "Qoder"),
    ("maki", "Maki"),
    ("muse", "Muse"),
)

FG = "\033[38;2;192;202;245m"
MUTED = "\033[38;2;86;95;137m"
ACCENT = "\033[38;2;122;162;247m"
MAUVE = "\033[38;2;187;154;247m"
GREEN = "\033[38;2;158;206;106m"
YELLOW = "\033[38;2;224;175;104m"
RED = "\033[38;2;247;118;142m"
SEL = "\033[48;2;51;70;124m"
BOLD = "\033[1m"
RESET = "\033[0m"


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code or "error"
        self.message = message or self.code


def herdr_bin():
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"


def herdr(*args, timeout=20):
    proc = subprocess.run(
        [herdr_bin(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    payload = None
    for stream in (proc.stdout, proc.stderr):
        text = (stream or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
            break
        except json.JSONDecodeError:
            continue
    if proc.returncode != 0:
        err = (payload or {}).get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            raise HerdrError(str(err.get("code") or ""), str(err.get("message") or ""))
        detail = (proc.stderr or proc.stdout or "").strip()
        raise HerdrError("error", detail or f"herdr exited {proc.returncode}")
    if payload is None:
        raise HerdrError("error", "herdr returned no JSON")
    return payload


def log(message):
    try:
        path = os.environ.get("HERDR_PLUGIN_STATE_DIR")
        if not path:
            path = os.path.expanduser("~/.local/state/herdr/plugins/local.agent-spawn")
        os.makedirs(path, exist_ok=True)
        file = os.path.join(path, "spawn.log")
        if os.path.exists(file) and os.path.getsize(file) > 200_000:
            os.remove(file)
        with open(file, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%H:%M:%S ") + message + "\n")
    except OSError:
        pass


def installed_agents():
    found = []
    for kind, label in CATALOG:
        if shutil.which(kind):
            found.append((kind, label))
    return found


def levenshtein(left, right):
    if abs(len(left) - len(right)) > 2:
        return 3
    prev = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        curr = [i]
        for j, other in enumerate(right, 1):
            curr.append(min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (char != other),
            ))
        prev = curr
    return prev[-1]


def subsequence(needle, haystack):
    pos = 0
    for char in needle:
        pos = haystack.find(char, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def score(query, kind, label):
    if not query:
        return 1
    folded = query.casefold()
    targets = (kind.casefold(), label.casefold())
    if folded in targets:
        return 100
    if any(target.startswith(folded) for target in targets):
        return 80
    if any(folded in target for target in targets):
        return 60
    if any(subsequence(folded, target) for target in targets):
        return 40
    limit = 1 if len(folded) < 6 else 2
    if any(levenshtein(folded, target) <= limit for target in targets):
        return 30
    return 0


def rank(query, agents):
    scored = []
    for index, (kind, label) in enumerate(agents):
        value = score(query, kind, label)
        if value:
            scored.append((value, -index, kind, label))
    scored.sort(reverse=True)
    return [(kind, label) for _, _, kind, label in scored]


def parse_query(text):
    """Split 'claude 3' or '4 claud' into an agent token and a count.

    The count defaults to 1. More than one number uses the last one.
    """
    count = None
    words = []
    for part in text.split():
        if part.isdigit():
            count = int(part)
        else:
            words.append(part)
    return " ".join(words), 1 if count is None else count


def grid_shape(count):
    """Return per-column row counts for a roughly even tiling."""
    cols = 1
    while cols * cols < count:
        cols += 1
    base, extra = divmod(count, cols)
    return [base + (1 if index < extra else 0) for index in range(cols)]


def resolve_context(workspace=None, cwd=None):
    workspace = workspace or os.environ.get("HERDR_SPAWN_WORKSPACE") or os.environ.get("HERDR_ACTIVE_WORKSPACE_ID")
    cwd = cwd or os.environ.get("HERDR_SPAWN_CWD") or os.environ.get("HERDR_ACTIVE_PANE_CWD")
    if workspace and cwd:
        return workspace, cwd
    snap = herdr("api", "snapshot")["result"]["snapshot"]
    if not workspace:
        workspace = snap.get("focused_workspace_id") or ""
    if not cwd:
        focused = snap.get("focused_pane_id")
        for pane in snap.get("panes") or []:
            if pane.get("pane_id") == focused:
                cwd = pane.get("cwd") or pane.get("foreground_cwd") or ""
                break
    return workspace or "", cwd or ""


def workspace_label(workspace_id):
    snap = herdr("api", "snapshot")["result"]["snapshot"]
    for workspace in snap.get("workspaces") or []:
        if workspace.get("workspace_id") == workspace_id:
            return str(workspace.get("label") or workspace_id)
    return workspace_id


def shell_ready(pane_id):
    info = herdr("pane", "process-info", "--pane", pane_id)["result"]["process_info"]
    procs = info.get("foreground_processes") or []
    if len(procs) != 1:
        return False
    name = str(procs[0].get("name") or "").casefold()
    argv0 = str(procs[0].get("argv0") or "").casefold().lstrip("-")
    return name in SHELLS or argv0 in SHELLS


def wait_for_shells(pane_ids, timeout=12):
    deadline = time.monotonic() + timeout
    pending = set(pane_ids)
    while pending and time.monotonic() < deadline:
        for pane_id in list(pending):
            try:
                if shell_ready(pane_id):
                    pending.discard(pane_id)
            except HerdrError:
                pass
        if pending:
            time.sleep(0.15)
    return pending


def name_taken(err):
    text = f"{err.code} {err.message}".casefold()
    return any(word in text for word in ("already", "taken", "in use", "duplicate", "unique")) and "name" in text


def start_agent(kind, pane_id, used, lock):
    number = 1
    while number < 40:
        with lock:
            while f"{kind}-{number}" in used:
                number += 1
            name = f"{kind}-{number}"
            used.add(name)
        if len(name) > 32:
            raise HerdrError("name", "agent name is too long")
        last = None
        for attempt in range(3):
            try:
                herdr(
                    "agent", "start", name,
                    "--kind", kind,
                    "--pane", pane_id,
                    "--timeout", "45000",
                    timeout=60,
                )
                return name, "ready"
            except HerdrError as err:
                last = err
                if err.code == "agent_not_ready":
                    return name, "needs you"
                if name_taken(err):
                    break
                time.sleep(0.4)
        else:
            with lock:
                used.discard(name)
            raise last
        number += 1
    raise HerdrError("name", "no free agent name")


def split_pane(pane_id, direction, ratio):
    result = herdr(
        "pane", "split", pane_id,
        "--direction", direction,
        "--ratio", f"{ratio:.4f}",
        "--no-focus",
        timeout=20,
    )
    return result["result"]["pane"]["pane_id"]


def tile(root, count):
    """Split root into count panes. root stays the top-left pane."""
    shape = grid_shape(count)
    columns = [root]
    current = root
    for index in range(len(shape) - 1):
        current = split_pane(current, "right", 1 / (len(shape) - index))
        columns.append(current)
    panes = []
    for column, rows in zip(columns, shape):
        cells = [column]
        current = column
        for index in range(rows - 1):
            current = split_pane(current, "down", 1 / (rows - index))
            cells.append(current)
        panes.extend(cells)
    return panes


def spawn_agents(kind, count, workspace, cwd, focus=True, on_status=None):
    def status(text):
        log(text)
        if on_status:
            on_status(text)

    label = f"{kind} ×{count}"
    status(f"tab {label}")
    args = ["tab", "create", "--workspace", workspace, "--label", label, "--no-focus"]
    if cwd:
        args += ["--cwd", cwd]
    created = herdr(*args, timeout=20)
    tab_id = created["result"]["tab"]["tab_id"]
    root = created["result"]["root_pane"]["pane_id"]
    try:
        status(f"tiling {count}")
        panes = tile(root, count)
    except Exception:
        try:
            herdr("tab", "close", tab_id, timeout=20)
        except HerdrError as err:
            log(f"close after tile failure: {err.message}")
        raise

    status("waiting for shells")
    pending = wait_for_shells(panes)
    if pending:
        log(f"shells still starting: {' '.join(sorted(pending))}")

    results = [None] * len(panes)
    used = set()
    lock = threading.Lock()
    errors = []

    def launch(index, pane_id):
        try:
            name, state = start_agent(kind, pane_id, used, lock)
            results[index] = (name, state)
            status(f"{name}  {state}")
        except (HerdrError, subprocess.TimeoutExpired) as err:
            message = getattr(err, "message", None) or str(err)
            results[index] = (f"pane {index + 1}", message)
            errors.append(message)
            status(f"pane {index + 1}  {message}")

    threads = []
    for index, pane_id in enumerate(panes):
        thread = threading.Thread(target=launch, args=(index, pane_id), daemon=True)
        threads.append(thread)
        thread.start()
        time.sleep(0.12)
    for thread in threads:
        thread.join()

    if focus:
        try:
            herdr("tab", "focus", tab_id, timeout=20)
        except HerdrError as err:
            log(f"focus failed: {err.message}")
    return {"tab_id": tab_id, "results": results, "errors": errors}


def open_popup():
    workspace, cwd = resolve_context()
    command = [
        herdr_bin(), "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", "spawn",
    ]
    if workspace:
        command += ["--env", f"HERDR_SPAWN_WORKSPACE={workspace}"]
    if cwd:
        command += ["--env", f"HERDR_SPAWN_CWD={cwd}"]
    subprocess.check_call(command)


def paint(lines, width, height):
    frame = ["\033[H\033[2J"]
    for row, line in enumerate(lines[:height]):
        frame.append(f"\033[{row + 1};1H\033[K{line}")
    sys.stdout.write("".join(frame))
    sys.stdout.flush()


def place_cursor(row, col):
    sys.stdout.write(f"\033[{row};{col}H")
    sys.stdout.flush()


def read_key():
    fd = sys.stdin.fileno()
    first = os.read(fd, 1)
    if not first:
        return "eof"
    if first == b"\x1b":
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            seq = bytearray(first)
            if select.select([sys.stdin], [], [], 0.04)[0]:
                while len(seq) < 6:
                    try:
                        nxt = os.read(fd, 1)
                    except BlockingIOError:
                        break
                    if not nxt:
                        break
                    seq += nxt
                    if not select.select([sys.stdin], [], [], 0.01)[0]:
                        break
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        if seq == b"\x1b":
            return "esc"
        text = seq.decode("ascii", "replace")
        mapping = {
            "\x1b[A": "up", "\x1bOA": "up",
            "\x1b[B": "down", "\x1bOB": "down",
            "\x1b[C": "right", "\x1bOC": "right",
            "\x1b[D": "left", "\x1bOD": "left",
            "\x1b[H": "home", "\x1bOH": "home",
            "\x1b[F": "end", "\x1bOF": "end",
            "\x1b[3~": "delete",
        }
        return mapping.get(text)
    if first in (b"\r", b"\n"):
        return "enter"
    if first in (b"\x7f", b"\x08"):
        return "backspace"
    if first == b"\x15":
        return "clear"
    if first == b"\x17":
        return "word"
    if first == b"\x03":
        return "ctrl-c"
    if first == b"\x01":
        return "home"
    if first == b"\x05":
        return "end"
    if first[0] < 32:
        return None
    buf = bytearray(first)
    extra = 0
    if first[0] >= 0xF0:
        extra = 3
    elif first[0] >= 0xE0:
        extra = 2
    elif first[0] >= 0xC0:
        extra = 1
    while extra and select.select([sys.stdin], [], [], 0.02)[0]:
        buf += os.read(sys.stdin.fileno(), 1)
        extra -= 1
    return buf.decode("utf-8", "replace")


def edit(query, cursor, key):
    if key == "backspace":
        if cursor:
            query = query[: cursor - 1] + query[cursor:]
            cursor -= 1
    elif key == "delete":
        query = query[:cursor] + query[cursor + 1 :]
    elif key == "clear":
        query, cursor = "", 0
    elif key == "word":
        cut = query.rfind(" ", 0, max(cursor - 1, 0))
        cut = 0 if cut < 0 else cut + 1
        query = query[:cut] + query[cursor:]
        cursor = cut
    elif key == "left":
        cursor = max(0, cursor - 1)
    elif key == "right":
        cursor = min(len(query), cursor + 1)
    elif key == "home":
        cursor = 0
    elif key == "end":
        cursor = len(query)
    elif isinstance(key, str) and len(key) == 1 and key.isprintable():
        query = query[:cursor] + key + query[cursor:]
        cursor += 1
    return query, cursor


def picker(agents, where):
    query = ""
    cursor = 0
    selection = 0
    notice = ""
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\033[?25h")
        while True:
            size = shutil.get_terminal_size((62, 15))
            width, height = size.columns, size.lines
            token, count = parse_query(query)
            matches = rank(token, agents)
            if matches:
                selection = max(0, min(selection, len(matches) - 1))
            else:
                selection = 0
            lines = ["", f"  {BOLD}{FG}spawn agents{RESET}", ""]
            prompt = f"  {ACCENT}spawn ›{RESET} {FG}{query}{RESET}"
            if matches and 1 <= count <= MAX_COUNT:
                prompt += f"  {MUTED}×{count}{RESET}"
            lines.append(prompt)
            lines.append("")
            list_room = max(1, height - 8)
            start = 0
            if matches and selection >= list_room:
                start = selection - list_room + 1
            visible = matches[start : start + list_room]
            if not agents:
                lines.append(f"  {YELLOW}no coding agents on PATH{RESET}")
            elif not matches:
                lines.append(f"  {YELLOW}nothing matches{RESET}")
            else:
                for offset, (kind, label) in enumerate(visible):
                    index = start + offset
                    chosen = index == selection
                    pointer = "▸" if chosen else " "
                    body = f"  {pointer} {kind:<12} {label}"
                    if chosen:
                        pad = " " * max(0, width - len(body))
                        lines.append(f"{SEL}{FG}{body}{pad}{RESET}")
                    else:
                        lines.append(
                            f"{FG}  {pointer} {ACCENT}{kind:<12}{RESET} {MUTED}{label}{RESET}"
                        )
            while len(lines) < height - 2:
                lines.append("")
            problem = notice
            if count < 1 or count > MAX_COUNT:
                problem = f"use 1–{MAX_COUNT} panes"
            footer = f"  {MUTED}enter opens   ·   ↑↓ moves   ·   esc closes{RESET}"
            place = f"  {MUTED}new tab in {where}   ·   1–{MAX_COUNT} panes{RESET}"
            if problem:
                place = f"  {YELLOW}{problem}{RESET}"
            lines.append(footer)
            lines.append(place)
            paint(lines, width, height)
            # Cursor sits on the input row, after the prompt and typed text.
            place_cursor(4, 11 + cursor)
            key = read_key()
            notice = ""
            if key in ("esc", "ctrl-c", "eof"):
                return None
            if key == "up":
                selection = max(0, selection - 1)
                continue
            if key == "down":
                selection = min(max(len(matches) - 1, 0), selection + 1)
                continue
            if key == "enter":
                if not matches:
                    notice = "type an agent, for example claude 3"
                    continue
                if count < 1 or count > MAX_COUNT:
                    notice = f"use 1–{MAX_COUNT} panes"
                    continue
                kind, label = matches[selection]
                return kind, label, count
            previous_query = query
            query, cursor = edit(query, cursor, key)
            if query != previous_query:
                selection = 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        sys.stdout.write("\033[?25h\033[0m")
        sys.stdout.flush()


def show_lines(lines):
    size = shutil.get_terminal_size((62, 15))
    padded = [""] + [f"  {line}" for line in lines]
    paint(padded, size.columns, size.lines)


def run_picker():
    if not sys.stdin.isatty():
        print("spawn agents needs a terminal", file=sys.stderr)
        return 1
    agents = installed_agents()
    workspace, cwd = resolve_context()
    if not workspace:
        print("open a workspace first", file=sys.stderr)
        return 1
    where = workspace_label(workspace)
    folder = Path(cwd).name if cwd else where
    choice = picker(agents, folder or where)
    if not choice:
        return 0
    kind, _label, count = choice
    lines = [f"{BOLD}{FG}{kind}{RESET}  {MUTED}×{count}{RESET}", f"{MUTED}{folder}{RESET}", ""]
    show_lines(lines + [f"{MUTED}opening{RESET}"])

    def on_status(text):
        lines.append(f"{FG}{text}{RESET}")
        show_lines(lines[-8:])

    try:
        outcome = spawn_agents(kind, count, workspace, cwd, focus=True, on_status=on_status)
    except (HerdrError, subprocess.TimeoutExpired) as err:
        message = getattr(err, "message", None) or str(err)
        show_lines(lines + [f"{RED}{message}{RESET}", f"{MUTED}press any key{RESET}"])
        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            read_key()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        return 1
    if outcome["errors"]:
        show_lines(lines[-8:] + [f"{YELLOW}some panes need a look{RESET}", f"{MUTED}press any key{RESET}"])
        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            read_key()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
    return 0


def self_test():
    assert parse_query("claude 3") == ("claude", 3)
    assert parse_query("claud 4") == ("claud", 4)
    assert parse_query("4 claude") == ("claude", 4)
    assert parse_query("claude") == ("claude", 1)
    assert parse_query("") == ("", 1)
    assert grid_shape(1) == [1]
    assert grid_shape(2) == [1, 1]
    assert grid_shape(3) == [2, 1]
    assert grid_shape(4) == [2, 2]
    assert grid_shape(8) == [3, 3, 2]
    agents = [("claude", "Claude Code"), ("codex", "Codex"), ("agy", "Antigravity")]
    assert rank("claud", agents)[0][0] == "claude"
    assert rank("codx", agents)[0][0] == "codex"
    assert rank("antigravity", agents)[0][0] == "agy"
    assert rank("", agents)[0][0] == "claude"
    assert rank("zzz", agents) == []
    print("ok")
    return 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    if "--open" in argv:
        open_popup()
        return 0
    if "--run" in argv:
        index = argv.index("--run")
        kind = argv[index + 1]
        count = int(argv[index + 2])
        workspace = None
        cwd = None
        focus = "--no-focus" not in argv
        if "--workspace" in argv:
            workspace = argv[argv.index("--workspace") + 1]
        if "--cwd" in argv:
            cwd = argv[argv.index("--cwd") + 1]
        workspace, cwd = resolve_context(workspace, cwd)
        outcome = spawn_agents(kind, count, workspace, cwd, focus=focus, on_status=print)
        print(json.dumps(outcome))
        return 1 if outcome["errors"] else 0
    return run_picker()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
