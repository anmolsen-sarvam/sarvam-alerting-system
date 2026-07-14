"""Samvaad weekly evals -- weekly insights digest for the client deliverable.

Per-campaign hallucination / loop / escalation-miss rates + effectiveness scores from the
insights pipeline. Copy to `samvaad/` in airflow-dags.
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
    dag_id="samvaad_weekly_evals",
    start_date=datetime(2026, 1, 1),
    schedule="0 5 * * 1",   # Mondays ~10:30 IST
    catchup=False,
    tags=["samvaad", "alerting"],
    access_control={
        "samvaad-team": {"can_read", "can_edit"},
        "Admin": {"can_read", "can_edit", "can_delete"},
    },
)
def samvaad_weekly_evals():
    @task(executor_config=_pod(), retries=2)
    def run() -> dict:
        from sarvam_alerting.clients.metabase import MetabaseClient
        from sarvam_alerting.config import load_config
        from sarvam_alerting.notify import build_notifiers
        from sarvam_alerting.reports import build_weekly_evals

        cfg = load_config()
        with MetabaseClient(cfg.metabase) as mb:
            report = build_weekly_evals(mb, cfg)
        if report:
            for notifier in build_notifiers(cfg.notifiers):
                if notifier.wants("reports"):
                    notifier.deliver_report(report)
        print(f"weekly evals: report={'yes' if report else 'none'}")
        return {"report": bool(report)}

    run()


samvaad_weekly_evals()
