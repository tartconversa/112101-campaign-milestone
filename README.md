# 治理阶段进度资料库

专项治理项目按阶段下达工作目标：牵头团队据此判断阶段是否达到预定治理结果，
执行团队说明各项工作何时启动、完成或因条件变化暂停。本仓库从本地合成的
阶段计划、目标范围与执行回执建立一个 **仅追加的 Python 数仓**（SQLite，
纯标准库），支持按任意历史记录时点复核阶段口径与进度。

原始资料使用 UTF-8 CSV 保存，时间值采用带时区的 ISO 8601 格式。资料为虚构
样例，其中包含不同事件时间、记录时间和待核实状态，不对应真实个人或平台。

## 目录

- `sources/` 初始批次的原始业务记录（`stages.csv`、`objectives.csv`、
  `scoped_items.csv`、`execution.csv`）。
- `sources/updates/<日期>/` 后续追加批次，结构相同，另附 `batch.json`
  （`label` 与 `ingested_at`）。
- `src/campaign_milestones/` 数仓包（结构、摄取、口径解析、报告、命令行）。
- `tests/` 源文件格式检查与数仓行为测试。

## 环境

项目仅使用 Python 标准库，无需访问网络或安装第三方包。

## 数据模型

### 两个时间轴

- **记录时间 `ingested_at`**：记录进入数仓的时间（按批次赋予）。as-of
  查询只看 `ingested_at <= T` 的记录，从而重放"当时数仓知道什么"。
- **业务时间**（`valid_from` / `due_at` / `started_at` / `reported_at` /
  `recorded_at` / `confirmed_at`）：记录自带的业务时点，用于判断迟报、
  截止后调整、启动早于确认等情形。

### 仅追加的原始层

`ingest_batch` 与四张 `raw_*` 表保存每一次批次的原始记录；触发器拒绝任何
UPDATE/DELETE，`record_id` 全局唯一（同一记录不能重复入库）。补报、复核
结论、范围调整都以**新记录**追加，历史时点口径因此可以完整重放。

### 口径解析规则（`queries.py`）

1. 阶段有效修订 = as-of 时点已入库的、revision 最大的阶段记录；若其
   **生效时间**晚于上一修订的截止时间，标记为"截止后调整"。
2. 目标按 `objective_id` 取最新版本；`confirmed` / `changed` 视为经确认
   口径进入分母，`draft` / `withdrawn` 不进入。目标的 `stage_revision`
   必须等于阶段有效修订，否则视为被替换的历史口径——进度保留备查，
   **不并入新分母**。
3. 事项范围按 `(objective_id, work_item_id)` 取最新范围记录，`included`
   才在范围内；`excluded` 的事项及历史回执进入调节项，不静默丢弃。
4. 回执按 `execution_id` 取最新版本。同一事项多次交接时，以启动最新的
   执行单为当前持有，其复核状态决定事项状态；历史回执全部保留在审计线中。
5. 事项状态：`verified`（已核验）/ `reported_unverified`（已报未核实）/
   `returned`（核实退回）/ `paused`（已暂停）/ `no_evidence`（无回执记录）/
   `out_of_scope`（已移出范围）。**缺失记录一律视为未核验，绝不推断完成。**

### 枚举取值

- 阶段状态：`planned` / `active` / `amended` / `closed`
- 目标状态：`draft` / `confirmed` / `changed` / `withdrawn`
- 范围状态：`included` / `excluded`
- 复核状态：`submitted` / `late`（迟报标记，复核上属已报未核实）/
  `accepted` / `returned` / `paused`

## 用法

以下命令均在仓库根目录执行。

### 运行测试

```bash
python -m unittest discover -s tests -v
```

### 演示（用自带合成资料走完整流程）

```bash
PYTHONPATH=src python -m campaign_milestones demo --db :memory:
```

输出：阶段清单、STG-1 在初始批次时点与当前口径下的结论复核、STG-2 结论、
CASE-7 / CASE-9 的事项说明、未核验事项清单。

### 建库与持续追加

```bash
PYTHONPATH=src python -m campaign_milestones init warehouse.db
PYTHONPATH=src python -m campaign_milestones ingest warehouse.db \
    --dir sources --label 初始批次 --at 2026-09-18T12:00:00Z
PYTHONPATH=src python -m campaign_milestones ingest warehouse.db \
    --dir sources/updates/2026-09-20 --label 追加批次 --at 2026-09-20T12:00:00Z
```

### 查询与复核

```bash
# 当前最新口径（缺省）或指定历史记录时点
PYTHONPATH=src python -m campaign_milestones report-stage warehouse.db STG-1
PYTHONPATH=src python -m campaign_milestones report-stage warehouse.db STG-1 \
    --as-of 2026-09-18T12:00:00Z

# 单个事项的交接、回执时间线与当前状态
PYTHONPATH=src python -m campaign_milestones report-item warehouse.db CASE-7

# 未核验事项清单（证据不足，不能视为完成）
PYTHONPATH=src python -m campaign_milestones unverified warehouse.db
```

## 报告能回答的问题

- 阶段结论来自**哪些有效目标**（经确认、属于当前修订口径）与**哪些实际
  事件**（每条回执的版本、团队、复核状态、入库时间）。
- 进度差异来自哪里：已报未核实、核实退回、暂停、无回执、范围变更
  （被替换的历史口径、移出范围的事项、从未纳入范围的范围外证据）。
- 哪些事项仍因证据不足而未能核验（`no_evidence` 不等于已完成）。
- 数据质量提示：迟报、截止后调整、启动早于目标首次确认、回报早于启动。

任务完成数不自动等价治理效果：报告只给出证据分类与口径来源，阶段是否
达标由牵头团队依据证据判断。
