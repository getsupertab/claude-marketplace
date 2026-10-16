#!/usr/bin/env python3
"""Plugin glue. The keeptabs code runs from the plugin (../keeptabs); its data
lives in the plugin's data folder (${CLAUDE_PLUGIN_DATA}), which Claude Code
deletes when the plugin is uninstalled. This script keeps that folder and the
collector in place.

  home.py session-start   prepare the folder, start the collector, print a SessionStart message
  home.py ensure          the same, silent (used by the guard on each prompt)
  home.py status          print what the plugin sees
  home.py stop-collector  stop a running collector and keep it stopped for the rest of
                          the session (before uninstalling); a session start brings it back

The plugin itself writes only budget.json there, once, if it is missing; it is
the user's file from then on. ledger/, raw/ and state/ are written by the
keeptabs code.
"""
import fcntl, json, os, signal, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = os.path.join(ROOT, "keeptabs")
COLLECTOR = os.path.join(CODE, "collector.py")
# Hooks get CLAUDE_PLUGIN_DATA; commands run through the Bash tool do not.
DATA = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/plugins/data/keeptabs-supertab")
STATE = os.path.join(DATA, "state")
PIDFILE = os.path.join(STATE, "collector.pid")
HEART = os.path.join(STATE, "collector.json")
STALE_HEARTBEAT = 60   # seconds; the collector writes every 15
LOCK = os.path.join(STATE, "collector.lock")
LOG = os.path.join(STATE, "collector.log")
STOPPED = os.path.join(STATE, "collector.stopped")   # written by stop-collector; ensure then leaves it down
SETTINGS = os.path.expanduser("~/.claude/settings.json")
LEGACY_PLIST = os.path.expanduser("~/Library/LaunchAgents/co.supertab.keeptabs.collector.plist")
PROBE = "http://127.0.0.1:4318/health"
TELEMETRY = {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_LOGS_EXPORTER": "otlp",
             "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
             "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"}

