"""HTTP 接口层冒烟：校验路由装配与状态码语义（202 收容 / 409 冻结冲突 / 版本重放）。

使用应用默认的进程内 SQLite，批次号用 UUID 保证用例间互不干扰。
"""
import sys
import unittest
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi.testclient import TestClient

from app.main import app


class HttpLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.today = date.today()

    def _new_batch(self, *, harvested=False):
        uid = uuid.uuid4().hex[:8]
        pond = self.client.post("/api/ponds/", json={
            "name": f"P-{uid}", "area": 10, "water_depth": 2, "species": "鲈鱼",
        }).json()
        batch = self.client.post("/api/batches/", json={
            "batch_number": f"B-{uid}", "pond_id": pond["id"], "species": "鲈鱼",
            "stocking_date": str(self.today - timedelta(days=30)),
            "actual_harvest_date": str(self.today + timedelta(days=10)),
        }).json()
        return batch["id"]

    def test_full_lifecycle_over_http(self):
        c = self.client
        bid = self._new_batch()

        # 窗口内实时投喂 -> 200
        r = c.post("/api/feeding-records/", json={
            "batch_id": bid, "feeding_date": str(self.today - timedelta(days=1)),
            "feed_type": "配合料", "feed_quantity": 5.0})
        self.assertEqual(r.status_code, 200)

        # 越过收获日的销售 -> 202 并附复核项，数据不生效
        r = c.post("/api/harvest-sales/", json={
            "batch_id": bid, "sale_date": str(self.today + timedelta(days=30)),
            "weight": 20, "unit_price": 30})
        self.assertEqual(r.status_code, 202)
        review = r.json()["review_item"]
        self.assertEqual(review["reason"], "after_harvest")
        self.assertEqual(review["status"], "pending")

        # 未裁定前关闭 -> 409 并列出未决项
        r = c.post(f"/api/batches/{bid}/close/")
        self.assertEqual(r.status_code, 409)
        self.assertIn("pending_count", r.json()["detail"])

        # 拒绝该未决项后关闭成功
        r = c.post(f"/api/review-items/{review['id']}/resolve/",
                   json={"decision": "rejected", "actor": "结算员"})
        self.assertEqual(r.status_code, 200)
        r = c.post(f"/api/batches/{bid}/close/", params={"actor": "结算员"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"]["version"], 1)
        self.assertFalse(r.json()["idempotent"])

        # 重复关闭幂等：同一版本
        r2 = c.post(f"/api/batches/{bid}/close/")
        self.assertTrue(r2.json()["idempotent"])
        self.assertEqual(r2.json()["version"]["version_id"],
                         r.json()["version"]["version_id"])

        # 关闭后直接改批次冻结字段 -> 409
        self.assertEqual(c.put(f"/api/batches/{bid}/",
                               json={"status": "active"}).status_code, 409)

        # 关闭后修改记录 -> 202 进入更正流程
        records = c.get(f"/api/feeding-records/?batch_id={bid}").json()
        feeding_id = records[0]["id"]
        r = c.put(f"/api/feeding-records/{feeding_id}/",
                  json={"feed_quantity": 88.0})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["review_item"]["reason"], "batch_closed")
        item_id = r.json()["review_item"]["id"]

        # 版本列表 + v1 重放为旧值
        versions = c.get(f"/api/batches/{bid}/versions/").json()
        self.assertEqual([v["version"] for v in versions], [1])
        v1 = c.get(f"/api/batches/{bid}/versions/1/").json()
        self.assertEqual(v1["records"]["feeding"][0]["feed_quantity"], 5.0)

        # 批准更正 -> v2；默认分析取最新版，v1 重放不变
        r = c.post(f"/api/review-items/{item_id}/resolve/",
                   json={"decision": "approved"})
        self.assertEqual(r.json()["new_version"]["version"], 2)
        v2 = c.get(f"/api/batches/{bid}/versions/2/").json()
        self.assertEqual(v2["records"]["feeding"][0]["feed_quantity"], 88.0)
        analysis = c.get(f"/api/analysis/cycle/{bid}/").json()
        self.assertEqual(analysis["feed_total"], 88.0)
        replay_v1 = c.get(f"/api/analysis/cycle/{bid}/", params={"version": 1}).json()
        self.assertEqual(replay_v1["feed_total"], 5.0)

        # 复核队列默认只列 pending
        pending = c.get("/api/review-items/", params={"batch_id": bid}).json()
        self.assertEqual(pending, [])

    def test_outside_backfill_window_is_quarantined(self):
        c = self.client
        bid = self._new_batch()
        r = c.post("/api/cost-records/", json={
            "batch_id": bid, "cost_date": str(self.today - timedelta(days=30)),
            "cost_type": "feed", "amount": 100})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["review_item"]["reason"], "outside_backfill")


if __name__ == "__main__":
    unittest.main()
