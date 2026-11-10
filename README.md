# ao-reboot-waves

Quorum-aware advanced reboot on Ansible Automation Platform 2.7 and Automation Orchestrator 2026.8.

The workflow runs a reboot window as a series of waves. Every wave starts with a live probe of the fleet, plans the hosts allowed to reboot in each tier, reboots them in parallel, then verifies the result before the next wave. The job templates only print lines and write small state files. They never reboot a machine.

## The idea

| Step | What it answers |
| --- | --- |
| Probe | What is true right now? (`pending`, `healthy`, `attempts` for every host) |
| Plan | Which hosts may reboot now without breaking a tier's minimum? |
| Reboot | Run the existing tier playbooks, side by side, for exactly those hosts |
| Verify | Did it work? Is any tier below its minimum? Can anything still move? |

Nothing is remembered between waves, so a restart is always safe.

## Run it on a laptop

```shell
pip install ansible-core
python3 local/emulate.py
```

`local/emulate.py` plays the part of the Orchestrator: the Approval, the do-while loop, the three parallel tier jobs, the Converge and the outcome Switch. It runs eight scenarios and checks what each wave does.

Try a single job by hand:

```shell
ansible-playbook playbooks/lab/seed.yml  -i inventory/storefront.yml -e ansible_connection=local -e lab_state_dir=/tmp/rw -e scenario=happy
ansible-playbook playbooks/forecast.yml  -i inventory/storefront.yml -e ansible_connection=local -e lab_state_dir=/tmp/rw
```

## Watch it live

`local/dashboard.py` serves a page on your laptop that shows every host as waiting, restarting, done, failed (will be retried), quarantined or down, and each tier's healthy hosts against its minimum. It reads the state files every 2 seconds and needs only Python 3.

```shell
python3 local/dashboard.py --ssh lab-user@<lab VM address>     # add -i <key> if needed
python3 local/dashboard.py --dir /tmp/rw                       # a run on this laptop
```

Open http://localhost:8080. A simulated reboot takes about a second, so to see hosts in the restarting state, set `lab_reboot_seconds` (for example `15`) next to `lab_host` on the `storefront` group in AAP. Each tier job then holds its reboots for that long.

## Layout

```text
inventory/
  storefront.yml        Svc-A: 4 web, 4 app, 3 database
  flex.yml              6 web, 3 app, 5 database
  solo-db.yml           4 web, 4 app, 1 database
  group_vars/           the rules: reboot_policy per tier group
playbooks/
  forecast.yml          every wave, read-only
  plan_wave.yml         probe, then the host list for each tier
  verify_wave.yml       probe, then classify the outcome
  report.yml            closing state, or fail with the reason
  reboot_web.yml        STAND-INS for the customer's existing tier playbooks
  reboot_app.yml
  reboot_db.yml
  lib/                  probe, live view and the planner shared by the jobs
  lab/                  seed.yml, scenarios.yml and the reboot simulator
local/emulate.py        the laptop stand-in for the orchestrator
local/dashboard.py      live status page for the lab fleet
```

## The contract with the existing tier playbooks

The workflow needs three things from each tier playbook:

1. It takes the hosts to reboot as the extra variable `reboot_hosts` (a list).
2. It reboots only those hosts, and nothing when the list is empty or missing.
3. It fails the job if any host does not come back.

If the real playbook cannot take a list, wrap it. Do not pass the hosts as a Limit: an empty Limit means all hosts in Ansible.

## Setting it up in AAP

Objects:

- A **Machine credential** for the lab VM, and an **AAP credential** for the Orchestrator to launch jobs.
- A **project** pointing at this repository.
- An **inventory** `storefront` with one source of type "Sourced from a Project", file `inventory/storefront.yml`. Set the group variable `lab_host` to the lab VM address.

Job templates (inventory `storefront`, project above, Machine credential; enable **Prompt on launch** for Extra variables on each, because the workflow sends them):

| Job template | Playbook | Extra variables from the workflow |
| --- | --- | --- |
| `rb-seed` | `playbooks/lab/seed.yml` | Survey: `scenario` |
| `rb-forecast` | `playbooks/forecast.yml` | `max_attempts` |
| `rb-plan-wave` | `playbooks/plan_wave.yml` | `run_token`, `max_attempts` |
| `rb-reboot-web` | `playbooks/reboot_web.yml` | `reboot_hosts`, `run_token` |
| `rb-reboot-app` | `playbooks/reboot_app.yml` | `reboot_hosts`, `run_token` |
| `rb-reboot-db` | `playbooks/reboot_db.yml` | `reboot_hosts`, `run_token` |
| `rb-verify-wave` | `playbooks/verify_wave.yml` | `run_token`, `max_attempts`, `wave_selected` |
| `rb-report` | `playbooks/report.yml` | `mode`, `run_token`, `max_attempts` |

## The workflow on the canvas

