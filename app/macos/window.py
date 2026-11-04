"""My Claude in its own window (macOS). Started by launcher.sh; the server itself runs separately
under the login item, so closing this window only closes the window."""
import os, sys, threading, time, urllib.request

DATA = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "my-claude")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
URL = f"http://127.0.0.1:{PORT}/p/overview"
RAISE = os.path.join(DATA, "window.raise")   # launcher touches this to bring an open window to the front

def _identity():
    """Name and icon in the menu bar, Dock and Cmd-Tab: this runs inside Python's own app bundle."""
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "My Claude"
    except Exception: pass

def _set_icon():
    try:
        from AppKit import NSApplication, NSImage
        img = NSImage.alloc().initWithContentsOfFile_(os.path.join(HERE, "AppIcon.icns"))
        if img is not None: NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception: pass

def _bring_to_front():
    try:
        from AppKit import NSApplication
        from PyObjCTools import AppHelper
        AppHelper.callAfter(lambda: NSApplication.sharedApplication().activateIgnoringOtherApps_(True))
    except Exception: pass

def _up():
    try: urllib.request.urlopen(URL, timeout=3); return True
    except Exception: return False

STARTING = """<html><body style="margin:0;height:100vh;display:grid;place-items:center;background:#141414;color:#e4e4e4;
font:15px -apple-system,sans-serif;text-align:center"><div><div style="width:56px;height:56px;border-radius:13px;background:#D97757;
color:#1a1a19;display:grid;place-items:center;font:700 30px Menlo,monospace;margin:0 auto 18px">C</div>
<div style="font-size:19px;font-weight:600">Starting My Claude…</div><div style="color:#8a8a8a;margin-top:6px">
The first start after an install reads your conversations once.</div></div></body></html>"""
FAILED = STARTING.replace("Starting My Claude…", "My Claude is taking longer than usual").replace(
    "The first start after an install reads your conversations once.", "Its log is in ~/Library/Logs/my-claude.log. Running the install command again usually fixes it.")

def main(window):
    _set_icon(); _bring_to_front()
    t0 = time.time()
    while not _up():
        if time.time() - t0 > 900: window.load_html(FAILED); break
        time.sleep(1)
    else:
        window.load_url(URL)
    last = os.path.getmtime(RAISE) if os.path.exists(RAISE) else 0
    while True:                       # a second click on the app touches RAISE instead of opening another window
        time.sleep(0.5)
        m = os.path.getmtime(RAISE) if os.path.exists(RAISE) else 0
        if m != last: last = m; _bring_to_front()

if __name__ == "__main__":
    import webview
    _identity()
    with open(os.path.join(DATA, "window.pid"), "w") as f: f.write(str(os.getpid()))
    w = webview.create_window("My Claude", html=STARTING, width=1440, height=920, min_size=(900, 600))
    webview.start(main, w)
