"""Start the household inventory web page and the two device adapters."""

import csv
import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import catalog
import pantry

DATA = Path(os.getenv("PANTRY_DATA_DIR", str(Path(__file__).parent / "data")))
DB = DATA / "pantry.sqlite3"
SHANGHAI = timezone(timedelta(hours=8))
HOST = os.getenv("PANTRY_HOST", "127.0.0.1")
PORT = int(os.getenv("PANTRY_PORT", "8768"))
TOKEN = ""
HEALTH = {"voice": "等待首次读取", "presence": "等待首次读取", "output": "等待调用"}
PRESENCE = ("waiting", None)
PREFIXES = ("保质期管家", "保质期助手")
PAGE = Path(__file__).with_name("page.html")
DEFAULT = {"enabled": "1", "start": "09:00", "end": "20:00", "lead_days": "2"}


def now():
    return datetime.now(SHANGHAI)


def settings(db):
    return {k: pantry.get_setting(db, k, v) for k, v in DEFAULT.items()}


def status(key, msg):
    HEALTH[key] = msg


def log_failure(component, exc):
    # Device libraries may include signed URLs in exception messages.
    msg = "暂不可用（" + type(exc).__name__ + "）"
    if HEALTH.get(component) != msg:
        logging.getLogger(__name__).warning("%s: %s", component, type(exc).__name__)
    status(component, msg)


def say(text):
    import speaker

    accepted = speaker.speak(text)
    status("output", "已受理 " + now().strftime("%H:%M") if accepted else "结果未确认")
    return bool(accepted)


def voice_step(db, since, seen=None):
    import speaker

    end = time.time()
    if seen is None:
        seen = {}
    for ident in list(seen):
        if seen[ident] < end - 120:
            del seen[ident]
    events = speaker.poll(max(since - 2, end - 120))
    for event in sorted(events, key=lambda e: e["ts"]):
        ts = float(event["ts"])
        if not end - 120 <= ts <= end + 10 or ts < since - 2:
            continue
        ident = str(event["id"])
        if ident in seen:
            continue
        raw = str(event["text"]).strip().removeprefix("小爱同学").lstrip("，,。 ")
        if not raw.startswith(PREFIXES) and not pantry.natural_voice(db, raw):
            seen[ident] = ts
            continue
        msg, fresh = pantry.command(
            db,
            "voice:" + str(event["id"]),
            raw,
            datetime.fromtimestamp(ts, SHANGHAI).date(),
            "speaker",
        )
        seen[ident] = ts
        if fresh:
            # Persist the command before acknowledging it on a physical speaker.
            with db:
                pantry.put(
                    db,
                    "last_voice",
                    datetime.fromtimestamp(ts, SHANGHAI).isoformat(timespec="seconds"),
                )
            try:
                say(msg)
            except Exception as exc:  # noqa: BLE001 - isolate adapter/request failures without logging secrets
                log_failure("output", exc)
    latest = speaker.last_record_time
    last_seen = (
        datetime.fromtimestamp(latest, SHANGHAI).strftime("%m-%d %H:%M")
        if latest
        else "暂无"
    )
    status("voice", "云端最近发言 " + last_seen + " · 查询 " + now().strftime("%H:%M"))
    return max([since] + [float(e["ts"]) for e in events if float(e["ts"]) <= end])


def voice_loop():
    with closing(pantry.connect(DB)) as db:
        # Cover brief restarts without importing conversations from before installation.
        started = time.time()
        with db:
            installed = float(pantry.get_setting(db, "voice_installed", str(started)))
            pantry.put(db, "voice_installed", str(installed))
        since = max(installed, started - 120)
        seen = {}
        failures = 0
        while True:
            # A ready transcript should not wait another five seconds behind XiaoAI.
            delay = 1
            try:
                since = voice_step(db, since, seen)
                failures = 0
            except Exception as exc:  # noqa: BLE001 - isolate adapter/request failures without logging secrets
                log_failure("voice", exc)
                failures = min(failures + 1, 4)
                delay = 30 * 2 ** (failures - 1)
            time.sleep(delay)


