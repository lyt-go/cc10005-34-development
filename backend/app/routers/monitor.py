"""采集监控接口：维护监控面板，接收采样并形成告警触发结果。

路由顺序：静态路径（/signals、/export）必须声明在 /{entry_id} 之前，否则会被当成
面板 ID 解析，导出与信号列表接口就会返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.monitor import MonitorService
from app.services.signals import SIGNAL_STATUSES

router = APIRouter(prefix="/api/monitor", tags=["采集监控"])

service = MonitorService()

LIST_FIELDS = ["面板编号", "监控指标集", "采样间隔秒", "趋势窗口分", "异常检测方法", "告警触发条件", "通知接收组", "自动降级开关"]
STATUSES = ["草稿", "面板评审中", "已启用", "已归档"]


@router.get("/signals", response_model=PageResult[dict])
def list_signals(
    panel_id: int | None = Query(default=None, description="按面板 ID 过滤"),
    status: str | None = Query(default=None, description="已观察、待确认、待复核、已确认、已合并、已忽略"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """告警触发结果列表：规则/面板关闭后历史记录仍保留，可按状态筛选待复核信号。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_signals(entry_id=panel_id, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/signals/statuses")
def signal_statuses() -> dict[str, Any]:
    """告警触发结果的状态口径，供前端筛选下拉使用。"""
    return {"statuses": SIGNAL_STATUSES}


@router.get("/decisions", response_model=PageResult[dict])
def list_decisions(
    panel_id: int | None = Query(default=None, description="按面板 ID 过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """限速决策列表：以决策记录为唯一来源，页面刷新与重新读取看到同一版本。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_decisions(entry_id=panel_id, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集监控清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "monitor", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按面板编号检索"),
    status: str | None = Query(default=None, description="草稿、面板评审中、已启用、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按面板编号与状态过滤采集监控列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条监控面板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="监控面板已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条监控面板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"监控面板 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/samples", response_model=ActionResult)
def submit_sample(entry_id: int, payload: EntryPayload) -> ActionResult:
    """上报一次采样：按指标集、采样间隔与错误率阈值形成告警触发结果。

    连续窗口满足条件后自动写入限速决策，并把决策同步回面板状态；慢采样与迟到窗口
    只留档，不覆盖较新的完整窗口。
    """
    entry, message, sample = service.submit_sample(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, sample=sample)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条监控面板执行提交面板评审、启用面板、归档面板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/signals/{signal_id}/confirm", response_model=ActionResult)
def confirm_signal(signal_id: int) -> ActionResult:
    """人工确认待确认/待复核的告警信号；同一信号重复确认返回同一份限速决策。"""
    decision, message = service.confirm_signal(signal_id)
    if decision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=decision)
