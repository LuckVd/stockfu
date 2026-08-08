"""全局配置（pydantic-settings，读取 .env）。

设计要点：所有可选项「不配置也能跑」——免费公开数据源作为默认能力，
配置 token/key 后增强质量。这与 daily_stock_analysis 的配置哲学一致。
"""
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# /opt/pro/stockfu
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 基础
    app_name: str = "StockFu·资产管理终端"
    api_host: str = "127.0.0.1"
    api_port: int = 8787

    # 存储
    db_url: str = f"sqlite:///{DATA_DIR / 'stockfu.db'}"
    # 算子结果是可再生缓存，必须与研究主数据物理隔离。
    operator_cache_db_url: str = f"sqlite:///{DATA_DIR / 'operator_cache.db'}"

    # 种子自选
    watchlist: str = "600519,000858,600036,601318,510300,510500"

    # 数据源增强
    tushare_token: str = ""

    # AI
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # 定时
    daily_cron: str = "18 18 * * 1-5"

    # 网络：港美股(yfinance)走代理；国内数据源(akshare/efinance)直连
    proxy_url: str = "http://127.0.0.1:7890"
    proxy_bypass: str = ("eastmoney.com,sina.com.cn,qq.com,cninfo.com.cn,"
                         "localhost,127.0.0.1,local,.local")

    @property
    def watchlist_codes(self) -> list[str]:
        return [c.strip() for c in self.watchlist.split(",") if c.strip()]

    def ensure_dirs(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()


def setup_network() -> None:
    """配置代理：yfinance 走代理(在 yfinance_source 内注入 proxy session)，国内源直连。

    重要：**不**设全局 HTTP_PROXY 环境变量——efinance/akshare 底层 session 一旦
    在 import/调用时见到全局代理就会误用(SSL EOF / InvalidSchema)，导致 A 股源
    全部失败、fallback 到 yfinance 返回港股脏数据(如 002594 显示 'BYD COMPANY LI')。
    故代理仅在 yfinance_source._proxy_session() 内按需注入；国内源全程无代理直连。
    no_proxy 仍设(防御外部已预设的代理)。
    """
    if settings.proxy_bypass:
        for k in ("no_proxy", "NO_PROXY"):
            os.environ.setdefault(k, settings.proxy_bypass)


# ---------- 运行时可变的外网代理（web 设置面板写入，供 yfinance 用）----------
_proxy_cache: dict[str, str] = {}


def get_overseas_proxy() -> str:
    """yfinance 实际使用的代理地址。

    优先 app_config['overseas_proxy']（web 面板设置过）；未设置过则回落 .env 的
    proxy_url（向后兼容）。空串 = 直连。带内存缓存，set 时清空。
    """
    if "overseas_proxy" in _proxy_cache:
        return _proxy_cache["overseas_proxy"]
    from stockfu.db import get_app_config, has_app_config
    if has_app_config("overseas_proxy"):
        effective = get_app_config("overseas_proxy")
    else:
        effective = settings.proxy_url
    _proxy_cache["overseas_proxy"] = effective
    return effective


def set_overseas_proxy(value: str) -> str:
    """保存外网代理到 db，清缓存。空串 = 直连。返回生效值。"""
    from stockfu.db import set_app_config
    set_app_config("overseas_proxy", (value or "").strip())
    _proxy_cache.clear()
    return get_overseas_proxy()


def test_overseas_proxy(proxy_url: str | None = None) -> dict:
    """用指定代理（None=当前生效；空串=直连）实际请求 Yahoo，三态判定。"""
    import time
    import requests

    url = proxy_url if proxy_url is not None else get_overseas_proxy()
    sess = requests.Session()
    if url:
        sess.proxies = {"http": url, "https": url}
    target = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d"
    t0 = time.time()
    try:
        r = sess.get(target, timeout=10)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "status": None, "latency_ms": None,
                "detail": f"代理连不上：{type(e).__name__}"}
    ms = int((time.time() - t0) * 1000)
    if r.status_code == 200:
        return {"ok": True, "status": 200, "latency_ms": ms,
                "detail": "代理可用，Yahoo 正常返回"}
    if r.status_code in (403, 429):
        return {"ok": False, "status": r.status_code, "latency_ms": ms,
                "detail": "代理可达，但该出口 IP 被 Yahoo 封了，需换住宅/海外节点"}
    return {"ok": False, "status": r.status_code, "latency_ms": ms,
            "detail": f"Yahoo 返回 {r.status_code}"}


