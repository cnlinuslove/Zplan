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
AKSHARE_ALLOW_TX_FALLBACK = os.getenv("AKSHARE_ALLOW_TX_FALLBACK", "false").lower() == "true"
AKSHARE_FAIL_CIRCUIT_THRESHOLD = int(os.getenv("AKSHARE_FAIL_CIRCUIT_THRESHOLD", "3"))
AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS = int(os.getenv("AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS", "20"))

# Phase A.1：日线回溯 + 近端分时（Parquet）
PARQUET_ROOT = Path(os.getenv("PARQUET_ROOT", str(BASE_DIR / "parquet"))).expanduser().resolve()
DAILY_BOOTSTRAP_CALENDAR_DAYS = int(os.getenv("DAILY_BOOTSTRAP_CALENDAR_DAYS", "400"))
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

# LLM summary (Gemini)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_BASE_URL = os.getenv(
    "GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
)
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "60"))
LLM_SUMMARY_ENABLED = os.getenv("LLM_SUMMARY_ENABLED", "true").lower() == "true"
GEMINI_SUMMARY_MAX_ITEMS = int(os.getenv("GEMINI_SUMMARY_MAX_ITEMS", "15"))
GEMINI_SUMMARY_CHARS_PER_ITEM = int(os.getenv("GEMINI_SUMMARY_CHARS_PER_ITEM", "900"))
GEMINI_MIN_SECONDS_BETWEEN_TOPICS = float(os.getenv("GEMINI_MIN_SECONDS_BETWEEN_TOPICS", "8"))
GEMINI_MIN_SECONDS_BETWEEN_CALLS = float(os.getenv("GEMINI_MIN_SECONDS_BETWEEN_CALLS", "3"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "4096"))
X_QUERY_EXCLUDE_SUFFIX = os.getenv("X_QUERY_EXCLUDE_SUFFIX", "").strip()

# WeChat
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
INFO_QUERY_LIVE_FETCH = os.getenv("INFO_QUERY_LIVE_FETCH", "true").lower() == "true"
INFO_QUERY_LIVE_MAX_KEYWORDS = int(os.getenv("INFO_QUERY_LIVE_MAX_KEYWORDS", "3"))
INFO_QUERY_MAX_SOURCES = int(os.getenv("INFO_QUERY_MAX_SOURCES", "5"))
INFO_QUERY_SNIPPET_CHARS = int(os.getenv("INFO_QUERY_SNIPPET_CHARS", "320"))