# http.server looks up the machine's full hostname before it serves anything.
# On macOS that reverse lookup of 127.0.0.1 can trigger the Local Network
# prompt and hang for 20s or more, and telemetry sent meanwhile can time out.
# The name is only used for display, so the lookup is stubbed out.
LAUNCH = ("import runpy, socket, sys; socket.getfqdn = lambda name='': name or 'localhost'; "
          "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_if_changed(path, text):
    try:
        if open(path).read() == text:
            return
    except OSError:
        pass
    tmp = path + ".plugin-tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


DEAD_BUDGET_KEYS = ("session_limit_tokens", "daily_limit_tokens", "turn_limit_tokens")
OLD_TURN_LIMIT_USD = 2            # the token-era default, when a token ceiling bit first
OLD_TURN_LIMIT_TOKENS = 100000    # the ceiling that bit first, at about $0.59


def migrate_budget(path):
    """Budgets are dollars now. Drop the keys the guard stopped reading, and move
    the request limit off the old default once, since without its token ceiling it
    would be more than three times looser. Returns a message, or None."""
    budget = load(path, None)
    if not isinstance(budget, dict):
        return None
    dead_keys = [key for key in DEAD_BUDGET_KEYS if key in budget]
    if not dead_keys:
        return None
    # Decided before the dead keys go: $2 alone cannot tell the old default from a
    # limit someone chose, so only move it when the token ceiling was also untouched.
    request_limit_moved = (float(budget.get("turn_limit_usd") or 0) == OLD_TURN_LIMIT_USD
                           and budget.get("turn_limit_tokens") == OLD_TURN_LIMIT_TOKENS)
    for dead_key in dead_keys:
        del budget[dead_key]
    shipped = load(os.path.join(CODE, "budget.json"), {})
    if request_limit_moved:
        budget["turn_limit_usd"] = shipped.get("turn_limit_usd", 0.6)
    # Only replace the shipped wording, never a comment the user rewrote.
    if "_limit_tokens" in str(budget.get("_comment", "")):
        budget["_comment"] = shipped.get("_comment", budget["_comment"])
    if "200k:" in str(budget.get("_job_comment", "")):
        budget["_job_comment"] = shipped.get("_job_comment", budget["_job_comment"])
    write_if_changed(path, json.dumps(budget, indent=2) + "\n")
    return ("keeptabs: budgets are in dollars now, so " + ", ".join(dead_keys) + " no longer "
            "did anything and " + ("were" if len(dead_keys) > 1 else "was") + " removed from "
            "your budget.json."
            + (f" Your request limit moved from ${OLD_TURN_LIMIT_USD:,.2f} to "
               f"${float(budget['turn_limit_usd']):,.2f}, which is about where the token "
               f"ceiling used to stop a request. Change it with /keeptabs:budget."
               if request_limit_moved else ""))


def prepare(quiet=True):
    """Returns messages for the user, if any."""
    os.makedirs(STATE, exist_ok=True)
    budget_path = os.path.join(DATA, "budget.json")
    if not os.path.exists(budget_path):
        write_if_changed(budget_path, open(os.path.join(CODE, "budget.json")).read())
        return []
    # Only at session start: a limit must not move mid-session with no one told.
    migration_message = None if quiet else migrate_budget(budget_path)
    return [migration_message] if migration_message else []


def probe():
    """(pid, /health payload) of the running collector, or (None, None). Any
    keeptabs collector counts, including one an older manual install started
    under launchd. The payload is {} for a collector that has started but is
    not answering yet (known from our pidfile)."""
    try:
        with urllib.request.urlopen(PROBE, timeout=0.5) as r:
            h = json.load(r)
            return int(h.get("pid")), h
    except Exception:
        pass
    # Started but not answering yet: trust our pidfile if that process is a collector.
    try:
        pid = int(open(PIDFILE).read().strip())
        return (pid, {}) if "keeptabs/collector.py" in command(pid) else (None, None)
    except (OSError, ValueError):
        return None, None


def stuck(pid, health):
    """Why a collector that answers on the port must be replaced, or None.
    Answering is not enough: if the data folder was deleted under it (Claude
    Code does that on uninstall) it keeps serving but writes its heartbeat
    nowhere, and health.py then reports it as never run. Older collectors
    exit on their own in that state only from this version on."""
    if COLLECTOR not in command(pid):
        return "runs another plugin version"
    if not health:            # still starting, give it a moment
        return None
    if health.get("data") != DATA:
        return f"writes to {health.get('data') or 'an unknown folder'}, not {DATA}"
    now = datetime.now(timezone.utc)
    try:
        alive = datetime.fromisoformat(json.load(open(HEART))["alive_at"])
    except (OSError, ValueError, KeyError, TypeError):
        try:   # answering before its first heartbeat write is not stuck yet
            if (now - datetime.fromisoformat(health["started_at"])).total_seconds() < STALE_HEARTBEAT:
                return None
        except (ValueError, KeyError, TypeError):
            pass
        return "answers but writes no heartbeat"
    if (now - alive).total_seconds() > STALE_HEARTBEAT:
        return f"answers but its heartbeat stopped at {alive.astimezone().strftime('%H:%M')}"
    return None


def command(pid):
    return subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                          capture_output=True, text=True).stdout


def port_taken():
    import socket
    s = socket.socket()
    try:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", 4318)) == 0
    finally:
        s.close()


