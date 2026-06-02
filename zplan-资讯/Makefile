.PHONY: bootstrap venv diag gemini-check smoke ui daily-news install-daily-news daily-prices install-daily-prices

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
	@./.venv/bin/python -m streamlit run viewer_app.py --server.port 8501

daily-news:
	@./scripts/run_daily_news.sh

install-daily-news:
	@chmod +x ./scripts/run_daily_news.sh ./scripts/install_daily_news_launchagent.sh
	@./scripts/install_daily_news_launchagent.sh

daily-prices:
	@chmod +x ../zplan-股价/scripts/run_daily_prices.sh
	@../zplan-股价/scripts/run_daily_prices.sh

install-daily-prices:
	@chmod +x ../zplan-股价/scripts/run_daily_prices.sh ../zplan-股价/scripts/install_daily_prices_launchagent.sh
	@../zplan-股价/scripts/install_daily_prices_launchagent.sh
