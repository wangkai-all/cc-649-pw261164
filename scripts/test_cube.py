"""立方体正确性测试。运行：python3 scripts/test_cube.py"""
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cube, service  # noqa: E402
from app.db import DB  # noqa: E402
from app.seed import seed_sales  # noqa: E402

REGIONS = ["北京", "上海", "广州", "全部", None]
CHANNELS = ["线上", "线下", None]
CATEGORIES = ["家电", "食品", None]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def seed(self, n=120):
        with self.db.tx() as conn:
            seed_sales(conn, n)

    def build_active(self, masks=cube.ALL_MASKS, chunk_size=500):
        vid = service.create_version(self.db, "v", masks, chunk_size)
        cube.build_version(self.db, vid, chunk_size=chunk_size, log=lambda *a: None)
        service.activate_version(self.db, vid)
        return vid

    def verify(self, vid):
        with self.db.tx() as conn:
            return cube.verify_version(conn, vid)

    def assert_ok(self, vid, ctx=""):
        rep = self.verify(vid)
        self.assertTrue(rep["ok"],
                        f"{ctx}\nmissing={rep['missing']}\nextra={rep['extra']}\n"
                        f"mismatch={rep['mismatch']}")

    def cells(self, vid, mask):
        with self.db.lock:
            return [dict(r) for r in self.db.conn.execute(
                "SELECT * FROM cube_cells WHERE version_id=? AND mask=?", (vid, mask))]

    def grand_total(self, vid):
        rows = self.cells(vid, 0)
        self.assertEqual(len(rows), 1, "总计格子必须恰好一行")
        return rows[0]


class BuildAndVerify(Base):
    def test_chunked_build_matches_direct_groupby(self):
        self.seed(120)
        vid = service.create_version(self.db, "v", cube.ALL_MASKS, chunk_size=7)
        cube.build_version(self.db, vid, chunk_size=7, log=lambda *a: None)
        service.activate_version(self.db, vid)
        self.assert_ok(vid, "分块构建后必须与同快照分组查询一致")
        for mask in cube.ALL_MASKS:
            self.assertTrue(self.cells(vid, mask), f"mask={mask} 应有格子")

    def test_grouping_markers_stored(self):
        self.seed(50)
        vid = self.build_active()
        # mask=3（地区×渠道）：类别被汇总，存储必须为 NULL，分组标记=1
        for row in self.cells(vid, 3):
            self.assertIsNone(row["category"])
            self.assertEqual(cube.grouping_flags(row["mask"]),
                             {"region": 0, "channel": 0, "category": 1})
        # 分组标记区分三种格子：真实空值 / 已汇总 / 真实值“全部”
        keys = {r["cell_key"] for r in self.cells(vid, 1)}
        self.assertIn(cube.cell_key(1, ("全部", None, None)), keys)   # 真实值“全部”
        self.assertIn(cube.cell_key(1, (None, None, None)), keys)     # 真实空值
        gt = self.grand_total(vid)
        self.assertEqual(gt["cell_key"], cube.cell_key(0, (None, None, None)))  # 已汇总
        self.assertNotEqual(cube.cell_key(1, (None, None, None)),
                            cube.cell_key(0, (None, None, None)))


class IncrementalProperty(Base):
    def test_random_ops_always_match_snapshot(self):
        self.seed(120)
        vid = self.build_active(chunk_size=13)
        rng = random.Random(99)
        live_ids = {r["id"] for r in self._all_sales()}
        for i in range(300):
            op = rng.choice(["insert", "update", "revoke", "unrevoke", "delete"])
            ctx = f"第{i}步 op={op}"
            if op == "insert" or not live_ids:
                sid = service.create_sale(
                    self.db, rng.choice(REGIONS), rng.choice(CHANNELS),
                    rng.choice(CATEGORIES),
                    None if rng.random() < 0.2 else rng.randint(1, 99999))["id"]
                live_ids.add(sid)
            elif op == "update":
                sid = rng.choice(sorted(live_ids))
                service.update_sale(self.db, sid, {
                    rng.choice(["region", "channel", "category"]): rng.choice(
                        [REGIONS, CHANNELS, CATEGORIES][rng.randrange(3)]),
                    **({"amount_cents": None if rng.random() < 0.2 else rng.randint(1, 99999)}
                       if rng.random() < 0.5 else {}),
                })
            elif op == "revoke":
                service.set_revoked(self.db, rng.choice(sorted(live_ids)), True)
            elif op == "unrevoke":
                service.set_revoked(self.db, rng.choice(sorted(live_ids)), False)
            else:
                sid = rng.choice(sorted(live_ids))
                service.delete_sale(self.db, sid)
                live_ids.discard(sid)
            self.assert_ok(vid, ctx)
        # 下钻与格子度量一致
        for mask in cube.ALL_MASKS:
            for cell in self.cells(vid, mask):
                with self.db.lock:
                    rows = cube.drilldown(
                        self.db.conn, mask, (cell["region"], cell["channel"], cell["category"]))
                self.assertEqual(len(rows), cell["sale_count"], f"mask={mask} 下钻笔数")
                self.assertEqual(
                    sum(1 for r in rows if r["amount_cents"] is not None),
                    cell["amount_count"])
                self.assertEqual(
                    sum(r["amount_cents"] or 0 for r in rows) or None,
                    cell["sum_cents"])

    def _all_sales(self):
        with self.db.lock:
            return self.db.conn.execute("SELECT id FROM sales").fetchall()


