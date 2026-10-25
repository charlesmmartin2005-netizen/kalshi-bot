"""Opens the dashboard as a desktop app: an Edge window of its own (no tabs or
address bar) on http://127.0.0.1:8050. The Desktop and Start-menu shortcuts
that install-app.ps1 makes run this with pythonw.exe, so no console shows.

If the bot isn't running, this starts the "Kalshi bot" task (run-bot.ps1, as
at logon) and shows a small "Starting" window until the dashboard answers. If
the app window is already open, it comes to the front instead of opening a
second one. Closing the window leaves the bot running; stop-bot.ps1 stops it.
"""
import ctypes
import os
import socket
import subprocess
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8050
URL = f"http://{HOST}:{PORT}/"
TASK = "Kalshi bot"
TITLE = "Kalshi bot"  # the page's <title>, which Edge gives the app window
STARTUP_S = 120
NO_WINDOW = subprocess.CREATE_NO_WINDOW

user32 = ctypes.windll.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def up() -> bool:
    # A raw connect with a short timeout: Windows takes ~2s to refuse a
    # closed localhost port, which would delay the "Starting" window. The
    # dashboard opens its port only once it's ready to serve.
    try:
        with socket.create_connection((HOST, PORT), timeout=0.4):
            return True
    except OSError:
        return False


def app_window() -> int:
    # Edge's windows are all Chrome_WidgetWin_1; only the app window is
    # titled exactly "Kalshi bot" (a browser tab adds " - Microsoft Edge").
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


def wait_for_bot() -> bool:
    """Shows "Starting the bot…" until the dashboard answers. False if the
    person closes the window or it doesn't come up in STARTUP_S."""
    import tkinter as tk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.title("Kalshi bot: starting")
    try:
        root.iconbitmap(str(ROOT / "static" / "icon.ico"))
    except tk.TclError:
        pass
    root.configure(bg="#0B1826", padx=28, pady=22)
    root.resizable(False, False)
    tk.Label(root, text="Kalshi bot", font=("Segoe UI Semibold", 16), fg="#EAF0F6", bg="#0B1826").pack(anchor="w")
    status = tk.Label(root, text="Starting the bot", font=("Segoe UI", 11), fg="#9DB0C3", bg="#0B1826",
                      width=34, anchor="w", justify="left")
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
            status.config(text="The bot didn't start.\nSee logs\\crash.log and logs\\launcher.log.",
                          fg="#FF8F7A")
            return
        dots[0] = dots[0] % 3 + 1
        status.config(text="Starting the bot" + "." * dots[0])
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
    kernel32.CreateMutexW(None, False, "Local\\KalshiBotApp")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    if not up():
        start_bot()
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
