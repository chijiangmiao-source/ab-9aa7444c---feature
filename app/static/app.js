"use strict";

/* 束流整形磁铁垫片校正复核 —— 前端逻辑。
 *
 * 精确性约定：所有整数在浏览器中始终以十进制字符串保存与展示，
 * 绝不经过 Number 转换，因此超过 2^53 的系数与目标不会丢失精度。
 *
 * 竞态约定：generation 为单调递增的世代号。发起复核/优选审计、编辑草稿、
 * 取消请求都会使其递增；只有世代号仍为当前值的响应才允许渲染，
 * 过期响应一律丢弃，绝不覆盖当前草稿的状态或结论。
 */

const $ = (id) => document.getElementById(id);

const draftFields = {
  variables: $("variables"),
  matrix: $("matrix"),
  target: $("target"),
  preferred: $("preferred"),
  weights: $("weights"),
};
const runButton = $("run");
const runOptimizeButton = $("run-optimize");
const cancelButton = $("cancel");
const statusLine = $("status");
const resultPanel = $("result-panel");
const resultBox = $("result");

let generation = 0; // 草稿/请求世代号
let inflight = null; // 在途请求的 AbortController

const INT_PATTERN = /^[+-]?\d+$/;

const EXAMPLE_SOLVABLE = {
  variables: "K1, K2, K3",
  matrix: "9007199254740993 1 0\n1 3 1\n0 2 4",
  target: "18014398509481989, 12, 10",
  preferred: "",
  weights: "",
};

const EXAMPLE_UNSOLVABLE = {
  variables: "D1, D2",
  matrix: "2 0\n0 4",
  target: "9007199254740993, 8",
  preferred: "",
  weights: "",
};

// 欠定系统（2 条耦合约束、3 个校正量），解空间为
// [0, B, -6B+3] + t·[1, -B, 3B]（B = 9007199254740993）；首选 [1, 2, 0]
// 与权重 [1, 2, 1] 下，精确加权最近格点为 [2, 3, 1]，总成本 4。
const EXAMPLE_OPTIMIZE = {
  variables: "K1, K2, K3",
  matrix: "9007199254740993 1 0\n1 3 1",
  target: "18014398509481989, 12",
  preferred: "1, 2, 0",
  weights: "1, 2, 1",
};

// ---------------------------------------------------------------- 草稿解析

function splitTokens(text) {
  return text.split(/[\s,，]+/).filter((token) => token.length > 0);
}

function parseIntegerToken(token, where) {
  if (!INT_PATTERN.test(token)) {
    throw new Error(
      `${where}：“${token}” 不是十进制整数。仅接受整数字符串，绝不进行浮点换算。`
    );
  }
  let text = token.replace(/^\+/, "");
  const negative = text.startsWith("-");
  let digits = negative ? text.slice(1) : text;
  digits = digits.replace(/^0+(?=\d)/, "");
  return (negative ? "-" : "") + digits;
}

