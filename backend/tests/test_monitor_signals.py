"""监控信号驱动限速的回归测试。

只依赖标准库，直接在进程内调用业务服务，避免引入测试框架依赖：
    cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services import signals
from app.store import store


def _reset() -> None:
    """每个用例开始前清空运行期记录并把种子行恢复成初始状态。"""
    for module in ("monitor_signals", "monitor_samples", "throttle_decisions"):
        store.rows(module).clear()
    for module in ("monitor", "throttle"):
        for row in store.rows(module):
            row.pop("当前速率", None)
            row.pop("限速状态", None)
            row.pop("最新限速决策", None)
            row.pop("最新决策窗口结束", None)
            row.pop("决策版本", None)
            row.pop("面板告警状态", None)
            row.pop("最新告警结果", None)
            row.pop("最新告警时间", None)
            row.pop("最新信号编号", None)
            row.pop("最新决策编号", None)
            row.pop("当前采样窗口", None)
            row.pop("当前错误率", None)
            row.pop("待复核信号数", None)
            row.pop("待确认信号数", None)
            row.pop("已合并信号数", None)
            row.pop("告警历史数", None)
            row.pop("决策历史数", None)
    throttle = store.find("throttle", 2)
    throttle["status"] = "已启用"
    throttle["当前速率"] = "120"
    throttle["限速状态"] = "正常"
    throttle["最新限速决策"] = ""
    throttle["最新决策窗口结束"] = ""
    throttle["决策版本"] = 0
    panel = store.find("monitor", 1)
    panel["status"] = "草稿"
    panel["自动降级开关"] = "关闭"
    panel2 = store.find("monitor", 2)
    panel2["status"] = "已启用"
    panel2["自动降级开关"] = "开启"
    panel2["面板告警状态"] = "正常"


def _sample(panel_id: int, hhmm: str, total: int, errors: int, sampled_hhmm: str = "", interval_minutes: int = 1):
    start_h, start_m = hhmm.split(":")
    end_minutes = int(start_m) + interval_minutes
    end_h = int(start_h) + end_minutes // 60
    end_m = end_minutes % 60
    start = f"2026-11-15T{start_h}:{start_m}:00Z"
    end = f"2026-11-15T{end_h:02d}:{end_m:02d}:00Z"
    sampled = sampled_hhmm or f"2026-11-15T{end_h:02d}:{end_m:02d}:05Z"
    return signals.ingest_sample(panel_id, {
        "窗口开始时间": start,
        "窗口结束时间": end,
        "采样时间": f"2026-11-15T{sampled}:00Z",
        "采集请求数": total,
        "错误请求数": errors,
    })


class SignalThrottleTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_consecutive_windows_trigger_slowdown_once(self) -> None:
        _, message, _ = _sample(2, "00:00", 100, 10)
        self.assertIn("连续窗口 1/2", message)
        rule = signals.get_rule(2)
        self.assertEqual(rule["决策版本"], 0)

        _, message, _ = _sample(2, "00:01", 100, 12)
        self.assertIn("降速", message)
        rule = signals.get_rule(2)
        self.assertEqual(rule["当前速率"], "60")
        self.assertEqual(rule["限速状态"], "降速中")
        self.assertEqual(rule["决策版本"], 1)
        self.assertEqual(len(signals.list_decisions(rule_id=2)[0]), 1)

    def test_second_panel_merges_into_existing_decision(self) -> None:
        _sample(2, "00:00", 100, 10)
        _sample(2, "00:01", 100, 12)
        _sample(3, "00:00", 100, 9)
        _, message, _ = _sample(3, "00:01", 100, 11)
        self.assertIn("合并", message)
        self.assertEqual(len(signals.list_decisions(rule_id=2)[0]), 1)
        self.assertEqual(signals.get_rule(2)["当前速率"], "60")
        merged = [row for row in signals.list_signals(rule_id=2)[0] if row["状态"] == "已合并"]
        self.assertEqual(len(merged), 1)

    def test_replayed_sample_is_idempotent(self) -> None:
        _sample(2, "00:00", 100, 10)
        _sample(2, "00:01", 100, 12)
        _, message, _ = _sample(2, "00:01", 100, 12)
        self.assertIn("未重复调整", message)
        self.assertEqual(len(signals.list_decisions(rule_id=2)[0]), 1)

    def test_late_and_slow_samples_do_not_override_newer_window(self) -> None:
        _sample(2, "00:00", 100, 10)
        _sample(2, "00:01", 100, 12)
        _, message, _ = _sample(2, "00:00", 100, 99, sampled_hhmm="00:20")
        self.assertIn("迟到", message)
        rule = signals.get_rule(2)
        self.assertEqual(rule["决策版本"], 1)
        self.assertEqual(rule["当前速率"], "60")
        panel = signals.get_panel(2)
        self.assertIn("00:02:00", panel["当前采样窗口"])

    def test_incomplete_window_goes_to_review(self) -> None:
        _, message, _ = signals.ingest_sample(2, {
            "窗口开始时间": "2026-11-15T00:05:00Z",
            "窗口结束时间": "2026-11-15T00:05:20Z",
            "采样时间": "2026-11-15T00:05:25Z",
            "采集请求数": 10,
            "错误请求数": 9,
        })
        self.assertIn("不完整", message)
        panel = signals.get_panel(2)
        self.assertEqual(panel["待复核信号数"], 1)
        self.assertEqual(panel["面板告警状态"], "待复核")
        self.assertEqual(signals.get_rule(2)["决策版本"], 0)

    def test_invalid_metric_set_goes_to_review(self) -> None:
        _, message, _ = signals.ingest_sample(2, {
            "窗口开始时间": "2026-11-15T00:05:00Z",
            "窗口结束时间": "2026-11-15T00:06:00Z",
            "采样时间": "2026-11-15T00:06:05Z",
            "采集请求数": 100,
            "错误请求数": 10,
            "监控指标集": "别的指标集",
        })
        self.assertIn("指标集", message)
        self.assertEqual(signals.get_panel(2)["待复核信号数"], 1)

    def test_gap_breaks_consecutive_windows(self) -> None:
        _sample(2, "00:00", 100, 20)
        _, message, _ = _sample(2, "00:02", 100, 20)
        self.assertIn("连续窗口 1/2", message)
        self.assertEqual(signals.get_rule(2)["决策版本"], 0)

    def test_closed_rule_keeps_history_and_blocks_new_decisions(self) -> None:
        _sample(2, "00:00", 100, 10)
        _sample(2, "00:01", 100, 12)
        decisions_before = len(signals.list_decisions(rule_id=2)[0])
        signals_rule = store.find("throttle", 2)
        signals_rule["status"] = "已关闭"
        signals.sync_rule(signals_rule)
        self.assertEqual(signals_rule["限速状态"], "已关闭")

        _sample(2, "00:10", 100, 30)
        _, message, _ = _sample(2, "00:11", 100, 30)
        self.assertIn("待复核", message)
        self.assertEqual(len(signals.list_decisions(rule_id=2)[0]), decisions_before)
        self.assertGreaterEqual(len(signals.list_signals(rule_id=2)[0]), 4)
        self.assertEqual(signals.get_rule(2)["当前速率"], "60")

    def test_disabled_panel_signal_goes_to_review(self) -> None:
        _sample(1, "00:00", 100, 40)
        _, message, _ = _sample(1, "00:01", 100, 40)
        self.assertIn("待复核", message)
        rows = signals.list_signals(panel_id=1)[0]
        self.assertTrue(all(row["决策编号"] == "" for row in rows))
        self.assertEqual(signals.get_rule(2)["决策版本"], 0)

    def test_recovery_needs_consecutive_quiet_windows(self) -> None:
        _sample(2, "00:00", 100, 10)
        _sample(2, "00:01", 100, 12)
        self.assertEqual(signals.get_rule(2)["当前速率"], "60")

        _, message, _ = _sample(2, "00:02", 100, 1)
        self.assertNotIn("恢复", message)
        _, message, _ = _sample(2, "00:03", 100, 1)
        self.assertNotIn("恢复", message)
        _, message, _ = _sample(2, "00:04", 100, 1)
        self.assertIn("恢复", message)
        rule = signals.get_rule(2)
        self.assertEqual(rule["当前速率"], "75")
        self.assertEqual(rule["限速状态"], "恢复中")

    def test_panel_and_rule_read_same_decision(self) -> None:
        _sample(2, "00:00", 100, 10)
        _sample(2, "00:01", 100, 12)
        rule = signals.get_rule(2)
        panel = signals.get_panel(2)
        self.assertEqual(panel["关联规则当前速率"], rule["当前速率"])
        self.assertEqual(panel["关联规则最新决策"], rule["最新限速决策"])
        self.assertEqual(signals.get_rule(2), rule)

    def test_manual_confirm_is_idempotent(self) -> None:
        # 关闭自动降级后，满足阈值的信号停在待确认，人工确认才写入决策。
        store.find("monitor", 2)["自动降级开关"] = "关闭"
        _sample(2, "00:00", 100, 10)
        _sample(2, "00:01", 100, 12)
        signal = signals.list_signals(panel_id=2)[0][0]
        self.assertEqual(signal["状态"], "待确认")
        self.assertEqual(signals.get_rule(2)["决策版本"], 0)

        decision_first, message_first = signals.confirm_signal(int(signal["id"]))
        decision_second, message_second = signals.confirm_signal(int(signal["id"]))
        self.assertIsNotNone(decision_first)
        self.assertEqual(decision_first["决策编号"], decision_second["决策编号"])
        self.assertIn("已确认", message_second)
        self.assertEqual(len(signals.list_decisions(rule_id=2)[0]), 1)


if __name__ == "__main__":
    unittest.main()
