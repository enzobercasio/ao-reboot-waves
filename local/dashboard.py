#!/usr/bin/env python3
"""Watch the lab fleet in a browser while the workflow runs.

Reads the per-host state files the playbooks keep, on the lab VM over SSH or
in a local directory, and serves a page that refreshes every 2 seconds:

    python3 local/dashboard.py --ssh lab-user@13.213.151.93
    python3 local/dashboard.py --dir /tmp/rw          # a run on this laptop

Then open http://localhost:8080. Each host shows as waiting, restarting, done,
failed (will be retried), quarantined or down; each tier shows its healthy
hosts against the minimum from inventory/group_vars.
"""

import argparse
import json
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIER_ORDER = ["web", "app", "db"]

# One shell loop prints "<host>\t<json on one line>" for every state file.
REMOTE_READ = ('for f in {dir}/*.json; do [ -e "$f" ] || continue; '
               'printf "%s\\t" "$(basename "$f" .json)"; tr -d "\\n" < "$f"; echo; done')


def read_local(state_dir):
    return {p.stem: json.loads(p.read_text()) for p in Path(state_dir).glob("*.json")}


def read_ssh(target, remote_dir, identity):
    # Reuse one connection: the lab VM drops bursts of new SSH connections.
    # Short path: a socket path must stay under 104 bytes, and macOS's TMPDIR is long.
    sock = Path("/tmp" if Path("/tmp").is_dir() else tempfile.gettempdir()) / "rw-dash-%C"
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
           "-o", "ControlMaster=auto", "-o", "ControlPersist=120", "-o", f"ControlPath={sock}"]
    if identity:
        cmd += ["-i", identity]
    cmd += [target, REMOTE_READ.format(dir=remote_dir)]
    r = subprocess.run(cmd, text=True, capture_output=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"ssh exited with {r.returncode}")
    states = {}
    for line in r.stdout.splitlines():
        host, _, body = line.partition("\t")
        if body:
            states[host] = json.loads(body)
    return states


def policies():
    """reboot_policy per tier, read from group_vars without needing PyYAML."""
    out = {}
    for path in sorted((ROOT / "inventory" / "group_vars").glob("tier_*.yml")):
        text = path.read_text()
        pol = {k: int(v) for k, v in re.findall(r"^\s+(min_available|max_unavailable):\s*(\d+)", text, re.M)}
        pol["allow_full_outage"] = bool(re.search(r"allow_full_outage:\s*true", text, re.I))
        out[path.stem.removeprefix("tier_")] = pol
    return out


def minimum(pol, count):
    # Same rule as playbooks/lib/plan_tier.yml.
    wanted = pol["min_available"] if "min_available" in pol else count - pol.get("max_unavailable", 0)
    return wanted if pol.get("allow_full_outage") else max(wanted, 1)


def status(state, latest_token, max_attempts):
    if state.get("rebooting"):
        return "restarting"
    if not state.get("healthy", True):
        return "down"
    if not state.get("pending"):
        return "done"
    # attempts only count inside the run that made them (attempt_token)
    if state.get("attempts", 0) and state.get("attempt_token") == latest_token:
        return "quarantined" if state["attempts"] >= max_attempts else "failed"
    return "waiting"


def snapshot(states, max_attempts):
    latest = max((s.get("attempt_token") or "" for s in states.values()), default="")
    pols = policies()
    hosts = [{"name": h, "tier": h.split("-")[0], "status": status(s, latest, max_attempts),
              "attempts": s.get("attempts", 0) if s.get("attempt_token") == latest else 0}
             for h, s in sorted(states.items())]
    tiers = []
    for tier in sorted({h["tier"] for h in hosts}, key=lambda t: (TIER_ORDER + [t]).index(t)):
        members = [h for h in hosts if h["tier"] == tier]
        healthy = sum(1 for h in members if h["status"] != "down")
        floor = minimum(pols[tier], len(members)) if tier in pols else None
        tiers.append({"tier": tier, "total": len(members), "healthy": healthy, "minimum": floor,
                      "below": floor is not None and healthy < floor})
    counts = {}
    for h in hosts:
        counts[h["status"]] = counts.get(h["status"], 0) + 1
    return {"hosts": hosts, "tiers": tiers, "counts": counts, "run_token": latest}


class Source:
    """Reads the fleet at most once per interval, however many tabs are open."""

    def __init__(self, args):
        self.args, self.lock, self.cached, self.at = args, threading.Lock(), None, 0.0

    def get(self):
        with self.lock:
            if self.cached and time.time() - self.at < self.args.interval:
                return self.cached
            a = self.args
            try:
                states = read_ssh(a.ssh, a.remote_dir, a.identity) if a.ssh else read_local(a.dir)
                data = snapshot(states, a.max_attempts)
                data["error"] = None if states else "No state files found. Run rb-seed first."
            except Exception as e:  # noqa: BLE001
                data = dict(self.cached or {"hosts": [], "tiers": [], "counts": {}, "run_token": ""})
                data["error"] = str(e)
            data["source"] = f"{a.ssh}:{a.remote_dir}" if a.ssh else str(a.dir)
            data["updated"] = time.strftime("%H:%M:%S")
            self.cached, self.at = data, time.time()
            return data


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reboot Waves Live</title>
<style>
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #1d2330; --quiet: #5d6675; --line: #dde1e7;
  --waiting: #8a93a3; --restarting: #2f6fde; --done: #1f9d55; --failed: #d98a00;
  --quarantined: #b4441f; --down: #d93a3a;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #14171c; --card: #1d2128; --ink: #e8ebf0; --quiet: #9aa3b2; --line: #2e343e;
          --waiting: #7d8696; --restarting: #5b93f0; --done: #3cbf75; --failed: #f0a62a;
          --quarantined: #e46a42; --down: #f05858; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
       font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; }