function readDraft(mode) {
  const variables = splitTokens(draftFields.variables.value);
  if (variables.length === 0) {
    throw new Error("请填写变量标识。");
  }
  if (new Set(variables).size !== variables.length) {
    throw new Error("变量标识存在重复。");
  }
  const rows = draftFields.matrix.value
    .split(/\n+/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (rows.length === 0) {
    throw new Error("请填写约束矩阵。");
  }
  const matrix = rows.map((line, i) => {
    const coeffs = splitTokens(line).map((token) =>
      parseIntegerToken(token, `矩阵第 ${i + 1} 行`)
    );
    if (coeffs.length !== variables.length) {
      throw new Error(
        `矩阵第 ${i + 1} 行有 ${coeffs.length} 个系数，与变量数 ${variables.length} 不一致。`
      );
    }
    return coeffs;
  });
  const target = splitTokens(draftFields.target.value).map((token) =>
    parseIntegerToken(token, "目标向量")
  );
  if (target.length !== matrix.length) {
    throw new Error(
      `目标向量长度 ${target.length} 与约束条数 ${matrix.length} 不一致。`
    );
  }
  const payload = { variables, matrix, target };
  if (mode === "optimize") {
    if (!draftFields.preferred.value.trim()) {
      throw new Error("发起优选校正审计前，须逐项填写每个机械校正项的首选整数垫片数。");
    }
    const preferred = splitTokens(draftFields.preferred.value).map((token, j) =>
      parseIntegerToken(token, `变量 ${variables[j] || j + 1} 的首选垫片数`)
    );
    if (preferred.length !== variables.length) {
      throw new Error(
        `首选垫片数有 ${preferred.length} 项，与变量数 ${variables.length} 不一致：须逐项填写且不得留空。`
      );
    }
    if (new Set(preferred).size !== preferred.length) {
      throw new Error("首选整数垫片数存在重复：每项机械校正的首选值必须互不相同。");
    }
    if (!draftFields.weights.value.trim()) {
      throw new Error("发起优选校正审计前，须逐项填写正整数权重。");
    }
    const weights = splitTokens(draftFields.weights.value).map((token, j) =>
      parseIntegerToken(token, `变量 ${variables[j] || j + 1} 的权重`)
    );
    if (weights.length !== variables.length) {
      throw new Error(
        `权重有 ${weights.length} 项，与变量数 ${variables.length} 不一致：须逐项填写且不得留空。`
      );
    }
    weights.forEach((token, j) => {
      if (token.startsWith("-") || token === "0") {
        throw new Error(`变量 ${variables[j]} 的权重为 ${token}：权重必须是正整数。`);
      }
    });
    payload.preferred = preferred;
    payload.weights = weights;
  }
  return payload;
}

// ---------------------------------------------------------------- 状态与竞态

function setStatus(message, kind) {
  statusLine.textContent = message;
  statusLine.dataset.kind = kind || "";
}

function markResultsStale() {
  if (!resultPanel.hidden && resultPanel.dataset.state === "fresh") {
    resultPanel.dataset.state = "stale";
    resultPanel.classList.add("stale");
  }
}

function invalidateDraft() {
  generation += 1; // 使任何在途响应立即过期
  if (inflight) {
    inflight.abort();
    inflight = null;
  }
  cancelButton.disabled = true;
  markResultsStale();
  setStatus("草稿已修改：先前计算即使返回也将被丢弃，不会覆盖当前草稿。", "warn");
}

Object.values(draftFields).forEach((field) =>
  field.addEventListener("input", invalidateDraft)
);

async function runRequest(mode) {
  let payload;
  try {
    payload = readDraft(mode);
  } catch (error) {
    setStatus(error.message, "error");
    return;
  }
  const myGeneration = ++generation;
  if (inflight) {
    inflight.abort();
  }
  const controller = new AbortController();
  inflight = controller;
  cancelButton.disabled = false;
  const endpoint = mode === "optimize" ? "/api/optimize" : "/api/review";
  const busyText = mode === "optimize" ? "优选校正审计计算中……" : "复核计算中……";
  setStatus(busyText, "busy");
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (myGeneration !== generation) {
      return; // 草稿已变更或已取消：丢弃过期响应
    }
    if (!response.ok || !data || data.ok !== true) {
      const message = data && data.error ? data.error : `HTTP ${response.status}`;
      const prefix = mode === "optimize" ? "优选校正审计被拒绝" : "复核失败";
      setStatus(`${prefix}：${message}`, "error");
      // 被拒绝的审计不得留下旧优选结论：仅移除旧结果中的优选区块，
      // 既有整数复核内容与其过期标记一律保持原样。
      if (mode === "optimize") {
        const oldOptimization = resultBox.querySelector(".optimization");
        if (oldOptimization) {
          oldOptimization.remove();
        }
      }
      return;
    }
    renderResult(data);
    resultPanel.hidden = false;
    resultPanel.dataset.state = "fresh";
    resultPanel.classList.remove("stale");
    setStatus(mode === "optimize" ? "优选校正审计完成。" : "复核完成。", "ok");
  } catch (error) {
    if (error && error.name === "AbortError") {
      return; // 已被取消或被更新的请求取代
    }
    if (myGeneration !== generation) {
      return;
    }
    setStatus(`请求失败：${error.message || error}`, "error");
  } finally {
    if (myGeneration === generation) {
      inflight = null;
      cancelButton.disabled = true;
    }
  }
}

function cancelReview() {
  generation += 1;
  if (inflight) {
    inflight.abort();
    inflight = null;
  }
  cancelButton.disabled = true;
  setStatus("已取消：先前计算若返回将被丢弃，不会覆盖当前草稿的状态或结论。", "warn");
}

runButton.addEventListener("click", () => runRequest("review"));
runOptimizeButton.addEventListener("click", () => runRequest("optimize"));
cancelButton.addEventListener("click", cancelReview);

function loadExample(example) {
  draftFields.variables.value = example.variables;
  draftFields.matrix.value = example.matrix;
  draftFields.target.value = example.target;
  draftFields.preferred.value = example.preferred;
  draftFields.weights.value = example.weights;
  invalidateDraft();
  setStatus("示例已载入，可发起复核或优选校正审计。", "");
}

