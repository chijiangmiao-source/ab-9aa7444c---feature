#!/usr/bin/env python3
"""One-shot acceptance routine for the shim-correction review stack.

Order of checks (mirrors the acceptance contract):
  1. Confirm the no-solution evidence for a non-divisible constraint via
     the review API (with a target beyond the IEEE-754 safe-integer range).
  2. Run the code test-suite and the build checks.
  3. HTTP smoke-test the health path and the review endpoint.
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


def post_optimize(payload) -> tuple[int, dict | None]:
    status, text = http("POST", f"{APP_BASE_URL}/api/optimize", payload)
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, None


def step4_optimization_audit() -> None:
    print("\n== 步骤 4：优选校正审计（精确加权最近格点）==", flush=True)

    # 欠定系统：2 条耦合约束、3 个整数校正量，含一个超大整数自由方向；
    # 首选 [1,2,0]、权重 [1,2,1] 下最优为 [2,3,1]，总成本恰为 4。
    payload = {
        "variables": ["K1", "K2", "K3"],
        "matrix": [
            ["9007199254740993", "1", "0"],
            ["1", "3", "1"],
        ],
        "target": ["18014398509481989", "12"],
        "preferred": ["1", "2", "0"],
        "weights": ["1", "2", "1"],
    }
    status, body = post_optimize(payload)
    check("优选审计返回 HTTP 200", status == 200, f"status={status}")
    check("判定为可解", bool(body) and body.get("solvable") is True)
    opt = (body or {}).get("optimization") or {}
    items = opt.get("items") or []
    check("逐项返回首选值/权重/最优值/偏差/加权项",
          len(items) == 3 and all(
              {"preferred", "weight", "optimal", "deviation",
               "deviationSquared", "weightedTerm"} <= set(item)
              for item in items
          ), f"items={items!r}"[:400])
    check("精确最优校正向量为 [2, 3, 1]",
          [i.get("optimal") for i in items] == ["2", "3", "1"],
          f"items={items!r}"[:400])
    check("逐项偏差精确为 [1, 1, 1]",
          [i.get("deviation") for i in items] == ["1", "1", "1"])
    check("加权项之和精确等于总成本 4（整数文本）",
          [i.get("weightedTerm") for i in items] == ["1", "2", "1"]
          and opt.get("cost") == "4",
          f"cost={opt.get('cost')!r}")
    check("最优解仍严格满足原耦合方程",
          all(c.get("satisfied") for c in (body or {}).get("constraints", [])))

    # 可复算证据：用特解与齐次解格坐标重建最优点。
    try:
        x0 = [int(v) for v in body["solution"]]
        basis = [[int(v) for v in row] for row in body["homogeneousBasis"]]
        coords = [int(v) for v in opt["coordinates"]]
        rebuilt = [
            x0[j] + sum(coords[s] * basis[s][j] for s in range(len(basis)))
            for j in range(len(x0))
        ]
        rebuilt_cost = sum(
            int(i["weight"]) * (int(i["optimal"]) - int(i["preferred"])) ** 2
            for i in items
        )
    except (KeyError, TypeError, ValueError):
        rebuilt, rebuilt_cost = None, None
    check("格坐标复算：特解 + Σ 坐标·齐次方向 = 最优向量",
          rebuilt == [2, 3, 1], f"rebuilt={rebuilt!r}")
    check("独立复算总成本与服务一致", rebuilt_cost == 4, f"cost={rebuilt_cost!r}")

    evidence = opt.get("boundEvidence") or {}
    check("返回 Babai 可行初始界点（非人为半径）",
          bool(evidence.get("babaiPoint"))
          and int(evidence.get("babaiCost", "-1")) >= int(opt.get("cost", "0")))
    gs = evidence.get("gramSchmidt") or {}
    norms = gs.get("squaredNorms") or []
    check("Gram-Schmidt 平方范数以精确分数文本返回",
          bool(norms) and all("/" in v for v in norms), f"norms={norms!r}")
    en = evidence.get("enumeration") or {}
    check("枚举计数可复算（至少完整评估 1 个叶子点）",
          int(en.get("leavesEvaluated", 0)) >= 1, f"enumeration={en!r}")

    # 并列：x+y=1，首选 (2,1)，权重 (1,3)：(0,1) 与 (1,0) 成本同为 4，
    # 按变量标识顺序的调整量字典序取 (-2,0) 即 (0,1)。
    tie_payload = {
        "variables": ["x", "y"],
        "matrix": [["2", "2"]],
        "target": ["2"],
        "preferred": ["2", "1"],
        "weights": ["1", "3"],
    }
    status, body = post_optimize(tie_payload)
    opt = ((body or {}).get("optimization") or {})
    check("等最小成本并列时 tieCount=2", status == 200 and opt.get("tieCount") == 2,
          f"status={status} opt={opt!r}"[:300])
    check("字典序稳定裁决为 (0,1)，调整量 (-2,0)",
          [i.get("optimal") for i in (opt.get("items") or [])] == ["0", "1"])

    # 无解时除尽障碍保留，优选结论为 null。
    bad = {
        "variables": ["shim_north", "shim_south"],
        "matrix": [["2", "0"], ["0", "2"]],
        "target": [BIG_TARGET, "4"],
        "preferred": ["3", "1"],
        "weights": ["1", "1"],
    }
    status, body = post_optimize(bad)
    check("无首选可行解时保留既有除尽障碍且 optimization=null",
          status == 200 and (body or {}).get("solvable") is False
          and (body or {}).get("optimization") is None
          and ((body or {}).get("obstruction") or {}).get("type") == "non_divisible")

    # 非法首选设置：缺失、重复、非整数、权重非正 —— 一律 400 拒绝。
    base = {
        "variables": ["x", "y"],
        "matrix": [["1", "0"], ["0", "1"]],
        "target": ["1", "2"],
    }
    invalid_cases = [
        ("首选值缺失", {"preferred": ["1", ""], "weights": ["1", "2"]}),
        ("首选值重复", {"preferred": ["7", "7"], "weights": ["1", "2"]}),
        ("首选值非整数", {"preferred": ["1.5", "0"], "weights": ["1", "2"]}),
        ("权重为零", {"preferred": ["1", "0"], "weights": ["1", "0"]}),
        ("权重为负", {"preferred": ["1", "0"], "weights": ["1", "-2"]}),
        ("完全未填首选", {}),
    ]
    for label, extra in invalid_cases:
        st, bd = post_optimize({**base, **extra})
        check(f"非法设置被拒绝（{label}）→ HTTP 400",
              st == 400 and bool(bd) and bd.get("ok") is False,
              f"status={st} body={bd!r}"[:200])

    # 既有 /api/review 响应不携带 optimization 字段（页面展示保持不变）。
    status, body = post_review({**payload})
    check("未发起优选时 /api/review 响应不含 optimization",
          status == 200 and "optimization" not in (body or {}))


def main() -> int:
    print(f"验收目标：{APP_BASE_URL}", flush=True)
    if not wait_for_app():
        check("等待应用就绪", False, f"{APP_BASE_URL}/health 未在限定时间内就绪")
    else:
        print("应用已就绪。", flush=True)
        step1_obstruction_evidence()
        step2_tests_and_build_checks()
        step3_http_smoke()
        step4_optimization_audit()

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
