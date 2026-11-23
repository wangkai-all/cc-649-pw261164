# 销售立方表（Sales Cube）

在网页上维护一张**可增量刷新**的销售立方表：按地区、渠道、商品类别自由选择
明细 / 小计 / 总计（GROUPING SETS），从汇总格子一键下钻到来源记录。
零第三方依赖，仅 Python 3 标准库（sqlite3 + http.server）。

## 运行

```bash
python3 -m app.server --db data/sales_cube.db --port 8000 --seed 240
# 打开 http://127.0.0.1:8000
```

- `--seed N`：库为空时写入 N 条确定性种子数据（含真实空值、名为“全部”的真实地区、空金额）。
- 首次启动自动创建默认版本（全部 8 个分组集）并构建、激活。

## 测试与演示

```bash
python3 scripts/test_cube.py        # 10 个测试：属性测试（300 随机操作逐步核对）+ 全部边界语义
python3 scripts/demo_correction.py  # 演示：跨地区更正 + 撤销/恢复，各粒度同步，快照核对
python3 scripts/demo_recovery.py    # 演示：构建被 kill -9 中断，重启后从检查点精确续建
```

## 数据模型与分组标记

三个维度：`region`（地区=1）、`channel`（渠道=2）、`category`（类别=4）。
一个分组集 = 位掩码 0..7，**bit 置位 = 该维度参与分组（明细维度），bit 清零 = 被汇总掉**。

每个格子（`cube_cells`）持久化三个度量与分组标记：

| 字段 | 含义 |
|---|---|
| `sale_count` | 销售笔数 `COUNT(*)` |
| `amount_count` | 非空金额笔数 `COUNT(amount)` |
| `sum_cents` | 精确金额合计 `SUM(amount)`，整数分存储，绝不浮点累加 |
| `mask` | 分组标记：每个维度 bit 置 0 表示“该维度在此格子被汇总” |

**均值不存储**，展示时 `avg = sum_cents / amount_count`（分子分母同源），
绝不平均各组均值（测试里有 1400/3 ≠ (200+1000)/2 的反例）。

### 三种格子严格区分（不用空值或文字占位混存）

| 情形 | 存储 | 分组标记 | 页面显示 |
|---|---|---|---|
| 真实空值（如地区未知） | `region=NULL, mask bit=1` | 0 | `∅ 空值`（琥珀色） |
| 该维度已被汇总（“所有地区”） | `region=NULL, mask bit=0` | 1 | `（全部）`（灰色斜体） |
| 真实维度值恰好叫“全部” | `region='全部', mask bit=1` | 0 | `全部`（蓝色，标注真实值） |

格子主键 `cell_key = JSON[mask, region, channel, category]`，JSON 编码使
`NULL`、`"全部"`、`""` 三者无歧义。下钻时明细维度用 `IS NULL` / `=` 精确过滤，
被汇总维度不加条件 —— 查询同样区分空值与小计。

## 项目明确的 SQL 语义约定

1. **最后一条明细删除后**：普通分组（mask≠0）的格子**消失**——`GROUP BY` 在空集上不产生行；
   全表总计（mask=0）的格子**保留为一行** `sale_count=0, amount_count=0, sum_cents=NULL`——
   `SELECT COUNT(*), SUM(x) FROM t` 在空表上仍返回一行 `(0, NULL)`。
2. **`SUM` 语义**：某格子非空金额笔数为 0 时 `sum_cents` 为 `NULL`（不是 0），均值显示 `—`。
3. **核对语义**：`/api/verify` 在同一事务快照上对每个分组集直接执行
   `SELECT …, COUNT(*), COUNT(amount_cents), SUM(amount_cents) … GROUP BY …`
   （等价于 `GROUP BY GROUPING SETS`，SQLite 无此语法，按分组集逐个执行再合并，
   分组标记由 mask 提供），与 `cube_cells` 逐格子比对，缺失/多出/不符都会报告。

## 增量维护

销售的增、改（含跨地区更正）、撤销/恢复、删除，在同一事务里换算成
before→after 两组投影，对**所有分组集**逐格加/减（`app/cube.py: apply_sale_delta`）：

- 投影到同一格子（该分组集不含被改维度）：只调金额度量；
- 投影到不同格子：旧格减、新格加；
- 格子笔数归零：普通分组删除、总计行清零保留（见上文 SQL 语义）。

撤销是逻辑删除（`revoked=1`，从所有粒度移除，可恢复）；删除是硬删除。

## 版本：调整分组组合 → 构建 → 切换

- 版本 = 一组 GROUPING SETS 的物化结果，状态机 `building → ready → active → superseded`；
- 网页勾选分组组合 → **构建新版本**（全量重建，不碰旧版本）→ **切换**（原子生效）；
- `ready` 版本与 `active` 版本同步接收增量，切换瞬间即为最新；
- 切换后旧版本**冻结**，不再接收新数据，不与新配置混用（可浏览、可删除）；
- **构建期间写操作一律 409 拒绝**，保证构建读到一致快照。

## 构建中断恢复

构建按批（默认 500 条/批）处理，**每批格子增量与检查点 `last_sale_id` 在同一事务提交**，
因此进程在任意时刻被 `kill -9`，已提交批次不丢、未提交批次不重。
重启时服务器自动发现 `building` 状态的版本并**从检查点续建**；
若崩溃发生在首次构建（无活动版本），续建完成后自动激活。
`scripts/demo_recovery.py` 完整演示这一过程。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 版本列表、活动版本、构建状态 |
| GET | `/api/sales` | 销售明细 |
| POST | `/api/sales` | 新增（增量同步所有粒度） |
| PUT | `/api/sales/{id}` | 修改维度/金额（如跨地区更正） |
| POST | `/api/sales/{id}/revoke`、`/unrevoke` | 撤销 / 恢复 |
| DELETE | `/api/sales/{id}` | 硬删除 |
| GET | `/api/cells?mask=&version_id=` | 立方体格子（含分组标记与派生均值） |
| POST | `/api/drilldown` | 从格子下钻到来源记录 |
| POST | `/api/versions` | 按所选分组集构建新版本 |
| POST | `/api/versions/{id}/activate` | 原子切换 |
| DELETE | `/api/versions/{id}` | 删除冻结/就绪版本 |
| POST | `/api/verify` | 同快照分组查询核对 |

## 目录结构

```
app/
  db.py       连接、schema、事务
  cube.py     分组集/分组标记、增量维护、可续建构建、快照核对、下钻
  service.py  业务操作（销售增删改、版本切换），HTTP 层与测试共用
  seed.py     确定性种子数据
  server.py   HTTP API + 静态页面，启动时自动续建
public/       单页前端（原生 JS，无构建步骤）
scripts/
  test_cube.py        测试套件
  demo_correction.py  跨地区更正演示
  demo_recovery.py    构建中断恢复演示
```
