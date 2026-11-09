"""AI 伪人巡检后台任务注册表（内存态 + tasks 表持久化）。

模式照搬 web/routers/lottery.py 的 _create_task / _spawn / _progress / _mark_cancelled，
但独立维护，不注册进 lottery_store：抽奖接口查不到、也操作不了巡检任务。
终态只用 completed / failed / cancelled / interrupted（前端 pollTask 只认这几个），
不完整结果放在 result.partial 里。
"""
import asyncio
import time
import uuid
from typing import Any, Coroutine, Dict, List, Optional

from core.logger import get_logger
from modules.lottery.patrol import store

logger = get_logger(__name__)

KINDS = ("scan", "judge", "report")
FINISHED_TTL_SECONDS = 3600
_INTERNAL_KEYS = frozenset({"started_monotonic", "finished_monotonic", "fingerprint", "cancel_requested"})


class PatrolTaskRegistry:
    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._background: Dict[str, "asyncio.Task[None]"] = {}

    # ---------- 查询 ----------

    def _prune(self) -> None:
        now = time.monotonic()
        for tid in [t for t, v in self._tasks.items()
                    if v.get("finished_monotonic") and now - v["finished_monotonic"] > FINISHED_TTL_SECONDS]:
            self._tasks.pop(tid, None)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._tasks.get(task_id)

    def running_of(self, kind: str) -> Optional[str]:
        for tid, t in self._tasks.items():
            if t["kind"] == kind and t["status"] == "running":
                return tid
        return None

    def running_for_scan(self, scan_id: int) -> List[str]:
        return [tid for tid, t in self._tasks.items() if t["status"] == "running" and t.get("scan_id") == scan_id]

    def any_running(self) -> bool:
        return any(t["status"] == "running" for t in self._tasks.values())

    def find_by_fingerprint(self, fingerprint: str) -> Optional[str]:
        for tid, t in self._tasks.items():
            if t.get("fingerprint") == fingerprint and t["status"] == "running":
                return tid
        return None

    def public(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self._tasks.get(task_id)
        if task is not None:
            return {k: v for k, v in task.items() if k not in _INTERNAL_KEYS}
        stored = store.task_load(task_id)
        if stored is None:
            return None
        stored.pop("request", None)
        stored.pop("checkpoint", None)
        return stored

    # ---------- 生命周期 ----------

    def create(self, kind: str, request: Dict[str, Any], *, fingerprint: Optional[str] = None,
               scan_id: Optional[int] = None, task_id: Optional[str] = None) -> str:
        self._prune()
        resumed = task_id is not None
        task_id = task_id or uuid.uuid4().hex
        self._tasks[task_id] = {
            "task_id": task_id, "kind": kind, "status": "running", "stage": "queued", "progress": 0,
            "message": "任务已进入队列", "estimated_seconds": None, "result": None, "scan_id": scan_id,
            "started_monotonic": time.monotonic(), "fingerprint": fingerprint, "cancel_requested": False,
        }
        if resumed:
            store.task_finish(task_id, "running", "")
        else:
            store.task_create(task_id, kind, request, scan_id=scan_id)
        return task_id

    def set_scan(self, task_id: str, scan_id: int) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            task["scan_id"] = scan_id
        store.task_set_scan(task_id, scan_id)

    def progress(self, task_id: str, stage: str, percent: int, message: str) -> None:
        task = self._tasks.get(task_id)
        if not task or task["status"] != "running":
            return
        pct = max(1, min(99, int(percent)))
        elapsed = max(0.1, time.monotonic() - task["started_monotonic"])
        task.update(stage=stage, progress=pct, message=message,
                    estimated_seconds=round(elapsed * (100 - pct) / pct) if pct >= 5 else None)

    def is_cancelled(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        return task is None or bool(task.get("cancel_requested")) or task["status"] != "running"

    def spawn(self, task_id: str, coro: Coroutine[Any, Any, None]) -> None:
        background = asyncio.create_task(coro)
        self._background[task_id] = background
        background.add_done_callback(lambda done, tid=task_id: self._on_done(tid, done))

    def _on_done(self, task_id: str, background: "asyncio.Task[None]") -> None:
        self._background.pop(task_id, None)
        task = self._tasks.get(task_id)
        if task is None:
            return
        if background.cancelled():
            self._mark(task_id, "cancelled", "任务已取消")
        if task["status"] == "running":
            self._mark(task_id, "failed", "任务意外中止，请重新发起")
        task["finished_monotonic"] = time.monotonic()
        store.task_finish(task_id, task["status"], task.get("message") or "", task.get("result"))

    def _mark(self, task_id: str, status: str, message: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None and task["status"] == "running":
            task.update(status=status, stage=status, message=message, estimated_seconds=0)

    def complete(self, task_id: str, result: Dict[str, Any], message: str = "任务已完成") -> None:
        task = self._tasks.get(task_id)
        if task is None or task["status"] != "running":
            return
        task.update(status="completed", stage="completed", progress=100, message=message, result=result,
                    estimated_seconds=0)

    def fail(self, task_id: str, message: str) -> None:
        self._mark(task_id, "failed", message)

    def mark_cancelled(self, task_id: str) -> None:
        self._mark(task_id, "cancelled", "任务已取消")

    def request_cancel(self, task_id: str) -> str:
        """请求取消；返回结果状态。已结束且非中断的任务抛 ValueError。"""
        task = self._tasks.get(task_id)
        if task is None:
            stored = store.task_load(task_id)
            if stored is None:
                raise KeyError(task_id)
            if stored["status"] == "interrupted":
                store.task_finish(task_id, "cancelled", "任务已取消")
                return "cancelled"
            if stored["status"] == "cancelled":
                return "cancelled"
            raise ValueError("任务已结束，无法取消")
        if task["status"] == "cancelled":
            return "cancelled"
        if task["status"] != "running":
            raise ValueError("任务已结束，无法取消")
        task["cancel_requested"] = True
        if task["kind"] == "report":
            # 举报任务：当前这条完成后由循环自行停止，不强制打断正在进行的 POST。
            task["message"] = "已请求取消，当前这条提交完成后停止"
            return "cancelling"
        self.mark_cancelled(task_id)
        background = self._background.get(task_id)
        if background is not None and not background.done():
            background.cancel()
        return "cancelled"

    def cancel_all(self, reason: str) -> int:
        count = 0
        for tid, task in list(self._tasks.items()):
            if task["status"] == "running":
                try:
                    self.request_cancel(tid)
                    task["message"] = f"任务已取消（{reason}）"
                    count += 1
                except (KeyError, ValueError) as exc:
                    logger.warning("[巡检] 取消任务失败: %s %s", tid, exc)
        return count


registry = PatrolTaskRegistry()
