"""Installer for reaper-ai MCP server — copies files and configures Claude Desktop."""

import json
import os
import shutil
import sys
from pathlib import Path


def _get_bundled_lua() -> Path | None:
    """Find reaper_daemon.lua — bundled in exe or in source tree."""
    if getattr(sys, "frozen", False):
        # PyInstaller bundle
        return Path(sys._MEIPASS) / "reaper_daemon.lua"
    # Dev: next to project root
    return Path(__file__).resolve().parent.parent / "reaper_daemon.lua"


def _default_reaper_scripts() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "REAPER" / "Scripts"


def _install_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "reaper-ai"


def _claude_desktop_config() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"


def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val or default


def run_install():
    print("=" * 60)
    print("  reaper-ai MCP Server Installer")
    print("=" * 60)
    print()

    # 1. Determine install directory
    install_dir = _install_dir()
    print(f"Install directory: {install_dir}")
    print()

    # 2. Get REAPER Scripts path
    default_scripts = str(_default_reaper_scripts())
    scripts_path = Path(_prompt("REAPER Scripts folder", default_scripts))

    if not scripts_path.exists():
        create = _prompt(f"  '{scripts_path}' does not exist. Create it? (y/n)", "y")
        if create.lower() in ("y", "yes"):
            scripts_path.mkdir(parents=True, exist_ok=True)
            print(f"  Created {scripts_path}")
        else:
            print("  Aborted.")
            return

    # 3. Copy exe to install dir
    install_dir.mkdir(parents=True, exist_ok=True)
    queue_path = install_dir / "queue"

    if getattr(sys, "frozen", False):
        src_exe = Path(sys.executable)
        dst_exe = install_dir / src_exe.name
        if src_exe.resolve() != dst_exe.resolve():
            print(f"\nCopying {src_exe.name} -> {install_dir}")
            shutil.copy2(src_exe, dst_exe)
        else:
            print(f"\nExe already in install directory.")
        exe_path = dst_exe
    else:
        print("\nRunning from source (not frozen exe). Skipping exe copy.")
        print("  The 'reaper-mcp' command from pip install -e . will be used.")
        exe_path = None

    # 4. Copy reaper_daemon.lua to REAPER Scripts
    lua_src = _get_bundled_lua()
    if lua_src and lua_src.exists():
        lua_dst = scripts_path / "reaper_daemon.lua"
        print(f"Copying reaper_daemon.lua -> {lua_dst}")
        shutil.copy2(lua_src, lua_dst)

        # Create config.json next to lua script so daemon finds the queue
        lua_config = scripts_path / "config.json"
        lua_cfg = {"queue_path": str(queue_path).replace("\\", "/")}
        with open(lua_config, "w", encoding="utf-8") as f:
            json.dump(lua_cfg, f, indent=2)
        print(f"Created {lua_config} (queue_path: {queue_path})")
    else:
        print("WARNING: reaper_daemon.lua not found — skipping.")
        print("  You'll need to copy it to your REAPER Scripts folder manually.")

    # 5. Create config.json next to exe / in install dir
    exe_config = install_dir / "config.json"
    exe_cfg = {"queue_path": str(queue_path).replace("\\", "/"), "timeout": 10}
    with open(exe_config, "w", encoding="utf-8") as f:
        json.dump(exe_cfg, f, indent=2)
    print(f"Created {exe_config}")

    # 6. Configure Claude Desktop
    cd_config_path = _claude_desktop_config()
    print(f"\nClaude Desktop config: {cd_config_path}")

    if exe_path:
        command_value = str(exe_path).replace("\\", "/")
    else:
        command_value = "reaper-mcp"

    if cd_config_path.exists():
        with open(cd_config_path, encoding="utf-8") as f:
            cd_config = json.load(f)
    else:
        cd_config = {}

    if "mcpServers" not in cd_config:
        cd_config["mcpServers"] = {}

    cd_config["mcpServers"]["reaper-ai"] = {"command": command_value}

    cd_config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cd_config_path, "w", encoding="utf-8") as f:
        json.dump(cd_config, f, indent=2)
    print("Added 'reaper-ai' MCP server to Claude Desktop config.")

    # 7. Done — print instructions
    print()
    print("=" * 60)
    print("  Setup complete!")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Open REAPER")
    print("  2. Actions > Show action list > Load ReaScript")
    print(f"     Browse to: {scripts_path / 'reaper_daemon.lua'}")
    print("  3. Run the action (it will say 'Daemon started' in the console)")
    print("  4. Open Claude Desktop — ask 'show me my REAPER tracks'")
    print()
    input("Press Enter to close...")
