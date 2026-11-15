"""动态限速接口：维护限速规则，覆盖提交规则评审、启用规则、关闭规则等动作。

路由顺序：静态路径（/signals、/decisions、/export）必须声明在 /{entry_id} 之前，
否则会被当成规则 ID 解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.signals import SIGNAL_STATUSES
from app.services.throttle import ThrottleService

router = APIRouter(prefix="/api/throttle", tags=["动态限速"])

service = ThrottleService()

LIST_FIELDS = ["规则编号", "目标域组", "初始速率", "最大速率", "加速因子", "减速因子", "错误率阈值", "降速恢复间隔"]
STATUSES = ["草稿", "规则评审中", "已启用", "已关闭"]


@router.get("/signals", response_model=PageResult[dict])
def list_signals(
    rule_id: int | None = Query(default=None, description="按限速规则 ID 过滤"),
    status: str | None = Query(default=None, description="已观察、待确认、待复核、已确认、已合并、已忽略"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """与限速规则相关的告警触发结果；规则关闭后历史记录仍然保留。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_signals(entry_id=rule_id, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/signals/statuses")
def signal_statuses() -> dict[str, Any]:
    """告警触发结果的状态口径。"""
    return {"statuses": SIGNAL_STATUSES}


@router.get("/decisions", response_model=PageResult[dict])
def list_decisions(
    rule_id: int | None = Query(default=None, description="按限速规则 ID 过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """限速决策历史：同一窗口同类调整只出现一次，版本号连续递增。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_decisions(entry_id=rule_id, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出动态限速清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "throttle", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已启用、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤动态限速列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条限速规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="限速规则已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条限速规则明细；最新速率与决策版本以决策记录为唯一来源。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"限速规则 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条限速规则执行提交规则评审、启用规则、关闭规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/signals/{signal_id}/confirm", response_model=ActionResult)
def confirm_signal(signal_id: int) -> ActionResult:
    """人工确认告警信号并写入限速决策；重复确认不会产生第二份决策。"""
    decision, message = service.confirm_signal(signal_id)
    if decision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=decision)
