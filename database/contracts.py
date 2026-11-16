import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

VERSION = "C-004-v1"
SOURCE = "candidate_synthetic"
DEFAULT_SEED = 20261115
DEFAULT_AS_OF = date(2026, 11, 15)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
ORDER_FIELDS = ("order_id", "user_id", "amount", "status", "create_time")
LOGISTICS_FIELDS = ("logistics_id", "order_id", "status", "update_time")
PRODUCT_FIELDS = ("sku_id", "name", "stock", "price")
ORDER_STATUS = {
    0: "待付款",
    1: "待发货",
    2: "已发货",
    3: "已完成",
    4: "已取消",
    5: "售后异常",
    6: "退款失败",
}
LOGISTICS_STATUS = {0: "待揽收", 1: "运输中", 2: "派送中", 3: "已签收", 4: "物流异常"}
COMPATIBLE_LOGISTICS = {2: {0, 1, 2, 4}, 3: {3}, 5: {3, 4}, 6: {3, 4}}
SPLIT_COUNTS = {"development": 120, "acceptance": 20, "evaluation": 60}
LOG_COUNTS = {"development": 20, "acceptance": 10, "evaluation": 20}
EFFECTIVE_ORDER_STATUSES = {1, 2, 3, 5, 6}


def stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def jsonl_rows(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def month_bounds(as_of: date) -> tuple[datetime, datetime]:
    end = as_of.replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    return datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())


def data_start(as_of: date) -> datetime:
    month = as_of.year * 12 + as_of.month - 1 - 5
    return datetime(month // 12, month % 12 + 1, 1)


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value))


def integer_in_range(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def parse_time(value: str) -> datetime:
    parsed = datetime.strptime(value, TIME_FORMAT)
    if parsed.strftime(TIME_FORMAT) != value:
        raise ValueError("时间必须使用 YYYY-MM-DD HH:MM:SS")
    return parsed


def normalize_order(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - set(ORDER_FIELDS):
        raise ValueError("订单包含未知字段")
    if not valid_id(raw.get("order_id")) or not valid_id(raw.get("user_id")):
        raise ValueError("订单 ID 和归属用户必填")
    if not integer_in_range(raw.get("amount"), 0, 1_000_000):
        raise ValueError("金额必须为 0..1000000 的整数分")
    status = raw.get("status")
    if status is not None and (type(status) is not int or status not in ORDER_STATUS):
        raise ValueError("未知订单状态码")
    created = raw.get("create_time")
    if created is not None:
        if not isinstance(created, str):
            raise ValueError("时间必须是字符串或 NULL")
        parse_time(created)
    return {field: raw.get(field) for field in ORDER_FIELDS}


def dictionary() -> dict:
    return {
        "contract_version": VERSION,
        "source_type": SOURCE,
        "currency": "CNY",
        "amount_unit": "fen",
        "timezone": "Asia/Shanghai",
        "identity": {"mode": "demo_registry_only", "trusted_membership_source": None,
                     "note": "users.jsonl 是模拟适用条件登记，不是生产认证；用户自称 VIP 不授权"},
        "order_status": {str(k): v for k, v in ORDER_STATUS.items()},
        "logistics_status": {str(k): v for k, v in LOGISTICS_STATUS.items()},
        "abnormal_order_statuses": [5, 6],
        "fields": {
            "orders.order_id": "订单唯一编号",
            "orders.user_id": "订单所属用户，可信服务端身份隔离",
            "orders.amount": "下单金额，整数人民币分，不是退款或净实付",
            "orders.status": "订单状态；NULL 未知；6 只是订单级退款失败标记",
            "orders.create_time": "下单时间，Asia/Shanghai；NULL 缺失",
            "logistics.logistics_id": "物流快照唯一编号",
            "logistics.order_id": "关联订单",
            "logistics.status": "物流最新快照状态",
            "logistics.update_time": "快照更新时间",
            "products.sku_id": "目录商品编号，无订单明细关联",
            "products.name": "商品名称",
            "products.stock": "可用库存件数",
            "products.price": "目录价格，整数人民币分",
        },
        "metrics": {
            "effective_order_amount": {
                "description": "按下单时间统计，排除待付款/取消/未知状态，不扣退款；不是净实付",
                "statuses": sorted(EFFECTIVE_ORDER_STATUSES),
                "unknown_fields": "排除并披露缺少状态/时间的订单数量",
            }
        },
        "relations": ["logistics.order_id -> orders.order_id"],
        "unsupported": ["退款流水及进度", "订单商品关联", "实付或净消费", "完整物流轨迹"],
    }
