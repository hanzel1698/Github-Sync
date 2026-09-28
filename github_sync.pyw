"""
GitHub Sync - side-by-side view of your GitHub repos vs. the project folders on this PC,
with one-click Pull / Push / Sync / Clone / Publish.

Requires: Git for Windows and the GitHub CLI (gh), signed in once.
Double-click this file (or the desktop shortcut) to run - no console window.
"""
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import threading
import queue
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

APP_NAME = "GitHub Sync"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "GitHubSync"
CONFIG_FILE = CONFIG_DIR / "config.json"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
NEW_CONSOLE = 0x00000010 if os.name == "nt" else 0        # CREATE_NEW_CONSOLE
PC_NAME = socket.gethostname()

# ----------------------------------------------------------------------------- helpers


def run(args, cwd=None, timeout=300):
    """Run a command silently. Returns (returncode, stdout, stderr)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=NO_WINDOW,
            stdin=subprocess.DEVNULL, env=env,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"'{args[0]}' was not found. Is it installed and on PATH?"
    except subprocess.TimeoutExpired:
        return 124, "", f"Timed out after {timeout}s"


def repo_key(url):
    """'https://github.com/Owner/Repo.git' -> 'owner/repo' (None if not GitHub)."""
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", (url or "").strip(), re.I)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def rel_time(iso_or_dt):
    if not iso_or_dt:
        return ""
    try:
        dt = iso_or_dt if isinstance(iso_or_dt, datetime) else \
            datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
    except ValueError:
        return str(iso_or_dt)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    for unit, size in (("year", 31536000), ("month", 2592000), ("day", 86400),
                       ("hour", 3600), ("min", 60)):
        if secs >= size:
            n = int(secs // size)
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def default_root():
    # The app normally lives inside the projects folder (e.g. ...\Created Apps & Programs\Github-sync)
    return str(Path(__file__).resolve().parent.parent)


# ----------------------------------------------------------------------------- scanning


def find_local_folders(root):
    """Top-level folders in root. A non-git folder that contains git repos is expanded one level."""
    out = []
    try:
        entries = sorted(Path(root).iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for d in entries:
        if not d.is_dir() or d.name.startswith("."):
            continue
        if (d / ".git").exists():
            out.append(d)
            continue
        try:
            subs = sorted((s for s in d.iterdir() if s.is_dir() and (s / ".git").exists()),
                          key=lambda p: p.name.lower())
        except OSError:
            subs = []
        out.extend(subs if subs else [d])
    return out


def local_info(path, do_fetch):
    path = Path(path)
    info = {"path": str(path), "name": path.name, "is_git": (path / ".git").exists(),
            "remotes": {}, "changes": 0, "ahead": 0, "behind": 0}
    if not info["is_git"]:
        return info
    _, out, _ = run(["git", "remote", "-v"], cwd=path)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            info["remotes"].setdefault(parts[0], parts[1])
    if do_fetch and info["remotes"]:
        rc, _, err = run(["git", "fetch", "--all", "--prune", "--quiet"], cwd=path, timeout=120)
        if rc:
            info["fetch_error"] = err.splitlines()[-1] if err else "fetch failed"
    rc, out, err = run(["git", "status", "--porcelain=v2", "--branch"], cwd=path)
    if rc:
        info["error"] = err or "git status failed"
        return info
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            info["branch"] = line[14:]
        elif line.startswith("# branch.oid "):
            info["no_commits"] = line[13:] == "(initial)"
        elif line.startswith("# branch.upstream "):
            info["upstream"] = line[18:]
        elif line.startswith("# branch.ab "):
            a, b = line[12:].split()
            info["ahead"], info["behind"] = int(a), -int(b)
        elif line and not line.startswith("#"):
            info["changes"] += 1
    if not info.get("no_commits"):
        _, out, _ = run(["git", "log", "-1", "--format=%cI%x1f%s"], cwd=path)
        if "\x1f" in out:
            info["last_commit_date"], info["last_commit_msg"] = out.split("\x1f", 1)
    return info


def fetch_github_repos():
    rc, out, err = run(["gh", "repo", "list", "--limit", "1000", "--json",
                        "name,nameWithOwner,url,isPrivate,defaultBranchRef,pushedAt,"
                        "description,isArchived,isFork"], timeout=120)
    if rc:
        raise RuntimeError(err or "gh repo list failed")
    return json.loads(out or "[]")


# ----------------------------------------------------------------------------- status

# tag -> (row background, sort priority)
TAG_STYLE = {
    "error":    ("#f8d0d0", 0),
    "diverged": ("#f8d0d0", 1),
    "behind":   ("#d3e5fb", 2),
    "ahead":    ("#fde7c8", 3),
    "dirty":    ("#fff5c2", 4),
    "missing":  ("#ececec", 5),
    "localonly": ("#e9dcf7", 6),
    "plain":    ("#f4f4f4", 7),
    "ok":       ("#d9f2dc", 8),
    "ignored":  ("#ffffff", 9),
}


def compute_status(row):
    gh, info = row.get("gh"), row.get("local")
    if info is None:
        if gh and gh.get("_empty"):
            return "Not on this PC (GitHub repo is empty)", "missing"
        return "Not on this PC  →  Clone", "missing"
    if not info["is_git"]:
        if gh:
            return "Not a git repo yet, GitHub repo with same name is empty  →  Publish", "plain"
        return "Folder is not a git repo  →  Publish", "plain"
    if info.get("error"):
        if "dubious ownership" in info["error"]:
            return "Git blocks this folder (created by another Windows user)  →  right-click → Trust", "error"
        return "Error: " + info["error"][:80], "error"
    if not info["remotes"] and gh:
        return "Git repo, GitHub repo with same name is empty  →  Publish", "localonly"
    if not info["remotes"]:
        extra = f", {info['changes']} changed files" if info["changes"] else ""
        return f"Only on this PC (no GitHub link){extra}  →  Publish", "localonly"
    if info.get("fetch_error"):
        return "Could not reach GitHub: " + info["fetch_error"][:70], "error"
    if info.get("no_commits"):
        return "No commits yet  →  Push", "dirty"
    if info.get("branch") == "(detached)":
        return "Detached HEAD (not on a branch)", "error"
    if not info.get("upstream"):
        return f"Branch '{info.get('branch')}' not on GitHub yet  →  Push", "ahead"
    a, b, c = info["ahead"], info["behind"], info["changes"]
    dirty = f"  ·  {c} uncommitted" if c else ""
    if a and b:
        return f"Diverged: {a} to push, {b} to pull  →  Sync{dirty}", "diverged"
    if b:
        return f"{b} new commit{'s' if b > 1 else ''} on GitHub  →  Pull{dirty}", "behind"
    if a:
        return f"{a} commit{'s' if a > 1 else ''} not on GitHub  →  Push{dirty}", "ahead"
    if c:
        return f"{c} uncommitted change{'s' if c > 1 else ''}  →  Push", "dirty"
    return "✔ In sync", "ok"


# ----------------------------------------------------------------------------- UI


class App(tk.Tk):
    COLS = [
        ("gh_name",   "☁  GitHub repository", 210, "w"),
        ("gh_vis",    "☁  Visibility",         80, "center"),
        ("gh_push",   "☁  Last push",         105, "w"),
        ("status",    "⇄  Status",            330, "w"),
        ("loc_name",  "💻  Folder on this PC", 210, "w"),
        ("loc_branch", "💻  Branch",            85, "w"),
        ("loc_commit", "💻  Last local commit", 120, "w"),
    ]

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — {PC_NAME}")
        self.geometry("1320x780")
        self.minsize(900, 500)
        try:
            self.state("zoomed")
        except tk.TclError:
            pass

        self.cfg = load_config()
        self.cfg.setdefault("root", default_root())
        self.cfg.setdefault("ignored", [])
        self.cfg.setdefault("fetch", True)
        save_config(self.cfg)

        self.rows = {}           # iid -> row dict
        self.gh_user = None
        self.busy = False
        self.ui_queue = queue.Queue()
        self.op_queue = queue.Queue()
        threading.Thread(target=self._op_worker, daemon=True).start()

        self._build_ui()
        self.after(100, self._drain_ui_queue)
        self.after(200, self.startup_check)

    # ---------- layout
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        k = self.winfo_fpixels("1i") / 96          # DPI scale factor
        self.k = k
        style.configure("Treeview", rowheight=int(26 * k), font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Big.TButton", padding=(10, 4))
        style.configure("Head.TLabel", font=("Segoe UI", 11, "bold"))

        # menu
        menubar = tk.Menu(self)
        m = tk.Menu(menubar, tearoff=0)
        m.add_command(label="Change projects folder…", command=self.change_root)
        m.add_command(label="Open projects folder", command=lambda: os.startfile(self.cfg["root"]))
        m.add_separator()
        m.add_command(label="Sign in to GitHub…", command=self.sign_in)
        m.add_command(label="Create desktop shortcut", command=self.create_shortcut)
        m.add_separator()
        m.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=m)
        self.config(menu=menubar)

        # top bar
        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        self.gh_label = ttk.Label(top, text="☁ GitHub: …", style="Head.TLabel")
        self.gh_label.pack(side="left")
        ttk.Label(top, text="   ⇄   ", style="Head.TLabel").pack(side="left")
        self.root_label = ttk.Label(top, text="", style="Head.TLabel")
        self.root_label.pack(side="left")

        ttk.Button(top, text="⟳ Refresh", style="Big.TButton", command=self.refresh).pack(side="right")
        self.fetch_var = tk.BooleanVar(value=self.cfg["fetch"])
        ttk.Checkbutton(top, text="Check GitHub for new commits (fetch)", variable=self.fetch_var,
                        command=self._save_fetch).pack(side="right", padx=10)

        # filter bar
        fb = ttk.Frame(self, padding=(10, 0, 10, 4))
        fb.pack(fill="x")
        ttk.Label(fb, text="Show:").pack(side="left")
        self.filter_var = tk.StringVar(value="All")
        cb = ttk.Combobox(fb, textvariable=self.filter_var, state="readonly", width=26, values=[
            "All", "Needs attention", "Behind (pull needed)", "Ahead / changed (push needed)",
            "Not on this PC", "Only on this PC", "In sync", "Ignored"])
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self.render())
        ttk.Label(fb, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.render())
        ttk.Entry(fb, textvariable=self.search_var, width=28).pack(side="left", padx=4)
        self.summary_label = ttk.Label(fb, text="")
        self.summary_label.pack(side="right")

        # action bar
        ab = ttk.Frame(self, padding=(10, 2, 10, 6))
        ab.pack(fill="x")
        self.action_buttons = []

        def btn(text, cmd, side="left"):
            b = ttk.Button(ab, text=text, style="Big.TButton", command=cmd)
            b.pack(side=side, padx=3)
            self.action_buttons.append(b)
            return b
        btn("⬇ Pull", self.do_pull)
        btn("⬆ Push", self.do_push)
        btn("⇅ Sync (pull + push)", self.do_sync)
        btn("📥 Clone to this PC", self.do_clone)
        btn("☁ Publish to GitHub", self.do_publish)
        ttk.Separator(ab, orient="vertical").pack(side="left", fill="y", padx=8)
        btn("📂 Open folder", self.open_folder)
        btn("🌐 Open on GitHub", self.open_github)
        btn("⬇ Pull all behind", self.pull_all, side="right")
        btn("📥 Clone all missing", self.clone_all, side="right")

        # paned: tree / bottom notebook
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        tf = ttk.Frame(pw)
        cols = [c[0] for c in self.COLS]
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        for key, text, width, anchor in self.COLS:
            self.tree.heading(key, text=text, anchor="w", command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=int(width * self.k), anchor=anchor, stretch=key in ("status",))
        for tag, (bg, _) in TAG_STYLE.items():
            self.tree.tag_configure(tag, background=bg)
        self.tree.tag_configure("ignored", foreground="#999999")
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.tree.bind("<Double-1>", lambda e: self.open_folder())
        self.tree.bind("<Button-3>", self.context_menu)
        pw.add(tf, weight=4)

        nb = ttk.Notebook(pw)
        self.details = tk.Text(nb, height=9, wrap="word", font=("Consolas", 10), borderwidth=0)
        self.log = tk.Text(nb, height=9, wrap="word", font=("Consolas", 10), borderwidth=0)
        self.log.tag_configure("err", foreground="#b00020")
        self.log.tag_configure("ok", foreground="#1b7a2f")
        self.log.tag_configure("cmd", foreground="#555555")
        nb.add(self.details, text="  Details  ")
        nb.add(self.log, text="  Activity log  ")
        self.notebook = nb
        pw.add(nb, weight=1)

        # status bar
        sb = ttk.Frame(self, padding=(10, 2))
        sb.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(sb, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        self.status_label = ttk.Label(sb, text="Ready")
        self.status_label.pack(side="left")

        self.sort_key, self.sort_rev = None, False
        self._update_root_label()

    def _update_root_label(self):
        self.root_label.config(text=f"💻 This PC: {self.cfg['root']}")

    def _save_fetch(self):
        self.cfg["fetch"] = self.fetch_var.get()
        save_config(self.cfg)

    # ---------- thread plumbing
    def ui(self, fn, *args):
        self.ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.after(80, self._drain_ui_queue)

    def set_busy(self, text):
        self.busy = bool(text)
        self.status_label.config(text=text or "Ready")
        if text:
            self.progress.start(12)
        else:
            self.progress.stop()

    def log_line(self, text, tag=None):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{stamp}] {text}\n", tag)
        self.log.see("end")

    def _op_worker(self):
        while True:
            label, fn = self.op_queue.get()
            self.ui(self.set_busy, label)
            try:
                fn()
            except Exception as e:  # never kill the worker
                self.ui(self.log_line, f"Unexpected error: {e}", "err")
            if self.op_queue.empty():
                self.ui(self.set_busy, "")

    def enqueue(self, label, fn):
        self.op_queue.put((label, fn))

    def git(self, args, cwd, timeout=300, quiet=False):
        """Run git/gh from a worker thread, echoing to the log. Returns (ok, output)."""
        if not quiet:
            self.ui(self.log_line, f"$ {' '.join(args)}   ({Path(cwd).name if cwd else ''})", "cmd")
        rc, out, err = run(args, cwd=cwd, timeout=timeout)
        text = "\n".join(t for t in (out, err) if t)
        if text and not quiet:
            self.ui(self.log_line, text, "err" if rc else None)
        return rc == 0, text

    # ---------- startup / refresh
    def startup_check(self):
        def work():
            rc, out, err = run(["git", "--version"])
            if rc:
                self.ui(messagebox.showerror, APP_NAME,
                        "Git is not installed.\n\nInstall 'Git for Windows' from https://git-scm.com and restart this app.")
                return
            rc, out, err = run(["gh", "--version"])
            if rc:
                self.ui(self._ask_install_gh)
                return
            rc, out, err = run(["gh", "api", "user", "--jq", ".login"])
            if rc or not out:
                self.ui(self._ask_sign_in)
                return
            self.gh_user = out
            self.ui(self.gh_label.config, {"text": f"☁ GitHub: {out}"})
            self.ui(self.refresh)
        self.enqueue("Checking Git and GitHub…", work)

    def _ask_install_gh(self):
        if messagebox.askyesno(APP_NAME, "The GitHub CLI (gh) is not installed. It is needed to list your "
                               "GitHub repositories.\n\nOpen the download page now?"):
            webbrowser.open("https://cli.github.com/")

    def _ask_sign_in(self):
        if messagebox.askyesno(APP_NAME, "You are not signed in to GitHub on this PC.\n\nSign in now? "
                               "A small window will show a one-time code and open your browser."):
            self.sign_in()

    def sign_in(self):
        script = ("gh auth login --hostname github.com --git-protocol https --web && "
                  "gh auth setup-git && echo. && echo Signed in! You can close this window and press "
                  "Refresh in GitHub Sync. & pause")
        subprocess.Popen(["cmd", "/c", script], creationflags=NEW_CONSOLE)

    def refresh(self):
        if self.busy and not self.op_queue.empty():
            return
        root = self.cfg["root"]
        do_fetch = self.fetch_var.get()

        def work():
            if not self.gh_user:
                rc, out, _ = run(["gh", "api", "user", "--jq", ".login"])
                if rc == 0 and out:
                    self.gh_user = out
                    self.ui(self.gh_label.config, {"text": f"☁ GitHub: {out}"})
            self.ui(self.status_label.config, {"text": "Loading GitHub repositories…"})
            gh_repos, gh_error = [], None
            try:
                gh_repos = fetch_github_repos()
            except Exception as e:
                gh_error = str(e)
                self.ui(self.log_line, f"Could not list GitHub repos: {e}", "err")
            for r in gh_repos:
                r["_empty"] = not (r.get("defaultBranchRef") or {}).get("name")

            folders = find_local_folders(root)
            self.ui(self.status_label.config,
                    {"text": f"Checking {len(folders)} local folders" + (" (fetching from GitHub)…" if do_fetch else "…")})
            with ThreadPoolExecutor(max_workers=8) as ex:
                infos = list(ex.map(lambda p: local_info(p, do_fetch), folders))

            rows = self._match(gh_repos, infos)
            self.ui(self._set_rows, rows)
            msg = f"Refreshed {datetime.now():%H:%M:%S} — {len(gh_repos)} GitHub repos, {len(infos)} local folders"
            self.ui(self.log_line, msg + (f"  (GitHub error: {gh_error})" if gh_error else ""), "err" if gh_error else "ok")
        self.enqueue("Refreshing…", work)

    def _match(self, gh_repos, infos):
        by_key = {r["nameWithOwner"].lower(): r for r in gh_repos}
        used = set()
        rows = {}
        for info in infos:
            gh, remote_key = None, None
            names = sorted(info["remotes"], key=lambda n: n != "origin")
            for n in names:
                k = repo_key(info["remotes"][n])
                remote_key = remote_key or k
                if k in by_key:
                    gh = by_key[k]
                    used.add(k)
                    break
            rows["L:" + info["path"]] = {"gh": gh, "local": info, "remote_key": remote_key}
        # A folder with no GitHub link whose name matches an EMPTY GitHub repo -> show them together
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        empty = {norm(r["name"]): k for k, r in by_key.items() if r["_empty"] and k not in used}
        for row in rows.values():
            info = row["local"]
            if not row["gh"] and not info["remotes"] and not info.get("error") and norm(info["name"]) in empty:
                k = empty.pop(norm(info["name"]))
                row["gh"], row["remote_key"] = by_key[k], k
                used.add(k)
        for k, r in by_key.items():
            if k not in used:
                rows["G:" + k] = {"gh": r, "local": None, "remote_key": k}
        return rows

    def _set_rows(self, rows):
        self.rows = rows
        self.render()

    def refresh_rows(self, iids):
        """Re-read local status for a few rows (after pull/push) without a full refresh."""
        fresh = {}
        for iid in iids:
            row = self.rows.get(iid)
            if row and row["local"]:
                fresh[iid] = local_info(row["local"]["path"], False)

        def apply():
            for iid, info in fresh.items():
                if iid in self.rows:
                    self.rows[iid]["local"] = info
            self.render()
        self.ui(apply)

    # ---------- rendering
    def row_values(self, row):
        gh, loc = row["gh"], row["local"]
        if gh:
            name = gh["name"] + ("  (archived)" if gh.get("isArchived") else "")
            vis = "Private" if gh.get("isPrivate") else "Public"
            pushed = rel_time(gh.get("pushedAt"))
        elif row.get("remote_key"):
            name, vis, pushed = f"({row['remote_key']})", "not yours", ""
        else:
            name, vis, pushed = "—", "", ""
        status, tag = compute_status(row)
        if loc:
            lname = loc["name"]
            parent = Path(loc["path"]).parent
            if str(parent) != str(Path(self.cfg["root"])):
                lname = f"{parent.name}\\{lname}"
            branch = loc.get("branch", "") if loc["is_git"] else ""
            commit = rel_time(loc.get("last_commit_date"))
        else:
            lname, branch, commit = "— not on this PC —", "", ""
        return (name, vis, pushed, status, lname, branch, commit), tag

    def is_ignored(self, iid):
        return iid in self.cfg["ignored"]

    def render(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        f = self.filter_var.get()
        q = self.search_var.get().strip().lower()
        counts = {}
        items = []
        for iid, row in self.rows.items():
            values, tag = self.row_values(row)
            ignored = self.is_ignored(iid)
            if not ignored:
                counts[tag] = counts.get(tag, 0) + 1
            show = {
                "All": not ignored,
                "Needs attention": not ignored and tag not in ("ok",),
                "Behind (pull needed)": not ignored and tag in ("behind", "diverged"),
                "Ahead / changed (push needed)": not ignored and tag in ("ahead", "dirty", "diverged"),
                "Not on this PC": not ignored and tag == "missing",
                "Only on this PC": not ignored and tag in ("localonly", "plain"),
                "In sync": not ignored and tag == "ok",
                "Ignored": ignored,
            }.get(f, True)
            if q and q not in (values[0] + " " + values[4]).lower():
                show = False
            if show:
                items.append((iid, values, "ignored" if ignored else tag))
        if self.sort_key:
            idx = [c[0] for c in self.COLS].index(self.sort_key)
            items.sort(key=lambda it: str(it[1][idx]).lower(), reverse=self.sort_rev)
        else:
            items.sort(key=lambda it: (TAG_STYLE[it[2]][1], (it[1][0] if it[1][0] != "—" else it[1][4]).lower()))
        for iid, values, tag in items:
            self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))
        keep = [i for i in sel if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        parts = []
        for tag, label in (("ok", "in sync"), ("behind", "to pull"), ("ahead", "to push"),
                           ("dirty", "with changes"), ("diverged", "diverged"),
                           ("missing", "not on this PC"), ("localonly", "only on this PC"),
                           ("plain", "not git"), ("error", "errors")):
            if counts.get(tag):
                parts.append(f"{counts[tag]} {label}")
        self.summary_label.config(text="   ·   ".join(parts))
        self.show_details()

    def sort_by(self, key):
        if self.sort_key == key:
            if self.sort_rev:
                self.sort_key, self.sort_rev = None, False   # third click: back to smart order
            else:
                self.sort_rev = True
        else:
            self.sort_key, self.sort_rev = key, False
        self.render()

    def selected(self):
        return [(iid, self.rows[iid]) for iid in self.tree.selection() if iid in self.rows]

    def show_details(self):
        self.details.delete("1.0", "end")
        sel = self.selected()
        if not sel:
            self.details.insert("end", "Select a row to see details. Tip: Ctrl/Shift-click to select several, "
                                       "right-click for more options, double-click to open the folder.")
            return
        if len(sel) > 1:
            self.details.insert("end", f"{len(sel)} rows selected.")
            return
        iid, row = sel[0]
        gh, loc = row["gh"], row["local"]
        status, _ = compute_status(row)
        lines = [f"Status:        {status}"]
        if gh:
            lines += [f"GitHub:        {gh['url']}   ({'private' if gh['isPrivate'] else 'public'})",
                      f"Default branch:{' ' + (gh.get('defaultBranchRef') or {}).get('name', '') or ' (empty repo)'}",
                      f"Last push:     {rel_time(gh.get('pushedAt'))}"]
            if gh.get("description"):
                lines.append(f"Description:   {gh['description']}")
        if loc:
            lines.append(f"Local folder:  {loc['path']}")
            for n, u in loc.get("remotes", {}).items():
                lines.append(f"Remote {n + ':':8}{u}")
            if loc.get("branch"):
                lines.append(f"Branch:        {loc['branch']}  →  {loc.get('upstream') or '(not on GitHub)'}")
            if loc.get("last_commit_msg"):
                lines.append(f"Last commit:   {loc['last_commit_msg']}  ({rel_time(loc.get('last_commit_date'))})")
            if loc.get("changes"):
                _, out, _ = run(["git", "status", "--short"], cwd=loc["path"])
                files = out.splitlines()
                lines.append(f"Uncommitted changes ({len(files)}):")
                lines += ["    " + f for f in files[:40]]
                if len(files) > 40:
                    lines.append(f"    … and {len(files) - 40} more")
        self.details.insert("end", "\n".join(lines))

    def context_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="⬇ Pull", command=self.do_pull)
        m.add_command(label="⬆ Push", command=self.do_push)
        m.add_command(label="⇅ Sync", command=self.do_sync)
        m.add_command(label="📥 Clone to this PC", command=self.do_clone)
        m.add_command(label="☁ Publish to GitHub", command=self.do_publish)
        m.add_separator()
        m.add_command(label="📂 Open folder", command=self.open_folder)
        m.add_command(label="🌐 Open on GitHub", command=self.open_github)
        row = self.rows.get(iid)
        if row and row["local"] and "dubious ownership" in (row["local"].get("error") or ""):
            m.add_command(label="🔓 Trust this folder (fix 'dubious ownership')", command=self.trust_folder)
        m.add_separator()
        if self.is_ignored(iid):
            m.add_command(label="Stop ignoring", command=lambda: self.toggle_ignore(False))
        else:
            m.add_command(label="Ignore on this PC (hide)", command=lambda: self.toggle_ignore(True))
        m.tk_popup(event.x_root, event.y_root)

    def trust_folder(self):
        paths = [r["local"]["path"] for _, r in self.selected()
                 if r["local"] and "dubious ownership" in (r["local"].get("error") or "")]
        if not paths or not messagebox.askyesno(
                APP_NAME, "Git refuses to work in folders created by a different Windows user account "
                          "(common after copying from another PC).\n\nMark these folders as trusted?\n\n"
                          + "\n".join(paths)):
            return

        def work():
            for p in paths:
                self.git(["git", "config", "--global", "--add", "safe.directory", Path(p).as_posix()], None)
            self.ui(self.refresh)
        self.enqueue("Trusting folders…", work)

    def toggle_ignore(self, ignore):
        for iid, _ in self.selected():
            if ignore and iid not in self.cfg["ignored"]:
                self.cfg["ignored"].append(iid)
            elif not ignore and iid in self.cfg["ignored"]:
                self.cfg["ignored"].remove(iid)
        save_config(self.cfg)
        self.render()

    # ---------- actions
    def _need_selection(self):
        sel = self.selected()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select one or more rows first.")
        return sel

    def _git_rows(self, sel, action):
        good = [(i, r) for i, r in sel if r["local"] and r["local"]["is_git"] and r["local"]["remotes"]]
        skipped = len(sel) - len(good)
        if skipped:
            self.log_line(f"{action}: skipped {skipped} row(s) that are not linked to GitHub on this PC "
                          "(use Clone or Publish for those).")
        if not good:
            messagebox.showinfo(APP_NAME, f"Nothing to {action.lower()}: the selected rows are not "
                                          "linked to GitHub on this PC.\nUse Clone or Publish instead.")
        return good

    def _ask_commit_message(self, rows):
        dirty = [r["local"]["name"] for _, r in rows if r["local"].get("changes") or r["local"].get("no_commits")]
        if not dirty:
            return ""
        names = ", ".join(dirty[:6]) + (" …" if len(dirty) > 6 else "")
        return simpledialog.askstring(
            APP_NAME, f"These have uncommitted changes that will be committed and pushed:\n{names}\n\n"
                      "Describe the change (commit message):",
            initialvalue=f"Update from {PC_NAME} {datetime.now():%Y-%m-%d %H:%M}", parent=self)

    def _pull_one(self, loc, allow_rebase):
        p = loc["path"]
        if not loc.get("upstream"):
            self.ui(self.log_line, f"{loc['name']}: branch is not on GitHub yet, nothing to pull.")
            return True
        if loc["ahead"] and loc["behind"]:
            if not allow_rebase:
                self.ui(self.log_line, f"{loc['name']}: diverged — use Sync to combine both sides.", "err")
                return False
            ok, out = self.git(["git", "pull", "--rebase", "--autostash"], p)
            if not ok:
                self.git(["git", "rebase", "--abort"], p)
                self.ui(self.log_line, f"{loc['name']}: CONFLICT — the same lines were changed on both PCs. "
                                       "Nothing was changed locally. Resolve manually (e.g. in VS Code).", "err")
                return False
        else:
            ok, out = self.git(["git", "pull", "--ff-only", "--autostash"], p)
            if not ok:
                return False
        self.ui(self.log_line, f"{loc['name']}: pulled ✔", "ok")
        return True

    def _push_one(self, loc, message):
        p = loc["path"]
        if loc.get("changes") or loc.get("no_commits"):
            ok, _ = self.git(["git", "add", "-A"], p)
            if ok and (loc.get("changes")):
                ok, _ = self.git(["git", "commit", "-m", message], p)
            if not ok:
                return False
        if loc.get("upstream"):
            ok, out = self.git(["git", "push"], p)
        else:
            ok, out = self.git(["git", "push", "-u", "origin", "HEAD"], p)
        if not ok:
            if "rejected" in out or "fetch first" in out or "non-fast-forward" in out:
                self.ui(self.log_line, f"{loc['name']}: GitHub has newer commits — use Sync (pull + push).", "err")
            return False
        self.ui(self.log_line, f"{loc['name']}: pushed ✔", "ok")
        return True

    def do_pull(self, sel=None):
        sel = sel or self._need_selection()
        rows = sel and self._git_rows(sel, "Pull")
        if not rows:
            return
        diverged = [r["local"]["name"] for _, r in rows if r["local"]["ahead"] and r["local"]["behind"]]
        allow_rebase = False
        if diverged:
            allow_rebase = messagebox.askyesno(
                APP_NAME, "These have commits on BOTH this PC and GitHub:\n" + ", ".join(diverged) +
                "\n\nPut your local commits on top of GitHub's (rebase)? If the same lines were edited on "
                "both sides, nothing will be changed and you'll be told.")

        def work():
            for iid, r in rows:
                self._pull_one(r["local"], allow_rebase)
            self.refresh_rows([i for i, _ in rows])
        self.enqueue(f"Pulling {len(rows)} repo(s)…", work)

    def do_push(self):
        sel = self._need_selection()
        rows = sel and self._git_rows(sel, "Push")
        if not rows:
            return
        msg = self._ask_commit_message(rows)
        if msg is None:
            return

        def work():
            for iid, r in rows:
                self._push_one(r["local"], msg or "Update")
            self.refresh_rows([i for i, _ in rows])
        self.enqueue(f"Pushing {len(rows)} repo(s)…", work)

    def do_sync(self):
        sel = self._need_selection()
        rows = sel and self._git_rows(sel, "Sync")
        if not rows:
            return
        msg = self._ask_commit_message(rows)
        if msg is None:
            return

        def work():
            for iid, r in rows:
                loc = r["local"]
                if loc.get("changes"):   # commit first so the pull can rebase cleanly
                    self.git(["git", "add", "-A"], loc["path"])
                    self.git(["git", "commit", "-m", msg or "Update"], loc["path"])
                if loc.get("upstream"):
                    self.git(["git", "fetch", "--quiet"], loc["path"], quiet=True)
                loc = local_info(loc["path"], False)
                if loc["behind"] and not self._pull_one(loc, allow_rebase=True):
                    continue
                loc = local_info(loc["path"], False)
                if loc["ahead"] or not loc.get("upstream") or loc.get("changes"):
                    self._push_one(loc, msg or "Update")
                else:
                    self.ui(self.log_line, f"{loc['name']}: in sync ✔", "ok")
            self.refresh_rows([i for i, _ in rows])
        self.enqueue(f"Syncing {len(rows)} repo(s)…", work)

    def do_clone(self, sel=None, confirm=True):
        sel = sel or self._need_selection()
        if not sel:
            return
        rows = [(i, r) for i, r in sel if r["gh"] and not r["local"]]
        if not rows:
            messagebox.showinfo(APP_NAME, "Select repos marked 'Not on this PC' to clone them.")
            return
        root = Path(self.cfg["root"])
        plan = []
        for iid, r in rows:
            target = root / r["gh"]["name"]
            if target.exists():
                new = simpledialog.askstring(
                    APP_NAME, f"A folder named '{target.name}' already exists here.\n"
                              f"Choose another folder name for {r['gh']['name']}:",
                    initialvalue=f"{r['gh']['name']}-github", parent=self)
                if not new:
                    continue
                target = root / new
                if target.exists():
                    messagebox.showerror(APP_NAME, f"'{new}' also exists. Skipping.")
                    continue
            plan.append((iid, r, target))
        if not plan:
            return
        if confirm and len(plan) > 1 and not messagebox.askyesno(
                APP_NAME, f"Clone {len(plan)} repos into {root}?"):
            return

        def replace_row(old_iid, new_iid, row):
            self.rows.pop(old_iid, None)
            self.rows[new_iid] = row
            self.render()

        def work():
            for iid, r, target in plan:
                ok, _ = self.git(["git", "clone", r["gh"]["url"] + ".git", str(target)], str(root), timeout=900)
                if ok:
                    self.ui(self.log_line, f"{r['gh']['name']}: cloned to {target} ✔", "ok")
                    row = {"gh": r["gh"], "local": local_info(target, False),
                           "remote_key": r["gh"]["nameWithOwner"].lower()}
                    self.ui(replace_row, iid, "L:" + str(target), row)
        self.enqueue(f"Cloning {len(plan)} repo(s)…", work)

    def do_publish(self):
        sel = self._need_selection()
        if not sel:
            return
        if len(sel) > 1:
            messagebox.showinfo(APP_NAME, "Publish one folder at a time.")
            return
        iid, r = sel[0]
        loc = r["local"]
        if not loc or (loc["is_git"] and loc["remotes"]):
            messagebox.showinfo(APP_NAME, "Publish is for folders that are only on this PC "
                                          "(not yet linked to GitHub).")
            return
        name = r["gh"]["name"] if r["gh"] else re.sub(r"[^A-Za-z0-9._-]+", "-", loc["name"]).strip("-")
        name = simpledialog.askstring(APP_NAME, "Name of the GitHub repository:", initialvalue=name, parent=self)
        if not name:
            return
        existing = next((x["gh"] for x in self.rows.values()
                         if x["gh"] and x["gh"]["name"].lower() == name.lower()), None)
        if existing and not existing["_empty"]:
            messagebox.showerror(APP_NAME, f"'{name}' already exists on your GitHub and has content.\n"
                                           "Pick another name, or clone that repo instead.")
            return
        private = None
        if not existing:
            private = messagebox.askyesnocancel(APP_NAME, f"Make '{name}' PRIVATE?\n\nYes = private (only you)\n"
                                                          "No = public (anyone can see)")
            if private is None:
                return
        path = Path(loc["path"])
        if not (path / ".gitignore").exists():
            if not messagebox.askyesno(
                    APP_NAME, "This folder has no .gitignore, so EVERY file in it will be uploaded "
                              "(including build outputs, node_modules, secrets like .env files).\n\nContinue?"):
                return

        def work():
            p = str(path)
            if not loc["is_git"]:
                if not self.git(["git", "init", "-b", "main"], p)[0]:
                    return
            info = local_info(p, False)
            if info.get("changes") or info.get("no_commits"):
                self.git(["git", "add", "-A"], p)
                ok, _ = self.git(["git", "commit", "-m", "Initial commit" if info.get("no_commits")
                                  else f"Publish from {PC_NAME}"], p)
                if not ok:
                    return
            if existing:   # empty repo already on GitHub: just link and push
                ok = (self.git(["git", "remote", "add", "origin", existing["url"] + ".git"], p)[0] and
                      self.git(["git", "push", "-u", "origin", "HEAD"], p, timeout=900)[0])
            else:
                ok, _ = self.git(["gh", "repo", "create", name, "--private" if private else "--public",
                                  "--source", p, "--remote", "origin", "--push"], p, timeout=900)
            if ok:
                self.ui(self.log_line, f"{name}: published to GitHub ✔", "ok")
                self.ui(self.refresh)
        self.enqueue(f"Publishing {name}…", work)

    def pull_all(self):
        sel = [(i, r) for i, r in self.rows.items()
               if not self.is_ignored(i) and compute_status(r)[1] in ("behind",)]
        if not sel:
            messagebox.showinfo(APP_NAME, "Nothing to pull — no repos are behind GitHub.\n"
                                          "(Diverged repos must be synced individually.)")
            return
        names = "\n".join("  • " + r["local"]["name"] for _, r in sel)
        if messagebox.askyesno(APP_NAME, f"Pull the latest changes from GitHub into:\n{names}"):
            self.do_pull(sel)

    def clone_all(self):
        sel = [(i, r) for i, r in self.rows.items()
               if not self.is_ignored(i) and r["gh"] and not r["local"]]
        if not sel:
            messagebox.showinfo(APP_NAME, "Every GitHub repo is already on this PC.")
            return
        names = "\n".join("  • " + r["gh"]["name"] for _, r in sel[:30]) + ("\n  …" if len(sel) > 30 else "")
        if messagebox.askyesno(APP_NAME, f"Clone {len(sel)} repos into {self.cfg['root']}?\n\n{names}\n\n"
                                         "Tip: right-click → 'Ignore on this PC' hides repos you don't want here."):
            self.do_clone(sel, confirm=False)

    def open_folder(self):
        for _, r in self.selected()[:5]:
            if r["local"]:
                os.startfile(r["local"]["path"])

    def open_github(self):
        for _, r in self.selected()[:5]:
            if r["gh"]:
                webbrowser.open(r["gh"]["url"])
            elif r.get("remote_key"):
                webbrowser.open("https://github.com/" + r["remote_key"])

    # ---------- settings
    def change_root(self):
        d = filedialog.askdirectory(initialdir=self.cfg["root"], title="Folder that holds your projects")
        if d:
            self.cfg["root"] = str(Path(d))
            save_config(self.cfg)
            self._update_root_label()
            self.refresh()

    def create_shortcut(self):
        pyw = Path(sys.executable)
        if pyw.name.lower() == "python.exe":
            pyw = pyw.with_name("pythonw.exe")
        script = Path(__file__).resolve()
        ico = script.with_name("github_sync.ico")
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
            "[Environment]::GetFolderPath('Desktop')+'\\GitHub Sync.lnk');"
            f"$s.TargetPath='{pyw}';$s.Arguments='\"{script}\"';"
            f"$s.WorkingDirectory='{script.parent}';"
            + (f"$s.IconLocation='{ico}';" if ico.exists() else "")
            + "$s.Save()"
        )
        rc, _, err = run(["powershell", "-NoProfile", "-Command", ps])
        if rc:
            messagebox.showerror(APP_NAME, f"Could not create shortcut:\n{err}")
        else:
            messagebox.showinfo(APP_NAME, "A 'GitHub Sync' shortcut was added to your desktop.")


if __name__ == "__main__":
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on high-DPI screens
        except Exception:
            pass
    App().mainloop()
