#!/usr/bin/env python3
"""Publish a thin context-usage bar to Herdr's Agent sidebar.

The daemon subscribes to Herdr pane events, reads local agent session artifacts,
and reports exactly one of three metadata tokens per supported pane.  Herdr owns
the colors; see CONFIG_SNIPPET below.
"""

from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, TextIO


SOURCE = "herdr-ctx-bar"
SUPPORTED_AGENTS = {"codex", "claude", "pi", "hermes"}
TOKEN_NAMES = ("ctx_green", "ctx_yellow", "ctx_red")
ROW_MARGIN = 5
DEFAULT_SIDEBAR_WIDTH = 26
MIN_BAR_WIDTH = 8
MAX_BAR_WIDTH = 80  # Herdr's metadata-token value limit.
WIDTH_POLL_SECONDS = 2.0
USAGE_POLL_SECONDS = 15.0
EVENT_SETTLE_SECONDS = 0.75
TAIL_BYTES = 1024 * 1024
DEMO_SECONDS = 5 * 60
DEMO_LEVELS = {
    "demo-green": 25.0,
    "demo-yellow": 65.0,
    "demo-red": 95.0,
}

SIDEBAR_SECTION = """[ui.sidebar.agents]
row_gap = 0
rows = [
  ["state_icon", "machine", "workspace", "tab"],
  ["agent"],
  [
    { token = "$ctx_green", fg = "#3fb950" },
    { token = "$ctx_yellow", fg = "#d29922" },
    { token = "$ctx_red", fg = "#f85149" },
  ],
]
"""

CONFIG_SNIPPET = f"""# Merge this section into ~/.config/herdr/config.toml, then run:
#   herdr server reload-config
{SIDEBAR_SECTION}"""


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, value))


def usage_percent(used: int | float, window: int | float) -> float | None:
    if not finite_positive(window) or not math.isfinite(float(used)) or used < 0:
        return None
    return clamp_percent(float(used) * 100.0 / float(window))


def color_for_percent(percent: float) -> str:
    if percent >= 80:
        return "ctx_red"
    if percent >= 50:
        return "ctx_yellow"
    return "ctx_green"


def render_bar(percent: float, width: int) -> str:
    """Use a heavy used segment and a thin remaining track.

    The glyphs stay within one terminal row and visually form a roughly
    three-pixel-high line in common terminal fonts.
    """
    width = max(MIN_BAR_WIDTH, min(MAX_BAR_WIDTH, int(width)))
    filled = max(0, min(width, round(width * clamp_percent(percent) / 100.0)))
    return "━" * filled + "─" * (width - filled)


