"""Samvaad cycle report -- daily post-cycle performance digest.

Connectivity / engagement / PTP / disposition funnels per active campaign, posted to the
reports channel. Copy to `samvaad/` in airflow-dags. See airflow/README.md.
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
    dag_id="samvaad_cycle_report",
    start_date=datetime(2026, 1, 1),
    schedule="0 14 * * *",   # ~19:30 IST, after the daily calling window
    catchup=False,
    tags=["samvaad", "alerting"],
    access_control={
        "samvaad-team": {"can_read", "can_edit"},
        "Admin": {"can_read", "can_edit", "can_delete"},
    },
)
def samvaad_cycle_report():
    @task(executor_config=_pod(), retries=2)
    def run() -> dict:
        from sarvam_alerting.clients.metabase import MetabaseClient
        from sarvam_alerting.config import load_config
        from sarvam_alerting.engine import discover_campaigns
        from sarvam_alerting.notify import build_notifiers
        from sarvam_alerting.reports import build_cycle_report

        cfg = load_config()
        with MetabaseClient(cfg.metabase) as mb:
            ids = [c.campaign_id for c in discover_campaigns(mb, cfg)]
            report = build_cycle_report(mb, cfg, ids)
        if report:
            for notifier in build_notifiers(cfg.notifiers, cfg.links, cfg.owners):
                if notifier.wants("reports"):
                    notifier.deliver_report(report)
        print(f"cycle report: {len(ids)} campaigns, report={'yes' if report else 'none'}")
        return {"campaigns": len(ids)}

    run()


samvaad_cycle_report()
