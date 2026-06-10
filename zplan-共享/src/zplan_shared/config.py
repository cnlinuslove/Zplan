import os
from pathlib import Path

from dotenv import load_dotenv


def resolve_zplan_root() -> Path:
    """数据与 .env 根目录：默认 monorepo 下的 ``zplan-资讯/``，可用 ``ZPLAN_ROOT`` 覆盖。"""
    explicit = os.getenv("ZPLAN_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    mono_root = Path(__file__).resolve().parents[3]
    for name in ("zplan-资讯", "zplan"):
        candidate = mono_root / name
        if candidate.is_dir():
            return candidate.resolve()
    return Path.cwd().resolve()


ZPLAN_ROOT = resolve_zplan_root()
load_dotenv(ZPLAN_ROOT / ".env")
load_dotenv()

BASE_DIR = ZPLAN_ROOT
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def resolve_db_url(raw: str | None = None) -> str:
    """SQLite 相对路径统一解析到 ``ZPLAN_ROOT``，避免各 Agent 工作目录不同连错库。"""
    url = (raw or os.getenv("DB_URL") or f"sqlite:///{BASE_DIR / 'zplan.db'}").strip()
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith("sqlite:////"):
        path_part = url[len(prefix) :]
        p = Path(path_part)
        if not p.is_absolute():
            return f"{prefix}{(BASE_DIR / path_part).resolve()}"
    return url


DB_URL = resolve_db_url()
AKSHARE_RATE_LIMIT_SECONDS = float(os.getenv("AKSHARE_RATE_LIMIT_SECONDS", "3"))
# 日线数据源（全库统一，见 data_sources.py）：
# em=东财 stock_zh_a_hist；tx=腾讯 stock_zh_a_hist_tx；sina=新浪 stock_zh_a_daily
# 已废弃：按票混源回退请勿开启
AKSHARE_ALLOW_TX_FALLBACK = os.getenv("AKSHARE_ALLOW_TX_FALLBACK", "false").lower() == "true"
AKSHARE_FAIL_CIRCUIT_THRESHOLD = int(os.getenv("AKSHARE_FAIL_CIRCUIT_THRESHOLD", "3"))
AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS = int(os.getenv("AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS", "20"))

# Phase A.1：日线回溯 + 近端分时（Parquet）
PARQUET_ROOT = Path(os.getenv("PARQUET_ROOT", str(BASE_DIR / "parquet"))).expanduser().resolve()
DAILY_BOOTSTRAP_CALENDAR_DAYS = int(os.getenv("DAILY_BOOTSTRAP_CALENDAR_DAYS", "400"))
# 东财日线单次请求跨度（天），过长易 RemoteDisconnected / 限流
AKSHARE_DAILY_CHUNK_DAYS = int(os.getenv("AKSHARE_DAILY_CHUNK_DAYS", "90"))
RECENT_INTRADAY_CALENDAR_DAYS = int(os.getenv("RECENT_INTRADAY_CALENDAR_DAYS", "14"))
# 东财 1 分钟 trends2 接口仅约 5 个交易日；5/15 分钟 K 线可覆盖更长区间
INTRADAY_FINE_PERIOD = os.getenv("INTRADAY_FINE_PERIOD", "1")
INTRADAY_COARSE_PERIOD = os.getenv("INTRADAY_COARSE_PERIOD", "5")
INTRADAY_FINE_CALENDAR_DAYS = int(os.getenv("INTRADAY_FINE_CALENDAR_DAYS", "5"))

# News Agent（默认偏省钱：4h、每 topic 10 条、单页；可用 .env 覆盖）
NEWS_SCHEDULE_HOURS = int(os.getenv("NEWS_SCHEDULE_HOURS", "4"))
NEWS_FETCH_LIMIT_PER_TOPIC = int(os.getenv("NEWS_FETCH_LIMIT_PER_TOPIC", "10"))
NEWS_WINDOW_HOURS = int(os.getenv("NEWS_WINDOW_HOURS", "2"))
WECHAT_PUSH_WEBHOOK = os.getenv("WECHAT_PUSH_WEBHOOK", "")
X_API_BASE_URL = os.getenv("X_API_BASE_URL", "https://api.twitter.com/2")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
X_HTTP_TIMEOUT_SECONDS = int(os.getenv("X_HTTP_TIMEOUT_SECONDS", "20"))
X_MAX_RESULTS_PER_PAGE = int(os.getenv("X_MAX_RESULTS_PER_PAGE", "10"))
X_MAX_PAGES_PER_TOPIC = int(os.getenv("X_MAX_PAGES_PER_TOPIC", "1"))
X_FETCH_USERNAMES = os.getenv("X_FETCH_USERNAMES", "false").lower() == "true"
X_RATE_LIMIT_SLEEP_SECONDS = int(os.getenv("X_RATE_LIMIT_SLEEP_SECONDS", "60"))
X_FAILOVER_TO_PLACEHOLDER = os.getenv("X_FAILOVER_TO_PLACEHOLDER", "true").lower() == "true"

# LLM (DeepSeek，OpenAI 兼容 API)
# 优先级：DEEPSEEK_API_KEY > .claude/settings.local.json 中的 ANTHROPIC_API_KEY > GEMINI_API_KEY
def _resolve_deepseek_api_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    # 尝试从 Claude Code 本地配置读取（DeepSeek 平台同一 Key 可用于标准 API 与 Anthropic 兼容端点）
    try:
        import json as _json
        settings_local = Path.home() / ".claude" / "settings.local.json"
        if settings_local.exists():
            data = _json.loads(settings_local.read_text(encoding="utf-8"))
            key = (data.get("env", {}).get("ANTHROPIC_API_KEY") or data.get("ANTHROPIC_API_KEY") or "").strip()
            if key:
                return key
    except Exception:
        pass
    # 最后回退到旧 Gemini Key（过渡期）
    return os.getenv("GEMINI_API_KEY", "").strip()


DEEPSEEK_API_KEY = _resolve_deepseek_api_key()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_BASE_URL = os.getenv(
    "DEEPSEEK_API_BASE_URL", "https://api.deepseek.com/v1"
)
DEEPSEEK_TIMEOUT_SECONDS = int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "90"))
DEEPSEEK_MAX_OUTPUT_TOKENS = int(os.getenv("DEEPSEEK_MAX_OUTPUT_TOKENS", "8192"))
DEEPSEEK_MIN_SECONDS_BETWEEN_CALLS = float(os.getenv("DEEPSEEK_MIN_SECONDS_BETWEEN_CALLS", "1.5"))

