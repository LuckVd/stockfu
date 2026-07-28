from contextlib import contextmanager
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine

from stockfu.services import backfill_checkpoint as checkpoints


class TestBackfillCheckpoint:
    def setup_method(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    @contextmanager
    def _scope(self):
        with Session(self.engine) as session:
            yield session

    def test_success_is_skipped_and_failed_is_retried(self):
        with patch.object(checkpoints, "session_scope", self._scope):
            checkpoints.mark_item("dividend", "v1:2007-2026", "000001", success=True)
            checkpoints.mark_item("dividend", "v1:2007-2026", "000002", success=False,
                                  error="network")

            pending, skipped = checkpoints.pending_items(
                "dividend", "v1:2007-2026", ["000001", "000002", "000003"],
            )
            assert pending == ["000002", "000003"]
            assert skipped == 1
            assert checkpoints.checkpoint_summary("dividend", "v1:2007-2026") == {
                "success": 1, "failed": 1,
            }

    def test_refresh_runs_every_item_and_success_clears_error(self):
        with patch.object(checkpoints, "session_scope", self._scope):
            checkpoints.mark_item("etf_quotes", "v1", "510300", success=False, error="timeout")
            checkpoints.mark_item("etf_quotes", "v1", "510300", success=True)

            pending, skipped = checkpoints.pending_items(
                "etf_quotes", "v1", ["510300", "510500"], refresh=True,
            )
            assert pending == ["510300", "510500"]
            assert skipped == 0
            assert checkpoints.checkpoint_summary("etf_quotes", "v1") == {
                "success": 1, "failed": 0,
            }
