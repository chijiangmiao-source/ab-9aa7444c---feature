"use strict";

/* 束流整形磁铁垫片校正复核 —— 前端逻辑。
 *
 * 精确性约定：所有整数在浏览器中始终以十进制字符串保存与展示，
 * 绝不经过 Number 转换，因此超过 2^53 的系数与目标不会丢失精度。
 *
 * 竞态约定：generation 为方程草稿世代号，auditGeneration 为首选设置
 * 世代号。编辑方程草稿使两者相关的在途响应全部过期；编辑首选设置
 * 仅使优选审计响应过期（复核结论不依赖首选设置）。发起复核、发起
 * 审计、取消都会推进相应世代号；只有世代号仍为当前值的响应才允许
 * 渲染，过期响应一律丢弃，绝不覆盖当前草稿的状态或结论。
 */

const $ = (id) => document.getElementById(id);

const draftFields = {
  variables: $("variables"),
  matrix: $("matrix"),
  target: $("target"),
};
const runButton = $("run");
const cancelButton = $("cancel");
const statusLine = $("status");
const resultPanel = $("result-panel");
const resultBox = $("result");
const auditPanel = $("audit-panel");
const prefRows = $("pref-rows");
const auditButton = $("audit");
const auditResult = $("audit-result");

let generation = 0; // 方程草稿世代号
let auditGeneration = 0; // 首选设置世代号
let reviewInFlight = null; // 在途复核的 AbortController
let auditInFlight = null; // 在途审计的 AbortController
let latestReview = null; // 最近一次新鲜复核响应（审计以此构造请求）

const INT_PATTERN = /^[+-]?\d+$/;

const EXAMPLE_SOLVABLE = {
  variables: "K1, K2, K3",
  matrix: "9007199254740993 1 0\n1 3 1\n0 2 4",
  target: "18014398509481989, 12, 10",
};

