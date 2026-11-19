# X5 群审批后接口建单

X5 的入口只处理一条经登记的「【测试】」草稿。机器人在汇报群获得有效同意后，发一次网页同款建单请求。官方接口读回并逐字段比对，再核验 D15 的删除条件。建单会触发同国家政策重算。提交和撤回由人工在明道云操作。

这里只提供配置结构。示例路径和标识均为占位值。真实验收由编排者在同一冻结候选的两层独立审核和当次放行后操作。

```sh
python -m qianshu.mingdao.create_order --config .private/占位配置.json --registration 占位登记号 .private/占位草稿.json
```

在本 Trace 独占的运行目录执行。固定使用该目录下权限为 `0700` 的 `.private/`，运行之间保留整个目录及其资格锁。配置、草稿、登录态和授权登记使用 `0600`。会话取自登录检查脚本生成的 `.private/mingdao-state.json`。飞书沿用 #8 的私有配置，默认读当前目录的 `.env`，也可用 `--env-file` 指定私有文件。官方接口令牌也从该文件取值，配置只登记键名。两个令牌进入请求头，台账、摘要、报告和错误信息均不记录令牌。

## 授权登记

`.private/create-authorizations.json` 按登记号索引，每项登记以下记录。

| 项 | 内容 |
| --- | --- |
| `is_test` | 固定为 `true` |
| `window.start`、`window.end` | 带时区的 ISO 8601 时刻，开始包含、结束不包含 |
| `config_sha256`、`draft_sha256`、`login_sha256` | 三份私有文件的完整原始字节 SHA-256 |
| `create_endpoint` | 与配置逐字一致的 `host`、`method`、`path`，逐项批准 |
| `owner_approval`、`readiness_confirmation`、`owner_login_confirmation` | 产品负责人当次建单授权、就绪自检确认和登录会话确认的私有回执 |
| `approver_notice`、`configurator_notice` | 审批人、配置人各自的回执。通知覆盖测试和清理期间，要求两方保持该单状态 |
| `two_reviews.internal`、`two_reviews.external` | 同一冻结候选的两份独立私有审核回执 |
| `trace_decision`、`comparison_report`、`same_country_count_evidence` | 当次放行、拟写字段比对材料、同国家生效政策只读计数的私有证据 |

草稿使用 `PolicyDraft.to_dict()` 的字段结构，每项为含 `value` 的对象。只传一条。名称以「【测试】」开头，起止日期用 `YYYY-MM-DD`，充值和赠送天数是非负整数，标签值是精确名称列表，节目包每行给出 `current_package` 和 `gift_package`（无赠送包时用 `null`）。草稿的待确认项应交人工；未给定的互斥组按 R7 核对模板为「是」，在审批卡片注明默认值。任务审批链接采用模板的值；草稿中的确定值也须和模板一致。

## 配置项

