import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/monitor'
const columns = [
  "面板编号",
  "监控指标集",
  "采样间隔秒",
  "趋势窗口分",
  "异常检测方法",
  "告警触发条件",
  "通知接收组",
  "自动降级开关"
]
const stateColumns = [
  "关联限速规则",
  "错误率阈值",
  "面板告警状态",
  "当前错误率",
  "最新告警结果",
  "最新决策编号"
]
const actions = ["提交面板评审", "启用面板", "归档面板"]
const signalStatuses = ["已观察", "待确认", "待复核", "已确认", "已合并", "已忽略"]

type Row = Record<string, string | number | boolean | null>
type Signal = Record<string, string | number | null>
type Decision = Record<string, string | number | null>

function toIsoMinute(value: string): string {
  if (!value) return ''
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString()
}

export default function MonitorView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Row | null>(null)
  const [signals, setSignals] = useState<Signal[]>([])
  const [decisions, setDecisions] = useState<Decision[]>([])
  const [signalFilter, setSignalFilter] = useState('')
  const [sampleForm, setSampleForm] = useState({
    start: '',
    end: '',
    sampled: '',
    total: '100',
    errors: '0',
  })
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('监控面板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集监控列表读取失败')
    }
  }, [filters])

  const loadDetail = useCallback(async (row: Row) => {
    setSelected(row)
    setErrorMessage('')
    const statusQuery = signalFilter ? `&status=${encodeURIComponent(signalFilter)}` : ''
    try {
      const [signalResp, decisionResp, panelResp] = await Promise.all([
        request(`${ENDPOINT}/signals?panel_id=${row.id}${statusQuery}`),
        request(`${ENDPOINT}/decisions?panel_id=${row.id}`),
        request(`${ENDPOINT}/${row.id}`),
      ])
      if (!signalResp.ok || !decisionResp.ok || !panelResp.ok) {
        throw new Error('告警信号与限速决策读取失败')
      }
      const signalPayload = await signalResp.json()
      const decisionPayload = await decisionResp.json()
      const panelPayload = await panelResp.json()
      setSignals(signalPayload.items ?? [])
      setDecisions(decisionPayload.items ?? [])
      setSelected(panelPayload)
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
        throw new Error('采集监控动作未生效，请稍后重试')
      }
      const payload = await response.json()
      setNotice(payload.message ?? '动作已执行')
      await reload()
      if (selected && selected.id === row.id) {
        await loadDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集监控操作失败')
    }
  }

  async function submitSample(event: React.FormEvent) {
    event.preventDefault()
    if (!selected) return
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${selected.id}/samples`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            窗口开始时间: toIsoMinute(sampleForm.start),
            窗口结束时间: toIsoMinute(sampleForm.end),
            采样时间: toIsoMinute(sampleForm.sampled) || new Date().toISOString(),
            采集请求数: Number(sampleForm.total),
            错误请求数: Number(sampleForm.errors),
          },
        }),
      })
      if (!response.ok) {
        throw new Error('采样上报未送达，限速决策未变化')
      }
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '采样不合法')
      }
      setNotice(payload.message ?? '采样已处理')
      await reload()
      await loadDetail(selected)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采样上报失败')
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

  function fillCurrentWindow(row: Row) {
    const interval = Number(row["采样间隔秒"]) || 60
    const end = new Date()
    end.setSeconds(0, 0)
    const start = new Date(end.getTime() - interval * 1000)
    const local = (value: Date) => {
      const offset = value.getTimezoneOffset() * 60000
      return new Date(value.getTime() - offset).toISOString().slice(0, 16)
    }
    setSampleForm((prev) => ({
      ...prev,
      start: local(start),
      end: local(end),
      sampled: local(new Date()),
    }))
  }

  return (
    <section className="page" data-module="monitor">
      <header className="page-head">
        <div>
          <h2>采集监控管理</h2>
          <p className="page-desc">配置采集运行状态的实时监控面板，对采集量、延迟、错误率和代理健康进行可视化。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('监控面板登记入口尚未接入审批流')}>
            登记监控面板
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出采集监控清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">监控面板总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">已启用面板</span>
          <strong className="stat-value">{rows.filter((row) => row.status === '已启用').length}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">告警中面板</span>
          <strong className="stat-value">{rows.filter((row) => row["面板告警状态"] === '告警中').length}</strong>
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
                  {column === '面板告警状态' ? (
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
              <td className="empty-state" colSpan={columns.length + stateColumns.length + 1}>暂无采集监控数据，可先登记监控面板</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>面板 {String(selected["面板编号"])} · 告警触发结果</h3>
            <button className="btn ghost" type="button" onClick={() => setSelected(null)}>收起</button>
          </header>

          <div className="detail-grid">
            <div><span>关联限速规则</span><strong>{selected["关联限速规则"] ?? '—'}</strong></div>
            <div><span>规则当前速率</span><strong>{selected["关联规则当前速率"] ?? '—'}</strong></div>
            <div><span>规则限速状态</span><strong>{selected["关联规则限速状态"] ?? '—'}</strong></div>
            <div><span>最新限速决策</span><strong>{selected["关联规则最新决策"] ?? '—'}</strong></div>
            <div><span>错误率阈值</span><strong>{selected["错误率阈值"] ?? '—'}%</strong></div>
            <div><span>连续窗口数</span><strong>{selected["连续窗口数"] ?? '—'}</strong></div>
          </div>

          <form className="sample-form" onSubmit={(event) => void submitSample(event)}>
            <label>
              <span>窗口开始</span>
              <input type="datetime-local" value={sampleForm.start}
                onChange={(event) => setSampleForm((prev) => ({ ...prev, start: event.target.value }))} required />
            </label>
            <label>
              <span>窗口结束</span>
              <input type="datetime-local" value={sampleForm.end}
                onChange={(event) => setSampleForm((prev) => ({ ...prev, end: event.target.value }))} required />
            </label>
            <label>
              <span>采样时间</span>
              <input type="datetime-local" value={sampleForm.sampled}
                onChange={(event) => setSampleForm((prev) => ({ ...prev, sampled: event.target.value }))} />
            </label>
            <label>
              <span>采集请求数</span>
              <input type="number" min="1" value={sampleForm.total}
                onChange={(event) => setSampleForm((prev) => ({ ...prev, total: event.target.value }))} required />
            </label>
            <label>
              <span>错误请求数</span>
              <input type="number" min="0" value={sampleForm.errors}
                onChange={(event) => setSampleForm((prev) => ({ ...prev, errors: event.target.value }))} required />
            </label>
            <button className="btn" type="button" onClick={() => fillCurrentWindow(selected)}>填入当前窗口</button>
            <button className="btn primary" type="submit">上报采样</button>
          </form>

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
                <th>信号编号</th><th>窗口结束</th><th>错误率</th><th>连续窗口数</th>
                <th>告警结果</th><th>状态</th><th>限速决策</th><th>说明</th><th>处理</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((signal) => (
                <tr key={String(signal.id)}>
                  <td>{signal["信号编号"]}</td>
                  <td>{signal["窗口结束时间"] || '—'}</td>
                  <td>{signal["错误率"] != null ? `${signal["错误率"]}%` : '—'}</td>
                  <td>{signal["连续窗口数"] ?? '—'}</td>
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

          <h4>限速决策</h4>
          <table className="data-table compact">
            <thead>
              <tr>
                <th>决策编号</th><th>版本</th><th>类型</th><th>窗口结束</th>
                <th>速率变化</th><th>限速状态</th><th>告警信号</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((decision) => (
                <tr key={String(decision.id)}>
                  <td>{decision["决策编号"]}</td>
                  <td>{decision["版本"]}</td>
                  <td>{decision["决策类型"]}</td>
                  <td>{decision["窗口结束时间"]}</td>
                  <td>{decision["决策前速率"]} → {decision["决策后速率"]}</td>
                  <td>{decision["限速状态"]}</td>
                  <td>{decision["信号编号"]}</td>
                </tr>
              ))}
              {decisions.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>该面板尚未产生限速决策</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条采集监控记录{notice ? ` · ${notice}` : ''}</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
