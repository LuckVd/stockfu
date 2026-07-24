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
