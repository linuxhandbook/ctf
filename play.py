#!/usr/bin/env python3
"""Linux Handbook CTF: capture-the-flag games you play in your terminal.

Needs Python 3.7+ and Docker. Levels run in containers on this machine; the
Linux Handbook server only checks flags and keeps the leaderboards.

    python3 play.py          play (picks up where you left off)
    python3 play.py link     link your handle to your Linux Handbook account
    python3 play.py -r       forget this computer's handle and start over
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

try:
    import pwd
except ImportError:   # Windows: play inside WSL instead
    pwd = None

# ── ANSI palette ─────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"

BCYAN   = "\033[96m"
BMAGENTA= "\033[95m"
BGREEN  = "\033[92m"
BYELLOW = "\033[93m"
BRED    = "\033[91m"
WHITE   = "\033[97m"

# ── Constants ─────────────────────────────────────────────────
VERSION       = "3.0"
BACKEND_URL   = os.environ.get("CTF_BACKEND_URL", "https://ctf.linuxhandbook.com").rstrip("/")
CTF_PAGE      = "https://linuxhandbook.com/ctf/"
SECRET_HEADER = "X-Player-Secret"
HANDLE_RE     = re.compile(r"^[A-Za-z0-9_-]{2,20}$")
W             = 58   # box width

secret = ""   # this computer's key, from ~/.ctf_user


def real_home():
    """Home of the person playing, even under sudo, so ~/.ctf_user is the same
    file with or without sudo."""
    user = os.environ.get("SUDO_USER")
    if pwd and user and os.geteuid() == 0:
        try:
            return pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


user_file_path = os.path.join(real_home(), ".ctf_user")

# ── UI helpers ───────────────────────────────────────────────
ANSI_RE = re.compile(r'\033\[[0-9;]*m')

def clear():
    print("\033[H\033[2J", end="", flush=True)

def cx(text, width=W):
    """Center text in given visible width (ignores ANSI codes)."""
    pad = max(0, width - len(ANSI_RE.sub('', text)))
    left = pad // 2
    return ' ' * left + text + ' ' * (pad - left)

def box_line(content, width=W):
    pad = max(0, width - len(ANSI_RE.sub('', content)))
    return f"  {BCYAN}║{RESET}{content}{' ' * pad}{BCYAN}║{RESET}"

def box_top(width=W):
    return f"  {BCYAN}╔{'═' * width}╗{RESET}"

def box_sep(width=W):
    return f"  {BCYAN}╠{'═' * width}╣{RESET}"

def box_bot(width=W):
    return f"  {BCYAN}╚{'═' * width}╝{RESET}"

def box(title, lines, width=W):
    print(box_top(width))
    print(box_line(cx(f"{BMAGENTA}{BOLD}{title}{RESET}", width), width))
    print(box_sep(width))
    for line in lines:
        print(box_line(line, width))
    print(box_bot(width))

def say(msg):
    print(f"  {BCYAN}▸{RESET}  {msg}")

def good(msg):
    print(f"  {BGREEN}✔{RESET}  {msg}")

def fail(msg, *more):
    print(f"\n  {BRED}✘{RESET}  {msg}")
    for line in more:
        print(f"     {line}")
    print()

def ask(prompt):
    """input() that returns None on Ctrl-C / Ctrl-D."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

LOGO = [
    f"{BCYAN}   ██████╗████████╗███████╗    {RESET}",
    f"{BCYAN}  ██╔════╝╚══██╔══╝██╔════╝    {RESET}",
    f"{BCYAN}  ██║        ██║   █████╗      {RESET}",
    f"{BCYAN}  ██║        ██║   ██╔══╝      {RESET}",
    f"{BCYAN}  ╚██████╗   ██║   ██║         {RESET}",
    f"{BCYAN}   ╚═════╝   ╚═╝   ╚═╝         {RESET}",
]

