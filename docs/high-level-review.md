# High-level review — what this system is, in plain terms

## The one-liner
It's an **automatic watchdog for live calling campaigns.** It constantly looks at what's
happening on every active campaign and shouts on Slack the moment something looks wrong —
so problems get caught in minutes instead of days.

## Positioning — what this is *not*
There's a separate, excellent tool (an analytics copilot in Slack) that you **ask** things:
"how's IDFC doing this week? build me a dashboard, alert me if PTP < 8%." That's a
**copilot** — pull-based, general-purpose, human-initiated, great for exploration.

**This is the opposite kind of tool: a control, not a copilot.** Think of it as
**CI checks + smoke tests for campaign deployments** — the seatbelt that's always on.

| | Analytics copilot | **This system** |
|---|---|---|
| You have to… | ask it | nothing — it runs itself |
| Knows to check… | whatever you think of | the **standardized QC checklist**, on every campaign, every 30 min |
| Made of | an LLM writing SQL on demand | fixed, deterministic **detectors** with tuned thresholds |
| Job | answer questions / build BI | **catch silent deployment regressions in the first hour** |
| Nature | exploratory, conversational | opinionated, autonomous, auditable, cheap |

Its unique wedge: **it encodes the CS/QC team's judgment as always-on guardrails.** A
copilot only catches a problem if someone thinks to ask about it; this catches the
default-variable-class bug (and the rest of the QC checklist) whether or not anyone is
looking. It's release-safety for campaigns, not business intelligence.

## Why it exists
A campaign calls thousands of people. Each call is supposed to use *that person's* details
(their name, their EMI, their due date). Once, a mapping broke and the agent used the same
**default** values for everyone — "am I speaking to <same name>?" on every call — and it ran
**3–4 days** before a human happened to notice it in Metabase. That caused client escalations.

The problem: nothing *crashed*. Calls kept "succeeding," dashboards looked fine. The failure
was in the *content* of the calls, not in any error. So normal monitoring can't see it.
**This system watches the content/data itself**, which is the only way to catch that class of bug.

## What it actually does (4 things)
1. **Alerts** — watches every active campaign and pings Slack when something's off:
   the default-variable bug, a connectivity crash, a failure-rate spike, agent looping, etc.
2. **Run summary** — a "what's live right now" digest: which campaigns are active, how many
   rows were uploaded vs filtered out, retry settings — grouped by client.
3. **Cycle report** — after a day's calling: connectivity %, engagement %, PTP %, and the
   disposition breakdown, per campaign.
4. **Client report** — a weekly per-client summary you can share.

## The single most important idea
"The same value on every call = a bug" is **wrong** — lots of values (bot name, GST rate)
are *supposed* to be the same for everyone. The real signal is: **a value that is normally
different per person suddenly becomes the same for everyone.** So it compares each campaign
to *its own recent normal*, not to a fixed rule. That one idea is what makes it accurate
instead of noisy.

## What it can and can't see
- ✅ It reads all the **structured data** — the variables used, connectivity, dispositions,
  durations, and the quality scores Sarvam already computes (hallucination, looping, etc.).
- ❌ It **cannot read the actual words** of the calls — transcripts are encrypted. So anything
  about "what was said" comes from Sarvam's existing eval scores, not from re-reading calls.

## Who it helps
- **CS / QC / FDSE** — replaces most of the manual daily QC checklist. Instead of eyeballing
  Metabase and listening to 5–6 calls hoping to catch a problem, the problems come to you.
- **Growth / leadership** — the weekly per-client reports and portfolio view.

## Where it runs
As scheduled jobs on the **Airflow/Kubernetes** platform (the 30-min scan + daily/weekly
reports). It's cross-client (all orgs) by default. There's also a command-line version for
running any check on demand.

## Honest status (as of now)
- **Built and proven against live data** — the detectors fire correctly (e.g. it flagged an
  88% failure rate on a live Tata Capital campaign) and stay quiet on healthy ones.
- **Not yet posting to Slack / not yet deployed** — that needs Slack credentials, a
  Kubernetes secret, and the repo published. All the code is ready; those are setup steps.
- The conversation-quality signals come from Sarvam's insights pipeline (because transcripts
  are encrypted), and the value-correctness rules (loan types, cohort sizes) need CS to fill
  in the expected values.

## If you remember five things
1. It watches the **data flowing through calls**, not just "did the job run."
2. It catches the **default-variable bug** and other silent regressions **within an hour**.
3. It's accurate because it compares each campaign to **its own history**, not fixed rules.
4. It **can't read encrypted transcripts** — quality signals come from existing eval scores.
5. It's **ready but not live** — remaining work is credentials + deployment, not code.
