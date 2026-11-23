"""演示脚本共用工具：启动/停止服务器子进程、HTTP 客户端。"""
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HttpError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body}")
        self.code = code
        self.body = body


def api(port, path, method="GET", body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode())


class Server:
    def __init__(self, db_path, port, seed=0):
        cmd = [sys.executable, "-m", "app.server",
               "--db", str(db_path), "--port", str(port)]
        if seed:
            cmd += ["--seed", str(seed)]
        self.proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.port = port

    def wait_ready(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"服务器提前退出 code={self.proc.returncode}\n{out}")
            try:
                return api(self.port, "/api/state")
            except Exception:
                time.sleep(0.2)
        raise TimeoutError("服务器未就绪")

    def wait_active(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = api(self.port, "/api/state")
            if st["active_version"]:
                return st
            time.sleep(0.2)
        raise TimeoutError("没有活动版本")

    def kill_hard(self):
        """kill -9：模拟崩溃，不给任何清理机会。"""
        self.proc.kill()
        self.proc.wait()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.kill_hard()

    def logs(self):
        if self.proc.stdout:
            return self.proc.stdout.read()
        return ""


def find_cell(cells, **conds):
    """在 /api/cells 结果里找格子；conds 如 region='北京' 或 region=None。"""
    for c in cells:
        if all(c[k] == v for k, v in conds.items()):
            return c
    return None


def show_cell(title, c):
    if c is None:
        print(f"    {title}: <格子不存在>")
        return
    avg = "—" if c["avg_cents"] is None else f"{c['avg_cents']/100:,.2f}"
    total = "NULL" if c["sum_cents"] is None else f"{c['sum_cents']/100:,.2f}"
    print(f"    {title}: 笔数={c['sale_count']} 非空金额笔数={c['amount_count']} "
          f"合计={total} 均值={avg}")