def stop(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        if not port_taken():
            return
        time.sleep(0.1)


def ensure_collector():
    """Start the collector if it is not running, or replace it when it runs
    code other than this plugin version's (after an update) or is stuck (see
    stuck()). Returns a problem or None. One instance per machine: a lock
    serialises sessions starting together, and the collector itself cannot
    start twice because it binds port 4318."""
    os.makedirs(STATE, exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        pid, health = probe()
        if pid and stuck(pid, health):
            stop(pid)
            pid = None
        if pid:
            return None
        if port_taken():
            return ("port 4318 is in use by another program, so the collector cannot start. "
                    "Telemetry will not be recorded until it is free.")
        with open(LOG, "a") as log:
            p = subprocess.Popen([sys.executable, "-c", LAUNCH, COLLECTOR],
                                 cwd=DATA, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
        for _ in range(30):
            if p.poll() is not None:
                break
            if port_taken():
                with open(PIDFILE, "w") as f:
                    f.write(f"{p.pid}\n")
                return None
            time.sleep(0.1)
        return f"the collector did not start (see {LOG})."


def legacy_problems():
    """Wiring left by the POC's own setup.py that would clash with the plugin."""
    out = []
    hooks = (load(SETTINGS, {}) or {}).get("hooks") or {}
    if any("keeptabs/guard.py" in str(h.get("command"))
           for groups in hooks.values() for g in groups or [] for h in g.get("hooks") or []):
        out.append("~/.claude/settings.json still has guard hooks from the old setup.py, so the "
                   "guard runs twice. Remove those hook entries, then run /keeptabs:setup again.")
    if os.path.exists(LEGACY_PLIST):
        out.append("the old setup.py's launchd job is still registered and restarts an outdated "
                   "collector. Remove it: launchctl bootout gui/$(id -u)/co.supertab.keeptabs.collector "
                   f"and delete {LEGACY_PLIST}.")
    return out


def telemetry_state():
    """"off" or "on": whether telemetry is set in settings.json. Whether this
    session actually sends it cannot be read from the environment (Claude Code
    does not pass the OTel settings on to hooks or child processes); the guard
    finds out from the ledger after the session's first reply (health.py)."""
    env = (load(SETTINGS, {}) or {}).get("env") or {}
    return "off" if any(str(env.get(k)) != v for k, v in TELEMETRY.items()) else "on"


def run(quiet):
    try:
        msgs = prepare(quiet)
    except OSError as e:
        return [f"keeptabs: could not set up {DATA}: {e}"]
    if quiet and os.path.exists(STOPPED):
        return msgs      # stop-collector ran: stay down until the next session start
    problem = ensure_collector()
    if problem:
        msgs.append(f"keeptabs: {problem}")
    if quiet:
        return msgs
    msgs += [f"keeptabs: {p}" for p in legacy_problems()]
    if telemetry_state() == "off":
        msgs.append("keeptabs: tracking from transcripts only, so figures are INCOMPLETE (they "
                    "miss background calls and web searches). Run /keeptabs:setup and restart "
                    "Claude Code for full tracking.")
    return msgs


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "session-start":
        try:
            json.load(sys.stdin)
        except Exception:
            pass
        try:
            os.remove(STOPPED)
        except OSError:
            pass
        msgs = run(quiet=False)
        if msgs:
            print(json.dumps({"systemMessage": "\n".join(msgs)}))
        return 0
    if cmd == "ensure":
        run(quiet=True)
        return 0
    if cmd == "stop-collector":
        pid, _ = probe()
        if pid:
            stop(pid)
            print(f"stopped collector (pid {pid})")
        else:
            print("collector is not running")
        try:
            os.makedirs(STATE, exist_ok=True)
            open(STOPPED, "w").close()
            print("it stays stopped until the next Claude Code session starts")
        except OSError:
            pass
        return 0
    if cmd == "status":
        print(f"data:      {DATA}")
        print(f"code:      {CODE}")
        pid, health = probe()
        why = stuck(pid, health) if pid else None
        print(f"collector: {'running, pid ' + str(pid) if pid else 'not running'}"
              + (f" ({command(pid).strip()[-60:]})" if pid else "")
              + (f"\n           stuck: {why}; the next prompt replaces it" if why else ""))
        print(f"telemetry: {telemetry_state()}")
        for p in legacy_problems():
            print(f"problem:   {p}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
