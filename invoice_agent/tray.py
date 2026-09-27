"""System-tray app: runs the agent in the background with an On/Off switch.

Launch with `pythonw main.py tray` (no console window) - `Invoice Agent.vbs` does this.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, OUTPUT_DIR, ROOT, Settings

LOG_FILE = DATA_DIR / "agent.log"
STATE_FILE = DATA_DIR / "tray.json"
LOCK_PORT = 47653  # a localhost port held while the tray runs, so only one copy starts
STARTUP_NAME = "Invoice Agent.vbs"

COLORS = {"on": "#2E9E4F", "off": "#8A8A8A", "busy": "#E0A100", "error": "#D13438"}


# ---------------------------------------------------------------- helpers
def redirect_output() -> None:
    """pythonw has no console, so send all output to data/agent.log."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
        LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": True}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def acquire_single_instance() -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        sock.close()
        return None
    return sock


def make_icon_image(color: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=12, fill=color)
    # a simple spreadsheet grid
    for x in (22, 42):
        d.line((x, 16, x, 50), fill="white", width=3)
    for y in (16, 28, 39, 50):
        d.line((12, y, 52, y), fill="white", width=3)
    return img


def open_path(path: Path) -> None:
    if path == OUTPUT_DIR:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        return
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening a local folder/file for the user
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_console(*args: str) -> None:
    """Open the text menu / setup wizard in a normal console window."""
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", str(ROOT / "start.bat"), *args],
                         cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen(["x-terminal-emulator", "-e", str(ROOT / "start.sh"), *args], cwd=ROOT)


# ---------------------------------------------------------------- Windows startup / shortcuts
def vbs_text(folder: str | None = None) -> str:
    """VBScript that starts the tray with pythonw (no console window)."""
    folder_expr = f'"{folder}"' if folder else \
        'CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)'
    return "\r\n".join([
        "' Starts Invoice Agent in the system tray (no window).",
        'Set sh = CreateObject("WScript.Shell")',
        f"folder = {folder_expr}",
        "sh.CurrentDirectory = folder",
        'If Not CreateObject("Scripting.FileSystemObject").FileExists(folder & "\\.venv\\Scripts\\pythonw.exe") Then',
        '    MsgBox "Please run start.bat once first to install Invoice Agent.", 48, "Invoice Agent"',
        "    WScript.Quit",
        "End If",
        'sh.Run """" & folder & "\\.venv\\Scripts\\pythonw.exe"" main.py tray", 0, False',
        "",
    ])


def startup_file() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup" / STARTUP_NAME


def autostart_enabled() -> bool:
    f = startup_file()
    return bool(f and f.exists())


def set_autostart(enabled: bool) -> None:
    f = startup_file()
    if f is None:
        return
    if enabled:
        f.write_text(vbs_text(str(ROOT)), encoding="utf-8")
    elif f.exists():
        f.unlink()


def ensure_icon_file() -> Path:
    ico = DATA_DIR / "icon.ico"
    if not ico.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        make_icon_image(COLORS["on"]).save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    return ico


def create_desktop_shortcut() -> str:
    """Put an 'Invoice Agent' icon on the Windows desktop that starts the tray app."""
    if sys.platform != "win32":
        return "Desktop shortcuts are only created on Windows."
    vbs = ROOT / STARTUP_NAME
    vbs.write_text(vbs_text(), encoding="utf-8")
    q = lambda p: str(p).replace("'", "''")  # noqa: E731 - PowerShell single-quote escaping
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\\Invoice Agent.lnk'); "
        "$lnk.TargetPath = 'wscript.exe'; "
        f"$lnk.Arguments = '\"{q(vbs)}\"'; "
        f"$lnk.WorkingDirectory = '{q(ROOT)}'; "
        f"$lnk.IconLocation = '{q(ensure_icon_file())}'; "
        "$lnk.Description = 'Invoice Agent - PDF invoices to Excel'; "
        "$lnk.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], check=True)
    return "Created 'Invoice Agent' shortcut on your desktop."


