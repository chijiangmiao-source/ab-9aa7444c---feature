"""HTTP-level tests for the review service."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import Handler


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def post_review(self, payload):
        request = urllib.request.Request(
            self.url("/api/review"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, text = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"status": "ok"})

    def test_index_page_served(self):
        status, text = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("复核", text)

    def test_static_assets_served(self):
        for path in ("/app.js", "/style.css"):
            status, _ = self.get(path)
            self.assertEqual(status, 200, path)

    def test_path_traversal_blocked(self):
        request = urllib.request.Request(self.url("/../app/server.py"))
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertIn(status, (400, 404))

    def test_review_solvable_exact_bigint(self):
        status, body = self.post_review(
            {
                "variables": ["K1", "K2", "K3"],
                "matrix": [
                    ["9007199254740993", "1", "0"],
                    ["1", "3", "1"],
                    ["0", "2", "4"],
                ],
                "target": ["18014398509481989", "12", "10"],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        self.assertEqual(body["solution"], ["2", "3", "1"])
        first = body["constraints"][0]
        self.assertEqual(first["terms"][0]["product"], str(9007199254740993 * 2))
        self.assertEqual(first["sum"], "18014398509481989")
        self.assertTrue(all(c["satisfied"] for c in body["constraints"]))

    def test_review_unsolvable_obstruction(self):
        status, body = self.post_review(
            {
                "variables": ["D1", "D2"],
                "matrix": [["2", "0"], ["0", "4"]],
                "target": ["9007199254740993", "8"],
            }
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["solvable"])
        obstruction = body["obstruction"]
        self.assertEqual(obstruction["type"], "non_divisible")
        self.assertEqual(obstruction["pivot"], "2")
        self.assertEqual(obstruction["transformedTarget"], "9007199254740993")
        self.assertEqual(obstruction["remainder"], "1")
        total = sum(int(term["product"]) for term in obstruction["uRowTerms"])
        self.assertEqual(str(total), obstruction["transformedTarget"])

    def test_json_number_integers_accepted(self):
        status, body = self.post_review(
            {"variables": ["x"], "matrix": [[4]], "target": [8]}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        self.assertEqual(body["solution"], ["2"])

    def test_float_coefficient_rejected(self):
        status, body = self.post_review(
            {"variables": ["x"], "matrix": [[1.5]], "target": [1]}
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_dimension_mismatch_rejected(self):
        status, body = self.post_review(
            {
                "variables": ["x", "y"],
                "matrix": [["1", "2"]],
                "target": ["1", "2"],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_unknown_route_404(self):
        request = urllib.request.Request(self.url("/nope"))
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)

    # ---------------------------------------------------------- 优选校正审计

    def review_payload(self):
        return {
            "variables": ["x", "y"],
            "matrix": [["1", "1"]],
            "target": ["2"],
        }

    def test_plain_review_has_no_optimization(self):
        status, body = self.post_review(self.review_payload())
        self.assertEqual(status, 200)
        self.assertNotIn("optimization", body)

    def test_audit_weighted_optimum(self):
        payload = self.review_payload()
        payload["preferences"] = [
            {"variable": "x", "preferred": "0", "weight": "1"},
            {"variable": "y", "preferred": "0", "weight": "100"},
        ]
        status, body = self.post_review(payload)
        self.assertEqual(status, 200)
        opt = body["optimization"]
        self.assertEqual(opt["correction"], ["2", "0"])
        self.assertEqual(opt["adjustments"], ["2", "0"])
        self.assertEqual(opt["cost"], "4")
        self.assertEqual(opt["perItemCost"], ["4", "0"])
        self.assertEqual(opt["tieBreak"], "lexicographic_adjustment")
        self.assertEqual(
            [entry["variable"] for entry in opt["preferred"]], ["x", "y"]
        )
        evidence = opt["evidence"]
        self.assertEqual(evidence["finalBound"], "4")
        self.assertEqual(evidence["constant"], "4")
        self.assertEqual(len(evidence["levels"]), 1)
        level = evidence["levels"][0]
        self.assertLessEqual(int(level["low"]), int(level["chosen"]))
        self.assertLessEqual(int(level["chosen"]), int(level["high"]))

    def test_audit_lexicographic_tie(self):
        payload = {
            "variables": ["x", "y"],
            "matrix": [["1", "1"]],
            "target": ["1"],
            "preferences": [
                {"variable": "x", "preferred": "0", "weight": "1"},
                {"variable": "y", "preferred": "0", "weight": "1"},
            ],
        }
        status, body = self.post_review(payload)
        self.assertEqual(status, 200)
        opt = body["optimization"]
        self.assertEqual(opt["cost"], "1")
        self.assertEqual(opt["adjustments"], ["0", "1"])
        self.assertGreaterEqual(opt["evidence"]["stats"]["tiesCompared"], 1)

    def test_audit_big_integer_exactness(self):
        big = str(2**70 + 12345)
        payload = {
            "variables": ["u", "v"],
            "matrix": [[big, "1"], ["1", "1"]],
            "target": [str((2**70 + 12345) * 5 + 7), "12"],
            "preferences": [
                {"variable": "u", "preferred": "0", "weight": "1"},
                {"variable": "v", "preferred": "0", "weight": "1"},
            ],
        }
        status, body = self.post_review(payload)
        self.assertEqual(status, 200)
        opt = body["optimization"]
        matrix = [[2**70 + 12345, 1], [1, 1]]
        target = [(2**70 + 12345) * 5 + 7, 12]
        correction = [int(value) for value in opt["correction"]]
        for i, row in enumerate(matrix):
            self.assertEqual(sum(a * x for a, x in zip(row, correction)), target[i])
        self.assertEqual(
            str(sum(int(c) for c in opt["perItemCost"])), opt["cost"]
        )

    def test_audit_rejections(self):
        base = self.review_payload()
        bad_sets = [
            [{"variable": "x", "preferred": "0", "weight": "1"}],  # 缺失 y
            [  # 重复变量
                {"variable": "x", "preferred": "0", "weight": "1"},
                {"variable": "x", "preferred": "1", "weight": "1"},
            ],
            [  # 首选值非整数
                {"variable": "x", "preferred": "0.5", "weight": "1"},
                {"variable": "y", "preferred": "1", "weight": "1"},
            ],
            [  # 权重为零
                {"variable": "x", "preferred": "0", "weight": "0"},
                {"variable": "y", "preferred": "1", "weight": "1"},
            ],
            [  # 权重为负
                {"variable": "x", "preferred": "0", "weight": "-1"},
                {"variable": "y", "preferred": "1", "weight": "1"},
            ],
            [  # 未声明变量
                {"variable": "z", "preferred": "0", "weight": "1"},
                {"variable": "y", "preferred": "1", "weight": "1"},
            ],
            [  # 首选值缺失键
                {"variable": "x", "weight": "1"},
                {"variable": "y", "preferred": "1", "weight": "1"},
            ],
            [  # 权重缺失键
                {"variable": "x", "preferred": "0"},
                {"variable": "y", "preferred": "1", "weight": "1"},
            ],
        ]
        for preferences in bad_sets:
            payload = dict(base)
            payload["preferences"] = preferences
            status, body = self.post_review(payload)
            self.assertEqual(status, 400, preferences)
            self.assertFalse(body["ok"])
            self.assertNotIn("optimization", body)

    def test_audit_preferences_not_a_list(self):
        payload = self.review_payload()
        payload["preferences"] = {"x": {"preferred": "0", "weight": "1"}}
        status, body = self.post_review(payload)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_unsolvable_keeps_obstruction_with_preferences(self):
        payload = {
            "variables": ["x", "y"],
            "matrix": [["2", "0"], ["0", "2"]],
            "target": ["3", "4"],
            "preferences": [
                {"variable": "x", "preferred": "0", "weight": "1"},
                {"variable": "y", "preferred": "1", "weight": "1"},
            ],
        }
        status, body = self.post_review(payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["solvable"])
        self.assertIn("obstruction", body)
        self.assertNotIn("optimization", body)


if __name__ == "__main__":
    unittest.main()
