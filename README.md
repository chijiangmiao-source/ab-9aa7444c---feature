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
- 可解后可对每个机械校正项填写**首选整数垫片数**与**正整数权重**发起
  **优选校正审计**：系统在原耦合方程严格成立的**全部整数解**中，以精确
  最近格点搜索选出加权平方偏差最小的校正向量，并列时按变量标识顺序的
  调整量字典序稳定裁决，并返回首选值、逐项偏差、总成本与可复算的最优界
  证据（Babai 初始界点、规约基与幺模坐标变换、有理 Gram-Schmidt、公分母
  整数枚举计数）；
- 前端以世代号 + `AbortController` 保证：发起较大复核后立即编辑草稿或取消时，
  过期返回不会覆盖当前草稿的状态或结论。

## 运行

```bash
# 启动页面与健康路径（宿主端口默认 8080，可用 HOST_PORT 配置）
docker compose up app
HOST_PORT=9000 docker compose up app

# 页面:  http://localhost:8080/
# 健康:  http://localhost:8080/health

# 一次性验收（无解证据 → 测试与构建 → HTTP 冒烟 → 优选校正审计；退出码即结论）
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

### `POST /api/optimize`

在 `/api/review` 的字段之外，须**逐项**附 `preferred`（每个变量的首选整数
垫片数，互不相同，字符串或安全范围内整数，禁止浮点）与 `weights`（每项
正整数权重）。可解时返回普通复核的全部字段，并额外给出：

- `optimization.items[]`：每项的 `preferred` / `weight` / `optimal` /
  `deviation` / `deviationSquared` / `weightedTerm`；
- `optimization.cost`：加权平方偏差总成本（精确整数文本），`tieCount`
  为达到该最小值的候选数，`coordinates` 为最优解在齐次解基中的整数坐标
  （可由“特解 + Σ 坐标·自由方向”复算）；
- `optimization.boundEvidence`：LLL δ 与所用算术（常规为 `exact_rational`；
  极端大整数基仅在 LLL 预处理决策上用自适应 Decimal，列运算始终为精确整数、
  坐标变换幺模，枚举本身恒为精确整数）、公分母 `commonDenominator`、目标对
  格空间的正交补常量 `orthogonalResidual`、精确 Babai 初始界点及其成本、
  规约基 `reducedBasis`、幺模坐标变换 `coordinateTransform`、加权 Gram 矩阵、
  有理 Gram-Schmidt（`mu` 与平方范数，均为分数文本），以及枚举访问节点 /
  叶子 / 空区间计数。

原方程无解时仍返回既有的 `obstruction`，且 `optimization` 为 `null`。
首选值缺失、重复、非整数，或权重非正、数量不符时以 HTTP 400 拒绝。
不带首选设置的 `/api/review` 请求与页面展示保持不变（响应无 `optimization`）。

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -t .   # 测试
python3 -m app.server                         # 启动（PORT 环境变量可改端口）
APP_BASE_URL=http://127.0.0.1:8080 python3 scripts/verify.py  # 验收
```

## 目录

```
app/diophantine.py   精确整数求解器（Smith 正规形，任意精度）；整数核基 gcd 构造
app/lattice.py       精确加权最近格点优化（LLL 预处理 + Babai 界 + 整数枚举）
app/server.py        标准库 HTTP 服务（页面、/health、/api/review、/api/optimize）
app/static/          复核页面（精确文本渲染、防竞态）
tests/               求解器、格优化器与 HTTP 层单元测试
scripts/verify.py    一次性验收例程
Dockerfile           应用镜像
docker-compose.yml   app（可配置宿主端口）+ verify（一次性验收）
```
