"""暂停/取消令牌测试。"""

from __future__ import annotations

import threading
import time

import pytest

from aimovie.errors import TaskCancelled
from aimovie.pipeline.control import TaskControl


def test_cancel_raises_on_checkpoint() -> None:
    control = TaskControl()
    control.checkpoint()
    control.cancel()
    assert control.cancelled is True
    with pytest.raises(TaskCancelled):
        control.checkpoint()


def test_pause_blocks_and_resume_releases() -> None:
    control = TaskControl()
    entered = threading.Event()
    released = threading.Event()

    def worker() -> None:
        entered.set()
        control.checkpoint()
        released.set()

    control.pause()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert entered.wait(1.0)
    assert control.paused is True
    time.sleep(0.2)
    assert not released.is_set(), "暂停后检查点不应继续"
    control.resume()
    assert released.wait(2.0), "继续后检查点应返回"
    thread.join(timeout=2.0)


def test_pause_then_cancel_releases_waiting_checkpoint() -> None:
    control = TaskControl()
    entered = threading.Event()
    result: list[str] = []

    def worker() -> None:
        entered.set()
        try:
            control.checkpoint()
        except TaskCancelled:
            result.append("cancelled")

    control.pause()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert entered.wait(1.0)
    time.sleep(0.2)
    control.cancel()
    thread.join(timeout=2.0)
    assert result == ["cancelled"]


def test_reset_clears_state() -> None:
    control = TaskControl()
    control.cancel()
    control.reset()
    assert not control.cancelled
    control.checkpoint()
