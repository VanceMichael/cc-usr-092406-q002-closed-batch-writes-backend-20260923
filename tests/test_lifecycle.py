"""批次生命周期与业务记录写入一致性的行为测试。

覆盖：
- 跨日边界：六类记录按投苗日/实际收获日/补录窗口做一致校验；
- 关闭事务：幂等关闭、失败回滚后重试不产生第二份结算版本；
- 历史越界数据进入可恢复复核队列，分析/追溯默认排除、按版本重放；
- 关闭后更正流程（create/update/delete 经复核裁定生效）；
- 并发关闭与并发写入不出现部分成功；
- 子进程模拟重启后，结算版本、复核队列与重放结果保持一致。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

# 必须在导入 app 之前指向独立的临时数据库
_TMPDIR = tempfile.mkdtemp(prefix="aq_lifecycle_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/lifecycle.db"
os.environ["BACKFILL_WINDOW_DAYS"] = "7"
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app import lifecycle, models  # noqa: E402

client = TestClient(app)

#: 六类记录类型的测试参数：类型名 -> (路由, 业务日期字段, 额外必填字段)
RECORD_CASES = {
    "stocking": ("/api/stocking-records/", None, {"species": "鲈鱼", "quantity": 1000}),
    "feeding": ("/api/feeding-records/", "feeding_date", {"feed_type": "颗粒料", "feed_quantity": 5.0}),
    "water_quality": ("/api/water-quality-records/", "record_date", {"water_temperature": 25.0, "ph_value": 7.5}),
    "medication": ("/api/medication-records/", "medication_date", {"drug_name": "二氧化氯", "dosage": 1.0}),
    "cost": ("/api/cost-records/", "cost_date", {"cost_type": "feed", "amount": 100.0}),
    "harvest_sale": ("/api/harvest-sales/", "sale_date", {"weight": 50.0, "unit_price": 20.0}),
}

STOCKING_DATE = "2026-01-10"
HARVEST_DATE = "2026-03-01"
WINDOW_EDGE = "2026-03-08"       # 收获日 + 7 天补录窗口边界
WINDOW_OUTSIDE = "2026-03-09"    # 窗口外一天
BEFORE_STOCKING = "2026-01-09"


def make_pond():
    resp = client.post("/api/ponds/", json={
        "name": "pond-" + uuid.uuid4().hex[:8],
        "area": 10.0,
        "water_depth": 2.0,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def make_batch(pond_id, harvest=None):
    payload = {
        "batch_number": "B-" + uuid.uuid4().hex[:8],
        "pond_id": pond_id,
        "species": "鲈鱼",
        "stocking_date": STOCKING_DATE,
    }
    if harvest:
        payload["actual_harvest_date"] = harvest
    resp = client.post("/api/batches/", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def record_payload(kind, batch_id, biz_date=None, **extra):
    url, date_field, defaults = RECORD_CASES[kind]
    payload = {"batch_id": batch_id, **defaults, **extra}
    if date_field is not None:
        payload[date_field] = biz_date or STOCKING_DATE
    return url, payload


def create_record(kind, batch_id, biz_date=None, **extra):
    url, payload = record_payload(kind, batch_id, biz_date, **extra)
    return client.post(url, json=payload)


def close_batch(batch_id, **body):
    return client.post(f"/api/batches/{batch_id}/close", json=body or None)


class CrossDayBoundaryTest(unittest.TestCase):
    """跨日边界：所有记录类型按同一套规则校验业务发生日。"""

    def test_business_date_boundaries_are_enforced_for_all_record_types(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id, harvest=HARVEST_DATE)
        for kind, (_, date_field, _) in RECORD_CASES.items():
            with self.subTest(kind=kind):
                if date_field is None:
                    # 投苗记录无独立业务日期，批次未关闭即可写入
                    resp = create_record(kind, batch_id)
                    self.assertEqual(resp.status_code, 200, resp.text)
                    continue
                resp = create_record(kind, batch_id, BEFORE_STOCKING)
                self.assertEqual(resp.status_code, 400, f"{kind} 早于投苗日应拒绝: {resp.text}")
                resp = create_record(kind, batch_id, STOCKING_DATE)
                self.assertEqual(resp.status_code, 200, f"{kind} 投苗日当天应允许: {resp.text}")
                resp = create_record(kind, batch_id, HARVEST_DATE)
                self.assertEqual(resp.status_code, 200, f"{kind} 收获日当天应允许: {resp.text}")
                resp = create_record(kind, batch_id, WINDOW_EDGE)
                self.assertEqual(resp.status_code, 200, f"{kind} 补录窗口边界应允许: {resp.text}")
                resp = create_record(kind, batch_id, WINDOW_OUTSIDE)
                self.assertEqual(resp.status_code, 400, f"{kind} 超出补录窗口应拒绝: {resp.text}")

    def test_update_crossing_boundary_is_rejected(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id, harvest=HARVEST_DATE)
        resp = create_record("feeding", batch_id, "2026-02-01")
        self.assertEqual(resp.status_code, 200)
        record_id = resp.json()["id"]

        resp = client.put(f"/api/feeding-records/{record_id}/", json={"feeding_date": BEFORE_STOCKING})
        self.assertEqual(resp.status_code, 400)
        resp = client.put(f"/api/feeding-records/{record_id}/", json={"feeding_date": WINDOW_OUTSIDE})
        self.assertEqual(resp.status_code, 400)
        resp = client.put(f"/api/feeding-records/{record_id}/", json={"feeding_date": WINDOW_EDGE})
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = client.delete(f"/api/feeding-records/{record_id}/")
        self.assertEqual(resp.status_code, 200)

    def test_writes_after_close_are_rejected_for_all_record_types(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id, harvest=HARVEST_DATE)
        existing = create_record("feeding", batch_id, "2026-02-01").json()["id"]
        resp = close_batch(batch_id, request_id="boundary-close")
        self.assertEqual(resp.status_code, 200)

        for kind, (url, date_field, _) in RECORD_CASES.items():
            with self.subTest(kind=kind):
                resp = create_record(kind, batch_id, "2026-02-15")
                self.assertEqual(resp.status_code, 409, f"{kind} 关闭后直写应 409: {resp.text}")

        resp = client.put(f"/api/feeding-records/{existing}/", json={"feed_quantity": 9.0})
        self.assertEqual(resp.status_code, 409)
        resp = client.delete(f"/api/feeding-records/{existing}/")
        self.assertEqual(resp.status_code, 409)
        resp = client.put(f"/api/batches/{batch_id}/", json={"species": "草鱼"})
        self.assertEqual(resp.status_code, 409)
        resp = client.delete(f"/api/batches/{batch_id}/")
        self.assertEqual(resp.status_code, 409)

    def test_batch_status_cannot_be_closed_via_plain_update(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id)
        resp = client.put(f"/api/batches/{batch_id}/", json={"status": "closed"})
        self.assertEqual(resp.status_code, 400)
        resp = client.put(f"/api/batches/{batch_id}/", json={"status": "harvested"})
        self.assertEqual(resp.status_code, 200)


class CloseIdempotencyTest(unittest.TestCase):
    """关闭幂等：重复关闭返回原结果，失败重试不产生第二份结算版本。"""

    def test_repeated_close_returns_original_settlement(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id, harvest=HARVEST_DATE)
        create_record("feeding", batch_id, "2026-02-01", feed_quantity=10.0)

        first = close_batch(batch_id, request_id="idem-1", closed_by="settler")
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["already_closed"])
        settlement = first.json()["settlement"]
        self.assertEqual(settlement["version_no"], 1)
        self.assertEqual(settlement["request_id"], "idem-1")

        # 相同 request_id 重试
        second = close_batch(batch_id, request_id="idem-1", closed_by="settler")
        # 不同 request_id 重复关闭
        third = close_batch(batch_id, request_id="idem-2")
        # 不带请求体的重复关闭
        fourth = close_batch(batch_id)
        for repeated in (second, third, fourth):
            self.assertEqual(repeated.status_code, 200)
            body = repeated.json()
            self.assertTrue(body["already_closed"])
            self.assertEqual(body["settlement"]["id"], settlement["id"])
            self.assertEqual(body["settlement"]["request_id"], "idem-1")
            self.assertEqual(body["settlement"]["closed_at"], settlement["closed_at"])
            self.assertEqual(body["settlement"]["snapshot"], settlement["snapshot"])

        db = SessionLocal()
        try:
            count = db.query(models.SettlementVersion).filter_by(batch_id=batch_id).count()
            self.assertEqual(count, 1)
        finally:
            db.close()

        resp = client.get(f"/api/batches/{batch_id}/settlement")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], settlement["id"])

    def test_failed_close_rolls_back_and_retry_creates_single_version(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id)
        create_record("feeding", batch_id, STOCKING_DATE, feed_quantity=3.0)

        no_raise = TestClient(app, raise_server_exceptions=False)
        original = lifecycle._insert_settlement

        def boom(*args, **kwargs):
            raise RuntimeError("模拟状态翻转后崩溃")

        lifecycle._insert_settlement = boom
        try:
            resp = no_raise.post(f"/api/batches/{batch_id}/close", json={"request_id": "fail-req"})
            self.assertEqual(resp.status_code, 500)
        finally:
            lifecycle._insert_settlement = original

        # 事务整体回滚：批次仍是 active，没有遗留结算版本
        resp = client.get(f"/api/batches/{batch_id}/")
        self.assertEqual(resp.json()["status"], "active")
        db = SessionLocal()
        try:
            self.assertEqual(
                db.query(models.SettlementVersion).filter_by(batch_id=batch_id).count(), 0
            )
        finally:
            db.close()

        # 重试成功，且只有一份结算版本
        resp = close_batch(batch_id, request_id="fail-req")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["already_closed"])
        db = SessionLocal()
        try:
            versions = db.query(models.SettlementVersion).filter_by(batch_id=batch_id).all()
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0].request_id, "fail-req")
        finally:
            db.close()


class QuarantineAndReviewTest(unittest.TestCase):
    """历史越界数据在关闭时进入复核队列；分析默认排除、裁定后恢复、版本可重放。"""

    def _batch_with_violations(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id)  # 先不登记收获日
        create_record("feeding", batch_id, "2026-02-01", feed_type="IN", feed_quantity=10.0)
        create_record("feeding", batch_id, "2026-03-20", feed_type="LATE", feed_quantity=99.0)
        # 模拟修复前遗留的越界数据（早于投苗日），直接落库
        db = SessionLocal()
        try:
            db.add(models.FeedingRecord(
                batch_id=batch_id,
                feeding_date=date(2026, 1, 1),
                feed_type="LEGACY",
                feed_quantity=50.0,
            ))
            db.commit()
        finally:
            db.close()
        # 登记实际收获日后，LATE 与 LEGACY 都成为历史越界数据
        resp = client.put(f"/api/batches/{batch_id}/", json={"actual_harvest_date": HARVEST_DATE})
        assert resp.status_code == 200, resp.text
        return batch_id

    def test_close_quarantines_violations_and_analysis_excludes_pending(self):
        batch_id = self._batch_with_violations()

        resp = close_batch(batch_id, request_id="quar-1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["settlement"]["quarantined_count"], 2)

        # 复核队列：两条越界记录待裁定
        resp = client.get(f"/api/review-queue/?batch_id={batch_id}&status=pending")
        items = resp.json()
        self.assertEqual(len(items), 2)
        self.assertEqual({item["operation"] for item in items}, {"quarantine"})
        self.assertEqual({item["reason"] for item in items}, {"out_of_cycle_bounds"})

        # 分析默认排除未裁定记录：只剩 IN 的 10kg
        resp = client.get(f"/api/analysis/cycle/{batch_id}/")
        self.assertEqual(resp.json()["feed_total"], 10.0)
        # include_review=true 可见全量
        resp = client.get(f"/api/analysis/cycle/{batch_id}/?include_review=true")
        self.assertEqual(resp.json()["feed_total"], 159.0)
        # 追溯同样默认排除
        resp = client.get(f"/api/analysis/traceability/{batch_id}/")
        self.assertEqual(len(resp.json()["feeding_records"]), 1)
        resp = client.get(f"/api/analysis/traceability/{batch_id}/?include_review=true")
        self.assertEqual(len(resp.json()["feeding_records"]), 3)

        # 记录列表可见复核状态
        resp = client.get(f"/api/feeding-records/?batch_id={batch_id}&review_status=pending")
        self.assertEqual(len(resp.json()), 2)

    def test_decision_recovers_or_excludes_records_and_replay_stays_frozen(self):
        batch_id = self._batch_with_violations()
        close_batch(batch_id, request_id="quar-2")

        frozen = client.get(f"/api/analysis/cycle/{batch_id}/?version=1").json()
        self.assertEqual(frozen["feed_total"], 10.0)
        self.assertEqual(frozen["version_no"], 1)
        self.assertIsNotNone(frozen["frozen_at"])

        items = client.get(f"/api/review-queue/?batch_id={batch_id}&status=pending").json()
        by_type = {}
        for item in items:
            by_type.setdefault(item["payload"]["feed_type"], item)

        # 裁定通过 LATE：恢复进分析
        late_id = by_type["LATE"]["id"]
        resp = client.post(f"/api/review-queue/{late_id}/decision", json={"decision": "approved", "decided_by": "auditor"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "approved")
        resp = client.get(f"/api/analysis/cycle/{batch_id}/")
        self.assertEqual(resp.json()["feed_total"], 109.0)

        # 裁定驳回 LEGACY：持续排除
        legacy_id = by_type["LEGACY"]["id"]
        resp = client.post(f"/api/review-queue/{legacy_id}/decision", json={"decision": "rejected", "decided_by": "auditor"})
        self.assertEqual(resp.status_code, 200)
        resp = client.get(f"/api/analysis/cycle/{batch_id}/")
        self.assertEqual(resp.json()["feed_total"], 109.0)

        # 已裁定项目不能重复裁定
        resp = client.post(f"/api/review-queue/{legacy_id}/decision", json={"decision": "approved"})
        self.assertEqual(resp.status_code, 409)

        # 冻结版本不受裁定影响，可随时重放
        replay = client.get(f"/api/analysis/cycle/{batch_id}/?version=1").json()
        self.assertEqual(replay["feed_total"], 10.0)
        replay_trace = client.get(f"/api/analysis/traceability/{batch_id}/?version=1").json()
        self.assertEqual(len(replay_trace["feeding_records"]), 1)
        self.assertEqual(replay_trace["version_no"], 1)


class CorrectionFlowTest(unittest.TestCase):
    """关闭后的更正流程：登记复核队列，裁定后才生效。"""

    def _closed_batch(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id, harvest=HARVEST_DATE)
        create_record("feeding", batch_id, "2026-02-01", feed_type="BASE", feed_quantity=10.0)
        create_record("cost", batch_id, "2026-02-01", amount=500.0)
        close_batch(batch_id, request_id="corr-" + uuid.uuid4().hex[:8])
        return batch_id

    def test_correction_requires_closed_batch(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id)
        resp = client.post(f"/api/batches/{batch_id}/corrections", json={
            "record_type": "feeding", "operation": "create",
            "payload": {"feeding_date": "2026-02-01", "feed_type": "X", "feed_quantity": 1},
        })
        self.assertEqual(resp.status_code, 400)

    def test_create_correction_lifecycle(self):
        batch_id = self._closed_batch()
        before = client.get(f"/api/analysis/cycle/{batch_id}/").json()["feed_total"]

        resp = client.post(f"/api/batches/{batch_id}/corrections", json={
            "record_type": "feeding",
            "operation": "create",
            "payload": {"feeding_date": "2026-03-25", "feed_type": "POST", "feed_quantity": 7.0},
            "submitted_by": "keeper",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        item = resp.json()
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["reason"], "post_close_correction")

        # 待裁定的更正不参与分析
        resp = client.get(f"/api/analysis/cycle/{batch_id}/")
        self.assertEqual(resp.json()["feed_total"], before)

        # 裁定通过后生效
        resp = client.post(f"/api/review-queue/{item['id']}/decision", json={"decision": "approved", "decided_by": "manager"})
        self.assertEqual(resp.status_code, 200)
        resp = client.get(f"/api/analysis/cycle/{batch_id}/")
        self.assertEqual(resp.json()["feed_total"], before + 7.0)

        # 冻结版本仍保持关闭时点的结果
        replay = client.get(f"/api/analysis/cycle/{batch_id}/?version=1").json()
        self.assertEqual(replay["feed_total"], before)

    def test_update_and_delete_corrections(self):
        batch_id = self._closed_batch()
        target = client.get(f"/api/feeding-records/?batch_id={batch_id}").json()[0]

        resp = client.post(f"/api/batches/{batch_id}/corrections", json={
            "record_type": "feeding",
            "operation": "update",
            "record_id": target["id"],
            "payload": {"feed_quantity": 42.0},
        })
        self.assertEqual(resp.status_code, 200)
        item = resp.json()
        resp = client.post(f"/api/review-queue/{item['id']}/decision", json={"decision": "approved"})
        self.assertEqual(resp.status_code, 200)
        resp = client.get(f"/api/feeding-records/{target['id']}/")
        self.assertEqual(resp.json()["feed_quantity"], 42.0)

        resp = client.post(f"/api/batches/{batch_id}/corrections", json={
            "record_type": "feeding",
            "operation": "delete",
            "record_id": target["id"],
        })
        self.assertEqual(resp.status_code, 200)
        item = resp.json()
        resp = client.post(f"/api/review-queue/{item['id']}/decision", json={"decision": "approved"})
        self.assertEqual(resp.status_code, 200)
        resp = client.get(f"/api/feeding-records/{target['id']}/")
        self.assertEqual(resp.status_code, 404)

    def test_rejected_create_correction_stays_excluded(self):
        batch_id = self._closed_batch()
        before = client.get(f"/api/analysis/cycle/{batch_id}/").json()["feed_total"]
        resp = client.post(f"/api/batches/{batch_id}/corrections", json={
            "record_type": "feeding",
            "operation": "create",
            "payload": {"feeding_date": "2026-03-25", "feed_type": "REJ", "feed_quantity": 8.0},
        })
        item = resp.json()
        resp = client.post(f"/api/review-queue/{item['id']}/decision", json={"decision": "rejected"})
        self.assertEqual(resp.status_code, 200)
        resp = client.get(f"/api/analysis/cycle/{batch_id}/")
        self.assertEqual(resp.json()["feed_total"], before)

    def test_invalid_correction_requests(self):
        batch_id = self._closed_batch()
        resp = client.post(f"/api/batches/{batch_id}/corrections", json={
            "record_type": "unknown", "operation": "create", "payload": {},
        })
        self.assertEqual(resp.status_code, 400)
        resp = client.post(f"/api/batches/{batch_id}/corrections", json={
            "record_type": "feeding", "operation": "create",
            "payload": {"feeding_date": "2026-03-25"},  # 缺少必填字段
        })
        self.assertEqual(resp.status_code, 400)
        resp = client.post(f"/api/batches/{batch_id}/corrections", json={
            "record_type": "feeding", "operation": "update", "payload": {"feed_quantity": 1},
        })
        self.assertEqual(resp.status_code, 400)


class ConcurrencyCloseTest(unittest.TestCase):
    """并发关闭与并发写入：不允许部分成功，结算版本唯一。"""

    def test_concurrent_close_and_writes_have_no_partial_success(self):
        pond_id = make_pond()
        batch_id = make_batch(pond_id, harvest=HARVEST_DATE)
        update_target = create_record("feeding", batch_id, "2026-02-01", feed_type="UPD", feed_quantity=1.0).json()["id"]
        delete_target = create_record("feeding", batch_id, "2026-02-02", feed_type="DEL", feed_quantity=1.0).json()["id"]

        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(9)

        def record(tag, resp):
            try:
                body = resp.json()
            except Exception:
                body = None
            with lock:
                results.append((tag, resp.status_code, body))

        def closer(i):
            c = TestClient(app)
            barrier.wait()
            record(f"close-{i}", c.post(f"/api/batches/{batch_id}/close", json={"request_id": f"race-{i}"}))

        def creator(i):
            c = TestClient(app)
            barrier.wait()
            record(f"create-{i}", c.post("/api/feeding-records/", json={
                "batch_id": batch_id, "feeding_date": "2026-02-10",
                "feed_type": f"RACE-{i}", "feed_quantity": 2.0,
            }))

        def updater():
            c = TestClient(app)
            barrier.wait()
            record("update", c.put(f"/api/feeding-records/{update_target}/", json={"feed_quantity": 5.0}))

        def deleter():
            c = TestClient(app)
            barrier.wait()
            record("delete", c.delete(f"/api/feeding-records/{delete_target}/"))

        threads = [threading.Thread(target=closer, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=creator, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=updater), threading.Thread(target=deleter)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for tag, status, body in results:
            self.assertLess(status, 500, f"{tag} -> {status}: {body}")

        closes = [body for tag, _, body in results if tag.startswith("close-")]
        self.assertEqual(len(closes), 3)
        # 所有并发关闭返回同一份结算版本，且只有一个是“新关闭”
        self.assertEqual(len({c["settlement"]["id"] for c in closes}), 1)
        self.assertEqual({c["settlement"]["version_no"] for c in closes}, {1})
        self.assertEqual(sum(1 for c in closes if not c["already_closed"]), 1)

        db = SessionLocal()
        try:
            self.assertEqual(
                db.query(models.SettlementVersion).filter_by(batch_id=batch_id).count(), 1
            )
            batch = db.get(models.Batch, batch_id)
            self.assertEqual(batch.status, "closed")
            # 新增：成功则记录必须存在，被拒则记录必须不存在（无部分成功）
            for i in range(4):
                status = next(s for tag, s, _ in results if tag == f"create-{i}")
                exists = db.query(models.FeedingRecord).filter_by(
                    batch_id=batch_id, feed_type=f"RACE-{i}"
                ).count() == 1
                if status == 200:
                    self.assertTrue(exists, f"create-{i} 返回成功但记录缺失")
                else:
                    self.assertEqual(status, 409)
                    self.assertFalse(exists, f"create-{i} 被拒绝但记录已写入")
            # 修改/删除：要么完整生效，要么被 409 拒绝
            self.assertIn(next(s for tag, s, _ in results if tag == "update"), (200, 409))
            self.assertIn(next(s for tag, s, _ in results if tag == "delete"), (200, 409))
        finally:
            db.close()


RESTART_SCRIPT_A = r"""
import json
from datetime import date
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import FeedingRecord

