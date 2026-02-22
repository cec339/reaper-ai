"""File-based IPC for communicating with the REAPER Lua daemon."""

import json
import time
import uuid
from pathlib import Path

DEFAULT_QUEUE_PATH = Path(__file__).resolve().parent.parent / "queue"
DEFAULT_TIMEOUT = 10.0
POLL_INTERVAL = 0.1


def load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_queue_path() -> Path:
    cfg = load_config()
    if "queue_path" in cfg:
        return Path(cfg["queue_path"])
    return DEFAULT_QUEUE_PATH


def get_timeout() -> float:
    cfg = load_config()
    return float(cfg.get("timeout", DEFAULT_TIMEOUT))


def send_command(op: str, **kwargs) -> dict:
    """Send a command to the REAPER daemon and wait for its response."""
    queue = get_queue_path()
    in_dir = queue / "in"
    out_dir = queue / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd_id = str(uuid.uuid4())
    cmd = {"id": cmd_id, "op": op, **kwargs}

    # Write command file
    cmd_file = in_dir / f"{cmd_id}.json"
    with open(cmd_file, "w", encoding="utf-8") as f:
        json.dump(cmd, f)

    # Poll for response
    timeout = get_timeout()
    resp_file = out_dir / f"{cmd_id}.json"
    start = time.time()

    while time.time() - start < timeout:
        if resp_file.exists():
            # Small delay to ensure file is fully written
            time.sleep(0.05)
            try:
                with open(resp_file, encoding="utf-8") as f:
                    result = json.load(f)
                resp_file.unlink(missing_ok=True)
                return result
            except (json.JSONDecodeError, OSError):
                # File might still be writing
                time.sleep(0.1)
                continue
        time.sleep(POLL_INTERVAL)

    # Timeout - clean up command file if still present
    cmd_file.unlink(missing_ok=True)
    return {
        "id": cmd_id,
        "status": "error",
        "errors": [
            f"Timeout after {timeout}s waiting for REAPER response. "
            "Is the daemon running? (Actions > Run reaper_daemon.lua)"
        ],
    }
