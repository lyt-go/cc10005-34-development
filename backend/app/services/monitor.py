"""采集监控业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services import signals

MODULE = "monitor"
REQUIRED_FIELDS = ["面板编号", "监控指标集", "采样间隔秒"]
STATUS_ORDER = ["草稿", "面板评审中", "已启用", "已归档"]
ACTION_RULES = {"提交面板评审": "面板评审中", "启用面板": "已启用", "归档面板": "已归档"}
NEGATIVE_ACTIONS = []


class MonitorService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("面板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        for row in page_rows:
            signals.sync_panel(row)
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return signals.get_panel(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 告警阈值与连续窗口数允许登记时一并给出，缺省用面板指标的默认口径。
        for field in ("关联限速规则", "错误率阈值", "连续窗口数", "恢复窗口数", "自动降级开关"):
            if str(values.get(field) or "").strip():
                entry[field] = values.get(field)
        entry.setdefault("自动降级开关", "开启")
        entry["面板告警状态"] = "正常"
        entry["最新告警结果"] = ""
        entry["最新告警时间"] = ""
        entry["当前采样窗口"] = ""
        entry["待复核信号数"] = 0
        entry["已合并信号数"] = 0
        entry["告警历史数"] = 0
        entry["决策历史数"] = 0
        rows.append(entry)
        signals.sync_panel(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"监控面板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集监控可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        signals.sync_panel(entry)
        return entry, f"监控面板已{action}"

    def submit_sample(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """接收面板采样，形成告警触发结果并驱动限速决策；采样不合法时说明原因。"""
        return signals.ingest_sample(entry_id, values)

    def list_signals(
        self,
        *,
        entry_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return signals.list_signals(panel_id=entry_id, status=status, page=page, size=size)

    def list_decisions(
        self, *, entry_id: int | None = None, page: int = 1, size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        return signals.list_decisions(panel_id=entry_id, page=page, size=size)

    def confirm_signal(self, signal_id: int) -> tuple[dict[str, Any] | None, str]:
        """人工确认告警信号；已确认的信号返回同一份限速决策。"""
        return signals.confirm_signal(signal_id)
