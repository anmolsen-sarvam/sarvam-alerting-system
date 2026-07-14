FROM apache/airflow:3.1.8

# Install Python deps from pyproject (same pattern as the platform guide).
COPY pyproject.toml .
RUN python -c "import tomllib; f=open('pyproject.toml','rb'); deps=tomllib.load(f)['project']['dependencies']; f.close(); print('\n'.join(deps))" > /tmp/requirements.txt && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# Bake the (secret-free) alerting config into the image. Secrets are injected at
# runtime via the samvaad-alerting-secrets K8s secret (see the DAGs).
USER root
COPY config.toml /opt/sarvam-alerting/config.toml
RUN chown -R airflow: /opt/sarvam-alerting
USER airflow