def print_boot():
    clear()
    print()
    for line in LOGO:
        print(line)
    print(f"  {BMAGENTA}{'─' * W}{RESET}")
    print(f"  {cx(f'{BMAGENTA}{BOLD}  CAPTURE THE FLAG  //  LINUX HANDBOOK  {RESET}')}")
    print(f"  {BMAGENTA}{'─' * W}{RESET}")
    print()

# ── Backend API ──────────────────────────────────────────────
def api(method, path, body=None, timeout=30):
    """Call the backend. Returns (status, payload); status is None if the
    server couldn't be reached, and payload is then the error text."""
    headers = {"User-Agent": f"linuxhandbook-ctf-play/{VERSION}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if secret:
        headers[SECRET_HEADER] = secret
    req = urllib.request.Request(BACKEND_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return None, str(getattr(e, "reason", e))
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode(errors="replace")

def error_message(status, payload):
    if isinstance(payload, dict) and payload.get("error"):
        return payload["error"]
    return f"HTTP {status}" if status else str(payload)

def get_me():
    """Return (me, status). me is None when this computer has no handle (404)
    or the server failed."""
    status, payload = api("GET", "/api/me", timeout=20)
    if status == 200 and isinstance(payload, dict):
        return payload, 200
    if status != 404:
        fail(f"Server error: {error_message(status, payload)}")
    return None, status

# ── Identity ─────────────────────────────────────────────────
# There's no password. This computer gets a random key, stored in ~/.ctf_user,
# and the server ties your handle to it. Link the handle to a free Linux
# Handbook account and you can sign in on other computers too.

def load_secret():
    try:
        with open(user_file_path) as f:
            s = json.load(f).get("secret")
    except (OSError, ValueError, AttributeError):
        return None
    return s if isinstance(s, str) and len(s) >= 32 else None

def save_identity(handle=None):
    fd = os.open(user_file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)   # O_CREAT's mode doesn't apply to an existing file
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):   # keep it the user's under sudo
        try:
            os.fchown(fd, int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", -1)))
        except (OSError, ValueError):
            pass
    with os.fdopen(fd, "w") as f:
        json.dump({"secret": secret, "handle": handle}, f)

def choose_handle(link_code=None):
    """Ask for a handle until the server accepts one. Returns it, or None."""
    print()
    print(f"  {DIM}2-20 characters: letters, digits, _ and -. It's shown on the leaderboard.{RESET}")
    while True:
        raw = ask(f"  {BMAGENTA}HANDLE {BCYAN}▶{RESET}  ")
        if raw is None:
            return None
        handle = re.sub(r"[^A-Za-z0-9_-]", "", raw)[:20]
        if len(handle) < 2:
            print(f"  {BRED}✘  Use at least 2 of A-Z a-z 0-9 _ -{RESET}\n")
            continue
        if handle != raw:
            say(f"Using {BCYAN}{handle}{RESET}")
        body = {"handle": handle}
        if link_code:
            body["linkCode"] = link_code
        status, payload = api("POST", "/api/players", body)
        if status == 201:
            save_identity(handle)
            good(f"Handle claimed. Welcome, {BCYAN}{BOLD}{handle}{RESET}!")
            time.sleep(0.8)
            return handle
        msg = error_message(status, payload)
        if status in (403, 409):
            print(f"  {BRED}✘  '{handle}': {msg}. Pick another.{RESET}\n")
            continue
        fail(f"Couldn't claim the handle: {msg}")
        return None

def link_account():
    """Open linuxhandbook.com in the browser to link (or sign in with) a Linux
    Handbook account. Returns the final status and the code used."""
    status, payload = api("POST", "/api/link/start")
    if status != 200:
        fail(f"Couldn't start: {error_message(status, payload)}")
        return None, None
    code, url = payload["code"], payload["url"]
    print()
    box("🔗  LINUX HANDBOOK ACCOUNT", [
        f"  Open this page and sign in (free accounts work):",
        f"  {BCYAN}{url}{RESET}",
        "",
        f"  Code: {BYELLOW}{BOLD}{code}{RESET}   {DIM}(expires in 10 minutes){RESET}",
    ], width=max(W, len(url) + 4))
    if os.geteuid() != 0:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(f"\n  {DIM}Waiting for you in the browser... Ctrl+C to cancel.{RESET}", flush=True)
    deadline = time.time() + payload.get("expiresIn", 600)
    try:
        while time.time() < deadline:
            time.sleep(2)
            st, res = api("GET", "/api/link/status?" + urllib.parse.urlencode({"code": code}), timeout=10)
            state = res.get("status") if isinstance(res, dict) else None
            if state in ("linked", "signed-in", "pick-handle", "expired"):
                return state, code
    except KeyboardInterrupt:
        print()
        return "cancelled", code
    return "expired", code

def welcome_new_computer():
    """First run on this computer: new handle, or sign in to an existing one."""
    print_boot()
    box("👋  WELCOME", [
        f"  {BCYAN}1{RESET}  I'm new here: pick a handle",
        f"  {BCYAN}2{RESET}  I've played before: sign in with my",
        f"     Linux Handbook account",
    ])
    while True:
        choice = ask(f"\n  {BMAGENTA}CHOOSE {BCYAN}▶{RESET}  ")
        if choice is None:
            return False
        if choice == "1":
            return choose_handle() is not None
        if choice == "2":
            state, code = link_account()
            if state == "signed-in":
                good("Signed in. Welcome back!")
                time.sleep(0.8)
                return True
            if state == "pick-handle":
                good("Signed in. Your account doesn't have a handle yet, so let's pick one.")
                return choose_handle(link_code=code) is not None
            fail("Sign-in didn't finish." if state != "cancelled" else "Cancelled.")
            return False

# ── Docker ───────────────────────────────────────────────────
def docker(*args):
    """Run a docker command quietly; returns the exit code."""
    return subprocess.call(["docker", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def check_docker():
    """Return True if Docker is usable, else explain what to do."""
    if not shutil.which("docker"):
        fail("Docker is not installed.",
             f"Install it from {BCYAN}https://docs.docker.com/engine/install/{RESET}",
             "(on macOS, Docker Desktop; on Windows, play inside WSL 2)")
        return False
    r = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode == 0:
        return True
    if "permission denied" in r.stderr.lower():
        fail("You don't have permission to use Docker.",
             f"Run {BCYAN}sudo python3 play.py{RESET}, or add yourself to the docker group:",
             f"{BCYAN}sudo usermod -aG docker $USER{RESET}  (then log out and back in)")
    else:
        fail("Docker is installed but not running.",
             f"Linux: {BCYAN}sudo systemctl start docker{RESET}   macOS: start Docker Desktop")
    return False

def have_image(lvl):
    return docker("image", "inspect", lvl["image"]) == 0

def pull_image(lvl):
    for attempt in range(3):
        if docker("pull", lvl["image"]) == 0:
            return True
        time.sleep(2 * (attempt + 1))
    return False

def ensure_image(lvl):
    if have_image(lvl):
        return True
    say(f"Downloading level {lvl['level']}...  ")
    if pull_image(lvl):
        return True
    fail(f"Couldn't download {lvl['image']}.", "Check your internet connection and try again.")
    return False

def prefetch(lvl):
    """Pull the next level in the background while this one is played."""
    if lvl:
        threading.Thread(target=lambda: have_image(lvl) or pull_image(lvl), daemon=True).start()

class Level:
    """One level's container."""

    def __init__(self, ctf, lvl, handle):
        self.ctf, self.lvl, self.handle = ctf, lvl, handle
        self.name = f"lhb-{ctf['id']}-l{lvl['level']}"

    def start(self):
        docker("rm", "-f", self.name)
        docker("run", "-dit", "--hostname", self.handle, "--user", self.lvl["user"],
               "--name", self.name, self.lvl["image"], "tail", "-f", "/dev/null")

    def running(self):
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self.name],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        return r.stdout.strip() == "true"

    def shell(self):
        if not self.running():
            self.start()
        subprocess.call(["docker", "exec", "-it", "-u", self.lvl["user"],
                         "-w", self.ctf.get("workdir") or "/", self.name, "sh"])
        try:   # docker exec can leave stdin at EOF; reopen the terminal
            sys.stdin = open("/dev/tty")
        except OSError:
            pass

    def stop(self):
        docker("rm", "-f", self.name)

    def remove(self):
        self.stop()
        docker("rmi", self.lvl["image"])

# ── Leaderboard ──────────────────────────────────────────────
def show_leaderboard(ctf):
    status, users = api("GET", f"/api/ctfs/{ctf['id']}/leaderboard", timeout=10)
    if status != 200 or not isinstance(users, list):
        fail("Leaderboard unavailable right now.")
        return
    w = 54
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    colors = {1: BYELLOW, 2: BCYAN, 3: BMAGENTA}
    rows = [f"{DIM}  {'#':>2}   {'HANDLE':<16}  {'SCORE':>8}  {'LEVELS':>6}  {RESET}"]
    if not users:
        rows.append(cx(f"{DIM}Nobody has scored yet. Be the first!{RESET}", w))
    for i, u in enumerate(users[:10], 1):
        row = (f"  {medals.get(i, '  ')}{i:>2}   {u.get('handle', '?')[:15]:<15}  "
               f"{u.get('score', 0):>8}  {len(u.get('solvedLevels', [])):>6}  ")
        rows.append(f"{colors.get(i, WHITE)}{row}{RESET}")
    print()
    box(f"🏆  {ctf['title'].upper()}: TOP 10", rows, width=w)
    print(f"  {DIM}Full leaderboard: {ctf.get('page') or CTF_PAGE}{RESET}\n")

# ── Playing a CTF ────────────────────────────────────────────
def print_hud(ctf, n, handle):
    clear()
    print()
    total = len(ctf["levels"])
    box(f"◈  {ctf['title'].upper()} // LEVEL {n:02d} of {total:02d}  ◈", [
        cx(f"{DIM}Playing as {BCYAN}{handle}{RESET}"),
        "",
        f"  {BCYAN}play{RESET}           {DIM}Enter the level ('exit' to come back){RESET}",
        f"  {BCYAN}submit <FLAG>{RESET}  {DIM}Submit the flag you found{RESET}",
        f"  {BCYAN}leaderboard{RESET}    {DIM}Top 10 for this CTF{RESET}",
        f"  {BCYAN}restart{RESET}        {DIM}Reset this level to a fresh state{RESET}",
        f"  {BCYAN}menu{RESET}           {DIM}Back to the list of CTFs{RESET}",
        f"  {BCYAN}quit{RESET}           {DIM}Leave; your progress is saved{RESET}",
    ])
    print(f"  {DIM}Start with {BCYAN}play{DIM}, then {BCYAN}cat hint.txt{DIM} inside the level.{RESET}\n")

def print_victory(ctf, handle):
    clear()
    print()
    for line in LOGO:
        print(line)
    print()
    print(f"  {BGREEN}{'═' * W}{RESET}")
    print(f"  {BGREEN}{BOLD}{'🎉  MISSION ACCOMPLISHED  🎉':^{W}}{RESET}")
    print(f"  {BGREEN}{BOLD}{(ctf['title'].upper() + ' CLEARED'):^{W}}{RESET}")
    print(f"  {BGREEN}{'═' * W}{RESET}\n")
    box("WELL PLAYED", [
        cx(f"{BYELLOW}{BOLD}{handle}{RESET}"),
        cx(f"{DIM}Your name is on the leaderboard:{RESET}"),
        cx(f"{BCYAN}{ctf.get('page') or CTF_PAGE}{RESET}"),
    ])
    ask(f"\n  {DIM}Press {BCYAN}Enter{DIM} to go back to the menu...{RESET}")

def play_ctf(ctf, n, handle):
    """Play from level n. Returns "menu" or "quit"."""
    levels = ctf["levels"]
    while n <= len(levels):
        lvl = levels[n - 1]
        if not ensure_image(lvl):
            return "quit"
        level = Level(ctf, lvl, handle)
        if not level.running():
            level.start()
        prefetch(levels[n] if n < len(levels) else None)
        print_hud(ctf, n, handle)

        while True:
            raw = ask(f"  {BMAGENTA}[{ctf['id'][:3].upper()}:L{n:02d}]{RESET}{BCYAN}▶ {RESET}")
            if raw is None or raw.lower() in ("quit", "exit", "q"):
                level.stop()
                return "quit"
            cmd = raw.lower()
            if not cmd:
                continue
            if cmd in ("play", "restart"):
                if cmd == "restart":
                    say(f"Resetting level {n}...")
                    level.start()
                print(f"\n  {DIM}Launching shell. Type {BCYAN}exit{DIM} to come back here.{RESET}\n")
                level.shell()
                ask(f"\n  {BGREEN}Got the flag? Copy it first!{RESET} {DIM}Press {BCYAN}Enter{DIM} for the menu...{RESET}")
                print_hud(ctf, n, handle)
            elif cmd == "submit" or cmd.startswith("submit "):
                flag = raw[7:].strip()
                if not flag:
                    print(f"\n  {BRED}▸{RESET}  Usage: {BCYAN}submit LHB{{...}}{RESET}\n")
                    continue
                print(f"\n  {DIM}Checking...{RESET}", end="", flush=True)
                status, res = api("POST", f"/api/ctfs/{ctf['id']}/submit", {"flag": flag})
                if status == 200 and res.get("correct"):
                    print(f"\r  {BGREEN}✔  Flag accepted! Level {n} cleared.{RESET}\n")
                    level.remove()
                    time.sleep(0.8)
                    n = res["level"]
                    break
                if status == 200:
                    print(f"\r  {BRED}✘  Not the flag. Keep digging.{RESET}")
                    if res.get("note"):
                        print(f"  {DIM}({res['note']}){RESET}")
                    print()
                elif status == 429:
                    print(f"\r  {BYELLOW}▸  Too many tries. Wait a few seconds.{RESET}\n")
                else:
                    print(f"\r  {BRED}▸{RESET}  {error_message(status, res)}\n")
            elif cmd == "leaderboard":
                show_leaderboard(ctf)
            elif cmd == "help":
                print_hud(ctf, n, handle)
            elif cmd == "menu":
                level.stop()
                return "menu"
            else:
                print(f"\n  {BRED}▸{RESET}  Unknown command. Type {BCYAN}help{RESET} to see them.\n")

    print_victory(ctf, handle)
    return "menu"

# ── CTF menu ─────────────────────────────────────────────────
def menu(ctfs):
    while True:
        me, status = get_me()
        if me is None:
            return
        print_boot()
        account = (f"{BGREEN}linked to Linux Handbook{RESET}" if me["member"]
                   else f"{DIM}not linked (type {BCYAN}link{DIM}){RESET}")
        lines = [f"  Handle: {BCYAN}{BOLD}{me['handle']}{RESET}   {account}", ""]
        for i, c in enumerate(ctfs, 1):
            p = me["progress"].get(c["id"], {"level": 1, "score": 0})
            total = len(c["levels"])
            if p.get("locked"):
                state = f"{BYELLOW}🔒 {'members' if c['access'] == 'members' else 'paid members'}{RESET}"
            elif p["level"] > total:
                state = f"{BGREEN}★ cleared, {p['score']} pts{RESET}"
            elif p["level"] > 1:
                state = f"{DIM}level {p['level']}/{total}, {p['score']} pts{RESET}"
            else:
                state = f"{DIM}{total} levels{RESET}"
            lines.append(f"  {BCYAN}{i}{RESET}  {c['title']:<24} {state}")
        box("🏴  CAPTURE THE FLAG", lines)
        print(f"  {DIM}Type a number to play, {BCYAN}link{DIM} to link your account, or {BCYAN}quit{DIM}.{RESET}")
        print(f"  {DIM}About the games: {CTF_PAGE}{RESET}")

        choice = ask(f"\n  {BMAGENTA}▶{RESET}  ")
        if choice is None or choice.lower() in ("quit", "exit", "q"):
            return
        if choice.lower() == "link":
            if me["member"]:
                good("This handle is already linked. Linking again refreshes your membership status.")
            state, _ = link_account()
            if state == "linked":
                good("Linked! You can now sign in with this handle on any computer.")
            elif state not in ("cancelled",):
                fail("Linking didn't finish.")
            ask(f"  {DIM}Press {BCYAN}Enter{DIM}...{RESET}")
            continue
        if not choice.isdigit() or not 1 <= int(choice) <= len(ctfs):
            continue
        c = ctfs[int(choice) - 1]
        p = me["progress"].get(c["id"], {"level": 1})
        if p.get("locked"):
            fail(p["locked"])
            ask(f"  {DIM}Press {BCYAN}Enter{DIM}...{RESET}")
            continue
        if p["level"] > len(c["levels"]):
            show_leaderboard(c)
            ask(f"  {DIM}You've cleared this one. Press {BCYAN}Enter{DIM}...{RESET}")
            continue
        if play_ctf(c, p["level"], me["handle"]) == "quit":
            return

# ── Main ─────────────────────────────────────────────────────
def reset_identity():
    if os.path.isfile(user_file_path):
        print(f"  {BYELLOW}▸{RESET}  This forgets this computer's key. Unless your handle is linked")
        print(f"     to a Linux Handbook account, you can't play as it again.")
        if (ask(f"  {BMAGENTA}Type 'yes' to continue {BCYAN}▶{RESET}  ") or "").lower() != "yes":
            print(f"  {DIM}Aborted.{RESET}\n")
            return
        os.remove(user_file_path)
    say("Done. The next run starts fresh.\n")

def main():
    global secret
    args = sys.argv[1:]
    if args and args[0] == "-r":
        return reset_identity()
    if args and args[0] not in ("link",):
        return print(__doc__)

    print_boot()
    print(f"  {BCYAN}▸{RESET}  Checking Docker...  ", end="", flush=True)
    if not check_docker():
        return
    print(f"{BGREEN}OK{RESET}")
    print(f"  {BCYAN}▸{RESET}  Reaching Linux Handbook...  ", end="", flush=True)
    # CTF_SHOW_HIDDEN=1 also lists CTFs that aren't launched yet, for testing.
    listing = "/api/ctfs?hidden=1" if os.environ.get("CTF_SHOW_HIDDEN") == "1" else "/api/ctfs"
    status, ctfs = api("GET", listing, timeout=20)
    if status != 200 or not isinstance(ctfs, list):
        fail("Can't reach the CTF server.", f"Check your connection or try again later ({BACKEND_URL}).")
        return
    print(f"{BGREEN}OK{RESET}")
    time.sleep(0.4)

    secret = load_secret()
    if not secret:
        secret = secrets.token_hex(32)
        save_identity()

    me, status = get_me()
    if me is None:
        if status != 404 or not welcome_new_computer():
            return
    elif args and args[0] == "link":
        state, _ = link_account()
        if state == "linked":
            good("Linked! You can now sign in with this handle on any computer.")
        else:
            fail("Linking didn't finish.")
        return

    try:
        menu(ctfs)
    except KeyboardInterrupt:
        print()
    print(f"\n  {BMAGENTA}{'  Progress saved. Run play.py again to continue.  ':^{W}}{RESET}\n")

if __name__ == "__main__":
    main()
