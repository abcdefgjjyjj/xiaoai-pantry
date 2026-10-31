"""家庭保质期管家的批次库存和中文指令。所有变更在一个 SQLite 事务中完成。"""

import calendar
import json
import re
import sqlite3
import time
from datetime import date, timedelta


CN = "零〇一二两三四五六七八九十百"
NUM = rf"(?:\d+|[{CN}]+)"
UNIT = "瓶盒袋包罐桶支管片块根把斤个份颗枚件提板卷打条杯箱筐排盆扎"
DATE = re.compile(
    rf"(?:(?:{NUM})年)?(?:{NUM})月(?:{NUM})(?:日|号)?|"
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}|"
    rf"(?:大后天|后天|明天|今天|月底|(?:(?:下|本|这)(?:个)?)?(?:星期|周|礼拜)[一二三四五六日天]|{NUM}天后)"
)
CHECK = re.compile("提醒|看看|检查|查看")
NO_DATE = re.compile(
    r"(?:先|暂时)?(?:只记库存|不(?:填|设|记)日期|没有到期日|不设到期日)$"
)
DISCARD = re.compile(r"丢掉|扔掉|丢弃|扔了|丢了")
CONSUME = re.compile(r"吃完|用完|喝完|吃了|用了|喝了|用掉|吃掉|喝掉|" + DISCARD.pattern)
MOVE = re.compile(r"改到|改为|改成|改期到")
PURCHASE = re.compile(
    r"^(?:帮我|请|今天|刚才|刚刚|刚|我|我们家|家里|又|还|新|从超市|刚去超市)*(?:买了|买回来|买回了|买的|购入|登记|入库)"
)
NOT_STOCK = re.compile(
    r"不要|别记|没买|如果|假如|要是|假设|打算|准备买|想买|怎么买|能不能|能吃吗|可以吃吗|怎么保存|是什么意思|听歌|播放|开灯|关灯|闹钟|开会|取快递|订阅|会员|信用卡|还款|合同|车险|话费|流量包|优惠券|天气"
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, qty INTEGER NOT NULL,
 unit TEXT NOT NULL, due TEXT NOT NULL, kind TEXT NOT NULL, location TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY, before_json TEXT NOT NULL, after_json TEXT NOT NULL, undone INTEGER DEFAULT 0, action TEXT NOT NULL DEFAULT '', created_at INTEGER);
CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, reply TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending(channel TEXT PRIMARY KEY, body TEXT NOT NULL, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notices(day TEXT PRIMARY KEY,state TEXT NOT NULL,detail TEXT NOT NULL);
"""


def connect(path):
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    return db


def initialize(db):
    db.executescript(SCHEMA)
    if "location" not in {r["name"] for r in db.execute("PRAGMA table_info(batches)")}:
        db.execute("ALTER TABLE batches ADD COLUMN location TEXT NOT NULL DEFAULT ''")
    columns = {r["name"] for r in db.execute("PRAGMA table_info(changes)")}
    if "action" not in columns:
        db.execute("ALTER TABLE changes ADD COLUMN action TEXT NOT NULL DEFAULT ''")
    if "created_at" not in columns:
        db.execute("ALTER TABLE changes ADD COLUMN created_at INTEGER")


def location_name(value):
    if not isinstance(value, str) or len(value.strip()) > 40:
        raise ValueError("请填写不超过40个字的存放位置。")
    value = re.sub(r"(?:里面|里边|里头|里)$", "", value.strip())
    return {"冷藏室": "冰箱冷藏室", "冷冻室": "冰箱冷冻室", "未指定": ""}.get(
        value, value
    )


def take_location(piece):
    # A location clause may precede or follow the date, even without punctuation.
    m = re.search(
        r"(?:存放在|存放于|放进|放入|放到了?|放在|(?<=[，,])放|放(?=冰箱|橱柜|储物柜|卫生间|冷藏室|冷冻室))([^，,。；;]+)",
        piece,
    )
    if not m:
        return piece, ""
    tail = m[1]
    cut = re.search(
        r"(?:"
        + DATE.pattern
        + r")|(?:先|暂时)?(?:只记库存|不设日期|不填日期|不记日期)",
        tail,
    )
    end = cut.start() if cut else len(tail)
    place = tail[:end]
    if not place or "放" in place:
        raise ValueError("请分别说清商品和存放位置。")
    return (piece[: m.start()] + piece[m.start(1) + end :]).strip(
        "，, "
    ), location_name(place)


def query_location(db, s):
    """Match explicit place names, including custom places that have no inventory yet."""
    if PURCHASE.match(s):
        return None
    s = re.sub(
        r"^(?:(?:请|帮我|查一下|看看|查查|查|我们家|家里的|家里|从|在|把))+", "", s
    )
    names = {
        r[0]
        for r in db.execute("SELECT DISTINCT location FROM batches WHERE location<>''")
    }
    names.update(
        (
            "冰箱",
            "冷藏室",
            "冷冻室",
            "冰箱冷藏室",
            "冰箱冷冻室",
            "橱柜",
            "储物柜",
            "储物间",
            "卫生间",
            "药箱",
            "书房",
            "厨房",
            "阳台",
        )
    )
    for name in sorted(names, key=len, reverse=True):
        if s.startswith(name) and re.match(
            r"(?:里|中|的|有|还|哪些|什么|快到期|库存|待查看|哪几)", s[len(name) :]
        ):
            return location_name(name)
    m = re.match(r"([^，,的里]{1,40})(?:里面|里)(?:的|有|还|哪些|什么|哪几|快到期)", s)
    return location_name(m[1]) if m and m[1] not in ("家", "我们家") else None


def at_location(rows, place):
    if place is None:
        return rows
    return [
        r
        for r in rows
        if r["location"] == place
        or (place == "冰箱" and r["location"] in ("冰箱冷藏室", "冰箱冷冻室"))
    ]


def number(s):
    if s.isdigit():
        return int(s)
    digit = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    if "百" in s:
        a, b = s.split("百", 1)
        return number(a or "一") * 100 + (number(b) if b else 0)
    if "十" in s:
        a, b = s.split("十", 1)
        return number(a or "一") * 10 + (number(b) if b else 0)
    return int("".join(str(digit[c]) for c in s))


def parse_date(s, today):
    if s in ("今天", "明天", "后天", "大后天"):
        return today + timedelta(days=("今天", "明天", "后天", "大后天").index(s))
    if s == "月底":
        return today.replace(day=calendar.monthrange(today.year, today.month)[1])
    if s.endswith("天后"):
        return today + timedelta(days=number(s[:-2]))
    if re.search("周|星期|礼拜", s):
        target = "一二三四五六日".index(s[-1].replace("天", "日"))
        delta = target - today.weekday()
        if s.startswith("下"):
            delta += 7
        elif delta < 0 and not s.startswith(("本", "这")):
            delta += 7
        return today + timedelta(days=delta)
    if "-" in s:
        return date(*map(int, s.split("-")))
    if "/" in s:
        m, d = map(int, s.split("/"))
        return date(today.year, m, d)
    parts = re.findall(NUM, s)
    return date(*([today.year] if len(parts) == 2 else []), *map(number, parts))


def get_setting(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def put(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))


def inventory(db):
    return [
        dict(r)
        for r in db.execute("SELECT * FROM batches WHERE qty>0 ORDER BY due='',due,id")
    ]


def describe(r):
    where = "，放在" + r["location"] if r.get("location") else ""
    if r["kind"] == "stock":
        return f"{r['name']}{r['qty']}{r['unit']}{where}，未设日期"
    due = date.fromisoformat(r["due"])
    when = f"{due.month}月{due.day}日"
    wording = when + ("到期" if r["kind"] == "expiry" else "提醒查看")
    if r.get("status") == "expired":
        wording = "已过期，到期日是" + when
    elif r.get("status") == "today":
        wording = "今天到期"
    elif r.get("status") == "review":
        wording = "待查看，查看日是" + when
    return f"{r['name']}{r['qty']}{r['unit']}{where}，{wording}"


def date_status(r, today, lead):
    if r["kind"] == "stock":
        return "undated"
    days = (date.fromisoformat(r["due"]) - today).days
    if r["kind"] == "check":
        return "review" if days <= 0 else "later"
    if days < 0:
        return "expired"
    if days == 0:
        return "today"
    return "soon" if days <= lead else "later"


def dated_inventory(db, today):
    lead = int(get_setting(db, "lead_days", "2"))
    return [dict(r, status=date_status(r, today, lead)) for r in inventory(db)]


def due_items(db, today):
    return [
        r
        for r in dated_inventory(db, today)
        if r["status"] in ("expired", "today", "soon", "review")
    ]


def date_query(s):
    if PURCHASE.match(s) or re.search(r"怎么|为什么|是什么|如果|会不会", s):
        return None
    if not re.search(
        r"哪些|什么|哪几|有啥|有没有|^(?:(?:帮我|请)?(?:查|看看)|临期$)", s
    ):
        return None
    if re.search("待处理|需要处理|要处理", s) and re.search(
        "库存|商品|食材|食物|东西", s
    ):
        return ("expired", "today", "soon", "review")
    if re.search(r"该查看|要查看|需要查看|要检查|该检查|待查看", s) and re.search(
        "库存|商品|东西|食材|食物|清单", s
    ):
        return ("review",)
    if re.search(r"快.*(?:过期|到期)|即将到期|临期", s):
        return ("today", "soon")
    if "今天" in s and "到期" in s:
        return ("today",)
    if "过期" in s or "已到期" in s or "已经到期" in s:
        return ("expired",)
    return None


def summary(rows, limit=5):
    if not rows:
        return "没有待提醒的商品。"
    return "；".join(map(describe, rows[:limit])) + (
        f"。另有{len(rows) - limit}批，详见清单。" if len(rows) > limit else "。"
    )


def remember(db, channel, data):
    db.execute(
        "INSERT OR REPLACE INTO pending VALUES(?,?,?)",
        (channel, json.dumps(data, ensure_ascii=False), time.time() + 180),
    )


def save_change(db, before, after, action):
    db.execute(
        "INSERT INTO changes(before_json,after_json,action,created_at) VALUES(?,?,?,?)",
        (
            json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
            action,
            int(time.time()),
        ),
    )


def recent_changes(db):
    result = []
    for row in db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT 20"):
        before = {r["id"]: r for r in json.loads(row["before_json"])}
        items = []
        for item in json.loads(row["after_json"]):
            prev = before.get(item["id"], {})
            items.append(
                {
                    "batch": item["id"],
                    "name": item["name"],
                    "unit": item["unit"],
                    "delta": item["qty"] - prev.get("qty", 0),
                    "qty_after": item["qty"],
                    "location": item.get("location", ""),
                    "due": item["due"],
                }
            )
        result.append(
            {
                "action": row["action"] or "legacy",
                "created_at": row["created_at"],
                "undone": bool(row["undone"]),
                "items": items,
            }
        )
    return result


def row_for(db, ident):
    row = db.execute("SELECT * FROM batches WHERE id=?", (ident,)).fetchone()
    return dict(row) if row else None


def normalize(raw):
    s = re.sub(r"\s+", "", raw).strip("，,。！!？?")
    s = re.sub(r"^小爱同学[，,。]?", "", s)
    s = re.sub(r"^(?:打开)?(?:保质期管家|保质期助手)[，,。：:]?", "", s)
    s = re.sub(r"^(我|我们家)?今天(?=(?:刚刚|刚|新|又)?买)", r"\1", s)
    s = (
        s.replace("它的保质期", "保质期")
        .replace("保质期是到", "到")
        .replace("到期时间是", "到期日")
    )
    s = re.sub(rf"(?:还有|再过)({NUM})天(?:就)?(到期|过期)", r"\1天后\2", s)
    # "周日提醒我看看菠菜" and "菠菜周日提醒我看看" should record the same check date.
    m = re.fullmatch(
        r"("
        + DATE.pattern
        + r")[，,]?(?:记得|提醒我)(?:看一下|看看|检查一下|检查|用掉|吃掉|喝掉)([^，,。]+)",
        s,
    )
    if m:
        s = m[2] + "，" + m[1] + "提醒我看看"
    return re.sub(r"[，,]?(?:帮我|给我|记得)(?:记一下|记下来|登记一下)$", "", s)


def focused_batch(db, channel):
    focus = json.loads(get_setting(db, "focus:" + channel, "null"))
    if focus and focus["until"] > time.time():
        return row_for(db, focus["id"])
    return None


def last_change(db):
    return db.execute(
        "SELECT coalesce(max(id),0) FROM changes WHERE undone=0"
    ).fetchone()[0]


def record_focus(db, channel, ident):
    value = {"id": ident, "until": time.time() + 180} if ident else None
    put(db, "focus:" + channel, json.dumps(value))


def bare_change(s):
    action = rf"(?:{CONSUME.pattern})(?:了)?(?:{NUM}[{UNIT}])?(?:了)?"
    move = rf"(?:{MOVE.pattern})(?:{DATE.pattern})"
    return re.fullmatch(
        rf"(?:刚才|刚刚|刚|我|已经|现在|把它|它|又)*(?:{action}|{move})", s
    )


def natural_voice(db, text, channel="speaker"):
    """Select stock-related speech in memory. Other conversations leave no database record."""
    s = normalize(text)
    if not s or NOT_STOCK.search(s):
        return False
    pending = db.execute(
        "SELECT body FROM pending WHERE channel=? AND until>?", (channel, time.time())
    ).fetchone()
    if pending:
        action = json.loads(pending[0])["action"]
        if s in ("取消", "算了", "退出"):
            return True
        if action == "batch" and re.fullmatch(rf"(?:选|选第|第)?{NUM}(?:个|批|号)?", s):
            return True
        if action == "date" and (
            NO_DATE.fullmatch(s)
            or re.fullmatch(
                r"(?:"
                + DATE.pattern
                + r")(?:到期|过期|提醒我看看|提醒查看|查看|提醒我|检查|了)?",
                s,
            )
        ):
            return True
    if date_query(s):
        return True
    if re.search(r"有什么|还有啥|有哪些|有啥", s) and query_location(db, s) is not None:
        return True
    if s.startswith("撤销"):
        return bool(
            re.search("库存|入库|登记", s)
            or (
                s in ("撤销", "撤销刚才那条", "撤销刚才那笔", "撤销上一条")
                and focused_batch(db, channel)
            )
        )
    if re.search(
        "查库存|看看库存|家里.*库存|临期|(?:什么|哪些|啥).*(?:到期|过期)|(?:到期|过期).*(?:什么|哪些|啥)|(?:冰箱|家里).*(?:还有什么|有什么)",
        s,
    ):
        return True
    if re.search("吗|能否|怎么买|怎么做|怎么吃|如何|多少钱", s):
        return False
    matches = any(r["name"] in s for r in inventory(db))
    if matches and (
        CONSUME.search(s)
        or MOVE.search(s)
        or re.search("哪天|几号|什么时候|还剩|还有几|有多少|还有多少|放在?哪|存在哪", s)
    ):
        return True
    if bare_change(s) and focused_batch(db, channel):
        return True
    if s in ("明天再提醒", "今天别提醒"):
        return bool(
            focused_batch(db, channel)
            or time.time() - float(get_setting(db, "recent_notice", "0")) < 180
        )
    if PURCHASE.match(s):
        return True
    d = DATE.search(s)
    return bool(
        d
        and d.start() > 0
        and re.search("到期|过期|保质期|有效期|提醒.*(?:看|吃|用|检查)", s)
    )


def undo(db):
    row = db.execute(
        "SELECT * FROM changes WHERE undone=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return "没有可以撤销的库存变更。"
    before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
    for r in before + after:
        r.setdefault("location", "")
    if any(row_for(db, r["id"]) != r for r in after):
        return "库存已经变化，请在清单中核对后修改。"
    for r in after:
        db.execute("DELETE FROM batches WHERE id=?", (r["id"],))
    for r in before:
        db.execute(
            "INSERT INTO batches(id,name,qty,unit,due,kind,location) VALUES(:id,:name,:qty,:unit,:due,:kind,:location)",
            r,
        )
    db.execute("UPDATE changes SET undone=1 WHERE id=?", (row["id"],))
    return "已撤销最近一次库存变更。"


def product(text):
    text = PURCHASE.sub("", text)
    text = re.sub(r"^(?:(?:请)?帮我|请|麻烦|给我)+", "", text)
    text = re.sub(r"^(?:记录|登记)(?:一下)?[，,]?", "", text)
    text = re.sub(
        r"^(?:(?:今天)?(?:我|刚刚|刚|又|新|还|另外|又新)?(?:买了|买的|买|购入|新买了|记一下|记一笔|记下|记录|登记|添加|入库|新增|有)|还有)+",
        "",
        text,
    )
    text = text.strip(" ，,。；;：:")
    # Brand names can contain digits. Only count a number when followed by a unit.
    m = re.search(rf"({NUM})([{UNIT}])", text)
    if len(re.findall(rf"{NUM}[{UNIT}]", text)) > 1 or re.search(r"[和、]|以及", text):
        raise ValueError("请把每种商品和它的日期分开说。")
    qty, unit = (number(m[1]), m[2]) if m else (1, "份")
    if m:
        text = (text[: m.start()] + text[m.end() :].lstrip("的")).strip(" ，,")
    text = re.sub(
        r"(，|,)?(?:到期日(?:期)?|有效期(?:至)?|保质期(?:到)?|到期|到|在|是|请|想|要|等|于|截止|截至|的)+$",
        "",
        text,
    ).strip(" ，,")
    if (
        not text
        or not 0 < qty <= 999
        or len(text) > 30
        or re.search(
            r"[，,；;。？！?!]|提醒|一下|生产|个月|怎么|什么|请问|查询|还有多少", text
        )
    ):
        raise ValueError("请说商品名、数量和日期，例如「两盒牛奶，11月5日到期」。")
    return text, qty, unit


def spoken_add_receipt(db, entries, today):
    """Describe only stock on hand before this utterance, inside its transaction."""
    parts = ["已记录。"]
    for name in dict.fromkeys(r["name"] for r in entries):
        old = db.execute(
            """SELECT unit, due, kind, SUM(qty) AS qty FROM batches
               WHERE name=? AND qty>0 GROUP BY due, kind, unit ORDER BY due, kind, unit""",
            (name,),
        ).fetchall()
        if not old:
            continue
        totals = {}
        dates = []
        for r in old:
            unit = r["unit"]
            totals[unit] = totals.get(unit, 0) + r["qty"]
            if r["kind"] == "stock":
                dates.append(
                    ("" if len(old) == 1 else f"{r['qty']}{unit}") + "未设日期"
                )
                continue
            d = date.fromisoformat(r["due"])
            day = (
                f"{d.year}年" if d.year != today.year else ""
            ) + f"{d.month}月{d.day}日"
            action = "到期" if r["kind"] == "expiry" else "提醒查看"
            dates.append(("" if len(old) == 1 else f"{r['qty']}{unit}") + day + action)
        count = "、".join(f"{qty}{unit}" for unit, qty in totals.items())
        detail = "，".join(dates[:3])
        if len(dates) > 3:
            detail += "，其他日期可以查清单"
        parts.append(f"{name}之前还有{count}，{detail}。")
    return "".join(parts)


def add(db, text, today, channel):
    # Only split multiple items after a complete date or an explicit semicolon.
    pieces = re.split(r"[;；。](?=.)|[，,、]?\s*(?:另外还?有|还有)(?=.)", text)
    entries = []
    for piece in filter(None, pieces):
        piece, location = take_location(piece)
        dates = list(DATE.finditer(piece))
        # A purchase date in the opening phrase is context, not the expiry.
        if piece.startswith("今天") and "买" in piece[:7]:
            piece = piece[2:]
            dates = list(DATE.finditer(piece))
        if re.search("生产|出厂", piece) or re.search(
            rf"保质期(?:为)?{NUM}(?:天|个?月|年)", piece
        ):
            return "请告诉我包装上的到期日期；如果只是想过几天查看，可以说「菠菜，三天后提醒我看看」。"
        if len(dates) > 1:
            return "这句话里有多个日期，请分别说清每种商品和它的到期日或查看日。"
        if dates:
            m = dates[0]
            name, qty, unit = product(piece[: m.start()])
            due = parse_date(m[0], today)
            tail = piece[m.end() :].strip(" ，,。；;")
            if tail and not re.fullmatch(
                r"(?:到期|过期|失效|截止|前|之前|是|的|请|就|再|要|提醒|我|我们|吃|用|一下|看|看看|查看|检查|处理|记得|用完|吃完|了|吧|[，, ])+",
                tail,
            ):
                return "日期后面的内容我没听清，请把每种商品和日期分开说。"
            kind = (
                "check"
                if CHECK.search(tail) and not re.search("到期|过期|失效", piece)
                else "expiry"
            )
            entries.append(
                dict(name=name, qty=qty, unit=unit, due=due.isoformat(), kind=kind)
            )
        elif NO_DATE.search(piece):
            name, qty, unit = product(NO_DATE.sub("", piece))
            entries.append(dict(name=name, qty=qty, unit=unit, due="", kind="stock"))
        else:
            name, qty, unit = product(piece)
            if len([p for p in pieces if p]) > 1:
                return f"{name}还缺日期，这次没有入库。请补全后再一起说。"
            prefix = f"{qty}{unit}{name}" + (f"，放在{location}" if location else "")
            remember(db, channel, {"action": "date", "prefix": prefix})
            return f"{name}哪天到期，还是哪天提醒查看？也可以说「只记库存」。"
        entries[-1]["location"] = location
    if not entries:
        return "请告诉我商品名和日期。"
    return save_entries(db, entries, today, channel)


def save_entries(db, entries, today, channel):
    receipt = spoken_add_receipt(db, entries, today) if channel == "speaker" else None
    after = []
    for r in entries:
        cur = db.execute(
            "INSERT INTO batches(name,qty,unit,due,kind,location) VALUES(:name,:qty,:unit,:due,:kind,:location)",
            {"location": "", **r},
        )
        after.append(row_for(db, cur.lastrowid))
    save_change(db, [], after, "add")
    record_focus(db, channel, after[0]["id"] if len(after) == 1 else None)
    return receipt or ("已记录：" + summary(after))


def change(db, ident, op, value=None):
    old = row_for(db, ident)
    if not old or old["qty"] <= 0:
        return "这批商品已经用完或已撤销。"
    if op == "review_tomorrow":
        if old["kind"] != "check" or old["due"] > value.isoformat():
            return "只有已到查看日的批次可以推迟查看。"
        db.execute(
            "UPDATE batches SET due=? WHERE id=?",
            ((value + timedelta(days=1)).isoformat(), ident),
        )
    elif op == "date":
        kind = "expiry" if old["kind"] == "stock" else old["kind"]
        db.execute("UPDATE batches SET due=?,kind=? WHERE id=?", (value, kind, ident))
    elif op == "location":
        db.execute(
            "UPDATE batches SET location=? WHERE id=?", (location_name(value), ident)
        )
    elif op in ("consume", "discard"):
        count = old["qty"] if value is None else int(value)
        if not 0 < count <= old["qty"]:
            return f"这批只剩{old['qty']}{old['unit']}，请核对数量。"
        db.execute("UPDATE batches SET qty=qty-? WHERE id=?", (count, ident))
    else:
        raise ValueError("请检查批次操作。")
    new = row_for(db, ident)
    save_change(db, [old], [new], op)
    if op == "review_tomorrow":
        return f"已将{old['name']}的查看日推迟到明天。"
    if op == "discard":
        return f"已记录丢弃{old['name']}{count}{old['unit']}，这批还剩{new['qty']}{old['unit']}。"
    if op == "location":
        return "已更新存放位置：" + (new["location"] or "未指定") + "。"
    if op == "date":
        return "已改期：" + describe(new) + "。"
    return (
        f"已更新，{old['name']}这批还剩{new['qty']}{old['unit']}。"
        if new["qty"]
        else f"已记下，{old['name']}这批用完了。"
    )


def interpret(db, raw, today, channel):
    s = normalize(raw)
    if not s:
        return "可以说「两盒酸奶，明天到期」，也可以问「什么快到期了」。"
    pending = db.execute(
        "SELECT body FROM pending WHERE channel=? AND until>?", (channel, time.time())
    ).fetchone()
    if pending:
        data = json.loads(pending[0])
        if data["action"] == "date" and (DATE.match(s) or NO_DATE.fullmatch(s)):
            db.execute("DELETE FROM pending WHERE channel=?", (channel,))
            return add(db, data["prefix"] + "，" + s, today, channel)
        if data["action"] == "batch":
            m = re.fullmatch(rf"(?:选|选第|第)?({NUM})(?:个|批|号)?", s)
            if m and 1 <= number(m[1]) <= len(data["ids"]):
                db.execute("DELETE FROM pending WHERE channel=?", (channel,))
                return change(
                    db, data["ids"][number(m[1]) - 1], data["op"], data["value"]
                )
    db.execute("DELETE FROM pending WHERE channel=?", (channel,))
    if s in ("取消", "算了", "退出"):
        return "已取消。"
    if re.search("不要|别记|不买|没买|不用记|取消入库|不确定", s):
        return "这次没有入库。"
    if s.startswith("撤销"):
        if channel == "speaker" and int(
            get_setting(db, "voice_change", "0")
        ) != last_change(db):
            return "有人刚刚更新过库存，请在清单里核对要撤销的那一笔。"
        return undo(db)
    if re.search("明天再提醒|今天别提醒|明天提醒我再说", s):
        put(db, "skip_day", today.isoformat())
        return "今天的主动提醒已暂停，明天重新检查库存。"
    if s in ("连接检查", "测试连接", "测试", "能听见吗"):
        return "保质期管家已经连接。"
    place = query_location(db, s)
    if re.search(r"放在?哪|存在哪", s):
        matches = [r for r in inventory(db) if r["name"] in s]
        if matches:
            return (
                "；".join(
                    f"{r['name']}{r['qty']}{r['unit']}，"
                    + ("放在" + r["location"] if r["location"] else "还没记位置")
                    for r in matches[:5]
                )
                + "。"
            )
        return "没找到这个商品。"
    if (wanted := date_query(s)) is not None:
        rows = [
            r for r in at_location(due_items(db, today), place) if r["status"] in wanted
        ]
        return summary(rows) if rows else "目前没有符合条件的在库商品。"
    if re.search(
        "什么时候到期|哪天到期|几号到期|还剩|有多少|还有多少|还有几", s
    ) and not s.startswith(("哪些", "什么", "最近")):
        matches = [r for r in at_location(inventory(db), place) if r["name"] in s]
        if matches:
            return summary(matches)
        if not re.search("什么|哪些|库存|清单", s):
            return "没找到这个商品。可以说「查库存」听现有清单。"
    if re.search(
        r"(?:什么|啥|哪些).*(?:临期|过期|到期)|(?:临期|过期|到期).*(?:什么|哪些|啥)|^(?:查|看看|查询)?(?:临期|快过期了|快到期了)$",
        s,
    ):
        return summary(at_location(due_items(db, today), place))
    if re.search(
        "库存|清单|还剩|还有什么|还有啥|有什么|有哪些|有啥|有多少|还有多少|什么时候到期|哪天到期|还有几",
        s,
    ) and not NO_DATE.search(s):
        rows = at_location(inventory(db), place)
        matches = [r for r in rows if r["name"] in s]
        if not matches and re.search("什么时候|哪天|有多少|还有多少|还有几", s):
            return "没找到这个商品。可以说「查库存」听现有清单。"
        return (
            summary(matches or rows)
            if rows
            else (f"{place}没有登记在库的商品。" if place else "库存是空的。")
        )
    consume = CONSUME.search(s)
    move = MOVE.search(s)
    if consume or move:
        op = "date" if move else ("discard" if DISCARD.search(s) else "consume")
        value = None
        if move:
            dates = DATE.findall(s[move.end() :])
            if len(dates) != 1:
                return "请说新的日期，例如「牛奶改到11月8日」。"
            value = parse_date(dates[0], today).isoformat()
        else:
            n = re.search(rf"({NUM})([{UNIT}])", s)
            if n:
                value = number(n[1])
            elif not (
                re.search("完|丢掉|扔掉", s) or (op == "discard" and "整批" in s)
            ):
                return (
                    "请说用了多少，例如「酸奶吃了一盒」。整批用完可以说「酸奶吃完了」。"
                )
        matches = [r for r in at_location(inventory(db), place) if r["name"] in s]
        if not matches and place is None and bare_change(s):
            focus = focused_batch(db, channel)
            if focus and focus["qty"] > 0:
                matches = [focus]
        if not matches:
            return "没找到匹配的在库商品。"
        longest = max(len(r["name"]) for r in matches)
        matches = [r for r in matches if len(r["name"]) == longest]
        if consume and value is not None:
            matches = [r for r in matches if r["unit"] == n[2]]
            if not matches:
                return "数量单位和库存不一致，请在清单里选对应批次核对。"
        if len(matches) > 1:
            remember(
                db,
                channel,
                {
                    "action": "batch",
                    "ids": [r["id"] for r in matches],
                    "op": op,
                    "value": value,
                },
            )
            return (
                "你指哪一批？"
                + "；".join(
                    f"第{i}批，{describe(r)}" for i, r in enumerate(matches[:5], 1)
                )
                + "。请回复第几批。"
            )
        record_focus(db, channel, matches[0]["id"])
        return change(db, matches[0]["id"], op, value)
    return add(db, s, today, channel)


def transact(db, request_id, work):
    db.execute("BEGIN IMMEDIATE")
    try:
        old = db.execute("SELECT reply FROM turns WHERE id=?", (request_id,)).fetchone()
        if old:
            db.commit()
            return old[0], False
        reply = work()
        db.execute("INSERT INTO turns VALUES(?,?)", (request_id, reply))
        db.commit()
        return reply, True
    except BaseException:
        db.rollback()
        raise


def command(db, request_id, raw, today, channel):
    def run():
        # Roll back partial changes if the date or count cannot be parsed.
        db.execute("SAVEPOINT parse")
        try:
            previous = last_change(db) if channel == "speaker" else None
            reply = interpret(db, raw, today, channel)
            if channel == "speaker" and last_change(db) > previous:
                put(db, "voice_change", str(last_change(db)))
            return reply
        except (ValueError, OverflowError) as e:
            db.execute("ROLLBACK TO parse")
            return (
                str(e) if str(e).startswith("请") else "日期或数量没听清，请再说一次。"
            )
        finally:
            db.execute("RELEASE parse")

    return transact(db, request_id, run)
