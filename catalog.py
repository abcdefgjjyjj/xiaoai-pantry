"""Household product dictionary. Keys identify products, never inventory batches."""

import re
import unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(name TEXT PRIMARY KEY, unit TEXT NOT NULL, location TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_keys(key TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL REFERENCES products(name));
CREATE INDEX IF NOT EXISTS keys_by_product ON product_keys(name);
"""


class InputError(ValueError):
    pass


def key(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def barcode(value):
    return bool(re.fullmatch(r"[0-9]{8}|[0-9]{12,14}", value))


def listing(db):
    rows = {
        r["name"]: dict(r, aliases=[], barcodes=[])
        for r in db.execute("SELECT * FROM products ORDER BY name")
    }
    for r in db.execute("SELECT * FROM product_keys WHERE kind!='name' ORDER BY label"):
        rows[r["name"]][r["kind"]].append(r["label"])
    return list(rows.values())


def lookup(db, value):
    return db.execute(
        "SELECT p.* FROM products p JOIN product_keys k ON p.name=k.name WHERE k.key=?",
        (key(value),),
    ).fetchone()


def save(db, body, place):
    def field(value, limit, optional=False):
        if (
            not isinstance(value, str)
            or not (int(not optional) <= len(value.strip()) <= limit)
            or any(ord(c) < 32 for c in value.strip())
        ):
            raise InputError("请核对商品名、单位和别名的长度。")
        return value.strip()

    name = field(body.get("name"), 60)
    unit = field(body.get("unit", "件"), 8)
    all_keys = [(name, "name")]
    for kind in ("aliases", "barcodes"):
        labels = body.get(kind, [])
        if not isinstance(labels, list) or len(labels) > 32:
            raise InputError("请把别名、条码分别作为列表提交，每种最多 32 个。")
        all_keys.extend((field(v, 60), kind) for v in labels)
    used = set()
    for label, kind in all_keys:
        k = key(label)
        if kind == "barcodes" and not barcode(k):
            raise InputError("请用文本填写 8、12、13 或 14 位数字条码，保留开头的零。")
        if kind != "barcodes" and k.isdecimal():
            raise InputError("请使用带文字的商品名或别名，数字条码填在条码栏。")
        found = lookup(db, k)
        if k in used or (found and found["name"] != name):
            raise InputError("名称、别名或条码重复，或已属于其他常买商品。")
        used.add(k)
    db.execute(
        "INSERT INTO products VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET unit=excluded.unit,location=excluded.location",
        (name, unit, place),
    )
    db.execute("DELETE FROM product_keys WHERE name=?", (name,))
    db.executemany(
        "INSERT INTO product_keys VALUES(?,?,?,?)",
        [(key(v), v, t, name) for v, t in all_keys],
    )
    return "常买商品已保存：" + name + "。"


def expand_query(db, text):
    # Match once, longest label first. An alias contained in its own canonical name
    # must not expand twice. Purchases resolve their parsed names by exact lookup.
    labels = {
        r["label"].casefold(): r["name"]
        for r in db.execute(
            "SELECT label,name FROM product_keys WHERE kind!='barcodes'"
        )
    }
    if not labels:
        return text
    pattern = "|".join(re.escape(k) for k in sorted(labels, key=len, reverse=True))
    return re.sub(
        pattern, lambda m: labels.get(m[0].casefold(), m[0]), text, flags=re.IGNORECASE
    )
