"""Samvaad campaign alerting -- 30-minute scan + run summary.

Runs the detector scan across all active campaigns and posts alerts + the run-summary
digest to Slack. Deduplicates findings across runs via an S3-backed state file (worker
pods are ephemeral, so local SQLite would not survive between runs).

Deploy: copy this file to `samvaad/` in the airflow-dags repo. See airflow/README.md.

NOTE on imports: `sarvam_alerting` and `boto3` are imported *inside* the task body on
purpose -- they exist only in the custom samvaad worker image, not in the DAG-processor's
base image, so importing them at module top would break DAG parsing.
"""

from airflow.decorators import dag, task
from kubernetes.client import models as k8s
from lib.ecr import ecr_image
from pendulum import datetime

SECRET_NAME = "samvaad-alerting-secrets"
CONFIG_PATH = "/opt/sarvam-alerting/config.toml"


def _secret(env_name: str, secret_key: str) -> k8s.V1EnvVar:
    return k8s.V1EnvVar(
        name=env_name,
        value_from=k8s.V1EnvVarSource(
            secret_key_ref=k8s.V1SecretKeySelector(name=SECRET_NAME, key=secret_key)
        ),
    )


def _pod() -> dict:
    """Worker pod running the custom samvaad image, with secrets + config path."""
    env = [
        _secret("METABASE_API_KEY", "metabase-api-key"),
        _secret("SLACK_BOT_TOKEN", "slack-bot-token"),
        _secret("SLACK_WEBHOOK_URL", "slack-webhook-url"),
        _secret("AZURE_OPENAI_API_KEY", "azure-openai-api-key"),
        _secret("SARVAM_ALERTING_STATE_S3", "state-s3-uri"),
        _secret("SARVAM_ALERTING_SCOPE_URI", "scope-s3-uri"),
        k8s.V1EnvVar(name="SARVAM_ALERTING_CONFIG", value=CONFIG_PATH),
    ]
    return {
        "pod_override": k8s.V1Pod(
            spec=k8s.V1PodSpec(
                containers=[k8s.V1Container(name="base", image=ecr_image("samvaad"), env=env)]
            )
        )
    }


@dag(
    dag_id="samvaad_alerting_scan",
    start_date=datetime(2026, 1, 1),
    schedule="*/30 * * * *",
    catchup=False,
    tags=["samvaad", "alerting"],
    access_control={
        "samvaad-team": {"can_read", "can_edit"},
        "Admin": {"can_read", "can_edit", "can_delete"},
    },
)
def samvaad_alerting_scan():
    @task(executor_config=_pod())
    def scan() -> dict:
        import os

        from sarvam_alerting.clients.metabase import MetabaseClient
        from sarvam_alerting.config import load_config
        from sarvam_alerting.engine import build_reports, run_scan
        from sarvam_alerting.notify import build_notifiers
        from sarvam_alerting.state import StateStore

        cfg = load_config()
        state_s3 = os.environ.get("SARVAM_ALERTING_STATE_S3", "").strip()

        # Pull the dedupe DB from S3 so cooldowns persist across ephemeral pods.
        if state_s3:
            _s3_download(state_s3, str(cfg.state.path))

        with MetabaseClient(cfg.metabase) as mb:
            findings, campaigns = run_scan(mb, cfg)
            reports = build_reports(mb, cfg, campaigns)

        import time as _time

        from sarvam_alerting.notify.console import ConsoleNotifier
        from sarvam_alerting.scope import is_muted

        state = StateStore(cfg.state.path, cfg.state.cooldown_hours)
        new = [f for f in findings if state.is_new(f)]

        # Recovery: findings open last run but absent now (and not muted) have cleared.
        current_fps = {f.fingerprint for f in findings}
        now = _time.time()
        resolved = [
            a for a in state.active_findings(now)
            if a["fingerprint"] not in current_fps
            and not is_muted(a["campaign_id"], cfg.mutes, now)
        ]
        # Escalation: still-open criticals nobody acknowledged (muted = acknowledged).
        escalate_after = int(cfg.tuning.get("escalate_after_minutes", 30)) * 60
        escalations = [
            e for e in state.escalation_candidates(escalate_after, now)
            if e["fingerprint"] in current_fps
            and not is_muted(e["campaign_id"], cfg.mutes, now)
        ]

        shadow = bool(cfg.tuning.get("shadow_mode", False))
        meta = {"campaigns_scanned": len(campaigns)}
        for notifier in build_notifiers(cfg.notifiers, cfg.links, cfg.owners):
            # In shadow mode only the console (task logs) sees alerts; digests still post.
            if notifier.wants("alerts") and not (shadow and not isinstance(notifier, ConsoleNotifier)):
                notifier.notify(new, meta)
                notifier.notify_recovery(resolved)
                notifier.notify_escalation(escalations)
            if notifier.wants("reports"):
                for report in reports:
                    notifier.deliver_report(report)
        for f in new:
            state.mark_notified(f)
        for a in resolved:
            state.clear(a["fingerprint"])
        for e in escalations:
            state.mark_escalated(e["fingerprint"])
        state.beat(now)  # heartbeat for the dead-man's switch
        state.close()

        if state_s3:
            _s3_upload(str(cfg.state.path), state_s3)

        print(f"scan: {len(findings)} findings, {len(new)} new, {len(campaigns)} campaigns, "
              f"{len(escalations)} escalation(s)")
        return {"findings": len(findings), "new": len(new), "campaigns": len(campaigns)}

    scan()


def _parse_s3(uri: str):
    import urllib.parse

    p = urllib.parse.urlparse(uri)
    return p.netloc, p.path.lstrip("/")


def _s3_download(uri: str, local_path: str) -> None:
    import os

    import boto3
    from botocore.exceptions import ClientError

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    bucket, key = _parse_s3(uri)
    try:
        boto3.client("s3").download_file(bucket, key, local_path)
    except ClientError:
        pass  # first run: no state yet


def _s3_upload(local_path: str, uri: str) -> None:
    import boto3

    bucket, key = _parse_s3(uri)
    boto3.client("s3").upload_file(local_path, bucket, key)


samvaad_alerting_scan()
