"""业务操作层：销售增删改/撤销、版本构建与切换。HTTP 层与测试共用。

一致性规则：
- 每个操作在一个事务里同时改 sales 和受影响版本的 cube_cells；
- 增量目标 = 状态为 active 或 ready 的版本（ready 版本保持同步，切换即最新；
  superseded 版本冻结，不再混用）；
- 任何版本处于 building 时拒绝写操作（409），保证构建读到一致快照。
"""
import json

from app import cube
from app.db import now_iso


class ConflictError(Exception):
    """构建进行中，写操作暂不可用。"""


class NotFoundError(Exception):
    pass


class BadRequest(Exception):
    pass


def _targets(conn):
    """增量维护的目标版本：active + ready。"""
    return conn.execute(
        "SELECT id, masks FROM cube_versions WHERE status IN ('active','ready')"
    ).fetchall()


def _guard_no_building(conn):
    if conn.execute(
            "SELECT 1 FROM cube_versions WHERE status='building' LIMIT 1").fetchone():
        raise ConflictError("有新版本正在构建，写操作暂时禁用，构建完成后自动恢复")


def _sale_dict(row):
    return {d: row[d] for d in cube.DIMS} | {"amount_cents": row["amount_cents"]}


def _apply(conn, before, after):
    for v in _targets(conn):
        cube.apply_sale_delta(conn, v["id"], json.loads(v["masks"]), before, after)


def create_sale(db, region, channel, category, amount_cents) -> dict:
    with db.tx() as conn:
        _guard_no_building(conn)
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO sales (region, channel, category, amount_cents, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (region, channel, category, amount_cents, ts, ts))
        sale_id = cur.lastrowid
        after = {"region": region, "channel": channel, "category": category,
                 "amount_cents": amount_cents}
        _apply(conn, None, after)
        return get_sale(conn, sale_id)


def update_sale(db, sale_id: int, fields: dict) -> dict:
    allowed = {"region", "channel", "category", "amount_cents"}
    bad = set(fields) - allowed
    if bad:
        raise BadRequest(f"不允许修改的字段: {sorted(bad)}")
    with db.tx() as conn:
        _guard_no_building(conn)
        old = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not old:
            raise NotFoundError(f"销售 {sale_id} 不存在")
        new = {k: fields.get(k, old[k]) for k in allowed}
        conn.execute(
            "UPDATE sales SET region=?, channel=?, category=?, amount_cents=?, updated_at=?"
            " WHERE id=?",
            (new["region"], new["channel"], new["category"], new["amount_cents"],
             now_iso(), sale_id))
        if not old["revoked"]:
            _apply(conn, _sale_dict(old), new)
        return get_sale(conn, sale_id)


def set_revoked(db, sale_id: int, revoked: bool) -> dict:
    with db.tx() as conn:
        _guard_no_building(conn)
        old = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not old:
            raise NotFoundError(f"销售 {sale_id} 不存在")
        if bool(old["revoked"]) == revoked:
            return get_sale(conn, sale_id)
        conn.execute("UPDATE sales SET revoked=?, updated_at=? WHERE id=?",
                     (1 if revoked else 0, now_iso(), sale_id))
        d = _sale_dict(old)
        # 撤销 = 从所有粒度中移除；恢复 = 重新计入。
        _apply(conn, d if revoked else None, None if revoked else d)
        return get_sale(conn, sale_id)


def delete_sale(db, sale_id: int):
    with db.tx() as conn:
        _guard_no_building(conn)
        old = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not old:
            raise NotFoundError(f"销售 {sale_id} 不存在")
        conn.execute("DELETE FROM sales WHERE id=?", (sale_id,))
        if not old["revoked"]:
            _apply(conn, _sale_dict(old), None)


def get_sale(conn, sale_id: int) -> dict:
    row = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    return dict(row)


# ---------- 版本 ----------

def create_version(db, name: str, masks, chunk_size: int = 500) -> int:
    masks = sorted({int(m) for m in masks})
    if not masks or any(m < 0 or m > 7 for m in masks):
        raise BadRequest("分组集掩码必须是 0..7 的非空子集")
    with db.tx() as conn:
        _guard_no_building(conn)
        cur = conn.execute(
            "INSERT INTO cube_versions (name, masks, status, created_at)"
            " VALUES (?,?, 'building', ?)",
            (name, json.dumps(masks), now_iso()))
        vid = cur.lastrowid
        conn.execute(
            "INSERT INTO build_jobs (version_id, total_sales)"
            " VALUES (?, (SELECT COUNT(*) FROM sales))", (vid,))
        return vid


def activate_version(db, version_id: int):
    """原子切换：旧 active → superseded（冻结），新 ready → active。"""
    with db.tx() as conn:
        v = conn.execute("SELECT status FROM cube_versions WHERE id=?",
                         (version_id,)).fetchone()
        if not v:
            raise NotFoundError(f"版本 {version_id} 不存在")
        if v["status"] != "ready":
            raise ConflictError(f"只有 ready 状态的版本可以切换（当前 {v['status']}）")
        conn.execute("UPDATE cube_versions SET status='superseded' WHERE status='active'")
        conn.execute(
            "UPDATE cube_versions SET status='active', activated_at=? WHERE id=?",
            (now_iso(), version_id))


def delete_version(db, version_id: int):
    with db.tx() as conn:
        v = conn.execute("SELECT status FROM cube_versions WHERE id=?",
                         (version_id,)).fetchone()
        if not v:
            raise NotFoundError(f"版本 {version_id} 不存在")
        if v["status"] in ("active", "building"):
            raise ConflictError("活动或构建中的版本不能删除")
        conn.execute("DELETE FROM cube_cells WHERE version_id=?", (version_id,))
        conn.execute("DELETE FROM build_jobs WHERE version_id=?", (version_id,))
        conn.execute("DELETE FROM cube_versions WHERE id=?", (version_id,))


def state(db) -> dict:
    with db.lock:
        conn = db.conn
        versions = []
        for v in conn.execute(
                "SELECT v.*, j.last_sale_id, j.total_sales, j.done"
                " FROM cube_versions v LEFT JOIN build_jobs j ON j.version_id=v.id"
                " ORDER BY v.id"):
            d = dict(v)
            d["masks"] = json.loads(d["masks"])
            versions.append(d)
        sales_total = conn.execute("SELECT COUNT(*) c FROM sales").fetchone()["c"]
        active = next((v["id"] for v in versions if v["status"] == "active"), None)
        building = any(v["status"] == "building" for v in versions)
        return {
            "versions": versions,
            "active_version": active,
            "building": building,
            "sales_total": sales_total,
            "mask_labels": cube.MASK_LABELS,
            "all_masks": cube.ALL_MASKS,
        }
