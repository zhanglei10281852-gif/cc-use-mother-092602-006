# 科学计算任务运营服务

这是一个面向科研平台、实验室和计算中心的 Python 后端，使用 FastAPI 与 SQLite 管理参数模板、计算任务提交、优先级排队、工作者领取、取消、失败重试、租约恢复、用户配额、结果版本和管理员人工干预记录。服务同时保留用户、角色、会话和审计等基础能力，所有运行数据都在单个本地数据库文件中，不需要另行部署数据库、缓存、消息队列或浏览器界面。

## 已有能力

- 参数模板：保存参数类型、必填项、数值范围、默认值、最大运行时间和最大尝试次数。
- 任务提交：根据模板校验参数，使用用户与幂等键避免重复创建，并保存项目、提交人和输入摘要。
- 排队领取：按优先级和进入队列的顺序分配任务，工作者可声明算法能力并获得有期限的租约。
- 执行回执：工作者可以续租、提交结果或报告失败；可重试错误使用确定的退避时间重新排队。
- 失败恢复：租约过期后可由恢复入口将任务重新排队，达到最大尝试次数的任务转为失败。
- 配额控制：可保存用户、角色或项目的排队数、运行数和每日提交上限；当前提交路径执行用户配额。
- 结果版本：每次成功回执保存不可变结果、指标摘要和内容摘要，任务指向当前结果版本。
- 人工干预：取消、人工重试、优先级调整和批量操作均保留操作者、原因、前后状态和批次标识。
- 结果登记簿：接收运行元数据（载荷/模型/参数版本、辐射剂量、热循环、是否降额）与结果摘要，维护不可变版本、质量标签、人工复核意见与发布状态，支持按实验条件比较、撤回错误标注和返回带 HMAC 签名摘要的查询响应；来源链（载荷与上游运行引用）随记录持久保存。
- 登录与角色：基础管理模块提供管理员初始化、用户、角色、会话和细粒度权限。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/compute-operations.db`。可以复制 `.env.example` 并通过 `TOWNSHIP_DATABASE_PATH` 指定其他本地路径。

## 数据库初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

计算任务摘要位于 `/api/compute/summary`，模板、配额、提交、领取、回执和人工操作接口统一使用 `/api/compute` 前缀。

## 结果登记簿

结果登记簿解决“同一模型在不同辐射剂量与热循环下结果不同、发布前必须可溯源”的问题，接口统一使用 `/api/registry` 前缀：

- `POST /runs`：登记一次运行。要求给出模型编码/版本、参数（载荷）编码/版本、实验条件（`radiation_dose`、`thermal_cycles`、`derated` 等，允许附加自定义条件）、运行时间、提交方、至少一个 `source_refs`（来源链）以及结果 `summary`（只存摘要，不存完整结果）。服务计算摘要摘要值、来源链摘要与覆盖全部溯源字段的指纹。
- 幂等：登记体（元数据+条件+来源链+摘要）的指纹完全相同的重复上传返回原记录（HTTP 200），不会产生新版本；任一条件或摘要变化都会形成新的运行身份（HTTP 201）。已登记记录只追加标签/复核/发布事件，不修改既有字段。
- `POST /runs/{uid}/quality-labels`、`POST /runs/{uid}/reviews`：追加质量标签（gold/silver/bronze/quarantined/untrusted）与逐版递增的人工复核意见。
- `POST /runs/{uid}/publish`、`POST /runs/{uid}/withdraw`：发布与撤回。撤回为终态，不能重新发布（须登记修正后的新运行），撤回的结果不会再出现在 `published_only=true` 或 `status=published` 的查询中。
- `GET /runs`：按模型、参数、是否降额、质量标签、发布状态过滤（仅列摘要字段）；`GET /runs/{uid}` 返回含来源链、标签历史、复核历史与发布事件的完整记录。
- `GET /compare?by=radiation_dose|thermal_cycles|payload|model|derated`：按实验条件分组比较。
- 查询与比较响应是带签名的信封 `{payload, generated_at, key_id, alg, signature}`（HMAC-SHA256，签名覆盖规范化后的整个 payload）。`POST /verify-signature` 可回验，任何字段被篡改都会验签失败。签名密钥取自环境变量 `RESULT_REGISTRY_SIGNING_KEY`；未配置时使用内置开发密钥且 `key_id` 标记为 `hmac-sha256:dev-insecure`，生产环境务必配置独立密钥。

登记、发布与撤回使用 SQLite 即时事务（`BEGIN IMMEDIATE`）配合指纹唯一约束：并发重复登记只有一条记录胜出，并发发布只有一个成功，复核版本号在并发下保持连续。所有状态落盘，重启连接/进程后发布状态与来源链仍可确定读取。

## 测试

```bash
python -m pytest
```

测试覆盖参数规则、幂等提交、配额拒绝、优先级领取、能力匹配、租约续期、失败退避、结果版本、取消、人工重试、批量操作和租约恢复，并覆盖结果登记簿的来源链、重复上传幂等、标签复核、发布撤回隔离、条件比较、签名验签、重启持久化与并发确定性，同时保留身份与既有科学计算模块的回归用例。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
python -m app.cli compute-demo
python -m app.cli registry-demo
```

`smoke` 在进程内检查根路径和健康接口，`compute-demo` 会创建示例参数模板、提交一个计算任务并让匹配能力的工作者领取，用于快速确认核心运营链路；`registry-demo` 登记一次运行、发布、读取带签名的发布查询并撤回，用于确认结果登记簿链路。

## 目录结构

```text
app/
  compute/         计算模板、配额、任务、结果版本和人工干预
  registry/        科学结果登记簿：不可变版本、标签、复核、发布撤回与签名查询
  api/             用户、角色、认证、审计和系统管理接口
  core/            时钟、安全、异常和分页能力
  repositories/    通用 SQLite 查询
  seismic/         既有地震计算示例领域
  services/        身份、审计和通用后台任务服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、计算运营和身份回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL、busy timeout 和同步写入策略。提交、领取、回执和人工干预使用即时事务；任务领取通过条件更新避免同一条排队记录被重复领取。服务保存 UTC 时间字符串，测试可以注入固定时钟验证退避、租约到期和跨日配额。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
