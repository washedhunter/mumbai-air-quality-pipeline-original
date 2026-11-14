# Airflow 3.4.0 + the mumbai_aq package (pipeline code, SQL models, Prophet).
FROM apache/airflow:3.4.0-python3.12

ARG AIRFLOW_VERSION=3.4.0
ARG PYTHON_VERSION=3.12

WORKDIR /opt/aq
COPY --chown=airflow:root pyproject.toml README.md LICENSE ./
COPY --chown=airflow:root src ./src
COPY --chown=airflow:root sql ./sql
COPY --chown=airflow:root config ./config

# Install inside Airflow's constraints so nothing Airflow depends on gets upgraded.
RUN pip install --no-cache-dir \
      --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt" \
      . pytest \
 && python -c "import prophet, mumbai_aq.aqi; print('prophet', prophet.__version__)"

COPY --chown=airflow:root dags /opt/airflow/dags
COPY --chown=airflow:root data/sample /opt/aq/sample
COPY --chown=airflow:root tests /opt/aq/tests

# Created here (owned by the airflow user) so the named volume mounted on it inherits the ownership.
RUN mkdir -p /opt/airflow/data/landing

ENV SAMPLE_DIR=/opt/aq/sample \
    LANDING_DIR=/opt/airflow/data/landing

WORKDIR /opt/airflow
