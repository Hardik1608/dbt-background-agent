# Sourced by box stages: the local model server (key read from its file, never printed).
LOCAL_LLM_API_KEY="$(cat ~/Hardik/Extras/llm-server/.api_key)"
export LOCAL_LLM_API_KEY
export DBT_AGENT_LLM_BASE_URL="http://127.0.0.1:8100/v1"
export PYTHONUNBUFFERED=1
export DBT_SEND_ANONYMOUS_USAGE_STATS=False
PY=.venv/bin/python
MODEL_CONFIGS="configs/local_agent.yaml configs/box.yaml"
