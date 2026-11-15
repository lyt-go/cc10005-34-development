import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/throttle'
const columns = [
  "规则编号",
  "目标域组",
  "初始速率",
  "最大速率",
  "加速因子",
  "减速因子",
  "错误率阈值",
  "降速恢复间隔"
]
const stateColumns = [
  "当前速率",
  "限速状态",
  "最新限速决策",
  "决策版本",
  "待复核信号数",
  "告警历史数"
]
const actions = ["提交规则评审", "启用规则", "关闭规则"]
const signalStatuses = ["已观察", "待确认", "待复核", "已确认", "已合并", "已忽略"]

type Row = Record<string, string | number | boolean | null>
type Signal = Record<string, string | number | null>
type Decision = Record<string, string | number | null>

export default function ThrottleView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Row | null>(null)
  const [signals, setSignals] = useState<Signal[]>([])
  const [decisions, setDecisions] = useState<Decision[]>([])
  const [signalFilter, setSignalFilter] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('限速规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '动态限速列表读取失败')
    }
  }, [filters])

  const loadDetail = useCallback(async (row: Row) => {
    setSelected(row)
    setErrorMessage('')
    const statusQuery = signalFilter ? `&status=${encodeURIComponent(signalFilter)}` : ''
    try {
      const [signalResp, decisionResp, ruleResp] = await Promise.all([
        request(`${ENDPOINT}/signals?rule_id=${row.id}${statusQuery}`),
        request(`${ENDPOINT}/decisions?rule_id=${row.id}`),
        request(`${ENDPOINT}/${row.id}`),
      ])
      if (!signalResp.ok || !decisionResp.ok || !ruleResp.ok) {
        throw new Error('告警信号与限速决策读取失败')
      }
      const signalPayload = await signalResp.json()
      const decisionPayload = await decisionResp.json()
      const rulePayload = await ruleResp.json()
      setSignals(signalPayload.items ?? [])
      setDecisions(decisionPayload.items ?? [])
      setSelected(rulePayload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '告警信号与限速决策读取失败')
    }
  }, [signalFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (selected) {
      void loadDetail(selected)
    }
    // 只在筛选条件下拉变化时重新拉取详情，避免 selected 更新造成循环。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signalFilter])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('动态限速动作未生效，请稍后重试')
      }
      const payload = await response.json()
      setNotice(payload.message ?? '动作已执行')
      await reload()
      if (selected && selected.id === row.id) {
        await loadDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '动态限速操作失败')
    }
  }

  async function confirmSignal(signal: Signal) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/signals/${signal.id}/confirm`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '信号确认未生效')
      }
      setNotice(payload.message)
      await reload()
      if (selected) {
        await loadDetail(selected)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '信号确认失败')
    }
  }

  return (
    <section className="page" data-module="throttle">
      <header className="page-head">
        <div>
          <h2>动态限速管理</h2>
          <p className="page-desc">管理动态限速规则，根据目标站点响应自动调整请求速率，避免触发反爬机制。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('限速规则登记入口尚未接入审批流')}>
            登记限速规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出动态限速清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">限速规则总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">已启用规则</span>
          <strong className="stat-value">{rows.filter((row) => row.status === '已启用').length}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">降速中规则</span>
          <strong className="stat-value">{rows.filter((row) => row["限速状态"] === '降速中').length}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">待复核信号</span>
          <strong className="stat-value">{rows.reduce((sum, row) => sum + Number(row["待复核信号数"] ?? 0), 0)}</strong>
        </article>
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            {stateColumns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={String(row.id)}
              className={selected && selected.id === row.id ? 'row-selected' : undefined}
              onClick={() => void loadDetail(row)}
            >
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              {stateColumns.map((column) => (
                <td key={column}>
                  {column === '限速状态' ? (
                    <span className={`badge badge-${String(row[column] ?? '正常')}`}>{row[column] ?? '—'}</span>
                  ) : (
                    row[column] ?? '—'
                  )}
                </td>
              ))}
              <td className="row-actions" onClick={(event) => event.stopPropagation()}>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + stateColumns.length + 1}>暂无动态限速数据，可先登记限速规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>规则 {String(selected["规则编号"])} · 限速决策</h3>
            <button className="btn ghost" type="button" onClick={() => setSelected(null)}>收起</button>
          </header>

          <div className="detail-grid">
            <div><span>当前速率</span><strong>{selected["当前速率"] ?? '—'}</strong></div>
            <div><span>限速状态</span><strong>{selected["限速状态"] ?? '—'}</strong></div>
            <div><span>最新决策</span><strong>{selected["最新限速决策"] || '—'}</strong></div>
            <div><span>决策版本</span><strong>{selected["决策版本"] ?? 0}</strong></div>
            <div><span>最新决策窗口</span><strong>{selected["最新决策窗口结束"] || '—'}</strong></div>
            <div><span>待复核信号</span><strong>{selected["待复核信号数"] ?? 0}</strong></div>
          </div>

          <div className="detail-toolbar">
            <h4>告警信号</h4>
            <label className="filter-item">
              <span>状态</span>
              <select value={signalFilter} onChange={(event) => setSignalFilter(event.target.value)}>
                <option value="">全部</option>
                {signalStatuses.map((status) => <option key={status} value={status}>{status}</option>)}
              </select>
            </label>
          </div>
          <table className="data-table compact">
            <thead>
              <tr>
                <th>信号编号</th><th>面板编号</th><th>窗口结束</th><th>错误率</th>
                <th>告警结果</th><th>状态</th><th>决策编号</th><th>说明</th><th>处理</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((signal) => (
                <tr key={String(signal.id)}>
                  <td>{signal["信号编号"]}</td>
                  <td>{signal["面板编号"]}</td>
                  <td>{signal["窗口结束时间"] || '—'}</td>
                  <td>{signal["错误率"] != null ? `${signal["错误率"]}%` : '—'}</td>
                  <td>{signal["告警结果"] || '—'}</td>
                  <td><span className={`badge badge-${String(signal["状态"])}`}>{signal["状态"]}</span></td>
                  <td>{signal["决策编号"] || '—'}</td>
                  <td className="cell-note">{signal["说明"] || '—'}</td>
                  <td>
                    {(signal["状态"] === '待确认' || signal["状态"] === '待复核') && (
                      <button className="link" type="button" onClick={() => void confirmSignal(signal)}>确认</button>
                    )}
                  </td>
                </tr>
              ))}
              {signals.length === 0 && (
                <tr><td className="empty-state" colSpan={9}>暂无告警信号</td></tr>
              )}
            </tbody>
          </table>

          <h4>限速决策历史</h4>
          <table className="data-table compact">
            <thead>
              <tr>
                <th>决策编号</th><th>版本</th><th>类型</th><th>面板编号</th>
                <th>窗口结束</th><th>速率变化</th><th>限速状态</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((decision) => (
                <tr key={String(decision.id)}>
                  <td>{decision["决策编号"]}</td>
                  <td>{decision["版本"]}</td>
                  <td>{decision["决策类型"]}</td>
                  <td>{decision["面板编号"]}</td>
                  <td>{decision["窗口结束时间"]}</td>
                  <td>{decision["决策前速率"]} → {decision["决策后速率"]}</td>
                  <td>{decision["限速状态"]}</td>
                </tr>
              ))}
              {decisions.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>该规则尚未产生限速决策</td></tr>
              )}
            </tbody>
          </table>
          <p className="hint-text">规则关闭后历史告警与决策仍保留；同一窗口只产生一次调整，重复/并发信号合并到已有决策。</p>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条动态限速记录{notice ? ` · ${notice}` : ''}</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
