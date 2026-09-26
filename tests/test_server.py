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

    # ------------------------------------------------------------- optimize
    def post_optimize(self, payload):
        request = urllib.request.Request(
            self.url("/api/optimize"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    OPTIMIZE_PAYLOAD = {
        "variables": ["K1", "K2", "K3"],
        "matrix": [
            ["9007199254740993", "1", "0"],
            ["1", "3", "1"],
        ],
        "target": ["18014398509481989", "12"],
        "preferred": ["1", "2", "0"],
        "weights": ["1", "2", "1"],
    }

    def test_optimize_returns_exact_weighted_optimum(self):
        status, body = self.post_optimize(self.OPTIMIZE_PAYLOAD)
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        opt = body["optimization"]
        # solution space is [2,3,1] + t*[0,-2,1]; the weighted optimum is exact
        values = [(item["variable"], item["optimal"], item["deviation"],
                   item["weightedTerm"]) for item in opt["items"]]
        self.assertEqual([v[1] for v in values], ["2", "3", "1"])
        self.assertEqual([v[2] for v in values], ["1", "1", "1"])
        self.assertEqual([v[3] for v in values], ["1", "2", "1"])
        self.assertEqual(opt["cost"], "4")
        self.assertEqual(opt["tieCount"], 1)
        self.assertEqual(
            sum(int(item["weightedTerm"]) for item in opt["items"]),
            int(opt["cost"]),
        )

    def test_optimize_lexicographic_tie(self):
        # x+y=1; prefer (2,1), weights (1,3).  Candidates (0,1):
        # (-2)^2*1 + 0 = 4 and (1,0): 1 + 3 = 4 tie.  Preferences are
        # distinct; the lexicographically smallest adjustment (-2,0) wins.
        payload = {
            "variables": ["x", "y"],
            "matrix": [["2", "2"]],
            "target": ["2"],
            "preferred": ["2", "1"],
            "weights": ["1", "3"],
        }
        status, body = self.post_optimize(payload)
        self.assertEqual(status, 200)
        opt = body["optimization"]
        self.assertEqual(opt["cost"], "4")
        self.assertEqual(opt["tieCount"], 2)
        self.assertEqual([i["optimal"] for i in opt["items"]], ["0", "1"])
        self.assertEqual([i["deviation"] for i in opt["items"]], ["-2", "0"])
        self.assertIn("字典序", opt["lexicographicRule"])

    def test_optimize_bound_evidence_present_and_exact(self):
        status, body = self.post_optimize(self.OPTIMIZE_PAYLOAD)
        self.assertEqual(status, 200)
        evidence = body["optimization"]["boundEvidence"]
        self.assertTrue(evidence["babaiPoint"])
        self.assertGreaterEqual(int(evidence["babaiCost"]), int(body["optimization"]["cost"]))
        self.assertIn("/", evidence["gramSchmidt"]["squaredNorms"][0])
        self.assertIn("/", evidence["orthogonalResidual"])
        self.assertGreaterEqual(int(evidence["commonDenominator"]), 1)
        self.assertGreaterEqual(evidence["enumeration"]["leavesEvaluated"], 1)
        # transform is an integer square matrix of kernel dimension
        self.assertTrue(evidence["coordinateTransform"])

    def test_optimize_unsolvable_keeps_obstruction_and_no_optimum(self):
        payload = {
            "variables": ["D1", "D2"],
            "matrix": [["2", "0"], ["0", "4"]],
            "target": ["9007199254740993", "8"],
            "preferred": ["3", "1"],
            "weights": ["1", "1"],
        }
        status, body = self.post_optimize(payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["solvable"])
        self.assertIsNone(body["optimization"])
        self.assertEqual(body["obstruction"]["type"], "non_divisible")
        self.assertEqual(body["obstruction"]["remainder"], "1")

    def test_optimize_requires_preferences(self):
        payload = {
            "variables": ["x"],
            "matrix": [["1"]],
            "target": ["1"],
        }
        status, body = self.post_optimize(payload)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_optimize_rejects_missing_preferred_entry(self):
        payload = {
            "variables": ["x", "y"],
            "matrix": [["1", "0"], ["0", "1"]],
            "target": ["1", "2"],
            "preferred": ["1", ""],
            "weights": ["1", "2"],
        }
        status, body = self.post_optimize(payload)
        self.assertEqual(status, 400)
        self.assertIn("缺失", body["error"])

    def test_optimize_rejects_duplicate_preferred(self):
        payload = {
            "variables": ["x", "y"],
            "matrix": [["1", "0"], ["0", "1"]],
            "target": ["1", "2"],
            "preferred": ["7", "7"],
            "weights": ["1", "2"],
        }
        status, body = self.post_optimize(payload)
        self.assertEqual(status, 400)
        self.assertIn("重复", body["error"])

    def test_optimize_rejects_non_integer_preferred(self):
        payload = {
            "variables": ["x", "y"],
            "matrix": [["1", "0"], ["0", "1"]],
            "target": ["1", "2"],
            "preferred": ["1.5", "0"],
            "weights": ["1", "2"],
        }
        status, body = self.post_optimize(payload)
        self.assertEqual(status, 400)

    def test_optimize_rejects_non_positive_weight(self):
        base = {
            "variables": ["x", "y"],
            "matrix": [["1", "0"], ["0", "1"]],
            "target": ["1", "2"],
            "preferred": ["1", "0"],
        }
        for weights in (["1", "0"], ["1", "-2"]):
            status, body = self.post_optimize({**base, "weights": weights})
            self.assertEqual(status, 400, weights)
            self.assertIn("正整数", body["error"])

    def test_review_response_unchanged_without_preferences(self):
        payload = dict(self.OPTIMIZE_PAYLOAD)
        status, body = self.post_review(payload)
        self.assertEqual(status, 200)
        self.assertNotIn("optimization", body)
        # Plain review returns the particular solution (free coordinates 0);
        # it is an exact solution but not the weighted optimum.
        self.assertEqual(
            body["solution"], ["0", "18014398509481989", "-54043195528445955"]
        )
        self.assertTrue(all(c["satisfied"] for c in body["constraints"]))
        self.assertEqual(len(body["homogeneousBasis"]), 1)

    def test_optimize_rejects_length_mismatch(self):
        payload = {
            "variables": ["x", "y"],
            "matrix": [["1", "0"], ["0", "1"]],
            "target": ["1", "2"],
            "preferred": ["1", "2", "3"],
            "weights": ["1", "1", "1"],
        }
        status, body = self.post_optimize(payload)
        self.assertEqual(status, 400)



if __name__ == "__main__":
    unittest.main()