$("load-solvable").addEventListener("click", () => loadExample(EXAMPLE_SOLVABLE));
$("load-unsolvable").addEventListener("click", () => loadExample(EXAMPLE_UNSOLVABLE));
$("load-optimize").addEventListener("click", () => loadExample(EXAMPLE_OPTIMIZE));

// ---------------------------------------------------------------- 结果渲染

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text; // 精确整数文本，绝不经过 Number
  }
  return node;
}

function renderResult(data) {
  resultBox.replaceChildren();
  if (data.solvable) {
    renderSolution(data);
  } else {
    renderObstruction(data);
  }
  resultBox.appendChild(renderSmithSummary(data.smith));
}

function renderSolution(data) {
  resultBox.appendChild(
    el("p", "verdict ok", "结论：可解 —— 以下为精确整数校正量（最小垫片单位的整数倍）。")
  );
  const list = el("ul", "solution-list");
  data.variables.forEach((name, i) => {
    list.appendChild(el("li", "num", `${name} = ${data.solution[i]}`));
  });
  resultBox.appendChild(list);

  if (data.optimization) {
    renderOptimization(data);
  }

  resultBox.appendChild(
    el("h3", null, "约束复核（点选任一约束，查看各项乘积、左侧和及其目标值）")
  );
  const listBox = el("div", "constraint-list");
  const detail = el("div", "constraint-detail");
  data.constraints.forEach((constraint, idx) => {
    const button = el(
      "button",
      "constraint-item num",
      `约束 ${idx + 1}：左侧和 ${constraint.sum} ＝ 目标 ${constraint.target} ${
        constraint.satisfied ? "✓" : "✗"
      }`
    );
    button.type = "button";
    button.addEventListener("click", () => {
      listBox
        .querySelectorAll(".constraint-item")
        .forEach((node) => node.classList.remove("active"));
      button.classList.add("active");
      renderConstraintDetail(detail, constraint);
    });
    listBox.appendChild(button);
  });
  resultBox.appendChild(listBox);
  resultBox.appendChild(detail);
  const first = listBox.querySelector(".constraint-item");
  if (first) {
    first.click();
  }

  if (data.homogeneousBasis && data.homogeneousBasis.length > 0) {
    const details = el("details", "homogeneous");
    details.appendChild(
      el(
        "summary",
        null,
        `自由校正方向（${data.homogeneousBasis.length} 个）：叠加其任意整数倍不改变任何左侧和`
      )
    );
    const table = el("table", "num");
    const head = el("tr");
    head.appendChild(el("th", null, "方向"));
    data.variables.forEach((name) => head.appendChild(el("th", null, name)));
    table.appendChild(head);
    data.homogeneousBasis.forEach((vector, k) => {
      const row = el("tr");
      row.appendChild(el("td", null, `方向 ${k + 1}`));
      vector.forEach((value) => row.appendChild(el("td", null, value)));
      table.appendChild(row);
    });
    details.appendChild(table);
    resultBox.appendChild(details);
  }
}

function signed(value) {
  return value.startsWith("-") ? value : `+${value}`;
}

function renderOptimization(data) {
  const opt = data.optimization;
  const section = el("section", "optimization");
  section.appendChild(
    el(
      "h3",
      null,
      "优选校正审计：在原耦合方程严格成立的全部整数解中，取加权平方偏差最小的校正向量"
    )
  );

  const table = el("table", "num opt-table");
  const head = el("tr");
  ["变量", "首选垫片数", "权重", "最优校正量", "逐项偏差", "偏差平方", "加权项（权重×偏差²）"].forEach(
    (title) => head.appendChild(el("th", null, title))
  );
  table.appendChild(head);
  opt.items.forEach((item) => {
    const row = el("tr");
    row.appendChild(el("td", null, item.variable));
    row.appendChild(el("td", null, item.preferred));
    row.appendChild(el("td", null, item.weight));
    row.appendChild(el("td", "opt-value", item.optimal));
    row.appendChild(el("td", null, signed(item.deviation)));
    row.appendChild(el("td", null, item.deviationSquared));
    row.appendChild(el("td", null, item.weightedTerm));
    table.appendChild(row);
  });
  section.appendChild(table);

  section.appendChild(
    el(
      "p",
      "verdict ok num",
      `加权平方偏差总成本 = ${opt.cost}（上表加权项之和，精确整数）`
    )
  );
  const tieText =
    opt.tieCount > 1
      ? `最小值共有 ${opt.tieCount} 个候选；${opt.lexicographicRule}。`
      : "最小值唯一，无需字典序裁决。";
  section.appendChild(el("p", "num tie-note", tieText));

  const basisCount = data.homogeneousBasis ? data.homogeneousBasis.length : 0;
  if (basisCount > 0 && opt.coordinates.length === basisCount) {
    const parts = opt.coordinates
      .map((z, j) => `(${z})·方向${j + 1}`)
      .join(" + ");
    section.appendChild(
      el(
        "p",
        "num explanation",
        `格坐标复算：最优校正 = 特解 [${data.solution.join(", ")}] + ${parts}`
      )
    );
  }

  section.appendChild(renderBoundEvidence(opt.boundEvidence));
  resultBox.appendChild(section);
}