# ── 模型无关的通用别名（新代码请用 LLM_*）─────────────────────────
LLM_API_KEY = DEEPSEEK_API_KEY
LLM_MODEL = DEEPSEEK_MODEL
LLM_API_BASE_URL = DEEPSEEK_API_BASE_URL
LLM_TIMEOUT_SECONDS = DEEPSEEK_TIMEOUT_SECONDS
LLM_MAX_OUTPUT_TOKENS = DEEPSEEK_MAX_OUTPUT_TOKENS
LLM_MIN_SECONDS_BETWEEN_CALLS = DEEPSEEK_MIN_SECONDS_BETWEEN_CALLS

# ── 向后兼容别名（旧代码仍可用，建议逐步迁移到 LLM_*）────────────
GEMINI_API_KEY = DEEPSEEK_API_KEY
GEMINI_MODEL = os.getenv("GEMINI_MODEL", DEEPSEEK_MODEL)
GEMINI_API_BASE_URL = os.getenv(
    "GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
)
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", str(DEEPSEEK_TIMEOUT_SECONDS)))
LLM_SUMMARY_ENABLED = os.getenv("LLM_SUMMARY_ENABLED", "true").lower() == "true"
GEMINI_SUMMARY_MAX_ITEMS = int(os.getenv("GEMINI_SUMMARY_MAX_ITEMS", "15"))
GEMINI_SUMMARY_CHARS_PER_ITEM = int(os.getenv("GEMINI_SUMMARY_CHARS_PER_ITEM", "900"))
GEMINI_MIN_SECONDS_BETWEEN_TOPICS = float(os.getenv("GEMINI_MIN_SECONDS_BETWEEN_TOPICS", "8"))
GEMINI_MIN_SECONDS_BETWEEN_CALLS = float(
    os.getenv("GEMINI_MIN_SECONDS_BETWEEN_CALLS", str(DEEPSEEK_MIN_SECONDS_BETWEEN_CALLS))
)
GEMINI_MAX_OUTPUT_TOKENS = int(
    os.getenv("GEMINI_MAX_OUTPUT_TOKENS", str(DEEPSEEK_MAX_OUTPUT_TOKENS))
)
X_QUERY_EXCLUDE_SUFFIX = os.getenv("X_QUERY_EXCLUDE_SUFFIX", "").strip()