# ---------- 定时抓取配置（web 设置面板写入，北京时间）----------
import re

_SCHEDULE_CACHE: dict[str, str] = {}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _cached_int(key: str, default: int) -> int:
    if key in _SCHEDULE_CACHE:
        try:
            return int(_SCHEDULE_CACHE[key])
        except (TypeError, ValueError):
            pass
    from stockfu.db import get_app_config, has_app_config
    raw = get_app_config(key, str(default)) if has_app_config(key) else str(default)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    _SCHEDULE_CACHE[key] = str(n)
    return n


def get_daily_fetch_time() -> str:
    """每日定时抓取的北京时间 HH:MM，默认 15:30（A 股收盘后）。"""
    if "daily_fetch_time" in _SCHEDULE_CACHE:
        return _SCHEDULE_CACHE["daily_fetch_time"]
    from stockfu.db import get_app_config, has_app_config
    v = get_app_config("daily_fetch_time", "15:30") if has_app_config("daily_fetch_time") else "15:30"
    if not _TIME_RE.match(v):
        v = "15:30"
    _SCHEDULE_CACHE["daily_fetch_time"] = v
    return v


def set_daily_fetch_time(value: str) -> str:
    v = (value or "").strip()
    if not _TIME_RE.match(v):
        v = "15:30"
    from stockfu.db import set_app_config
    set_app_config("daily_fetch_time", v)
    _SCHEDULE_CACHE.clear()
    return get_daily_fetch_time()


def get_fetch_retry_interval() -> int:
    """拉取失败的重试间隔（分钟），默认 1（港美股断连时不等太久）。"""
    return max(1, _cached_int("fetch_retry_interval", 1))


def set_fetch_retry_interval(value) -> int:
    from stockfu.db import set_app_config
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 10
    set_app_config("fetch_retry_interval", str(max(1, n)))
    _SCHEDULE_CACHE.clear()
    return get_fetch_retry_interval()


def get_fetch_retry_count() -> int:
    """重试次数（0=只跑一轮不重试），默认 3。"""
    return max(0, _cached_int("fetch_retry_count", 3))


def set_fetch_retry_count(value) -> int:
    from stockfu.db import set_app_config
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 3
    set_app_config("fetch_retry_count", str(max(0, n)))
    _SCHEDULE_CACHE.clear()
    return get_fetch_retry_count()


# ---------- 邮件定时（web 设置面板写入，QQ 邮箱默认）----------
# 邮箱服务预设：面板选预设即自动填 host/port/SSL；也可自填（通用 SMTP）
MAIL_PRESETS = {
    "qq":    {"host": "smtp.qq.com",    "port": 465, "ssl": True,  "label": "QQ 邮箱"},
    "163":   {"host": "smtp.163.com",   "port": 465, "ssl": True,  "label": "163 邮箱"},
    "gmail": {"host": "smtp.gmail.com", "port": 587, "ssl": False, "label": "Gmail"},
}

_MAIL_CACHE: dict[str, str] = {}


def _mail_cfg(key: str, default: str) -> str:
    if key in _MAIL_CACHE:
        return _MAIL_CACHE[key]
    from stockfu.db import get_app_config, has_app_config
    v = get_app_config(key, default) if has_app_config(key) else default
    _MAIL_CACHE[key] = v
    return v


def _set_mail_cfg(key: str, value: str) -> None:
    from stockfu.db import set_app_config
    set_app_config(key, value)
    _MAIL_CACHE.clear()