c = TestClient(app)
c.post('/api/ponds/', json={'name': 'RP', 'area': 10.0, 'water_depth': 2.0})
c.post('/api/batches/', json={
    'batch_number': 'RB', 'pond_id': 1, 'species': '鲈鱼',
    'stocking_date': '2026-01-10', 'actual_harvest_date': '2026-03-01',
})
c.post('/api/feeding-records/', json={
    'batch_id': 1, 'feeding_date': '2026-02-01', 'feed_type': 'IN', 'feed_quantity': 10.0,
})
db = SessionLocal()
db.add(FeedingRecord(batch_id=1, feeding_date=date(2026, 3, 20), feed_type='LEGACY', feed_quantity=99.0))
db.commit()
db.close()
r = c.post('/api/batches/1/close', json={'request_id': 'restart-req-1', 'closed_by': 'settler'})
body = r.json()
print(json.dumps({
    'status': r.status_code,
    'settlement_id': body['settlement']['id'],
    'version_no': body['settlement']['version_no'],
    'closed_at': body['settlement']['closed_at'],
    'quarantined': body['settlement']['quarantined_count'],
    'feed_total': body['settlement']['snapshot']['cycle']['feed_total'],
}))
"""

RESTART_SCRIPT_B = r"""
import json
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import SettlementVersion

