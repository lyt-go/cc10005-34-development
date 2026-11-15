"""动态限速业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services import signals

MODULE = "throttle"
REQUIRED_FIELDS = ["规则编号", "目标域组", "初始速率"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已关闭"]
ACTION_RULES = {"提交规则评审": "规则评审中", "启用规则": "已启用", "关闭规则": "已关闭"}
NEGATIVE_ACTIONS = []


class ThrottleService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        for row in page_rows:
            signals.sync_rule(row)
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return signals.get_rule(entry_id)

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
        for field in ("最大速率", "加速因子", "减速因子", "错误率阈值", "降速恢复间隔"):
            if str(values.get(field) or "").strip():
                entry[field] = values.get(field)
        entry["当前速率"] = entry.get("初始速率")
        entry["限速状态"] = "正常"
        entry["最新限速决策"] = ""
        entry["最新决策窗口结束"] = ""
        entry["决策版本"] = 0
        entry["待复核信号数"] = 0
        entry["告警历史数"] = 0
        rows.append(entry)
        signals.sync_rule(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"限速规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于动态限速可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if target == "已关闭":
            # 关闭规则只停用后续调整，历史告警与决策保留可查。
            entry["限速状态"] = "已关闭"
        signals.sync_rule(entry)
        return entry, f"限速规则已{action}"

    def confirm_signal(self, signal_id: int) -> tuple[dict[str, Any] | None, str]:
        return signals.confirm_signal(signal_id)

    def list_signals(
        self,
        *,
        entry_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return signals.list_signals(rule_id=entry_id, status=status, page=page, size=size)

    def list_decisions(
        self, *, entry_id: int | None = None, page: int = 1, size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        return signals.list_decisions(rule_id=entry_id, page=page, size=size)
