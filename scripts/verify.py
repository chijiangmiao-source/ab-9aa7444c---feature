#!/usr/bin/env python3
"""One-shot acceptance routine for the shim-correction review stack.

Order of checks (mirrors the acceptance contract):
  1. Confirm the no-solution evidence for a non-divisible constraint via
     the review API (with a target beyond the IEEE-754 safe-integer range).
  2. Run the code test-suite and the build checks.
  3. HTTP smoke-test the health path and the review endpoint.
  4. Exercise the preferred-shim optimal-correction audit: exact weighted
     optimum, lexicographic tie adjudication, rejection of malformed
     preference settings, and unchanged behaviour without preferences.
The process exit code reports the overall result: 0 = pass, 1 = fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8080").rstrip("/")

# 2**53 + 1 exceeds the JavaScript/IEEE-754 safe-integer range; the whole
# stack must still carry it as exact integer text.
BIG_TARGET = str(2**53 + 1)  # 9007199254740993

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}", flush=True)
    if not condition:
        if detail:
            print(f"       {detail}", flush=True)
        failures.append(name)
    return condition


def http(method: str, url: str, payload=None) -> tuple[int, str]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return -1, str(exc)


def post_review(payload) -> tuple[int, dict | None]:
    status, text = http("POST", f"{APP_BASE_URL}/api/review", payload)
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, None


def wait_for_app(attempts: int = 30, delay: float = 1.0) -> bool:
    for _ in range(attempts):
        status, _ = http("GET", f"{APP_BASE_URL}/health")
        if status == 200:
            return True
        time.sleep(delay)
    return False


def step1_obstruction_evidence() -> None:
    print("\n== 步骤 1：不可整除约束的无解证据 ==", flush=True)
    status, body = post_review(
        {
            "variables": ["shim_north", "shim_south"],
            "matrix": [["2", "0"], ["0", "2"]],
            "target": [BIG_TARGET, "4"],
        }
    )
    check("不可整除约束复核返回 HTTP 200", status == 200, f"status={status}")
    check("判定为无整数解", bool(body) and body.get("solvable") is False,
          f"body={body!r}"[:400])
    obstruction = (body or {}).get("obstruction") or {}
    check("障碍类型为规范除尽障碍 non_divisible",
          obstruction.get("type") == "non_divisible", f"obstruction={obstruction!r}"[:400])
    check("主元精确为 2", obstruction.get("pivot") == "2")
    check("变换后目标以精确大整数文本呈现",
          obstruction.get("transformedTarget") == BIG_TARGET,
          f"got {obstruction.get('transformedTarget')!r}")
    check("余数精确为 1（主元不能整除变换后目标）",
          obstruction.get("remainder") == "1")
    u_terms = obstruction.get("uRowTerms") or []
    total = sum(int(term["product"]) for term in u_terms) if u_terms else None
    check("行变换各项乘积之和等于变换后目标",
          total is not None and str(total) == BIG_TARGET)


def run_subprocess(name: str, argv: list[str]) -> None:
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    check(name, proc.returncode == 0, output[-2000:] if proc.returncode != 0 else "")
    if proc.returncode == 0 and output:
        for line in output.splitlines()[-3:]:
            print(f"       {line}", flush=True)


def step2_tests_and_build_checks() -> None:
    print("\n== 步骤 2：代码测试与构建检查 ==", flush=True)
    run_subprocess(
        "单元测试（求解器与 HTTP 层）",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
    )
    run_subprocess(
        "语法构建检查 compileall",
        [sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests"],
    )
    run_subprocess(
        "模块导入检查",
        [sys.executable, "-c", "import app.diophantine, app.server; print('imports ok')"],
    )
    for rel in (
        "Dockerfile",
        "docker-compose.yml",
        "app/static/index.html",
        "app/static/app.js",
        "app/static/style.css",
    ):
        check(f"构建所需文件存在：{rel}",
              os.path.exists(os.path.join(ROOT, rel)))


def step3_http_smoke() -> None:
    print("\n== 步骤 3：健康路径与复核接口 HTTP 冒烟 ==", flush=True)
    status, text = http("GET", f"{APP_BASE_URL}/health")
    healthy = False
    try:
        healthy = json.loads(text).get("status") == "ok"
    except json.JSONDecodeError:
        pass
    check("健康路径 /health 返回 200 且 status=ok", status == 200 and healthy,
          f"status={status} body={text[:200]}")

    status, text = http("GET", f"{APP_BASE_URL}/")
    check("页面 / 返回 200 且包含复核界面", status == 200 and "复核" in text,
          f"status={status}")

    status, body = post_review(
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
    check("复核接口（可解大整数用例）返回 HTTP 200", status == 200, f"status={status}")
    check("判定为可解", bool(body) and body.get("solvable") is True,
          f"body={body!r}"[:400])
    check("精确整数校正量为 [2, 3, 1]",
          bool(body) and body.get("solution") == ["2", "3", "1"])
    constraints = (body or {}).get("constraints") or []
    check("每条约束的左侧和均精确等于目标值",
          bool(constraints) and all(c["satisfied"] for c in constraints))
    if constraints:
        first_products = [t["product"] for t in constraints[0]["terms"]]
        check("首条约束各项乘积精确（含超大整数）",
              first_products == [str(9007199254740993 * 2), "3", "0"],
              f"products={first_products!r}")

    status, body = post_review(
        {"variables": ["x"], "matrix": [[1.5]], "target": [1]}
    )
    check("浮点系数被拒绝（HTTP 400，绝不四舍五入）",
          status == 400 and bool(body) and body.get("ok") is False,
          f"status={status} body={body!r}"[:300])


def step4_preferred_audit() -> None:
    print("\n== 步骤 4：优选校正审计 ==", flush=True)

    # x + 2y = 3，首选 (0, 0)、权重 (1, 1)：可行解 x = 3 - 2y，
    # 代价 (3-2y)^2 + y^2 在 y = 1 处取最小值 2，最优校正量 (1, 1)。
    status, body = post_review(
        {
            "variables": ["x", "y"],
            "matrix": [["1", "2"]],
            "target": ["3"],
            "preferences": [
                {"variable": "x", "preferred": "0", "weight": "1"},
                {"variable": "y", "preferred": "0", "weight": "1"},
            ],
        }
    )
    opt = (body or {}).get("optimization") or {}
    check("带首选设置的复核返回 HTTP 200", status == 200, f"status={status}")
    check("审计给出精确最优校正量 (1, 1)",
          opt.get("correction") == ["1", "1"], f"optimization={opt!r}"[:400])
    check("逐项偏差为 (1, 1)", opt.get("adjustments") == ["1", "1"])
    check("逐项加权平方偏差为 (1, 1)", opt.get("perItemCost") == ["1", "1"])
    check("总成本精确为 2", opt.get("cost") == "2")
    evidence = opt.get("evidence") or {}
    check("最优界证据的最终界等于总成本", evidence.get("finalBound") == "2")
    levels = evidence.get("levels") or []
    check("最优界证据覆盖全部自由坐标层", len(levels) == 1,
          f"levels={levels!r}"[:300])
    if levels:
        level = levels[0]
        check("最优坐标落在证据枚举区间内",
              int(level["low"]) <= int(level["chosen"]) <= int(level["high"]))

    # x + y = 1，首选 (0, 0)、权重 (1, 1)：(0, 1) 与 (1, 0) 同值，
    # 按变量标识顺序的调整量字典序应裁决为调整量 (0, 1)。
    status, body = post_review(
        {
            "variables": ["x", "y"],
            "matrix": [["1", "1"]],
            "target": ["1"],
            "preferences": [
                {"variable": "x", "preferred": "0", "weight": "1"},
                {"variable": "y", "preferred": "0", "weight": "1"},
            ],
        }
    )
    opt = (body or {}).get("optimization") or {}
    check("同值候选按调整量字典序裁决（调整量 (0, 1)）",
          status == 200 and opt.get("adjustments") == ["0", "1"],
          f"status={status} optimization={opt!r}"[:400])

    # 权重决定偏差分配：x + y = 2，首选 (0, 0)，y 权重 100 -> (2, 0)。
    status, body = post_review(
        {
            "variables": ["x", "y"],
            "matrix": [["1", "1"]],
            "target": ["2"],
            "preferences": [
                {"variable": "x", "preferred": "0", "weight": "1"},
                {"variable": "y", "preferred": "0", "weight": "100"},
            ],
        }
    )
    opt = (body or {}).get("optimization") or {}
    check("权重引导偏差分配（最优校正量 (2, 0)，成本 4）",
          status == 200 and opt.get("correction") == ["2", "0"]
          and opt.get("cost") == "4",
          f"status={status} optimization={opt!r}"[:400])

    # 非法首选设置一律拒绝（HTTP 400）。
    base = {"variables": ["x", "y"], "matrix": [["1", "1"]], "target": ["2"]}
    bad_cases = {
        "首选值缺失": [{"variable": "x", "preferred": "0", "weight": "1"}],
        "变量重复": [
            {"variable": "x", "preferred": "0", "weight": "1"},
            {"variable": "x", "preferred": "1", "weight": "1"},
        ],
        "首选值非整数": [
            {"variable": "x", "preferred": "0.5", "weight": "1"},
            {"variable": "y", "preferred": "1", "weight": "1"},
        ],
        "权重非正（零）": [
            {"variable": "x", "preferred": "0", "weight": "0"},
            {"variable": "y", "preferred": "1", "weight": "1"},
        ],
        "权重非正（负）": [
            {"variable": "x", "preferred": "0", "weight": "-2"},
            {"variable": "y", "preferred": "1", "weight": "1"},
        ],
    }
    for name, preferences in bad_cases.items():
        payload = dict(base)
        payload["preferences"] = preferences
        status, body = post_review(payload)
        check(f"非法首选设置被拒绝（{name}，HTTP 400）",
              status == 400 and bool(body) and body.get("ok") is False
              and "optimization" not in (body or {}),
              f"status={status} body={body!r}"[:300])

    # 未填写首选设置：既有复核响应保持不变（无 optimization 键）。
    status, body = post_review(base)
    check("未填写首选设置时复核响应保持不变",
          status == 200 and bool(body) and "optimization" not in body,
          f"status={status}")

    # 方程无解：保留既有除尽障碍，不产生优选结论。
    status, body = post_review(
        {
            "variables": ["x", "y"],
            "matrix": [["2", "0"], ["0", "2"]],
            "target": ["3", "4"],
            "preferences": [
                {"variable": "x", "preferred": "0", "weight": "1"},
                {"variable": "y", "preferred": "1", "weight": "1"},
            ],
        }
    )
    check("无解时保留除尽障碍且不产生优选结论",
          status == 200 and bool(body) and body.get("solvable") is False
          and "obstruction" in body and "optimization" not in body,
          f"status={status} body={body!r}"[:300])


def main() -> int:
    print(f"验收目标：{APP_BASE_URL}", flush=True)
    if not wait_for_app():
        check("等待应用就绪", False, f"{APP_BASE_URL}/health 未在限定时间内就绪")
    else:
        print("应用已就绪。", flush=True)
        step1_obstruction_evidence()
        step2_tests_and_build_checks()
        step3_http_smoke()
        step4_preferred_audit()

    print("\n== 验收结论 ==", flush=True)
    if failures:
        print(f"失败 {len(failures)} 项：", flush=True)
        for name in failures:
            print(f"  - {name}", flush=True)
        return 1
    print("全部验收项通过。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
