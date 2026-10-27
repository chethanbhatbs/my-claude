"""My-Claude — interactive dashboard of your Claude installation (FastAPI replica of the Streamlit app, no Streamlit)."""
import asyncio, io, json, os, pty, re, select, shutil, signal, sqlite3, subprocess, sys, tempfile, threading, time, zipfile
from urllib.parse import quote
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HOME = Path.home()
CLAUDE = HOME / ".claude"
SKILL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SKILL_DIR / "config.json"

CAT_COLOR = {
    "Skills": "#D97757", "Sessions": "#C89968", "Connectors": "#A98DD0",
    "Plugins": "#6DAEA0", "Config": "#8FB06B", "Cache": "#8A8072", "Tasks": "#D08AA0",
}
FAMILY_COLOR = {"opus": "#A98DD0", "sonnet": "#D97757", "haiku": "#6DAEA0", "fable": "#6E8FD1"}

PAGES = ["Overview", "Skills", "MCP & Plugins", "Usage", "History", "System", "Help"]
PAGE_SLUG = {p: re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in PAGES}
SLUG_PAGE = {v: k for k, v in PAGE_SLUG.items()}
MCP_SLUG = PAGE_SLUG["MCP & Plugins"]

# old slugs -> current page (keep bookmarks/links working after the revamp)
REDIRECTS = {"token-usage": "usage", "mcp-connectors": MCP_SLUG, "plugins": MCP_SLUG,
             "extensions": MCP_SLUG, "sessions": "history", "search": "history",
             "cleanup": "overview", "config": "system", "history-cache": "history",
             "tasks-plans": "overview", "sunburst": "overview", "graph": "overview", "memory": "system"}

# eyebrow = where each page's data lives (structure as information)
EYEBROW = {
    "Overview": "~/.claude", "Skills": "~/.claude/skills",
    "MCP & Plugins": "settings.json · ~/.claude.json · ~/.claude/plugins", "Usage": "~/.claude/projects · usage",
    "History": "~/.claude/projects · transcripts", "System": "~/.claude · config + memory", "Help": "reference",
}

app = FastAPI(title="My Claude", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=SKILL_DIR / "static"), name="static")
templates = Jinja2Templates(directory=SKILL_DIR / "templates")

# ---------- tiny ttl cache ----------
_CACHE = {}
def ttl_cache(seconds=120):
    def deco(fn):
        def wrap(*a, **k):
            key = (fn.__name__, a, tuple(sorted(k.items())))
            try: hash(key)
            except TypeError: return fn(*a, **k)
            hit = _CACHE.get(key)
            if hit and time.time() - hit[0] < seconds: return hit[1]
            val = fn(*a, **k); _CACHE[key] = (time.time(), val); return val
        wrap.cache_clear = lambda: _CACHE.clear()
        wrap.__wrapped__ = fn
        wrap.__name__ = fn.__name__
        return wrap
    return deco

def refresh_entry(cached_fn, *args):
    """Recompute a cached loader OUTSIDE the cache and swap the result in.

    Deliberately not evict-then-recompute: readers keep getting the previous value for the whole
    rebuild, so a background refresh can never turn a 20ms page into a 2s one.
    """
    raw = getattr(cached_fn, "__wrapped__", cached_fn)
    val = raw(*args)
    _CACHE[(raw.__name__, args, ())] = (time.time(), val)
    return val
def clear_cache(): _CACHE.clear()

# Data the dashboard owns lives here, not in the app folder: install.sh replaces ~/.my-claude.
DATA_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "my-claude"
LABELS_PATH = DATA_DIR / "labels.json"

def load_labels():
    try: d = json.loads(LABELS_PATH.read_text())
    except Exception: return {}
    return d if isinstance(d, dict) else {}

def save_label(session, name, tags):
    if not re.fullmatch(r"[0-9a-zA-Z_-]{1,80}", session or ""): raise ValueError("Unknown conversation.")
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:120]
    clean = []
    for t in tags or []:
        t = re.sub(r"[^\w-]", "", str(t).strip().lstrip("#").lower())[:30]
        if t and t not in clean: clean.append(t)
    d = load_labels()
    if name or clean: d[session] = {"name": name, "tags": clean[:10]}
    else: d.pop(session, None)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LABELS_PATH.with_suffix(".tmp"); tmp.write_text(json.dumps(d, indent=2)); tmp.replace(LABELS_PATH)
    return d.get(session, {"name": "", "tags": []})

# ---------- freshness: detect new data instead of waiting for a TTL ----------
# Long TTLs made pages fast but meant a brand-new conversation or an edited skill could sit
# invisible for up to 15 minutes. A stat-only scan of the corpus costs ~6ms, so every request can
# ask "did anything change?" and, if so, rebuild in the background (the current request still
# answers instantly from cache; the next one is fresh).
_fp_cache = {"val": None, "at": 0.0}
_fp_seen = None
_rebuilding = threading.Event()

def data_fingerprint(max_age=2.0):
    """(transcript count, newest transcript mtime, skills-dir entry count, newest skill mtime)."""
    if _fp_cache["val"] and time.time() - _fp_cache["at"] < max_age:
        return _fp_cache["val"]
    n = 0; newest = 0.0
    try:
        for p in (CLAUDE / "projects").rglob("*.jsonl"):
            try: st = p.stat()
            except OSError: continue
            n += 1
            if st.st_mtime > newest: newest = st.st_mtime
    except Exception: pass
    sn = 0; snewest = 0.0
    try:
        for p in (CLAUDE / "skills").iterdir():
            # skip this app's own directory: it holds the caches the rebuild itself writes, so
            # counting it made every rebuild change the fingerprint and trigger another rebuild
            if p == SKILL_DIR: continue
            sn += 1
            for q in (p, p / "SKILL.md"):   # dir mtime catches add/remove, SKILL.md catches edits
                try: m = q.stat().st_mtime
                except OSError: continue
                if m > snewest: snewest = m
    except Exception: pass
    fp = (n, round(newest, 3), sn, round(snewest, 3))
    _fp_cache.update({"val": fp, "at": time.time()})
    return fp

def _rebuild_in_background():
    def run():
        try: rebuild_caches()
        finally: _rebuilding.clear()
    threading.Thread(target=run, daemon=True).start()

REBUILD_MIN_GAP = 60.0   # while Claude is running, the open transcript changes every few seconds
_last_rebuild = 0.0

def ensure_fresh():
    """Serve-stale-then-rebuild, coalesced. Returns True when a rebuild was actually started.

    _fp_seen is only advanced when a rebuild starts, so a change that arrives inside the coalesce
    window is not forgotten — it is picked up by the next request after the window closes.
    """
    global _fp_seen, _last_rebuild
    fp = data_fingerprint()
    if _fp_seen is None:
        _fp_seen = fp; return False
    if fp == _fp_seen: return False
    now = time.time()
    if _rebuilding.is_set() or now - _last_rebuild < REBUILD_MIN_GAP: return False
    _fp_seen = fp; _last_rebuild = now
    _rebuilding.set(); _rebuild_in_background()
    return True

# ---------- format helpers ----------
def fmt_size(n):
    if not n: return "0 B"
    n = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024: return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"

def fmt_rel(ts):
    """'just now', '5 min ago', '3 h ago', '2 days ago' for an epoch time."""
    secs = max(0, time.time() - float(ts))
    if secs < 60: return "just now"
    if secs < 3600: return f"{int(secs // 60)} min ago"
    if secs < 86400: return f"{int(secs // 3600)} h ago"
    d = int(secs // 86400); return f"{d} day{'s' if d != 1 else ''} ago"

def fmt_num(n):
    n = float(n or 0); sign = "-" if n < 0 else ""; n = abs(n)
    if n >= 1e12: return f"{sign}{n/1e12:.2f}T"
    if n >= 1e9:  return f"{sign}{n/1e9:.2f}B"
    if n >= 1e6:  return f"{sign}{n/1e6:.2f}M"
    if n >= 1e3:  return f"{sign}{n/1e3:.1f}K"
    return f"{sign}{int(n)}"

def fmt_when(ts):
    try: return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception: return ""

def html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))

@ttl_cache(900)
def dir_size(p_str):
    p = Path(p_str)
    if not p.exists(): return 0
    if p.is_file(): return p.stat().st_size
    total = 0
    try:
        for root, _, files in os.walk(p, followlinks=False):
            for f in files:
                try: total += os.lstat(os.path.join(root, f)).st_size
                except OSError: pass
    except OSError: pass
    return total

@ttl_cache(900)
def latest_mtime(p_str):
    p = Path(p_str)
    if not p.exists(): return 0
    if p.is_file(): return p.stat().st_mtime
    latest = 0
    try:
        for root, _, files in os.walk(p, followlinks=False):
            for f in files:
                try:
                    m = os.lstat(os.path.join(root, f)).st_mtime
                    if m > latest: latest = m
                except OSError: pass
    except OSError: pass
    return latest

# ---------- desktop actions ----------
def open_in_finder(path):
    try: subprocess.run(["open", path], check=False); return True
    except Exception: return False

def open_in_terminal(path):
    try:
        target = path if os.path.isdir(path) else os.path.dirname(path)
        subprocess.run(["open", "-a", "Terminal", target], check=False); return True
    except Exception: return False

def terminal_run(cmd, cwd=None):
    """Open Terminal and run cmd via a temp .command file + `open`.
    This does NOT require Automation (Apple Events) permission, unlike
    `osascript ... do script`, which macOS blocks by default (error -1743)."""
    workdir = cwd or os.getcwd()
    try:
        fd, path = tempfile.mkstemp(prefix="my-claude-", suffix=".command")
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/bash\ncd " + json.dumps(workdir) + "\n" + cmd + "\n")
        os.chmod(path, 0o755)
        rc = subprocess.run(["open", path], check=False).returncode
        return rc == 0
    except Exception:
        return False

# Terminals we know how to launch, in preference order. "run" = we can hand it a command to
# execute; "open" = we can only bring the app up, so the command goes to the clipboard instead.
TERMINAL_APPS = [
    {"id": "terminal", "name": "Terminal",  "app": "Terminal",             "path": "/System/Applications/Utilities/Terminal.app", "mode": "run"},
    {"id": "iterm",    "name": "iTerm2",    "app": "iTerm",                "path": "/Applications/iTerm.app",                     "mode": "run"},
    {"id": "warp",     "name": "Warp",      "app": "Warp",                 "path": "/Applications/Warp.app",                      "mode": "run"},
    {"id": "ghostty",  "name": "Ghostty",   "app": "Ghostty",              "path": "/Applications/Ghostty.app",                   "mode": "run"},
    {"id": "kitty",    "name": "kitty",     "app": "kitty",                "path": "/Applications/kitty.app",                     "mode": "run"},
    {"id": "wezterm",  "name": "WezTerm",   "app": "WezTerm",              "path": "/Applications/WezTerm.app",                   "mode": "run"},
    # Alacritty: tested here, it neither ran a script passed through `open --args -e` nor through its
    # own CLI, so it is treated as open-only (app comes up, command goes to the clipboard).
    {"id": "alacritty","name": "Alacritty", "app": "Alacritty",            "path": "/Applications/Alacritty.app",                 "mode": "open"},
    {"id": "hyper",    "name": "Hyper",     "app": "Hyper",                "path": "/Applications/Hyper.app",                     "mode": "run"},
    {"id": "vscode",   "name": "VS Code",   "app": "Visual Studio Code",   "path": "/Applications/Visual Studio Code.app",        "mode": "open"},
]

@ttl_cache(900)
def available_terminals():
    """Terminal apps actually installed on this machine (/Applications or ~/Applications)."""
    out = []
    for t in TERMINAL_APPS:
        p = Path(t["path"])
        alt = HOME / "Applications" / Path(t["path"]).name
        if p.exists() or alt.exists():
            out.append({"id": t["id"], "name": t["name"], "mode": t["mode"]})
    return out

def _term_spec(term_id):
    for t in TERMINAL_APPS:
        if t["id"] == term_id: return t
    return None

def terminal_run_in(cmd, cwd=None, term_id=None):
    """Run cmd in a specific terminal app. Falls back to the default handler when no app is named.
    Returns (ok, note) — note is non-empty when the command was copied instead of executed."""
    if not term_id: return (terminal_run(cmd, cwd=cwd), "")
    spec = _term_spec(term_id)
    if not spec: return (terminal_run(cmd, cwd=cwd), "")
    workdir = cwd or os.getcwd()
    pbcopy(cmd)   # always leave the command on the clipboard, so a paste is the worst case
    if spec["mode"] == "open":
        # apps we can't hand a command to: open them, the command is already on the clipboard
        ok = subprocess.run(["open", "-a", spec["app"], workdir], check=False).returncode == 0
        return (ok, f"Opened {spec['name']} · command copied to clipboard")
    try:
        fd, path = tempfile.mkstemp(prefix="my-claude-", suffix=".command")
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/bash\ncd " + json.dumps(workdir) + "\n" + cmd + "\n")
        os.chmod(path, 0o755)
    except Exception:
        return (False, "")
    if spec["id"] in ("ghostty", "kitty", "wezterm", "alacritty"):
        # these take the script as an argument to a new instance. Their own CLI binary is more
        # reliable than `open -na` (verified: Alacritty ignores the -e passed through `open`).
        cli = {"ghostty": "ghostty", "kitty": "kitty", "wezterm": "wezterm", "alacritty": "alacritty"}[spec["id"]]
        exe = shutil.which(cli)
        if exe:
            argv = {"ghostty":  [exe, "-e", "/bin/bash", path],
                    "kitty":    [exe, "/bin/bash", path],
                    "wezterm":  [exe, "start", "--", "/bin/bash", path],
                    "alacritty":[exe, "-e", "/bin/bash", path]}[spec["id"]]
            try:
                subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
                rc = 0
            except Exception: rc = 1
        else:
            args = {"ghostty":  ["--args", "-e", "/bin/bash", path],
                    "kitty":    ["--args", "/bin/bash", path],
                    "wezterm":  ["--args", "start", "--", "/bin/bash", path],
                    "alacritty":["--args", "-e", "/bin/bash", path]}[spec["id"]]
            rc = subprocess.run(["open", "-na", spec["app"]] + args, check=False).returncode
    else:
        rc = subprocess.run(["open", "-a", spec["app"], path], check=False).returncode
    if rc != 0:
        ok = subprocess.run(["open", "-a", spec["app"]], check=False).returncode == 0
        return (ok, f"{spec['name']} would not take the command · copied to clipboard")
    return (True, "")

def start_claude_in_project(project_path, resume_id=None, term_id=None):
    cmd = "claude" + (f" --resume {resume_id}" if resume_id else "")
    return terminal_run_in(cmd, cwd=project_path, term_id=term_id)

def open_file_default(path):
    try:
        if shutil.which("code"): subprocess.run(["code", path], check=False)
        else: subprocess.run(["open", "-e", path], check=False)
        return True
    except Exception: return False

def reveal(path):
    try: subprocess.run(["open", "-R", path], check=False); return True
    except Exception: return False

def pbcopy(text):
    try:
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE); p.communicate(text.encode()); return True
    except Exception: return False

def editor_name(): return "VSCode" if shutil.which("code") else "TextEdit"

# ---------- parsing ----------
def redact_secrets(obj):
    SECRETY = re.compile(r"(token|key|secret|password|pass|auth|bearer|cred)", re.I)
    if isinstance(obj, dict):
        return {k: ("<redacted>" if SECRETY.search(k) and isinstance(v, str) else redact_secrets(v)) for k, v in obj.items()}
    if isinstance(obj, list): return [redact_secrets(x) for x in obj]
    return obj

def parse_skill_meta(skill_dir):
    md = Path(skill_dir) / "SKILL.md"
    if not md.exists(): return {"description": "", "body": "", "raw": ""}
    try:
        text = md.read_text(errors="ignore")
        m = re.search(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.S | re.M)
        if not m: return {"description": "", "body": text, "raw": text}
        fm, body = m.group(1), m.group(2)
        d = re.search(r"^description:\s*(.+?)$", fm, re.M)
        return {"description": (d.group(1).strip().strip('"').strip("'") if d else "")[:500], "body": body, "raw": text}
    except Exception:
        return {"description": "", "body": "", "raw": ""}

# ---- per-file usage parse, memoised on disk by (mtime, size) ----
# Re-reading every transcript cost ~11s on a cold cache, which is what made tab switches lag.
# Now only files that actually changed are re-read; everything else loads from this cache.
USAGE_CACHE_FILE = SKILL_DIR / ".mc_usage_cache.json"
_UROW = ("ts", "model", "input_tokens", "output_tokens", "cache_creation", "cache_read")
_UCACHE_V = 1
_ucache = None; _udirty = False

def _ucache_load():
    global _ucache
    if _ucache is None:
        try: _ucache = json.loads(USAGE_CACHE_FILE.read_text())
        except Exception: _ucache = {}
        if _ucache.get("_v") != _UCACHE_V: _ucache = {"_v": _UCACHE_V}
    return _ucache

def _ucache_flush():
    global _udirty
    if _udirty and _ucache is not None:
        try: USAGE_CACHE_FILE.write_text(json.dumps(_ucache)); _udirty = False
        except Exception: pass