def finite_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def read_tail_lines(path: str | Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
        if size > max_bytes:
            data = data.split(b"\n", 1)[-1]
        return data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def json_lines_reverse(path: str | Path) -> Iterable[dict[str, Any]]:
    for raw in reversed(read_tail_lines(path)):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


def session_info(pane: dict[str, Any]) -> tuple[str, str, str]:
    session = pane.get("agent_session") or {}
    agent = str(pane.get("agent") or session.get("agent") or "").lower()
    return agent, str(session.get("kind") or ""), str(session.get("value") or "")


def newest(paths: Iterable[str]) -> str | None:
    candidates = []
    for path in paths:
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            pass
    return max(candidates, default=(0, None))[1]


def mtime_or_zero(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def read_first_json(path: str | Path) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.readline(1024 * 1024)
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def resolve_codex_path(session_id: str, cwd: str) -> str | None:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    if session_id:
        direct = newest(
            glob.iglob(
                str(root / "**" / f"rollout-*-{session_id}.jsonl"), recursive=True
            )
        )
        if direct:
            return direct
    # Safe fallback: inspect newest rollouts and require an exact cwd match.
    paths = sorted(
        glob.iglob(str(root / "**" / "rollout-*.jsonl"), recursive=True),
        key=mtime_or_zero,
        reverse=True,
    )[:100]
    for path in paths:
        meta = read_first_json(path)
        if not meta:
            continue
        payload = meta.get("payload") or {}
        if session_id and session_id in (payload.get("id"), payload.get("session_id")):
            return path
        if cwd and os.path.normpath(str(payload.get("cwd") or "")) == os.path.normpath(
            cwd
        ):
            return path
    return None


def collect_codex(pane: dict[str, Any]) -> tuple[int, int] | None:
    _, _, session_id = session_info(pane)
    path = resolve_codex_path(
        session_id, str(pane.get("foreground_cwd") or pane.get("cwd") or "")
    )
    if not path:
        return None
    for entry in json_lines_reverse(path):
        payload = entry.get("payload") or {}
        if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        usage = info.get("last_token_usage") or {}
        used = usage.get("total_tokens", usage.get("input_tokens"))
        window = info.get("model_context_window")
        if finite_positive(used) and finite_positive(window):
            return int(float(used)), int(float(window))
    return None


CLAUDE_WINDOWS = (
    ("claude-sonnet-5", 1_000_000),
    ("claude-opus-5", 1_000_000),
    ("claude-fable-5", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-opus-4-7", 1_000_000),
    ("claude-opus-4-6", 1_000_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-sonnet-4-5", 1_000_000),
    ("claude-sonnet-4", 1_000_000),
    ("claude-haiku-4-5", 200_000),
    ("claude-3-5-haiku", 200_000),
    ("claude-opus-4-5", 200_000),
    ("claude-opus-4-1", 200_000),
)


def claude_window(model: str) -> int | None:
    normalized = model.lower().removesuffix("[1m]")
    for fragment, window in sorted(
        CLAUDE_WINDOWS, key=lambda row: len(row[0]), reverse=True
    ):
        if fragment in normalized:
            return window
    return None


def resolve_claude_path(session_id: str) -> str | None:
    if not session_id:
        return None
    root = Path(
        os.environ.get("CLAUDE_PROJECTS_ROOT", Path.home() / ".claude" / "projects")
    )
    return newest(glob.iglob(str(root / "*" / f"{session_id}.jsonl")))


def collect_claude(pane: dict[str, Any]) -> tuple[int, int] | None:
    _, _, session_id = session_info(pane)
    path = resolve_claude_path(session_id)
    if not path:
        return None
    compact_post_tokens = None
    for entry in json_lines_reverse(path):
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            value = (entry.get("compactMetadata") or {}).get("postTokens")
            if finite_positive(value):
                compact_post_tokens = int(float(value))
            continue
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        usage = message.get("usage") or {}
        used = sum(
            int(float(usage.get(key) or 0))
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        window = claude_window(str(message.get("model") or ""))
        if compact_post_tokens and window:
            return compact_post_tokens, window
        if used > 0 and window:
            return used, window
    return None


def pi_model_windows(session_path: str) -> dict[str, int]:
    path = Path(session_path).expanduser()
    roots: list[Path] = []
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured:
        roots.append(Path(configured).expanduser())
    try:
        sessions = path.parent.parent
        if sessions.name == "sessions":
            roots.append(sessions.parent)
    except IndexError:
        pass
    roots.append(Path.home() / ".pi" / "agent")
    windows: dict[str, int] = {}
    for root in reversed(list(dict.fromkeys(roots))):
        try:
            stored = json.loads((root / "models-store.json").read_text())
            for provider, catalog in stored.items():
                for model in (catalog or {}).get("models", []):
                    set_model_window(
                        windows, provider, model.get("id"), model.get("contextWindow")
                    )
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
        try:
            configured_models = json.loads((root / "models.json").read_text())
            for provider, definition in (
                configured_models.get("providers") or {}
            ).items():
                for model in (definition or {}).get("models", []):
                    set_model_window(
                        windows,
                        provider,
                        model.get("id"),
                        model.get("contextWindow", 128_000),
                    )
                for model_id, override in (
                    (definition or {}).get("modelOverrides") or {}
                ).items():
                    set_model_window(
                        windows,
                        provider,
                        model_id,
                        (override or {}).get("contextWindow"),
                    )
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
    return windows


def set_model_window(
    windows: dict[str, int], provider: Any, model: Any, window: Any
) -> None:
    if not model or not finite_positive(window):
        return
    windows[str(model)] = int(float(window))
    if provider:
        windows[f"{provider}/{model}"] = int(float(window))


def resolve_pi_path(pane: dict[str, Any]) -> str | None:
    _, kind, value = session_info(pane)
    if (
        kind == "path"
        and value.endswith(".jsonl")
        and Path(value).expanduser().is_file()
    ):
        return str(Path(value).expanduser())
    cwd = str(pane.get("foreground_cwd") or pane.get("cwd") or "")
    root = (
        Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
        / "sessions"
    )
    candidates = sorted(
        glob.iglob(str(root / "**" / "*.jsonl"), recursive=True),
        key=mtime_or_zero,
        reverse=True,
    )[:100]
    if not cwd:
        return None
    for path in candidates:
        for entry in read_tail_lines(path, 32 * 1024)[:3]:
            try:
                obj = json.loads(entry)
            except json.JSONDecodeError:
                continue
            if os.path.normpath(str(obj.get("cwd") or "")) == os.path.normpath(cwd):
                return path
    return None


def collect_pi(pane: dict[str, Any]) -> tuple[int, int] | None:
    path = resolve_pi_path(pane)
    if not path:
        return None
    windows = pi_model_windows(path)
    for entry in json_lines_reverse(path):
        if entry.get("type") == "compaction":
            return None  # wait for the first real post-compaction response
        message = entry.get("message") or {}
        if message.get("role") != "assistant" or message.get("stopReason") in (
            "aborted",
            "error",
        ):
            continue
        snapshot = message.get("contextSnapshot") or {}
        usage = message.get("usage") or {}
        used = snapshot.get("promptTokens") or usage.get("totalTokens")
        if not finite_positive(used):
            used = sum(
                float(usage.get(key) or 0)
                for key in ("input", "output", "cacheRead", "cacheWrite")
            )
        provider, model = (
            str(message.get("provider") or ""),
            str(message.get("model") or ""),
        )
        window = windows.get(f"{provider}/{model}") or windows.get(model)
        if finite_positive(used) and finite_positive(window):
            return int(float(used)), int(window)
    return None


def hermes_db_paths() -> Iterable[Path]:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    yield root / "state.db"
    yield from sorted((root / "profiles").glob("*/state.db"))


def rough_tokens(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return max(0, math.ceil(len(value) / 4))


def hermes_context_window(model: str, model_config: Any) -> int | None:
    try:
        config = (
            json.loads(model_config or "{}")
            if isinstance(model_config, str)
            else (model_config or {})
        )
    except json.JSONDecodeError:
        config = {}
    for key in ("context_length", "contextWindow", "context_window"):
        if finite_positive(config.get(key)):
            return int(float(config[key]))
    # Hermes' own config uses model.context_length. Parse just that scalar to
    # keep this plugin dependency-free; unknown windows fail closed.
    config_path = (
        Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml"
    )
    try:
        in_model = False
        for raw in config_path.read_text().splitlines():
            stripped = raw.strip()
            if not raw.startswith((" ", "\t")):
                in_model = stripped == "model:"
            elif in_model and stripped.startswith("context_length:"):
                value = (
                    stripped.split(":", 1)[1].split("#", 1)[0].strip().replace("_", "")
                )
                if finite_positive(value):
                    return int(float(value))
    except OSError:
        pass
    # Match Hermes' own dependency-free last resort for models whose provider
    # metadata is unavailable. Hermes runs the same session at this 256K
    # fallback, so the meter and its compression guard stay aligned.
    return claude_window(model) or 256_000


def collect_hermes(pane: dict[str, Any]) -> tuple[int, int] | None:
    _, _, session_id = session_info(pane)
    if not session_id:
        return None
    for db_path in hermes_db_paths():
        if not db_path.is_file():
            continue
        connection = None
        try:
            uri = f"file:{db_path}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0.1)
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                "SELECT model, model_config, COALESCE(system_prompt, "
                "(SELECT prompt FROM system_prompts WHERE hash = sessions.system_prompt_hash)) "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                continue
            model, model_config, system_prompt = row
            messages = connection.execute(
                "SELECT token_count, content, tool_calls, reasoning, reasoning_content "
                "FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
                (session_id,),
            ).fetchall()
            used = rough_tokens(system_prompt)
            for (
                token_count,
                content,
                tool_calls,
                reasoning,
                reasoning_content,
            ) in messages:
                if finite_positive(token_count):
                    used += int(token_count)
                else:
                    used += sum(
                        rough_tokens(value)
                        for value in (content, tool_calls, reasoning, reasoning_content)
                    )
            window = hermes_context_window(str(model or ""), model_config)
            if used > 0 and window:
                return used, window
        except (sqlite3.Error, OSError):
            continue
        finally:
            if connection is not None:
                connection.close()
    return None


COLLECTORS = {
    "codex": collect_codex,
    "claude": collect_claude,
    "pi": collect_pi,
    "hermes": collect_hermes,
}


def collect_percent(pane: dict[str, Any]) -> float | None:
    agent, _, _ = session_info(pane)
    collector = COLLECTORS.get(agent)
    if not collector:
        return None
    usage = collector(pane)
    return usage_percent(*usage) if usage else None


def state_dir() -> Path:
    value = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    path = (
        Path(value)
        if value
        else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / SOURCE
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def demo_overrides_path() -> Path:
    return state_dir() / "demo-overrides.json"


def read_demo_overrides(now: float | None = None) -> dict[str, float]:
    """Return unexpired pane previews as pane_id -> percentage."""
    current_time = time.time() if now is None else now
    try:
        stored = json.loads(demo_overrides_path().read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(stored, dict):
        return {}
    overrides: dict[str, float] = {}
    for pane_id, entry in stored.items():
        if not isinstance(pane_id, str) or not isinstance(entry, dict):
            continue
        percent = entry.get("percent")
        expires_at = entry.get("expires_at")
        try:
            percent_value = float(percent)
            expires_value = float(expires_at)
        except (TypeError, ValueError):
            continue
        if math.isfinite(percent_value) and expires_value > current_time:
            overrides[pane_id] = clamp_percent(percent_value)
    return overrides


def update_demo_override(
    pane_id: str, percent: float | None, now: float | None = None
) -> None:
    """Atomically add or clear one pane's temporary preview."""
    current_time = time.time() if now is None else now
    lock = (state_dir() / "demo-overrides.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = demo_overrides_path()
        try:
            stored = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        # Discard expired and malformed entries while the file is locked.
        live = read_demo_overrides(current_time)
        cleaned = {
            key: {
                "percent": value,
                "expires_at": float((stored.get(key) or {}).get("expires_at")),
            }
            for key, value in live.items()
        }
        if percent is None:
            cleaned.pop(pane_id, None)
        else:
            cleaned[pane_id] = {
                "percent": clamp_percent(percent),
                "expires_at": current_time + DEMO_SECONDS,
            }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".demo-overrides.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(cleaned, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    finally:
        lock.close()


def config_path() -> Path:
    configured = os.environ.get("HERDR_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "herdr" / "config.toml"


def install_sidebar_config(path: Path) -> tuple[str, Path | None]:
    """Install sidebar rows only when no custom Agent layout would be lost."""
    try:
        original = path.read_text()
    except FileNotFoundError:
        original = ""
    if all(f'"${name}"' in original for name in TOKEN_NAMES):
        return "already-installed", None
    if any(line.strip() == "[ui.sidebar.agents]" for line in original.splitlines()):
        return "custom-layout", None

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = time.time_ns() % 1_000_000
        backup = path.with_name(
            f"{path.name}.before-herdr-ctx-bar-{timestamp}-{suffix:06d}"
        )
        shutil.copy2(path, backup)

    separator = (
        ""
        if not original or original.endswith("\n\n")
        else "\n"
        if original.endswith("\n")
        else "\n\n"
    )
    updated = original + separator + SIDEBAR_SECTION
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return "installed", backup


def setup_sidebar() -> int:
    path = config_path()
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    before = subprocess.run(
        [herdr, "config", "check"], capture_output=True, text=True, check=False
    )
    if before.returncode != 0 and path.exists():
        print(
            "Context Bar did not change the config because it is already invalid:",
            file=sys.stderr,
        )
        print(before.stderr or before.stdout, file=sys.stderr, end="")
        return 1

    status, backup = install_sidebar_config(path)
    if status == "custom-layout":
        print(f"Context Bar found custom Agent rows in {path} and left them unchanged.")
        print(CONFIG_SNIPPET, end="")
        return 2
    if status == "already-installed":
        print(f"Context Bar sidebar rows are already configured in {path}.")
    else:
        print(f"Context Bar added its sidebar row to {path}.")
        if backup:
            print(f"Backup: {backup}")

    checked = subprocess.run(
        [herdr, "config", "check"], capture_output=True, text=True, check=False
    )
    if checked.returncode != 0:
        if backup:
            shutil.copy2(backup, path)
        else:
            path.unlink(missing_ok=True)
        print(
            "The updated config failed validation and was rolled back:",
            file=sys.stderr,
        )
        print(checked.stderr or checked.stdout, file=sys.stderr, end="")
        return 1

    reloaded = subprocess.run(
        [herdr, "server", "reload-config"],
        capture_output=True,
        text=True,
        check=False,
    )
    if reloaded.returncode != 0:
        print("Config is valid, but Herdr could not reload it:", file=sys.stderr)
        print(reloaded.stderr or reloaded.stdout, file=sys.stderr, end="")
        return 1
    print("Herdr configuration reloaded.")
    return 0


def lock_path(socket_path: str) -> Path:
    digest = hashlib.sha256(socket_path.encode()).hexdigest()[:12]
    return state_dir() / f"daemon-{digest}.lock"


def running_pid(socket_path: str) -> int | None:
    path = lock_path(socket_path)
    try:
        with path.open() as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
    except FileNotFoundError:
        return None
    except OSError:
        pass
    try:
        return int(path.read_text().strip()) or None
    except (OSError, ValueError):
        return None


def acquire_daemon_lock(socket_path: str) -> TextIO | None:
    """Acquire the daemon lock, then publish this process's PID.

    Opening in append mode avoids truncating the active daemon's PID before
    we know that this process owns the lock.
    """
    lock_file = lock_path(socket_path).open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


class HerdrClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(3)
            sock.connect(self.socket_path)
            request = {
                "id": f"{SOURCE}:{time.time_ns()}",
                "method": method,
                "params": params,
            }
            sock.sendall((json.dumps(request) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                data += chunk
            return json.loads(data.split(b"\n", 1)[0])
        except (OSError, ValueError):
            return None
        finally:
            sock.close()


class Watcher:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.client = HerdrClient(socket_path)
        self.subscription: socket.socket | None = None
        self.panes: dict[str, dict[str, Any]] = {}
        self.percentages: dict[str, float] = {}
        self.bar_width = DEFAULT_SIDEBAR_WIDTH - ROW_MARGIN
        self.pending_refresh_at = 0.0
        self.next_usage_poll = 0.0

    def pane_list(self) -> list[dict[str, Any]]:
        response = self.client.request("pane.list", {}) or {}
        return (response.get("result") or {}).get("panes") or []

    def refresh_panes(self) -> None:
        self.panes = {p["pane_id"]: p for p in self.pane_list() if p.get("pane_id")}

    def refresh_width(self) -> bool:
        pane_id = next(iter(self.panes), None)
        if not pane_id:
            return False
        response = self.client.request("pane.layout", {"pane_id": pane_id}) or {}
        try:
            sidebar_width = int(response["result"]["layout"]["area"]["x"])
        except (KeyError, TypeError, ValueError):
            return False
        if sidebar_width < 8:  # collapsed, hidden, or mobile
            return False
        width = max(MIN_BAR_WIDTH, min(MAX_BAR_WIDTH, sidebar_width - ROW_MARGIN))
        if width == self.bar_width:
            return False
        self.bar_width = width
        return True

    def publish(self, pane_id: str, percent: float | None) -> None:
        tokens: dict[str, str | None] = {name: None for name in TOKEN_NAMES}
        if percent is not None:
            tokens[color_for_percent(percent)] = render_bar(percent, self.bar_width)
        self.client.request(
            "pane.report_metadata",
            {
                "pane_id": pane_id,
                "source": SOURCE,
                "seq": time.time_ns(),
                "tokens": tokens,
            },
        )

    def refresh_usage(self) -> None:
        self.refresh_panes()
        self.refresh_width()
        demo_overrides = read_demo_overrides()
        live_supported: set[str] = set()
        for pane_id, pane in self.panes.items():
            agent, _, _ = session_info(pane)
            if agent not in SUPPORTED_AGENTS:
                continue
            live_supported.add(pane_id)
            percent = demo_overrides.get(pane_id)
            if percent is None:
                percent = collect_percent(pane)
            self.publish(pane_id, percent)
            if percent is None:
                self.percentages.pop(pane_id, None)
            else:
                self.percentages[pane_id] = percent
        for pane_id in set(self.percentages) - live_supported:
            self.publish(pane_id, None)
            self.percentages.pop(pane_id, None)
        self.next_usage_poll = time.monotonic() + USAGE_POLL_SECONDS
        self.pending_refresh_at = 0.0

    def rerender(self) -> None:
        for pane_id, percent in self.percentages.items():
            self.publish(pane_id, percent)

    def schedule_refresh(self) -> None:
        deadline = time.monotonic() + EVENT_SETTLE_SECONDS
        if not self.pending_refresh_at or deadline < self.pending_refresh_at:
            self.pending_refresh_at = deadline

    def run(self) -> None:
        self.refresh_usage()
        self.subscription = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.subscription.connect(self.socket_path)
        self.subscription.settimeout(WIDTH_POLL_SECONDS)
        subscriptions = [
            {"type": event}
            for event in (
                "pane.created",
                "pane.closed",
                "pane.focused",
                "pane.agent_detected",
                "pane.updated",
                "layout.updated",
            )
        ]
        request = {
            "id": f"{SOURCE}:subscribe",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        self.subscription.sendall((json.dumps(request) + "\n").encode())
        buffer = b""
        while True:
            try:
                chunk = self.subscription.recv(65536)
                if not chunk:
                    self.subscription.close()
                    self.subscription = None
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    data = message.get("data") or {}
                    kind = str(data.get("type") or message.get("event") or "")
                    if kind in ("layout_updated", "layout.updated"):
                        self.refresh_panes()
                        if self.refresh_width():
                            self.rerender()
                    elif kind:
                        self.schedule_refresh()
            except socket.timeout:
                pass
            now = time.monotonic()
            if self.pending_refresh_at and now >= self.pending_refresh_at:
                self.refresh_usage()
            elif now >= self.next_usage_poll:
                self.refresh_usage()
            else:
                self.refresh_panes()
                if self.refresh_width():
                    self.rerender()


def socket_path() -> str | None:
    value = os.environ.get("HERDR_SOCKET_PATH", "").strip()
    return value if value and os.path.exists(value) else None


def run_daemon(path: str) -> int:
    devnull = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(devnull, descriptor)
    lock_file = acquire_daemon_lock(path)
    if lock_file is None:
        return 0
    try:
        Watcher(path).run()
    except (OSError, KeyboardInterrupt):
        pass
    return 0


def ensure(path: str, quiet: bool = False) -> int:
    if running_pid(path):
        if not quiet:
            print("Context Bar daemon is already running.")
        return 0
    devnull = os.open(os.devnull, os.O_RDWR)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "daemon"],
        stdin=devnull,
        stdout=devnull,
        stderr=devnull,
        start_new_session=True,
    )
    os.close(devnull)
    if not quiet:
        print("Context Bar daemon started.")
    return 0


def stop(path: str) -> None:
    pid = running_pid(path)
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and running_pid(path):
        time.sleep(0.05)


def run_demo(path: str, command: str) -> int:
    pane_id = os.environ.get("HERDR_PANE_ID", "").strip()
    if not pane_id:
        print(
            "Context Bar preview needs a focused Herdr pane. Focus one and invoke the action again.",
            file=sys.stderr,
        )
        return 1
    percent = DEMO_LEVELS.get(command)
    if command != "demo-clear" and percent is None:
        return 2
    update_demo_override(pane_id, percent)
    watcher = Watcher(path)
    watcher.refresh_usage()
    ensure(path, quiet=True)
    if percent is None:
        print(f"Context Bar preview cleared for {pane_id}; live usage restored.")
    else:
        print(
            f"Context Bar preview set to {percent:g}% for {pane_id}; "
            f"it expires in {DEMO_SECONDS // 60} minutes."
        )
    return 0


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "ensure"
    if command == "setup":
        result = setup_sidebar()
        path = socket_path()
        if result == 0 and path:
            ensure(path, quiet=True)
        return result
    path = socket_path()
    if not path:
        if command == "status":
            print("Context Bar daemon is not running (no Herdr socket found).")
            return 1
        print("Context Bar must be run inside a Herdr plugin context.", file=sys.stderr)
        return 1
    if command == "daemon":
        return run_daemon(path)
    if command == "ensure":
        return ensure(path)
    if command == "status":
        pid = running_pid(path)
        print(
            f"Context Bar daemon: {'running (pid ' + str(pid) + ')' if pid else 'not running'}"
        )
        print(f"Supported agents: {', '.join(sorted(SUPPORTED_AGENTS))}")
        return 0 if pid else 1
    if command == "restart":
        stop(path)
        return ensure(path)
    if command == "refresh":
        watcher = Watcher(path)
        watcher.refresh_usage()
        print("Context bars refreshed.")
        return 0
    if command in (*DEMO_LEVELS, "demo-clear"):
        return run_demo(path, command)
    print(f"Unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
