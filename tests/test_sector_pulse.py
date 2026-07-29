from contextlib import contextmanager
from datetime import date, timedelta
from unittest import TestCase, mock

from sqlmodel import SQLModel, Session, create_engine, select

import stockfu.models  # noqa: F401
from stockfu.models import SectorFlowSnapshot, SectorSnapshot


class TestSectorPulse(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.day = date(2026, 7, 23)

    def tearDown(self):
        self.session.close()

    @contextmanager
    def _scope(self):
        yield self.session

    def _seed(self, name: str, *, flow_today=True):
        for i in range(30):
            d = self.day - timedelta(days=29 - i)
            close = 100 + i
            self.session.add(SectorSnapshot(sector_name=name, snap_date=d, close=close,
                amount=1000 + i * 10, pct_chg=1.0 if i else None))
            if flow_today or d != self.day:
                self.session.add(SectorFlowSnapshot(sector_name=name, snap_date=d,
                    net_inflow=1.0 if i >= 25 else -1.0))
        self.session.commit()

    def test_only_same_day_quote_and_flow_are_exported(self):
        self._seed("完整行业")
        self._seed("缺当日资金", flow_today=False)
        with mock.patch("stockfu.services.sector_pulse.session_scope", self._scope):
            from stockfu.services.sector_pulse import build
            result = build(self.day)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rows"][0]["name"], "完整行业")
        self.assertEqual(result["rows"][0]["state"], "连续流入")

    def test_same_day_flow_uses_cross_section_rank_from_first_day(self):
        for i in range(10):
            name = f"行业{i}"
            self._seed(name)
            row = self.session.exec(select(SectorFlowSnapshot).where(
                SectorFlowSnapshot.sector_name == name,
                SectorFlowSnapshot.snap_date == self.day)).one()
            row.net_inflow = float(i - 5)
        self.session.commit()
        with mock.patch("stockfu.services.sector_pulse.session_scope", self._scope):
            from stockfu.services.sector_pulse import build
            result = build(self.day)
        rows = {r["name"]: r for r in result["rows"]}
        self.assertEqual(result["count"], 10)
        self.assertGreater(rows["行业9"]["fund_rank"], rows["行业0"]["fund_rank"])
        self.assertIsNotNone(rows["行业9"]["greed"])

    def test_single_flow_day_is_not_labeled_continuous(self):
        from stockfu.services.sector_pulse import _state
        self.assertEqual(_state([1.0]), "当日净流入")


class _FakeResp:
    """模拟 requests.Response(只需 status_code + text)。"""
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


class TestSectorKlineRetry(TestCase):
    """get_sector_kline_period: 端点失败时重试 + 失败可见(不再静默 return [])。"""

    @contextmanager
    def _noop_cm(self):
        yield

    def _src_with_catalog(self):
        from stockfu.data.akshare_source import AkshareSource
        src = AkshareSource()
        return src, mock.patch.object(src, "get_sector_catalog_ths",
                                      return_value=[{"name": "银行", "code": "881155"}])

    def test_retries_on_exception_then_returns_empty(self):
        src, cat_patch = self._src_with_catalog()
        with cat_patch, \
             mock.patch("stockfu.data.akshare_source.direct_connection", self._noop_cm), \
             mock.patch("requests.get", side_effect=TimeoutError("simulated")) as rg, \
             mock.patch("stockfu.data.akshare_source.time.sleep"):
            bars = src.get_sector_kline_period("银行", "20260101", "20260128")
        self.assertEqual(bars, [])
        self.assertEqual(rg.call_count, 3)          # 初试 + 2 次重试

    def test_retries_on_non_200_then_returns_empty(self):
        src, cat_patch = self._src_with_catalog()
        with cat_patch, \
             mock.patch("stockfu.data.akshare_source.direct_connection", self._noop_cm), \
             mock.patch("requests.get", return_value=_FakeResp("x", status=503)) as rg, \
             mock.patch("stockfu.data.akshare_source.time.sleep"):
            bars = src.get_sector_kline_period("银行", "20260101", "20260128")
        self.assertEqual(bars, [])
        self.assertEqual(rg.call_count, 3)

    def test_success_no_retry_parses_bars(self):
        payload = 'cb({"data":"20260105,10,11,9,10.5,1000,10000;20260106,10.5,11,9.5,11,1200,12000"})'
        src, cat_patch = self._src_with_catalog()
        with cat_patch, \
             mock.patch("stockfu.data.akshare_source.direct_connection", self._noop_cm), \
             mock.patch("requests.get", return_value=_FakeResp(payload)) as rg:
            bars = src.get_sector_kline_period("银行", "20260101", "20260128")
        self.assertEqual(len(bars), 2)
        self.assertEqual([b.date for b in bars], [date(2026, 1, 5), date(2026, 1, 6)])
        self.assertEqual(rg.call_count, 1)          # 成功不重试