function renderBoundEvidence(evidence) {
  const details = el("details", "bound-evidence");
  details.appendChild(
    el("summary", null, "最优界证据（可复算：初始上界、规约基、Gram-Schmidt、枚举计数）")
  );
  details.appendChild(el("p", "explanation", evidence.method));

  const facts = el("table", "num facts");
  [
    ["LLL 参数 δ", evidence.delta],
    ["公分母 Q（整数枚举）", evidence.commonDenominator],
    ["目标对格空间的正交补常量", evidence.orthogonalResidual],
    ["Babai 初始界点（格内可行点）", `[${evidence.babaiPoint.join(", ")}]`],
    ["Babai 界点成本（枚举初始上界）", evidence.babaiCost],
    ["最优点恰好达到该初始界", evidence.boundAttainedByOptimum ? "是" : "否（枚举中进一步收紧）"],
    ["枚举访问节点数", String(evidence.enumeration.nodesVisited)],
    ["完整评估的叶子点数", String(evidence.enumeration.leavesEvaluated)],
    ["空区间剪枝次数", String(evidence.enumeration.emptyIntervals)],
    ["比较方式", evidence.enumeration.boundComparison],
  ].forEach(([key, value]) => {
    const row = el("tr");
    row.appendChild(el("th", null, key));
    row.appendChild(el("td", null, value));
    facts.appendChild(row);
  });
  details.appendChild(facts);

  details.appendChild(
    el("p", "num", `Babai 规约坐标 ζ = [${evidence.babaiReducedCoordinates.join(", ")}]`)
  );

  const renderMatrix = (title, rows) => {
    details.appendChild(el("h4", null, title));
    const table = el("table", "num matrix-evidence");
    rows.forEach((r) => {
      const tr = el("tr");
      r.forEach((value) => tr.appendChild(el("td", null, value)));
      table.appendChild(tr);
    });
    details.appendChild(table);
  };

  if (evidence.reducedBasis && evidence.reducedBasis.length > 0) {
    // Store bases column-major from the server; transpose for display.
    const k = evidence.reducedBasis.length;
    const n = evidence.reducedBasis[0].length;
    const rows = [];
    for (let i = 0; i < n; i += 1) {
      rows.push(evidence.reducedBasis.map((column) => column[i]));
    }
    renderMatrix(`LLL 规约基（列向量，共 ${k} 个；按变量坐标逐行展示）`, rows);
    const cRows = [];
    for (let s = 0; s < k; s += 1) {
      cRows.push(evidence.coordinateTransform.map((column) => column[s]));
    }
    renderMatrix(
      "幺模坐标变换 C（规约列 ℓ = 原始基 · C[:,ℓ]；原始坐标 = C·ζ）",
      cRows
    );
    renderMatrix("加权 Gram 矩阵 G = B_redᵀ·diag(权重)·B_red", evidence.gram);
    const gs = evidence.gramSchmidt;
    const muRows = gs.mu.map((row, i) => {
      const full = [];
      for (let j = 0; j < i; j += 1) {
        full.push(row[j] || "0");
      }
      full.push("1");
      return full;
    });
    renderMatrix("Gram-Schmidt 系数 μ（下三角，对角为 1；精确分数）", muRows);
    renderMatrix(
      "Gram-Schmidt 正交向量加权平方范数 ‖b*ᵢ‖²_W（精确分数）",
      [gs.squaredNorms]
    );
  } else {
    details.appendChild(el("p", "num", "齐次解空间为零维：解唯一，无需格搜索，初始界即唯一成本。"));
  }
  return details;
}

