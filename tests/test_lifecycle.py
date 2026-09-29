"""批次生命周期与业务记录写入一致性测试。

覆盖：
- 六种记录类型基于 批次状态/投苗日/实际收获日/业务发生日 的一致校验与跨日边界；
- 关闭前补录窗口与关闭后更正流程的区分；
- 关闭事务确认未决写入、冻结结算版本、幂等关闭、失败重试不产生第二份版本；
- 关闭与新增/修改/删除并发时不出现部分成功（确定性加锁顺序 + 随机交错）；
- 文件库重启恢复、历史越界数据迁移进可恢复复核队列（迁移幂等）；
- 分析与追溯默认排除未裁定记录，并保留按版本重放能力。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import itertools
from datetime import date, datetime, timedelta
from pathlib import Path

_counter = itertools.count(1)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy.orm import sessionmaker

from app.database import create_sqlite_engines
from app.models import (
    Pond, Batch, FeedingRecord, MedicationRecord,
    ReviewItem, SettlementVersion,
    LIFECYCLE_ACTIVE, LIFECYCLE_FLAGGED,
)
from app.services import lifecycle
from app.services.lifecycle import LifecycleError


# --------------------------------------------------------------------------- 测试夹具

def make_env(db_url="sqlite:///:memory:"):
    """返回一组隔离的引擎与会话工厂，并完成建表。"""
    reader, writer = create_sqlite_engines(db_url)
    lifecycle.ensure_schema(reader)
    rsession = sessionmaker(bind=writer if ":memory:" in db_url else reader,
                            autocommit=False, autoflush=False)
    wsession = sessionmaker(bind=writer, autocommit=False, autoflush=False)
    return {
        "url": db_url, "reader": reader, "writer": writer,
        "read": rsession, "write": wsession,
    }


def seed_batch(env, *, stocking=None, harvest=None, status="active"):
    stocking = stocking or date(2026, 9, 1)
    db = env["write"]()
    pond = Pond(name=f"P-{os.getpid()}-{id(env)}-{next(_counter)}", area=10,
                water_depth=2, species="鲈鱼")
    db.add(pond); db.commit(); db.refresh(pond)
    batch = Batch(batch_number=f"B-{pond.id}-{next(_counter)}",
                  pond_id=pond.id, species="鲈鱼", stocking_date=stocking,
                  actual_harvest_date=harvest, status=status)
    db.add(batch); db.commit(); db.refresh(batch)
    ids = (batch.id, pond.id)
    db.close()
    return ids


# --------------------------------------------------------------------------- 测试夹具（续）


def payload(kind, bid, day, **extra):
    base = {
        "feeding": {"batch_id": bid, "feeding_date": day, "feed_type": "配合料", "feed_quantity": 10.0},
        "water_quality": {"batch_id": bid, "record_date": day, "ph_value": 7.2},
        "medication": {"batch_id": bid, "medication_date": day, "drug_name": "碘制剂", "dosage": 1.0},
        "cost": {"batch_id": bid, "cost_date": day, "cost_type": "feed", "amount": 100.0},
        "harvest_sale": {"batch_id": bid, "sale_date": day, "weight": 50.0, "unit_price": 20.0},
        "stocking": {"batch_id": bid, "species": "鲈鱼苗", "quantity": 1000},
    }[kind]
    base.update(extra)
    return base


DATED_TYPES = ["feeding", "water_quality", "medication", "cost", "harvest_sale"]


# --------------------------------------------------------------------------- 边界校验

class BoundaryValidationTest(unittest.TestCase):
    TODAY = date(2026, 9, 29)

    def setUp(self):
        self.env = make_env()

    def _batch(self, stocking, harvest, status="active"):
        bid, _ = seed_batch(self.env, stocking=stocking, harvest=harvest, status=status)
        return bid

    def _submit(self, bid, kind, day, **extra):
        return lifecycle.submit_record(
            self.env["write"], kind, lifecycle.OP_CREATE,
            payload(kind, bid, day, **extra), today=self.TODAY)

    def test_window_boundaries_for_every_dated_type(self):
        # 养殖周期覆盖测试日，专注验证补录窗口：含第 7 天，第 8 天收容
        bid = self._batch(date(2026, 8, 1), date(2026, 10, 31))
        for kind in DATED_TYPES:
            with self.subTest(kind=kind, edge="window_inclusive_7_days"):
                outcome, _ = self._submit(bid, kind, self.TODAY - timedelta(days=7))
                self.assertEqual(outcome, "applied")
            with self.subTest(kind=kind, edge="window_exceeded_8_days"):
                outcome, item = self._submit(bid, kind, self.TODAY - timedelta(days=8))
                self.assertEqual(outcome, "quarantined")
                self.assertEqual(item.reason, lifecycle.REASON_OUTSIDE_BACKFILL)
                self.assertEqual(item.status, lifecycle.REVIEW_PENDING)

    def test_future_and_today_are_live_uploads_not_backfill(self):
        bid = self._batch(date(2026, 8, 1), date(2026, 10, 31))
        for kind in DATED_TYPES:
            self.assertEqual(self._submit(bid, kind, self.TODAY)[0], "applied", f"当天 {kind}")
            self.assertEqual(self._submit(bid, kind, self.TODAY + timedelta(days=2))[0],
                             "applied", f"未来 {kind}")

    def test_before_stocking_boundary_is_inclusive(self):
        # 投苗日前一天收容、投苗日当天允许；用靠近今天的投苗日避免窗口干扰
        stocking = self.TODAY - timedelta(days=2)
        bid = self._batch(stocking, date(2026, 10, 31))
        for kind in DATED_TYPES:
            outcome, item = self._submit(bid, kind, stocking - timedelta(days=1))
            self.assertEqual(outcome, "quarantined", kind)
            self.assertEqual(item.reason, lifecycle.REASON_BEFORE_STOCKING, kind)
            self.assertEqual(self._submit(bid, kind, stocking)[0], "applied", kind)

    def test_after_harvest_boundary_is_inclusive(self):
        # 收获日=今天：当天允许，次日起收容（销售与成本也不例外）
        bid = self._batch(date(2026, 8, 1), self.TODAY)
        for kind in DATED_TYPES:
            self.assertEqual(self._submit(bid, kind, self.TODAY)[0], "applied", kind)
            outcome, item = self._submit(bid, kind, self.TODAY + timedelta(days=1))
            self.assertEqual(outcome, "quarantined", kind)
            self.assertEqual(item.reason, lifecycle.REASON_AFTER_HARVEST, kind)

    def test_stocking_record_exempt_from_backfill_window(self):
        # 投苗记录业务日即投苗锚点：不受补录窗口限制，但仍受状态约束
        bid = self._batch(date(2026, 8, 1), date(2026, 10, 31))
        outcome, _ = lifecycle.submit_record(
            self.env["write"], "stocking", lifecycle.OP_CREATE,
            payload("stocking", bid, None, quantity=500), today=self.TODAY)
        self.assertEqual(outcome, "applied")

    def test_closed_batch_sends_every_type_to_correction_queue(self):
        bid = self._batch(date(2026, 8, 1), date(2026, 10, 31))
        lifecycle.close_batch(self.env["write"], bid)
        for kind in DATED_TYPES + ["stocking"]:
            day = None if kind == "stocking" else self.TODAY
            outcome, item = lifecycle.submit_record(
                self.env["write"], kind, lifecycle.OP_CREATE,
                payload(kind, bid, day), today=self.TODAY)
            self.assertEqual(outcome, "quarantined", kind)
            self.assertEqual(item.reason, lifecycle.REASON_BATCH_CLOSED, kind)

    def _pending_ids(self, bid):
        db = self.env["write"]()
        ids = [x.id for x in db.query(ReviewItem).filter(
            ReviewItem.batch_id == bid, ReviewItem.status == "pending").all()]
        db.close()
        return ids

    def test_update_and_delete_share_same_guard(self):
        bid = self._batch(date(2026, 8, 1), date(2026, 10, 31))
        _, row = self._submit(bid, "feeding", self.TODAY - timedelta(days=1))
        # 改为收获日之后 -> 收容，原数据保持不变
        outcome, item = lifecycle.submit_record(
            self.env["write"], "feeding", lifecycle.OP_UPDATE,
            {"feeding_date": date(2026, 11, 5)}, record_id=row.id, today=self.TODAY)
        self.assertEqual(outcome, "quarantined")
        self.assertEqual(item.reason, lifecycle.REASON_AFTER_HARVEST)
        db = self.env["read"]()
        self.assertEqual(db.get(FeedingRecord, row.id).feeding_date,
                         self.TODAY - timedelta(days=1))
        db.close()

        lifecycle.close_batch(self.env["write"], bid, confirm_pending=True)
        # 关闭后删除 -> 收容而非删除
        outcome, item = lifecycle.submit_record(
            self.env["write"], "feeding", lifecycle.OP_DELETE,
            record_id=row.id, today=self.TODAY)
        self.assertEqual(outcome, "quarantined")
        self.assertEqual(item.operation, lifecycle.OP_DELETE)
        self.assertEqual(item.reason, lifecycle.REASON_BATCH_CLOSED)
        db = self.env["read"]()
        self.assertIsNotNone(db.get(FeedingRecord, row.id))
        db.close()

    def test_quarantined_create_leaves_no_business_row_and_is_recoverable(self):
        bid = self._batch(date(2026, 8, 1), self.TODAY)
        outcome, item = self._submit(bid, "medication", self.TODAY + timedelta(days=3))
        self.assertEqual((outcome, item.reason),
                         ("quarantined", lifecycle.REASON_AFTER_HARVEST))
        db = self.env["read"]()
        self.assertEqual(db.query(MedicationRecord).filter_by(batch_id=bid).count(), 0)
        db.close()
        # 更正流程批准后恢复生效
        decision = lifecycle.resolve_review_item(
            self.env["write"], item.id, lifecycle.REVIEW_APPROVED)
        self.assertTrue(decision["effective_data_changed"])
        db = self.env["read"]()
        rows = db.query(MedicationRecord).filter_by(
            batch_id=bid, lifecycle_status=LIFECYCLE_ACTIVE).all()
        self.assertEqual(len(rows), 1)
        db.close()


# --------------------------------------------------------------------------- 关闭与结算版本

class CloseSettlementTest(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.today = date(2026, 9, 10)
        self.bid, _ = seed_batch(self.env, stocking=date(2026, 9, 1),
                                 harvest=date(2026, 9, 20))

    def _feed(self, day, qty=10.0):
        return lifecycle.submit_record(
            self.env["write"], "feeding", lifecycle.OP_CREATE,
            payload("feeding", self.bid, day, feed_quantity=qty), today=self.today)

    def test_close_freezes_version_and_reclose_is_idempotent(self):
        self._feed(date(2026, 9, 5), qty=3.0)
        result = lifecycle.close_batch(self.env["write"], self.bid, actor="结算员")
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["version"]["version"], 1)
        self.assertEqual(result["version"]["metrics"]["feed_total"], 3.0)

        # 重复关闭返回原结果（同一版本行，不产生第二份）
        again = lifecycle.close_batch(self.env["write"], self.bid)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["version"]["version_id"], result["version"]["version_id"])

        db = self.env["read"]()
        self.assertEqual(db.query(SettlementVersion).filter_by(batch_id=self.bid).count(), 1)
        self.assertEqual(db.get(Batch, self.bid).current_version, 1)
        db.close()

    def test_failed_close_then_retry_creates_single_version(self):
        # 制造一条未决项，关闭失败（409），重试不得生成任何版本
        outcome, item = lifecycle.submit_record(
            self.env["write"], "feeding", lifecycle.OP_CREATE,
            payload("feeding", self.bid, date(2026, 8, 30)), today=self.today)
        self.assertEqual(outcome, "quarantined")
        with self.assertRaises(LifecycleError) as ctx:
            lifecycle.close_batch(self.env["write"], self.bid)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(item.id, ctx.exception.extra["pending_review_item_ids"])

        db = self.env["read"]()
        self.assertEqual(db.query(SettlementVersion).filter_by(batch_id=self.bid).count(), 0)
        db.close()

        # 失败后带确认关闭：只生成一份版本
        result = lifecycle.close_batch(self.env["write"], self.bid, confirm_pending=True)
        self.assertEqual(result["version"]["version"], 1)
        self.assertEqual(result["pending_count"], 1)
        db = self.env["read"]()
        self.assertEqual(db.query(SettlementVersion).filter_by(batch_id=self.bid).count(), 1)
        db.close()

    def test_correction_flow_after_close_freezes_new_version(self):
        self._feed(date(2026, 9, 5), qty=3.0)
        lifecycle.close_batch(self.env["write"], self.bid)

        # 关闭后补写 -> 复核队列；批准恢复 -> v2
        outcome, item = lifecycle.submit_record(
            self.env["write"], "feeding", lifecycle.OP_CREATE,
            payload("feeding", self.bid, date(2026, 9, 6), feed_quantity=7.0),
            today=self.today)
        self.assertEqual((outcome, item.reason), ("quarantined", lifecycle.REASON_BATCH_CLOSED))
        decision = lifecycle.resolve_review_item(
            self.env["write"], item.id, lifecycle.REVIEW_APPROVED, actor="场区主管")
        self.assertTrue(decision["effective_data_changed"])
        self.assertEqual(decision["new_version"]["version"], 2)

        # 拒绝不改变生效集，不产生新版本
        outcome, item2 = lifecycle.submit_record(
            self.env["write"], "cost", lifecycle.OP_CREATE,
            payload("cost", self.bid, date(2026, 9, 7), amount=50.0), today=self.today)
        rejected = lifecycle.resolve_review_item(
            self.env["write"], item2.id, lifecycle.REVIEW_REJECTED)
        self.assertFalse(rejected["effective_data_changed"])
        self.assertIsNone(rejected["new_version"])

        # 已裁定项不能重复裁定
        with self.assertRaises(LifecycleError):
            lifecycle.resolve_review_item(
                self.env["write"], item.id, lifecycle.REVIEW_REJECTED)

        db = self.env["read"]()
        self.assertEqual(db.get(Batch, self.bid).current_version, 2)
        db.close()

    def test_sale_amount_recomputed_when_correction_updates_price(self):
        from app.models import HarvestSale
        # 关闭前先有一笔销售并关闭
        lifecycle.submit_record(
            self.env["write"], "harvest_sale", lifecycle.OP_CREATE,
            payload("harvest_sale", self.bid, date(2026, 9, 5),
                    weight=10.0, unit_price=20.0), today=self.today)
        db = self.env["read"]()
        sale_id = db.query(HarvestSale).filter_by(batch_id=self.bid).first().id
        db.close()
        lifecycle.close_batch(self.env["write"], self.bid)

        # 关闭后改单价 -> 收容；批准后总额联动重算并冻结 v2
        _, item = lifecycle.submit_record(
            self.env["write"], "harvest_sale", lifecycle.OP_UPDATE,
            {"unit_price": 30.0}, record_id=sale_id, today=self.today)
        self.assertEqual(item.reason, lifecycle.REASON_BATCH_CLOSED)
        decision = lifecycle.resolve_review_item(
            self.env["write"], item.id, lifecycle.REVIEW_APPROVED)
        self.assertEqual(decision["new_version"]["version"], 2)
        v2 = lifecycle.get_version_snapshot(self.env["read"], self.bid, 2)
        sale = v2["records"]["harvest_sale"][0]
        self.assertEqual(sale["unit_price"], 30.0)
        self.assertEqual(sale["total_amount"], 300.0)
        self.assertEqual(v2["metrics"]["total_revenue"], 300.0)

    def test_delete_correction_removes_row_in_new_version(self):
        _, row = self._feed(date(2026, 9, 5), qty=4.0)
        lifecycle.close_batch(self.env["write"], self.bid)
        outcome, item = lifecycle.submit_record(
            self.env["write"], "feeding", lifecycle.OP_DELETE,
            record_id=row.id, today=self.today)
        self.assertEqual(outcome, "quarantined")
        decision = lifecycle.resolve_review_item(
            self.env["write"], item.id, lifecycle.REVIEW_APPROVED)
        self.assertEqual(decision["new_version"]["version"], 2)
        v1 = lifecycle.get_version_snapshot(self.env["read"], self.bid, 1)
        v2 = lifecycle.get_version_snapshot(self.env["read"], self.bid, 2)
        self.assertEqual(len(v1["records"]["feeding"]), 1)
        self.assertEqual(len(v2["records"]["feeding"]), 0)


# --------------------------------------------------------------------------- 并发：关闭 vs 写入

class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self.tmp.name)/'conc.db'}"
        self.env = make_env(self.url)
        self.today = date(2026, 9, 10)

    def tearDown(self):
        self.env["reader"].dispose()
        self.env["writer"].dispose()
        self.tmp.cleanup()

    def _fresh_batch(self, index):
        bid, _ = seed_batch(self.env, stocking=date(2026, 9, 1),
                            harvest=date(2026, 9, 20))
        # 预置一条窗口内投喂
        lifecycle.submit_record(
            self.env["write"], "feeding", lifecycle.OP_CREATE,
            payload("feeding", bid, date(2026, 9, 5), feed_quantity=1.0),
            today=self.today)
        return bid

    def _assert_invariant(self, bid):
        """关闭完成后：唯一版本；生效集与冻结快照一致；不存在'生效但未被冻结'的写入。"""
        db = self.env["read"]()
        try:
            versions = db.query(SettlementVersion).filter_by(batch_id=bid).all()
            self.assertEqual(len(versions), 1, f"批次 {bid} 出现多份结算版本")
            self.assertEqual(db.get(Batch, bid).status, "closed")
            frozen = {r["id"] for r in lifecycle.loads(versions[0].snapshot_json)["records"]["feeding"]}
            live = {r.id for r in db.query(FeedingRecord).filter_by(
                batch_id=bid, lifecycle_status=LIFECYCLE_ACTIVE)}
            self.assertEqual(live, frozen,
                             f"批次 {bid} 关闭后生效集与冻结快照不一致（签署数据仍在变化）")
        finally:
            db.close()

    def test_record_first_then_close_freezes_inflight_write(self):
        """强制顺序：写入先持锁 → 关闭等待 → 提交后该行必须进入 v1。"""
        bid = self._fresh_batch(0)
        holding = threading.Event()
        errors = []

        def writer():
            sess = self.env["write"]()
            try:
                sess.query(Batch).filter_by(id=bid).first()  # 触发 BEGIN IMMEDIATE，持锁
                holding.set()
                time.sleep(0.3)
                sess.add(FeedingRecord(**payload("feeding", bid, date(2026, 9, 6),
                                                 feed_quantity=9.0)))
                sess.commit()
            except Exception as exc:  # pragma: no cover
                errors.append(exc); sess.rollback()
            finally:
                sess.close()

        result = {}
        def closer():
            holding.wait(1)
            time.sleep(0.1)  # 写入仍持锁，close 的 BEGIN IMMEDIATE 必须等待
            try:
                result["close"] = lifecycle.close_batch(self.env["write"], bid)
            except Exception as exc:  # pragma: no cover
                result["err"] = repr(exc)

        t = threading.Thread(target=writer)
        ct = threading.Thread(target=closer)
        t.start(); ct.start(); t.join(5); ct.join(15)
        self.assertFalse(errors)
        self.assertNotIn("err", result)
        self.assertEqual(result["close"]["version"]["version"], 1)
        snap = lifecycle.get_version_snapshot(self.env["read"], bid, 1)
        self.assertIn("2026-09-06",
                      [r["feeding_date"] for r in snap["records"]["feeding"]])
        self._assert_invariant(bid)

    def test_close_first_then_record_goes_to_review(self):
        """强制顺序：关闭先持锁 → 写入等待 → 关闭提交后写入必须被收容而非部分成功。"""
        bid = self._fresh_batch(1)
        close_holding = threading.Event()
        errors = []

        def closer():
            sess = self.env["write"]()
            try:
                batch = sess.query(Batch).filter_by(id=bid).first()  # 持写锁
                close_holding.set()
                time.sleep(0.4)  # 让写入在此期间阻塞在 BEGIN IMMEDIATE 上
                snap = lifecycle.build_snapshot(sess, batch)
                sess.add(SettlementVersion(batch_id=bid, version=1, trigger="close",
                                           snapshot_json=lifecycle.dumps(snap)))
                batch.status = "closed"
                batch.closed_at = datetime.utcnow()
                batch.current_version = 1
                sess.commit()
            except Exception as exc:  # pragma: no cover
                errors.append(exc); sess.rollback()
            finally:
                sess.close()

        outcome = {}
        def writer():
            close_holding.wait(1)
            time.sleep(0.1)  # 关闭已持锁后再发起写入
            try:
                o, item = lifecycle.submit_record(
                    self.env["write"], "feeding", lifecycle.OP_CREATE,
                    payload("feeding", bid, date(2026, 9, 7), feed_quantity=5.0),
                    today=self.today)
                outcome["result"] = (o, item.reason)
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        ct = threading.Thread(target=closer)
        wt = threading.Thread(target=writer)
        ct.start(); wt.start(); ct.join(15); wt.join(15)
        self.assertFalse(errors)
        self.assertEqual(outcome["result"], ("quarantined", lifecycle.REASON_BATCH_CLOSED))
        self._assert_invariant(bid)


    def test_randomized_interleavings_create_update_delete(self):
        """关闭与新增随机交错 N 轮：写入要么被冻结进 v1，要么被收容，绝不部分成功。"""
        rounds = 24

        def run_one(idx):
            bid = self._fresh_batch(100 + idx)
            barrier = threading.Event()

            def close_side():
                barrier.wait(2)
                lifecycle.close_batch(self.env["write"], bid, confirm_pending=True)

            def write_side():
                barrier.set()
                lifecycle.submit_record(
                    self.env["write"], "feeding", lifecycle.OP_CREATE,
                    payload("feeding", bid, date(2026, 9, 8), feed_quantity=2.0),
                    today=self.today)

            t1, t2 = threading.Thread(target=close_side), threading.Thread(target=write_side)
            t1.start(); t2.start(); t1.join(15); t2.join(15)
            self.assertFalse(t1.is_alive() or t2.is_alive(), f"第 {idx} 轮线程未结束")

        for i in range(rounds):
            run_one(i)

        db = self.env["read"]()
        try:
            closed_batches = db.query(Batch).filter_by(status="closed").all()
            self.assertEqual(len(closed_batches), rounds)
            for batch in closed_batches:
                versions = db.query(SettlementVersion).filter_by(batch_id=batch.id).all()
                self.assertEqual(len(versions), 1, "失败重试/竞态不得产生第二份结算版本")
                snap = lifecycle.loads(versions[0].snapshot_json)
                frozen_ids = {r["id"] for r in snap["records"]["feeding"]}
                live_active = {r.id for r in db.query(FeedingRecord).filter_by(
                    batch_id=batch.id, lifecycle_status=LIFECYCLE_ACTIVE)}
                # 生效集与冻结快照严格一致：签署后的周期数据不再变化
                self.assertEqual(live_active, frozen_ids)
                # 收容的 create 不进业务表：投喂表行数恰好等于快照行数
                table_rows = db.query(FeedingRecord).filter_by(batch_id=batch.id).count()
                self.assertEqual(table_rows, len(frozen_ids))
        finally:
            db.close()


    def test_concurrent_close_requests_produce_single_version(self):
        """多个关闭请求并发：只有一个真正关闭，其余返回原结果，绝不产生第二份版本。"""
        bid = self._fresh_batch(2)
        gate = threading.Event()
        outcomes = []
        lock = threading.Lock()

        def close_one():
            gate.wait(2)
            try:
                res = lifecycle.close_batch(self.env["write"], bid)
                with lock:
                    outcomes.append((res["idempotent"], res["version"]["version_id"]))
            except Exception as exc:  # pragma: no cover
                with lock:
                    outcomes.append(("error", repr(exc)))

        threads = [threading.Thread(target=close_one) for _ in range(5)]
        for t in threads: t.start()
        gate.set()  # 同时放行 5 个关闭请求
        for t in threads: t.join(20)

        self.assertEqual(len(outcomes), 5)
        version_ids = {v for _, v in outcomes}
        self.assertEqual(len(version_ids), 1, "并发关闭产生了多份结算版本")
        # 恰好一个真正关闭，其余全部幂等返回
        self.assertEqual(sum(1 for flag, _ in outcomes if flag is False), 1)
        self.assertEqual(sum(1 for flag, _ in outcomes if flag is True), 4)
        self._assert_invariant(bid)

    def test_close_races_with_update_and_delete(self):
        """关闭与修改、删除并发：两种裁决（冻结或收容）都不允许部分成功。"""
        def one_round(idx, operation):
            bid = self._fresh_batch(300 + idx)
            db = self.env["write"]()
            row_id = db.query(FeedingRecord).filter_by(batch_id=bid).first().id
            db.close()

            gate = threading.Event()

            def close_side():
                gate.wait(2)
                lifecycle.close_batch(self.env["write"], bid, confirm_pending=True)

            def write_side():
                gate.set()
                if operation == "update":
                    lifecycle.submit_record(
                        self.env["write"], "feeding", lifecycle.OP_UPDATE,
                        {"feed_quantity": 77.0}, record_id=row_id, today=self.today)
                else:
                    lifecycle.submit_record(
                        self.env["write"], "feeding", lifecycle.OP_DELETE,
                        record_id=row_id, today=self.today)

            t1 = threading.Thread(target=close_side)
            t2 = threading.Thread(target=write_side)
            t1.start(); t2.start(); t1.join(15); t2.join(15)
            self.assertFalse(t1.is_alive() or t2.is_alive())

            # 关闭后，无论更新/删除先赢还是后赢：生效集必须与冻结快照一致
            self._assert_invariant(bid)
            db = self.env["read"]()
            pending = db.query(ReviewItem).filter_by(
                batch_id=bid, status="pending").all()
            db.close()
            # 若写操作在关闭后被判收容，必须有一条对应的未决项（可恢复）
            for item in pending:
                self.assertEqual(item.operation, operation)
                self.assertEqual(item.reason, lifecycle.REASON_BATCH_CLOSED)

        for i in range(8):
            one_round(i, "update")
        for i in range(8):
            one_round(100 + i, "delete")


# --------------------------------------------------------------------------- 历史数据迁移

LEGACY_DDL = {
    "ponds": """
        CREATE TABLE ponds (
            id INTEGER PRIMARY KEY, name VARCHAR(100) UNIQUE, area FLOAT NOT NULL,
            water_depth FLOAT NOT NULL, species VARCHAR(100), status VARCHAR(20),
            created_at DATETIME, updated_at DATETIME)""",
    "batches": """
        CREATE TABLE batches (
            id INTEGER PRIMARY KEY, batch_number VARCHAR(50) UNIQUE, pond_id INTEGER,
            species VARCHAR(100), stocking_date DATE NOT NULL,
            estimated_harvest_date DATE, actual_harvest_date DATE,
            status VARCHAR(20), created_at DATETIME, updated_at DATETIME)""",
    "feeding_records": """
        CREATE TABLE feeding_records (
            id INTEGER PRIMARY KEY, batch_id INTEGER, feeding_date DATE NOT NULL,
            feed_type VARCHAR(100), feed_quantity FLOAT, feeding_time VARCHAR(20),
            weather VARCHAR(50), water_temperature FLOAT, notes TEXT, created_at DATETIME)""",
}


class LegacyMigrationTest(unittest.TestCase):
    def _build_legacy_db(self, path):
        conn = sqlite3.connect(path)
        try:
            for ddl in LEGACY_DDL.values():
                conn.execute(ddl)
            conn.execute("INSERT INTO ponds VALUES (1,'老塘',10,2,'鲈鱼','active',NULL,NULL)")
            conn.execute(
                "INSERT INTO batches VALUES (1,'OLD-1',1,'鲈鱼','2026-09-01',NULL,"
                "'2026-09-20','harvested',NULL,NULL)")
            # 越界数据：一条早于投苗日，一条晚于实际收获日，一条窗口外但区间内（不应被判越界）
            conn.execute("INSERT INTO feeding_records (id,batch_id,feeding_date,feed_type,"
                         "feed_quantity,created_at) VALUES (1,1,'2026-08-25','A',5,NULL)")
            conn.execute("INSERT INTO feeding_records (id,batch_id,feeding_date,feed_type,"
                         "feed_quantity,created_at) VALUES (2,1,'2026-09-25','A',6,NULL)")
            conn.execute("INSERT INTO feeding_records (id,batch_id,feeding_date,feed_type,"
                         "feed_quantity,created_at) VALUES (3,1,'2026-09-01','A',7,NULL)")
            conn.commit()
        finally:
            conn.close()

    def test_legacy_out_of_bounds_rows_enter_recoverable_queue(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "legacy.db"
            self._build_legacy_db(db_path)
            url = f"sqlite:///{db_path}"

            reader, writer = create_sqlite_engines(url)
            wsf = sessionmaker(bind=writer, autocommit=False, autoflush=False)
            lifecycle.ensure_schema(reader)  # 应 ALTER 补齐新列并建新表
            queued = lifecycle.migrate_legacy_records(wsf)
            self.assertEqual(queued, 2)

            ws = wsf()
            items = ws.query(ReviewItem).filter_by(status="pending").all()
            reasons = sorted(i.reason for i in items)
            self.assertEqual(reasons, ["after_harvest", "before_stocking"])
            # 原行被标记 flagged 并回链复核项，分析默认排除它们
            flagged = ws.query(FeedingRecord).filter_by(
                lifecycle_status=LIFECYCLE_FLAGGED).count()
            self.assertEqual(flagged, 2)
            still_active = ws.get(FeedingRecord, 3)
            self.assertEqual(still_active.lifecycle_status, LIFECYCLE_ACTIVE)
            target = ws.query(ReviewItem).filter_by(
                reason="after_harvest", status="pending").first().id
            ws.close()

            # 迁移必须幂等
            self.assertEqual(lifecycle.migrate_legacy_records(wsf), 0)

            # 可恢复：批准一条后原行回到 active
            decision = lifecycle.resolve_review_item(
                wsf, target, lifecycle.REVIEW_APPROVED)
            # 批次尚未关闭：恢复生效但不产生结算版本
            self.assertTrue(decision["effective_data_changed"])
            self.assertIsNone(decision["new_version"])
            ws = wsf()
            self.assertEqual(ws.get(FeedingRecord, 2).lifecycle_status, LIFECYCLE_ACTIVE)
            ws.close()

            # 已人工批准的越界数据，再次（重启）迁移不得被重新收容
            self.assertEqual(lifecycle.migrate_legacy_records(wsf), 0)
            ws = wsf()
            self.assertEqual(ws.get(FeedingRecord, 2).lifecycle_status, LIFECYCLE_ACTIVE)
            re_queue = ws.query(ReviewItem).filter_by(
                record_id=2, status="pending").count()
            ws.close()
            self.assertEqual(re_queue, 0)
            reader.dispose(); writer.dispose()


# --------------------------------------------------------------------------- 重启恢复 + 分析/版本重放

class RestartRecoveryTest(unittest.TestCase):
    def test_consistency_across_restart_with_file_db(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "restart.db"
            url = f"sqlite:///{db_path}"
            env = make_env(url)
            today = date(2026, 9, 10)
            bid, _ = seed_batch(env, stocking=date(2026, 9, 1),
                                harvest=date(2026, 9, 20))
            lifecycle.submit_record(env["write"], "feeding", lifecycle.OP_CREATE,
                                    payload("feeding", bid, date(2026, 9, 5), feed_quantity=3.0),
                                    today=today)
            res1 = lifecycle.close_batch(env["write"], bid)
            outcome, item = lifecycle.submit_record(
                env["write"], "water_quality", lifecycle.OP_CREATE,
                payload("water_quality", bid, date(2026, 9, 6), ph_value=8.1), today=today)
            self.assertEqual(outcome, "quarantined")
            env["reader"].dispose(); env["writer"].dispose()

            # —— 模拟进程重启：在同一文件上重建引擎并重放启动流程 ——
            reader2, writer2 = create_sqlite_engines(url)
            wsf2 = sessionmaker(bind=writer2, autocommit=False, autoflush=False)
            lifecycle.ensure_schema(reader2)
            self.assertEqual(lifecycle.migrate_legacy_records(wsf2), 0)

            # 关闭状态、版本、复核队列完整保留；重复关闭仍返回原结果
            again = lifecycle.close_batch(wsf2, bid)
            self.assertTrue(again["idempotent"])
            self.assertEqual(again["version"]["version_id"], res1["version"]["version_id"])
            ws = wsf2()
            self.assertEqual(ws.query(SettlementVersion).filter_by(batch_id=bid).count(), 1)
            ws.close()

            # 重启后继续更正流程：批准 -> v2
            ws = wsf2()
            pending_id = ws.query(ReviewItem).filter_by(status="pending").first().id
            ws.close()
            decision = lifecycle.resolve_review_item(
                wsf2, pending_id, lifecycle.REVIEW_APPROVED)
            self.assertEqual(decision["new_version"]["version"], 2)

            # 按版本重放：v1 无水质记录，v2 包含；实时分析默认与最新版本一致
            v1 = lifecycle.get_version_snapshot(wsf2, bid, 1)
            v2 = lifecycle.get_version_snapshot(wsf2, bid, 2)
            self.assertEqual(len(v1["records"]["water_quality"]), 0)
            self.assertEqual(len(v2["records"]["water_quality"]), 1)

            rs = wsf2()
            batch = rs.get(Batch, bid)
            pond = rs.get(Pond, batch.pond_id)
            live = lifecycle.compute_metrics(rs, batch, pond)
            rs.close()
            self.assertEqual(live["feed_total"], v2["metrics"]["feed_total"])

            # 未裁定记录默认排除：再造一条 pending，分析数字不变；收容的 create 不落业务表
            _, extra = lifecycle.submit_record(
                wsf2, "feeding", lifecycle.OP_CREATE,
                payload("feeding", bid, date(2026, 9, 7), feed_quantity=99.0), today=today)
            self.assertEqual(extra.reason, lifecycle.REASON_BATCH_CLOSED)
            rs = wsf2()
            batch = rs.get(Batch, bid); pond = rs.get(Pond, batch.pond_id)
            live2 = lifecycle.compute_metrics(rs, batch, pond)
            active_rows = lifecycle.effective_query(rs, FeedingRecord, bid).count()
            all_rows = rs.query(FeedingRecord).filter_by(batch_id=bid).count()
            rs.close()
            self.assertEqual(live2["feed_total"], live["feed_total"])
            self.assertEqual(active_rows, 1)  # pending 记录被默认排除
            self.assertEqual(all_rows, 1)     # 收容 create 未进投喂表，仍只有冻结的 1 条

            reader2.dispose(); writer2.dispose()


if __name__ == "__main__":
    unittest.main()
