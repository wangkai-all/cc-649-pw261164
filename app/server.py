"""HTTP 服务：JSON API + 静态页面。仅标准库。

运行：  python3 -m app.server [--db data/sales_cube.db] [--port 8000] [--seed 240]

启动行为：
- 首次启动（无任何版本）：创建默认版本（全部 8 个分组集）并构建、激活；
- 发现 status='building' 的版本（上次构建被中断）：自动从检查点续建；
- 若续建完成后仍无活动版本（首次构建即崩溃的情形），自动激活该版本。
"""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from app import cube, service
from app.db import DB, now_iso
from app.seed import seed_sales
from app.service import BadRequest, ConflictError, NotFoundError

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def build_then_autoactivate(db, vid, chunk_size=500, chunk_delay=0.0):
    try:
        cube.build_version(db, vid, chunk_size=chunk_size, chunk_delay=chunk_delay, log=log)
    except Exception as e:  # 崩溃恢复留给下次启动续建
        log(f"[build] 版本 {vid} 构建中断：{e!r}（重启后自动续建）")
        return
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM cube_versions WHERE status='active'").fetchone():
            conn.execute(
                "UPDATE cube_versions SET status='active', activated_at=?"
                " WHERE id=? AND status='ready'", (now_iso(), vid))
            log(f"[build] 无活动版本，已自动激活版本 {vid}")


def cell_to_json(row):
    mask = row["mask"]
    acnt = row["amount_count"]
    return {
        "mask": mask,
        "grouping": cube.grouping_flags(mask),   # 分组标记：1=该维度被汇总
        "region": row["region"],
        "channel": row["channel"],
        "category": row["category"],
        "sale_count": row["sale_count"],
        "amount_count": acnt,
        "sum_cents": row["sum_cents"],
        # 均值 = 精确合计 / 非空金额笔数，同源分子分母，绝不平均各组均值
        "avg_cents": (row["sum_cents"] / acnt) if acnt else None,
    }


def _dim(v, name):
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise BadRequest(f"{name} 必须是非空字符串或 null（不允许空串占位）")
    return v


def _amount(v):
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise BadRequest("amount_cents 必须是整数（分）或 null")
    return v