c = TestClient(app)
out = {}
r1 = c.post('/api/batches/1/close', json={'request_id': 'restart-req-1', 'closed_by': 'settler'})
out['reclose_same'] = [r1.status_code, r1.json()['already_closed'], r1.json()['settlement']['id']]
r2 = c.post('/api/batches/1/close', json={'request_id': 'another-req'})
out['reclose_other'] = [r2.status_code, r2.json()['settlement']['id'], r2.json()['settlement']['request_id']]
r3 = c.get('/api/analysis/cycle/1/?version=1')
out['replay'] = [r3.status_code, r3.json()['feed_total'], r3.json()['version_no']]
r4 = c.get('/api/review-queue/?batch_id=1&status=pending')
out['pending'] = len(r4.json())
r5 = c.post('/api/feeding-records/', json={
    'batch_id': 1, 'feeding_date': '2026-02-02', 'feed_type': 'X', 'feed_quantity': 1.0,
})
out['post_close_write'] = r5.status_code
items = r4.json()
if items:
    r6 = c.post('/api/review-queue/%s/decision' % items[0]['id'],
                json={'decision': 'approved', 'decided_by': 'auditor'})
    out['decision'] = [r6.status_code, r6.json()['status']]
    out['live_after_approve'] = c.get('/api/analysis/cycle/1/').json()['feed_total']