| 项 | 内容 |
| --- | --- |
| `create.endpoint` | `host` 填逐条批准的明道云网页主机，`method` 为 `POST`，`path` 填批准的 `AddWorksheetRow` 完整路径。入口核对固定平台目标及授权登记 |
| `create.identifiers` | 目标 `worksheetId`、`appId`、`viewId`、`projectId` |
| `create.session_cookie`、`create.response_id` | 私有登录态中的 cookie 名，建单响应 UUID 的取值路径 |
| `configurator.id`、`configurator.label` | 用于群内 @ 配置负责人的开放标识和显示名 |
| `template.row_id` | 同国家模板记录 UUID |
| `template.country` | 国家字段的 `control` 及预期原始 `value` |
| `fields` | 以控件 ID 为键，每项登记 `label`、数值 `type`。有对应草稿字段的登记 `draft`。七个可变值的草稿字段、任务审批链接、公司、互斥组均须映射。其他确定的草稿值也须有映射 |
| `fields.<控件>.draft_values` | 枚举、关系等字段把草稿值映射到平台类型。键为草稿值的紧凑 JSON 字符串，例如文本值的键为 `"\"占位选项\""`，值形如 `["占位选项键"]`。字段比较时仅用该映射核对模板 |
| `fields.<子表控件>` | `children` 以子控件 ID 为键，各含 `type`；`current`、`gift` 各含 `control`、`name_path`，用于从模板原始单元格读精确包名。可用 `row_metadata` 指定子行的非控件元数据键 |
| `readback_skip` | 经实测确认由工作流回填的控件 ID 列表，初次用空数组。可变字段和审批链接必须比对 |
| `official.origin`、`official.token_key` | 官方 HTTPS 源和私有环境文件中的令牌键名 |
| `official.record` | 记录的 `id`、`name`、`status`、`revision`、`cells` 的取值路径；如单元格与元数据平铺，可用 `metadata` 排除明确的非控件键 |
| `official.status_map` | 以平台原始状态的字符串为键，值为产品文档所列状态 |
| `official.read`、`search`、`tags`、`logs`、`delete` | 每项含 `path`、`success_key`、`success_value`、`data`。读取和删除指向政策表的 `/占位前缀/占位表标识/rows/{record_id}`，日志在该路径后加 `/logs`。删除登记 `method: "DELETE"`，入口固定传 `permanent=false` |
| `official.search`、`official.tags` | 配置各自表的 `/占位前缀/占位表标识/rows`，及 `name_param`、`name`、`id`、`rows`、`has_more`；可通过 `params` 提供必要的固定读取参数。同名预检须覆盖政策表，勿加视图或状态范围 |
| `official.read` | 用 `missing_status`、`missing_key`、`missing_value` 登记经平台确认的记录不存在信号 |
| `official.logs` | `rows`、`has_more`、`complete` 及单条日志的 `id`、`revision`、`before`、`after`、`time` 的取值路径 |
| `official.delete.condition` | 官方接口的条件校验请求键；`condition_value` 可为 `revision`（默认）或 `status`。仅当守望者确认平台不支持时登记为 `null` |

取值路径用 JSON 数组，例如 `["占位外层", "占位字段"]`；空数组取当前位置。读取源须在放行前确认真实响应形态。单选、下拉及多选使用选项键；关联和成员分别写成含 `sid` 或 `accountId` 的对象数组。若官方读接口的对象标识键不同，可为对应控件登记 `relation_id_key` 或 `member_id_key`。节目包当前包和赠送包按行与草稿完全一致时，照抄整行的可写控件。子表写成二维数组 JSON 字符串，各内层单元格含 `controlId`、`value`。

## 回读、停止与台账

- `trial-orders.jsonl` 和 `trace-12-create.lock` 沿用 Trace 既有名称。台账排他加锁、追加并同步落盘。曾发出或无法证明未发出的建单永久占用该 Trace 资格。仅在台账持久记录确未交给传输时，才可凭另一份当次授权重新登记。原登记号始终不可再用。
- `create-fields-*.json` 仅存字段名、类型、来源及值的 12 位 SHA-256 前缀。`create-preflight-*.json` 记需要人工处理的组装停止原因。`create-readback-*.json` 仅存字段名和比对结果。各文件以 `0600` 一次性写入；群内回读汇报只给计数。
- 建单结果未知时，台账追加 `create_unknown`，只按名称用官方接口回读供人工对账。记录匹配也不能恢复清理资格。
- D15 需要同一 UUID 从建单版本 0 连续到当前版本的完整历史，且前后状态衔接、时刻单调、末尾状态及版本与即时回读一致。无时区平台日志按 UTC+8 解释再转 UTC。只有平台响应明确完整且没有后续页才接受。`待审批` 可出现在历史里；任何一次进入 `待配置` 或后续状态即停止。删前、删后均在群里 @ 配置负责人。平台返回已登记的不存在信号，才写入 `delete_verified`。
- 退出码 `0` 表示回读全部通过且删除已核验。`1` 表示停止、回读差异或流程未确认，以私有报告和台账为准。`2` 表示调用参数格式无效。
