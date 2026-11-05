#!/usr/bin/env python3
"""Emulate the `reboot-waves` Automation Orchestrator workflow on a laptop.

The canvas runs the Approval, the do-while loop, the three parallel tier jobs,
the Converge and the outcome Switch. This script does the same in the
same order so the data they pass can be checked: waves, outcomes, retries,
quarantine, an unsafe tier, a stuck run and a restart.

Each scenario has its own state directory and runs in its own thread, and in a
wave the three tier jobs run at the same time, as they do on the canvas.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PB = ROOT / "playbooks"
INV = ROOT / "inventory"
MAX_ITERATIONS = 12


def ap(playbook, state_dir, extra=None, inventory="storefront", check=False):
    """Run one job template. Returns (rc, output, artifacts)."""
    cmd = ["ansible-playbook", str(PB / playbook), "-i", str(INV / f"{inventory}.yml"),
           "-e", "ansible_connection=local", "-e", f"lab_state_dir={state_dir}"]
    if extra:
        cmd += ["-e", json.dumps(extra)]
    env = dict(os.environ, ANSIBLE_SHOW_CUSTOM_STATS="1", ANSIBLE_FORCE_COLOR="0",
               ANSIBLE_CONFIG=str(ROOT / "ansible.cfg"))
    r = subprocess.run(cmd, text=True, capture_output=True, env=env, cwd=ROOT)
    out = r.stdout + r.stderr
    arts = {}
    m = re.search(r"CUSTOM STATS: \*+\s*\n\s*RUN: (\{.*\})", out)
    if m:
        arts = json.loads(m.group(1))
    if check and r.returncode != 0:
        sys.stderr.write(out)
        raise SystemExit(f"{playbook} failed")
    return r.returncode, out, arts


def seed(state_dir, scenario, inventory="storefront"):
    ap("lab/seed.yml", state_dir, {"scenario": scenario}, inventory, check=True)


def host_states(state_dir):
    return {p.stem: json.loads(p.read_text()) for p in Path(state_dir).glob("*.json")}


def workflow(state_dir, inventory="storefront", approve=True, max_attempts=2):
    """One execution of the workflow. Returns a dict describing the run."""
    run = {"waves": [], "verifies": [], "ended": None, "report": ""}
    # forecast
    _, _, fc = ap("forecast.yml", state_dir, {"max_attempts": max_attempts}, inventory, check=True)
    run["forecast"] = fc
    token = fc["run_token"]
    # approve_plan (Approval): Rejected is unconnected, so the run ends
    if not approve:
        run["ended"] = "rejected"
        return run
    # wave_loop (do-while)
    for _ in range(MAX_ITERATIONS):
        _, _, plan = ap("plan_wave.yml", state_dir,
                        {"run_token": token, "max_attempts": max_attempts}, inventory, check=True)
        run["waves"].append(plan)
        tier_keys = [k for k in plan if k.startswith("reboot_")]
        with ThreadPoolExecutor(max_workers=3) as pool:       # parallel tier jobs
            futures = {k: pool.submit(ap, f"{k}.yml", state_dir,
                                      {"reboot_hosts": plan[k], "run_token": token}, inventory)
                       for k in tier_keys}
            results = {k: f.result() for k, f in futures.items()}  # Converge: all
        # every tier job is set to "Continue on failure": the wave goes on to verify
        _, _, ver = ap("verify_wave.yml", state_dir,
                       {"run_token": token, "max_attempts": max_attempts,
                        "wave_selected": plan["selected_hosts"]}, inventory, check=True)
        ver["_tier_rc"] = {k: r[0] for k, r in results.items()}
        run["verifies"].append(ver)
        if ver["outcome"] in ("unsafe", "stuck"):             # wave_outcome (Switch) -> stop_run
            rc, out, _ = ap("report.yml", state_dir,
                            {"mode": "exception", "run_token": token, "max_attempts": max_attempts}, inventory)
            assert rc != 0, "stop_run must fail the run"
            run["ended"] = "stopped"
            run["report"] = out
            return run
        if ver["gate"] != "continue":                         # while condition
            break
    ap("report.yml", state_dir, {"mode": "complete", "run_token": token}, inventory, check=True)
    run["ended"] = "complete"
    return run


def counts(plan):
    return tuple(len(plan[f"reboot_{t}"]) for t in ("web", "app", "db"))


CHECKS = []


def check(scenario, ok, msg):
    CHECKS.append((scenario, ok, msg))


def pending_hosts(state_dir):
    return sorted(h for h, s in host_states(state_dir).items() if s["pending"])


def s_happy(base):
    d = str(base / "happy"); seed(d, "happy")
    run = workflow(d)
    sizes = [p["selected_count"] for p in run["waves"]]
    check("happy", sizes == [6, 4, 1], f"waves are 6, 4, 1 (got {sizes})")
    check("happy", counts(run["waves"][0]) == (3, 2, 1), "wave 1 is 3 web, 2 app, 1 db")
    check("happy", run["ended"] == "complete" and not pending_hosts(d), "run completes, nothing pending")
    fc_hosts = [w["hosts"] for w in run["forecast"]["waves"]]
    got = [{t: p[f"reboot_{t}"] for t in ("web", "app", "db")} for p in run["waves"]]
    check("happy", fc_hosts == got, "executed waves match the forecast exactly")
    check("happy", run["forecast"]["peak_concurrency"] == 6, "forecast peak concurrency is 6")


def s_reject(base):
    d = str(base / "reject"); seed(d, "happy")
    before = host_states(d)
    run = workflow(d, approve=False)
    check("reject", run["ended"] == "rejected" and not run["waves"], "approval rejected: no wave runs")
    check("reject", host_states(d) == before, "no host state changed")


def s_flaky(base):
    d = str(base / "flaky"); seed(d, "flaky")
    run = workflow(d)
    outs = [v["outcome"] for v in run["verifies"]]
    check("flaky", outs[0] == "partial" and run["verifies"][0]["failed_hosts"] == ["web-02"], f"wave 1 is partial with web-02 failed (got {outs})")
    check("flaky", run["verifies"][0]["_tier_rc"]["reboot_web"] != 0, "the web job itself reported failure")
    check("flaky", "web-02" in run["waves"][1]["reboot_web"], "wave 2 retries web-02 with a fresh quorum check")
    check("flaky", run["ended"] == "complete" and not pending_hosts(d), "run completes without a person")


def s_stubborn(base):
    d = str(base / "stubborn"); seed(d, "stubborn")
    run = workflow(d)
    check("stubborn", run["ended"] == "stopped" and run["verifies"][-1]["outcome"] == "stuck", "run stops as stuck")
    check("stubborn", pending_hosts(d) == ["db-01"], f"every other host was rebooted (pending: {pending_hosts(d)})")
    check("stubborn", run["verifies"][-1]["quarantined"] == ["db-01"], "db-01 is quarantined after its attempt budget")
    check("stubborn", "Quarantined after 2 attempts: db-01" in run["report"].replace("\n", " "), "report names the host")
    attempts = sum(1 for w in run["waves"] if "db-01" in w["reboot_db"])
    check("stubborn", attempts == 2, f"db-01 was tried exactly twice (got {attempts})")
    seed(d, "heal")                                            # an operator fixes the host
    run2 = workflow(d)                                         # Retry run: new run, fresh budget
    check("stubborn", run2["ended"] == "complete" and not pending_hosts(d), "Retry run after the fix completes")
    check("stubborn", [p["selected_hosts"] for p in run2["waves"]][0] == ["db-01"], "Retry run plans only what is pending")


def s_collateral(base):
    d = str(base / "collateral"); seed(d, "collateral")
    run = workflow(d)
    v = run["verifies"][0]
    check("collateral", v["outcome"] == "unsafe" and v["broken_tiers"] == ["app"], f"unsafe: app below its minimum (got {v['outcome']}, {v['broken_tiers']})")
    check("collateral", run["ended"] == "stopped" and len(run["waves"]) == 1, "run stops after wave 1, no second wave")


def s_blocked(base):
    d = str(base / "blocked"); seed(d, "blocked")
    before = host_states(d)
    run = workflow(d)
    check("blocked", run["waves"][0]["selected_count"] == 0, "no host is selected")
    check("blocked", run["verifies"][0]["outcome"] == "stuck" and run["ended"] == "stopped", "run stops as stuck")
    check("blocked", host_states(d) == before, "nothing was rebooted")


def s_flex(base):
    d = str(base / "flex"); seed(d, "happy", "flex")
    run = workflow(d, inventory="flex")
    check("flex", counts(run["waves"][0]) == (5, 1, 1), f"6/3/5 fleet: first wave is 5, 1, 1 (got {counts(run['waves'][0])})")
    check("flex", run["ended"] == "complete", "run completes")


def s_solo_db(base):
    d = str(base / "solo"); seed(d, "happy", "solo-db")
    run = workflow(d, inventory="solo-db")
    check("solo_db", run["forecast"]["unschedulable"] == ["db-01"], "forecast says the only database cannot be scheduled")
    check("solo_db", all(not p["reboot_db"] for p in run["waves"]), "the only database is never selected")
    check("solo_db", pending_hosts(d) == ["db-01"] and run["ended"] == "stopped", "everything else is done; the run stops for a person")


def main():
    base = Path(tempfile.mkdtemp(prefix="reboot-waves-"))
    scenarios = [s_happy, s_reject, s_flaky, s_stubborn, s_collateral, s_blocked, s_flex, s_solo_db]
    with ThreadPoolExecutor(max_workers=int(os.environ.get("EMULATE_WORKERS", "5"))) as pool:
        futures = [(s.__name__, pool.submit(s, base)) for s in scenarios]
        for name, f in futures:
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                check(name, False, f"scenario crashed: {e!r}")
    failed = 0
    last = None
    for scn, ok, msg in CHECKS:
        if scn != last:
            print(f"\n{scn}")
            last = scn
        print(f"  {'ok  ' if ok else 'FAIL'} {msg}")
        failed += 0 if ok else 1
    print(f"\n{len(CHECKS) - failed} of {len(CHECKS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
