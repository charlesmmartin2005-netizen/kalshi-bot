"""Opens the dashboard as a desktop app, Kalshi Bot Pro: an Edge window of its
own (no tabs or address bar). The Desktop and Start-menu shortcuts that
install-app.ps1 makes run this with pythonw.exe, so no console shows.

On its own it opens this PC's bot at http://127.0.0.1:8050. If that bot isn't
running, it starts the "Kalshi bot" task (run-bot.ps1, as at logon) and shows
a small "Starting" window until the dashboard answers. Closing the window
leaves the bot running; stop-bot.ps1 stops it.

With --server (install-app.ps1 -Server) it opens the server's bot (README,
"Run on a server") at http://127.0.0.1:8060, through an SSH tunnel it opens
first if there isn't a working one, with a "Connecting" window meanwhile.

Either way, if the app window is already open, it comes to the front instead
of opening a second one.
"""
import ctypes
import os
import socket
import subprocess
import sys
import time
import urllib.request
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = "--server" in sys.argv
HOST, PORT = "127.0.0.1", 8060 if SERVER else 8050
URL = f"http://{HOST}:{PORT}/"
TASK = "Kalshi bot"
SSH_HOST = "kalshi-bot"  # the Host entry in ~\.ssh\config
TITLE = "Kalshi Bot Pro"  # the page's <title>, which Edge gives the app window
STARTUP_S = 30 if SERVER else 120
NO_WINDOW = subprocess.CREATE_NO_WINDOW

user32 = ctypes.windll.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def up() -> bool:
    # A raw connect with a short timeout: Windows takes ~2s to refuse a
    # closed localhost port, which would delay the "Starting" window. The
    # dashboard opens its port only once it's ready to serve.
    try:
        with socket.create_connection((HOST, PORT), timeout=0.4):
            pass
    except OSError:
        return False
    if not SERVER:
        return True
    # A tunnel's port stays open while ssh notices its connection died (up to
    # ~90s after the PC sleeps), so ask the server's bot itself.
    try:
        with urllib.request.urlopen(URL + "api/state", timeout=3):
            return True
    except OSError:
        return False


def app_window() -> int:
    # Edge's windows are all Chrome_WidgetWin_1; only the app window is
    # titled exactly "Kalshi Bot Pro" (a browser tab adds " - Microsoft Edge").
    return user32.FindWindowW("Chrome_WidgetWin_1", TITLE)


def focus(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)


def start_bot() -> None:
    r = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        # No task (removed, or install-app.ps1 never ran): start the same
        # restart loop the task runs, in a console with no window.
        subprocess.Popen(
            ["conhost.exe", "--headless", "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ROOT / "run-bot.ps1")],
            cwd=ROOT, creationflags=NO_WINDOW)


def start_tunnel() -> None:
    # Ends a tunnel that still listens but no longer reaches the server, then
    # opens a new one: ssh with no window, which outlives this launcher.
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name = 'ssh.exe'\" | "
         f"Where-Object {{ $_.CommandLine -match '{PORT}:127\\.0\\.0\\.1:8050' }} | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True, creationflags=NO_WINDOW)
    ssh = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
    subprocess.Popen(
        [str(ssh) if ssh.exists() else "ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
         "-L", f"{PORT}:127.0.0.1:8050", SSH_HOST],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


def wait_for_bot() -> bool:
    """Shows "Starting the bot…" (with --server, "Connecting to the server…")
    until the dashboard answers. False if the person closes the window or it
    doesn't come up in STARTUP_S."""
    import tkinter as tk

    doing = "Connecting to the server" if SERVER else "Starting the bot"
    failed = ("Couldn't reach the server.\nCheck the internet connection, or run: ssh kalshi-bot" if SERVER
              else "The bot didn't start.\nSee logs\\crash.log and logs\\launcher.log.")

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.title(f"{TITLE}: {doing.lower()}")
    try:
        root.iconbitmap(str(ROOT / "static" / "icon.ico"))
    except tk.TclError:
        pass
    root.configure(bg="#0B1826", padx=28, pady=22)
    root.resizable(False, False)
    tk.Label(root, text=TITLE, font=("Segoe UI Semibold", 16), fg="#EAF0F6", bg="#0B1826").pack(anchor="w")
    status = tk.Label(root, text=doing, font=("Segoe UI", 11), fg="#9DB0C3", bg="#0B1826",
                      width=46 if SERVER else 34, anchor="w", justify="left")
    status.pack(anchor="w", pady=(6, 0))
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")

    ready = {"ok": False}
    deadline = time.monotonic() + STARTUP_S
    dots = [0]

    def check():
        if up():
            ready["ok"] = True
            root.destroy()
            return
        if time.monotonic() > deadline:
            status.config(text=failed, fg="#FF8F7A")
            return
        dots[0] = dots[0] % 3 + 1
        status.config(text=doing + "." * dots[0])
        root.after(500, check)

    root.after(300, check)
    root.mainloop()
    return ready["ok"]


def edge() -> str | None:
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as k:
                path = winreg.QueryValue(k, None)
        except OSError:
            continue
        if path and Path(path).exists():
            return path
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (p := Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe").exists():
            return str(p)
    return None


def main() -> None:
    # One launcher at a time: a second click while the bot is starting would
    # otherwise open a second window when it's up.
    kernel32.CreateMutexW(None, False, "Local\\KalshiBotProServer" if SERVER else "Local\\KalshiBotApp")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    if not up():
        start_tunnel() if SERVER else start_bot()
        if not wait_for_bot():
            return
    if hwnd := app_window():
        focus(hwnd)  # the page polls every 1.5s, so it recovers by itself
        return
    if exe := edge():
        subprocess.Popen([exe, f"--app={URL}"], creationflags=NO_WINDOW)
    else:
        os.startfile(URL)


if __name__ == "__main__":
    main()
