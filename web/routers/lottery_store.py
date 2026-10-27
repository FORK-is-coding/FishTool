"""抽奖后台任务持久化（复用 ``tasks`` 表，以 ``task_key`` 存对外 UUID 句柄）。

只在生命周期节点写库：创建、完成 / 失败 / 取消 / 中断；进度百分比仍只在内存里刷新。
数据库不可用时一律降级为只记 warning，任务照常在内存里运行，绝不因持久化失败让抽奖失败。
"""

# 时间戳，用于开始 / 完成时间与过期清理。
from datetime import datetime, timedelta
# 类型标注。
from typing import Any, Dict, Optional

# 复用既有任务表与会话工厂。
from core.database import Task, get_session
# 统一日志记录器。
from core.logger import get_logger

# 模块级日志实例。
logger = get_logger(__name__)

#: 抽奖任务在 ``tasks.task_type`` 中的取值，与评论采集等其他任务类型隔离。
TASK_TYPES = {"filter": "lottery_filter", "draw": "lottery_draw"}
#: 已结束任务的默认保留天数，可用 ``lottery.task_retention_days`` 覆盖。
DEFAULT_RETENTION_DAYS = 7


def _kind_of(task_type: Optional[str]) -> Optional[str]:
    """把表中的 task_type 还原成 filter / draw。"""
    for kind, value in TASK_TYPES.items():
        if value == task_type:
            return kind
    return None


def _to_state(row: Task) -> Dict[str, Any]:
    """把数据库行转换成与内存任务表一致的对外状态。"""
    params = row.params if isinstance(row.params, dict) else {}
    status = row.status or "failed"
    return {
        "task_id": row.task_key,
        "kind": _kind_of(row.task_type),
        "status": status,
        "stage": status,
        "progress": 100 if status == "completed" else int(round((row.progress or 0) * 100)),
        "message": row.error or ("任务已完成" if status == "completed" else ""),
        "estimated_seconds": 0 if status != "running" else None,
        "result": row.result if status == "completed" else None,
        "request": params.get("request"),
    }


def create(task_id: str, kind: str, request: Dict[str, Any]) -> None:
    """记录一个新建的运行中任务。

    Args:
        task_id: 对外 UUID 句柄。
        kind: ``filter`` 或 ``draw``。
        request: JSON 化后的请求体，供中断后按原参数继续执行。
    """
    session = None
    try:
        session = get_session()
        session.add(Task(
            task_key=task_id,
            task_type=TASK_TYPES[kind],
            task_name=f"抽奖{'真人筛选' if kind == 'filter' else '随机抽奖'}",
            params={"request": request},
            status="running",
            progress=0.0,
            started_at=datetime.now(),
        ))
        session.commit()
    except Exception as exc:  # noqa: BLE001 - 持久化失败只降级，不影响任务本身
        logger.warning("抽奖任务持久化失败（任务仍在内存中运行）: %s %s", task_id, exc)
        if session is not None:
            session.rollback()
    finally:
        if session is not None:
            session.close()


def finish(task_id: str, status: str, message: str, result: Optional[Dict[str, Any]] = None) -> None:
    """记录任务终态；完成时连同结果一次写入，保证「有结果即已完成」不会被重跑。

    Args:
        task_id: 对外 UUID 句柄。
        status: ``completed`` / ``failed`` / ``cancelled`` / ``interrupted`` / ``running``。
        message: 对外提示文案（失败原因等）。
        result: 完成时的结果。
    """
    session = None
    try:
        session = get_session()
        row = session.query(Task).filter(Task.task_key == task_id).first()
        if row is None:
            return
        row.status = status
        row.error = message
        if status == "completed":
            row.result = result
            row.progress = 1.0
        if status != "running":
            row.completed_at = datetime.now()
        else:
            row.completed_at = None
            row.started_at = datetime.now()
        session.commit()
    except Exception as exc:  # noqa: BLE001 - 持久化失败只降级
        logger.warning("抽奖任务状态持久化失败: %s %s %s", task_id, status, exc)
        if session is not None:
            session.rollback()
    finally:
        if session is not None:
            session.close()


def load(task_id: str) -> Optional[Dict[str, Any]]:
    """按句柄读取任务状态；不存在或数据库不可用时返回 None。"""
    session = None
    try:
        session = get_session()
        row = session.query(Task).filter(
            Task.task_key == task_id, Task.task_type.in_(list(TASK_TYPES.values()))
        ).first()
        return _to_state(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001 - 读失败按「不存在」处理
        logger.warning("读取抽奖任务失败: %s %s", task_id, exc)
        return None
    finally:
        if session is not None:
            session.close()


def recover_on_startup(retention_days: int = DEFAULT_RETENTION_DAYS) -> Dict[str, Any]:
    """服务启动时的收尾：遗留 running 标为 interrupted，清理过期记录，取回最近一次中奖名单。

    Args:
        retention_days: 已结束任务的保留天数。

    Returns:
        ``{"interrupted": 条数, "pruned": 条数, "latest_winners": 名单}``。
    """
    outcome: Dict[str, Any] = {"interrupted": 0, "pruned": 0, "latest_winners": []}
    session = None
    try:
        session = get_session()
        types = list(TASK_TYPES.values())
        # 上次进程退出时仍在运行的任务：不自动续跑（会消耗配额与 token），等用户点「继续执行」。
        stale = session.query(Task).filter(Task.task_type.in_(types), Task.status == "running").all()
        for row in stale:
            row.status = "interrupted"
            row.error = "服务重启，任务已中断；可点击「继续执行」，已完成的部分会直接复用"
            row.completed_at = datetime.now()
        outcome["interrupted"] = len(stale)
        # 过期清理只针对已结束的抽奖任务，不碰其他任务类型。
        cutoff = datetime.now() - timedelta(days=max(1, int(retention_days)))
        outcome["pruned"] = session.query(Task).filter(
            Task.task_type.in_(types),
            Task.status.in_(["completed", "failed", "cancelled", "interrupted"]),
            Task.completed_at.isnot(None),
            Task.completed_at < cutoff,
        ).delete(synchronize_session=False)
        latest = session.query(Task).filter(
            Task.task_type == TASK_TYPES["draw"], Task.status == "completed"
        ).order_by(Task.completed_at.desc()).first()
        if latest is not None and isinstance(latest.result, dict):
            winners = latest.result.get("winners")
            outcome["latest_winners"] = [dict(item) for item in winners if isinstance(item, dict)] if isinstance(winners, list) else []
        session.commit()
    except Exception as exc:  # noqa: BLE001 - 启动收尾失败不阻塞 Web 启动
        logger.warning("抽奖任务启动收尾失败: %s", exc)
        if session is not None:
            session.rollback()
    finally:
        if session is not None:
            session.close()
    return outcome
