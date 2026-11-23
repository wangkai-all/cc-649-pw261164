"""演示二：构建中断与恢复 —— kill -9 杀掉构建中的服务器，重启后从检查点续建。

运行：python3 scripts/demo_recovery.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HttpError, Server, api, free_port  # noqa: E402


def wait_version(port, vid, status, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = api(port, "/api/state")
        v = next((x for x in st["versions"] if x["id"] == vid), None)
        if v and v["status"] == status:
            return v
        time.sleep(0.3)
    raise TimeoutError(f"版本 {vid} 未在 {timeout}s 内变为 {status}")


def main():
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "demo.db"
    port = free_port()
    srv = Server(db_path, port, seed=8000)
    vid = None
    try:
        srv.wait_ready()
        st = srv.wait_active()
        print("=" * 72)
        print("演示：新版本构建被 kill -9 中断，重启后从检查点精确续建")
        print("=" * 72)
        print(f"[0] 服务器已启动，活动版本 v{st['active_version']}，销售 {st['sales_total']} 条")

        # 1. 发起一个带人为延迟的构建（每 100 条一批，批间 60ms，便于中途杀掉）
        r = api(port, "/api/versions", "POST",
                {"name": "v2 全量重建", "masks": list(range(8)),
                 "chunk_size": 100, "chunk_delay_ms": 60})
        vid = r["version_id"]
        print(f"[1] 开始构建版本 v{vid}（8 个分组集，故意放慢以便中断）")

        # 2. 构建期间写操作必须被拒绝
        time.sleep(0.5)
        try:
            api(port, "/api/sales", "POST", {"region": "北京", "amount_cents": 1})
            raise AssertionError("构建期间写操作应被拒绝")
        except HttpError as e:
            assert e.code == 409, e
            print("[2] 构建期间写操作被拒绝（409）✔ —— 保证构建读到一致快照")

        # 3. 等构建进行到一半，然后 kill -9
        deadline = time.time() + 30
        while True:
            st = api(port, "/api/state")
            v = next(x for x in st["versions"] if x["id"] == vid)
            done, total = v["last_sale_id"], v["total_sales"]
            if 0 < done < total:
                break
            if done >= total:
                raise AssertionError("构建太快，没来得及中断")
            if time.time() > deadline:
                raise TimeoutError("构建没有进展")
            time.sleep(0.2)
        print(f"[3] 构建进度 {done}/{total}（{done*100//total}%）—— 现在 kill -9！")
        srv.kill_hard()
        print("    进程已被强制终止（模拟崩溃）")

        # 4. 同一库文件重启：自动从检查点续建
        srv2 = Server(db_path, port)
        try:
            srv2.wait_ready()
            print("[4] 服务器已重启，检测到未完成的构建，自动从检查点续建…")
            v = wait_version(port, vid, "ready")
            print(f"    v{vid} 构建完成（断点续建，检查点={v['last_sale_id']}）")

            # 5. 切换并核对
            api(port, f"/api/versions/{vid}/activate", "POST")
            rep = api(port, "/api/verify", "POST", {})
            assert rep["ok"], rep
            print(f"[5] 已切换到 v{vid}；同快照分组查询核对："
                  f"{rep['actual_cells']} 个格子全部一致 ✔")

            # 6. 恢复后增量维护正常：做一笔跨地区更正再核对
            sale = api(port, "/api/sales", "POST",
                       {"region": "北京", "channel": "线上", "category": "家电",
                        "amount_cents": 88800})
            api(port, f"/api/sales/{sale['id']}", "PUT", {"region": "上海"})
            rep = api(port, "/api/verify", "POST", {})
            assert rep["ok"], rep
            print("[6] 恢复后增量维护正常（跨地区更正后核对仍一致）✔")
            print("\n结果：PASS —— 构建中断后精确续建，未丢一批、未重一批")
        finally:
            srv2.stop()
    finally:
        if srv.proc.poll() is None:
            srv.stop()
        tmp.cleanup()


if __name__ == "__main__":
    main()
