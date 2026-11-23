"""演示一：跨地区更正与撤销 —— 所有受影响粒度同步修正，并用同快照分组查询核对。

运行：python3 scripts/demo_correction.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Server, api, find_cell, free_port, show_cell  # noqa: E402


def main():
    tmp = tempfile.TemporaryDirectory()
    port = free_port()
    srv = Server(Path(tmp.name) / "demo.db", port, seed=240)
    try:
        srv.wait_ready()
        st = srv.wait_active()
        print("=" * 72)
        print("演示：跨地区更正（北京 → 上海）+ 撤销/恢复，观察各粒度同步")
        print("=" * 72)
        print(f"[0] 服务器已启动，活动版本 v{st['active_version']}，"
              f"种子销售 {st['sales_total']} 条")

        # 1. 新增一笔北京销售
        sale = api(port, "/api/sales", "POST",
                   {"region": "北京", "channel": "线上", "category": "家电",
                    "amount_cents": 123456})
        sid = sale["id"]
        print(f"\n[1] 新增销售 #{sid}: 北京 / 线上 / 家电 / ¥1,234.56")

        def cells(mask):
            return api(port, f"/api/cells?mask={mask}")["cells"]

        def snapshot(note):
            c1, c7, c0 = cells(1), cells(7), cells(0)
            print(f"  {note}")
            show_cell("地区小计·北京        ", find_cell(c1, region="北京"))
            show_cell("地区小计·上海        ", find_cell(c1, region="上海"))
            show_cell("明细·北京/线上/家电  ", find_cell(c7, region="北京",
                      channel="线上", category="家电"))
            show_cell("明细·上海/线上/家电  ", find_cell(c7, region="上海",
                      channel="线上", category="家电"))
            show_cell("全表总计            ", c0[0])
            return c1, c7, c0

        snapshot("[2] 更正前：")
        bj1 = find_cell(cells(1), region="北京")["sale_count"]
        sh1 = find_cell(cells(1), region="上海")["sale_count"]

        # 2. 跨地区更正
        api(port, f"/api/sales/{sid}", "PUT", {"region": "上海"})
        print(f"\n[3] 跨地区更正：销售 #{sid} 地区 北京 → 上海")
        snapshot("[4] 更正后：")
        bj2 = find_cell(cells(1), region="北京")["sale_count"]
        sh2 = find_cell(cells(1), region="上海")["sale_count"]
        assert bj2 == bj1 - 1 and sh2 == sh1 + 1, "地区小计未同步！"
        print("  ✔ 北京 -1、上海 +1，全表总计不变 —— 所有粒度已同步")

        # 3. 撤销与恢复
        api(port, f"/api/sales/{sid}/revoke", "POST")
        print(f"\n[5] 撤销销售 #{sid}")
        snapshot("    撤销后：")
        sh3 = find_cell(cells(1), region="上海")["sale_count"]
        assert sh3 == sh1, "撤销后上海小计应回到更正前"
        api(port, f"/api/sales/{sid}/unrevoke", "POST")
        print(f"[6] 恢复销售 #{sid}")
        sh4 = find_cell(cells(1), region="上海")["sale_count"]
        assert sh4 == sh1 + 1, "恢复后上海小计应再次 +1"
        print("  ✔ 撤销扣减、恢复回补，各粒度一致")

        # 4. 下钻验证：上海小计格能下钻到这笔销售
        dd = api(port, "/api/drilldown", "POST",
                 {"mask": 1, "region": "上海", "channel": None, "category": None})
        assert any(r["id"] == sid for r in dd["rows"]), "下钻结果应包含该销售"
        print(f"[7] 下钻检查：地区小计·上海 → {dd['count']} 条来源记录，"
              f"包含 #{sid} ✔")

        # 5. 同快照分组查询核对
        rep = api(port, "/api/verify", "POST", {})
        assert rep["ok"], rep
        print(f"[8] 同快照分组查询核对：v{rep['version_id']} "
              f"{rep['actual_cells']} 个格子全部一致 ✔")
        print("\n结果：PASS —— 跨地区更正与撤销已在所有粒度同步，且通过快照核对")
    finally:
        srv.stop()
        tmp.cleanup()


if __name__ == "__main__":
    main()
