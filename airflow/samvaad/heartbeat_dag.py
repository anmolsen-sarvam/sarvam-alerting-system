"""Dead-man's switch — alerts if the alerting scan has stopped running.

Runs on its OWN schedule (offset from the scan) so it still fires when the scan is dead. It
reads the scan's heartbeat from the shared S3 state and alerts if it's stale.

Deploy: copy to `samvaad/` in the airflow-dags repo. See airflow/README.md.
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
    env = [
        _secret("METABASE_API_KEY", "metabase-api-key"),
        _secret("SLACK_BOT_TOKEN", "slack-bot-token"),
        _secret("SLACK_WEBHOOK_URL", "slack-webhook-url"),
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
    dag_id="samvaad_alerting_heartbeat",
    start_date=datetime(2026, 1, 1),
    schedule="15,45 * * * *",  # offset from the :00/:30 scan so it runs independently
    catchup=False,
    tags=["samvaad", "alerting"],
    access_control={"samvaad-team": {"can_read", "can_edit"}, "Admin": {"can_read", "can_edit", "can_delete"}},
)
def samvaad_alerting_heartbeat():
    @task(executor_config=_pod())
    def check() -> dict:
        import os
        import time

        from sarvam_alerting.config import load_config
        from sarvam_alerting.models import Finding, Severity
        from sarvam_alerting.notify import build_notifiers
        from sarvam_alerting.state import StateStore

        cfg = load_config()
        state_s3 = os.environ.get("SARVAM_ALERTING_STATE_S3", "").strip()
        if state_s3:
            _s3_download(state_s3, str(cfg.state.path))

        max_silence_h = float(cfg.tuning.get("heartbeat_max_silence_hours", 2.0))
        state = StateStore(cfg.state.path, cfg.state.cooldown_hours)
        last = state.last_beat()
        state.close()
        now = time.time()

        if last is not None and (now - last) < max_silence_h * 3600:
            print(f"heartbeat OK: last scan {int((now - last) / 60)} min ago")
            return {"ok": True}

        ago = "never" if last is None else f"{int((now - last) / 3600)}h ago"
        finding = Finding(
            detector="heartbeat", severity=Severity.CRITICAL, campaign_id="ALERTING-SYSTEM",
            title="Alerting scan has stopped running",
            detail=(f"No successful scan in over {max_silence_h:g}h (last: {ago}). Alerts are "
                    f"NOT being generated — check the samvaad_alerting_scan DAG."),
            dedupe_key="heartbeat:stale",
        )
        for notifier in build_notifiers(cfg.notifiers, cfg.links, cfg.owners):
            if notifier.wants("alerts"):
                notifier.notify([finding], {"campaigns_scanned": 0})
        print(f"heartbeat STALE: last scan {ago} — alerted")
        return {"ok": False, "last": ago}

    check()


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
        pass


samvaad_alerting_heartbeat()
