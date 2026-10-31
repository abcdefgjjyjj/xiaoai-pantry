"""Regression tests for household speech and duplicate physical reminders."""

import csv
import io
import json
import tempfile
import threading
import unittest
from datetime import date, datetime
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import app
import catalog
import gateway
import pantry
import speaker


class HouseholdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "pantry.sqlite3"
        self.db = pantry.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript(pantry.SCHEMA)
        self.today = date(2026, 10, 31)
        self.seq = 0

    def utter(self, text, channel="speaker"):
        self.seq += 1
        return pantry.command(self.db, str(self.seq), text, self.today, channel)[0]

    def test_http_catalog_shelf_replay_conflict_and_diagnosis(self):
        with (
            patch.object(app, "DB", self.path),
            patch.object(app, "TOKEN", "local-test"),
            app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler) as server,
        ):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:

                def request(path, body=None, auth=True):
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                    headers = {"Content-Type": "application/json"}
                    if auth:
                        headers["Authorization"] = "Bearer local-test"
                    try:
                        conn.request(
                            "POST" if body is not None else "GET",
                            path,
                            None if body is None else json.dumps(body).encode(),
                            headers,
                        )
                        resp = conn.getresponse()
                        return resp.status, json.loads(resp.read())
                    finally:
                        conn.close()

                p = {
                    "id": "catalog-create",
                    "name": "牙膏",
                    "unit": "管",
                    "location": "卫生间",
                    "aliases": ["薄荷膏"],
                    "barcodes": ["00123456"],
                }
                self.assertEqual(request("/api/catalog", p, False)[0], 401)
                self.assertEqual(request("/api/catalog", p)[0], 200)
                self.assertEqual(
                    request(
                        "/api/catalog", dict(p, id="catalog-conflict", name="另一款")
                    )[0],
                    400,
                )
                item = {
                    "id": "purchase-1",
                    "barcode": "00123456",
                    "qty": 2,
                    "produced_on": "2024-02-29",
                    "shelf_value": 1,
                    "shelf_unit": "year",
                }
                for _ in range(2):
                    self.assertEqual(request("/api/add", item)[0], 200)
                for change in [
                    {"barcode": "99999999"},
                    {"due": "2025-03-01"},
                    {"shelf_value": True},
                ]:
                    self.assertEqual(
                        request(
                            "/api/add",
                            dict(item, **change, id="invalid-" + str(len(change))),
                        )[0],
                        400,
                    )
                status, state = request("/api/state")
                self.assertEqual(status, 200)
                self.assertEqual(len(state["catalog"]), 1)
                self.assertEqual(len(state["rows"]), 1)
                row = state["rows"][0]
                self.assertEqual(
                    (row["due"], row["unit"], row["location"]),
                    ("2025-02-28", "管", "卫生间"),
                )
                self.assertEqual(state["reminder_today"]["due_count"], 1)
            finally:
                server.shutdown()
                thread.join(5)

    def test_shelf_dates_provenance_and_manual_override(self):
        raw = "买了两盒牛奶，10月20日生产，保质期30天"
        self.assertTrue(pantry.natural_voice(self.db, raw))
        self.assertIn("已记录", self.utter(raw))
        r = pantry.inventory(self.db)[0]
        self.assertEqual(
            (r["due"], r["produced_on"], r["shelf_value"], r["shelf_unit"]),
            ("2026-11-19", "2026-10-20", 30, "day"),
        )
        self.assertIn("已改期", self.utter("牛奶改到11月18日"))
        self.assertFalse(pantry.row_for(self.db, r["id"])["produced_on"])
        self.utter("撤销")
        self.assertEqual(pantry.row_for(self.db, r["id"]), r)
        for raw, expected in [
            ("买了一盒面霜，生产日期是2024年2月29日，保质期一年", "2025-02-28"),
            ("面霜，保质期为一个月，1月31日生产", "2026-02-28"),
            ("买了一盒昨天生产的面霜，保质期2天", "2026-11-01"),
        ]:
            self.assertIn("已记录", self.utter(raw))
            self.assertEqual(
                self.db.execute(
                    "SELECT due FROM batches ORDER BY id DESC LIMIT 1"
                ).fetchone()[0],
                expected,
            )
        for raw in [
            "牛奶，明天生产，保质期30天",
            "牛奶，10月20日生产，保质期0天",
            "牛奶，10月20日生产，保质期两年，明天到期",
            "牛奶，保质期30天",
            "牛奶，2月30日生产，保质期30天",
        ]:
            self.assertNotIn("已记录", self.utter(raw))
        self.assertEqual(len(pantry.inventory(self.db)), 4)

    def test_shopping_list_is_atomic_and_common_date_is_explicit(self):
        for raw in [
            "买了两盒牛奶明天到期一袋面包后天到期",
            "买了两盒牛奶和一袋面包，都明天到期",
            "两盒牛奶，10月20日生产，保质期30天，另外一管牙膏放在卫生间，只记库存",
        ]:
            before = len(pantry.inventory(self.db))
            self.assertIn("已记录", self.utter(raw), raw)
            self.assertEqual(len(pantry.inventory(self.db)), before + 2)
        self.assertEqual(
            [r[0] for r in self.db.execute("SELECT due FROM batches ORDER BY id")],
            ["2026-11-01", "2026-11-02", "2026-11-01", "2026-11-01", "2026-11-19", ""],
        )
        for raw in [
            "买了一盒牛奶和一袋面包，明天到期",
            "买了一盒牛奶明天到期，还有一袋面包",
            "买了一盒牛奶明天到期，还有",
            "买了一盒牛奶明天到期，另外牙膏，2月30日到期",
        ]:
            self.assertNotIn("已记录", self.utter(raw), raw)
        self.assertEqual(len(pantry.inventory(self.db)), 6)
        self.utter("撤销", "web")
        self.assertEqual(len(pantry.inventory(self.db)), 4)

    def test_catalog_aliases_barcodes_separate_batches(self):
        item = {
            "name": "早餐纯牛奶",
            "unit": "盒",
            "aliases": ["小白奶"],
            "barcodes": ["00123456"],
        }
        with self.db:
            catalog.save(self.db, item, "橱柜")
        self.assertEqual(catalog.listing(self.db)[0]["barcodes"], ["00123456"])
        for raw in ["买了小白奶，明天到期", "买了一瓶00123456，放在书房，后天到期"]:
            self.assertIn("已记录", self.utter(raw))
        a, b = pantry.inventory(self.db)
        self.assertEqual(
            (a["name"], a["unit"], a["location"]), ("早餐纯牛奶", "盒", "橱柜")
        )
        self.assertEqual(
            (b["name"], b["unit"], b["location"]), ("早餐纯牛奶", "瓶", "书房")
        )
        self.assertTrue(pantry.natural_voice(self.db, "小白奶还有多少"))
        self.assertIn("早餐纯牛奶", self.utter("小白奶还有多少"))
        self.utter("小白奶喝了一瓶")
        self.assertEqual(pantry.inventory(self.db), [a])
        self.assertNotIn(
            "已记录", self.utter("一瓶牛奶明天到期，另外一箱12345678明天到期")
        )
        self.assertEqual(pantry.inventory(self.db), [a])
        with self.assertRaises(catalog.InputError), self.db:
            catalog.save(self.db, dict(item, name="其他牛奶"), "")
        with self.db:
            catalog.save(self.db, dict(item, aliases=["早餐奶"], barcodes=[]), "")
        self.assertIsNone(catalog.lookup(self.db, "00123456"))
        self.assertIsNone(catalog.lookup(self.db, "小白奶"))
        self.assertEqual(pantry.inventory(self.db), [a])

    @patch.dict(
        "os.environ",
        {"XIAOMUSIC_URL": "configured", "XIAOAI_KITCHEN_DID": "configured"},
    )
    def test_current_reasons_and_persisted_daytime_check(self):
        noon = datetime(2026, 10, 31, 12, tzinfo=app.SHANGHAI)

        def observation(occupied=True, minutes=2, age=0):
            return (
                "ok",
                gateway.PresenceSample(
                    occupied,
                    int(noon.timestamp() - age),
                    "",
                    "fresh",
                    duration_minutes=minutes,
                ),
            )

        self.assertEqual(
            set(
                app.reminder_status(self.db, noon.replace(hour=23), observation())[0][
                    "reasons"
                ]
            ),
            {"no_items", "quiet", "stale"},
        )
        self.utter("一盒酸奶，明天到期")
        for obs, code in [
            (observation(False), "vacant"),
            (observation(minutes=0), "short_stay"),
            (observation(age=181), "stale"),
            (observation(age=-1), "stale"),
            (("error", None), "sensor_error"),
        ]:
            report = app.reminder_status(self.db, noon, obs)[0]
            self.assertIn(code, report["reasons"])
            self.assertIsNone(app.reserve_summary(self.db, noon, obs))
        night = app.reminder_status(self.db, noon.replace(hour=22), observation())[0]
        self.assertIn("读取失败", night["last_window_check"]["headline"])
        self.assertIsNone(night["notice_state"])
        self.assertIn("酸奶", app.reserve_summary(self.db, noon, observation()))
        self.assertIsNone(app.reserve_summary(self.db, noon, observation()))
        with self.db:
            self.db.execute("UPDATE notices SET state='结果未确认'")
        self.assertIn(
            "不再重复", app.reminder_status(self.db, noon, observation())[0]["headline"]
        )
        nextday = app.reminder_status(
            self.db, noon.replace(month=11, day=1), observation()
        )[0]
        self.assertIsNone(nextday["notice_state"])
        self.assertIsNone(nextday["last_window_check"])

    @patch.dict(
        "os.environ",
        {"XIAOMUSIC_URL": "configured", "XIAOAI_KITCHEN_DID": "configured"},
    )
    def reserve_occupied(self, db, dt):
        sample = gateway.PresenceSample(
            True, int(dt.timestamp()), "occupied", "fresh", duration_minutes=2
        )
        return app.reserve_summary(db, dt, ("ok", sample))

    def test_natural_language_without_prefix(self):
        def voice(text):
            self.assertTrue(pantry.natural_voice(self.db, text))
            return self.utter(text)

        for raw in [
            "明天提醒我开会",
            "告诉我明天的天气",
            "我的会员明天到期",
            "如果我买了两盒酸奶会坏吗",
            "播放牛奶咖啡的歌",
            "明天到期",
            "第一批",
        ]:
            self.assertFalse(pantry.natural_voice(self.db, raw), raw)
        voice("我今天刚买了两盒测试酸奶，明天到期")
        self.assertEqual(
            [(r["name"], r["qty"]) for r in pantry.inventory(self.db)],
            [("测试酸奶", 2)],
        )
        voice("喝掉了一盒")
        self.assertNotIn("测试酸奶", voice("冰箱里有什么快过期了"))
        self.assertIn("测试酸奶1盒", voice("家里有什么快过期了"))
        voice("刚买了一管牙膏")
        voice("下周五到期")
        self.assertEqual(pantry.inventory(self.db)[-1]["name"], "牙膏")

    def test_mixed_shopping_and_partial_use(self):
        self.utter("两盒酸奶，11月5日到期；还有一把菠菜，明天提醒我看看")
        self.utter("一盒酸奶，11月2日到期")
        prompt = self.utter("酸奶吃了一盒")
        self.assertIn("哪一批", prompt)
        self.utter("第二批")
        self.assertEqual([r["qty"] for r in pantry.inventory(self.db)], [1, 1, 1])
        self.assertIn("11月5日", self.utter("酸奶什么时候到期"))
        self.assertEqual(
            [r["name"] for r in pantry.due_items(self.db, self.today)], ["酸奶"]
        )
        self.utter("撤销刚才那条")
        self.assertEqual(pantry.inventory(self.db)[-1]["qty"], 2)

    def test_spoken_receipt_counts_only_previous_remaining_stock(self):
        self.assertEqual(self.utter("买了三盒牛奶，明天到期"), "已记录。")
        self.utter("牛奶喝了一盒")
        self.assertEqual(
            self.utter("又买了一盒牛奶，后天到期"),
            "已记录。牛奶之前还有2盒，11月1日到期。",
        )
        self.assertIn("11月1日", self.utter("一盒牛奶，明天到期", "web"))
        self.utter("一瓶牛奶，明天提醒我看看", "web")
        self.assertEqual(
            self.utter("一盒牛奶，11月5日到期；还有两盒酸奶，明天到期"),
            "已记录。牛奶之前还有1瓶、4盒，1瓶11月1日提醒查看，3盒11月1日到期，1盒11月2日到期。",
        )
        self.utter("酸奶吃完了")
        self.assertEqual(self.utter("又买了一盒酸奶，明天到期"), "已记录。")

    def test_single_utterance_cannot_report_itself_as_previous_stock(self):
        raw = "一盒牛奶，明天到期；还有两盒牛奶，后天到期"
        for fresh in (True, False):
            self.assertEqual(
                pantry.command(self.db, "same-turn", raw, self.today, "speaker"),
                ("已记录。", fresh),
            )
        self.assertEqual(sum(r["qty"] for r in pantry.inventory(self.db)), 3)

    def test_locations_apply_per_batch_across_queries_and_reminders(self):
        self.utter("买了两盒牛奶，放在冷藏室，明天到期")
        self.utter("买了一盒牛奶，明天到期，放在橱柜")
        self.utter("买了一管牙膏，放在卫生间")
        self.utter("明天到期")
        self.utter("买了一支放大镜，只记库存")
        self.assertTrue(pantry.natural_voice(self.db, "橱柜里有什么"))
        self.assertIn("牛奶2盒", self.utter("冰箱里有什么快到期了"))
        self.assertNotIn("牛奶1盒", self.utter("冰箱里有什么快到期了"))
        self.assertNotIn("牛奶", self.utter("客房抽屉里有什么快到期了"))
        self.assertIn("橱柜", self.utter("牛奶放在哪里"))
        self.assertIn("卫生间", self.utter("家里有什么快到期了"))
        self.assertIn("放大镜", self.utter("查库存"))
        self.utter("橱柜里的牛奶喝完了")
        milk = [r for r in pantry.inventory(self.db) if r["name"] == "牛奶"]
        self.assertEqual([(r["location"], r["qty"]) for r in milk], [("冰箱冷藏室", 2)])
        notice = self.reserve_occupied(
            self.db, datetime(2026, 10, 31, 12, 0, tzinfo=app.SHANGHAI)
        )
        self.assertIn("卫生间", notice)
        self.assertIn("冰箱冷藏室", notice)

    def test_migrate_and_undo_pre_location_history(self):
        import json

        with self.db:
            self.db.execute("DROP TABLE batches")
            self.db.execute(
                "CREATE TABLE batches(id INTEGER PRIMARY KEY, name TEXT, qty INTEGER, unit TEXT, due TEXT, kind TEXT)"
            )
            old = {
                "id": 1,
                "name": "纸巾",
                "qty": 3,
                "unit": "包",
                "due": "",
                "kind": "stock",
            }
            new = dict(old, qty=1)
            self.db.execute(
                "INSERT INTO batches VALUES(:id,:name,:qty,:unit,:due,:kind)", new
            )
            self.db.execute(
                "INSERT INTO changes(before_json,after_json) VALUES(?,?)",
                (json.dumps([old]), json.dumps([new])),
            )
            pantry.initialize(self.db)
            pantry.initialize(self.db)
        self.assertIn("已撤销", self.utter("撤销", "web"))
        self.assertEqual(
            pantry.row_for(self.db, 1), dict(old, location="", **pantry.SHELF_DEFAULTS)
        )

    def test_review_and_discard_workflow_and_replay(self):
        self.utter("一把菠菜，今天提醒我看看")
        self.utter("两盒牛奶，今天到期，放在橱柜")
        self.utter("一盒牛奶，今天到期，放在冰箱")
        self.assertTrue(pantry.natural_voice(self.db, "家里有哪些东西要处理"))
        self.assertIn("菠菜", self.utter("家里有哪些东西要处理"))
        for fresh in (True, False):
            msg, ok = pantry.command(
                self.db,
                "voice:discard-once",
                "橱柜里的牛奶扔了一盒",
                self.today,
                "speaker",
            )
            self.assertEqual(ok, fresh)
            self.assertIn("丢弃", msg)
        last = pantry.recent_changes(self.db)[0]
        self.assertEqual((last["action"], last["items"][0]["delta"]), ("discard", -1))
        veg = next(r for r in pantry.inventory(self.db) if r["name"] == "菠菜")
        milk = next(r for r in pantry.inventory(self.db) if r["name"] == "牛奶")
        self.assertIn(
            "只有", pantry.change(self.db, milk["id"], "review_tomorrow", self.today)
        )
        with self.db:
            pantry.change(self.db, veg["id"], "review_tomorrow", self.today)
        self.assertNotIn("菠菜", self.utter("家里有哪些东西要处理"))
        self.utter("撤销", "web")
        self.assertTrue(pantry.recent_changes(self.db)[0]["undone"])
        self.assertIn("菠菜", self.utter("家里有哪些东西要处理"))

    def test_csv_export_round_trip_and_safe_spreadsheet_cells(self):
        entry = {
            "name": "=SUM(1,2)",
            "qty": 2,
            "unit": "盒",
            "location": '书房,"柜子"',
            "due": "",
            "kind": "stock",
        }
        pantry.transact(
            self.db,
            "export-fixture",
            lambda: pantry.save_entries(self.db, [entry], self.today, "web:test"),
        )
        self.utter("一盒饼干，明天到期")
        self.utter("饼干吃完了")
        rows = list(
            csv.DictReader(
                io.StringIO(app.inventory_csv(self.db, self.today).decode("utf-8-sig"))
            )
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["名称"], rows[0]["数量"], rows[0]["存放位置"], rows[0]["状态"]),
            ("'=SUM(1,2)", "2", entry["location"], "未设日期"),
        )
        self.assertEqual(pantry.inventory(self.db)[0]["name"], entry["name"])

    def test_consume_matches_the_spoken_unit(self):
        self.utter("两瓶牛奶，明天到期")
        self.utter("三盒牛奶，明天到期")
        self.utter("牛奶喝了一盒")
        self.assertIn("单位", self.utter("牛奶喝了一杯"))
        self.assertEqual(
            {r["unit"]: r["qty"] for r in pantry.inventory(self.db)}, {"瓶": 2, "盒": 2}
        )

    def test_invalid_multi_item_rolls_back_and_missing_date_can_be_completed(self):
        self.utter("两盒牛奶，明天到期；还有一支牙膏，2月30日到期")
        self.assertEqual(pantry.inventory(self.db), [])
        self.assertIn("哪天", self.utter("一支牙膏"))
        self.utter("下周五到期")
        self.assertEqual(pantry.inventory(self.db)[0]["due"], "2026-11-06")

    def test_only_fresh_prefixed_speech_is_committed_once(self):
        ts = datetime(2026, 10, 31, 15, 0, tzinfo=app.SHANGHAI).timestamp()
        events = [
            {"id": "a", "ts": ts - 600, "text": "保质期管家，白菜，明天到期"},
            {"id": "b", "ts": ts, "text": "关一下灯"},
            {"id": "c", "ts": ts, "text": "保质期管家，牛奶两盒，明天到期"},
        ]
        with (
            patch.object(speaker, "poll", return_value=events),
            patch("app.time.time", return_value=ts),
            patch("app.say") as say,
        ):
            app.voice_step(self.db, ts - 5)
            app.voice_step(self.db, ts)
            self.assertEqual(say.call_count, 1)
        self.assertEqual(
            [(r["name"], r["qty"]) for r in pantry.inventory(self.db)], [("牛奶", 2)]
        )

    def test_quiet_window_and_persistent_daily_reservation(self):
        self.utter("一盒豆腐，明天到期")
        dt = datetime(2026, 10, 31, 22, 0, tzinfo=app.SHANGHAI)
        self.assertIsNone(self.reserve_occupied(self.db, dt))
        dt = dt.replace(hour=12)
        self.assertIn("豆腐", self.reserve_occupied(self.db, dt))
        other = pantry.connect(self.path)
        try:
            self.assertIsNone(self.reserve_occupied(other, dt))
        finally:
            other.close()
        self.utter("豆腐吃完了")
        self.assertIsNone(self.reserve_occupied(self.db, dt.replace(month=11, day=1)))

    def test_stock_without_date_and_distinct_reminder_status(self):
        self.utter("买了两卷纸巾")
        self.assertTrue(pantry.natural_voice(self.db, "只记库存"))
        self.assertEqual(self.utter("只记库存"), "已记录。")
        self.assertIn("未设日期", self.utter("又买了一卷纸巾，只记库存"))
        for text in (
            "一盒牛奶，10月30日到期",
            "一包饼干，今天到期",
            "一支牙膏，后天到期",
            "一瓶墨水，三天后到期",
            "一把菠菜，今天提醒我看看",
        ):
            self.utter(text)
        self.assertEqual(
            {
                r["name"]: r["status"]
                for r in pantry.dated_inventory(self.db, self.today)
            },
            {
                "纸巾": "undated",
                "牛奶": "expired",
                "饼干": "today",
                "牙膏": "soon",
                "墨水": "later",
                "菠菜": "review",
            },
        )
        for question, included, excluded in [
            ("家里哪些东西已经过期了", "牛奶", ["饼干", "菠菜"]),
            ("什么快到期了", "牙膏", ["墨水", "纸巾", "牛奶"]),
            ("哪些商品今天到期", "饼干", ["牙膏", "牛奶"]),
            ("哪些东西该查看了", "菠菜", ["牛奶", "纸巾"]),
        ]:
            self.assertTrue(pantry.natural_voice(self.db, question))
            answer = self.utter(question)
            self.assertIn(included, answer)
            for name in excluded:
                self.assertNotIn(name, answer)
        notice = self.reserve_occupied(
            self.db, datetime(2026, 10, 31, 12, 0, tzinfo=app.SHANGHAI)
        )
        self.assertIn("已过期", notice)
        self.assertIn("待查看", notice)
        self.assertNotIn("纸巾", notice)


if __name__ == "__main__":
    unittest.main()