# ---------------------------------------------------------------- the tray app
class TrayApp:
    def __init__(self):
        state = load_state()
        self.enabled = bool(state.get("enabled", True))
        self.status = "Starting..."
        self.last_check = "never"
        self.sent_total = 0
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.icon = None

    # -- worker
    def worker(self) -> None:
        from .cli import MAIL_ERRORS, run_once

        while not self.stop.is_set():
            settings = Settings.load()
            forced = self.wake.is_set()
            self.wake.clear()
            if self.enabled or forced:
                if settings.missing():
                    self.set_status("error", "Setup needed - right-click > Settings")
                else:
                    self.set_status("busy", "Checking inbox...")
                    print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} check ===")
                    try:
                        counts = run_once(settings, ask=False)
                        self.last_check = datetime.now().strftime("%H:%M")
                        self.sent_total += counts["sent"]
                        if counts["sent"]:
                            self.notify(f"Sent {counts['sent']} Excel repl{'y' if counts['sent'] == 1 else 'ies'}.")
                        if counts["error"]:
                            self.notify(f"{counts['error']} email(s) had problems - see log.")
                        self.set_status("on" if self.enabled else "off",
                                        f"Last check {self.last_check}, {self.sent_total} sent")
                    except MAIL_ERRORS as e:
                        print(f"Mailbox error: {e}")
                        self.set_status("error", f"Mailbox error: {str(e)[:60]}")
                    except Exception:  # keep the background thread alive whatever happens
                        traceback.print_exc()
                        self.set_status("error", "Error - see log")
            else:
                self.set_status("off", "Paused")
            self.wake.wait(timeout=max(1, settings.poll_interval_minutes) * 60)

    # -- ui
    def set_status(self, kind: str, text: str) -> None:
        self.status = text
        if self.icon is not None:
            self.icon.icon = make_icon_image(COLORS[kind])
            self.icon.title = f"Invoice Agent - {'ON' if self.enabled else 'OFF'}\n{text}"[:127]
            self.icon.update_menu()

    def notify(self, message: str) -> None:
        try:
            if self.icon is not None and self.icon.HAS_NOTIFICATION:
                self.icon.notify(message, "Invoice Agent")
        except Exception:  # notifications are best-effort
            pass

    def toggle(self, *_):
        self.enabled = not self.enabled
        save_state({"enabled": self.enabled})
        self.set_status("on" if self.enabled else "off", "Turned on - checking now" if self.enabled else "Paused")
        if self.enabled:
            self.wake.set()

    def check_now(self, *_):
        self.wake.set()

    def toggle_autostart(self, *_):
        set_autostart(not autostart_enabled())

    def quit(self, icon, *_):
        self.stop.set()
        self.wake.set()
        icon.stop()

    def run(self) -> None:
        import pystray
        from pystray import Menu, MenuItem as Item

        menu = Menu(
            Item(lambda _: f"Status: {self.status}", None, enabled=False),
            Menu.SEPARATOR,
            Item("Agent ON (auto-reply)", self.toggle, checked=lambda _: self.enabled, default=True),
            Item("Check inbox now", self.check_now),
            Menu.SEPARATOR,
            Item("Open Excel folder", lambda *_: open_path(OUTPUT_DIR)),
            Item("Review inbox manually...", lambda *_: open_console("once")),
            Item("History...", lambda *_: open_console("history")),
            Item("View log", lambda *_: open_path(LOG_FILE)),
            Item("Settings...", lambda *_: open_console("setup")),
            Item("Start with Windows", self.toggle_autostart, checked=lambda _: autostart_enabled(),
                 visible=sys.platform == "win32"),
            Menu.SEPARATOR,
            Item("Quit", self.quit),
        )
        self.icon = pystray.Icon("invoice_agent", make_icon_image(COLORS["off"]), "Invoice Agent", menu)
        threading.Thread(target=self.worker, daemon=True).start()
        self.icon.run()


def main() -> None:
    redirect_output()
    lock = acquire_single_instance()
    if lock is None:
        print("Invoice Agent tray is already running.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if Settings.load().missing():
        open_console("setup")
    TrayApp().run()
