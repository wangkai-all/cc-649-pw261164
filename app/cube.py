"""立方体核心：分组集（GROUPING SETS）、分组标记、增量维护、构建、校验、下钻。

维度与位掩码
------------
三个维度：region(地区)=1, channel(渠道)=2, category(类别)=4。
一个分组集 = 位掩码 0..7：bit 置位表示该维度参与分组（是明细维度），
bit 清零表示该维度被汇总掉。被汇总掉的维度在 cube_cells 里存 NULL，
并由 mask 这个“分组标记”与真实空值区分：

    mask bit=0（已汇总，“全部”）  ↔  SQL 里 GROUPING(d)=1
    mask bit=1 且值为 NULL（空值） ↔  GROUPING(d)=0 且 d IS NULL
    mask bit=1 且值为 '全部'       ↔  真实维度值，恰好叫“全部”，正常显示

空表 SQL 语义（本项目明确约定，见 README）
----------------------------------------
最后一条明细删除后：
- 普通分组（mask != 0）：GROUP BY 在空集上不产生行 → 格子行删除；
- 全表总计（mask = 0）：SELECT COUNT(*), SUM(x) 在空表上仍返回一行
  (0, NULL) → 总计行保留，sale_count=0, amount_count=0, sum_cents=NULL。

均值：不存均值，展示时 avg = sum_cents / amount_count（分子分母同源），
绝不平均各组均值。
"""
import json
import time

DIMS = ("region", "channel", "category")
BIT = {"region": 1, "channel": 2, "category": 4}
ALL_MASKS = list(range(8))

MASK_LABELS = {
    7: "地区 × 渠道 × 类别（明细）",
    3: "地区 × 渠道",
    5: "地区 × 类别",
    6: "渠道 × 类别",
    1: "地区（小计）",
    2: "渠道（小计）",
    4: "类别（小计）",
    0: "总计（全表）",
}


def dims_of_mask(mask: int):
    return [d for d in DIMS if mask & BIT[d]]


def grouping_flags(mask: int):
    """每个维度的分组标记：1=被汇总掉（对应 SQL GROUPING()=1），0=明细维度。"""
    return {d: (0 if mask & BIT[d] else 1) for d in DIMS}


def project(sale: dict, mask: int):
    """把一条销售投影到某分组集：被汇总维度置 NULL。"""
    return tuple(sale[d] if mask & BIT[d] else None for d in DIMS)


def cell_key(mask: int, dims3) -> str:
    # JSON 编码：null 与任何字符串（包括 "全部"、""）都无歧义。
    return json.dumps([mask, dims3[0], dims3[1], dims3[2]], ensure_ascii=False)


def cell_adjust(conn, version_id: int, mask: int, dims3, d_cnt: int, d_acnt: int, d_sum: int):
    """对一个格子施加增量（可正可负）。必须在事务内调用。"""
    key = cell_key(mask, dims3)
    row = conn.execute(
        "SELECT sale_count, amount_count, sum_cents FROM cube_cells "
        "WHERE version_id=? AND cell_key=?",
        (version_id, key),
    ).fetchone()
    if row is None:
        if d_cnt <= 0:
            raise AssertionError(f"对不存在的格子做减项: {key}")
        conn.execute(
            "INSERT INTO cube_cells (version_id, mask, region, channel, category,"
            " cell_key, sale_count, amount_count, sum_cents)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (version_id, mask, dims3[0], dims3[1], dims3[2], key,
             d_cnt, d_acnt, d_sum if d_acnt > 0 else None),
        )
        return
    cnt = row["sale_count"] + d_cnt
    acnt = row["amount_count"] + d_acnt
    s = (row["sum_cents"] or 0) + d_sum
    if cnt < 0 or acnt < 0 or acnt > cnt:
        raise AssertionError(f"格子度量越界: {key} cnt={cnt} acnt={acnt}")
    if cnt == 0:
        if mask == 0:
            # SQL 语义：空表上总计仍是一行 (0, NULL)，总计格子保留。
            conn.execute(
                "UPDATE cube_cells SET sale_count=0, amount_count=0, sum_cents=NULL"
                " WHERE version_id=? AND cell_key=?",
                (version_id, key),
            )
        else:
            # SQL 语义：GROUP BY 在空集上不产生行，普通分组格子消失。
            conn.execute(
                "DELETE FROM cube_cells WHERE version_id=? AND cell_key=?",
                (version_id, key),
            )
    else:
        conn.execute(
            "UPDATE cube_cells SET sale_count=?, amount_count=?, sum_cents=?"
            " WHERE version_id=? AND cell_key=?",
            (cnt, acnt, s if acnt > 0 else None, version_id, key),
        )