class EmptyTableSemantics(Base):
    def test_last_detail_deleted(self):
        self.seed(20)
        vid = self.build_active()
        ids = [r["id"] for r in
               self.db.conn.execute("SELECT id FROM sales").fetchall()]
        for sid in ids:
            service.delete_sale(self.db, sid)
            self.assert_ok(vid, f"删除 {sid} 后")
        # SQL 语义：普通分组在空集上不产生行 → 全部消失
        for mask in cube.ALL_MASKS:
            if mask != 0:
                self.assertEqual(self.cells(vid, mask), [], f"mask={mask} 应无格子")
        # SQL 语义：空表上 COUNT/SUM 仍返回一行 (0, NULL) → 总计行保留
        gt = self.grand_total(vid)
        self.assertEqual((gt["sale_count"], gt["amount_count"], gt["sum_cents"]),
                         (0, 0, None))
        self.assert_ok(vid, "空表终态")
        # 再插入一条，格子重新出现
        service.create_sale(self.db, "北京", "线上", "家电", 100)
        self.assert_ok(vid, "空表后新增")
        self.assertEqual(self.grand_total(vid)["sale_count"], 1)


class NullVsAllVsLiteral(Base):
    def test_three_kinds_of_cells(self):
        self.seed(120)  # 种子含真实地区“全部”与 NULL 地区
        vid = self.build_active()
        region_cells = {r["region"]: r for r in self.cells(vid, 1)}
        self.assertIn("全部", region_cells, "真实地区“全部”必须是独立格子")
        self.assertIn(None, region_cells, "空地区必须是独立格子")
        # 下钻：真实值“全部”只命中 region='全部' 的记录
        with self.db.lock:
            lit = cube.drilldown(self.db.conn, 1, ("全部", None, None))
            nul = cube.drilldown(self.db.conn, 1, (None, None, None))
            total = cube.drilldown(self.db.conn, 0, (None, None, None))
        self.assertTrue(lit)
        self.assertTrue(all(r["region"] == "全部" for r in lit))
        self.assertTrue(nul)
        self.assertTrue(all(r["region"] is None for r in nul))
        self.assertEqual(len(lit), region_cells["全部"]["sale_count"])
        self.assertEqual(len(nul), region_cells[None]["sale_count"])
        self.assertEqual(len(total), self.grand_total(vid)["sale_count"])
        # 三种格子互不相同的 key
        self.assertEqual(len({cube.cell_key(1, ("全部", None, None)),
                              cube.cell_key(1, (None, None, None)),
                              cube.cell_key(0, (None, None, None))}), 3)


class AvgSemantics(Base):
    def test_avg_from_sum_over_count_not_avg_of_avgs(self):
        vid = self.build_active(masks=[1, 0])  # 地区小计 + 总计
        service.create_sale(self.db, "A", "线上", "家电", 100)
        service.create_sale(self.db, "A", "线上", "家电", 300)   # A 均值 200
        service.create_sale(self.db, "B", "线上", "家电", 1000)  # B 均值 1000
        service.create_sale(self.db, "C", "线上", "家电", None)  # 金额为空
        gt = self.grand_total(vid)
        self.assertEqual((gt["sale_count"], gt["amount_count"], gt["sum_cents"]),
                         (4, 3, 1400))
        # 总计均值 = 1400/3 ≈ 466.67，而不是 (200+1000)/2 = 600
        self.assertAlmostEqual(gt["sum_cents"] / gt["amount_count"], 466.666, places=2)
        self.assertNotEqual(gt["sum_cents"] / gt["amount_count"], 600)
        cells = {r["region"]: r for r in self.cells(vid, 1)}
        self.assertEqual((cells["C"]["sale_count"], cells["C"]["amount_count"],
                          cells["C"]["sum_cents"]), (1, 0, None))
        self.assert_ok(vid)