def reminder_status(db, dt, observation=None):
    """Read-only explanation, using the same conditions as the reminder worker."""
    from gateway import MAX_AGE

    day = dt.date().isoformat()
    cfg = settings(db)
    clock = dt.strftime("%H:%M")
    in_window = cfg["start"] <= clock < cfg["end"]
    rows = pantry.due_items(db, dt.date())
    reasons = {}
    if cfg["enabled"] != "1":
        reasons["disabled"] = "主动播报已关闭"
    if pantry.get_setting(db, "skip_day") == day:
        reasons["paused"] = "今天已暂停主动提醒"
    if not rows:
        reasons["no_items"] = "没有到期、临期或到查看日的在库批次"
    if not in_window:
        reasons["quiet"] = f"当前不在允许播报时段（{cfg['start']}–{cfg['end']}）"
    observation = PRESENCE if observation is None else observation
    mode, sample = observation
    if not os.getenv("XIAOMUSIC_URL") or not os.getenv("XIAOAI_KITCHEN_DID"):
        reasons["unconfigured"] = "未配置厨房传感器或音箱接入"
    elif mode == "error":
        reasons["sensor_error"] = "厨房传感器读取失败，等待下次检查"
    elif sample is None:
        reasons["waiting"] = "等待厨房传感器首次上报"
    elif sample.reason in ("conflicting_reports", "no_valid_status"):
        reasons["unknown"] = "厨房传感器状态冲突或缺失"
    elif (
        sample.updated_at is None
        or not 0 <= dt.timestamp() - sample.updated_at <= MAX_AGE
        or sample.occupied is None
    ):
        reasons["stale"] = "厨房没有有效的近期上报，无法判断是否有人"
    elif not sample.occupied:
        reasons["vacant"] = "厨房当前无人"
    elif sample.duration_minutes < 2:
        reasons["short_stay"] = "厨房有人，等待传感器确认已满 2 分钟"
    notice = db.execute("SELECT state FROM notices WHERE day=?", (day,)).fetchone()
    state = notice[0] if notice else None
    headline = {
        "准备播报": "今天已登记播报尝试，尚无发送结果",
        "结果未确认": "今天已尝试播报，音箱结果未确认，不再重复发送",
        "音箱服务已受理": "今天的提醒已被音箱服务受理",
    }.get(state)
    last = json.loads(pantry.get_setting(db, "window_check", "null"))
    return {
        "day": day,
        "time": clock,
        "due_count": len(rows),
        "in_window": in_window,
        "notice_state": state,
        "reasons": reasons,
        "headline": headline
        or next(iter(reasons.values()), "条件已满足，等待下一次检查"),
        "last_window_check": last if last and last["day"] == day else None,
    }, rows


def reserve_summary(db, dt, observation=None):
    day = dt.date().isoformat()
    db.execute("BEGIN IMMEDIATE")
    try:
        report, rows = reminder_status(db, dt, observation)
        if report["in_window"] and not report["notice_state"]:
            old = report["last_window_check"]
            check = {
                "day": day,
                "time": report["time"],
                "headline": report["headline"],
                "reasons": list(report["reasons"].values()),
            }
            if not old or old != check:
                pantry.put(db, "window_check", json.dumps(check, ensure_ascii=False))
        if report["reasons"] or report["notice_state"]:
            db.commit()
            return None
        msg = "保质期提醒。" + pantry.summary(rows)
        reserved = db.execute(
            "INSERT OR IGNORE INTO notices VALUES(?,?,?)", (day, "准备播报", msg)
        ).rowcount
        db.commit()
        return msg if reserved else None
    except BaseException:
        db.rollback()
        raise


def presence_loop():
    global PRESENCE
    import gateway

    with closing(pantry.connect(DB)) as db:
        while True:
            try:
                sample = gateway.read_sample()
                PRESENCE = ("ok", sample)
                state = {True: "厨房有人", False: "厨房无人", None: "暂无新鲜状态"}[
                    sample.occupied
                ]
                if sample.updated_at:
                    state += " · 上报 " + datetime.fromtimestamp(
                        sample.updated_at, SHANGHAI
                    ).strftime("%m-%d %H:%M")
                elif sample.occupied is None:
                    state += " · 等待设备连接"
                status("presence", state)
            except Exception as exc:  # noqa: BLE001 - isolate adapter/request failures without logging secrets
                PRESENCE = ("error", None)
                log_failure("presence", exc)
            try:
                dt = now()
                msg = reserve_summary(db, dt, PRESENCE)
                if msg:
                    delivery = "结果未确认"
                    try:
                        if say(msg):
                            delivery = "音箱服务已受理"
                    except Exception as exc:  # noqa: BLE001 - isolate adapter/request failures without logging secrets
                        log_failure("output", exc)
                    finally:
                        with db:
                            pantry.put(db, "recent_notice", str(time.time()))
                            db.execute(
                                "UPDATE notices SET state=? WHERE day=?",
                                (delivery, dt.date().isoformat()),
                            )
            except Exception as exc:  # noqa: BLE001 - isolate adapter/request failures without logging secrets
                log_failure("presence", exc)
            time.sleep(15)


