# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级；另设**加固立项台**，
在加固设计开工前核对产权同意、维修资金与疏散分区临时安置容量。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/reinforcement_rules.py`：加固立项纯规则——资金缺口、三类立项卡点、容量叠加与可调整窗口。
- `src/repository.py`：SQLite建表、事务、版本控制、审计链和加固立项存档。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：加固立项台操作页面（登记、改值、批准、分区容量、审计）。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8317
```

默认端口为`8317`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 加固立项台

登记字段：产权单位与是否书面同意、估价、可用维修资金（资金缺口=估价-资金）、
疏散分区、施工起止日期、临时安置人数。

立项规则（判断只在 `src/reinforcement_rules.py`）：

1. **产权同意缺失**（`owner_consent=false`）或**资金不足**（缺口>0）：项目只能停留在`pending`待立项；
2. 同一疏散分区内，施工窗口与已批准项目重叠时安置人数逐天叠加，任一天超过分区容量即`capacity`卡点，
   不得批准，并返回自原开工日顺延找到的第一个等长**可调整窗口**（首尾相接不算重叠）；分区未登记容量同样卡住；
3. 三类卡点全部为空，`review_board` 才能批准为`approved`，批准留存快照（含当时缺口）；
4. 批准后改动任一受护字段（同意、估价、资金、分区、施工起止、安置人数），**原批复立即失效**：
   回到`pending`、清空快照、记录`reinforcement_void`审计，重新计算卡点后才能再次批准；仅改建筑名称不作废；
5. 改值与批准都带`expected_version`乐观锁，并发改动返回409。列表接口直接返回`blockers`、`funding_gap`
   和`suggested_window`，操作页据此显示卡点。

角色：assessor/structural_engineer 可登记与改值；review_board 可登记分区容量并批准；viewer 只读。

加固立项接口：

- `GET /api/reinforcement`（可选`?status=pending|approved`）、`GET /api/reinforcement/{id}`
- `POST /api/reinforcement`、`POST /api/reinforcement/{id}`（改值，需`expected_version`）
- `POST /api/reinforcement/{id}/approve`（需`expected_version`；409响应带卡点与建议窗口）
- `GET /api/zones`、`POST /api/zones`（`{zone, capacity}`）

## 测试

```bash
python3 -m unittest discover -s tests -v
```