def get_smtp_host():    return _mail_cfg("smtp_host", "smtp.qq.com")
def get_smtp_port():    return int(_mail_cfg("smtp_port", "465") or 465)
def get_smtp_user():    return _mail_cfg("smtp_user", "")
def get_smtp_pass():    return _mail_cfg("smtp_pass", "")
def get_smtp_from():    return _mail_cfg("smtp_from", "")  # 空 = 用 smtp_user
def get_mail_to():      return _mail_cfg("mail_to", "")
def get_mail_enabled(): return _mail_cfg("mail_enabled", "0") == "1"
def get_mail_time():
    v = _mail_cfg("mail_time", "16:00")
    return v if _TIME_RE.match(v) else "16:00"
def get_mail_days():    return _mail_cfg("mail_days", "mon-fri") or "mon-fri"


def set_mail_config(data: dict) -> None:
    """批量写入邮件配置（空 smtp_pass 视为不改密码）。"""
    for k in ("smtp_host", "smtp_port", "smtp_from", "mail_to"):
        if data.get(k) is not None:
            _set_mail_cfg(k, str(data[k]).strip())
    if data.get("smtp_user") is not None:           # 账号允许清空
        _set_mail_cfg("smtp_user", str(data["smtp_user"]).strip())
    if data.get("smtp_pass"):                        # 空串 = 不改
        _set_mail_cfg("smtp_pass", str(data["smtp_pass"]))
    if "mail_enabled" in data:
        _set_mail_cfg("mail_enabled", "1" if data["mail_enabled"] else "0")
    if "mail_time" in data:
        v = str(data["mail_time"]).strip()
        _set_mail_cfg("mail_time", v if _TIME_RE.match(v) else "16:00")
    if "mail_days" in data:
        _set_mail_cfg("mail_days", str(data["mail_days"]).strip() or "mon-fri")


def get_mail_config() -> dict:
    """聚合读（密码脱敏为 has_password 布尔）。"""
    return {
        "smtp_host": get_smtp_host(),
        "smtp_port": get_smtp_port(),
        "smtp_user": get_smtp_user(),
        "has_password": bool(get_smtp_pass()),
        "smtp_from": get_smtp_from(),
        "mail_to": get_mail_to(),
        "mail_enabled": get_mail_enabled(),
        "mail_time": get_mail_time(),
        "mail_days": get_mail_days(),
        "presets": MAIL_PRESETS,
    }


def is_mail_ready() -> bool:
    """是否具备发信条件（账号 + 密码 + 收件人）。enabled 由调用方另判。"""
    return bool(get_smtp_user() and get_smtp_pass() and get_mail_to())


# ---------- 策略信号扫描与推荐邮件 ----------
_SIGNAL_CACHE: dict[str, str] = {}


def _signal_cfg(key: str, default: str) -> str:
    if key in _SIGNAL_CACHE:
        return _SIGNAL_CACHE[key]
    from stockfu.db import get_app_config, has_app_config
    value = get_app_config(key, default) if has_app_config(key) else default
    _SIGNAL_CACHE[key] = value
    return value


def _set_signal_cfg(key: str, value: str) -> None:
    from stockfu.db import set_app_config
    set_app_config(key, value)
    _SIGNAL_CACHE.clear()


def get_signal_factor_enabled() -> bool:
    """是否运行全指数成分因子扫描；默认开启。"""
    return _signal_cfg("signal_factor_enabled", "1") == "1"


def get_signal_llm_enabled() -> bool:
    """是否允许逐股 LLM 分析；仍需逐股订阅开启。"""
    return _signal_cfg("signal_llm_enabled", "0") == "1"


def get_signal_mail_enabled() -> bool:
    """是否发送推荐专用邮件；默认关闭。"""
    return _signal_cfg("signal_mail_enabled", "0") == "1"


def get_signal_scan_time() -> str:
    value = _signal_cfg("signal_scan_time", "16:10")
    return value if _TIME_RE.match(value) else "16:10"


