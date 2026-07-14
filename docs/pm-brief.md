# PM brief — who this is for and how it wins

Framing this as a product, not a script. The wedge, the personas, the gaps, the rollout,
and how we'd know it's working.

## The problem, in product terms
Campaign deployments fail **silently** (default variables, bad mappings, wrong loan types),
and today the only safety net is human vigilance — which failed for 3–4 days and caused
client escalations. We're selling **deployment safety**: turn "someone might catch it in
Metabase" into "it's caught automatically in the first hour."

Positioning (locked): a **control, not a copilot** — CI/smoke-tests for campaigns, not a
chat-with-your-data analyst. It runs itself.

## Personas & jobs-to-be-done

### FDSE (Forward-Deployed / Field Solutions Engineer) — the primary wedge
- **Owns:** campaign setup — cohort upload, variable mapping, agent config, launch.
- **JTBD:** *"Launch correctly, and if my config broke something, tell me in minutes — before the client notices."*
- **Their pain is the sharpest** (they get blamed for escalations), and they own the fix. So they are both the **beneficiary and the buyer**.
- **How we serve them:** `variable_collapse`, `required_populated`, `expected_values`, `connectivity` — catch *their* mistakes at launch, with deep links to the evidence.
- **What they still need:** alerts routed to the **owning** FDSE (per-campaign/org channel), a fix-hint/runbook per alert, and a true launch-instant trigger (today ~30-min lag).

### QC — the daily operator
- **Owns:** the daily quality checklist across all active campaigns.
- **JTBD:** *"Guarantee every campaign meets the bar without manually eyeballing 450 of them."*
- **How we serve them:** automates the checklist (connectivity, dispositions, short-calls, insights quality), run summary + cycle report, weekly evals, Master-QC-sheet auto-fill, and **Slack scope control** so they own what's watched.
- **What they still need:** trust (low false positives — why the connectivity fix mattered), the `[[expected]]` rules filled in, and the QC-sheet + scope-control wired live.

### GTM / Growth — the client-facing owner
- **Owns:** client relationships, retention, expansion.
- **JTBD:** *"Fewer escalations, and a credible weekly story to tell the client."*
- **How we serve them:** the per-client **client report** + weekly evals; every caught regression is an escalation avoided (protects revenue).
- **What they still need:** the client report is raw markdown today — needs **polish/branding**, **week-over-week trends**, and a **portfolio view** across clients.

### Leadership / Ops — the sponsor
- **JTBD:** *"Is the book of business healthy, and are escalations going down?"*
- **Gap:** we track no success metrics yet (see below) and have no exec/portfolio view.

## Where it's strong vs weak (honest fit)
| Persona | Fit today | Biggest gap |
|---|---|---|
| FDSE | Strong — core detectors | ownership routing + fix-hints + instant launch trigger |
| QC | Strong on metrics | trust/tuning, sheet + scope-control live, expected-values filled |
| GTM | Partial | client report polish, trends, portfolio view |
| Leadership | Weak | success metrics + exec dashboard |

## Rollout (how a PM would land this)
1. **Land with FDSE first, one or two clients.** Sharpest pain, clearest owner. Use scope
   control to onboard client-by-client (`monitor chola.com`) rather than boiling the ocean.
2. **Two-week shadow/tuning period.** Alerts go to a staging channel; measure false-positive
   rate; tune thresholds per detector. Alert fatigue is the #1 killer — earn trust before
   going loud. (We already killed the biggest FP: the absolute failure-rate alert.)
3. **Assign an owner per channel.** An alert with no one accountable to act is just noise.
   FDSE on-call acts; QC lead owns thresholds + expected-values.
4. **Then expand** to all clients and turn on the GTM/leadership reporting.

## Success metrics
- **North star:** *days-of-exposure* for silent regressions — from ~3–4 days → **< 1 hour**.
- **Leading:** coverage (% active campaigns monitored), **alert precision** (1 − false-positive rate; target > ~80%), **MTTD** (bug → alert).
- **Lagging:** # config/silent-bug escalations (target → 0 for the default-variable class),
  time-to-resolve, QC hours saved/week.

## Prioritized roadmap (by persona value)
- **P0 — trust & adoption (FDSE):** alert → owning-FDSE routing (needs a campaign→owner map),
  shadow/tuning period, launch-instant trigger, fix-hints/runbook links on alerts.
- **P1 — QC operationalization:** wire Master-QC-sheet + Slack scope-control live; QC fills
  `[[expected]]` values; per-detector threshold tuning surface.
- **P1 — GTM:** polished/branded client report + week-over-week trends.
- **P2 — leadership & automation:** metrics dashboard (MTTD / precision / escalations),
  and auto-pause remediation (stop a broken campaign automatically, human-confirmed).

## Top risks
1. **Alert fatigue** from false positives → abandonment. *Mitigation:* shadow period, tuning,
   dedupe/cooldown (built), baseline-relative detectors (built).
2. **Ownership vacuum** — nobody acts on alerts. *Mitigation:* named owner per channel.
3. **Tool confusion** vs the analytics copilot. *Mitigation:* "control, not copilot" positioning.
4. **Expectation gap on conversation quality** — transcripts are encrypted, so depth comes
   from the insights pipeline, not our own reading. *Mitigation:* set expectations; use insights.
5. **Config ownership** — if QC doesn't fill `[[expected]]` / scope, coverage stays generic.
