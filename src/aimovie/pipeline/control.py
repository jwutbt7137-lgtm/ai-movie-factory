"""后台任务的暂停/取消控制。

所有耗时循环都必须调用 :meth:`TaskControl.checkpoint`，以便及时响应用户操作。
暂停在检查点生效；正在执行的单个外部进程（如 ffmpeg 抽取音轨）会先完成当前步骤。
"""

from __future__ import annotations

import threading
import time

from ..errors import TaskCancelled


class TaskControl:
    """线程安全的暂停/取消令牌。"""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._paused = threading.Event()
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set() and not self._cancel.is_set()

    def reset(self) -> None:
        with self._lock:
            self._cancel.clear()
            self._paused.clear()
            self._resume.set()

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            self._paused.clear()
            self._resume.set()

    def pause(self) -> None:
        with self._lock:
            if self._cancel.is_set():
                return
            self._paused.set()
            self._resume.clear()

    def resume(self) -> None:
        with self._lock:
            self._paused.clear()
            self._resume.set()

    def checkpoint(self) -> None:
        """取消则抛 :class:`TaskCancelled`；暂停则阻塞到继续或取消。"""
        if self._cancel.is_set():
            raise TaskCancelled()
        if not self._resume.is_set():
            self._paused.set()
            # 每 0.2 秒醒来一次，保证取消操作不被暂停卡住
            while not self._resume.wait(0.2):
                if self._cancel.is_set():
                    raise TaskCancelled()
            self._paused.clear()
        if self._cancel.is_set():
            raise TaskCancelled()

    def sleep(self, seconds: float) -> None:
        """可取消的短暂等待（测试与轮询使用）。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            self.checkpoint()
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        self.checkpoint()
