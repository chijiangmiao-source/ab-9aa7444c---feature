# 束流整形磁铁垫片校正复核

更换束流整形磁铁垫片后，多处机械校正量须同时满足测得的耦合位移关系，
且每项调整只能取整数个最小垫片单位。本应用将该校核建模为整数丢番图
方程组 `A·x = b`，以**任意精度整数**精确判定可解性：

- 可解时给出精确整数校正量，并可点选任一约束查看各项乘积、左侧和及其目标值；
- 无解时展示由可逆整数行变换（Smith 正规形 `U·A·V = D`）导出的**规范除尽障碍**：
  变换后目标 `U·b` 的某一分量不能被对应主元整除（或零行目标非零），
  并列出该行变换与原始目标的逐项乘积来源；
- 全程不将大整数转为浮点数、不对有理解四舍五入；超过 2⁵³ 的系数与目标
  在页面上仍以精确整数文本呈现；
- 前端以世代号 + `AbortController` 保证：发起较大复核后立即编辑草稿或取消时，
  过期返回不会覆盖当前草稿的状态或结论。

## 优选校正审计

复核得到可解结论后，工程师可为每项机械校正填写**首选整数垫片数**与
**正整数权重**，发起优选校正审计，使装配基准不再任意落在某个可行解上：

- 系统在原耦合方程严格成立的**全部整数解**中，精确选出加权平方偏差
  `Σ wᵢ(xᵢ − pᵢ)²` 最小的校正向量；最小值相同的候选按变量标识顺序的
  调整量字典序稳定裁决；
- 求解从现有特解与齐次解格构造**最近格点搜索**（精确有理 LLL 约化 +
  Fincke–Pohst 枚举 + 有理 LDLᵀ 三角化）：搜索椭球始终以现任可行解的
  精确代价为界，界定与比较全部以任意精度整数/有理数完成，不使用浮点
  距离、不逐个自由方向贪心、不设人为搜索半径；
- 审计返回首选值、逐项偏差、逐项成本、总成本以及**可复算的最优界证据**
  （二次型 `G, h, c`、LDLᵀ 对角、连续下界 ρ₀、逐层枚举区间与最优坐标、
  枚举统计）；
- 原方程无解时仍保留既有除尽障碍；首选值缺失、重复、非整数或权重非正
  时拒绝（HTTP 400）并清除旧优选结论；未填写首选设置的复核响应与页面
  展示保持不变；修改任一方程或首选设置并立即重提时，过期审计结果不会
  覆盖当前草稿。

## 运行

```bash
# 启动页面与健康路径（宿主端口默认 8080，可用 HOST_PORT 配置）
docker compose up app
HOST_PORT=9000 docker compose up app

# 页面:  http://localhost:8080/
# 健康:  http://localhost:8080/health

# 一次性验收（先确认无解证据，再跑测试与构建检查，最后 HTTP 冒烟；退出码即结论）
docker compose up --exit-code-from verify verify
# 或
docker compose run --rm verify
```

## 接口

### `GET /health`
返回 `{"status": "ok"}`。

### `POST /api/review`
请求体（整数请以字符串提交，避免浮点误差；安全范围内的 JSON 整数也可接受，
浮点数一律拒绝）：

```json
{
  "variables": ["K1", "K2", "K3"],
  "matrix": [["9007199254740993", "1", "0"], ["1", "3", "1"], ["0", "2", "4"]],
  "target": ["18014398509481989", "12", "10"]
}
```

可解响应（节选）：`solution` 为精确整数校正量；`constraints[i]` 含每项乘积
`terms[]`、左侧和 `sum` 与目标值 `target`；`homogeneousBasis` 为自由校正方向。

无解响应（节选）：`obstruction` 含 `type`（`non_divisible` / `zero_row`）、
变换后行号 `row`、`pivot`、`transformedTarget`、`remainder` 以及行变换来源
`uRow` / `uRowTerms`；`smith` 给出主元序列与变换后目标向量。

#### 可选的优选校正审计

请求体附带 `preferences` 时（每项变量必须恰好出现一次，`preferred` 为整数、
`weight` 为正整数，否则 HTTP 400），可解响应额外包含 `optimization`：

```json
{
  "variables": ["x", "y"],
  "matrix": [["1", "2"]],
  "target": ["3"],
  "preferences": [
    {"variable": "x", "preferred": "0", "weight": "1"},
    {"variable": "y", "preferred": "0", "weight": "1"}
  ]
}
```

`optimization`（节选）：`correction` 为最优校正向量；`adjustments` 为逐项
偏差（最优 − 首选）；`perItemCost` 为逐项加权平方偏差；`cost` 为总成本；
`coordinates` 为最优自由坐标（原齐次基下）；`tieBreak` 为同值裁决规则；
`evidence` 含约化二次型 `gram` / `linear` / `constant`、幺模坐标变换
`basisTransform`、最优约化坐标 `reducedCoordinates`、LDLᵀ 对角 `diagonal`、
连续下界 `rationalMinimum`、最终界 `finalBound`、逐层枚举区间 `levels`
与枚举统计 `stats`，全部以精确整数/有理数文本给出，可独立复算最优性。

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -t .   # 测试
python3 -m app.server                         # 启动（PORT 环境变量可改端口）
APP_BASE_URL=http://127.0.0.1:8080 python3 scripts/verify.py  # 验收
```

## 目录

```
app/diophantine.py   精确整数求解器（Smith 正规形，任意精度）
app/optimize.py      优选校正审计（精确最近格点搜索，Fincke–Pohst + 有理 LDLᵀ）
app/server.py        标准库 HTTP 服务（页面、/health、/api/review）
app/static/          复核页面（精确文本渲染、防竞态、审计面板）
tests/               求解器、优选审计与 HTTP 层单元测试
scripts/verify.py    一次性验收例程
Dockerfile           应用镜像
docker-compose.yml   app（可配置宿主端口）+ verify（一次性验收）
```
