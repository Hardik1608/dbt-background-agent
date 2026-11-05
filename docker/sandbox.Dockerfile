# Sandbox image: executes the agent's tools (dbt, DuckDB, git, sqlfluff). The agent loop and
# all credentials stay on the host. Run it only through DockerSandbox, which adds:
#   --network none --read-only --tmpfs /tmp:noexec --cap-drop ALL
#   --security-opt no-new-privileges --memory 2g --memory-swap 2g --pids-limit 512
#   --user <host uid>:<gid>, one bind mount (/work) + the toolbox script (read-only)
FROM python:3.11-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY docker/requirements-sandbox.txt /tmp/requirements-sandbox.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements-sandbox.txt \
 && rm /tmp/requirements-sandbox.txt \
 && mkdir -p /opt/toolbox /work

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DBT_SEND_ANONYMOUS_USAGE_STATS=False \
    DO_NOT_TRACK=1
WORKDIR /work
USER 1000:1000
CMD ["sleep", "infinity"]
