"""确定性种子数据：覆盖真实空值、名为“全部”的真实地区、空金额等边界情形。"""
import random

from app.db import now_iso

# 前置的精心构造样本，保证边界情形一定出现
CRAFTED = [
    # region, channel, category, amount_cents
    ("全部", "线上", "家电", 12800),      # 真实地区名就叫“全部”
    ("全部", "线下", "食品", 6600),
    (None, "线上", "数码", 99900),        # 地区为空（真实 NULL）
    (None, "电话", "服装", None),         # 地区空 + 金额空
    ("北京", None, "家电", 4500),         # 渠道为空
    ("上海", "线上", None, 7800),         # 类别为空
    (None, None, None, None),             # 三个维度全空 + 金额空
    ("广州", "线下", "食品", None),       # 金额空
]

REGIONS = ["北京", "上海", "广州", "深圳", "杭州", "全部", None]
CHANNELS = ["线上", "线下", "电话", None]
CATEGORIES = ["家电", "食品", "服装", "数码", None]


def seed_sales(conn, n: int = 240, seed: int = 20231123):
    rng = random.Random(seed)
    rows = list(CRAFTED)
    while len(rows) < n:
        amount = None if rng.random() < 0.08 else rng.randint(500, 200000)
        rows.append((rng.choice(REGIONS), rng.choice(CHANNELS), rng.choice(CATEGORIES), amount))
    ts = now_iso()
    conn.executemany(
        "INSERT INTO sales (region, channel, category, amount_cents, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        [(r, c, k, a, ts, ts) for r, c, k, a in rows],
    )
    return len(rows)