def _scan_usage(jsonl_path):
    records = []
    try:
        with open(jsonl_path, "r", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: obj = json.loads(line)
                except Exception: continue
                msg = obj.get("message") or {}
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if not usage: continue
                model = msg.get("model") or obj.get("model") or ""
                ts = obj.get("timestamp") or obj.get("ts") or ""
                records.append({"ts": ts, "model": model,
                                "input_tokens": int(usage.get("input_tokens") or 0),
                                "output_tokens": int(usage.get("output_tokens") or 0),
                                "cache_creation": int(usage.get("cache_creation_input_tokens") or 0),
                                "cache_read": int(usage.get("cache_read_input_tokens") or 0)})
    except Exception: pass
    return records

def parse_transcript_usage(jsonl_path):
    # No in-memory TTL on purpose: the disk cache below is keyed by (mtime, size), so this always
    # reflects the file as it is now. A 900s memo here made a growing transcript look frozen.
    global _udirty
    d = _ucache_load()
    try: st = os.stat(jsonl_path)
    except OSError: return []
    c = d.get(jsonl_path)
    if c and c.get("mt") == st.st_mtime and c.get("sz") == st.st_size:
        return [dict(zip(_UROW, r)) for r in c["rows"]]
    rows = _scan_usage(jsonl_path)
    d[jsonl_path] = {"mt": st.st_mtime, "sz": st.st_size, "rows": [[r[k] for k in _UROW] for r in rows]}
    _udirty = True
    return rows

# Claude API list prices, per 1M tokens (USD). Source: platform.claude.com/docs/en/about-claude/pricing (verified 2026-07-10).
# cache_write = 5-minute cache write (1.25x input); cache_read = cache hit (0.1x input).
MODEL_PRICING = {
    "fable 5":    {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00},
    "mythos 5":   {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00},
    "opus 4.8":   {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_read": 0.50},
    "opus 4.7":   {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_read": 0.50},
    "opus 4.6":   {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_read": 0.50},
    "opus 4.5":   {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_read": 0.50},
    "opus 4.1":   {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "opus 4":     {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet 5":   {"input":  2.00, "output": 10.00, "cache_write":  2.50, "cache_read": 0.20},  # introductory thru 2026-08-31 (then 3/15)
    "sonnet 4.6": {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "sonnet 4.5": {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "haiku 4.5":  {"input":  1.00, "output":  5.00, "cache_write":  1.25, "cache_read": 0.10},
    "haiku 3.5":  {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_read": 0.08},
}
# current-generation tier rates, used when a model's exact version isn't listed above
TIER_PRICING = {
    "opus":   {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_read": 0.50},
    "sonnet": {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "haiku":  {"input":  1.00, "output":  5.00, "cache_write":  1.25, "cache_read": 0.10},
    "fable":  {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00},
}
def model_family(model_name):
    # pricing-tier hint (opus/sonnet/haiku/fable) used only as a fallback when the exact version isn't in MODEL_PRICING
    n = (model_name or "").lower()
    if "opus" in n: return "opus"
    if "haiku" in n: return "haiku"
    if "fable" in n or "mythos" in n: return "fable"
    return "sonnet"
def pricing_for(model_name):
    lbl = model_label(model_name)
    if lbl in MODEL_PRICING: return MODEL_PRICING[lbl]
    return TIER_PRICING.get(model_family(model_name), TIER_PRICING["sonnet"])
def model_label(model_name):
    # version-aware DISPLAY label so opus versions split out and fable/others show as themselves
    # e.g. claude-opus-4-8 -> "opus 4.8", claude-fable-5 -> "fable 5", claude-haiku-4-5-2025.. -> "haiku 4.5"
    n = (model_name or "").lower()
    fam = next((f for f in ("opus", "sonnet", "haiku", "fable") if f in n), None)
    if not fam:
        return (model_name or "unknown").replace("claude-", "").replace("<", "").replace(">", "").replace("-", " ").strip() or "unknown"
    nums = []
    for p in n.split(fam, 1)[1].replace("_", "-").split("-"):
        if p == "": continue
        if p.isdigit() and len(p) <= 2: nums.append(p)
        else: break
    return (fam + " " + ".".join(nums[:2])) if nums else fam
def label_color(label):
    return FAMILY_COLOR.get((label or "").split(" ")[0], "#8a8a8a")
def project_label(decoded):
    # honest, readable name for a decoded project path (the home-dir project would otherwise show as the username)
    home = str(Path.home())
    if decoded in (home, home + "/"): return "~ (home)"
    if decoded in ("/", "", "-"): return "/ (root)"
    return decoded.split("/")[-1] or decoded
def cost_for(model_name, in_tok, out_tok, cw_tok, cr_tok):
    p = pricing_for(model_name)
    return (in_tok*p["input"] + out_tok*p["output"] + cw_tok*p["cache_write"] + cr_tok*p["cache_read"]) / 1_000_000

@ttl_cache(900)
def aggregate_usage():
    rows = []
    pdir = CLAUDE / "projects"
    if not pdir.exists(): return pd.DataFrame()
    for proj in pdir.iterdir():
        if not proj.is_dir(): continue
        decoded = proj.name.replace("-", "/") if proj.name.startswith("-") else proj.name
        # rglob, not glob: sub-agent and workflow transcripts live in nested dirs and their turns are
        # billed too. Globbing only the top level under-counted 2,075 turns / 0.11B tokens here.
        for jf in proj.rglob("*.jsonl"):
            rel = jf.relative_to(proj)
            sid = jf.name.split(".")[0]
            kind = "conversation" if len(rel.parts) == 1 else ("workflow" if "wf_" in str(rel) else "sub-agent")
            # a sub-agent's turns belong to the conversation that spawned it, so bill them to that session
            parent_sid = rel.parts[0].split(".")[0] if len(rel.parts) > 1 else sid
            for r in parse_transcript_usage(str(jf)):
                r2 = dict(r); r2["project"] = decoded; r2["session"] = parent_sid
                r2["kind"] = kind; r2["transcript"] = sid
                rows.append(r2)
    _ucache_flush()
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows)
    # per-model lookups done once per distinct model, then mapped (was per-row: ~55k calls, seconds of CPU)
    uniq = df["model"].unique()
    fam = {m: model_family(m) for m in uniq}; lbl = {m: model_label(m) for m in uniq}
    df["family"] = df["model"].map(fam); df["label"] = df["model"].map(lbl)
    pr = {m: pricing_for(m) for m in uniq}
    df["cost"] = (df["input_tokens"] * df["model"].map(lambda m: pr[m]["input"])
                  + df["output_tokens"] * df["model"].map(lambda m: pr[m]["output"])
                  + df["cache_creation"] * df["model"].map(lambda m: pr[m]["cache_write"])
                  + df["cache_read"] * df["model"].map(lambda m: pr[m]["cache_read"])) / 1_000_000
    # UTC calendar date of each turn (unchanged from the per-row pd.to_datetime(...).date() it replaces)
    dt = pd.to_datetime(df["ts"], errors="coerce", utc=True, format="mixed")
    df["date"] = dt.dt.date.where(dt.notna(), None)
    return df

@ttl_cache(600)
def transcript_label(jsonl_path):
    try:
        with open(jsonl_path, "r", errors="ignore") as f:
            for line in f:
                if not line.strip(): continue
                try: obj = json.loads(line)
                except Exception: continue
                msg = obj.get("message") or obj
                role = obj.get("type") or msg.get("role") or ""
                if role not in ("user", "human"): continue
                content = msg.get("content") if isinstance(msg, dict) else None
                text = ""
                if isinstance(content, str): text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text": text = c.get("text", ""); break
                text = re.sub(r"\s+", " ", text).strip()
                if text and not text.startswith("<"):
                    return text[:80] + ("…" if len(text) > 80 else "")
    except Exception: pass
    return ""

# Claude Code writes many records as type "user" that the person never typed: background-task
# notices, skill bodies and reminders (isMeta), compaction summaries, command output, other
# sessions' messages, and prompts sent by scripts through the SDK. Only real typing is "user".
_OUTPUT_TAGS = ("<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                "<bash-stdout>", "<bash-stderr>")
def message_role(entry, text):
    if entry.get("type") == "assistant": return "assistant"
    if entry.get("isMeta") or entry.get("isCompactSummary"): return "system"
    kind = (entry.get("origin") or {}).get("kind")
    if kind == "human": return "user"
    if kind == "task-notification" or text.startswith("<task-notification>"): return "notification"
    if kind == "peer": return "peer"
    if kind: return "system"
    if text.startswith(_OUTPUT_TAGS): return "output"
    if text.startswith(("<system-reminder>", "[Request interrupted by user")): return "system"
    if str(entry.get("entrypoint") or "").startswith("sdk"): return "script"
    return "user"

def parse_transcript_messages(jsonl_path, max_messages=20000):
    """Every readable message in a transcript. The old 500-message cap silently truncated long
    conversations (12 of mine), so the cap now only exists as a runaway guard."""
    messages = []
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: entry = json.loads(line)
                except json.JSONDecodeError: continue
                if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                    cm = entry.get("compactMetadata") or {}
                    pre = cm.get("preTokens")
                    messages.append({"role": "compact", "ts": entry.get("timestamp"),
                                     "text": f"Conversation compacted ({cm.get('trigger') or 'unknown'})"
                                             + (f" · {fmt_num(pre)} tokens of context before this point" if isinstance(pre, (int, float)) else "")
                                             + " · everything above was summarised for Claude, and is still here for you"})
                    continue
                if entry.get("type", "") not in ("user", "assistant"): continue
                msg = entry.get("message", {})
                content = msg.get("content", ""); ts = entry.get("timestamp")
                if isinstance(content, str): text = content.strip()
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            bt = block.get("type", "")
                            if bt == "text": parts.append(block.get("text", ""))
                            elif bt == "tool_use": parts.append(f"`[Tool: {block.get('name','unknown')}]`")
                        elif isinstance(block, str): parts.append(block)
                    text = "\n".join(parts).strip()
                else: text = ""
                if not text: continue
                non_tool = [l for l in text.split("\n") if l.strip() and not l.strip().startswith("`[Tool:")]
                if not non_tool: continue
                messages.append({"role": message_role(entry, text), "text": text, "ts": ts})
                if len(messages) >= max_messages: break
    except Exception: pass
    return messages

# ---------- config ----------
DEFAULT_CONFIG = {"github_owner": "", "default_visibility": "private", "repo_prefix": "claude-skill-",
                  "commit_message_template": "Update {skill} skill", "pushed_skills": {}}
def load_config_dict():
    if CONFIG_PATH.exists():
        try: return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
        except Exception: pass
    return dict(DEFAULT_CONFIG)
def save_config(cfg): CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

# ---------- gh ----------
def run_cmd(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def _gh_status_fresh():
    if not shutil.which("gh"):
        return {"logged_in": False, "user": "", "raw": "", "gh_present": False, "host": ""}
    rc, out, err = run_cmd(["gh", "auth", "status"])
    text = (out + "\n" + err).strip()
    logged_in = rc == 0 and "Logged in to" in text
    user = host = ""
    m = re.search(r"account ([A-Za-z0-9_-]+)", text) or re.search(r"as ([A-Za-z0-9_-]+)", text)
    if m: user = m.group(1)
    h = re.search(r"Logged in to ([A-Za-z0-9.\-]+)", text)
    if h: host = h.group(1)
    return {"logged_in": logged_in, "user": user, "raw": text, "gh_present": True, "host": host}

@ttl_cache(300)
def gh_auth_status():
    return _gh_status_fresh()

def gh_repo_exists(owner, repo):
    rc, _, _ = run_cmd(["gh", "repo", "view", f"{owner}/{repo}"]); return rc == 0

# ---------- GitHub device-code login (no Terminal, no stored tokens) ----------
# State machine: idle -> pending (code shown, gh polling) -> done (logged in) | closed (gh exited/expired)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][0-9;]*")
_GH_URL = "https://github.com/login/device"
_gh_login = {"pid": None, "fd": None, "code": None, "url": _GH_URL, "state": "idle", "started": 0.0}
_gh_lock = threading.Lock()

def _proc_alive(pid):
    if not pid: return False
    try: os.kill(pid, 0); return True
    except OSError: return False

def _gh_release(kill=True):
    """Release pty/process resources. Does NOT change state (caller owns that)."""
    if _gh_login["fd"] is not None:
        try: os.close(_gh_login["fd"])
        except OSError: pass
        _gh_login["fd"] = None
    if kill and _gh_login["pid"]:
        try: os.kill(_gh_login["pid"], signal.SIGTERM)
        except OSError: pass
    if _gh_login["pid"]:
        try: os.waitpid(_gh_login["pid"], os.WNOHANG)
        except OSError: pass
    _gh_login["pid"] = None

def _kill_stray_gh_logins():
    """Belt-and-suspenders: reap any orphaned `gh auth login` processes this app spawned."""
    try:
        out = subprocess.run(["pgrep", "-f", "gh auth login"], capture_output=True, text=True).stdout
        for p in out.split():
            try: os.kill(int(p), signal.SIGTERM)
            except (OSError, ValueError): pass
    except Exception: pass

def _gh_drain(fd, pid):
    """Keep the pty alive so gh can poll GitHub: auto-accept follow-up prompts, mark state on exit."""
    buf = ""
    end = time.time() + 900
    while time.time() < end:
        try: r, _, _ = select.select([fd], [], [], 1.0)
        except (OSError, ValueError): break
        if fd in r:
            try: data = os.read(fd, 2048)
            except OSError: break
            if not data: break
            buf = (buf + _ANSI.sub("", data.decode(errors="replace")))[-2000:]
            if re.search(r"\?\s.*\((Y/n|y/N)\)", buf) or buf.rstrip().endswith("?"):
                try: os.write(fd, b"\n")
                except OSError: break
                buf = ""
        if not _proc_alive(pid): break
    with _gh_lock:
        if _gh_login["pid"] == pid:
            logged_in = _gh_status_fresh()["logged_in"]
            _gh_release(kill=True)
            _gh_login["state"] = "done" if logged_in else "closed"

def gh_login_start():
    with _gh_lock:
        if not shutil.which("gh"):
            return {"ok": False, "error": "gh CLI not installed. Install with: brew install gh"}
        # reuse a still-live pending code (dedupes rapid double-clicks) unless it's old
        if (_gh_login["state"] == "pending" and _gh_login["code"] and _proc_alive(_gh_login["pid"])
                and (time.time() - _gh_login["started"]) < 600):
            return {"ok": True, "code": _gh_login["code"], "url": _GH_URL}
        _gh_release(kill=True)
        _kill_stray_gh_logins()
        _gh_login.update({"state": "idle", "code": None})
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["CLICOLOR"] = "0"; os.environ["NO_COLOR"] = "1"
            os.execvp("gh", ["gh", "auth", "login", "-h", "github.com", "-p", "https", "-w"])
            os._exit(127)
        _gh_login.update({"pid": pid, "fd": fd, "code": None, "url": _GH_URL, "state": "starting", "started": time.time()})
    # read output until the one-time code appears
    buf = ""; code = None; deadline = time.time() + 25
    while time.time() < deadline and code is None:
        try: r, _, _ = select.select([fd], [], [], 0.5)
        except (OSError, ValueError): break
        if fd in r:
            try: data = os.read(fd, 2048)
            except OSError: break
            if not data: break
            buf += _ANSI.sub("", data.decode(errors="replace"))
            m = re.search(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})", buf)
            if m: code = m.group(1)
    if not code:
        with _gh_lock: _gh_release(kill=True); _gh_login["state"] = "closed"
        return {"ok": False, "error": "Could not get a device code from gh. Run `gh auth login` in a terminal instead."}
    with _gh_lock: _gh_login["code"] = code; _gh_login["state"] = "pending"
    # Return the code first so the app can show it; gh opens the browser a few seconds
    # LATER (see _gh_after_code) so the user has time to read/copy the code.
    threading.Thread(target=_gh_after_code, args=(fd, pid), daemon=True).start()
    return {"ok": True, "code": code, "url": _GH_URL}

def _gh_after_code(fd, pid):
    time.sleep(3)  # let the user read/copy the code before gh opens the browser
    try: os.write(fd, b"\n")  # press Enter so gh opens the browser and starts polling GitHub
    except OSError: pass
    _gh_drain(fd, pid)

def gh_login_status():
    st = _gh_status_fresh()
    if st["logged_in"]:
        with _gh_lock: _gh_login["state"] = "done"
        return {"logged_in": True, "user": st["user"], "host": st["host"], "state": "done"}
    with _gh_lock:
        state = _gh_login["state"]
        # a "pending" login whose gh process has died = expired/closed
        if state == "pending" and not _proc_alive(_gh_login["pid"]):
            _gh_login["state"] = state = "closed"
        elif state == "pending" and (time.time() - _gh_login["started"]) > 890:
            _gh_release(kill=True); _gh_login["state"] = state = "closed"
    return {"logged_in": False, "user": "", "host": "", "state": state}

SECRET_PATTERNS = [
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("OpenAI/Anthropic key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Assigned secret", re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|access[_-]?key|client[_-]?secret|auth[_-]?token)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")),
]
SKIP_SECRET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".pdf", ".woff", ".woff2", ".ico", ".mp4", ".mov", ".pyc", ".lock"}
def scan_skill_secrets(skill_path, max_bytes=300_000):
    """Best-effort scan of a skill's files for likely credentials, before pushing to GitHub."""
    findings = []; base = Path(skill_path)
    if not base.exists(): return findings
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix.lower() in SKIP_SECRET_EXT: continue
        rel = str(p.relative_to(base))
        if "/.git/" in ("/" + rel): continue
        if p.name == ".env" or p.name.endswith(".env"):
            findings.append({"file": rel, "line": 0, "type": "Env file", "snippet": ".env file (will be published)"}); continue
        try:
            if p.stat().st_size > max_bytes: continue
            text = p.read_text(errors="ignore")
        except Exception: continue
        for i, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            if "<redacted>" in low or "example" in low or "your-" in low or "xxxx" in low: continue
            for label, rx in SECRET_PATTERNS:
                if rx.search(line):
                    findings.append({"file": rel, "line": i, "type": label, "snippet": line.strip()[:120]}); break
            if len(findings) >= 40: return findings
    return findings

def push_skill_to_github(skill_path, owner, repo, visibility):
    log = []; skill_path = Path(skill_path)
    if not skill_path.exists(): return False, "", f"Skill path not found: {skill_path}"
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / repo; stage.mkdir()
        ignore = shutil.ignore_patterns(".venv", "__pycache__", ".DS_Store", "*.pyc", ".git")
        for entry in skill_path.iterdir():
            dst = stage / entry.name
            try:
                if entry.is_dir(): shutil.copytree(entry, dst, ignore=ignore, dirs_exist_ok=True)
                else: shutil.copy2(entry, dst)
            except Exception as e: log.append(f"skip {entry.name}: {e}")
        readme = stage / "README.md"
        if not readme.exists():
            meta = parse_skill_meta(skill_path)
            readme.write_text(f"# {skill_path.name}\n\n{meta['description']}\n\n---\n\n{meta['body']}")
        for cmd in [["git", "init", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-m", f"Initial commit of {skill_path.name} skill"]]:
            rc, out, err = run_cmd(cmd, cwd=str(stage)); log.append(f"$ {' '.join(cmd)}\n{out}\n{err}")
            if rc != 0 and "nothing to commit" not in (out + err): return False, "", "\n".join(log)
        full = f"{owner}/{repo}"
        if gh_repo_exists(owner, repo):
            log.append(f"Repo {full} exists, pushing updates.")
            run_cmd(["git", "remote", "add", "origin", f"https://github.com/{full}.git"], cwd=str(stage))
            rc, out, err = run_cmd(["git", "push", "-u", "--force", "origin", "main"], cwd=str(stage)); log.append(out + err)
            if rc != 0: return False, "", "\n".join(log)
        else:
            cmd = ["gh", "repo", "create", full, f"--{visibility}", "--source", str(stage),
                   "--remote", "origin", "--push", "--description", f"Claude skill: {skill_path.name}"]
            rc, out, err = run_cmd(cmd); log.append(out + err)
            if rc != 0: return False, "", "\n".join(log)
        return True, f"https://github.com/{full}", "\n".join(log)

def install_skill_from_url(url, custom_name=None):
    log = []
    m = re.match(r"https?://github\.com/([^/]+)/([^/.]+)(?:\.git)?/?$", url.strip())
    if not m: return False, "", "URL must be of the form https://github.com/owner/repo"
    owner, repo = m.group(1), m.group(2)
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", (custom_name or repo).strip())
    target = CLAUDE / "skills" / name
    if target.exists(): return False, name, f"Already exists: {target}"
    rc, out, err = run_cmd(["git", "clone", "--depth", "1", url.strip(), str(target)]); log.append(out + err)
    if rc != 0: return False, name, "\n".join(log)
    if not (target / "SKILL.md").exists():
        log.append("Warning: no SKILL.md in repo root. Claude won't recognize it as a skill until SKILL.md is added.")
    return True, name, "\n".join(log)

def mcp_check(name, cfg):
    if not isinstance(cfg, dict): return ("unknown", "config not a dict")
    if "url" in cfg:
        try:
            import urllib.request, urllib.error
            req = urllib.request.Request(cfg["url"], method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp: return ("ok", f"HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            # MCP servers answer a bare GET with 401/403/405 (no auth, wrong method): the server is up.
            if e.code < 500: return ("ok", f"HTTP {e.code}, server answered")
            return ("fail", f"HTTP {e.code}")
        except Exception as e: return ("fail", f"{type(e).__name__}: {str(e)[:80]}")
    cmd = cfg.get("command")
    if cmd:
        if shutil.which(cmd) or os.path.exists(cmd): return ("ok", f"command found: {cmd}")
        return ("fail", f"command not found: {cmd}")
    return ("unknown", "no url or command")

def make_skill_zip(skill_path):
    skill_path = Path(skill_path); buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_path):
            dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__", ".git")]
            for f in files:
                if f == ".DS_Store" or f.endswith(".pyc"): continue
                full = Path(root) / f
                zf.write(full, arcname=str(full.relative_to(skill_path.parent)))
    buf.seek(0); return buf.read()

# ---------- collectors ----------
@ttl_cache(900)
def get_plugin_skill_names():
    names = set(); pdir = CLAUDE / "plugins"
    if not pdir.exists(): return names
    for root, dirs, files in os.walk(pdir, followlinks=False):
        if "SKILL.md" in files: names.add(Path(root).name)
    return names

_SKILL_CATS = [
    ("Evals & Emergent", ["eval", "emergent", "ecu", "redash", "trajector", "job-", "template-eval", "remix", "nudge", "investigate"]),
    ("Security", ["security", "vuln", "pentest", "pen-test", "threat", "exploit", "owasp", "secret", "jwt", "xss", "sql-injection", "injection", "red-team", "fuzz", "sast", "csp", "cors", "zap", "burp", "malware", "auth-", "oauth", "zeroize", "yara", "seatbelt", "supply-chain"]),
    ("Testing & QA", ["test", "qa", "playwright", "cypress", "selenium", "e2e", "coverage", "bdd", "tdd", "mutation", "flak", "regression", "assert", "mock", "fixture", "appium", "espresso", "junit", "pytest", "jest", "naodeng", "qe-", "qcsd", "vitest", "cucumber"]),
    ("Database & Data", ["sql", "database", "postgres", "mongo", "redis", "data-quality", "migration", "schema", "orm", "drizzle", "prisma", "elasticsearch", "data-pipeline", "snowflake", "spark", "statistic", "analyst", "analytics", "pandas"]),
    ("DevOps & Infra", ["kubernetes", "k8s", "docker", "terraform", "helm", "cloud", "aws", "azure", "gcp", "ci-", "cicd", "deploy", "observ", "slo", "sre", "runbook", "incident", "infra", "devops", "monitoring", "canary", "serverless", "operator"]),
    ("Frontend & UI", ["react", "vue", "angular", "svelte", "next", "frontend", "css", "design-system", "accessibility", "a11y", "responsive", "visual", "screenshot", "dark-mode", "storybook", "shadcn", "theme", "ux", "layout", "design"]),
    ("AI, LLM & Agents", ["llm", "agent", "mcp", "prompt", "rag", "neural", "swarm", "fine-tun", "reasoningbank", "flow-nexus", "ml-"]),
    ("Skill & Workflow Authoring", ["skill", "workflow", "subagent", "sparc"]),
    ("Docs & Reports", ["report", "markdown", "md-", "slides", "marp", "pdf", "xlsx", "pptx", "docx", "changelog", "technical-writing", "doc-", "documenter"]),
    ("Productivity & Process", ["plan", "brainstorm", "review", "git", "github", "handoff", "memory", "remember", "resume", "eod", "eos", "news", "daily", "organize", "task", "scrum", "jira", "confluence"]),
]
_EXACT = {"ecu", "orm", "ml", "ux", "ci", "eval"}
def categorize_skill(name, desc):
    def hit(text, kws):
        toks = re.findall(r"[a-z0-9]+", text)
        for kw in kws:
            if "-" in kw or " " in kw:
                if kw in text: return True
            elif kw in _EXACT:
                if any(t == kw for t in toks): return True
            elif any(t == kw or t.startswith(kw) for t in toks): return True
        return False
    nm = name.lower()
    for label, kws in _SKILL_CATS:
        if hit(nm, kws): return label
    hay = (name + " " + desc).lower()
    for label, kws in _SKILL_CATS:
        if hit(hay, kws): return label
    return "Other"

@ttl_cache(900)
def load_skills():
    sk = CLAUDE / "skills"; plugin_names = get_plugin_skill_names(); rows = []
    if not sk.exists(): return []
    for p in sorted(sk.iterdir()):
        if not p.is_dir(): continue
        meta = parse_skill_meta(p)
        rows.append({"name": p.name, "description": meta["description"], "size": dir_size(str(p)),
                     "size_human": fmt_size(dir_size(str(p))), "path": str(p), "mtime": latest_mtime(str(p)),
                     "cat": categorize_skill(p.name, meta["description"]),
                     "source": "plugin" if p.name in plugin_names else "local"})
    return rows

@ttl_cache(900)
def load_memory():
    """Read the persistent memory files (~/.claude/projects/*/memory/*.md)."""
    out = []; base = CLAUDE / "projects"; home_short = HOME.name
    if base.exists():
        for md in base.glob("*/memory/*.md"):
            try: txt = md.read_text(errors="ignore")
            except OSError: continue
            slug = md.parent.parent.name
            project = slug.replace("-", "/") if slug.startswith("-") else slug
            proj_short = project.split("/")[-1] or project
            m = re.search(r"^---\s*\n(.*?)\n---\s*\n?(.*)", txt, re.S)
            name = md.stem; desc = ""
            if m:
                nn = re.search(r"^name:\s*(.+)$", m.group(1), re.M); dd = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
                if nn: name = nn.group(1).strip().strip('"').strip("'")
                if dd: desc = dd.group(1).strip().strip('"').strip("'")
            if not desc:
                for ln in (m.group(2) if m else txt).splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith(("#", "-", "*", "|", ">", "`")): desc = ln[:160]; break
            is_index = md.name.upper() == "MEMORY.MD"
            title = f"{proj_short} · index" if is_index else name
            out.append({"name": name, "title": title, "desc": desc[:200], "path": str(md), "file": md.name,
                        "project": proj_short, "is_index": is_index, "is_home": proj_short == home_short,
                        "size": md.stat().st_size})
    out.sort(key=lambda x: (not x["is_home"], not x["is_index"], x["project"].lower(), x["name"].lower()))
    return out

HOOK_DESC = {
    "sessionstart": "Runs when a Claude Code session starts.",
    "sessionend": "Runs when a session ends.",
    "userpromptsubmit": "Runs each time you submit a prompt.",
    "pretooluse": "Runs before a tool executes; can block or change it.",
    "posttooluse": "Runs after a tool finishes.",
    "precompact": "Runs before the conversation context is compacted.",
    "notification": "Runs on notifications, e.g. when Claude needs permission.",
    "stop": "Runs when the main agent finishes responding.",
    "subagentstop": "Runs when a subagent finishes.",
}
SETTINGS_DESC = {
    "effortlevel": "Default reasoning effort for the model.",
    "enabledplugins": "Plugins currently switched on.",
    "extraknownmarketplaces": "Extra plugin marketplaces Claude Code trusts.",
    "hooks": "Shell commands wired to lifecycle events.",
    "permissions": "Allow / ask / deny rules for tools.",
    "skipautopermissionprompt": "Skip the automatic permission prompt.",
    "skipdangerousmodepermissionprompt": "Skip the dangerous-mode confirmation prompt.",
    "tui": "Terminal UI preferences.",
    "model": "Default model selection.",
    "theme": "Colour theme preference.",
    "statusline": "Custom status-line command.",
    "env": "Environment variables passed to tools.",
    "includecoauthoredby": "Adds a Co-Authored-By line to git commits.",
    "outputstyle": "Response output style.",
    "autoupdaterstatus": "Auto-update behaviour for the CLI.",
    "apikeyhelper": "Command that supplies the API key.",
}
def _cfg_desc(table, key):
    return table.get(re.sub(r"[^a-z0-9]", "", key.lower()), "")

@ttl_cache(900)
def load_config_detail():
    """settings.json permissions + hooks, and CLAUDE.md content."""
    settings = {}
    sp = CLAUDE / "settings.json"
    if sp.exists():
        try: settings = json.loads(sp.read_text())
        except Exception: settings = {}
    perms = settings.get("permissions") or {}
    hooks = settings.get("hooks") or {}
    hk = sorted(hooks.keys()) if isinstance(hooks, dict) else []
    hook_events = [{"name": h, "desc": _cfg_desc(HOOK_DESC, h)} for h in hk]
    settings_keys = [{"name": k, "desc": _cfg_desc(SETTINGS_DESC, k)} for k in sorted(settings.keys())]
    claude_md = ""
    cm = CLAUDE / "CLAUDE.md"
    if cm.exists():
        try: claude_md = cm.read_text(errors="ignore")
        except OSError: pass
    return {"allow": perms.get("allow") or [], "deny": perms.get("deny") or [], "ask": perms.get("ask") or [],
            "hook_events": hook_events, "claude_md": claude_md, "settings_keys": settings_keys}

@ttl_cache(900)
def load_connectors():
    """All MCP servers from every config source: global (settings.json / .claude.json)
    and per-project (.claude.json projects[*].mcpServers). Scope shows where it applies."""
    out = []
    for cfg in [CLAUDE / "settings.json", HOME / ".claude.json"]:
        if not cfg.exists(): continue
        try: data = json.loads(cfg.read_text())
        except Exception: continue
        for k, v in (data.get("mcpServers") or {}).items():
            out.append({"name": k, "source": cfg.name, "scope": "global", "config": redact_secrets(v)})
        projs = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        for ppath, pv in projs.items():
            for k, v in ((pv or {}).get("mcpServers") or {}).items():
                proj = ppath.replace(str(HOME), "~")
                out.append({"name": k, "source": cfg.name, "scope": proj, "config": redact_secrets(v)})
    return out

@ttl_cache(900)
def load_plugins():
    out = []; pdir = CLAUDE / "plugins"
    if not pdir.exists(): return out
    inst = pdir / "installed_plugins.json"
    if inst.exists():
        try:
            data = json.loads(inst.read_text()); plugins = data.get("plugins") or data
            if isinstance(plugins, dict):
                for k, v in plugins.items(): out.append({"name": k, "info": v})
            elif isinstance(plugins, list):
                for v in plugins: out.append({"name": v.get("name", "?"), "info": v})
        except Exception: pass
    mk = pdir / "marketplaces"
    if mk.exists():
        for m in mk.iterdir():
            if m.is_dir(): out.append({"name": f"marketplace/{m.name}", "info": {"path": str(m), "size": fmt_size(dir_size(str(m)))}})
    return out

@ttl_cache(900)
def load_projects():
    pdir = CLAUDE / "projects"; rows = []
    if not pdir.exists(): return []
    for p in sorted(pdir.iterdir()):
        if not p.is_dir(): continue
        slug = p.name; decoded = slug.replace("-", "/") if slug.startswith("-") else slug
        files = list(p.glob("*.jsonl"))
        sz = dir_size(str(p))
        rows.append({"project": decoded, "slug": slug, "transcripts": len(files), "size": sz,
                     "size_human": fmt_size(sz), "path": str(p),
                     "last_active": max((f.stat().st_mtime for f in files), default=0),
                     "exists": Path(decoded).exists()})
    rows.sort(key=lambda r: r["last_active"], reverse=True)
    return rows

def list_project_sessions(project_storage_path):
    p = Path(project_storage_path)
    if not p.exists(): return []
    out = []
    for f in p.glob("*.jsonl"):
        s = f.stat(); out.append({"name": f.name, "size": s.st_size, "mtime": s.st_mtime, "path": str(f)})
    return sorted(out, key=lambda x: -x["mtime"])

@ttl_cache(900)
def load_config_files():
    rows = []
    for f in ["settings.json", "settings.local.json", "CLAUDE.md", "policy-limits.json", "stats-cache.json", "mcp-needs-auth-cache.json", ".last-cleanup"]:
        p = CLAUDE / f
        if p.exists(): rows.append({"name": f, "size": dir_size(str(p)), "size_human": fmt_size(dir_size(str(p))), "path": str(p)})
    cdir = CLAUDE / "commands"
    if cdir.exists():
        for c in cdir.iterdir():
            rows.append({"name": f"commands/{c.name}", "size": dir_size(str(c)), "size_human": fmt_size(dir_size(str(c))), "path": str(c)})
    rows.sort(key=lambda r: r["size"], reverse=True)
    return rows

@ttl_cache(900)
def load_cache():
    rows = []
    for d in ["shell-snapshots", "paste-cache", "file-history", "image-cache", "session-env", "telemetry", "ide", "cache", "downloads", "debug", "backups", "todos"]:
        p = CLAUDE / d
        if p.exists():
            rows.append({"name": d, "items": len(list(p.iterdir())) if p.is_dir() else 0, "size": dir_size(str(p)), "size_human": fmt_size(dir_size(str(p))), "path": str(p)})
    h = CLAUDE / "history.jsonl"
    if h.exists(): rows.append({"name": "history.jsonl", "items": 0, "size": h.stat().st_size, "size_human": fmt_size(h.stat().st_size), "path": str(h)})
    rows.sort(key=lambda r: r["size"], reverse=True)
    return rows

@ttl_cache(900)
def load_tasks():
    rows = []
    for d in ["tasks", "plans"]:
        p = CLAUDE / d
        if p.exists(): rows.append({"name": d, "items": len(list(p.iterdir())), "size": dir_size(str(p)), "size_human": fmt_size(dir_size(str(p))), "path": str(p)})
    return rows

def ts_epoch(v):
    """Best-effort timestamp -> epoch seconds. Handles ISO strings and int/float."""
    if v is None: return 0
    if isinstance(v, (int, float)): return float(v if v < 1e12 else v / 1000)
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit(): n = int(s); return float(n if n < 1e12 else n / 1000)
        try: return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception: return 0
    return 0

# ---- persistent per-file scan cache (keyed by path + mtime + size), for fast cold loads ----
CACHE_FILE = SKILL_DIR / ".mc_cache.json"
_disk = None; _dirty = False
CMD_RE = re.compile(r"<command-name>/?([a-zA-Z0-9:_.-]{2,48})</command-name>")
# Claude Code built-in slash commands — excluded from skill-usage attribution so a built-in
# like /resume isn't miscounted as a same-named skill. (Skill tool_use invocations still count.)
BUILTIN_CMDS = {"add-dir", "agents", "bug", "clear", "compact", "config", "cost", "doctor", "exit",
                "export", "help", "hooks", "init", "login", "logout", "mcp", "memory", "model",
                "permissions", "pr-comments", "release-notes", "resume", "review", "status",
                "terminal-setup", "usage-credits", "vim", "effort", "fast", "context", "rewind"}

_CACHE_V = 3  # bump when _scan_meta output shape/logic changes, to invalidate the on-disk cache
def _disk_load():
    global _disk
    if _disk is None:
        try: _disk = json.loads(CACHE_FILE.read_text())
        except Exception: _disk = {}
        if _disk.get("_v") != _CACHE_V: _disk = {"_v": _CACHE_V}
    return _disk

def _disk_flush():
    global _dirty
    if _dirty:
        try: CACHE_FILE.write_text(json.dumps(_disk)); _dirty = False
        except Exception: pass

def _scan_meta(path):
    """One read per transcript: preview, timestamps, message count, and skill invocations."""
    first_user = ""; first_ts = last_ts = 0; n = 0; skills = []; ai_title = custom_title = ""; compactions = 0
    try:
        with open(path, "r", errors="ignore") as f:
            for raw in f:
                raw = raw.strip()
                if not raw: continue
                cmds = CMD_RE.findall(raw) if "<command-name>" in raw else []
                skill_tool = '"name":"Skill"' in raw
                try: e = json.loads(raw)
                except Exception: continue
                et = e.get("type")
                # Claude Code's own names: /rename writes custom-title, and it writes ai-title itself
                if et == "custom-title" and e.get("customTitle"): custom_title = str(e["customTitle"])[:120]; continue
                if et == "ai-title" and e.get("aiTitle"): ai_title = str(e["aiTitle"])[:120]; continue
                if et == "system" and e.get("subtype") == "compact_boundary": compactions += 1; continue
                if et not in ("user", "assistant"): continue
                t = ts_epoch(e.get("timestamp") or e.get("ts"))
                msg = e.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                for cm in cmds:
                    if cm.split(":")[-1] not in BUILTIN_CMDS: skills.append([cm, t])
                if skill_tool and isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                            sk = (b.get("input") or {}).get("skill")
                            if sk: skills.append([sk, t])
                text = ""
                if isinstance(content, str): text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text": text += c.get("text", "") + " "
                text = re.sub(r"\s+", " ", text).strip()
                if not text or text.startswith("<"): continue
                n += 1
                if t:
                    if not first_ts: first_ts = t
                    last_ts = t
                role = e.get("type") or (msg.get("role") if isinstance(msg, dict) else "")
                if role in ("user", "human") and not first_user: first_user = text[:120]
    except Exception: pass
    return {"preview": first_user or "(no preview)", "first_ts": first_ts, "last_ts": last_ts, "n": n, "skills": skills,
            "title": custom_title or ai_title, "compactions": compactions}

def _conv_meta(path):
    global _dirty
    d = _disk_load()
    try: st = os.stat(path)
    except OSError: return _scan_meta(path)
    c = d.get(path)
    if c and c.get("mt") == st.st_mtime and c.get("sz") == st.st_size:
        return c["v"]
    v = _scan_meta(path)
    d[path] = {"mt": st.st_mtime, "sz": st.st_size, "v": v}; _dirty = True
    return v

@ttl_cache(900)
def conversation_index(include_sub=False):
    """Top-level conversations by default. include_sub also walks nested sub-agent + workflow transcripts."""
    pdir = CLAUDE / "projects"; out = []; labels = load_labels()
    if not pdir.exists(): return out
    for proj in pdir.iterdir():
        if not proj.is_dir(): continue
        decoded = proj.name.replace("-", "/") if proj.name.startswith("-") else proj.name
        globber = proj.rglob("*.jsonl") if include_sub else proj.glob("*.jsonl")
        for jf in globber:
            meta = dict(_conv_meta(str(jf)))
            rel = jf.relative_to(proj)
            if len(rel.parts) == 1: kind = "conversation"
            elif "wf_" in str(rel): kind = "workflow"
            else: kind = "sub-agent"
            if meta["n"] == 0:
                # tool-only / meta-only transcript: still a real session, so list it instead of dropping it
                if not meta.get("last_ts"):
                    try: meta["first_ts"] = meta["last_ts"] = os.stat(jf).st_mtime
                    except OSError: pass
                meta["preview"] = "(no readable text — tool-only session)"
            sid = jf.name.split(".")[0]; lab = labels.get(sid) or {}
            meta.update({"project": decoded, "session": sid, "path": str(jf), "kind": kind,
                         "title": lab.get("name") or meta.get("title") or meta["preview"], "tags": lab.get("tags") or []})
            out.append(meta)
    out.sort(key=lambda r: r["last_ts"] or 0, reverse=True)
    _disk_flush()
    return out

HISTORY_FILE = CLAUDE / "history.jsonl"

@ttl_cache(900)
def archived_sessions():
    """Sessions whose transcript file is no longer on disk (Claude Code rotates old project
    transcripts away) but whose prompts survive in ~/.claude/history.jsonl. Prompts only —
    no assistant replies — so they are labelled 'archived' in the UI."""
    if not HISTORY_FILE.exists(): return []
    on_disk = set()
    pdir = CLAUDE / "projects"
    if pdir.exists():
        for jf in pdir.rglob("*.jsonl"): on_disk.add(jf.stem)
    sess = {}
    try:
        with open(HISTORY_FILE, "r", errors="ignore") as f:
            for raw in f:
                raw = raw.strip()
                if not raw: continue
                try: o = json.loads(raw)
                except Exception: continue
                sid = o.get("sessionId")
                if not sid or sid in on_disk: continue
                ts = ts_epoch(o.get("timestamp"))
                disp = re.sub(r"\s+", " ", str(o.get("display") or "")).strip()
                s = sess.get(sid)
                if not s:
                    s = sess[sid] = {"n": 0, "first_ts": ts, "last_ts": ts, "project": o.get("project") or "",
                                     "preview": "", "prompts": []}
                s["n"] += 1
                if ts:
                    s["last_ts"] = max(s["last_ts"] or ts, ts)
                    s["first_ts"] = min(s["first_ts"] or ts, ts)
                if disp:
                    s["prompts"].append([ts, disp])
                    # prefer a real typed prompt over a "[Pasted text …]" placeholder as the title
                    if not s["preview"] or (s["preview"].startswith("[Pasted") and not disp.startswith("[Pasted")):
                        s["preview"] = disp[:120]
    except Exception: return []
    out = []
    for sid, s in sess.items():
        out.append({"preview": s["preview"] or "(no prompt text)", "first_ts": s["first_ts"], "last_ts": s["last_ts"],
                    "n": s["n"], "skills": [], "project": s["project"], "session": sid,
                    "path": "archived:" + sid, "kind": "archived"})
    out.sort(key=lambda r: r["last_ts"] or 0, reverse=True)
    return out

def archived_prompts(sid):
    """The prompts of one archived session, in order."""
    rows = []
    try:
        with open(HISTORY_FILE, "r", errors="ignore") as f:
            for raw in f:
                raw = raw.strip()
                if not raw: continue
                try: o = json.loads(raw)
                except Exception: continue
                if o.get("sessionId") != sid: continue
                disp = str(o.get("display") or "")
                rows.append({"role": "user", "text": disp, "ts": None,
                             "ts_epoch": ts_epoch(o.get("timestamp")), "project": o.get("project") or ""})
    except Exception: pass
    for r in rows:
        r["ts"] = datetime.fromtimestamp(r["ts_epoch"]).isoformat() if r["ts_epoch"] else None
    return rows

_HIST_CMD = re.compile(r"^/([a-zA-Z0-9:_.-]{2,48})")

@ttl_cache(900)
def skill_usage():
    """Per-skill invocation counts + last-used.

    Two sources, because transcripts alone under-count: command-markers and Skill tool calls in the
    transcripts on disk, PLUS slash-commands in ~/.claude/history.jsonl belonging to sessions whose
    transcript Claude Code has already rotated away (171 such invocations here, e.g.
    emergent-test-agent 36 -> 41, eos 11 -> 19). Only names that match an installed skill are
    counted, so a prompt starting with /Users/... or /var/... can't inflate anything.
    """
    agg = {}
    def bump(key, ts, src):
        a = agg.setdefault(key, {"count": 0, "last_ts": 0, "from_transcripts": 0, "from_history": 0})
        a["count"] += 1; a[src] += 1
        if ts and ts > a["last_ts"]: a["last_ts"] = ts
    for m in conversation_index():
        for name, ts in m.get("skills", []):
            bump(str(name).split(":")[-1], ts, "from_transcripts")
    for key, ts in archived_slash_commands():
        bump(key, ts, "from_history")
    return agg

@ttl_cache(900)
def archived_slash_commands():
    """(skill, timestamp) for slash-commands in history.jsonl whose transcript is gone.

    Filtered to names that are actually installed skills, so path-like prompts (/Users/…, /var/…)
    and Claude Code's own built-ins are never counted as skill usage.
    """
    if not HISTORY_FILE.exists(): return []
    known = {s["name"] for s in load_skills()}
    on_disk = {p.stem for p in (CLAUDE / "projects").rglob("*.jsonl")}
    out = []
    try:
        with open(HISTORY_FILE, "r", errors="ignore") as f:
            for raw in f:
                raw = raw.strip()
                if not raw or "/" not in raw: continue
                try: o = json.loads(raw)
                except Exception: continue
                sid = o.get("sessionId")
                if not sid or sid in on_disk: continue
                mt = _HIST_CMD.match(str(o.get("display") or "").strip())
                if not mt: continue
                key = mt.group(1).split(":")[-1]
                if key in known and key not in BUILTIN_CMDS:
                    out.append((key, ts_epoch(o.get("timestamp"))))
    except Exception: pass
    return out

@ttl_cache(900)
def usage_stats():
    """Everything derived from per-turn usage: per-session cost, 7d/30d trends, cache savings, activity heatmap."""
    df = aggregate_usage()
    if df.empty: return {"empty": True, "by_session": {}}
    df = df.copy()
    df["billable"] = df["input_tokens"] + df["cache_creation"] + df["cache_read"] + df["output_tokens"]
    inp = df["model"].map(lambda m: pricing_for(m)["input"]); crp = df["model"].map(lambda m: pricing_for(m)["cache_read"])
    df["savings"] = df["cache_read"] * (inp - crp) / 1_000_000
    by_session = {s: {"cost": round(float(g["cost"].sum()), 4), "tokens": int(g["billable"].sum()), "turns": int(len(g))}
                  for s, g in df.groupby("session")}
    today = datetime.now().date()
    def win(a, b):
        sub = df[df["date"].apply(lambda d: bool(d) and a <= d < b)]
        return float(sub["cost"].sum()), int(sub["billable"].sum())
    c7, t7 = win(today - timedelta(days=7), today + timedelta(days=1))
    cp7, _ = win(today - timedelta(days=14), today - timedelta(days=7))
    c30, t30 = win(today - timedelta(days=30), today + timedelta(days=1))
    dd = df.dropna(subset=["date"])
    heat = [{"date": str(d), "v": int(v)} for d, v in dd.groupby("date").size().items()]
    daycost = dd.groupby("date")["cost"].sum()
    top_day = [str(daycost.idxmax()), round(float(daycost.max()), 2)] if len(daycost) else [None, 0]
    by_fam = {f: round(float(g["cost"].sum()), 2) for f, g in df.groupby("family")}
    return {"empty": False, "by_session": by_session, "cost7": c7, "tok7": t7, "cost_prev7": cp7,
            "cost30": c30, "tok30": t30, "savings": round(float(df["savings"].sum()), 2),
            "heat": heat, "top_day": top_day, "by_family": by_fam}

# ---------- token-reduction insights ----------
# Every finding here is measured from this machine's own transcripts and files. No rules of thumb:
# if a number cannot be computed, the finding is not shown.
CHARS_PER_TOKEN = 4        # rough but stated everywhere it is used

@ttl_cache(900)
def token_insights():
    out = []
    df = aggregate_usage()
    skills = load_skills(); usage = skill_usage()

    # 1. the always-loaded skill catalogue: every request carries name + description of every skill
    chars = sum(len(s["name"]) + len(s["description"]) for s in skills)
    unused = [s for s in skills if usage.get(s["name"], {}).get("count", 0) == 0]
    un_chars = sum(len(s["name"]) + len(s["description"]) for s in unused)
    per_req = chars // CHARS_PER_TOKEN; un_per_req = un_chars // CHARS_PER_TOKEN
    if skills:
        out.append({
            "metric": fmt_num(per_req) + " tokens", "unit": "on every single request",
            "title": "Your skill catalogue is the largest fixed cost",
            "detail": (f"{len(skills)} skills are installed, and their names + descriptions travel in the system "
                       f"prompt of every request: about {per_req:,} tokens before you type anything. "
                       f"{len(unused)} of them have never been invoked, worth ~{un_per_req:,} tokens per request."),
            "action": f"Move the {len(unused)} never-used skills out of ~/.claude/skills (keep a folder elsewhere and "
                      f"symlink back what you need). That is the single biggest reduction available here.",
            "severity": "high" if un_per_req > 5000 else "normal"})

    # 2. context growth with session length — the case for /clear
    if not df.empty:
        d = df.sort_values("ts").copy()
        d["billable"] = d.input_tokens + d.output_tokens + d.cache_creation + d.cache_read
        d["ctx"] = d.input_tokens + d.cache_read + d.cache_creation
        d["idx"] = d.groupby("session").cumcount()
        early = d[d["idx"] < 50]; late = d[d["idx"] >= 400]
        if len(early) and len(late):
            ratio = late["ctx"].median() / max(1, early["ctx"].median())
            share = late["billable"].sum() / max(1, d["billable"].sum()) * 100
            out.append({
                "metric": f"{ratio:.1f}x", "unit": "more context per turn late in a session",
                "title": "Long sessions are where the tokens actually go",
                "detail": (f"A turn in the first 50 of a session sends a median {fmt_num(early['ctx'].median())} tokens of "
                           f"context. Past turn 400 that is {fmt_num(late['ctx'].median())}. Those late turns are "
                           f"{len(late)/len(d)*100:.0f}% of your turns but {share:.0f}% of your billable tokens."),
                "action": "Start a fresh session (or /clear) once a task is done instead of continuing in the same one. "
                          "Everything already in context is re-sent, at cache-read price, on every following turn.",
                "severity": "high" if share > 50 else "normal"})

        # 3. turns that hit the context ceiling
        ceiling = d[d["ctx"] > 900_000]
        if len(ceiling):
            out.append({
                "metric": str(len(ceiling)), "unit": "turns above 900K context",
                "title": "Some turns ran right up against the context window",
                "detail": (f"{len(ceiling)} turns sent more than 900K tokens of context, the largest "
                           f"{fmt_num(d['ctx'].max())}. At that size every further turn is close to the maximum "
                           f"the model will accept, and auto-compaction is doing the thinking about what to drop."),
                "action": "Compact or split the work before the window fills, so you choose what survives rather than "
                          "letting summarisation choose.",
                "severity": "normal"})

        # 4. cache health — cheap to check, expensive when it is wrong
        cr = float(d.cache_read.sum()); cw = float(d.cache_creation.sum())
        if cr + cw:
            hit = cr / (cr + cw) * 100
            out.append({
                "metric": f"{hit:.1f}%", "unit": "cache hit rate",
                "title": "Prompt caching is working" if hit > 90 else "Prompt caching is being missed",
                "detail": (f"{fmt_num(cr)} tokens were served from cache at ~10% of input price, against "
                           f"{fmt_num(cw)} written. Cache reads are what make long sessions survivable at all."),
                "action": ("Nothing to fix here — but note it only holds while the start of your context stays "
                           "identical. Editing CLAUDE.md or your MCP set mid-session invalidates it."
                           if hit > 90 else
                           "Something is changing early in your context between turns (CLAUDE.md edits, MCP servers "
                           "coming and going). Keep the prefix stable to get cache reads back."),
                "severity": "normal" if hit > 90 else "high"})

    # 5. files re-read many times inside one session
    dup = repeated_reads()
    if dup["redundant"]:
        top = dup["worst"][0]
        out.append({
            "metric": str(dup["redundant"]), "unit": "redundant file reads",
            "title": "The same files get read over and over in a session",
            "detail": (f"{dup['sessions']} sessions read at least one file 5+ times. Worst: "
                       f"{top['count']}x {top['file']}. Each re-read pays for the whole file again, "
                       f"and pushes older context towards compaction."),
            "action": "Ask for a targeted read (offset/limit or grep) instead of the whole file, and say so once — "
                      "'you already read this' costs nothing to type.",
            "severity": "normal"})

    # 6. what is loaded before you type: CLAUDE.md + the memory index
    cm = CLAUDE / "CLAUDE.md"; mem_idx = None
    for cand in (CLAUDE / "projects").glob("*/memory/MEMORY.md"): mem_idx = cand; break
    cm_t = (cm.stat().st_size // CHARS_PER_TOKEN) if cm.exists() else 0
    mi_t = (mem_idx.stat().st_size // CHARS_PER_TOKEN) if mem_idx else 0
    if cm_t or mi_t:
        out.append({
            "metric": fmt_num(cm_t + mi_t) + " tokens", "unit": "of instructions per session",
            "title": "Your standing instructions have a size",
            "detail": (f"CLAUDE.md is ~{cm_t:,} tokens and the memory index ~{mi_t:,}. Both are loaded before your "
                       f"first message, and both are re-sent on every turn of the session."),
            "action": "Worth a trim pass when either grows: rules that never fire still cost tokens every turn.",
            "severity": "normal"})
    return out

@ttl_cache(900)
def repeated_reads(min_repeat=5):
    """Files a session read min_repeat+ times — tokens paid more than once."""
    pdir = CLAUDE / "projects"
    sessions = 0; redundant = 0; worst = []
    if not pdir.exists(): return {"sessions": 0, "redundant": 0, "worst": []}
    home = str(HOME)
    for jf in pdir.glob("*/*.jsonl"):
        reads = {}
        try:
            with open(jf, "r", errors="ignore") as f:
                for raw in f:
                    if '"name":"Read"' not in raw: continue
                    try: e = json.loads(raw)
                    except Exception: continue
                    msg = e.get("message") or {}
                    c = msg.get("content")
                    if not isinstance(c, list): continue
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Read":
                            fp = (b.get("input") or {}).get("file_path")
                            if fp: reads[fp] = reads.get(fp, 0) + 1
        except Exception: continue
        hits = [(n, fp) for fp, n in reads.items() if n >= min_repeat]
        if hits:
            sessions += 1
            redundant += sum(n - 1 for n, _ in hits)
            for n, fp in hits:
                worst.append({"count": n, "file": fp.replace(home, "~"), "session": jf.stem})
    worst.sort(key=lambda r: -r["count"])
    return {"sessions": sessions, "redundant": redundant, "worst": worst[:8]}

# ---------- live sessions (what is running right now) ----------
# Two independent facts, deliberately not joined into one:
#   1. interactive `claude` processes (pid, tty, cwd, uptime) — from ps/lsof
#   2. sessions whose transcript was written to in the last few minutes, with their tokens
# Claude Code neither keeps its transcript open nor exposes CLAUDE_SESSION_ID to other processes
# (checked: no .jsonl in lsof, `ps eww` hides the env), so a pid cannot be tied to a session id with
# certainty. Pretending otherwise would put a wrong session's cost next to a terminal tab.
LIVE_WINDOW = 600          # a transcript touched this recently counts as a live session
CLAUDE_BIN_HINT = "claude"

@ttl_cache(8)
def live_processes():
    out = []
    try:
        ps = subprocess.run(["ps", "-Ao", "pid=,tty=,lstart=,command="], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return out
    for line in ps.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2: continue
        pid, rest = parts
        try: bits = rest.split(None, 6)
        except Exception: continue
        if len(bits) < 6: continue
        tty = bits[0]; started = " ".join(bits[1:6]); cmd = bits[6] if len(bits) > 6 else ""
        base = cmd.split()[0].split("/")[-1] if cmd.strip() else ""
        if base != CLAUDE_BIN_HINT: continue
        if tty in ("??", "?"): continue                       # daemon / bg-pty helpers, not a session
        if any(k in cmd for k in (" daemon ", "bg-pty-host", "bg-spare", "mcp ")): continue
        cwd = ""
        try:
            lf = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=8).stdout
            for ln in lf.splitlines():
                if ln.startswith("n"): cwd = ln[1:]; break
        except Exception: pass
        started_ts = 0.0
        try: started_ts = datetime.strptime(started + " " + str(datetime.now().year), "%a %b %d %H:%M:%S %Y").timestamp()
        except Exception:
            try: started_ts = datetime.strptime(started, "%a %b %d %H:%M:%S %Y").timestamp()
            except Exception: pass
        out.append({"pid": int(pid), "tty": tty, "cwd": cwd, "project": project_label(cwd) if cwd else "—",
                    "started": fmt_when(started_ts) if started_ts else started,
                    "uptime": human_since(started_ts) if started_ts else "",
                    "uptime_s": int(time.time() - started_ts) if started_ts else 0})
    out.sort(key=lambda r: r["tty"])
    return out

def human_since(ts):
    if not ts: return ""
    s = max(0, time.time() - ts)
    if s < 60: return "%ds" % int(s)
    if s < 3600: return "%dm" % int(s // 60)
    if s < 86400: return "%dh %dm" % (int(s // 3600), int((s % 3600) // 60))
    return "%dd %dh" % (int(s // 86400), int((s % 86400) // 3600))

def live_sessions():
    """Sessions written to inside LIVE_WINDOW, with their real token spend so far."""
    pdir = CLAUDE / "projects"
    if not pdir.exists(): return []
    now = time.time(); out = []
    for jf in pdir.glob("*/*.jsonl"):
        try: st = jf.stat()
        except OSError: continue
        if now - st.st_mtime > LIVE_WINDOW: continue
        rows = parse_transcript_usage(str(jf))
        if not rows: continue
        billable = sum(r["input_tokens"] + r["output_tokens"] + r["cache_creation"] + r["cache_read"] for r in rows)
        cost = sum(cost_for(r["model"], r["input_tokens"], r["output_tokens"], r["cache_creation"], r["cache_read"]) for r in rows)
        last = rows[-1]
        context = last["input_tokens"] + last["cache_read"] + last["cache_creation"]
        proj = jf.parent.name
        proj = proj.replace("-", "/") if proj.startswith("-") else proj
        # a title from the session's first prompt: every home-dir session was showing as "~ (home)",
        # which made three different conversations look like one
        _m = _conv_meta(str(jf)) or {}
        title = (load_labels().get(jf.stem) or {}).get("name") or _m.get("title") or _m.get("preview") or ""
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) > 74: title = title[:74].rstrip() + "…"
        out.append({"session": jf.stem, "title": title or "(no prompt yet)",
                    "project": project_label(proj), "project_path": proj,
                    "turns": len(rows), "tokens": billable, "tokens_h": fmt_num(billable),
                    "cost": round(cost, 2), "cost_h": "$%.2f" % cost,
                    "context": context, "context_h": fmt_num(context),
                    "model": model_label(last["model"]),
                    "idle": human_since(st.st_mtime), "idle_s": int(now - st.st_mtime),
                    "last": fmt_when(st.st_mtime), "path": str(jf)})
    out.sort(key=lambda r: r["idle_s"])
    return out

@app.get("/api/live")
def api_live():
    procs = live_processes(); sess = live_sessions()
    per_project = {}
    for p in procs: per_project[p["project"]] = per_project.get(p["project"], 0) + 1
    return {"processes": procs, "sessions": sess,
            "totals": {"processes": len(procs), "sessions": len(sess),
                       "tokens": sum(s["tokens"] for s in sess),
                       "tokens_h": fmt_num(sum(s["tokens"] for s in sess)),
                       "cost_h": "$%.2f" % sum(s["cost"] for s in sess)},
            "by_project": per_project, "window_min": LIVE_WINDOW // 60}

# ---------- live usage limits (the same numbers Claude's Settings → Usage panel shows) ----------
# Source of truth: GET https://api.anthropic.com/api/oauth/usage with the Claude Code OAuth token
# from the macOS keychain (the identical endpoint + credential the CLI itself uses). If the token
# can't be read (keychain prompt declined, Linux, logged out) we fall back to the copy Claude Code
# already cached in ~/.claude.json → cachedUsageUtilization, and say so in the UI.
USAGE_API = "https://api.anthropic.com/api/oauth/usage"
_tok_cache = {"tok": "", "at": 0}
_limits_cache = {"at": 0, "val": None}

def _oauth_token():
    """Claude Code's OAuth access token, read from the login keychain. Kept in memory for 10
    minutes, never written to disk, never logged."""
    if _tok_cache["tok"] and time.time() - _tok_cache["at"] < 600: return _tok_cache["tok"]
    tok = ""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=25)
        d = json.loads((r.stdout or "").strip() or "{}")
        tok = ((d.get("claudeAiOauth") or {}).get("accessToken") or "").strip()
    except Exception: tok = ""
    if tok: _tok_cache.update({"tok": tok, "at": time.time()})
    return tok

def _fetch_usage_live():
    tok = _oauth_token()
    if not tok: return None, "keychain: could not read the Claude Code credential"
    try:
        import urllib.request
        req = urllib.request.Request(USAGE_API, headers={
            "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json", "User-Agent": "my-claude-dashboard"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

def _cached_utilization():
    try:
        d = json.loads((HOME / ".claude.json").read_text())
        c = d.get("cachedUsageUtilization") or {}
        u = c.get("utilization")
        if u: return u, (c.get("fetchedAtMs") or 0) / 1000
    except Exception: pass
    return None, 0

def _plan_label():
    try:
        oa = (json.loads((HOME / ".claude.json").read_text()).get("oauthAccount") or {})
    except Exception: return ""
    if oa.get("organizationType") == "claude_team": return "Team"
    tier = (oa.get("userRateLimitTier") or "").replace("default_claude_", "").replace("_", " ")
    return tier.title()

def _reset_text(iso):
    """'Resets in 7 min' for the near term, 'Resets Mon 4:29 PM' further out (Claude's own wording)."""
    if not iso: return ""
    try: dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
    except Exception: return ""
    secs = (dt - datetime.now().astimezone()).total_seconds()
    if secs <= 0: return "Resets now"
    if secs < 3600: return f"Resets in {int(secs // 60)} min"
    if secs < 24 * 3600:
        h = int(secs // 3600); m = int((secs % 3600) // 60)
        return f"Resets in {h}h" + (f" {m}m" if m else "")
    return "Resets " + dt.strftime("%a %-I:%M %p")

def _norm_limits(u, source, fetched_at):
    rows = []
    for lim in (u.get("limits") or []):
        kind = lim.get("kind") or ""
        scope = (lim.get("scope") or {}).get("model") or {}
        if kind == "session": label, group = "Current session", "session"
        elif kind == "weekly_all": label, group = "All models", "weekly"
        elif kind == "weekly_scoped": label, group = (scope.get("display_name") or "Scoped"), "weekly"
        else: label, group = kind.replace("_", " ").title(), (lim.get("group") or "other")
        pct = lim.get("percent")
        rows.append({"label": label, "group": group, "kind": kind,
                     "pct": 0 if pct is None else round(float(pct), 1),
                     "resets_at": lim.get("resets_at"), "resets": _reset_text(lim.get("resets_at")),
                     "severity": lim.get("severity") or "normal", "active": bool(lim.get("is_active")),
                     "unused": (pct in (0, 0.0, None)) and not lim.get("resets_at")})
    order = {"session": 0, "weekly": 1}
    rows.sort(key=lambda r: (order.get(r["group"], 2), -r["pct"]))
    ex = u.get("extra_usage") or {}
    spend = u.get("spend") or {}
    return {"ok": bool(rows), "source": source, "fetched_at": fetched_at, "rows": rows,
            "plan": _plan_label(),
            "extra": {"enabled": bool(ex.get("is_enabled")), "used_credits": ex.get("used_credits"),
                      "monthly_limit": ex.get("monthly_limit"), "utilization": ex.get("utilization"),
                      "disabled_reason": ex.get("disabled_reason")},
            "spend": {"enabled": bool(spend.get("enabled")), "percent": spend.get("percent"),
                      "used_minor": ((spend.get("used") or {}).get("amount_minor")),
                      "currency": ((spend.get("used") or {}).get("currency") or "USD")}}

def live_limits(force=False):
    if not force and _limits_cache["val"] and time.time() - _limits_cache["at"] < 60:
        return _limits_cache["val"]
    u, err = _fetch_usage_live()
    if u:
        val = _norm_limits(u, "live", time.time())
    else:
        cu, at = _cached_utilization()
        if cu:
            val = _norm_limits(cu, "cache", at); val["error"] = err
        else:
            val = {"ok": False, "source": "none", "error": err, "rows": [], "plan": _plan_label(),
                   "fetched_at": 0, "extra": {}, "spend": {}}
    _limits_cache.update({"at": time.time(), "val": val})
    return val

# ---------- full-text search index (SQLite FTS5) ----------
# The old search re-grepped every transcript on every query, capped at 500 hits, and could only link
# to a whole conversation. This keeps an incremental index (mtime+size keyed, same idea as the other
# caches) so queries are instant, uncapped in practice, and carry the message index for deep links.
SEARCH_DB = SKILL_DIR / ".mc_search.db"
_search_lock = threading.Lock()

def _search_conn():
    con = sqlite3.connect(SEARCH_DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mt REAL, sz INTEGER)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5("
                "text, path UNINDEXED, idx UNINDEXED, role UNINDEXED, ts UNINDEXED, "
                "project UNINDEXED, tokenize='porter unicode61')")
    return con

def search_available():
    try:
        with sqlite3.connect(":memory:") as c:
            c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except Exception:
        return False

def build_search_index(force=False):
    """Index every transcript message. Only files whose (mtime, size) changed are re-read."""
    if not search_available(): return {"ok": False, "reason": "sqlite build has no FTS5"}
    pdir = CLAUDE / "projects"
    if not pdir.exists(): return {"ok": False, "reason": "no projects dir"}
    with _search_lock:
        con = _search_conn()
        try:
            if force:
                con.execute("DELETE FROM msgs"); con.execute("DELETE FROM files")
            known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, mt, sz FROM files")}
            seen = set(); changed = 0; added = 0
            for jf in pdir.rglob("*.jsonl"):
                p = str(jf); seen.add(p)
                try: st = jf.stat()
                except OSError: continue
                if known.get(p) == (st.st_mtime, st.st_size): continue
                proj = jf.relative_to(pdir).parts[0]
                proj = proj.replace("-", "/") if proj.startswith("-") else proj
                rows = []
                for i, m in enumerate(parse_transcript_messages(p, max_messages=100000)):
                    rows.append((m["text"], p, i, m["role"], m["ts"] or "", proj))
                con.execute("DELETE FROM msgs WHERE path = ?", (p,))
                con.executemany("INSERT INTO msgs(text, path, idx, role, ts, project) VALUES (?,?,?,?,?,?)", rows)
                con.execute("INSERT OR REPLACE INTO files(path, mt, sz) VALUES (?,?,?)", (p, st.st_mtime, st.st_size))
                changed += 1; added += len(rows)
            for gone in set(known) - seen:
                con.execute("DELETE FROM msgs WHERE path = ?", (gone,))
                con.execute("DELETE FROM files WHERE path = ?", (gone,))
            con.commit()
            total = con.execute("SELECT count(*) FROM msgs").fetchone()[0]
            return {"ok": True, "files_reindexed": changed, "messages_added": added, "messages_total": total}
        finally:
            con.close()

def _fts_query(q):
    """User text -> FTS5 expression. Quoted terms are AND-ed; keeps punctuation-heavy queries valid."""
    terms = [t for t in re.findall(r'"[^"]+"|\S+', q.strip()) if t.strip('"')]
    return " AND ".join('"' + t.strip('"').replace('"', '""') + '"' for t in terms)

def search_messages(q, limit=500):
    """Ranked message hits: one row per matching message, with its index inside the transcript."""
    expr = _fts_query(q)
    if not expr: return []
    build_search_index()
    con = _search_conn()
    try:
        cur = con.execute(
            "SELECT path, idx, role, ts, project, snippet(msgs, 0, '', '', '…', 14) "
            "FROM msgs WHERE msgs MATCH ? ORDER BY rank LIMIT ?", (expr, limit))
        return [{"path": r[0], "idx": r[1], "role": r[2], "ts": r[3], "project": r[4], "snippet": r[5]}
                for r in cur]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()

@ttl_cache(900)
def search_transcripts(needle, max_hits=200):
    needle_lc = needle.lower(); out = []; pdir = CLAUDE / "projects"
    if not pdir.exists(): return out
    for proj in pdir.iterdir():
        if not proj.is_dir(): continue
        decoded = proj.name.replace("-", "/") if proj.name.startswith("-") else proj.name
        for jf in proj.glob("*.jsonl"):
            try:
                with open(jf, "r", errors="ignore") as f:
                    for ln, line in enumerate(f, 1):
                        if needle_lc not in line.lower(): continue
                        try: obj = json.loads(line)
                        except Exception: snippet = line.strip()[:200]
                        else:
                            msg = obj.get("message") or obj
                            content = msg.get("content") if isinstance(msg, dict) else None
                            text = ""
                            if isinstance(content, str): text = content
                            elif isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text": text += c.get("text", "") + " "
                            snippet = re.sub(r"\s+", " ", text or line)[:240]
                        out.append({"project": decoded, "session": jf.name.split(".")[0], "line": ln, "snippet": snippet, "path": str(jf)})
                        if len(out) >= max_hits: return out
            except Exception: continue
    return out

# ---------- shared context ----------
def theme_of(request): return request.cookies.get("theme", "light")

def base_ctx(request, page):
    auth = gh_auth_status(); cfg = load_config_dict()
    if auth["gh_present"] and auth["logged_in"]:
        chip = {"cls": "", "text": "GitHub: " + (auth["user"] or "connected")}
    elif auth["gh_present"]:
        chip = {"cls": "warn", "text": "GitHub not connected"}
    else:
        chip = {"cls": "warn", "text": "gh CLI not installed"}
    try: asset_ver = str(int((SKILL_DIR / "static" / "app.css").stat().st_mtime))
    except Exception: asset_ver = "1"
    return {"request": request, "theme": theme_of(request), "claude_home": str(CLAUDE),
            "pages": PAGES, "page_slug": PAGE_SLUG, "active": page, "chip": chip,
            "eyebrow": EYEBROW.get(page, ""), "today": datetime.now().date().isoformat(),
            "updated": datetime.now().strftime("%H:%M"), "editor": editor_name(), "asset_ver": asset_ver}

def render(request, template, page, **ctx):
    c = base_ctx(request, page); c.update(ctx)
    return templates.TemplateResponse(request, template, c)

# ---------- routes: pages ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request): return RedirectResponse("/p/overview")

@app.get("/toggle-theme")
def toggle_theme(request: Request, next: str = "/p/overview"):
    cur = theme_of(request); resp = RedirectResponse(next)
    resp.set_cookie("theme", "dark" if cur == "light" else "light", max_age=31536000)
    return resp

def warm_caches():
    """Pre-compute the expensive scans so no tab switch ever pays for a cold cache."""
    try:
        _sizes(); load_skills(); conversation_index(); conversation_index(True)
        archived_sessions(); archived_slash_commands(); skill_usage(); token_insights()
        aggregate_usage(); usage_stats(); load_cache(); load_memory()
        available_terminals(); gh_auth_status(); build_search_index()
    except Exception: pass

def rebuild_caches():
    """Refresh every derived value in dependency order, swapping each in when it is ready.

    Order matters: a loader recomputed here still reads its dependencies through the cache, so
    aggregate_usage has to land before usage_stats, and archived/index before skill_usage.
    """
    # per-path helpers first: load_skills reads sizes and mtimes through them, so leaving their
    # entries in place would rebuild the skill list with yesterday's timestamps
    for name in ("latest_mtime", "dir_size"):
        for k in [k for k in list(_CACHE) if k[0] == name]: _CACHE.pop(k, None)
    for fn, args in ((conversation_index, ()), (conversation_index, (True,)),
                     (load_skills, ()), (archived_sessions, ()), (archived_slash_commands, ()),
                     (skill_usage, ()), (aggregate_usage, ()), (usage_stats, ()), (token_insights, ()),
                     (load_cache, ()), (load_memory, ()), (_sizes, ())):
        try: refresh_entry(fn, *args)
        except Exception: pass
    try: build_search_index()
    except Exception: pass

def warm_async():
    threading.Thread(target=warm_caches, daemon=True).start()

@app.on_event("startup")
def _on_startup():
    warm_async(); threading.Thread(target=_auto_backup_loop, daemon=True).start()

@app.get("/refresh")
def refresh(request: Request, next: str = "/p/overview"):
    clear_cache(); _limits_cache["at"] = 0; warm_caches(); return RedirectResponse(next)

@app.get("/p/{slug}", response_class=HTMLResponse)
def page(request: Request, slug: str):
    if slug in REDIRECTS:
        qs = ("?" + str(request.url.query)) if request.url.query else ""
        return RedirectResponse(f"/p/{REDIRECTS[slug]}{qs}")
    name = SLUG_PAGE.get(slug)
    if not name: return RedirectResponse("/p/overview")
    ensure_fresh()   # spots new transcripts/skills and rebuilds in the background
    if name == "Overview": return render_overview(request)
    if name == "Skills": return render_skills(request)
    if name == "MCP & Plugins": return render_extensions(request)
    if name == "Usage": return render_token(request)
    if name == "History": return render_history(request)
    if name == "System": return render_system(request)
    if name == "Help": return render_help(request)
    return RedirectResponse("/p/overview")

def _sizes():
    skills = load_skills(); projects = load_projects(); cfg = load_config_files(); cache = load_cache(); tasks = load_tasks()
    return {"Skills": sum(s["size"] for s in skills), "Sessions": sum(p["size"] for p in projects),
            "Config": sum(c["size"] for c in cfg), "Cache": sum(c["size"] for c in cache),
            "Tasks": sum(t["size"] for t in tasks), "Plugins": dir_size(str(CLAUDE / "plugins"))}

def render_overview(request):
    skills = load_skills(); projects = load_projects(); convs = conversation_index()
    connectors = load_connectors(); plugins = load_plugins(); cache = load_cache()
    n_mcp = len({c["name"] for c in connectors})
    sizes = _sizes(); total = sum(sizes.values()); now = datetime.now().timestamp()

    us = usage_stats()
    tokens30 = fmt_num(us["tok30"]) if not us.get("empty") else "—"
    cost30 = f"${us['cost30']:,.0f} · 30d" if not us.get("empty") else ""

    # this-week strip
    week = None
    if not us.get("empty"):
        delta = None
        if us["cost_prev7"] > 0:
            delta = (us["cost7"] - us["cost_prev7"]) / us["cost_prev7"] * 100
        proj_msgs = {}
        for c in convs: proj_msgs[c["project"]] = proj_msgs.get(c["project"], 0) + c["n"]
        busiest = max(proj_msgs.items(), key=lambda x: x[1]) if proj_msgs else (None, 0)
        week = {
            "cost7": f"${us['cost7']:,.2f}", "delta": (None if delta is None else round(delta)),
            "savings": f"${us['savings']:,.0f}",
            "busiest": (project_label(busiest[0]) if busiest[0] else "—"), "busiest_n": busiest[1],
            "top_day": us["top_day"], "heat": json.dumps(us["heat"]),
        }

    kpis = [
        {"label": "Skills", "value": str(len(skills)), "sub": fmt_size(sizes["Skills"]), "href": "/p/skills"},
        {"label": "Conversations", "value": str(len(convs)), "sub": f"{sum(c['n'] for c in convs)} messages", "href": "/p/history"},
        {"label": "Tokens · 30d", "value": tokens30, "sub": cost30, "href": "/p/usage?win=30+days#measured"},
        {"label": "On disk", "value": fmt_size(total), "sub": f"{n_mcp} MCP · {len(plugins)} plugins", "href": "/p/mcp-plugins"},
    ]
    recent_convs = [{"preview": c.get("title") or c["preview"], "project": c["project"].split("/")[-1] or c["project"],
                     "when": fmt_when(c["last_ts"]) if c["last_ts"] else "—", "n": c["n"], "path": c["path"]}
                    for c in convs[:5]]
    recent_skills = sorted(skills, key=lambda s: -s["mtime"])[:5]

    # attention items (folded from Cleanup)
    stale = [p for p in projects if p["last_active"] and (now - p["last_active"]) / 86400 > 90]
    attention = []
    if stale:
        attention.append({"text": f"{len(stale)} stale projects (>90d inactive)",
                          "sub": f"{fmt_size(sum(p['size'] for p in stale))} reclaimable", "path": str(CLAUDE / "projects")})
    big = sorted(cache, key=lambda r: -r["size"])[:1]
    if big and big[0]["size"] > 50 * 1024 * 1024:
        attention.append({"text": f"Largest cache: {big[0]['name']}", "sub": big[0]["size_human"], "path": big[0]["path"]})

    bd = sorted([(k, v) for k, v in sizes.items() if v > 0], key=lambda x: x[1])
    chart = {"y": [k for k, v in bd], "x": [v for k, v in bd],
             "text": [fmt_size(v) for k, v in bd], "colors": [CAT_COLOR[k] for k, v in bd]}
    return render(request, "overview.html", "Overview", kpis=kpis, week=week, recent_convs=recent_convs,
                  recent_skills=recent_skills, attention=attention, chart=json.dumps(chart))

def _mcp_groups():
    """Group MCP registrations by server name (same server can be registered in several scopes)."""
    groups = {}
    for c in load_connectors():
        g = groups.setdefault(c["name"], {"name": c["name"], "scopes": [], "config": c["config"], "source": c["source"]})
        g["scopes"].append(c["scope"])
    return groups

def render_extensions(request):
    tab = request.query_params.get("tab", "mcp")
    q = request.query_params.get("q", "").lower()
    plugins = load_plugins(); groups = _mcp_groups()
    conn_shown = []
    for name, g in groups.items():
        if q and q not in name.lower(): continue
        has_global = "global" in g["scopes"]
        nproj = len([s for s in g["scopes"] if s != "global"])
        parts = (["global"] if has_global else []) + ([f"{nproj} project" + ("s" if nproj != 1 else "")] if nproj else [])
        conn_shown.append({"name": name, "scope_label": " · ".join(parts) or g["scopes"][0],
                           "scopes": g["scopes"], "source": g["source"], "key": name,
                           "config": json.dumps(g["config"], indent=2)})
    plug_shown = [{"name": p["name"], "info": json.dumps(p["info"], indent=2)}
                  for p in plugins if not q or q in p["name"].lower()]
    return render(request, "extensions.html", "MCP & Plugins", tab=tab, q=request.query_params.get("q", ""),
                  connectors=conn_shown, plugins=plug_shown, n_conn=len(groups), n_plug=len(plugins))

@app.get("/api/mcp-health")
def api_mcp_health():
    from concurrent.futures import ThreadPoolExecutor
    groups = list(_mcp_groups().values())
    def chk(g):
        status, detail = mcp_check(g["name"], g["config"])
        return {"key": g["name"], "status": status, "detail": detail}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(chk, groups))

EDITED_WINDOWS = {"Any edit date": 0, "Edited last 7 days": 7, "Edited last 30 days": 30}

def render_skills(request):
    cfg = load_config_dict(); auth = gh_auth_status(); skills = load_skills(); usage = skill_usage()
    q = request.query_params.get("q", ""); source = request.query_params.get("source", "All")
    sort = request.query_params.get("sort", "Recently used")
    # Edit-date filter. Was a bare "Recent" toggle: it read as a mystery switch (and defaulted ON,
    # hiding 689 of 698 skills), so it is now a named dropdown that says what it filters on.
    edited = request.query_params.get("edited", "")
    if edited not in EDITED_WINDOWS:
        _rv = request.query_params.getlist("recent")   # legacy links: recent=1 meant 7 days
        edited = "Edited last 7 days" if (_rv and _rv[-1] == "1") else "Any edit date"
    recent = "1" if edited != "Any edit date" else "0"
    cats_sel = [c for c in request.query_params.getlist("cat") if c and c != "All"]
    try: page_n = int(request.query_params.get("page", "1"))
    except ValueError: page_n = 1
    now = datetime.now().timestamp(); recent_threshold = now - 7 * 24 * 3600
    edited_cutoff = now - EDITED_WINDOWS[edited] * 24 * 3600 if EDITED_WINDOWS[edited] else 0
    pushed = cfg.get("pushed_skills") or {}
    for s in skills:
        u = usage.get(s["name"], {})
        s["uses"] = u.get("count", 0); s["last_used"] = u.get("last_ts", 0)
        s["is_recent"] = s["mtime"] > recent_threshold; s["pushed_url"] = pushed.get(s["name"], "")
        # full date + time, so "used" answers when exactly (was date only)
        s["last_used_h"] = fmt_when(s["last_used"]) if s["last_used"] else "never"
        s["edited_h"] = fmt_when(s["mtime"])          # shown next to "used", so the two can't be confused
        s["uses_hist"] = u.get("from_history", 0)     # invocations recovered from rotated-away sessions
    total = len(skills)
    cats = sorted({s["cat"] for s in skills})
    f = list(skills)
    if source == "Local": f = [s for s in f if s["source"] == "local"]
    elif source == "Plugin": f = [s for s in f if s["source"] == "plugin"]
    elif source == "Unused": f = [s for s in f if s["uses"] == 0]
    if cats_sel: f = [s for s in f if s["cat"] in cats_sel]
    if edited_cutoff: f = [s for s in f if s["mtime"] > edited_cutoff]
    if q:
        ql = q.lower(); f = [s for s in f if ql in s["name"].lower() or ql in s["description"].lower()]
    if sort == "Recently used":
        # default order: everything you have actually used, newest use first; never-used skills after
        f.sort(key=lambda s: (0 if s["last_used"] else 1, -s["last_used"], s["name"].lower()))
    elif sort == "Name (A-Z)": f.sort(key=lambda s: s["name"].lower())
    elif sort == "Size (high to low)": f.sort(key=lambda s: -s["size"])
    elif sort == "Recently modified": f.sort(key=lambda s: -s["mtime"])
    elif sort == "Most used": f.sort(key=lambda s: (-s["uses"], s["name"].lower()))
    elif sort == "Last used": f.sort(key=lambda s: -s["last_used"])
    user_n = sum(1 for s in skills if s["source"] == "local"); plugin_n = total - user_n
    unused_n = sum(1 for s in skills if s["uses"] == 0)
    filt_size = fmt_size(sum(s["size"] for s in f))
    PAGE_SIZE = 30; total_pages = max(1, (len(f) + PAGE_SIZE - 1) // PAGE_SIZE)
    page_n = min(max(1, page_n), total_pages)
    chunk = f[(page_n-1)*PAGE_SIZE: page_n*PAGE_SIZE]
    # Push works as soon as GitHub is connected: the owner defaults to your connected account
    # (you can override it with a default owner/org in Settings, but you don't have to).
    can_push = auth["logged_in"] and bool(cfg.get("github_owner") or auth.get("user"))
    push_msg = ""
    if not can_push:
        push_msg = "Push disabled: connect your GitHub account in Settings."
    return render(request, "skills.html", "Skills", skills=chunk, count=len(f), total=total,
                  filt_size=filt_size, user_n=user_n, plugin_n=plugin_n, unused_n=unused_n, q=q, source=source, sort=sort,
                  recent=recent, edited=edited, edited_opts=list(EDITED_WINDOWS),
                  cats_sel=cats_sel, cats=cats, page_n=page_n, total_pages=total_pages, can_push=can_push, push_msg=push_msg)

def render_connectors(request):
    items = load_connectors(); q = request.query_params.get("q", "").lower()
    health = request.query_params.get("health", "")
    shown = []
    for c in items:
        if q and q not in c["name"].lower(): continue
        rec = {"name": c["name"], "source": c["source"], "config": json.dumps(c["config"], indent=2)}
        if health:
            status, detail = mcp_check(c["name"], c["config"])
            rec["status"] = status; rec["detail"] = detail
            rec["status_color"] = {"ok": "#10b981", "fail": "#ef4444", "unknown": "#94a3b8"}[status]
        shown.append(rec)
    return render(request, "connectors.html", "MCP Connectors", items=shown, total=len(items), q=q, health=health)

def render_plugins(request):
    items = load_plugins(); q = request.query_params.get("q", "").lower()
    shown = [{"name": p["name"], "info": json.dumps(p["info"], indent=2)} for p in items if not q or q in p["name"].lower()]
    return render(request, "plugins.html", "Plugins", items=shown)

def render_sessions(request):
    projects = load_projects(); q = request.query_params.get("q", "").lower()
    f = projects if not q else [p for p in projects if q in p["project"].lower()]
    for p in f:
        p["last_active_h"] = fmt_when(p["last_active"]) if p["last_active"] else "—"
    filt_size = fmt_size(sum(p["size"] for p in f))
    return render(request, "sessions.html", "Sessions", projects=f, count=len(f), total=len(projects),
                  filt_size=filt_size, q=q)

@app.get("/p/sessions/{slug}/transcripts", response_class=HTMLResponse)
def session_transcripts(request: Request, slug: str):
    """Lazy-loaded transcript list for a project (HTML partial)."""
    storage = CLAUDE / "projects" / slug
    project_real = slug.replace("-", "/") if slug.startswith("-") else slug
    sessions = list_project_sessions(str(storage))
    for s in sessions:
        s["label"] = transcript_label(s["path"]) or "(unnamed conversation)"
        s["short_id"] = s["name"].split(".")[0][:8]; s["sid_full"] = s["name"].split(".")[0]
        s["when"] = fmt_when(s["mtime"]); s["size_h"] = fmt_size(s["size"])
    return templates.TemplateResponse(request, "_transcripts.html", {"request": request, "theme": theme_of(request),
        "sessions": sessions, "slug": slug, "project_real": project_real,
        "real_exists": Path(project_real).exists(), "editor": editor_name()})

def render_search(request):
    q = request.query_params.get("q", "").strip()
    ctx = {"q": q, "hits": None}
    if q:
        hits = search_transcripts(q); groups = {}
        for h in hits: groups.setdefault((h["project"], h["session"], h["path"]), []).append(h)
        sessions = [{"project": k[0], "sid": k[1], "sid_short": k[1][:8], "path": k[2],
                     "matches": v[:30], "more": max(0, len(v) - 30), "count": len(v)} for k, v in groups.items()]
        ctx.update({"hits": len(hits), "session_count": len({h["session"] for h in hits}), "groups": sessions})
    return render(request, "search.html", "Search", editor=editor_name(), **ctx)

def render_cleanup(request):
    projects = load_projects(); cache = load_cache(); now = datetime.now().timestamp()
    stale = []
    for p in projects:
        age = int(round((now - p["last_active"]) / 86400)) if p["last_active"] else 99999
        if age > 90: stale.append({**p, "age_days": age})
    stale.sort(key=lambda r: -r["size"])
    recoverable = fmt_size(sum(s["size"] for s in stale))
    big = sorted(cache, key=lambda r: -r["size"])[:10]
    return render(request, "cleanup.html", "Cleanup", stale=stale, recoverable=recoverable, big=big,
                  projects_dir=str(CLAUDE / "projects"))

def render_history(request):
    """History & Cache — merges the history-viewer conversation browser with the cache table."""
    q = request.query_params.get("q", "").strip().lower()
    win = request.query_params.get("win", "All time")
    year = request.query_params.get("year", "All")
    include_sub = request.query_params.get("sub") == "1"
    show_arch = request.query_params.get("arch", "1") != "0"
    convs = list(conversation_index(include_sub))
    arch = archived_sessions()
    arch_n = len(arch)
    if show_arch:
        convs = sorted(convs + arch, key=lambda c: c["last_ts"] or 0, reverse=True)
    years = sorted({datetime.fromtimestamp(c["last_ts"]).year for c in convs if c["last_ts"]}, reverse=True)
    if year != "All":
        try:
            yi = int(year); convs = [c for c in convs if c["last_ts"] and datetime.fromtimestamp(c["last_ts"]).year == yi]
        except ValueError: pass
    elif win != "All time":
        days = 30 if win == "30 days" else 7
        cutoff = (datetime.now() - timedelta(days=days)).timestamp()
        convs = [c for c in convs if (c["last_ts"] or 0) >= cutoff]
    content_hits = None; search_capped = False; match_counts = {}; first_match = {}
    if q:
        # full-text search folded in: match on preview/project OR message body,
        # ranked so title/preview matches come first, then content-only matches, each by recency
        SEARCH_CAP = 2000
        hits = search_messages(q, limit=SEARCH_CAP)
        search_capped = len(hits) >= SEARCH_CAP   # surfaced in the UI: never truncate silently
        for h in hits:
            match_counts[h["path"]] = match_counts.get(h["path"], 0) + 1
            first_match.setdefault(h["path"], h["idx"])
        hit_paths = set(match_counts)
        content_hits = len(hit_paths)
        def _title_match(c):
            hay = " ".join([c.get("title") or "", c["preview"], c["project"], " ".join("#" + t for t in c.get("tags") or [])]).lower()
            return q in hay
        matched = [c for c in convs if _title_match(c) or c["path"] in hit_paths]
        matched.sort(key=lambda c: (0 if _title_match(c) else 1, -match_counts.get(c["path"], 0), -(c["last_ts"] or 0)))
        convs = matched
    bs = usage_stats().get("by_session", {})
    shown = []
    for c in convs:   # no cap: every conversation in the filtered set is listed
        cost = bs.get(c["session"], {}).get("cost", 0)
        shown.append({**c, "when": fmt_when(c["last_ts"]) if c["last_ts"] else "—",
                      "cost_h": (f"${cost:.2f}" if cost else ""), "path_q": c["path"],
                      "hits": match_counts.get(c["path"], 0), "first_hit": first_match.get(c["path"])})
    total_msgs = sum(c["n"] for c in convs)
    try: all_files = sum(1 for _ in (CLAUDE / "projects").rglob("*.jsonl"))
    except Exception: all_files = 0
    sub_n = max(0, all_files - len(conversation_index()))  # nested sub-agent + workflow transcripts
    cache = load_cache()
    for r in cache: r["path_q"] = r["path"]
    return render(request, "history.html", "History", convs=shown, conv_total=len(convs),
                  total_msgs=total_msgs, win=win, year=year, years=years, q=request.query_params.get("q", ""), ret=retention_info(),
                  content_hits=content_hits, cache=cache, sub_n=sub_n, include_sub=include_sub,
                  arch_n=arch_n, show_arch=show_arch, search_capped=search_capped)

@app.get("/api/conversation", response_class=HTMLResponse)
def api_conversation(request: Request, path: str):
    if path.startswith("archived:"):
        sid = path.split(":", 1)[1]
        rows = archived_prompts(sid)
        project_real = rows[0]["project"] if rows else ""
        label = next((r["text"][:80] for r in rows if r["text"] and not r["text"].startswith("[Pasted")), "") \
                or (rows[0]["text"][:80] if rows else "(no prompt text)")
        messages = [{"role": "user", "text": r["text"], "ts": r["ts"]} for r in rows]
        return templates.TemplateResponse(request, "_conv_detail.html", {"request": request,
            "theme": theme_of(request), "messages": messages, "label": label, "count": len(messages),
            "path": path, "session": sid, "project_real": project_real, "cost_h": "",
            "real_exists": bool(project_real) and Path(project_real).exists(), "editor": editor_name(),
            "archived": True, "archived_src": str(HISTORY_FILE)})
    MSG_CAP = 20000
    messages = parse_transcript_messages(path, max_messages=MSG_CAP)
    p = Path(path)
    lab = load_labels().get(p.stem) or {}
    label = lab.get("name") or _conv_meta(path).get("title") or transcript_label(path) or "(unnamed conversation)"
    slug = p.parent.name
    project_real = slug.replace("-", "/") if slug.startswith("-") else slug
    sess = usage_stats().get("by_session", {}).get(p.stem, {})
    cost_h = f"${sess['cost']:.2f}" if sess.get("cost") else ""
    return templates.TemplateResponse(request, "_conv_detail.html", {"request": request,
        "theme": theme_of(request), "messages": messages, "label": label, "count": len(messages),
        "path": path, "session": p.stem, "project_real": project_real, "cost_h": cost_h,
        "real_exists": Path(project_real).exists(), "editor": editor_name(), "archived": False,
        "capped": len(messages) >= MSG_CAP, "cap": MSG_CAP, "tags": lab.get("tags") or [], "custom_name": lab.get("name") or "",
        "compactions": [i for i, m in enumerate(messages) if m["role"] == "compact"]})

def render_system(request):
    tab = request.query_params.get("tab", "config")
    return render(request, "system.html", "System", tab=tab, cfg=load_config_detail(), mem=load_memory())

@app.get("/api/memory", response_class=PlainTextResponse)
def api_memory(path: str):
    p = Path(path)
    if "/memory/" not in str(p) or p.suffix != ".md" or not str(p).startswith(str(CLAUDE)):
        return PlainTextResponse("(not a memory file)")
    try: return PlainTextResponse(p.read_text(errors="ignore"))
    except Exception: return PlainTextResponse("(could not read)")

@app.get("/api/palette")
def api_palette(q: str = ""):
    ql = q.lower().strip(); res = []
    for p in PAGES:
        if not ql or ql in p.lower(): res.append({"t": "Page", "label": p, "href": "/p/" + PAGE_SLUG[p]})
    if ql:
        for s in load_skills():
            if ql in s["name"].lower():
                res.append({"t": "Skill", "label": s["name"], "view": s["name"]})
                if len(res) > 28: break
        for c in conversation_index():
            if ql in ((c.get("title") or "") + " " + c["preview"] + " " + c["project"] + " " + " ".join("#" + t for t in c.get("tags") or [])).lower():
                res.append({"t": "Chat", "label": (c.get("title") or c["preview"])[:70],
                            "sub": c["project"].split("/")[-1], "href": "/p/history?conv=" + quote(c["path"])})
                if len(res) > 45: break
    return res[:45]

def usage_window(df, win="All time", family="All", d_from="", d_to=""):
    """The same filter the Usage page applies: model, then a custom range or the 30/7-day window."""
    if family != "All": df = df[df["label"] == family]
    if (d_from or d_to) and df["date"].notna().any():
        try:
            lo = datetime.fromisoformat(d_from).date() if d_from else datetime(2000, 1, 1).date()
            hi = datetime.fromisoformat(d_to).date() if d_to else datetime.now().date()
            return df[df["date"].apply(lambda d: bool(d) and lo <= d <= hi)]
        except ValueError: return df
    if win != "All time" and df["date"].notna().any():
        cutoff = (datetime.now() - timedelta(days=30 if win == "30 days" else 7)).date()
        df = df[df["date"].apply(lambda d: bool(d) and d >= cutoff)]
    return df

@app.get("/export/usage")
def export_usage(fmt: str = "csv", win: str = "All time", family: str = "All", d_from: str = "", d_to: str = ""):
    """Tokens per day and model for the window on screen. cost_usd_est is an API list-price estimate."""
    df = aggregate_usage()
    cols = ["date", "model", "turns", "input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "cost_usd_est"]
    out = []
    if not df.empty:
        df = usage_window(df.dropna(subset=["date"]), win, family, d_from, d_to)
        g = df.groupby(["date", "label"]).agg(turns=("model", "size"), input_tokens=("input_tokens", "sum"),
                                              cache_write_tokens=("cache_creation", "sum"), cache_read_tokens=("cache_read", "sum"),
                                              output_tokens=("output_tokens", "sum"), cost_usd_est=("cost", "sum")).reset_index()
        for r in g.sort_values(["date", "label"]).itertuples(index=False):
            out.append({"date": str(r.date), "model": r.label, "turns": int(r.turns), "input_tokens": int(r.input_tokens),
                        "cache_write_tokens": int(r.cache_write_tokens), "cache_read_tokens": int(r.cache_read_tokens),
                        "output_tokens": int(r.output_tokens), "cost_usd_est": round(float(r.cost_usd_est), 4)})
    name = f"claude-usage-{datetime.now().date().isoformat()}"
    if fmt == "json":
        return JSONResponse(out, headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
    import csv
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=cols); w.writeheader(); w.writerows(out)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})

@app.get("/export/conversation")
def export_conversation(path: str):
    if path.startswith("archived:"):
        sid = path.split(":", 1)[1]
        rows = archived_prompts(sid)
        msgs = [{"role": "user", "text": r["text"]} for r in rows]
        label = next((r["text"][:80] for r in rows if r["text"] and not r["text"].startswith("[Pasted")), "archived-session")
        src = f"{HISTORY_FILE} (archived session {sid} — prompts only)"
    else:
        msgs = parse_transcript_messages(path, max_messages=100000)
        label = transcript_label(path) or "conversation"
        src = path
    out = [f"# {label}\n", f"_Source: {src}_\n"]
    for m in msgs: out.append(f"\n## {m['role']}\n\n{m['text']}\n")
    data = "\n".join(out).encode()
    fn = (re.sub(r"[^A-Za-z0-9_.-]+", "-", label)[:50] or "conversation")
    return StreamingResponse(io.BytesIO(data), media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fn}.md"'})

def render_simple(request, title, rows, description):
    q = request.query_params.get("q", "").lower()
    f = rows if not q else [r for r in rows if q in r["name"].lower()]
    filt_size = fmt_size(sum(r["size"] for r in f))
    return render(request, "simple_table.html", title, title=title, rows=f, count=len(f), total=len(rows),
                  filt_size=filt_size, q=q, description=description)

HEAT_WEEKS = 53

def heatmap_window(today=None):
    """The activity grid's date window, computed here so it is testable: the LAST column is the week
    containing today (anchoring to the start instead once cut the grid off 6 days before today)."""
    end = today or datetime.now().date()
    last_sunday = end - timedelta(days=(end.weekday() + 1) % 7)
    start = last_sunday - timedelta(days=(HEAT_WEEKS - 1) * 7)
    return {"start": start.isoformat(), "end": end.isoformat(), "weeks": HEAT_WEEKS}

def render_token(request):
    df = aggregate_usage()
    win = request.query_params.get("win", "All time")
    family = request.query_params.get("family", "All")
    d_from = request.query_params.get("from", ""); d_to = request.query_params.get("to", "")
    _today = datetime.now().date().isoformat()  # never allow future dates
    if d_from > _today: d_from = _today
    if d_to > _today: d_to = _today
    if df.empty:
        return render(request, "token.html", "Usage", empty=True, win=win, family=family, d_from=d_from, d_to=d_to, models=["All"])
    df = df.copy()
    df["billable"] = df["input_tokens"] + df["cache_creation"] + df["cache_read"] + df["output_tokens"]
    df["context"] = df["input_tokens"] + df["cache_read"] + df["output_tokens"]
    inp = df["model"].map(lambda m: pricing_for(m)["input"]); crp = df["model"].map(lambda m: pricing_for(m)["cache_read"])
    df["savings"] = df["cache_read"] * (inp - crp) / 1_000_000
    model_list = sorted(df["label"].dropna().unique().tolist())
    if family != "All":
        df = df[df["label"] == family]
    custom = bool(d_from or d_to)
    if custom and df["date"].notna().any():
        try:
            lo = datetime.fromisoformat(d_from).date() if d_from else datetime(2000, 1, 1).date()
            hi = datetime.fromisoformat(d_to).date() if d_to else datetime.now().date()
            df = df[df["date"].apply(lambda d: bool(d) and lo <= d <= hi)]
        except ValueError: pass
    elif win != "All time" and df["date"].notna().any():
        days = 30 if win == "30 days" else 7
        cutoff = (datetime.now() - timedelta(days=days)).date()
        df = df[df["date"].apply(lambda d: bool(d) and d >= cutoff)]
    if df.empty:
        return render(request, "token.html", "Usage", empty=True, win=win, family=family, d_from=d_from, d_to=d_to, empty_window=True, models=["All"] + model_list)
    cache_total = int(df["cache_read"].sum() + df["cache_creation"].sum())
    cache_hit = (df["cache_read"].sum() / cache_total * 100) if cache_total else 0
    ndays = max(1, df["date"].dropna().nunique())
    proj_monthly = df["cost"].sum() / ndays * 30
    dd = df.dropna(subset=["date"])
    # billable tokens per day, not turns: a day of 3 huge Opus turns matters more than 30 tiny ones
    heat = [{"date": str(d), "v": int(v)} for d, v in dd.groupby("date")["billable"].sum().items()]
    heat_win = heatmap_window()
    # Real, measured usage leads. The dollar figures are API-list-price arithmetic — you are on a
    # subscription, so they are not money you spent; they live in the collapsible section below.
    metrics1 = [("Billable tokens", fmt_num(df["billable"].sum())), ("Context tokens", fmt_num(df["context"].sum())),
                ("Output", fmt_num(df["output_tokens"].sum())), ("Turns logged", fmt_num(len(df)))]
    metrics2 = [("Cache hit rate", f"{cache_hit:.1f}%"), ("Cached tokens reused", fmt_num(df["cache_read"].sum())),
                ("Sessions", fmt_num(df["session"].nunique())), ("Days active", fmt_num(ndays))]
    cost_metrics = [("At API list prices", f"${df['cost'].sum():,.2f}"),
                    ("Same rate for 30 days", f"${proj_monthly:,.0f}"),
                    ("Saved by cache hits", f"${df['savings'].sum():,.0f}")]
    day_chart = None
    if df["date"].notna().any():
        bd = df.dropna(subset=["date"]).groupby(["date", "label"], as_index=False).agg(tokens=("billable", "sum"))
        traces = []
        for lbl in sorted(bd["label"].unique().tolist()):
            sub = bd[bd["label"] == lbl]
            if not sub.empty:
                traces.append({"name": lbl, "x": [str(d) for d in sub["date"]], "y": [int(t) for t in sub["tokens"]], "color": label_color(lbl)})
        day_chart = json.dumps(traces)
    by_proj = df.groupby("project", as_index=False).agg(tokens=("billable", "sum"), cost=("cost", "sum"), turns=("project", "count")).sort_values("cost", ascending=False)
    proj_rows = [{"project": r["project"], "q": r["project"].split("/")[-1], "tokens": fmt_num(r["tokens"]), "cost": f"${r['cost']:.2f}", "turns": int(r["turns"])} for _, r in by_proj.iterrows()]
    by_model = df.groupby("label", as_index=False).agg(cost=("cost", "sum")).sort_values("cost", ascending=False)
    pie = {"labels": list(by_model["label"]), "values": [round(float(c), 2) for c in by_model["cost"]],
           "colors": [label_color(f) for f in by_model["label"]]}
    # per-model list prices actually used for these estimates (models present in the data, most-used first)
    price_rows = [{"model": lbl, **(MODEL_PRICING.get(lbl) or TIER_PRICING.get(model_family(lbl), TIER_PRICING["sonnet"]))}
                  for lbl in by_model["label"]]
    return render(request, "token.html", "Usage", empty=False, win=win, family=family, d_from=d_from, d_to=d_to,
                  metrics1=metrics1, metrics2=metrics2, cost_metrics=cost_metrics, day_chart=day_chart, proj_rows=proj_rows,
                  pie=json.dumps(pie), price_rows=price_rows,
                  heat=json.dumps(heat), heat_win=json.dumps(heat_win), models=["All"] + model_list,
                  insights=token_insights())

def render_sunburst(request):
    rows = []
    for r in load_skills():    rows.append(("Skills", r["name"], int(r["size"]), r["path"]))
    for r in load_projects():  rows.append(("Sessions", r["project"], int(r["size"]), r["path"]))
    for r in load_config_files(): rows.append(("Config", r["name"], int(r["size"]), r["path"]))
    for r in load_cache():     rows.append(("Cache", r["name"], int(r["size"]), r["path"]))
    for r in load_tasks():     rows.append(("Tasks", r["name"], int(r["size"]), r["path"]))
    rows = [r for r in rows if r[2] > 0]
    cats = {}
    for cat, name, size, path in rows: cats.setdefault(cat, 0); cats[cat] += size
    grand = sum(cats.values())
    labels = ["My Claude"]; parents = [""]; values = [grand]; colors = ["#888"]; customdata = [[fmt_size(grand), ""]]
    for cat, tot in cats.items():
        labels.append(cat); parents.append("My Claude"); values.append(tot)
        colors.append(CAT_COLOR.get(cat, "#888")); customdata.append([fmt_size(tot), ""])
    for cat, name, size, path in rows:
        labels.append(name); parents.append(cat); values.append(size)
        colors.append(CAT_COLOR.get(cat, "#888")); customdata.append([fmt_size(size), path])
    data = {"labels": labels, "parents": parents, "values": values, "colors": colors, "customdata": customdata}
    return render(request, "sunburst.html", "Sunburst", data=json.dumps(data))

def render_graph(request):
    try: top_n = int(request.query_params.get("top_n", "15"))
    except ValueError: top_n = 15
    top_n = min(max(5, top_n), 50)
    is_dark = theme_of(request) == "dark"
    edge_c = "#4a4238" if is_dark else "#cbc0ad"; edge_l = "#322C25" if is_dark else "#E5DDD0"
    fg = "#EDE6DC" if is_dark else "#201C17"
    nodes = [{"id": "ROOT", "label": "My Claude", "color": fg, "size": 42}]
    edges = []
    skills = sorted(load_skills(), key=lambda r: -r["size"])[:top_n]
    cat_data = {"Skills": [(s["name"], s["size"], s["path"]) for s in skills],
                "Sessions": [(p["project"], p["size"], p["path"]) for p in load_projects()[:top_n]],
                "Config": [(c["name"], c["size"], c["path"]) for c in load_config_files()[:top_n]],
                "Cache": [(c["name"], c["size"], c["path"]) for c in load_cache()[:top_n]],
                "Tasks": [(t["name"], t["size"], t["path"]) for t in load_tasks()[:top_n]]}
    for cat, items in cat_data.items():
        cid = f"CAT_{cat}"; tot = sum(s for _, s, _ in items)
        nodes.append({"id": cid, "label": f"{cat} ({len(items)})", "color": CAT_COLOR[cat], "size": 30, "title": f"{cat}: {len(items)} items · {fmt_size(tot)}"})
        edges.append({"from": "ROOT", "to": cid, "color": edge_c, "width": 2})
        for name, sz, path in items:
            ns = max(8, min(28, 8 + (sz ** 0.25) / 2))
            nid = f"{cat}_{name}"
            nodes.append({"id": nid, "label": name[:32], "color": CAT_COLOR[cat], "size": ns, "title": f"{name}\n{fmt_size(sz)}\n{path}"})
            edges.append({"from": cid, "to": nid, "color": edge_l, "width": 1})
    connectors = load_connectors(); plugins = load_plugins()
    nodes.append({"id": "CAT_Connectors", "label": f"Connectors ({len(connectors)})", "color": CAT_COLOR["Connectors"], "size": 26})
    edges.append({"from": "ROOT", "to": "CAT_Connectors", "color": edge_c, "width": 2})
    for c in connectors:
        nodes.append({"id": f"MCP_{c['name']}", "label": c["name"], "color": CAT_COLOR["Connectors"], "size": 14, "title": c["name"]})
        edges.append({"from": "CAT_Connectors", "to": f"MCP_{c['name']}", "color": edge_l, "width": 1})
    nodes.append({"id": "CAT_Plugins", "label": f"Plugins ({len(plugins)})", "color": CAT_COLOR["Plugins"], "size": 24})
    edges.append({"from": "ROOT", "to": "CAT_Plugins", "color": edge_c, "width": 2})
    for p in plugins:
        nodes.append({"id": f"PLG_{p['name']}", "label": p["name"], "color": CAT_COLOR["Plugins"], "size": 12, "title": p["name"]})
        edges.append({"from": "CAT_Plugins", "to": f"PLG_{p['name']}", "color": edge_l, "width": 1})
    graph = {"nodes": nodes, "edges": edges, "bg": "#14110D" if is_dark else "#F7F4EE", "font": fg}
    return render(request, "graph.html", "Graph", graph=json.dumps(graph), top_n=top_n)

LINKS_FILE = SKILL_DIR / "links.json"

@ttl_cache(900)
def load_links():
    """Curated reference links, read from links.json so you can edit the list without touching code."""
    try:
        d = json.loads(LINKS_FILE.read_text())
        return {"sections": d.get("sections", []), "checked": d.get("checked", "")}
    except Exception:
        return {"sections": [], "checked": ""}

def render_help(request):
    sections = [
        ("Overview", "overview", "Skills, conversations, token spend, and disk footprint at a glance.",
         ["Tiles summarise skills, conversations, tokens, and storage.",
          "The token tile shows the last 30 days; the Usage page has all-time and custom windows.",
          "Below the tiles: recent conversations, recently updated skills, and anything needing attention."]),
        ("Skills", "skills", "Capabilities loaded into Claude, one folder each.",
         ["Each skill is a folder under ~/.claude/skills/ with a SKILL.md and optional scripts.",
          "Filter by user-created vs plugin-installed; usage counts come from your transcripts.",
          "View reads the full SKILL.md; Push snapshots it to a GitHub repo; Share downloads a portable .zip."]),
        ("MCP & Plugins", "mcp-plugins", "External tool servers and installed plugin bundles.",
         ["Scoped to Claude Code: read from ~/.claude.json and per-project .mcp.json.",
          "Claude Desktop and claude.ai web connectors are configured separately and are not shown here.",
          "Connectors are tool servers wired in via the Model Context Protocol; secrets are auto-redacted. Check health pings each one.",
          "Plugins are bundles installed with /plugin, each shipping skills, commands, or hooks."]),
        ("Usage", "usage", "Token spend and estimated cost, parsed from local transcripts.",
         ["Aggregated from each turn's usage block across every transcript.",
          "Billable is what you'd be charged for; context is what the model saw.",
          "Cost uses Anthropic's public pricing; pick a time window or model family to filter."]),
        ("History", "history", "Browse and read your full conversation history.",
         ["Search message text, filter by time, and read any transcript in a two-pane viewer.",
          "Export to Markdown, resume in Claude, or open the file on disk.",
          "The Caches section lists the performance caches Claude rebuilds as needed."]),
        ("System", "system", "Config and Memory that shape how Claude behaves.",
         ["Config shows your permission rules (allow/ask/deny), hook events, and renders CLAUDE.md.",
          "Edit CLAUDE.md in place; a backup is kept and you confirm before saving.",
          "Memory browses the persistent notes Claude carries between sessions."]),
        ("Command palette", "palette", "Jump anywhere with the keyboard.",
         ["Press Cmd-K (or Ctrl-K) anywhere.",
          "Jump to a page or search across skills and conversations."]),
        ("Settings", "settings", "GitHub push defaults, in the sidebar.",
         ["Set default owner, repo prefix, visibility, and commit template.",
          "Uses your existing gh CLI login; no tokens are stored."]),
    ]
    links = load_links()
    return render(request, "help.html", "Help", sections=sections,
                  link_sections=links["sections"], links_checked=links["checked"])

# ---------- routes: dialogs (HTML partials) ----------
@app.get("/dialog/view/{name}", response_class=HTMLResponse)
def dialog_view(request: Request, name: str):
    path = str(CLAUDE / "skills" / name); meta = parse_skill_meta(path)
    return templates.TemplateResponse(request, "_dlg_view.html", {"request": request, "theme": theme_of(request),
        "name": name, "path": path, "meta": meta})

@app.get("/dialog/transcript", response_class=HTMLResponse)
def dialog_transcript(request: Request, path: str):
    messages = parse_transcript_messages(path)
    label = transcript_label(path) or "(unnamed)"
    return templates.TemplateResponse(request, "_dlg_transcript.html", {"request": request, "theme": theme_of(request),
        "messages": messages, "path": path, "label": label, "count": len(messages)})

@app.get("/dialog/push/{name}", response_class=HTMLResponse)
def dialog_push(request: Request, name: str):
    cfg = load_config_dict(); auth = gh_auth_status(); path = str(CLAUDE / "skills" / name)
    owner = cfg.get("github_owner") or auth["user"] or ""
    repo = (cfg.get("repo_prefix") or "") + name
    exists = gh_repo_exists(owner, repo) if owner else False
    secrets = scan_skill_secrets(path) if owner else []
    return templates.TemplateResponse(request, "_dlg_push.html", {"request": request, "theme": theme_of(request),
        "name": name, "path": path, "auth": auth, "owner": owner, "repo": repo, "exists": exists,
        "visibility": cfg.get("default_visibility", "private"), "secrets": secrets})

@app.get("/dialog/share/{name}", response_class=HTMLResponse)
def dialog_share(request: Request, name: str):
    cfg = load_config_dict(); path = str(CLAUDE / "skills" / name)
    return templates.TemplateResponse(request, "_dlg_share.html", {"request": request, "theme": theme_of(request),
        "name": name, "path": path, "pushed_url": (cfg.get("pushed_skills") or {}).get(name, "")})

@app.get("/dialog/settings", response_class=HTMLResponse)
def dialog_settings(request: Request):
    cfg = load_config_dict(); auth = gh_auth_status()
    return templates.TemplateResponse(request, "_dlg_settings.html", {"request": request, "theme": theme_of(request),
        "cfg": cfg, "auth": auth, "config_path": str(CONFIG_PATH), "config_json": json.dumps(cfg, indent=2)})

# Claude Code deletes transcripts older than cleanupPeriodDays (default 30, minimum 1) in a
# background sweep after a session starts: code.claude.com/docs/en/settings-reference#cleanupperioddays
RETENTION_DEFAULT = 30
def _read_cleanup_days(path):
    """(days or None, error or None) for one settings file."""
    try: d = json.loads(Path(path).read_text())
    except FileNotFoundError: return None, None
    except Exception: return None, "not valid JSON"
    v = d.get("cleanupPeriodDays") if isinstance(d, dict) else None
    return (v if isinstance(v, int) and not isinstance(v, bool) else None), None

@ttl_cache(300)
def known_project_dirs():
    """Real folders Claude Code has run in: the cwd recorded in each transcript, plus history.jsonl."""
    dirs = set(); pdir = CLAUDE / "projects"
    if pdir.exists():
        for jf in pdir.glob("*/*.jsonl"):
            try:
                with open(jf, "r", errors="ignore") as f:
                    for _, line in zip(range(30), f):
                        if '"cwd"' in line:
                            c = json.loads(line).get("cwd")
                            if c: dirs.add(c); break
            except Exception: pass
    try:
        with open(HISTORY_FILE, "r", errors="ignore") as f:
            for line in f:
                try: pj = json.loads(line).get("project")
                except Exception: continue
                if pj: dirs.add(pj)
    except Exception: pass
    return sorted(d for d in dirs if os.path.isdir(d))

MANAGED_SETTINGS = Path("/Library/Application Support/ClaudeCode/managed-settings.json") if sys.platform == "darwin" \
    else Path("/etc/claude-code/managed-settings.json")

def retention_info():
    """The user-file value, plus every other settings file that sets cleanupPeriodDays. An open issue
    (anthropics/claude-code#90148) reports that a project's value controls the sweep machine-wide, so
    the lowest value anywhere is shown as the one at risk of applying."""
    days, err = _read_cleanup_days(CLAUDE / "settings.json")
    if err: return {"days": None, "set": False, "error": "~/.claude/settings.json is not valid JSON, so it can't be read or changed here.",
                    "others": [], "lowest": None}
    files = [MANAGED_SETTINGS, CLAUDE / "settings.local.json"]
    for d in known_project_dirs():
        if Path(d) == HOME: continue  # ~/.claude/settings*.json already covered
        files += [Path(d) / ".claude" / "settings.json", Path(d) / ".claude" / "settings.local.json"]
    others = []
    for f in files:
        v, e = _read_cleanup_days(f)
        if v is not None or e:
            others.append({"file": str(f).replace(str(HOME), "~"), "days": v, "error": e,
                           "managed": f == MANAGED_SETTINGS})
    cur = days if days is not None else RETENTION_DEFAULT
    vals = [cur] + [o["days"] for o in others if o["days"] is not None]
    return {"days": cur, "set": days is not None, "error": None, "others": others, "lowest": min(vals)}

def set_retention(days):
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("Enter a whole number of days, 1 or more.")
    sp = CLAUDE / "settings.json"
    d = json.loads(sp.read_text()) if sp.exists() else {}
    if not isinstance(d, dict): raise ValueError("~/.claude/settings.json is not a JSON object.")
    if sp.exists(): shutil.copy2(str(sp), str(sp) + ".my-claude.bak")  # own name: never clobber another tool's .bak
    d["cleanupPeriodDays"] = days
    sp.write_text(json.dumps(d, indent=2) + "\n")

# Copy of every transcript, kept outside ~/.claude so Claude Code's cleanup sweep can't reach it.
# Copy-only: a file deleted from ~/.claude/projects stays here, which is the point.
BACKUP_DIR = DATA_DIR / "transcript-backup"
DATA_SETTINGS = DATA_DIR / "settings.json"
_backup_lock = threading.Lock()

def data_settings():
    try: d = json.loads(DATA_SETTINGS.read_text())
    except Exception: d = {}
    return d if isinstance(d, dict) else {}

def set_data_setting(key, value):
    d = data_settings(); d[key] = value
    DATA_DIR.mkdir(parents=True, exist_ok=True); DATA_SETTINGS.write_text(json.dumps(d, indent=2))

def backup_transcripts():
    src = CLAUDE / "projects"; copied = total = 0; nbytes = 0
    if not src.exists(): return {"copied": 0, "files": 0}
    with _backup_lock:
        for jf in src.rglob("*.jsonl"):
            total += 1
            try:
                st = jf.stat(); dst = BACKUP_DIR / jf.relative_to(src)
                if dst.exists():
                    ds = dst.stat()
                    if ds.st_size == st.st_size and ds.st_mtime >= st.st_mtime: continue
                dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(jf, dst)
                copied += 1; nbytes += st.st_size
            except OSError: pass
        info = {"at": time.time(), "copied": copied, "files": total, "bytes": nbytes}
        set_data_setting("last_backup", info)
    return info

def backup_status():
    d = data_settings(); last = d.get("last_backup") or {}
    kept = sum(1 for _ in BACKUP_DIR.rglob("*.jsonl")) if BACKUP_DIR.exists() else 0
    return {"auto": bool(d.get("auto_backup")), "last": last, "last_h": fmt_rel(last["at"]) if last.get("at") else "",
            "kept": kept, "dir": str(BACKUP_DIR).replace(str(HOME), "~")}

def _auto_backup_loop():
    while True:
        try:
            if data_settings().get("auto_backup"): backup_transcripts()
        except Exception: pass
        time.sleep(3600)

@app.get("/dialog/retention", response_class=HTMLResponse)
def dialog_retention(request: Request):
    return templates.TemplateResponse(request, "_dlg_retention.html", {"request": request, "theme": theme_of(request),
        "ret": retention_info(), "default": RETENTION_DEFAULT, "bk": backup_status()})

@app.get("/dialog/label", response_class=HTMLResponse)
def dialog_label(request: Request, path: str):
    sid = Path(path).stem; lab = load_labels().get(sid) or {}
    return templates.TemplateResponse(request, "_dlg_label.html", {"request": request, "theme": theme_of(request),
        "session": sid, "name": lab.get("name") or "", "tags": " ".join("#" + t for t in lab.get("tags") or []),
        "claude_title": _conv_meta(path).get("title") or "", "path": path, "store": str(LABELS_PATH).replace(str(HOME), "~")})

@app.get("/dialog/install", response_class=HTMLResponse)
def dialog_install(request: Request):
    return templates.TemplateResponse(request, "_dlg_install.html", {"request": request, "theme": theme_of(request)})

# ---------- routes: actions (JSON) ----------
# NOTE: specific /action/* routes MUST be declared before the catch-all /action/{kind},
# otherwise Starlette matches the catch-all first and returns "unknown action".
@app.post("/action/push")
async def action_push(request: Request):
    d = await request.json()
    name = d["name"]; path = str(CLAUDE / "skills" / name)
    ok, url, log = push_skill_to_github(path, d["owner"], d["repo"], d.get("visibility", "private"))
    if ok:
        cfg = load_config_dict(); cfg.setdefault("pushed_skills", {})[name] = url; save_config(cfg)
        if url: pbcopy(url)
    return {"ok": ok, "url": url, "log": log}

@app.post("/action/install")
async def action_install(request: Request):
    d = await request.json()
    ok, nm, log = install_skill_from_url(d.get("url", ""), d.get("name") or None)
    if ok: clear_cache()
    return {"ok": ok, "name": nm, "log": log}

@app.post("/action/save-claudemd")
async def action_save_claudemd(request: Request):
    d = await request.json()
    content = d.get("content", "")
    cm = CLAUDE / "CLAUDE.md"
    try:
        if cm.exists(): shutil.copy2(str(cm), str(cm) + ".bak")  # keep a backup before overwriting
        cm.write_text(content)
        clear_cache()
        return {"ok": True, "msg": "Saved. Previous version backed up to CLAUDE.md.bak"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/action/backup")
async def action_backup(request: Request):
    d = await request.json()
    try:
        if "auto" in d: set_data_setting("auto_backup", bool(d["auto"]))
        if d.get("now") or d.get("auto"):
            info = await asyncio.to_thread(backup_transcripts)
            return {"ok": True, "msg": f"Backed up {info['copied']} new or changed of {info['files']} transcripts.", "status": backup_status()}
        return {"ok": True, "msg": "Automatic backup off.", "status": backup_status()}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/action/retention")
async def action_retention(request: Request):
    d = await request.json()
    try:
        set_retention(d.get("days"))
        return {"ok": True, "msg": f"Saved. Claude Code now keeps conversations for {d.get('days')} days. Previous file backed up to settings.json.my-claude.bak"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/action/label")
async def action_label(request: Request):
    d = await request.json()
    try:
        lab = save_label(d.get("session"), d.get("name"), d.get("tags"))
        for k in [k for k in list(_CACHE) if k[0] == "conversation_index"]: _CACHE.pop(k, None)
        return {"ok": True, "msg": "Saved.", "label": lab}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/action/gh-login")
async def action_gh_login(request: Request):
    return gh_login_start()

@app.get("/action/gh-login-status")
async def action_gh_login_status(request: Request):
    st = gh_login_status()
    if st["logged_in"]: clear_cache()  # refresh push-availability across the app once connected
    return st

@app.post("/action/gh-logout")
async def action_gh_logout(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    host = (body.get("host") or "github.com").strip()
    user = (body.get("user") or "").strip()
    cmd = ["gh", "auth", "logout", "--hostname", host]
    if user: cmd += ["--user", user]  # required when multiple accounts are logged in
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        ok = r.returncode == 0
        if ok:
            clear_cache()  # drop the cached gh_auth_status so the reopened dialog + chip show fresh state
            with _gh_lock: _gh_release(kill=True); _gh_login["state"] = "idle"; _gh_login["code"] = None
        msg = (r.stderr or r.stdout or "").strip() or (f"Signed out of {host}." if ok else "Logout failed.")
        return {"ok": ok, "msg": msg[:400]}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/action/{kind}")
async def action(kind: str, request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    path = body.get("path", ""); resume = body.get("resume")
    if kind == "finder": return {"ok": open_in_finder(path), "msg": "Opened in Finder"}
    if kind == "terminal": return {"ok": open_in_terminal(path), "msg": f"Opened Terminal at {path}"}
    if kind == "reveal": return {"ok": reveal(path), "msg": "Revealed"}
    if kind == "editor": return {"ok": open_file_default(path), "msg": f"Opened in {editor_name()}"}
    if kind == "copy": return {"ok": pbcopy(body.get("text", path)), "msg": "Copied"}
    if kind == "copyfile":
        try: return {"ok": pbcopy(Path(path).read_text(errors="ignore")), "msg": "Copied to clipboard"}
        except Exception as e: return {"ok": False, "msg": f"Failed: {e}"}
    if kind == "claude":
        term = body.get("term") or ""
        ok, note = start_claude_in_project(path, resume_id=resume, term_id=term)
        name = next((t["name"] for t in available_terminals() if t["id"] == term), "your default terminal")
        return {"ok": ok, "msg": note or (f"Launching Claude in {name}…" if ok else "Could not launch Claude")}
    return {"ok": False, "msg": "unknown action"}

@app.get("/api/terminals")
def api_terminals():
    return {"terminals": available_terminals()}

@app.get("/api/data-version")
def api_data_version():
    """Freshness probe the open page polls (a stat-only scan, ~6ms).

    `version` is the fingerprint of the data currently IN the cache, not what is on disk — so the
    page only re-renders when a rebuild has actually landed and the content will really differ.
    `pending` means newer data exists and a rebuild is queued behind the coalesce window.
    """
    ensure_fresh()
    fp = data_fingerprint(); served = _fp_seen or fp
    return {"version": "%d-%.0f-%d-%.0f" % served, "transcripts": served[0], "skills": served[2],
            "pending": fp != served, "rebuilding": _rebuilding.is_set()}

@app.get("/api/usage-limits")
def api_usage_limits(fresh: int = 0):
    return live_limits(force=bool(fresh))

@app.post("/settings/save")
async def settings_save(github_owner: str = Form(""), repo_prefix: str = Form("claude-skill-"),
                        default_visibility: str = Form("private"), commit_message_template: str = Form("Update {skill} skill")):
    cfg = load_config_dict()
    cfg.update({"github_owner": github_owner.strip(), "repo_prefix": repo_prefix.strip(),
                "default_visibility": default_visibility, "commit_message_template": commit_message_template.strip()})
    save_config(cfg); clear_cache()
    return {"ok": True, "msg": "Saved"}

@app.get("/download/skill/{name}.zip")
def download_zip(name: str):
    path = CLAUDE / "skills" / name
    data = make_skill_zip(str(path))
    return StreamingResponse(io.BytesIO(data), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})
