"""监控信号驱动的动态限速。

采集监控面板把一次采样换算成一个时间窗口，按「监控指标集 + 采样间隔 + 错误率阈值」
判断窗口是否成立；连续若干个完整窗口都超过阈值后形成告警触发结果。确认后的信号才
写入限速决策，并把结果同步回监控面板状态。

刻意的边界处理：
- 窗口时长小于采样间隔的 90% 视为不完整，进入待复核，不参与连续窗口计数；
- 采样延迟超过 2 个采样间隔视为慢采样，只记录，不覆盖较新的完整窗口；
- 窗口结束时间不晚于该面板最近一次已接受窗口的，视为迟到/重复窗口，直接忽略；
- 同一采样重复上报按幂等键命中，返回第一次的信号与决策，不产生第二份调整；
- 同一条限速规则在一个告警周期内只产生一次降速决策，后到的面板信号标记为已合并；
- 规则未启用、已关闭、面板未启用或配置缺失时，信号进入待复核，不修改限速规则；
- 告警信号与限速决策独立留存，规则关闭后历史记录仍可读取。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

MONITOR_MODULE = "monitor"
THROTTLE_MODULE = "throttle"
SIGNAL_MODULE = "monitor_signals"
SAMPLE_MODULE = "monitor_samples"
DECISION_MODULE = "throttle_decisions"

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_CONSECUTIVE_WINDOWS = 2
DEFAULT_RECOVERY_WINDOWS = 3
RECOVERY_THRESHOLD_RATIO = 0.5
MAX_SAMPLING_DELAY_FACTOR = 2.0
MIN_WINDOW_COVERAGE = 0.9
MIN_RATE = 1.0

SIGNAL_STATUSES = ["已观察", "待确认", "待复核", "已确认", "已合并", "已忽略"]

# FastAPI 的同步路由在线程池中执行，并发上报同一面板时用一把锁保证只调整一次。
_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _format_dt(_now())


def _format_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        digits = "".join(ch for ch in text if ch.isdigit() or ch in ".-+")
        try:
            return float(digits)
        except ValueError:
            return default


def _as_int(value: Any, default: int | None = None) -> int | None:
    number = _as_float(value)
    if number is None:
        return default
    return int(number)


def _fmt_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _next_code(rows: list[dict[str, Any]], prefix: str) -> str:
    biggest = 0
    for row in rows:
        code = str(row.get("信号编号") or row.get("决策编号") or row.get("采样编号") or "")
        if code.startswith(prefix + "-"):
            suffix = code[len(prefix) + 1:]
            if suffix.isdigit():
                biggest = max(biggest, int(suffix))
    return f"{prefix}-{biggest + 1:04d}"


def _find_panel(panel_id: int) -> dict[str, Any] | None:
    return store.find(MONITOR_MODULE, panel_id)


def get_panel(panel_id: int) -> dict[str, Any] | None:
    panel = _find_panel(panel_id)
    return sync_panel(panel) if panel is not None else None


def get_rule(rule_id: int) -> dict[str, Any] | None:
    rule = _find_rule_by_id(rule_id)
    return sync_rule(rule) if rule is not None else None


def _find_rule_by_id(rule_id: int) -> dict[str, Any] | None:
    return store.find(THROTTLE_MODULE, rule_id)


def _find_rule_by_code(code: str | None) -> dict[str, Any] | None:
    if not code:
        return None
    wanted = str(code).strip().upper()
    for row in store.rows(THROTTLE_MODULE):
        if str(row.get("规则编号", "")).strip().upper() == wanted:
            return row
    return None


def _rule_for_panel(panel: dict[str, Any]) -> dict[str, Any] | None:
    rule_id = panel.get("关联限速规则ID")
    if rule_id not in (None, ""):
        rule = _find_rule_by_id(int(rule_id))
        if rule is not None:
            return rule
    return _find_rule_by_code(panel.get("关联限速规则"))


def _panel_signals(panel_id: int) -> list[dict[str, Any]]:
    return [row for row in store.rows(SIGNAL_MODULE) if int(row.get("面板ID", 0)) == panel_id]


def _rule_decisions(rule_id: int) -> list[dict[str, Any]]:
    return [row for row in store.rows(DECISION_MODULE) if int(row.get("规则ID", 0)) == rule_id]


def _latest_decision(rule_id: int) -> dict[str, Any] | None:
    rows = _rule_decisions(rule_id)
    if not rows:
        return None
    return max(rows, key=lambda row: (int(row.get("版本", 0)), int(row.get("id", 0))))


# ---------------------------------------------------------------------------
# 采样窗口
# ---------------------------------------------------------------------------

def _accepted_samples(panel_id: int) -> list[dict[str, Any]]:
    rows = [
        row for row in store.rows(SAMPLE_MODULE)
        if int(row.get("面板ID", 0)) == panel_id and row.get("已接受")
    ]
    return sorted(rows, key=lambda row: (row.get("窗口结束时间", ""), int(row.get("id", 0))))


def _latest_accepted_sample(panel_id: int) -> dict[str, Any] | None:
    rows = _accepted_samples(panel_id)
    return rows[-1] if rows else None


def _window_is_contiguous(previous: dict[str, Any], current: dict[str, Any], interval: float) -> bool:
    previous_end = _as_dt(previous.get("窗口结束时间"))
    current_start = _as_dt(current.get("窗口开始时间"))
    current_end = _as_dt(current.get("窗口结束时间"))
    if previous_end is None or current_start is None or current_end is None:
        return False
    gap = (current_start - previous_end).total_seconds()
    # 相邻窗口首尾相接；允许最多半个采样间隔的抖动，超过就视为断档，连续计数重新开始。
    return -interval <= gap <= interval * 0.5 and current_end > previous_end


def _consecutive_streak(
    panel_id: int,
    interval: float,
    predicate: Any,
    after: datetime | None = None,
) -> int:
    """从最近一个已接受窗口往前数连续满足条件的窗口数。

    after 用于恢复判定：只统计上一次决策之后的窗口，避免恢复一次之后每个窗口都继续加速。
    """
    samples = _accepted_samples(panel_id)
    streak = 0
    newer: dict[str, Any] | None = None
    for sample in reversed(samples):
        window_end = _as_dt(sample.get("窗口结束时间"))
        if after is not None and (window_end is None or window_end <= after):
            break
        if not predicate(sample):
            break
        if newer is not None and not _window_is_contiguous(sample, newer, interval):
            break
        streak += 1
        newer = sample
    return streak


# ---------------------------------------------------------------------------
# 信号与决策记录
# ---------------------------------------------------------------------------

def _new_signal(
    *,
    panel: dict[str, Any] | None,
    sample: dict[str, Any] | None,
    rule: dict[str, Any] | None,
    status: str,
    result: str,
    reason: str,
    decision_type: str = "",
    error_rate: float | None = None,
    threshold: float | None = None,
    streak: int = 0,
) -> dict[str, Any]:
    rows = store.rows(SIGNAL_MODULE)
    signal = {
        "id": _next_id(rows),
        "信号编号": _next_code(rows, "SIG"),
        "面板ID": int(panel.get("id", 0)) if panel else 0,
        "面板编号": str(panel.get("面板编号", "")) if panel else "",
        "规则ID": int(rule.get("id", 0)) if rule else 0,
        "规则编号": str(rule.get("规则编号", "")) if rule else str((panel or {}).get("关联限速规则", "")),
        "监控指标集": str((sample or {}).get("监控指标集") or (panel or {}).get("监控指标集", "")),
        "采样间隔秒": int((sample or {}).get("采样间隔秒") or _as_int((panel or {}).get("采样间隔秒"), 0) or 0),
        "窗口开始时间": str((sample or {}).get("窗口开始时间", "")),
        "窗口结束时间": str((sample or {}).get("窗口结束时间", "")),
        "采样时间": str((sample or {}).get("采样时间", "")),
        "采样延迟秒": (sample or {}).get("采样延迟秒", 0),
        "采集请求数": (sample or {}).get("采集请求数", 0),
        "错误请求数": (sample or {}).get("错误请求数", 0),
        "错误率": round(error_rate, 2) if error_rate is not None else None,
        "错误率阈值": threshold,
        "连续窗口数": streak,
        "告警结果": result,
        "决策类型": decision_type,
        "状态": status,
        "决策ID": 0,
        "决策编号": "",
        "说明": reason,
        "创建时间": _now_text(),
    }
    rows.append(signal)
    return signal


def _update_panel_counters(panel: dict[str, Any]) -> None:
    signals = _panel_signals(int(panel.get("id", 0)))
    panel["待复核信号数"] = sum(1 for row in signals if row.get("状态") == "待复核")
    panel["待确认信号数"] = sum(1 for row in signals if row.get("状态") == "待确认")
    panel["已合并信号数"] = sum(1 for row in signals if row.get("状态") == "已合并")
    panel["告警历史数"] = sum(1 for row in signals if row.get("决策类型"))
    decisions = [
        row for row in store.rows(DECISION_MODULE)
        if int(row.get("面板ID", 0)) == int(panel.get("id", 0))
    ]
    panel["决策历史数"] = len(decisions)


def _update_rule_counters(rule: dict[str, Any]) -> None:
    rule_id = int(rule.get("id", 0))
    signals = [row for row in store.rows(SIGNAL_MODULE) if int(row.get("规则ID", 0)) == rule_id]
    rule["待复核信号数"] = sum(1 for row in signals if row.get("状态") == "待复核")
    rule["待确认信号数"] = sum(1 for row in signals if row.get("状态") == "待确认")
    rule["已合并信号数"] = sum(1 for row in signals if row.get("状态") == "已合并")
    rule["告警历史数"] = len(_rule_decisions(rule_id))


def _mark_needs_review(
    signal: dict[str, Any],
    panel: dict[str, Any] | None,
    reason: str,
) -> None:
    signal["状态"] = "待复核"
    signal["说明"] = reason
    if panel is not None:
        panel["面板告警状态"] = "待复核"
        panel["最新告警结果"] = reason
        panel["最新告警时间"] = signal["创建时间"]
        panel["最新信号编号"] = signal["信号编号"]
        _update_panel_counters(panel)


def _mark_observed(
    signal: dict[str, Any],
    panel: dict[str, Any],
    result: str,
    reason: str,
) -> None:
    signal["状态"] = "已观察"
    signal["告警结果"] = result
    signal["说明"] = reason
    # 未触发阈值不代表有告警；若面板此前在告警中，保持告警中直到恢复条件满足。
    if panel.get("面板告警状态") not in ("告警中", "待确认", "待复核"):
        panel["面板告警状态"] = "正常"
    panel["最新告警结果"] = result
    panel["最新告警时间"] = signal["创建时间"]
    panel["最新信号编号"] = signal["信号编号"]
    _update_panel_counters(panel)


def _merge_into_decision(
    signal: dict[str, Any],
    panel: dict[str, Any],
    rule: dict[str, Any],
    decision: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    signal["状态"] = "已合并"
    signal["决策ID"] = int(decision.get("id", 0))
    signal["决策编号"] = str(decision.get("决策编号", ""))
    signal["说明"] = reason
    if decision.get("决策类型") == "降速" or panel.get("面板告警状态") == "告警中":
        panel["面板告警状态"] = "告警中"
    panel["最新告警结果"] = reason
    panel["最新告警时间"] = signal["创建时间"]
    panel["最新信号编号"] = signal["信号编号"]
    panel["最新决策编号"] = decision.get("决策编号", "")
    panel["最新决策窗口结束"] = decision.get("窗口结束时间", "")
    _update_panel_counters(panel)
    _update_rule_counters(rule)
    return decision


# ---------------------------------------------------------------------------
# 采样上报
# ---------------------------------------------------------------------------

def ingest_sample(panel_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
    """接收一条采样并形成告警触发结果；返回 (面板, 说明, 采样记录)。"""
    with _LOCK:
        panel = _find_panel(panel_id)
        if panel is None:
            return None, f"监控面板 {panel_id} 不存在或已归档", None

        rule = _rule_for_panel(panel)
        interval = _as_float(panel.get("采样间隔秒"), DEFAULT_INTERVAL_SECONDS) or DEFAULT_INTERVAL_SECONDS
        if interval <= 0:
            interval = DEFAULT_INTERVAL_SECONDS

        start_text = str(values.get("窗口开始时间") or values.get("窗口开始") or "").strip()
        end_text = str(values.get("窗口结束时间") or values.get("窗口结束") or "").strip()
        metric_set = str(values.get("监控指标集") or panel.get("监控指标集") or "").strip()
        total = _as_int(values.get("采集请求数") if "采集请求数" in values else values.get("请求数"))
        errors = _as_int(values.get("错误请求数") if "错误请求数" in values else values.get("错误数"))
        sample_time = _as_dt(values.get("采样时间")) or _now()
        sample_code = str(values.get("采样编号") or "").strip()

        window_start = _as_dt(start_text)
        window_end = _as_dt(end_text)

        def failed(reason: str) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
            signal = _new_signal(
                panel=panel,
                sample={"窗口开始时间": start_text, "窗口结束时间": end_text, "采样时间": _format_dt(sample_time)},
                rule=rule,
                status="待复核",
                result="采样不合法",
                reason=reason,
            )
            _mark_needs_review(signal, panel, reason)
            return None, reason, None

        if window_start is None or window_end is None:
            return failed("窗口开始时间/结束时间为空或格式不可识别，信号进入待复核")
        if window_end <= window_start:
            return failed("采样窗口结束时间必须晚于开始时间，信号进入待复核")
        if total is None or total <= 0:
            return failed("采集请求数缺失或不大于 0，信号进入待复核")
        if errors is None or errors < 0 or errors > total:
            return failed("错误请求数缺失或不在 0 到采集请求数之间，信号进入待复核")
        if metric_set and str(panel.get("监控指标集") or "").strip() and metric_set != str(panel.get("监控指标集")).strip():
            return failed(
                f"采样指标集「{metric_set}」与面板指标集「{panel.get('监控指标集')}」不一致，信号进入待复核"
            )

        duration = (window_end - window_start).total_seconds()
        if duration < interval * MIN_WINDOW_COVERAGE:
            return failed(
                f"采样窗口 {duration:g}s 小于采样间隔 {interval:g}s 的 {int(MIN_WINDOW_COVERAGE * 100)}%，"
                "窗口不完整，信号进入待复核"
            )

        delay = max((sample_time - window_end).total_seconds(), 0.0)
        slow = delay > interval * MAX_SAMPLING_DELAY_FACTOR

        # 幂等：同一采样重复上报（页面重复提交、刷新后重放）只命中第一次的结果。
        idempotency_key = "|".join([
            f"panel:{panel_id}",
            sample_code or "|".join([
                _format_dt(window_start), _format_dt(window_end),
                str(total), str(errors), _format_dt(sample_time),
            ]),
        ])
        for existing in store.rows(SAMPLE_MODULE):
            if existing.get("幂等键") == idempotency_key:
                signal = store.find(SIGNAL_MODULE, int(existing.get("信号ID", 0)))
                if signal is not None:
                    return panel, "同一采样已处理，沿用原告警结果与限速决策，未重复调整", existing

        latest = _latest_accepted_sample(panel_id)
        latest_end = _as_dt(latest.get("窗口结束时间")) if latest else None

        error_rate = errors / total * 100.0
        threshold = _as_float(panel.get("错误率阈值"))
        if threshold is None and rule is not None:
            threshold = _as_float(rule.get("错误率阈值"))
        if threshold is None:
            return failed("面板与关联限速规则都没有可用的错误率阈值，信号进入待复核")

        sample_row = {
            "id": _next_id(store.rows(SAMPLE_MODULE)),
            "采样编号": sample_code or _next_code(store.rows(SAMPLE_MODULE), "SAMP"),
            "幂等键": idempotency_key,
            "面板ID": panel_id,
            "面板编号": str(panel.get("面板编号", "")),
            "规则ID": int(rule.get("id", 0)) if rule else 0,
            "规则编号": str(rule.get("规则编号", "")) if rule else "",
            "监控指标集": metric_set,
            "采样间隔秒": int(interval),
            "窗口开始时间": _format_dt(window_start),
            "窗口结束时间": _format_dt(window_end),
            "采样时间": _format_dt(sample_time),
            "采样延迟秒": round(delay, 1),
            "采集请求数": total,
            "错误请求数": errors,
            "错误率": round(error_rate, 2),
            "慢采样": slow,
            "已接受": False,
            "创建时间": _now_text(),
        }
        store.rows(SAMPLE_MODULE).append(sample_row)

        # 迟到/重复窗口：不覆盖面板当前窗口与较新的限速决策。
        if latest_end is not None and window_end <= latest_end:
            result = "迟到信号，未覆盖较新时间窗口"
            if slow:
                result = "慢采样且迟到，未覆盖较新时间窗口"
            signal = _new_signal(
                panel=panel,
                sample=sample_row,
                rule=rule,
                status="已忽略",
                result=result,
                reason=f"窗口结束时间 {_format_dt(window_end)} 不晚于最近完整窗口 {_format_dt(latest_end)}，只留档不调整",
                error_rate=error_rate,
                threshold=threshold,
            )
            sample_row["信号ID"] = signal["id"]
            _update_panel_counters(panel)
            return panel, result, sample_row

        sample_row["已接受"] = True

        consecutive_required = _as_int(panel.get("连续窗口数"), DEFAULT_CONSECUTIVE_WINDOWS) or DEFAULT_CONSECUTIVE_WINDOWS
        recovery_required = _as_int(panel.get("恢复窗口数"), DEFAULT_RECOVERY_WINDOWS) or DEFAULT_RECOVERY_WINDOWS
        consecutive_required = max(consecutive_required, 1)
        recovery_required = max(recovery_required, 1)

        high_streak = _consecutive_streak(panel_id, interval, lambda row: float(row.get("错误率", 0)) > threshold)
        recovery_threshold = threshold * RECOVERY_THRESHOLD_RATIO
        # 恢复只从上一次决策之后开始数，避免一次恢复之后每个窗口都继续加速。
        last_decision_window = None
        rule_for_decision = rule
        if rule_for_decision is not None:
            latest_for_rule = _latest_decision(int(rule_for_decision.get("id", 0)))
            if latest_for_rule is not None:
                last_decision_window = _as_dt(latest_for_rule.get("窗口结束时间"))
        recovery_streak = _consecutive_streak(
            panel_id,
            interval,
            lambda row: float(row.get("错误率", 0)) <= recovery_threshold,
            after=last_decision_window,
        )
        # 恢复判定既要够连续窗口数，也要规则确实处在降速/恢复状态，避免正常速率下反复加速。
        rule_snapshot = sync_rule(rule) if rule is not None else None
        rule_state = str((rule_snapshot or {}).get("限速状态", "正常"))
        rule_rate = _as_float((rule_snapshot or {}).get("当前速率"), _as_float((rule_snapshot or {}).get("初始速率"), 0.0))
        initial_rate = _as_float((rule_snapshot or {}).get("初始速率"), rule_rate or 0.0) or 0.0
        should_recover = (
            recovery_streak >= recovery_required
            and rule_state in ("降速中", "恢复中")
            and rule_rate < initial_rate - 1e-9
        )

        panel["当前采样窗口"] = f"{sample_row['窗口开始时间']} ~ {sample_row['窗口结束时间']}"
        panel["当前错误率"] = sample_row["错误率"]

        if high_streak >= consecutive_required:
            decision_type = "降速"
            result = f"错误率 {error_rate:.2f}% 超过阈值 {_fmt_number(threshold)}%，连续 {high_streak} 个完整窗口，触发降速"
        elif should_recover:
            decision_type = "恢复"
            result = (
                f"错误率 {error_rate:.2f}% 连续 {recovery_streak} 个窗口不高于恢复线 "
                f"{_fmt_number(recovery_threshold)}%，触发恢复"
            )
        else:
            decision_type = ""
            if error_rate > threshold:
                result = (
                    f"错误率 {error_rate:.2f}% 超过阈值 {_fmt_number(threshold)}%，"
                    f"连续窗口 {high_streak}/{consecutive_required}，暂不触发"
                )
            else:
                result = f"错误率 {error_rate:.2f}% 未超过阈值 {_fmt_number(threshold)}%"

        # 面板未启用、规则未启用/已关闭时，告警条件即使满足也无法执行，直接记待复核。
        blocked_reason = ""
        if str(panel.get("status")) != "已启用":
            blocked_reason = f"监控面板当前为「{panel.get('status')}」，未启用，告警信号进入待复核"
        elif rule is None:
            blocked_reason = "面板未关联到可用的限速规则，告警信号进入待复核"
        elif str(rule.get("status")) != "已启用":
            blocked_reason = (
                f"限速规则 {rule.get('规则编号')} 当前为「{rule.get('status')}」，"
                "未启用或已关闭，告警信号进入待复核"
            )

        if blocked_reason and decision_type:
            signal = _new_signal(
                panel=panel,
                sample=sample_row,
                rule=rule,
                status="待复核",
                result=result,
                reason=blocked_reason,
                decision_type=decision_type,
                error_rate=error_rate,
                threshold=threshold,
                streak=high_streak if decision_type == "降速" else recovery_streak,
            )
            sample_row["信号ID"] = signal["id"]
            _mark_needs_review(signal, panel, blocked_reason)
            return panel, blocked_reason, sample_row

        signal = _new_signal(
            panel=panel,
            sample=sample_row,
            rule=rule,
            status="待确认" if decision_type else "已观察",
            result=result,
            reason=result,
            decision_type=decision_type,
            error_rate=error_rate,
            threshold=threshold,
            streak=high_streak if decision_type == "降速" else recovery_streak,
        )
        sample_row["信号ID"] = signal["id"]
        panel["最新信号编号"] = signal["信号编号"]

        if decision_type == "":
            _mark_observed(signal, panel, result, result)
            return panel, result, sample_row

        decision, message = _apply_signal(signal, panel, rule, manual=False)
        return panel, message, sample_row


# ---------------------------------------------------------------------------
# 确认信号并写入限速决策
# ---------------------------------------------------------------------------

def _apply_signal(
    signal: dict[str, Any],
    panel: dict[str, Any] | None,
    rule: dict[str, Any] | None,
    *,
    manual: bool,
) -> tuple[dict[str, Any] | None, str]:
    if panel is None:
        _mark_needs_review(signal, None, "信号没有关联的监控面板，进入待复核")
        return None, signal["说明"]
    if rule is None:
        _mark_needs_review(signal, panel, "未关联到可用的限速规则，信号进入待复核")
        return None, signal["说明"]
    if str(panel.get("status")) != "已启用":
        _mark_needs_review(signal, panel, f"监控面板当前为「{panel.get('status')}」，未启用，信号进入待复核")
        return None, signal["说明"]
    if str(rule.get("status")) != "已启用":
        state = str(rule.get("status"))
        _mark_needs_review(
            signal, panel,
            f"限速规则 {rule.get('规则编号')} 当前为「{state}」，未启用或已关闭，信号进入待复核",
        )
        return None, signal["说明"]
    if not manual and str(panel.get("自动降级开关") or "开启") != "开启":
        signal["状态"] = "待确认"
        signal["说明"] = "面板未开启自动降级，信号等待人工确认"
        panel["面板告警状态"] = "待确认"
        panel["最新告警结果"] = signal["说明"]
        panel["最新告警时间"] = signal["创建时间"]
        panel["最新信号编号"] = signal["信号编号"]
        _update_panel_counters(panel)
        _update_rule_counters(rule)
        return None, signal["说明"]

    decision_type = str(signal.get("决策类型") or "")
    if decision_type not in ("降速", "恢复"):
        signal["状态"] = "已观察"
        return None, "信号没有需要执行的限速动作"

    rule_id = int(rule.get("id", 0))
    rule_state = str(rule.get("限速状态") or "正常")
    window_end = str(signal.get("窗口结束时间", ""))

    # 同一窗口已经产生过同类决策：直接合并，保证重复/并发的面板信号只调整一次。
    for decision in _rule_decisions(rule_id):
        if decision.get("窗口结束时间") == window_end and decision.get("决策类型") == decision_type:
            existing_signal_id = int(decision.get("信号ID", 0))
            existing_signal = store.find(SIGNAL_MODULE, existing_signal_id)
            same_panel = existing_signal is not None and int(existing_signal.get("面板ID", 0)) == int(panel.get("id", 0))
            if not manual and same_panel:
                # 本面板同一窗口的重复确认：沿用已有决策，不新增信号状态。
                signal["状态"] = "已确认"
                signal["决策ID"] = int(decision.get("id", 0))
                signal["决策编号"] = str(decision.get("决策编号", ""))
                signal["说明"] = f"本面板同一窗口已确认过，沿用决策 {decision.get('决策编号')}"
                return decision, signal["说明"]
            message = f"同一窗口已由决策 {decision.get('决策编号')} 完成{decision_type}，本次信号合并，不重复调整"
            _merge_into_decision(signal, panel, rule, decision, message)
            return decision, message

    # 一个告警周期内只允许一次降速：其他面板同时触发时合并到当前决策。
    if decision_type == "降速" and rule_state == "降速中":
        current = _latest_decision(rule_id)
        if current is not None and current.get("决策类型") == "降速":
            message = f"限速规则已处于告警周期，决策 {current.get('决策编号')} 已生效，本次信号合并"
            _merge_into_decision(signal, panel, rule, current, message)
            return current, message

    if decision_type == "恢复" and rule_state == "正常":
        signal["状态"] = "已合并"
        current = _latest_decision(rule_id)
        signal["决策ID"] = int(current.get("id", 0)) if current else 0
        signal["决策编号"] = str(current.get("决策编号", "")) if current else ""
        signal["说明"] = "限速规则已处于正常速率，本次恢复信号无需再调整"
        panel["面板告警状态"] = "正常"
        panel["最新告警结果"] = signal["说明"]
        panel["最新告警时间"] = signal["创建时间"]
        panel["最新信号编号"] = signal["信号编号"]
        _update_panel_counters(panel)
        _update_rule_counters(rule)
        return current, signal["说明"]

    initial_rate = _as_float(rule.get("初始速率"), 100.0) or 100.0
    maximum_rate = _as_float(rule.get("最大速率"), max(initial_rate, 1.0)) or max(initial_rate, 1.0)
    current_rate = _as_float(rule.get("当前速率"), initial_rate) or initial_rate
    slow_factor = _as_float(rule.get("减速因子"), 0.5) or 0.5
    fast_factor = _as_float(rule.get("加速因子"), 1.2) or 1.2
    if not (0 < slow_factor <= 1):
        slow_factor = 0.5
    if fast_factor <= 1:
        fast_factor = 1.2

    if decision_type == "降速":
        next_rate = max(MIN_RATE, current_rate * slow_factor)
        if next_rate >= current_rate:
            next_rate = max(MIN_RATE, current_rate - 1)
        next_state = "降速中"
    else:
        # 恢复一次只走一档回满，分多次恢复，避免单个窗口把速率直接推回初始值。
        next_rate = min(initial_rate, current_rate * fast_factor)
        next_rate = min(maximum_rate, next_rate)
        next_state = "正常" if next_rate >= initial_rate - 1e-9 else "恢复中"

    version = max((int(row.get("版本", 0)) for row in _rule_decisions(rule_id)), default=0) + 1
    decision_rows = store.rows(DECISION_MODULE)
    decision = {
        "id": _next_id(decision_rows),
        "决策编号": _next_code(decision_rows, "DECI"),
        "规则ID": rule_id,
        "规则编号": str(rule.get("规则编号", "")),
        "面板ID": int(panel.get("id", 0)),
        "面板编号": str(panel.get("面板编号", "")),
        "信号ID": int(signal.get("id", 0)),
        "信号编号": str(signal.get("信号编号", "")),
        "合并信号编号": [],
        "决策类型": decision_type,
        "窗口开始时间": signal.get("窗口开始时间", ""),
        "窗口结束时间": window_end,
        "错误率": signal.get("错误率"),
        "错误率阈值": signal.get("错误率阈值"),
        "连续窗口数": signal.get("连续窗口数"),
        "决策前速率": _fmt_number(current_rate),
        "决策后速率": _fmt_number(next_rate),
        "限速状态": next_state,
        "版本": version,
        "状态": "已生效",
        "说明": (
            f"{signal.get('面板编号')} 的告警信号 {signal.get('信号编号')} 确认后执行{decision_type}："
            f"{_fmt_number(current_rate)} → {_fmt_number(next_rate)}"
        ),
        "创建时间": _now_text(),
    }
    decision_rows.append(decision)

    rule["当前速率"] = decision["决策后速率"]
    rule["限速状态"] = next_state
    rule["最新限速决策"] = decision["决策编号"]
    rule["最新决策窗口结束"] = window_end
    rule["决策版本"] = version
    rule["abnormal"] = next_state != "正常"
    _update_rule_counters(rule)

    signal["状态"] = "已确认"
    signal["决策ID"] = decision["id"]
    signal["决策编号"] = decision["决策编号"]
    signal["说明"] = decision["说明"]
    panel["面板告警状态"] = "告警中" if next_state in ("降速中", "恢复中") else "正常"
    panel["最新告警结果"] = signal["告警结果"]
    panel["最新告警时间"] = signal["创建时间"]
    panel["最新信号编号"] = signal["信号编号"]
    panel["最新决策编号"] = decision["决策编号"]
    panel["最新决策窗口结束"] = window_end
    _update_panel_counters(panel)
    return decision, decision["说明"]


def confirm_signal(signal_id: int) -> tuple[dict[str, Any] | None, str]:
    """人工确认待确认/待复核信号；幂等，已确认的信号返回原决策。"""
    with _LOCK:
        signal = store.find(SIGNAL_MODULE, signal_id)
        if signal is None:
            return None, f"告警信号 {signal_id} 不存在"
        if signal.get("状态") == "已确认":
            decision = store.find(DECISION_MODULE, int(signal.get("决策ID", 0)))
            return decision, f"信号 {signal.get('信号编号')} 已确认，沿用决策 {signal.get('决策编号')}"
        if signal.get("状态") in ("已忽略", "已观察"):
            return None, f"信号 {signal.get('信号编号')} 为「{signal.get('状态')}」，无需确认"
        panel = _find_panel(int(signal.get("面板ID", 0))) if signal.get("面板ID") else None
        rule = _find_rule_by_id(int(signal.get("规则ID", 0))) if signal.get("规则ID") else None
        if rule is None:
            rule = _find_rule_by_code(str(signal.get("规则编号") or ""))
        decision, message = _apply_signal(signal, panel, rule, manual=True)
        return decision, message


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def list_signals(
    *,
    panel_id: int | None = None,
    rule_id: int | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    rows = list(store.rows(SIGNAL_MODULE))
    if panel_id is not None:
        rows = [row for row in rows if int(row.get("面板ID", 0)) == panel_id]
    if rule_id is not None:
        rows = [row for row in rows if int(row.get("规则ID", 0)) == rule_id]
    if status:
        rows = [row for row in rows if row.get("状态") == status]
    rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
    total = len(rows)
    start = max(page - 1, 0) * size
    return rows[start:start + size], total


def list_decisions(
    *,
    rule_id: int | None = None,
    panel_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    rows = list(store.rows(DECISION_MODULE))
    if rule_id is not None:
        rows = [row for row in rows if int(row.get("规则ID", 0)) == rule_id]
    if panel_id is not None:
        rows = [row for row in rows if int(row.get("面板ID", 0)) == panel_id]
    rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
    total = len(rows)
    start = max(page - 1, 0) * size
    return rows[start:start + size], total


def sync_panel(panel: dict[str, Any]) -> dict[str, Any]:
    """读取面板时从信号表回算计数，避免页面与接口出现两份不一致的状态。"""
    with _LOCK:
        _update_panel_counters(panel)
        rule = _rule_for_panel(panel)
        if rule is not None:
            panel["关联限速规则ID"] = int(rule.get("id", 0))
            panel["关联限速规则"] = str(rule.get("规则编号", ""))
            panel["关联规则状态"] = str(rule.get("status", ""))
            panel["关联规则当前速率"] = rule.get("当前速率", "")
            panel["关联规则限速状态"] = rule.get("限速状态", "")
            panel["关联规则最新决策"] = rule.get("最新限速决策", "")
        return panel


def sync_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """限速规则的最新决策以决策表为唯一来源，读取时回填镜像字段。

    规则关闭后不再跟随历史决策的限速状态，而是固定为「已关闭」，避免关闭又被旧决策
    回填成「降速中」；历史决策仍然保留在决策表里可查。
    """
    with _LOCK:
        latest = _latest_decision(int(rule.get("id", 0)))
        if latest is not None:
            rule["当前速率"] = latest.get("决策后速率", rule.get("当前速率", ""))
            rule["最新限速决策"] = latest.get("决策编号", "")
            rule["最新决策窗口结束"] = latest.get("窗口结束时间", "")
            rule["决策版本"] = int(latest.get("版本", 0))
            if str(rule.get("status")) != "已关闭":
                rule["限速状态"] = latest.get("限速状态", rule.get("限速状态", "正常"))
        if str(rule.get("status")) == "已关闭":
            rule["限速状态"] = "已关闭"
            rule["abnormal"] = False
        _update_rule_counters(rule)
        return rule