h1 { font-size: 22px; margin: 0; }
.meta { color: var(--quiet); font-size: 13px; }
.error { margin: 16px 0 0; padding: 10px 14px; border-radius: 8px; border: 1px solid var(--down);
         color: var(--down); background: color-mix(in srgb, var(--down) 10%, transparent); }
.counts { display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
        background: var(--card); border: 1px solid var(--line); font-size: 13px; }
.chip b { font-variant-numeric: tabular-nums; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--c); flex: none; }
.tiers { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.tier { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
.tier.below { border-color: var(--down); }
.tier h2 { font-size: 16px; margin: 0; text-transform: capitalize; }
.tier .rule { color: var(--quiet); font-size: 13px; margin: 2px 0 12px; }
.tier .rule.bad { color: var(--down); font-weight: 600; }
.hosts { display: grid; gap: 8px; }
.host { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 8px;
        border: 1px solid var(--line); border-left: 4px solid var(--c); }
.host .name { font-weight: 600; font-variant-numeric: tabular-nums; }
.host .state { margin-left: auto; color: var(--c); font-size: 13px; font-weight: 600; }
.host.restarting { background: color-mix(in srgb, var(--restarting) 10%, transparent); }
.host.restarting .dot { animation: pulse 1s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: .25; transform: scale(.7); } }
.s-waiting { --c: var(--waiting); } .s-restarting { --c: var(--restarting); } .s-done { --c: var(--done); }
.s-failed { --c: var(--failed); } .s-quarantined { --c: var(--quarantined); } .s-down { --c: var(--down); }
</style>
</head>
<body>
<main>
  <header>
    <h1>Reboot waves</h1>
    <span class="meta" id="meta">Connecting...</span>
  </header>
  <div id="error" class="error" hidden></div>
  <div class="counts" id="counts"></div>
  <div class="tiers" id="tiers"></div>
</main>
<script>
const LABELS = {waiting: "Waiting", restarting: "Restarting", done: "Done",
                failed: "Failed, will retry", quarantined: "Quarantined", down: "Down"};
const el = (tag, cls, text) => { const e = document.createElement(tag);
  if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

function render(d) {
  document.getElementById("meta").textContent =
    `${d.source} · updated ${d.updated}` + (d.run_token ? ` · run ${d.run_token}` : "");
  const err = document.getElementById("error");
  err.hidden = !d.error; err.textContent = d.error || "";

  const counts = document.getElementById("counts"); counts.replaceChildren();
  for (const s of Object.keys(LABELS)) {
    const c = el("span", `chip s-${s}`); c.append(el("span", "dot"), LABELS[s] + " ", el("b", "", d.counts[s] || 0));
    counts.append(c);
  }

  const tiers = document.getElementById("tiers"); tiers.replaceChildren();
  for (const t of d.tiers) {
    const card = el("section", "tier" + (t.below ? " below" : ""));
    card.append(el("h2", "", t.tier));
    const rule = t.minimum === null ? `${t.healthy} of ${t.total} healthy`
      : `${t.healthy} of ${t.total} healthy · minimum ${t.minimum}` + (t.below ? " · BELOW MINIMUM" : "");
    card.append(el("div", "rule" + (t.below ? " bad" : ""), rule));
    const list = el("div", "hosts");
    for (const h of d.hosts.filter(h => h.tier === t.tier)) {
      const row = el("div", `host ${h.status} s-${h.status}`);
      const label = LABELS[h.status] + (h.attempts && h.status !== "done" ? ` (${h.attempts} attempt${h.attempts > 1 ? "s" : ""})` : "");
      row.append(el("span", "dot"), el("span", "name", h.name), el("span", "state", label));
      list.append(row);
    }
    card.append(list); tiers.append(card);
  }
}

async function tick() {
  try { const r = await fetch("/api/state", {cache: "no-store"}); render(await r.json()); }
  catch (e) { const err = document.getElementById("error"); err.hidden = false;
              err.textContent = "Lost contact with dashboard.py: " + e; }
  setTimeout(tick, 2000);
}
tick();
</script>
</body>
</html>
"""


def handler(source):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state":
                body, kind = json.dumps(source.get()).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, kind = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--ssh", metavar="USER@HOST", help="read the state files on the lab VM")
    where.add_argument("--dir", type=Path, help="read the state files in a local directory")
    p.add_argument("--remote-dir", default="/var/tmp/reboot-waves", help="lab_state_dir on the VM")
    p.add_argument("--identity", "-i", help="SSH private key, if not in your ssh config or agent")
    p.add_argument("--max-attempts", type=int, default=2, help="the workflow's max_attempts")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between reads")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(Source(args)))
    print(f"Reboot waves dashboard on http://localhost:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