class Handler(BaseHTTPRequestHandler):
    server_version = "SalesCube/1.0"
    db: DB = None  # 由 main() 注入

    # ---------- 基础 ----------
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: Path, ctype: str):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            raise BadRequest("请求体不是合法 JSON")

    def log_message(self, fmt, *args):
        pass  # 静音访问日志，保留应用日志

    # ---------- 路由 ----------
    def _route(self, method):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/":
                return self._static(PUBLIC / "index.html", "text/html; charset=utf-8")
            if method == "GET" and path == "/app.js":
                return self._static(PUBLIC / "app.js", "text/javascript; charset=utf-8")
            if method == "GET" and path == "/style.css":
                return self._static(PUBLIC / "style.css", "text/css; charset=utf-8")

            if method == "GET" and path == "/api/state":
                return self._json(service.state(self.db))

            if method == "GET" and path == "/api/sales":
                limit = min(int(qs.get("limit", [500])[0]), 2000)
                offset = int(qs.get("offset", [0])[0])
                with self.db.lock:
                    total = self.db.conn.execute("SELECT COUNT(*) c FROM sales").fetchone()["c"]
                    rows = [dict(r) for r in self.db.conn.execute(
                        "SELECT * FROM sales ORDER BY id LIMIT ? OFFSET ?", (limit, offset))]
                return self._json({"total": total, "rows": rows})

            if method == "GET" and path == "/api/cells":
                mask = int(qs["mask"][0])
                vid = qs.get("version_id", [None])[0]
                with self.db.lock:
                    if vid is None:
                        row = self.db.conn.execute(
                            "SELECT id FROM cube_versions WHERE status='active'").fetchone()
                        if not row:
                            return self._json({"version_id": None, "cells": []})
                        vid = row["id"]
                    rows = self.db.conn.execute(
                        "SELECT * FROM cube_cells WHERE version_id=? AND mask=?"
                        " ORDER BY region, channel, category", (vid, mask)).fetchall()
                return self._json({"version_id": int(vid),
                                   "cells": [cell_to_json(r) for r in rows]})

            if method == "POST" and path == "/api/sales":
                b = self._body()
                sale = service.create_sale(
                    self.db, _dim(b.get("region"), "region"),
                    _dim(b.get("channel"), "channel"),
                    _dim(b.get("category"), "category"),
                    _amount(b.get("amount_cents")))
                return self._json(sale, 201)

            m = re.fullmatch(r"/api/sales/(\d+)", path)
            if m and method == "PUT":
                b = self._body()
                fields = {}
                for k in ("region", "channel", "category"):
                    if k in b:
                        fields[k] = _dim(b[k], k)
                if "amount_cents" in b:
                    fields["amount_cents"] = _amount(b["amount_cents"])
                return self._json(service.update_sale(self.db, int(m.group(1)), fields))

            if m and method == "DELETE":
                service.delete_sale(self.db, int(m.group(1)))
                return self._json({"ok": True})

            m = re.fullmatch(r"/api/sales/(\d+)/(revoke|unrevoke)", path)
            if m and method == "POST":
                sale = service.set_revoked(self.db, int(m.group(1)),
                                           m.group(2) == "revoke")
                return self._json(sale)

            if method == "POST" and path == "/api/drilldown":
                b = self._body()
                mask = int(b["mask"])
                dims3 = (b.get("region"), b.get("channel"), b.get("category"))
                with self.db.lock:
                    rows = [dict(r) for r in cube.drilldown(self.db.conn, mask, dims3)]
                return self._json({"count": len(rows), "rows": rows})

            if method == "POST" and path == "/api/versions":
                b = self._body()
                chunk_delay = min(float(b.get("chunk_delay_ms", 0)), 2000) / 1000.0
                chunk_size = max(1, min(int(b.get("chunk_size", 500)), 10000))
                vid = service.create_version(
                    self.db, b.get("name") or "自定义版本", b["masks"], chunk_size)
                threading.Thread(
                    target=build_then_autoactivate,
                    args=(self.db, vid, chunk_size, chunk_delay), daemon=True).start()
                return self._json({"version_id": vid}, 201)

            m = re.fullmatch(r"/api/versions/(\d+)/activate", path)
            if m and method == "POST":
                service.activate_version(self.db, int(m.group(1)))
                return self._json({"ok": True})

            m = re.fullmatch(r"/api/versions/(\d+)", path)
            if m and method == "DELETE":
                service.delete_version(self.db, int(m.group(1)))
                return self._json({"ok": True})

            if method == "POST" and path == "/api/verify":
                b = self._body()
                vid = b.get("version_id")
                with self.db.tx() as conn:  # 同一事务快照：核对与增量看到的数据一致
                    if vid is None:
                        row = conn.execute(
                            "SELECT id FROM cube_versions WHERE status='active'").fetchone()
                        if not row:
                            raise BadRequest("没有活动版本")
                        vid = row["id"]
                    report = cube.verify_version(conn, int(vid))
                return self._json(report)

            return self._json({"error": "not found"}, 404)
        except BadRequest as e:
            return self._json({"error": str(e)}, 400)
        except NotFoundError as e:
            return self._json({"error": str(e)}, 404)
        except ConflictError as e:
            return self._json({"error": str(e)}, 409)
        except Exception as e:  # noqa: BLE001
            log(f"[error] {method} {path}: {e!r}")
            return self._json({"error": f"internal: {e!r}"}, 500)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "sales_cube.db"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=0, help="库为空时写入 N 条种子销售")
    ap.add_argument("--chunk-size", type=int, default=500)
    args = ap.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = DB(args.db)

    with db.tx() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM sales").fetchone()["c"]
        if args.seed and n == 0:
            seed_sales(conn, args.seed)
            log(f"[init] 写入 {args.seed} 条种子销售")
        # 首次启动：建默认版本；崩溃恢复：下面统一续建所有 building 版本
        if not conn.execute("SELECT 1 FROM cube_versions LIMIT 1").fetchone():
            cur = conn.execute(
                "INSERT INTO cube_versions (name, masks, status, created_at)"
                " VALUES ('v1 默认（全部分组集）', ?, 'building', ?)",
                (json.dumps(cube.ALL_MASKS), now_iso()))
            conn.execute(
                "INSERT INTO build_jobs (version_id, total_sales)"
                " VALUES (?, (SELECT COUNT(*) FROM sales))", (cur.lastrowid,))
            log(f"[init] 创建默认版本 {cur.lastrowid}（全部分组集）")
    with db.lock:
        building = [r["id"] for r in db.conn.execute(
            "SELECT id FROM cube_versions WHERE status='building'")]
    for vid in building:
        log(f"[init] 启动构建/续建版本 {vid}")
        threading.Thread(target=build_then_autoactivate,
                         args=(db, vid, args.chunk_size), daemon=True).start()

    Handler.db = db
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log(f"[init] 销售立方表已启动: http://{args.host}:{args.port}  (db={args.db})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        db.close()


if __name__ == "__main__":
    main()