const EXAMPLE_UNSOLVABLE = {
  variables: "D1, D2",
  matrix: "2 0\n0 4",
  target: "9007199254740993, 8",
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

function readDraft() {
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
  return { variables, matrix, target };
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
  markAuditStale();
}

function markAuditStale() {
  if (!auditPanel.hidden && auditPanel.dataset.state === "fresh") {
    auditPanel.dataset.state = "stale";
    auditPanel.classList.add("stale");
  }
}

function abortInflight() {
  if (reviewInFlight) {
    reviewInFlight.abort();
    reviewInFlight = null;
    cancelButton.disabled = true;
  }
  if (auditInFlight) {
    auditInFlight.abort();
    auditInFlight = null;
  }
}

function invalidateDraft() {
  generation += 1; // 使任何在途复核/审计响应立即过期
  auditGeneration += 1;
  abortInflight();
  latestReview = null;
  latestPayload = null;
  auditPanel.hidden = true;
  markResultsStale();
  setStatus("草稿已修改：先前计算即使返回也将被丢弃，不会覆盖当前草稿。", "warn");
}

function invalidatePreferences() {
  auditGeneration += 1; // 仅使在途优选审计响应过期
  if (auditInFlight) {
    auditInFlight.abort();
    auditInFlight = null;
  }
  auditResult.replaceChildren(); // 设置已变：旧优选结论不再可信，立即清除
  markAuditStale();
}

function clearAuditOnReject(message) {
  auditResult.replaceChildren(); // 拒绝时清除旧优选结论
  auditPanel.dataset.state = "stale";
  auditPanel.classList.add("stale");
  setStatus(message, "error");
}

Object.values(draftFields).forEach((field) =>
  field.addEventListener("input", invalidateDraft)
);

async function runReview() {
  let payload;
  try {
    payload = readDraft();
  } catch (error) {
    setStatus(error.message, "error");
    return;
  }
  const myGeneration = ++generation;
  auditGeneration += 1; // 新复核使任何在途审计过期（方程与首选面板都将重建）
  if (reviewInFlight) {
    reviewInFlight.abort();
  }
  if (auditInFlight) {
    auditInFlight.abort();
    auditInFlight = null;
  }
  const controller = new AbortController();
  reviewInFlight = controller;
  cancelButton.disabled = false;
  setStatus("复核计算中……", "busy");
  try {
    const response = await fetch("/api/review", {
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
      setStatus(`复核失败：${message}`, "error");
      return;
    }
    latestReview = data;
    latestPayload = payload;
    renderResult(data);
    resultPanel.hidden = false;
    resultPanel.dataset.state = "fresh";
    resultPanel.classList.remove("stale");
    prepareAuditPanel(data);
    setStatus("复核完成。", "ok");
  } catch (error) {
    if (error && error.name === "AbortError") {
      return; // 已被取消或被更新的请求取代
    }
    if (myGeneration !== generation) {
      return;
    }
    setStatus(`复核请求失败：${error.message || error}`, "error");
  } finally {
    if (myGeneration === generation) {
      reviewInFlight = null;
      cancelButton.disabled = true;
    }
  }
}

function cancelReview() {
  generation += 1;
  if (reviewInFlight) {
    reviewInFlight.abort();
    reviewInFlight = null;
  }
  cancelButton.disabled = true;
  setStatus("已取消：先前计算若返回将被丢弃，不会覆盖当前草稿的状态或结论。", "warn");
}

runButton.addEventListener("click", runReview);
cancelButton.addEventListener("click", cancelReview);

// ---------------------------------------------------------------- 优选审计

let latestPayload = null; // 最近一次新鲜复核的方程草稿（精确文本）

function prepareAuditPanel(data) {
  auditResult.replaceChildren();
  auditPanel.hidden = !data.solvable;
  if (!data.solvable) {
    return;
  }
  // 重新复核（方程未变）时保留工程师已填的首选值与权重
  const previous = new Map();
  prefRows.querySelectorAll('input[data-field]').forEach((node) => {
    previous.set(`${node.dataset.variable}:${node.dataset.field}`, node.value);
  });
  auditPanel.dataset.state = "fresh";
  auditPanel.classList.remove("stale");
  prefRows.replaceChildren();
  const table = el("table", "num pref-table");
  const head = el("tr");
  ["变量", "首选整数垫片数", "正整数权重"].forEach((title) =>
    head.appendChild(el("th", null, title))
  );
  table.appendChild(head);
  data.variables.forEach((name, i) => {
    const row = el("tr");
    row.appendChild(el("td", null, name));
    const preferredCell = el("td");
    const preferredInput = document.createElement("input");
    preferredInput.type = "text";
    preferredInput.spellcheck = false;
    preferredInput.autocomplete = "off";
    preferredInput.className = "pref-input num";
    preferredInput.dataset.field = "preferred";
    // 默认首选当前复核给出的精确整数解；工程师可逐项改写
    preferredInput.value = data.solution[i];
    preferredInput.dataset.variable = name;
    if (previous.has(`${name}:preferred`)) {
      preferredInput.value = previous.get(`${name}:preferred`);
    }
    preferredInput.addEventListener("input", invalidatePreferences);
    preferredCell.appendChild(preferredInput);
    row.appendChild(preferredCell);
    const weightCell = el("td");
    const weightInput = document.createElement("input");
    weightInput.type = "text";
    weightInput.spellcheck = false;
    weightInput.autocomplete = "off";
    weightInput.className = "pref-input num";
    weightInput.dataset.field = "weight";
    weightInput.value = "1";
    weightInput.dataset.variable = name;
    if (previous.has(`${name}:weight`)) {
      weightInput.value = previous.get(`${name}:weight`);
    }
    weightInput.addEventListener("input", invalidatePreferences);
    weightCell.appendChild(weightInput);
    row.appendChild(weightCell);
    table.appendChild(row);
  });
  prefRows.appendChild(table);
}

function readPreferences() {
  if (!latestReview || !latestReview.solvable) {
    throw new Error("请先发起复核并得到可解结论。");
  }
  const preferredNodes = prefRows.querySelectorAll('input[data-field="preferred"]');
  const weightNodes = prefRows.querySelectorAll('input[data-field="weight"]');
  const preferences = [];
  for (const name of latestReview.variables) {
    const prefNode = [...preferredNodes].find((node) => node.dataset.variable === name);
    const weightNode = [...weightNodes].find((node) => node.dataset.variable === name);
    const prefRaw = ((prefNode && prefNode.value) || "").trim();
    if (!prefRaw) {
      throw new Error(`变量 ${name} 的首选值缺失：已拒绝本次优选审计。`);
    }
    const preferredText = parseIntegerToken(prefRaw, `变量 ${name} 的首选值`);
    const weightRaw = ((weightNode && weightNode.value) || "").trim();
    if (!weightRaw) {
      throw new Error(`变量 ${name} 的权重缺失：已拒绝本次优选审计。`);
    }
    const weightText = parseIntegerToken(weightRaw, `变量 ${name} 的权重`);
    if (weightText === "0" || weightText.startsWith("-")) {
      throw new Error(`变量 ${name} 的权重必须为正整数：已拒绝本次优选审计。`);
    }
    preferences.push({ variable: name, preferred: preferredText, weight: weightText });
  }
  return preferences;
}

async function runAudit() {
  let preferences;
  try {
    preferences = readPreferences();
  } catch (error) {
    clearAuditOnReject(error.message); // 本地校验拒绝：清除旧优选结论
    return;
  }
  const myGeneration = ++auditGeneration;
  if (auditInFlight) {
    auditInFlight.abort();
  }
  const controller = new AbortController();
  auditInFlight = controller;
  auditButton.disabled = true;
  setStatus("优选校正审计计算中（精确最近格点搜索）……", "busy");
  try {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...latestPayload, preferences }),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (myGeneration !== auditGeneration) {
      return; // 首选设置/方程草稿已变更：丢弃过期审计
    }
    if (!response.ok || !data || data.ok !== true) {
      const message = data && data.error ? data.error : `HTTP ${response.status}`;
      clearAuditOnReject(`优选审计被拒绝：${message}`); // 服务端拒绝：清除旧优选结论
      return;
    }
    renderAudit(data);
    auditPanel.dataset.state = "fresh";
    auditPanel.classList.remove("stale");
    setStatus("优选校正审计完成：已在全部整数解中精确取得加权偏差最小的校正向量。", "ok");
  } catch (error) {
    if (error && error.name === "AbortError") {
      return;
    }
    if (myGeneration !== auditGeneration) {
      return;
    }
    setStatus(`优选审计请求失败：${error.message || error}`, "error");
  } finally {
    if (myGeneration === auditGeneration) {
      auditInFlight = null;
      auditButton.disabled = false;
    }
  }
}

