# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/initiation_rules.py`：加固立项判断（同意、资金缺口、疏散容量、可调整窗口、批复快照），纯函数不碰存档。
- `src/initiation_service.py`：立项登记、修订作废重算、批准编排、列表卡点视图和演示数据。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `static/initiation.html`：加固立项台操作页面。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8317            # 普通启动
python3 app.py --db ./data.db --port 8317 --seed     # 首次启动灌入立项台演示数据
```

默认端口为`8317`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。
立项台页面：`http://127.0.0.1:8317/initiation`。

## 加固立项台

立项判断、存档、操作页面分开承担：`initiation_rules.py`只做判断，
`repository.py`只存档，`initiation_service.py`编排，`static/initiation.html`是操作页面。

登记项：产权单位、估价、可动用维修资金（资金缺口=估价-维修资金）、施工起止、
安置人数、疏散分区、产权同意。

立项规则：

- 产权单位未同意，或资金缺口为正（维修资金不足），只能留在“待立项”。
- 同一疏散分区内，与已立项项目施工窗口重叠的峰值安置人数（含本项目）超过分区容量，不得批准。
- 容量冲突时返回保持工期不变的“可调整窗口”建议，整体前移/后移滑动选取；同意、资金问题不提供窗口建议。
- 批准后改动产权单位、估价、资金、起止、人数、分区或同意任一批复值，原批复立即失效留痕，项目回到待立项并重新判断。
- 修订和批准都必须带`expected_version`做乐观并发控制。

立项接口（角色：initiator 立项经办人、review_board 立项审查、viewer 只读）：

- `GET  /api/initiation/projects`：列表，每条带`approvable`、`blockers`、`funding_gap`、`overlap_peak`、`window_suggestions`和批复状态。
- `POST /api/initiation/projects`：登记立项。
- `GET  /api/initiation/projects/{id}`：单条（含卡点和批复）。
- `POST /api/initiation/projects/{id}`：修订登记值，体含`expected_version`。
- `POST /api/initiation/projects/{id}/approvals`：批准，体含`expected_version`；卡点未消除返回409并附`blockers`和`window_suggestions`。
- `GET  /api/initiation/approvals`、`GET /api/initiation/projects/{id}/approvals`：批复存档（含已作废批复）。
- `GET  /api/initiation/zones`、`POST /api/initiation/zones`：疏散分区容量登记/调整。

## 主要接口（鉴定排序）

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