function renderConstraintDetail(parent, constraint) {
  parent.replaceChildren();
  const table = el("table", "num");
  const head = el("tr");
  ["变量", "系数", "校正量", "乘积（系数 × 校正量）"].forEach((title) =>
    head.appendChild(el("th", null, title))
  );
  table.appendChild(head);
  constraint.terms.forEach((term) => {
    const row = el("tr");
    row.appendChild(el("td", null, term.variable));
    row.appendChild(el("td", null, term.coefficient));
    row.appendChild(el("td", null, term.correction));
    row.appendChild(el("td", null, term.product));
    table.appendChild(row);
  });
  parent.appendChild(table);
  parent.appendChild(el("p", "num", `左侧和 = ${constraint.sum}`));
  parent.appendChild(el("p", "num", `目标值 = ${constraint.target}`));
  parent.appendChild(
    el(
      "p",
      constraint.satisfied ? "verdict ok" : "verdict bad",
      constraint.satisfied ? "左侧和与目标值精确相等 ✓" : "左侧和与目标值不相等 ✗"
    )
  );
}

function renderObstruction(data) {
  const ob = data.obstruction;
  resultBox.appendChild(
    el("p", "verdict bad", "结论：无整数解 —— 存在由可逆整数行变换导出的规范除尽障碍。")
  );
  if (data.optimization === null) {
    resultBox.appendChild(
      el(
        "p",
        "explanation warn-note",
        "已随附首选设置：原方程无整数解，故不产生优选结论；以下既有除尽障碍保持不变。"
      )
    );
  }
  const explanation =
    ob.type === "non_divisible"
      ? `对约束系统施加可逆整数行变换（Smith 正规形 U·A·V = D）后，第 ${
          ob.row + 1
        } 行的主元为 ${ob.pivot}，而变换后目标为 ${
          ob.transformedTarget
        }，余数 ${ob.remainder} ≠ 0：主元不能整除变换后目标，因此不存在整数校正量。`
      : `对约束系统施加可逆整数行变换（Smith 正规形 U·A·V = D）后，第 ${
          ob.row + 1
        } 行为零行（主元 0），但变换后目标为 ${
          ob.transformedTarget
        } ≠ 0：等式 0 = ${ob.transformedTarget} 不可能成立。`;
  resultBox.appendChild(el("p", "explanation", explanation));

  const facts = el("table", "num facts");
  [
    ["障碍类型", ob.type === "non_divisible" ? "主元除尽失败" : "零行目标非零"],
    ["变换后行号", `第 ${ob.row + 1} 行`],
    ["主元", ob.pivot],
    ["变换后目标", ob.transformedTarget],
    ["余数（变换后目标 mod 主元）", ob.remainder],
  ].forEach(([key, value]) => {
    const row = el("tr");
    row.appendChild(el("th", null, key));
    row.appendChild(el("td", null, value));
    facts.appendChild(row);
  });
  resultBox.appendChild(facts);

  resultBox.appendChild(
    el(
      "h3",
      null,
      `行变换来源：变换后目标 = 第 ${ob.row + 1} 行变换行 U 与原始目标向量的乘积和`
    )
  );
  const table = el("table", "num");
  const head = el("tr");
  ["原约束行", "行变换系数 U", "原目标值", "乘积（U × 原目标）"].forEach((title) =>
    head.appendChild(el("th", null, title))
  );
  table.appendChild(head);
  ob.uRowTerms.forEach((term) => {
    const row = el("tr");
    row.appendChild(el("td", null, `约束 ${term.row + 1}`));
    row.appendChild(el("td", null, term.coefficient));
    row.appendChild(el("td", null, term.target));
    row.appendChild(el("td", null, term.product));
    table.appendChild(row);
  });
  resultBox.appendChild(table);
  resultBox.appendChild(
    el("p", "num", `各项乘积之和 = ${ob.transformedTarget}（即变换后目标）`)
  );
}

function renderSmithSummary(smith) {
  const details = el("details", "smith");
  details.appendChild(el("summary", null, "Smith 正规形摘要（可逆整数行变换导出）"));
  details.appendChild(el("p", "num", `秩 = ${smith.rank}`));
  details.appendChild(
    el("p", "num", `主元序列（d₁ | d₂ | …）= [${smith.diagonal.join(", ")}]`)
  );
  details.appendChild(
    el("p", "num", `变换后目标向量 U·b = [${smith.transformedTarget.join(", ")}]`)
  );
  return details;
}

// ---------------------------------------------------------------- 初始化

loadExample(EXAMPLE_SOLVABLE);
setStatus("已载入可解示例，可直接发起复核；填写首选值与权重后可发起优选校正审计。", "");