db = SessionLocal()
out['settlement_count'] = db.query(SettlementVersion).count()
db.close()
print(json.dumps(out))
"""


class RestartRecoveryTest(unittest.TestCase):
    """重启恢复：新进程读取同一数据库，结算版本、复核队列与重放结果一致。"""

    def test_close_and_review_survive_restart(self):
        workdir = tempfile.mkdtemp(prefix="aq_restart_")
        db_file = Path(workdir) / "restart.db"
        env = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{db_file}",
            "PYTHONPATH": str(BACKEND),
        }

        proc_a = subprocess.run(
            [sys.executable, "-c", RESTART_SCRIPT_A],
            cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc_a.returncode, 0, proc_a.stderr)
        result_a = json.loads(proc_a.stdout.strip().splitlines()[-1])
        self.assertEqual(result_a["status"], 200)
        self.assertEqual(result_a["version_no"], 1)
        self.assertEqual(result_a["quarantined"], 1)
        self.assertEqual(result_a["feed_total"], 10.0)

        # 全新进程（模拟服务重启）读取同一 SQLite 文件
        proc_b = subprocess.run(
            [sys.executable, "-c", RESTART_SCRIPT_B],
            cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc_b.returncode, 0, proc_b.stderr)
        result_b = json.loads(proc_b.stdout.strip().splitlines()[-1])

        # 重复关闭返回原结算版本，重试不产生第二份
        self.assertEqual(result_b["reclose_same"], [200, True, result_a["settlement_id"]])
        self.assertEqual(result_b["reclose_other"], [200, result_a["settlement_id"], "restart-req-1"])
        self.assertEqual(result_b["settlement_count"], 1)
        # 冻结版本可重放，复核队列在重启后仍可裁定
        self.assertEqual(result_b["replay"], [200, 10.0, 1])
        self.assertEqual(result_b["pending"], 1)
        self.assertEqual(result_b["post_close_write"], 409)
        self.assertEqual(result_b["decision"], [200, "approved"])
        self.assertEqual(result_b["live_after_approve"], 109.0)


if __name__ == "__main__":
    unittest.main()