def apply_sale_delta(conn, version_id: int, masks, before: dict | None, after: dict | None):
    """把一条销售的变更（before→after，None 表示不存在）同步到所有分组集。

    before=None → 新增；after=None → 删除/撤销；两者都有 → 修改。
    """
    for mask in masks:
        bdims = project(before, mask) if before else None
        adims = project(after, mask) if after else None
        if bdims == adims:
            if bdims is None:
                continue
            # 投影到同一格子（该分组集不含被修改的维度）：只可能金额变了。
            b, a = before["amount_cents"], after["amount_cents"]
            d_acnt = (1 if a is not None else 0) - (1 if b is not None else 0)
            d_sum = (a or 0) - (b or 0)
            if d_acnt or d_sum:
                cell_adjust(conn, version_id, mask, bdims, 0, d_acnt, d_sum)
        else:
            if bdims is not None:
                amt = before["amount_cents"]
                cell_adjust(conn, version_id, mask, bdims, -1,
                            -1 if amt is not None else 0, -(amt or 0))
            if adims is not None:
                amt = after["amount_cents"]
                cell_adjust(conn, version_id, mask, adims, 1,
                            1 if amt is not None else 0, amt or 0)


def ensure_grand_total(conn, version_id: int, masks):
    """空表时总计格子也要存在（SQL：空表上 COUNT/SUM 仍返回一行）。"""
    if 0 in masks:
        key = cell_key(0, (None, None, None))
        row = conn.execute(
            "SELECT 1 FROM cube_cells WHERE version_id=? AND cell_key=?",
            (version_id, key),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO cube_cells (version_id, mask, region, channel, category,"
                " cell_key, sale_count, amount_count, sum_cents)"
                " VALUES (?,0,NULL,NULL,NULL,?,0,0,NULL)",
                (version_id, key),
            )


def build_version(db, version_id: int, chunk_size: int = 500, chunk_delay: float = 0.0,
                  fault_after_chunks: int | None = None, log=print):
    """全量构建一个版本的立方体，支持断点续建。

    每处理完一批销售，格子增量与检查点 last_sale_id 在同一事务提交，
    因此进程在任意时刻被杀，重启后从检查点续建都是精确一次语义。
    chunk_delay / fault_after_chunks 仅用于演示与测试中断恢复。
    """
    with db.lock:
        row = db.conn.execute(
            "SELECT masks FROM cube_versions WHERE id=?", (version_id,)).fetchone()
        masks = json.loads(row["masks"])
    chunks = 0
    while True:
        with db.tx() as conn:
            job = conn.execute(
                "SELECT last_sale_id FROM build_jobs WHERE version_id=?",
                (version_id,)).fetchone()
            last = job["last_sale_id"]
            rows = conn.execute(
                "SELECT id, region, channel, category, amount_cents, revoked"
                " FROM sales WHERE id>? ORDER BY id LIMIT ?",
                (last, chunk_size)).fetchall()
            if not rows:
                ensure_grand_total(conn, version_id, masks)
                conn.execute("UPDATE build_jobs SET done=1 WHERE version_id=?", (version_id,))
                conn.execute("UPDATE cube_versions SET status='ready' WHERE id=?", (version_id,))
                log(f"[build] 版本 {version_id} 构建完成")
                return
            for r in rows:
                if not r["revoked"]:
                    after = {d: r[d] for d in DIMS}
                    after["amount_cents"] = r["amount_cents"]
                    apply_sale_delta(conn, version_id, masks, None, after)
            conn.execute("UPDATE build_jobs SET last_sale_id=? WHERE version_id=?",
                         (rows[-1]["id"], version_id))
        chunks += 1
        if fault_after_chunks is not None and chunks >= fault_after_chunks:
            raise RuntimeError(f"模拟崩溃：构建在 {chunks} 个批次后中断")
        if chunk_delay:
            time.sleep(chunk_delay)


def verify_version(conn, version_id: int) -> dict:
    """用同一事务快照上直接执行的分组查询，逐格子核对 cube_cells。

    等价于 GROUP BY GROUPING SETS（SQLite 无此语法，按分组集逐个 GROUP BY
    再合并，语义相同；分组标记由 mask 提供）。必须在事务内调用。
    """
    v = conn.execute(
        "SELECT masks, status FROM cube_versions WHERE id=?", (version_id,)).fetchone()
    masks = json.loads(v["masks"])
    expected = {}
    for mask in masks:
        dims = dims_of_mask(mask)
        if dims:
            cols = ", ".join(dims)
            sql = (f"SELECT {cols}, COUNT(*) AS c, COUNT(amount_cents) AS ac,"
                   f" SUM(amount_cents) AS s FROM sales WHERE revoked=0 GROUP BY {cols}")
        else:
            # 总计：无 GROUP BY，空表也返回一行 (0, 0, NULL)。
            sql = ("SELECT COUNT(*) AS c, COUNT(amount_cents) AS ac,"
                   " SUM(amount_cents) AS s FROM sales WHERE revoked=0")
        for row in conn.execute(sql):
            dims3 = tuple(row[d] if d in dims else None for d in DIMS)
            expected[cell_key(mask, dims3)] = (row["c"], row["ac"], row["s"])
    actual = {}
    for row in conn.execute(
            "SELECT cell_key, sale_count, amount_count, sum_cents"
            " FROM cube_cells WHERE version_id=?", (version_id,)):
        actual[row["cell_key"]] = (row["sale_count"], row["amount_count"], row["sum_cents"])
    missing = {k: expected[k] for k in expected.keys() - actual.keys()}
    extra = {k: actual[k] for k in actual.keys() - expected.keys()}
    mismatch = {
        k: {"expected": expected[k], "actual": actual[k]}
        for k in expected.keys() & actual.keys()
        if expected[k] != actual[k]
    }
    return {
        "version_id": version_id,
        "status": v["status"],
        "ok": not missing and not extra and not mismatch,
        "expected_cells": len(expected),
        "actual_cells": len(actual),
        "missing": missing,
        "extra": extra,
        "mismatch": mismatch,
    }


def drilldown(conn, mask: int, dims3, limit: int = 2000):
    """从汇总格子下钻到来源记录。

    明细维度：NULL → IS NULL（不是 ='' 或占位符），值 → 等值匹配；
    被汇总维度：不加条件（“全部”）。revoked 的记录不属于任何格子。
    """
    conds, args = ["revoked=0"], []
    for d in DIMS:
        if mask & BIT[d]:
            v = dims3[DIMS.index(d)]
            if v is None:
                conds.append(f"{d} IS NULL")
            else:
                conds.append(f"{d} = ?")
                args.append(v)
    sql = ("SELECT id, region, channel, category, amount_cents, revoked, updated_at"
           f" FROM sales WHERE {' AND '.join(conds)} ORDER BY id LIMIT ?")
    return conn.execute(sql, (*args, limit)).fetchall()
