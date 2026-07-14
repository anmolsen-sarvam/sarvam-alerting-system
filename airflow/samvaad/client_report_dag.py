"""Samvaad client report -- weekly per-use-case (per-org) markdown reports to S3.

Generates one report per org and uploads to S3 (set `s3_prefix` in the baked config).
Copy to `samvaad/` in airflow-dags.
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
    dag_id="samvaad_client_report",
    start_date=datetime(2026, 1, 1),
    schedule="0 6 * * 1",   # Mondays ~11:30 IST
    catchup=False,
    tags=["samvaad", "alerting"],
    access_control={
        "samvaad-team": {"can_read", "can_edit"},
        "Admin": {"can_read", "can_edit", "can_delete"},
    },
)
def samvaad_client_report():
    @task(executor_config=_pod(), retries=2)
    def run() -> dict:
        from sarvam_alerting.clients.metabase import MetabaseClient
        from sarvam_alerting.config import load_config
        from sarvam_alerting.engine import discover_campaigns
        from sarvam_alerting.reports import build_client_reports

        cfg = load_config()
        with MetabaseClient(cfg.metabase) as mb:
            campaigns = discover_campaigns(mb, cfg)
            paths = build_client_reports(mb, cfg, campaigns)
        for p in paths:
            print("wrote", p)
        return {"reports": len(paths)}

    run()


samvaad_client_report()
