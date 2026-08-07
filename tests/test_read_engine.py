"""read_engine / use_read_engine 机制测试（阻塞 1 基建，db.py）。

contextvar 未设置时回落全局 engine（全 app/V1 行为不变）；use_read_engine 在块内
切到注入引擎、退出恢复；session_scope/get_session 跟随 read_engine。
"""
from __future__ import annotations

from sqlalchemy import create_engine

import stockfu.db as db


def test_read_engine_defaults_to_global():
    assert db.read_engine() is db.engine


def test_use_read_engine_swaps_and_restores():
    fake = create_engine("sqlite://")
    try:
        assert db.read_engine() is db.engine
        with db.use_read_engine(fake):
            assert db.read_engine() is fake
        assert db.read_engine() is db.engine
    finally:
        fake.dispose()


def test_use_read_engine_nests():
    a = create_engine("sqlite://")
    b = create_engine("sqlite://")
    try:
        with db.use_read_engine(a):
            assert db.read_engine() is a
            with db.use_read_engine(b):
                assert db.read_engine() is b
            assert db.read_engine() is a
        assert db.read_engine() is db.engine
    finally:
        a.dispose()
        b.dispose()


def test_session_scope_follows_read_engine():
    fake = create_engine("sqlite://")
    try:
        with db.use_read_engine(fake):
            with db.session_scope() as s:
                assert s.get_bind() is fake
        # 退出后回到全局
        with db.session_scope() as s:
            assert s.get_bind() is db.engine
    finally:
        fake.dispose()


def test_get_session_generator_follows_read_engine():
    """get_session 是 FastAPI 依赖用的生成器（非 contextmanager）；手动驱动。"""
    fake = create_engine("sqlite://")
    try:
        with db.use_read_engine(fake):
            s = next(db.get_session())
            try:
                assert s.get_bind() is fake
            finally:
                s.close()
    finally:
        fake.dispose()