# ── Web Search（概念产品摘要 grounding）────────────────────────────
# 默认 DuckDuckGo（免费），设置 TAVILY_API_KEY 后自动升级
WEB_SEARCH_BACKEND = os.getenv("WEB_SEARCH_BACKEND", "duckduckgo").strip().lower()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
WEB_SEARCH_MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_SEARCH_TIMEOUT_SECONDS = int(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "15"))

# WeChat
CHAT_HISTORY_ENABLED = os.getenv("CHAT_HISTORY_ENABLED", "true").lower() == "true"
# 会话窗口：用户 @ 过 Zplan 后，多少分钟内同群消息无需再次 @（默认 120 分钟）
CHAT_SESSION_TTL_MINUTES = int(os.getenv("CHAT_SESSION_TTL_MINUTES", "120"))
WECHAT_PUSH_MODE = os.getenv("WECHAT_PUSH_MODE", "markdown")
WECHAT_PUSH_DIGEST = os.getenv("WECHAT_PUSH_DIGEST", "true").lower() == "true"
WECHAT_HTTP_TOKEN = os.getenv("WECHAT_HTTP_TOKEN", "").strip()
WECHAT_CORP_ID = os.getenv("WECHAT_CORP_ID", "").strip()
WECHAT_CORP_SECRET = os.getenv("WECHAT_CORP_SECRET", "").strip()
WECHAT_AGENT_ID = int(os.getenv("WECHAT_AGENT_ID", "0") or "0")
WECHAT_CALLBACK_TOKEN = os.getenv("WECHAT_CALLBACK_TOKEN", "").strip()
WECHAT_CALLBACK_AES_KEY = os.getenv("WECHAT_CALLBACK_AES_KEY", "").strip()

# 多源资讯 ETL
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()
NEWSAPI_BASE_URL = os.getenv("NEWSAPI_BASE_URL", "https://newsapi.org/v2").rstrip("/")
NEWSAPI_TIMEOUT_SECONDS = int(os.getenv("NEWSAPI_TIMEOUT_SECONDS", "25"))
NEWSAPI_MODE = os.getenv("NEWSAPI_MODE", "top_headlines").strip().lower()
NEWSAPI_TOP_HEADLINES_COUNTRY = os.getenv("NEWSAPI_TOP_HEADLINES_COUNTRY", "us").strip()
NEWSAPI_QUERY = os.getenv("NEWSAPI_QUERY", "Macroeconomy OR Geopolitics").strip()
NEWSAPI_LANGUAGE = os.getenv("NEWSAPI_LANGUAGE", "en").strip()
NEWSAPI_PAGE_SIZE = int(os.getenv("NEWSAPI_PAGE_SIZE", "30"))
GOOGLE_RSS_KEYWORDS = os.getenv("GOOGLE_RSS_KEYWORDS", "Macroeconomy,Geopolitics,美联储,地缘冲突")
GOOGLE_RSS_HL = os.getenv("GOOGLE_RSS_HL", "zh-CN")
# Google News RSS 时间过滤：空=不过滤；值如 24h/7d/30d（官方支持 h/d/w/m 单位）
GOOGLE_RSS_WHEN = os.getenv("GOOGLE_RSS_WHEN", "24h").strip() or None
HTTP_USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (compatible; zplan-sentiment-etl/1.0; +https://github.com/)",
)
SENTIMENT_INDEX_SYMBOLS = [
    s.strip()
    for s in os.getenv("SENTIMENT_INDEX_SYMBOLS", "000001,399001,399006").split(",")
    if s.strip()
]
SENTIMENT_INDEX_HIST_DAYS = int(os.getenv("SENTIMENT_INDEX_HIST_DAYS", "30"))
SENTIMENT_NORTHBOUND_INTRADAY = os.getenv("SENTIMENT_NORTHBOUND_INTRADAY", "true").lower() == "true"
SENTIMENT_WECHAT_PUSH = os.getenv("SENTIMENT_WECHAT_PUSH", "true").lower() == "true"
SENTIMENT_WECHAT_SAMPLE_PER_SOURCE = int(os.getenv("SENTIMENT_WECHAT_SAMPLE_PER_SOURCE", "4"))
# 数据陈旧阈值（交易日+周末容忍度内置）：各 factor_kind 最新 as_of_utc 超过此天数则告警
SENTIMENT_STALE_DAYS_NORTHBOUND = int(os.getenv("SENTIMENT_STALE_DAYS_NORTHBOUND", "5"))
SENTIMENT_STALE_DAYS_MARGIN = int(os.getenv("SENTIMENT_STALE_DAYS_MARGIN", "5"))
SENTIMENT_STALE_DAYS_INDEX = int(os.getenv("SENTIMENT_STALE_DAYS_INDEX", "5"))
# brief=用户可读简报；debug=全源 ETL 样例（运维）
SENTIMENT_WECHAT_STYLE = os.getenv("SENTIMENT_WECHAT_STYLE", "brief").strip().lower()
SENTIMENT_WECHAT_DIGEST_LLM = os.getenv("SENTIMENT_WECHAT_DIGEST_LLM", "true").lower() == "true"
INFO_QUERY_LIVE_FETCH = os.getenv("INFO_QUERY_LIVE_FETCH", "true").lower() == "true"
INFO_QUERY_LIVE_MAX_KEYWORDS = int(os.getenv("INFO_QUERY_LIVE_MAX_KEYWORDS", "3"))
INFO_QUERY_MAX_SOURCES = int(os.getenv("INFO_QUERY_MAX_SOURCES", "5"))
INFO_QUERY_SNIPPET_CHARS = int(os.getenv("INFO_QUERY_SNIPPET_CHARS", "320"))

