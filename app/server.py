"""HTTP front-end for the shim-correction review service.

Serves the review page, a health endpoint and the exact Diophantine
review API.  Standard library only; every integer is handled as an
arbitrary-precision Python ``int`` and serialised as decimal text so
values beyond the IEEE-754 safe-integer range stay exact end to end.
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Computed products/sums may exceed the default 4300-digit str() limit;
# the service is explicitly arbitrary-precision, so lift it (input sizes
# are still capped below).
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

from app.diophantine import solve_diophantine
from app.lattice import optimize_corrections
from fractions import Fraction

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = (BASE_DIR / "static").resolve()

MAX_DIMENSION = 64
MAX_DIGITS = 4096
MAX_VARIABLES = 64
MAX_BODY_BYTES = 1 << 20

_INT_RE = re.compile(r"^[+-]?\d+$")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class RequestError(ValueError):
    """Client-side payload problem, answered with HTTP 400."""


def parse_integer(value, where: str) -> int:
    """Parse an exact integer; floats are rejected, never rounded."""
    if isinstance(value, bool):
        raise RequestError(f"{where}：不接受布尔值，必须为整数字符串")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise RequestError(
            f"{where}：检测到浮点数 {value!r}，为保持精确性已拒绝；请以整数字符串提交"
        )
    if isinstance(value, str):
        text = value.strip()
        if not _INT_RE.match(text):
            raise RequestError(f"{where}：“{text}” 不是十进制整数")
        if len(text.lstrip("+-")) > MAX_DIGITS:
            raise RequestError(f"{where}：整数位数超过上限 {MAX_DIGITS}")
        return int(text)
    raise RequestError(f"{where}：类型不支持，必须为整数字符串")


def parse_payload(data) -> tuple[list[str], list[list[int]], list[int]]:
    if not isinstance(data, dict):
        raise RequestError("请求体必须是 JSON 对象")

    variables = data.get("variables")
    matrix_raw = data.get("matrix")
    target_raw = data.get("target")

    if not isinstance(variables, list) or not variables:
        raise RequestError("variables 必须是非空数组")
    if len(variables) > MAX_VARIABLES:
        raise RequestError(f"变量个数超过上限 {MAX_VARIABLES}")
    seen = set()
    for k, name in enumerate(variables):
        if not isinstance(name, str) or not name.strip():
            raise RequestError(f"第 {k + 1} 个变量标识为空或不是字符串")
        name = name.strip()
        if len(name) > 40:
            raise RequestError(f"变量标识 “{name}” 过长")
        if name in seen:
            raise RequestError(f"变量标识 “{name}” 重复")
        seen.add(name)
    variables = [name.strip() for name in variables]

    if not isinstance(matrix_raw, list) or not matrix_raw:
        raise RequestError("matrix 必须是非空行数组")
    if len(matrix_raw) > MAX_DIMENSION:
        raise RequestError(f"约束条数超过上限 {MAX_DIMENSION}")
    matrix = []
    for i, row in enumerate(matrix_raw):
        if not isinstance(row, list):
            raise RequestError(f"矩阵第 {i + 1} 行不是数组")
        if len(row) != len(variables):
            raise RequestError(
                f"矩阵第 {i + 1} 行有 {len(row)} 个系数，与变量数 {len(variables)} 不一致"
            )
        matrix.append(
            [parse_integer(cell, f"矩阵第 {i + 1} 行第 {j + 1} 列") for j, cell in enumerate(row)]
        )

    if not isinstance(target_raw, list) or len(target_raw) != len(matrix):
        raise RequestError("target 必须是长度与约束条数一致的数组")
    target = [parse_integer(value, f"目标向量第 {i + 1} 项") for i, value in enumerate(target_raw)]

    return variables, matrix, target


def parse_preferences(
    data, variables: list[str]
) -> tuple[list[int], list[int]] | None:
    """Parse ``preferred`` / ``weights`` settings of an optimization audit.

    Returns ``None`` when no preference section is supplied at all (a plain
    review request, whose response must stay unchanged).  Present-but
    invalid settings (missing, duplicated, non-integer preferred values or
    non-positive/non-integer weights) raise :class:`RequestError` so the
    service rejects the audit and never returns a stale optimization.
    """
    if "preferred" not in data and "weights" not in data:
        return None

    n = len(variables)
    preferred_raw = data.get("preferred")
    weights_raw = data.get("weights")

    if not isinstance(preferred_raw, list) or len(preferred_raw) != n:
        raise RequestError(
            f"preferred 必须是长度与变量数 {n} 一致的数组（每个变量都须填写首选整数垫片数）"
        )
    if not isinstance(weights_raw, list) or len(weights_raw) != n:
        raise RequestError(
            f"weights 必须是长度与变量数 {n} 一致的数组（每个变量都须填写正整数权重）"
        )

    def is_blank(value) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    for j, value in enumerate(preferred_raw):
        if is_blank(value):
            raise RequestError(
                f"变量 “{variables[j]}” 的首选整数垫片数缺失：发起优选校正审计前须逐项填写"
            )
    preferred = [
        parse_integer(value, f"变量 “{variables[j]}” 的首选垫片数")
        for j, value in enumerate(preferred_raw)
    ]
    seen_preferred: set[int] = set()
    for j, value in enumerate(preferred):
        if value in seen_preferred:
            raise RequestError(
                f"变量 “{variables[j]}” 的首选垫片数 {value} 与另一项重复："
                "每项机械校正的首选整数垫片数必须互不相同"
            )
        seen_preferred.add(value)
    weights = []
    for j, value in enumerate(weights_raw):
        if is_blank(value):
            raise RequestError(f"变量 “{variables[j]}” 的权重缺失：权重必须为正整数")
        weight = parse_integer(value, f"变量 “{variables[j]}” 的权重")
        if weight <= 0:
            raise RequestError(
                f"变量 “{variables[j]}” 的权重为 {weight}：权重必须是正整数"
            )
        weights.append(weight)
    return preferred, weights


def _fraction_text(value: Fraction) -> str:
    """Exact rational as ``"numerator/denominator"`` decimal text."""
    return f"{value.numerator}/{value.denominator}"


def build_review_response(variables, matrix, target) -> dict:
    result = solve_diophantine(matrix, target)
    body = {
        "ok": True,
        "solvable": result.solvable,
        "variables": variables,
        "smith": {
            "rank": result.smith.rank,
            "diagonal": [str(d) for d in result.smith.diagonal],
            "transformedTarget": [str(v) for v in result.transformed_target],
        },
    }
    if result.solvable:
        solution = result.solution
        constraints = []
        for i, row in enumerate(matrix):
            terms = []
            total = 0
            for j, name in enumerate(variables):
                product = row[j] * solution[j]
                total += product
                terms.append(
                    {
                        "variable": name,
                        "coefficient": str(row[j]),
                        "correction": str(solution[j]),
                        "product": str(product),
                    }
                )
            constraints.append(
                {
                    "index": i,
                    "terms": terms,
                    "sum": str(total),
                    "target": str(target[i]),
                    "satisfied": total == target[i],
                }
            )
        body["solution"] = [str(value) for value in solution]
        body["constraints"] = constraints
        body["homogeneousBasis"] = [
            [str(value) for value in vector] for vector in result.homogeneous_basis
        ]
    else:
        obstruction = result.obstruction
        u_terms = [
            {
                "row": k,
                "coefficient": str(coefficient),
                "target": str(target[k]),
                "product": str(coefficient * target[k]),
            }
            for k, coefficient in enumerate(obstruction.u_row)
            if coefficient != 0
        ]
        body["obstruction"] = {
            "type": obstruction.kind,
            "row": obstruction.row,
            "pivot": str(obstruction.pivot),
            "transformedTarget": str(obstruction.transformed_target),
            "remainder": str(obstruction.remainder),
            "uRow": [str(coefficient) for coefficient in obstruction.u_row],
            "uRowTerms": u_terms,
        }
    return body


def build_optimize_response(variables, matrix, target, preferred, weights) -> dict:
    """Run the review plus the exact weighted closest-point audit."""
    body = build_review_response(variables, matrix, target)
    if not body["solvable"]:
        # No integer solution: keep the canonical divisibility obstruction
        # exactly as the plain review reports it; no optimization exists.
        body["optimization"] = None
        return body

    result = solve_diophantine(matrix, target)
    opt = optimize_corrections(
        particular=result.solution,
        basis=result.homogeneous_basis,
        preferred=preferred,
        weights=weights,
    )

    # The enumeration bound is independently re-checkable: reproduce the
    # Babai seed point from its reduced coordinates and confirm its exact
    # integer cost is the bound the enumeration started from.
    n = len(variables)
    k = len(opt.reduced_basis)
    babai_seed = list(result.solution)
    for ell in range(k):
        coord = opt.babai_coordinates[ell]
        if coord:
            for i in range(n):
                babai_seed[i] += coord * opt.reduced_basis[ell][i]
    if babai_seed != opt.babai_point:
        raise ArithmeticError("内部校验失败：Babai 初始界点未能复现")

    items = []
    for i, name in enumerate(variables):
        items.append(
            {
                "variable": name,
                "preferred": str(preferred[i]),
                "weight": str(weights[i]),
                "optimal": str(opt.optimal[i]),
                "deviation": str(opt.deviations[i]),
                "deviationSquared": str(opt.deviations[i] ** 2),
                "weightedTerm": str(opt.weighted_terms[i]),
            }
        )

    # Unimodular coordinate evidence: reduced column ell = original @ C[:,ell],
    # i.e. original_coordinates = C @ reduced_coordinates.
    body["optimization"] = {
        "items": items,
        "cost": str(opt.cost),
        "tieCount": opt.tie_count,
        "lexicographicRule": "总成本相同的候选间，取变量标识顺序下调整量（偏差）向量字典序最小者",
        "coordinates": [str(value) for value in opt.optimal_coordinates],
        "boundEvidence": {
            "method": (
                "LLL 精确有理规约后，对 Gram-Schmidt 正交层做精确 Babai 最近平面取点，"
                "得到格内可行点作为加权平方距离的可证明初始上界（非人为搜索半径）；"
                "随后 Fincke-Pohst 枚举在该界内逐层以整数平方根与有理数精确划定整数区间，"
                "遍历全部不超过该界的整数坐标"
            ),
            "delta": "3/4",
            "babaiReducedCoordinates": [str(value) for value in opt.babai_coordinates],
            "babaiPoint": [str(value) for value in opt.babai_point],
            "babaiCost": str(opt.babai_cost),
            "boundAttainedByOptimum": opt.cost == opt.babai_cost,
            "orthogonalResidual": _fraction_text(opt.orthogonal_residual),
            "commonDenominator": str(opt.common_denominator),
            "lllArithmetic": opt.lll_arithmetic,
            "lllRole": "LLL 仅为搜索预处理（列运算始终为精确整数，坐标变换幺模）；最优选定与全部界定比较均为精确整数/有理数",
            "enumerationIdentity": (
                "距离² = 目标对格张成空间的正交补常量 + Σ N_i·(Q·z_i − R_i)² / Q³；"
                "N_i、R_i、Q 均为整数，逐层区间由整数平方根精确划定"
            ),
            "reducedBasis": [
                [str(value) for value in column] for column in opt.reduced_basis
            ],
            "coordinateTransform": [
                [str(value) for value in column] for column in opt.transform
            ],
            "gram": [[str(value) for value in row] for row in opt.gram],
            "gramSchmidt": {
                "mu": [
                    [_fraction_text(value) for value in row[:i]]
                    for i, row in enumerate(opt.mu)
                ],
                "squaredNorms": [_fraction_text(value) for value in opt.gs_norm],
            },
            "enumeration": {
                "nodesVisited": opt.nodes_visited,
                "leavesEvaluated": opt.leaves_evaluated,
                "emptyIntervals": opt.empty_intervals,
                "boundComparison": "区间判定全程为整数/有理数精确比较，无任何浮点距离",
            },
        },
    }
    return body


class Handler(BaseHTTPRequestHandler):
    server_version = "ShimReview/1.0"

    def log_message(self, fmt, *args):  # keep container logs concise
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ---------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, path: Path) -> None:
        data = path.read_bytes()
        content_type = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/":
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not target.is_file() or STATIC_DIR not in target.parents:
            self._send_json(404, {"ok": False, "error": "资源不存在"})
            return
        self._send_static(target)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/api/review", "/api/optimize"):
            self._send_json(404, {"ok": False, "error": "未知路径"})
            return
        optimize = path == "/api/optimize"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "请求体缺失或超过大小上限"})
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"ok": False, "error": f"JSON 解析失败：{exc}"})
            return
        try:
            variables, matrix, target = parse_payload(data)
            preferences = parse_preferences(data, variables) if optimize else None
            if optimize and preferences is None:
                raise RequestError(
                    "优选校正审计必须逐项提供 preferred（首选整数垫片数）与 "
                    "weights（正整数权重）"
                )
        except RequestError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        try:
            if optimize:
                preferred, weights = preferences
                self._send_json(
                    200,
                    build_optimize_response(variables, matrix, target, preferred, weights),
                )
            else:
                self._send_json(200, build_review_response(variables, matrix, target))
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(500, {"ok": False, "error": f"服务器内部错误：{exc}"})


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"shim-review listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