def inventory_csv(db, today):
    """Export current batches only. Quote spreadsheet formula prefixes in text fields."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, quoting=csv.QUOTE_ALL)
    writer.writerow(
        [
            "批次",
            "名称",
            "数量",
            "单位",
            "存放位置",
            "日期用途",
            "日期",
            "状态",
            "生产日期",
            "保质期数值",
            "保质期单位",
        ]
    )
    kinds = {"expiry": "到期日", "check": "查看日", "stock": "只记库存"}
    labels = {
        "expired": "已过期",
        "today": "今天到期",
        "soon": "即将到期",
        "review": "待查看",
        "later": "日期尚早",
        "undated": "未设日期",
    }

    def safe(value):
        if value.lstrip().startswith(
            ("=", "+", "-", "@", "＝", "＋", "－", "＠")
        ) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
        return value

    for r in pantry.dated_inventory(db, today):
        writer.writerow(
            [
                r["id"],
                safe(r["name"]),
                r["qty"],
                safe(r["unit"]),
                safe(r["location"]),
                kinds[r["kind"]],
                r["due"],
                labels[r["status"]],
                r["produced_on"],
                r["shelf_value"] or "",
                {"day": "天", "month": "月", "year": "年", "": ""}[r["shelf_unit"]],
            ]
        )
    return out.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, obj, html=False, cookie=False):
        data = obj if html else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8" if html else "application/json; charset=utf-8",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if cookie:
            self.send_header(
                "Set-Cookie",
                "pantry="
                + TOKEN
                + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
            )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authenticated(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            auth = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            value = auth or (cookies["pantry"].value if "pantry" in cookies else "")
            return hmac.compare_digest(value.encode(), TOKEN.encode())
        except ValueError:
            return False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            return self.send(200, PAGE.read_bytes(), html=True)
        if path == "/health":
            return self.send(200, {"ok": True})
        if path not in ("/api/state", "/api/export.csv"):
            return self.send(404, {"error": "地址不存在"})
        if not self.authenticated():
            return self.send(401, {"error": "请先连接家庭清单"})
        with closing(pantry.connect(DB)) as db:
            db.execute("BEGIN")
            dt = now()
            today = dt.date()
            if path == "/api/export.csv":
                data = inventory_csv(db, today)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition", f'attachment; filename="pantry-{today}.csv"'
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            notice = db.execute(
                "SELECT day,state FROM notices ORDER BY day DESC LIMIT 1"
            ).fetchone()
            can_undo = (
                db.execute("SELECT 1 FROM changes WHERE undone=0 LIMIT 1").fetchone()
                is not None
            )
            self.send(
                200,
                {
                    "rows": pantry.dated_inventory(db, today),
                    "today": today.isoformat(),
                    "settings": settings(db),
                    "devices": dict(HEALTH),
                    "last_voice": pantry.get_setting(db, "last_voice"),
                    "can_undo": can_undo,
                    "recent_changes": pantry.recent_changes(db),
                    "notice": dict(notice) if notice else None,
                    "reminder_today": reminder_status(db, dt, PRESENCE)[0],
                    "catalog": catalog.listing(db),
                },
            )

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send(403, {"error": "请从家庭清单页面提交"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if (
                size <= 0
                or size > 8192
                or self.headers.get("Content-Type", "").split(";")[0]
                != "application/json"
            ):
                return self.send(400, {"error": "请求格式不正确"})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise TypeError()
            if self.path == "/api/login":
                if not hmac.compare_digest(
                    str(body.get("token", "")).strip().encode(), TOKEN.encode()
                ):
                    return self.send(401, {"error": "家庭访问码不正确"})
                return self.send(200, {"ok": True}, cookie=True)
            if not self.authenticated():
                return self.send(401, {"error": "请先连接家庭清单"})
            with closing(pantry.connect(DB)) as db:
                if self.path == "/api/settings":
                    cfg = {k: str(body[k]) for k in DEFAULT}
                    for k in ("start", "end"):
                        if (
                            datetime.strptime(cfg[k], "%H:%M").strftime("%H:%M")
                            != cfg[k]
                        ):
                            raise ValueError()
                    if (
                        cfg["start"] >= cfg["end"]
                        or cfg["enabled"] not in ("0", "1")
                        or not 0 <= int(cfg["lead_days"]) <= 90
                    ):
                        raise ValueError()
                    with db:
                        for k, v in cfg.items():
                            pantry.put(db, k, v)
                    return self.send(200, {"reply": "提醒设置已保存。"})
                key = str(body["id"])
                if not 8 <= len(key) <= 160:
                    raise ValueError()
                channel = "web:" + str(body.get("channel", "browser"))[:80]
                if self.path == "/api/add":
                    if "barcode" in body:
                        code = body["barcode"]
                        if (
                            body.get("name")
                            or not isinstance(code, str)
                            or not catalog.barcode(catalog.key(code))
                        ):
                            raise catalog.InputError(
                                "请提供已绑定的数字条码文本，商品名与条码二选一。"
                            )
                        body["name"] = code.strip()
                    body.setdefault("unit", "")
                    if (
                        "produced_on" in body
                        or "shelf_value" in body
                        or "shelf_unit" in body
                    ):
                        if body.get("kind", "expiry") != "expiry" or body.get("due"):
                            raise catalog.InputError(
                                "请选择直接填写到期日，或填写生产日期和保质期。"
                            )
                        try:
                            fields = pantry.shelf_fields(
                                datetime.strptime(
                                    body["produced_on"], "%Y-%m-%d"
                                ).date(),
                                body["shelf_value"],
                                body["shelf_unit"],
                                now().date(),
                            )
                        except (TypeError, KeyError, ValueError, OverflowError):
                            raise catalog.InputError(
                                "请填写有效的生产日期和正整数保质期，生产日期不能晚于今天。"
                            ) from None
                        body.update(fields)
                    item = {
                        field: body[field].strip()
                        for field in ("name", "unit", "kind", "due")
                        if isinstance(body.get(field), str)
                    }
                    qty = body.get("qty")
                    if (
                        len(item) != 4
                        or not 1 <= len(item["name"]) <= 60
                        or len(item["unit"]) > 8
                        or type(qty) is not int
                        or not 1 <= qty <= 999
                        or item["kind"] not in ("stock", "check", "expiry")
                    ):
                        raise ValueError()
                    if item["kind"] != "stock":
                        item["due"] = (
                            datetime.strptime(item["due"], "%Y-%m-%d")
                            .date()
                            .isoformat()
                        )
                    elif item["due"]:
                        raise ValueError()
                    item["qty"] = qty
                    if "produced_on" in body:
                        item.update(fields)
                    item["location"] = pantry.location_name(body.get("location", ""))
                    msg, _ = pantry.transact(
                        db,
                        "web:" + key,
                        lambda: pantry.save_entries(db, [item], now().date(), channel),
                    )
                elif self.path == "/api/catalog":
                    msg, _ = pantry.transact(
                        db,
                        "web:" + key,
                        lambda: catalog.save(
                            db, body, pantry.location_name(body.get("location", ""))
                        ),
                    )
                elif self.path == "/api/command":
                    raw = str(body["text"]).strip()
                    if not 0 < len(raw) <= 240:
                        raise ValueError()
                    msg, _ = pantry.command(
                        db,
                        "web:" + key,
                        raw,
                        now().date(),
                        channel,
                    )
                elif self.path == "/api/batch":
                    op = body["op"]
                    if op not in (
                        "date",
                        "consume",
                        "location",
                        "discard",
                        "review_tomorrow",
                    ):
                        raise ValueError()
                    v = body.get("value")
                    if op == "date":
                        v = datetime.strptime(v, "%Y-%m-%d").date().isoformat()
                    elif op == "location":
                        v = pantry.location_name(v)
                    elif op == "review_tomorrow":
                        v = now().date()
                    elif (
                        op == "discard"
                        and (type(v) is not int or v <= 0)
                        or v is not None
                        and int(v) <= 0
                    ):
                        raise ValueError()
                    msg, _ = pantry.transact(
                        db,
                        "web:" + key,
                        lambda: pantry.change(db, int(body["batch"]), op, v),
                    )
                else:
                    return self.send(404, {"error": "地址不存在"})
                return self.send(200, {"reply": msg})
        except catalog.InputError as exc:
            return self.send(400, {"error": str(exc)})
        except (ValueError, TypeError, KeyError):
            return self.send(
                400,
                {
                    "error": "请检查日期、时间或数量。安静时段可通过允许播报的起止时间设置。"
                },
            )
        except Exception as exc:  # noqa: BLE001 - isolate adapter/request failures without logging secrets
            logging.getLogger(__name__).error("web: %s", type(exc).__name__)
            return self.send(500, {"error": "请求暂未完成，请刷新清单核对。"})


def main():
    global TOKEN
    DATA.mkdir(parents=True, exist_ok=True)
    token_file = DATA / "web.token"
    TOKEN = os.getenv("PANTRY_TOKEN", "").strip()
    if not TOKEN:
        if not token_file.exists():
            with open(
                os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
            ) as f:
                f.write(secrets.token_urlsafe(24))
        TOKEN = token_file.read_text().strip()
    if not TOKEN:
        raise RuntimeError("缺少家庭访问码")
    with closing(pantry.connect(DB)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        pantry.initialize(db)
    from backup import start_backup_worker

    start_backup_worker(DB)
    if os.environ.get("XIAOMUSIC_URL"):
        threading.Thread(target=voice_loop, daemon=True, name="voice").start()
        if os.environ.get("XIAOAI_KITCHEN_DID"):
            threading.Thread(target=presence_loop, daemon=True, name="presence").start()
        else:
            status("presence", "未配置厨房传感器")
    else:
        for key in ("voice", "presence", "output"):
            status(key, "未启用设备接入")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