# ── 港股 (HKEX) 配置 ──────────────────────────────────────────────
# 港股日线数据源（目前仅东财）
HK_DAILY_PROVIDER = os.getenv("HK_DAILY_PROVIDER", "em").strip().lower()
# 新标的港股日线回溯天数
HK_DAILY_BOOTSTRAP_CALENDAR_DAYS = int(os.getenv("HK_DAILY_BOOTSTRAP_CALENDAR_DAYS", "400"))
# 港股日线分段请求跨度（天）
HK_DAILY_CHUNK_DAYS = int(os.getenv("HK_DAILY_CHUNK_DAYS", "90"))
# 港股截面最少标的数（面板就绪判定）
HK_MIN_PANEL_SYMBOLS = int(os.getenv("HK_MIN_PANEL_SYMBOLS", "500"))
# 港股分时周期（与 A 股相同的 1min/5min 窗口）
HK_INTRADAY_FINE_PERIOD = os.getenv("HK_INTRADAY_FINE_PERIOD", "1")
HK_INTRADAY_COARSE_PERIOD = os.getenv("HK_INTRADAY_COARSE_PERIOD", "5")
HK_INTRADAY_FINE_CALENDAR_DAYS = int(os.getenv("HK_INTRADAY_FINE_CALENDAR_DAYS", "5"))
HK_RECENT_INTRADAY_CALENDAR_DAYS = int(os.getenv("HK_RECENT_INTRADAY_CALENDAR_DAYS", "14"))
# 港股估值截面：是否启用逐票调用（stock_hk_financial_indicator_em）
HK_SNAPSHOT_PER_SYMBOL_ENABLED = os.getenv("HK_SNAPSHOT_PER_SYMBOL_ENABLED", "false").lower() == "true"
# 港股市场健康检查最大陈旧天数
HK_MAX_STALE_DAYS = int(os.getenv("HK_MAX_STALE_DAYS", "3"))

# ── 大盘预测验证阈值 ──
FORECAST_VERIFY_THRESHOLD_PCT = float(os.getenv("FORECAST_VERIFY_THRESHOLD_PCT", "0.3"))

# ── 筹码峰 (CYQ) 配置 ──
CYQ_WORKERS = int(os.getenv("CYQ_WORKERS", "6"))
CYQ_RATE_LIMIT = float(os.getenv("CYQ_RATE_LIMIT", "2.0"))
CYQ_MAX_STALE_DAYS = int(os.getenv("CYQ_MAX_STALE_DAYS", "5"))
