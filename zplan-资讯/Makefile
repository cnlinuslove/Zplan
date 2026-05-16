.PHONY: bootstrap venv diag gemini-check smoke ui

# 创建/同步 .venv 与 requirements.txt
bootstrap:
	@./scripts/bootstrap_env.sh

venv: bootstrap

diag:
	@./.venv/bin/python openclaw_bridge.py diag

gemini-check:
	@./.venv/bin/python openclaw_bridge.py gemini-check

smoke:
	@./.venv/bin/python smoke_test.py

ui:
	@./.venv/bin/streamlit run viewer_app.py --server.port 8501
