# 多源异构数据接入引擎

面向多源异构数据接入、智能代理调度、动态限速、爬虫深度管理、数据去噪、格式转换与采集质量监控的数据接入工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 连接器管理 | `connector` | 数据连接器 | 连接器编号、协议类型、目标端点 |
| 代理池管理 | `proxypool` | 代理实例 | 实例编号、代理地址、代理端口 |
| 深度爬取 | `deepcrawl` | 爬虫配置 | 配置编号、种子URL、最大深度 |
| JS渲染采集 | `jsrender` | 渲染配置 | 配置编号、目标URL、浏览器类型 |
| 字段解析器 | `parser` | 解析模板 | 模板编号、目标字段数、XPath表达式 |
| 数据去噪 | `dataclean` | 去噪规则 | 规则编号、目标字段、HTML标签清理 |
| 格式转换 | `formatter` | 转换器 | 转换器编号、输入格式、输出格式 |
| 采集去重 | `dedupengine` | 去重引擎 | 引擎编号、去重字段组、哈希算法 |
| 动态限速 | `throttle` | 限速规则 | 规则编号、目标域组、初始速率 |
| 验证码处理 | `captcha` | 验证码策略 | 策略编号、验证码类型、识别服务商 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、监控指标集、采样间隔秒 |
| 数据分发 | `exporttask` | 分发任务 | 任务编号、数据源标识、目标系统 |
| 种子管理 | `seedmgr` | URL种子 | 种子编号、种子URL、发现来源 |
| 站点地图 | `sitemap` | 站点描述 | 站点编号、站点域名、robots规则 |
| Cookie管理 | `cookiejar` | 会话凭证 | 凭证编号、所属站点、登录方式 |
| 采集器健康 | `healthcheck` | 健康报告 | 报告编号、采集器名称、CPU占用率 |
| 采集快照 | `snapshot` | 数据快照 | 快照编号、关联任务、快照时间 |
| 操作审计 | `audittrail` | 审计记录 | 记录编号、操作类型、操作对象 |

## 监控信号驱动限速

采集监控面板接收采样后，按「监控指标集 + 采样间隔 + 错误率阈值」形成告警触发结果；
连续若干个完整窗口超过阈值后产生被确认的信号，信号写入限速决策，并把决策同步回
面板状态。口径由 `backend/app/services/signals.py` 固化：

- 默认连续 **2** 个完整窗口超过阈值触发降速，连续 **3** 个窗口不高于阈值一半触发恢复；
- 窗口时长不足采样间隔的 90% 视为不完整，进入待复核，不参与连续计数；
- 采样延迟超过 2 个采样间隔视为慢采样；窗口结束时间不晚于已接受的最新窗口视为迟到，
  两者只留档，不覆盖较新的完整窗口；
- 同一采样重复上报按幂等键命中，同一规则同一告警周期只产生一次降速决策，其他面板
  同时触发的信号标记为「已合并」；
- 面板未启用、规则未启用或已关闭、配置缺失时信号进入「待复核」，不修改限速规则；
- 规则关闭后历史告警与历史决策仍保留，可继续通过列表接口读取。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/monitor/{id}/samples` | 上报采样，返回告警触发结果与面板状态 |
| GET | `/api/monitor/signals` | 告警触发结果列表（可按 `panel_id`、`status` 过滤） |
| GET | `/api/monitor/decisions` | 面板维度的限速决策列表 |
| POST | `/api/monitor/signals/{id}/confirm` | 人工确认待确认/待复核信号，幂等 |
| GET | `/api/throttle/signals` | 规则维度的告警触发结果（含已关闭规则的历史） |
| GET | `/api/throttle/decisions` | 规则维度的限速决策历史 |
| POST | `/api/throttle/signals/{id}/confirm` | 人工确认信号并写入限速决策 |

限速规则与监控面板的最新速率、限速状态、最新决策编号都从决策表回填，页面刷新、
导出与重新读取看到的是同一份决策；重复上报不会生成第二份不一致的版本。

后端回归测试（只依赖标准库）：

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
