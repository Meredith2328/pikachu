import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.protocol import Notification, ProtocolError


class TestNotification(unittest.TestCase):
    def test_roundtrip(self):
        n = Notification(title="你好", body="世界", level="warn", source="t")
        d = n.to_dict()
        n2 = Notification.from_dict(d)
        self.assertEqual(n2.title, "你好")
        self.assertEqual(n2.body, "世界")
        self.assertEqual(n2.level, "warn")
        self.assertEqual(n2.source, "t")
        self.assertEqual(n2.ttl, n.ttl)

    def test_defaults(self):
        n = Notification.from_dict({"title": "x"})
        self.assertEqual(n.body, "")
        self.assertEqual(n.level, "info")
        self.assertEqual(n.source, "pika")
        self.assertEqual(n.ttl, 10.0)

    def test_ttl_zero_allowed(self):
        n = Notification.from_dict({"title": "x", "ttl": 0})
        self.assertEqual(n.ttl, 0.0)

    def test_missing_title(self):
        for bad in ({}, {"title": ""}, {"title": "  "}, {"title": 123}):
            with self.assertRaises(ProtocolError):
                Notification.from_dict(bad)

    def test_bad_level(self):
        with self.assertRaises(ProtocolError):
            Notification.from_dict({"title": "x", "level": "boom"})

    def test_bad_ttl(self):
        for bad in ({"title": "x", "ttl": -1}, {"title": "x", "ttl": "abc"},
                    {"title": "x", "ttl": True}):
            with self.assertRaises(ProtocolError):
                Notification.from_dict(bad)

    def test_nan_infinity_rejected(self):
        """NaN/Infinity 必须拒绝：否则 to_dict 会序列化成非法 JSON。"""
        for bad_val in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ProtocolError):
                Notification.from_dict({"title": "x", "ttl": bad_val})
            with self.assertRaises(ProtocolError):
                Notification.from_dict({"title": "x", "ts": bad_val})
        # JSON 字面量 NaN 也应被拒绝
        with self.assertRaises(ProtocolError):
            Notification.from_json('{"title":"x","ttl":NaN}')

    def test_not_a_dict(self):
        with self.assertRaises(ProtocolError):
            Notification.from_dict([1, 2, 3])
        with self.assertRaises(ProtocolError):
            Notification.from_dict("title")

    def test_bad_json(self):
        with self.assertRaises(ProtocolError):
            Notification.from_json("{not json")


if __name__ == "__main__":
    unittest.main()
