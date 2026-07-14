"""Samvaad conversationality review -- daily LLM transcript scoring (Layer B).

Samples calls per active campaign, scores them with the configured LLM, posts a report
and raises alerts when a campaign's conversation quality drops. LLM-backed, so it runs
once daily (not on the 30-min scan). Copy to `samvaad/` in airflow-dags.
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
        _secret("AZURE_OPENAI_API_KEY", "azure-openai-api-key"),
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
    dag_id="samvaad_conversationality_review",
    start_date=datetime(2026, 1, 1),
    schedule="0 15 * * *",   # ~20:30 IST, once daily
    catchup=False,
    tags=["samvaad", "alerting"],
    access_control={
        "samvaad-team": {"can_read", "can_edit"},
        "Admin": {"can_read", "can_edit", "can_delete"},
    },
)
def samvaad_conversationality_review():
    @task(executor_config=_pod(), retries=1)
    def run() -> dict:
        from sarvam_alerting.clients.llm import LLMClient
        from sarvam_alerting.clients.metabase import MetabaseClient
        from sarvam_alerting.config import load_config
        from sarvam_alerting.engine import discover_campaigns
        from sarvam_alerting.notify import build_notifiers
        from sarvam_alerting.reports import build_conversationality_review

        cfg = load_config()
        llm = LLMClient.from_config(cfg.llm)
        if llm is None:
            print("LLM not configured; skipping.")
            return {"skipped": True}

        with MetabaseClient(cfg.metabase) as mb:
            ids = [c.campaign_id for c in discover_campaigns(mb, cfg)]
            report, findings = build_conversationality_review(mb, cfg, llm, ids)

        meta = {"campaigns_scanned": len(ids)}
        for notifier in build_notifiers(cfg.notifiers):
            if notifier.wants("alerts"):
                notifier.notify(findings, meta)
            if report and notifier.wants("reports"):
                notifier.deliver_report(report)
        print(f"conversationality: {len(ids)} campaigns, {len(findings)} findings")
        return {"campaigns": len(ids), "findings": len(findings)}

    run()


samvaad_conversationality_review()
