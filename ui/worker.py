"""把阻塞的会话操作放到线程池, 结果/进度/错误通过信号回到界面线程。

用法::

    def job(ctx):                 # ctx.emit_progress(done, total) 可选
        entries = session.listdir(path)
        return entries            # 任何异常会走 failure 信号

    run_async(job, on_success=self._on_listed, on_error=self._show_err)
"""
from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    success = Signal(object)           # 正常结果
    failure = Signal(object)           # Exception 实例
    progress = Signal(object, object)  # (已传输, 总量或 None)
    label = Signal(str)                # 进度文字(正在传输哪个文件等)


_bridge: QObject | None = None


def _ensure_bridge() -> QObject:
    """返回一个与 app 同生命周期的主线程 QObject。

    跨线程信号发送后, 若发送端对象被 Python GC 回收, 排队中的信号事件
    可能被丢弃(表现为任务完成却不回调)。把信号对象挂到桥接对象名下,
    让它在整个 app 生命周期内存活。
    """
    global _bridge
    if _bridge is None:
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        _bridge = QObject(app) if app is not None else QObject()
    return _bridge


class _Ctx:
    """工作线程里传给任务函数的上下文。"""

    def __init__(self) -> None:
        self._sig = _Signals(_ensure_bridge())
        self._cancel = threading.Event()

    # ---- 供界面线程使用 ----
    def cancel(self) -> None:
        self._cancel.set()

    def connect_progress(self, slot) -> None:
        self._sig.progress.connect(slot)

    def connect_label(self, slot) -> None:
        self._sig.label.connect(slot)

    # ---- 供工作线程使用 ----
    def emit_progress(self, done, total) -> None:
        self._sig.progress.emit(done, total)

    def emit_label(self, text: str) -> None:
        self._sig.label.emit(text)

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()


class _Worker(QRunnable):
    def __init__(self, fn: Callable[[_Ctx], object], ctx: _Ctx):
        super().__init__()
        self._fn = fn
        self._ctx = ctx

    def run(self) -> None:
        try:
            result = self._fn(self._ctx)
        except Exception as e:  # noqa: BLE001 - 必须兜底送给 UI
            self._ctx._sig.failure.emit(e)
        else:
            self._ctx._sig.success.emit(result)


class Task:
    """一次后台任务的句柄, 用于从界面线程取消。"""

    def __init__(self, ctx: _Ctx):
        self._ctx = ctx

    def cancel(self) -> None:
        self._ctx.cancel()


def run_async(fn: Callable[[_Ctx], object], *,
              on_success=None, on_error=None, on_progress=None,
              on_label=None) -> Task:
    ctx = _Ctx()
    if on_progress is not None:
        ctx.connect_progress(on_progress)
    if on_label is not None:
        ctx.connect_label(on_label)
    worker = _Worker(fn, ctx)
    if on_success is not None:
        ctx._sig.success.connect(on_success)
    if on_error is not None:
        ctx._sig.failure.connect(on_error)
    QThreadPool.globalInstance().start(worker)
    return Task(ctx)
