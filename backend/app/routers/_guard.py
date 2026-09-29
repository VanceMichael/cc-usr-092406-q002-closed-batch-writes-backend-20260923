"""记录类路由共享的生命周期守卫接入。"""
from fastapi.responses import JSONResponse

from ..database import WriterSessionLocal
from ..services import lifecycle


def _review_item_payload(item):
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "record_type": item.record_type,
        "operation": item.operation,
        "record_id": item.record_id,
        "reason": item.reason,
        "business_date": item.business_date.isoformat() if item.business_date else None,
        "status": item.status,
        "payload": item.payload,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def quarantined_response(item, action: str) -> JSONResponse:
    """越界 / 关闭后写入统一返回 202，数据已进入可恢复的复核队列。"""
    return JSONResponse(
        status_code=202,
        content={
            "detail": "记录未直接生效，已进入复核队列等待裁定",
            "action": action,
            "review_item": _review_item_payload(item),
        },
    )


def submit_create(record_type: str, data: dict):
    outcome, obj = lifecycle.submit_record(
        WriterSessionLocal, record_type, lifecycle.OP_CREATE, data)
    if outcome == "quarantined":
        return quarantined_response(obj, "create")
    return obj


def submit_update(record_type: str, record_id: int, data: dict):
    outcome, obj = lifecycle.submit_record(
        WriterSessionLocal, record_type, lifecycle.OP_UPDATE, data, record_id=record_id)
    if outcome == "quarantined":
        return quarantined_response(obj, "update")
    return obj


def submit_delete(record_type: str, record_id: int):
    """返回 JSONResponse 表示已收容（202）；返回 None 表示已直接删除。"""
    outcome, obj = lifecycle.submit_record(
        WriterSessionLocal, record_type, lifecycle.OP_DELETE, record_id=record_id)
    if outcome == "quarantined":
        return quarantined_response(obj, "delete")
    return None