auditButton.addEventListener("click", runAudit);

// ---------------------------------------------------------------- 审计渲染

function renderAudit(data) {
  auditResult.replaceChildren();
  const opt = data.optimization;
  if (!opt) {
    auditResult.appendChild(
      el("p", "verdict bad", "审计响应缺少优选结论，请重新发起。")
    );
    return;
  }
  auditResult.appendChild(
    el("h3", null, "最优校正向量（精确整数垫片数）")
  );
  const table = el("table", "num");
  const head = el("tr");
  ["变量", "最优校正量", "首选值", "调整量（最优 − 首选）", "权重", "逐项加权平方偏差"]
    .forEach((title) => head.appendChild(el("th", null, title)));
  table.appendChild(head);
  opt.preferred.forEach((entry, i) => {
    const row = el("tr");
    row.appendChild(el("td", null, entry.variable));
    row.appendChild(el("td", null, opt.correction[i]));
    row.appendChild(el("td", null, entry.preferred));
    row.appendChild(el("td", null, opt.adjustments[i]));
    row.appendChild(el("td", null, entry.weight));
    row.appendChild(el("td", null, opt.perItemCost[i]));
    table.appendChild(row);
  });
  auditResult.appendChild(table);
  auditResult.appendChild(
    el(
      "p",
      "verdict ok num",
      `总成本 = Σ 权重 × 调整量² = ${opt.cost}；同值候选已按变量标识顺序的调整量字典序裁决（${opt.tieBreak}）。`
    )
  );

  const details = el("details", "audit-evidence");
  details.open = true;
  details.appendChild(el("summary", null, "可复算的最优界证据（LLL 约化 + Fincke–Pohst / 精确有理 LDLᵀ）"));
  const ev = opt.evidence;
  details.appendChild(
    el(
      "p",
      "num explanation",
      `整数二次型 f(t) = tᵀGt + 2hᵀt + c：特解代价（初始界）c = ${ev.constant}，` +
        `连续最小代价 ρ₀ = ${ev.rationalMinimum.numerator}/${ev.rationalMinimum.denominator}，` +
        `最终界（最优整数代价）= ${ev.finalBound}。枚举在 LLL 约化坐标 t′ 中进行` +
        `（t = U·t′，U 幺模），搜索椭球始终以现任可行解的精确代价为界，` +
        `不使用浮点距离，也不设人为半径。`
    )
  );
  details.appendChild(el("p", "num", "约化 Gram 矩阵 G = UᵀBᵀWBU："));
  details.appendChild(renderMatrix(ev.gram));
  details.appendChild(el("p", "num", `约化线性系数 h（线性项为 2hᵀt′）= [${ev.linear.join(", ")}]`));
  details.appendChild(el("p", "num", "幺模坐标变换 U（t = U·t′）："));
  details.appendChild(renderMatrix(ev.basisTransform));
  details.appendChild(
    el("p", "num", `最优约化坐标 t′ = [${ev.reducedCoordinates.join(", ")}]`)
  );

  const levelTable = el("table", "num");
  const levelHead = el("tr");
  ["层（自由坐标）", "枚举下界", "枚举上界", "最优取值", "截至该层的精确有理代价"]
    .forEach((title) => levelHead.appendChild(el("th", null, title)));
  levelTable.appendChild(levelHead);
  ev.levels.forEach((entry) => {
    const row = el("tr");
    row.appendChild(el("td", null, `t_${entry.level + 1}`));
    row.appendChild(el("td", null, entry.low));
    row.appendChild(el("td", null, entry.high));
    row.appendChild(el("td", null, entry.chosen));
    row.appendChild(
      el("td", null, `${entry.accumulated.numerator}/${entry.accumulated.denominator}`)
    );
    levelTable.appendChild(row);
  });
  details.appendChild(levelTable);
  details.appendChild(
    el(
      "p",
      "num",
      `枚举统计：访问节点 ${ev.stats.nodesVisited}，剪枝节点 ${ev.stats.nodesPruned}，` +
        `评估可行候选 ${ev.stats.leavesEvaluated}，同值裁决比较 ${ev.stats.tiesCompared}。`
    )
  );
  auditResult.appendChild(details);
}

function renderMatrix(rows) {
  const table = el("table", "num matrix");
  rows.forEach((row) => {
    const tr = el("tr");
    row.forEach((value) => tr.appendChild(el("td", null, value)));
    table.appendChild(tr);
  });
  return table;
}

function loadExample(example) {
  draftFields.variables.value = example.variables;
  draftFields.matrix.value = example.matrix;
  draftFields.target.value = example.target;
  invalidateDraft();
  setStatus("示例已载入，可发起复核。", "");
}

$("load-solvable").addEventListener("click", () => loadExample(EXAMPLE_SOLVABLE));
$("load-unsolvable").addEventListener("click", () => loadExample(EXAMPLE_UNSOLVABLE));

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
setStatus("已载入可解示例，可直接发起复核。", "");