def get_signal_strategy_ids() -> list[str]:
    """动态启用策略；未配置时使用正式全周期目录。"""
    import json
    from stockfu.backtest.full_cycle_update import catalog_ids

    raw = _signal_cfg("signal_strategy_ids", "")
    if not raw:
        return catalog_ids()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = raw.split(",")
    if not isinstance(parsed, list):
        return catalog_ids()
    return list(dict.fromkeys(
        str(value).strip() for value in parsed if str(value).strip()
    ))


def set_signal_config(data: dict) -> None:
    """保存信号扫描全局配置；逐股开关由 subscription 表管理。"""
    import json

    for key in ("factor_enabled", "llm_enabled", "mail_enabled"):
        if key in data:
            _set_signal_cfg(f"signal_{key}", "1" if data[key] else "0")
    if "scan_time" in data:
        value = str(data["scan_time"] or "").strip()
        _set_signal_cfg("signal_scan_time", value if _TIME_RE.match(value) else "16:10")
    if "strategy_ids" in data:
        raw = data["strategy_ids"]
        if not isinstance(raw, list):
            raise ValueError("strategy_ids 必须是数组")
        ids = list(dict.fromkeys(
            str(value).strip() for value in raw if str(value).strip()
        ))
        if not ids:
            raise ValueError("至少选择一个策略")
        _set_signal_cfg("signal_strategy_ids", json.dumps(ids, ensure_ascii=False))


def get_signal_config() -> dict:
    return {
        "factor_enabled": get_signal_factor_enabled(),
        "llm_enabled": get_signal_llm_enabled(),
        "mail_enabled": get_signal_mail_enabled(),
        "scan_time": get_signal_scan_time(),
        "strategy_ids": get_signal_strategy_ids(),
    }


# ---------- LLM 配置（web 设置面板写入，AI 顾问用；回落 .env）----------
# 与邮件/代理同机制：app_config 表持久化 + 内存缓存（set 时清空）。
# ai/client.py 调 get_llm_*() 而非直接读 settings.llm_*，故面板改完无需重启即生效。
_LLM_CACHE: dict[str, str] = {}


def _llm_cfg(key: str, default: str) -> str:
    if key in _LLM_CACHE:
        return _LLM_CACHE[key]
    from stockfu.db import get_app_config, has_app_config
    v = get_app_config(key, default) if has_app_config(key) else default
    _LLM_CACHE[key] = v
    return v


def _set_llm_cfg(key: str, value: str) -> None:
    from stockfu.db import set_app_config
    set_app_config(key, value)
    _LLM_CACHE.clear()


def get_llm_base_url() -> str:
    """LLM 网关地址。优先面板设置，回落 .env 的 llm_base_url。"""
    return _llm_cfg("llm_base_url", settings.llm_base_url)


def get_llm_api_key() -> str:
    """LLM API Key。优先面板设置，回落 .env。"""
    return _llm_cfg("llm_api_key", settings.llm_api_key)


def get_llm_model() -> str:
    """LLM 模型名。优先面板设置，回落 .env。"""
    return _llm_cfg("llm_model", settings.llm_model)


def set_llm_config(data: dict) -> None:
    """批量写入 LLM 配置（api_key 空串 = 不改；base_url/model 传 None = 不改）。"""
    if data.get("llm_base_url") is not None:
        _set_llm_cfg("llm_base_url", str(data["llm_base_url"]).strip())
    if data.get("llm_model") is not None:
        _set_llm_cfg("llm_model", str(data["llm_model"]).strip())
    if data.get("llm_api_key"):              # 空串 = 不改
        _set_llm_cfg("llm_api_key", str(data["llm_api_key"]).strip())


def get_llm_config() -> dict:
    """聚合读（api_key 脱敏为 has_api_key 布尔；source 标注值来自面板还是 .env）。"""
    from stockfu.db import has_app_config
    db_configured = any(has_app_config(k) for k in ("llm_base_url", "llm_api_key", "llm_model"))
    return {
        "llm_base_url": get_llm_base_url(),
        "llm_model": get_llm_model(),
        "has_api_key": bool(get_llm_api_key()),
        "source": "db" if db_configured else "env",
    }
