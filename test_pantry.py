"""Regression tests for household speech and duplicate physical reminders."""

import tempfile
import csv
import io
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import app
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
        notice = app.reserve_summary(
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
            old = dict(id=1, name="纸巾", qty=3, unit="包", due="", kind="stock")
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
        self.assertEqual(pantry.row_for(self.db, 1), dict(old, location=""))

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
        entry = dict(
            name="=SUM(1,2)",
            qty=2,
            unit="盒",
            location='书房,"柜子"',
            due="",
            kind="stock",
        )
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
        self.assertIsNone(app.reserve_summary(self.db, dt))
        dt = dt.replace(hour=12)
        self.assertIn("豆腐", app.reserve_summary(self.db, dt))
        other = pantry.connect(self.path)
        try:
            self.assertIsNone(app.reserve_summary(other, dt))
        finally:
            other.close()
        self.utter("豆腐吃完了")
        self.assertIsNone(app.reserve_summary(self.db, dt.replace(month=11, day=1)))

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
        notice = app.reserve_summary(
            self.db, datetime(2026, 10, 31, 12, 0, tzinfo=app.SHANGHAI)
        )
        self.assertIn("已过期", notice)
        self.assertIn("待查看", notice)
        self.assertNotIn("纸巾", notice)


if __name__ == "__main__":
    unittest.main()