class CrossRegionCorrection(Base):
    def test_region_move_and_revoke_sync_all_granularities(self):
        self.seed(120)
        vid = self.build_active()
        sid = service.create_sale(self.db, "北京", "线上", "家电", 123456)["id"]

        def region_cell(name):
            return next(r for r in self.cells(vid, 1) if r["region"] == name)

        bj_before = region_cell("北京")["sale_count"]
        sh_before = region_cell("上海")["sale_count"]
        gt_before = self.grand_total(vid)["sale_count"]
        # 跨地区更正：北京 → 上海
        service.update_sale(self.db, sid, {"region": "上海"})
        self.assertEqual(region_cell("北京")["sale_count"], bj_before - 1)
        self.assertEqual(region_cell("上海")["sale_count"], sh_before + 1)
        self.assertEqual(self.grand_total(vid)["sale_count"], gt_before)
        self.assert_ok(vid, "改地区后")
        # 撤销：所有粒度同步扣减
        service.set_revoked(self.db, sid, True)
        self.assertEqual(region_cell("上海")["sale_count"], sh_before)
        self.assertEqual(self.grand_total(vid)["sale_count"], gt_before - 1)
        self.assert_ok(vid, "撤销后")
        # 恢复：还原
        service.set_revoked(self.db, sid, False)
        self.assertEqual(region_cell("上海")["sale_count"], sh_before + 1)
        self.assert_ok(vid, "恢复后")
        # 删除已撤销记录：立方体不再变化
        service.set_revoked(self.db, sid, True)
        gt = self.grand_total(vid)["sale_count"]
        service.delete_sale(self.db, sid)
        self.assertEqual(self.grand_total(vid)["sale_count"], gt)
        self.assert_ok(vid, "删除已撤销记录后")


class Recovery(Base):
    def test_interrupted_build_resumes_from_checkpoint(self):
        self.seed(500)
        vid = service.create_version(self.db, "v", cube.ALL_MASKS, chunk_size=10)
        with self.assertRaises(RuntimeError):
            cube.build_version(self.db, vid, chunk_size=10, fault_after_chunks=7,
                               log=lambda *a: None)
        with self.db.lock:
            st = self.db.conn.execute(
                "SELECT status FROM cube_versions WHERE id=?", (vid,)).fetchone()["status"]
            ckpt = self.db.conn.execute(
                "SELECT last_sale_id FROM build_jobs WHERE version_id=?", (vid,)).fetchone()
        self.assertEqual(st, "building")
        self.assertGreater(ckpt["last_sale_id"], 0)
        # 模拟重启后续建
        cube.build_version(self.db, vid, chunk_size=10, log=lambda *a: None)
        service.activate_version(self.db, vid)
        self.assert_ok(vid, "中断续建后")


class VersionSwitch(Base):
    def test_switch_freezes_old_and_syncs_ready(self):
        self.seed(100)
        v1 = self.build_active()
        service.create_sale(self.db, "北京", "线上", "家电", 500)
        # 构建 v2（仅明细 + 总计）
        v2 = service.create_version(self.db, "v2", [7, 0], chunk_size=9)
        cube.build_version(self.db, v2, chunk_size=9, log=lambda *a: None)
        # ready 版本与 active 同步接收增量
        service.create_sale(self.db, "上海", "线下", "食品", 700)
        with self.db.lock:
            gt1 = self.db.conn.execute(
                "SELECT sale_count FROM cube_cells WHERE version_id=? AND mask=0",
                (v1,)).fetchone()["sale_count"]
            gt2 = self.db.conn.execute(
                "SELECT sale_count FROM cube_cells WHERE version_id=? AND mask=0",
                (v2,)).fetchone()["sale_count"]
        self.assertEqual(gt1, gt2)
        self.assert_ok(v2, "ready 版本与当前快照一致")
        # 切换：v1 冻结，v2 接管
        service.activate_version(self.db, v2)
        service.create_sale(self.db, "广州", "电话", "服装", 900)
        with self.db.lock:
            gt1_after = self.db.conn.execute(
                "SELECT sale_count FROM cube_cells WHERE version_id=? AND mask=0",
                (v1,)).fetchone()["sale_count"]
        self.assertEqual(gt1_after, gt1, "superseded 版本必须冻结，不混用新数据")
        self.assert_ok(v2, "切换后 v2 增量正常")
        self.assertFalse(self.verify(v1)["ok"], "冻结的 v1 与当前快照不再一致（预期行为）")


class MutationGuard(Base):
    def test_writes_rejected_while_building(self):
        self.seed(50)
        self.build_active()
        vid = service.create_version(self.db, "v2", [7], chunk_size=5)  # 未构建=building
        with self.assertRaises(service.ConflictError):
            service.create_sale(self.db, "北京", "线上", "家电", 100)
        with self.assertRaises(service.ConflictError):
            service.create_version(self.db, "v3", [0])
        cube.build_version(self.db, vid, chunk_size=5, log=lambda *a: None)
        service.create_sale(self.db, "北京", "线上", "家电", 100)  # 构建完成后恢复
        self.assert_ok(vid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