Name `reboot-waves`. Manual trigger with one input: `max_attempts` (integer, 2). Every run waits for approval after the forecast. Expressions follow the catalog form `${node_id.field}`; use the token the builder inserts if it differs.

```text
trigger -> forecast -> approve_plan (Approval) -Approved-> wave_loop (do-while, max 12)
                                             -Rejected-> (unconnected: the run ends)
   Iterate:  plan_wave -> reboot_web \
                       -> reboot_app  > wave_settled (Converge: all) -> verify_wave -> wave_outcome (Switch)
                       -> reboot_db  /                   unsafe | stuck -> stop_run
                                                         default -> back to wave_loop
   Complete: finish_report
```

| Node | Type | Settings |
| --- | --- | --- |
| `forecast` | Job template `rb-forecast` | `max_attempts` = `${trigger.max_attempts}` |
| `approve_plan` | Approval | Prompt includes `${forecast.artifacts.summary}`; `approver_groups` the change board; `decision_window` 3600; `fallback_decision` reject; leave Rejected unconnected |
| `wave_loop` | While | `condition` `${verify_wave.artifacts.gate} == "continue"`, `max_iterations` 12 |
| `plan_wave` | Job template `rb-plan-wave` | `run_token` = `${forecast.artifacts.run_token}`, `max_attempts` = `${trigger.max_attempts}` |
| `reboot_web` | Job template `rb-reboot-web` | `reboot_hosts` = `${plan_wave.artifacts.reboot_web}`, `run_token`; On failure: **Continue on failure** |
| `reboot_app` | Job template `rb-reboot-app` | `reboot_hosts` = `${plan_wave.artifacts.reboot_app}`, same settings |
| `reboot_db` | Job template `rb-reboot-db` | `reboot_hosts` = `${plan_wave.artifacts.reboot_db}`, same settings |
| `wave_settled` | Converge | `strategy` all |
| `verify_wave` | Job template `rb-verify-wave` | `wave_selected` = `${plan_wave.artifacts.selected_hosts}`, `run_token`, `max_attempts` |
| `wave_outcome` | Switch | Case `unsafe`: `${verify_wave.artifacts.outcome} == "unsafe"`. Case `stuck`: `... == "stuck"`. Default back to the loop |
| `stop_run` | Job template `rb-report` | `mode` = exception; On failure: stop the workflow |
| `finish_report` | Job template `rb-report` | `mode` = complete; on the loop's Complete port |

Parallel is three edges leaving `plan_wave`. Do not add a retry policy to the tier jobs: a failed host is retried by the next wave, after the planner has re-counted the fleet.

## Scenarios

Run `rb-seed` first, then launch the workflow.

| `scenario` / input | Expected |
| --- | --- |
| `happy` | Waves of 6, 4 and 1; the executed waves equal the forecast |
| Approval rejected | Nothing runs |
| `flaky` | web-02 fails once, the next wave retries it, the run completes |
| `stubborn` | db-01 is quarantined after 2 attempts, everything else finishes, the run stops `stuck`. Then `heal`, then **Retry run**: only db-01 is planned and the run completes |
| `collateral` | Wave 1 is `unsafe` (app below its minimum); the run stops |
| `blocked` | `stuck` at wave 1; nothing is rebooted |
| Inventory `flex` | First wave 5 web, 1 app, 1 database |
| Inventory `solo-db` | The only database is never selected; the forecast lists it as not schedulable |

## From the lab to production

1. Replace `reboot_web`, `reboot_app` and `reboot_db` with the customer's own playbooks (see the contract above).
2. In `playbooks/lib/probe.yml`, replace the state-file read with real checks: `needs-restarting -r` for `pending`, the service health check for `healthy`.
3. Remove the host aliases from the inventory and use the real hosts, grouped as `tier_*`, with the rules in `group_vars`.
4. Have the tier playbooks record `attempts` and `attempt_token` for each host, or drop the quarantine and rely on `max_iterations`.

## What was verified

Everything above except the canvas was run on a laptop with ansible-core: see `local/emulate.py`. The node settings and expressions follow the 2026.8 documentation but have not been run on a canvas. Check these first: the `wave_outcome` case expressions, `${trigger.*}` inputs, and that a tier job set to Continue on failure still lets `wave_settled` fire.

## Sample artifacts for step testing

Use these to test a single node before the whole workflow exists. They are what `rb-plan-wave` and `rb-verify-wave` produce for the first wave of the `storefront` inventory.

`rb-plan-wave` artifacts:

```json
{
  "selected_hosts": ["web-01", "web-02", "web-03", "app-01", "app-02", "db-01"],
  "selected_count": 6,
  "reboot_web": ["web-01", "web-02", "web-03"],
  "reboot_app": ["app-01", "app-02"],
  "reboot_db": ["db-01"]
}
```

`rb-verify-wave` artifacts after a clean wave:

```json
{ "outcome": "clean", "gate": "continue", "failed_hosts": [], "pending_count": 5, "quarantined": [], "broken_tiers": [] }
```
