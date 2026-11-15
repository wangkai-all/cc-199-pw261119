# 设备测量数据分布汇总（本地应用）

面向**持续修订**的设备测量数据：原始记录可以新增、更正、撤销，页面按设备组与
时间段统计固定边界直方图、中位数（P50）与高分位值（P90/P95/P99）。纯 Python
标准库 + 原生 HTML/JS/SVG 实现，**零第三方依赖**，数据落在本地 SQLite。

## 运行

```bash
# 1. 初始化样例数据库（含重复修订、边界值、极端值、1200 条大样本）并启动
python3 -m app.main --reset --seed --port 8000
# 浏览器打开 http://127.0.0.1:8000

# 2. 从空目录起步（仅建表，不写任何数据/桶方案）
python3 -m app.main --db data/empty.db --port 8001

# 3. 只跑测试（隔离模式，不继承环境变量）
python3 -I run_tests.py
```

无网络、无 pip install、无构建步骤。要求 Python 3.11+（仅用标准库）。

## 需求是如何被满足的

### 1. 修订可撤回，绝不在不可撤回的摘要上假装删除

* 每条原始记录有 `active / superseded / revoked` 三态；更正 = 旧记录置
  `superseded` 并产生一条新记录，撤销 = 置 `revoked`，修订链完整保留
    （`app/storage.py`）。
* 汇总不是只能加不能减的计数：`ContributionLedger` 保存每个
  设备组×周期的**值多重集合**，修订时显式 `remove(旧值)` 再 `add(新值)`；
  重复值按多重集合语义只移除其中一个（`app/summary.py`）。
* 每次修订同时跑**增量路径**（台账 remove/add）与**独立全量重算路径**
  （从全部有效记录重建），两者结果不一致就抛 `SummaryMismatchError` 回滚
    （`app/service.py`）。页面上的“全量重算比对/校验当前版本”可随时复核。
* 汇总版本**只增不改、不可变**：撤销/更正生成新版本，旧版本原样保留，
  页面可逐版本查看并两两比较。

### 2. 写入失败不发布半份汇总

修订事件、记录状态变更、新汇总版本在**同一个 SQLite 事务**
（`BEGIN IMMEDIATE`）内提交。表单与 API 提供 `fail_before_commit` 故障注入：
在提交前主动抛错，整体回滚——测试断言回滚后记录数、修订数、版本数三项均
不变（`tests/test_service.py::AtomicityTest`、`tests/test_api.py`）。

### 3. 固定边界、带版本的直方图桶；桶外值单独计数

* 桶方案存表、只允许新增版本，旧版本边界冻结（`app/buckets.py`、
  `bucket_schemes` 表）。
* 归属规则左闭右开：`edges[j] <= v < edges[j+1]`；恰好等于末边以及更大的值
  计入**桶外上界**，小于首边计入**桶外下界**，两者各自单独计数，绝不并入
  任何桶。边界值样例（0/10/20/…/100）在种子数据与测试中固定断言。

### 4. 分位数定义明确，小样本精确、大样本带误差口径

* 统一定义：**Hyndman & Fan (1996) type 7**，`h=(n-1)p` 线性插值
  （numpy/R 的默认口径），定义字符串随每个汇总返回并显示在页面上
    （`app/quantile.py`）。
* `n <= 1000`：用全部有效值精确计算，最坏绝对误差为 0。
* `n > 1000`：仅用桶计数在目标桶内做均匀线性插值；给出**确定性最坏绝对
  误差界 = 所在桶宽度**（不是抽样误差）。目标分位落在桶外时无有限宽度可
  约束，显式返回 `unbounded` 并在页面瓦片标注“桶外，无有限误差界”，
  绝不编造数字。种子数据的 1200 条压力台记录可直接看到这三种状态。

### 5. 页面能力

* 单行过滤器（版本 / 设备组 / 粒度：全部·天·ISO 周·月 / 周期）作用于其下
  所有图表与表格，数字始终同源。
* SVG 直方图（含桶外双侧柱，点击/回车查看**桶内原始记录**抽屉）、统计瓦片、
  桶计数表格（图表的表格视图孪生）、原始记录表（行内更正/撤销）、不可变
  版本历史表、版本比较抽屉（计数与 P50 差异）、全量重算比对横幅。
* **空分组与单条记录有明确表现**：空行 `count=0`、分位标记 `empty/未定义`、
  空桶画 1px 占位；单条记录时所有分位等于该值且标注精确。
* 所有 API 字符串经 `textContent` 插入 DOM；浅/深色主题分别取 dataviz 参考
  调色板的已校验色值，颜色只承担标识、文字只用文本色。

## 目录结构

```
app/
  quantile.py      # type-7 精确分位 + 桶插值近似与误差界
  buckets.py       # 固定边界桶方案（版本化、左闭右开、桶外归属）
  summary.py       # 贡献台账、周期切分、增量/全量两条汇总路径
  storage.py       # SQLite schema 与事务
  service.py       # 修订编排、双路径互校、版本比较/校验、故障注入
  server.py        # 标准库 HTTP 服务与 JSON API
  seed.py          # 样例数据
  main.py          # 入口
  static/          # index.html / app.js / styles.css
tests/             # 53 个 unittest（分位、桶、台账、服务、HTTP）
run_tests.py       # -I 隔离模式测试入口
```

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/summaries` `/api/summary?version=` | 版本列表 / 某不可变版本详情 |
| GET | `/api/groups` `/api/records?group_id=` | 设备组 / 原始记录（含已更正、已撤销） |
| GET | `/api/revisions` | 完整修订事件流 |
| GET | `/api/recompute` `/api/verify?version=` | 独立全量重算 / 与存储版本逐字段比对 |
| GET | `/api/compare?a=&b=` | 两个版本的分组×周期差异 |
| POST | `/api/records` `/api/correct` `/api/revoke` | 新增 / 更正 / 撤销（原因必填，支持故障注入） |
| POST | `/api/schemes` | 新增桶方案版本（旧版本冻结） |
