# X5 群审批后接口建单

X5 的入口只处理一条经登记的「【测试】」草稿。机器人在汇报群获得有效同意后，发一次网页同款建单请求。官方接口读回并逐字段比对，再核验 D15 的删除条件。建单会触发同国家政策重算。提交和撤回由人工在明道云操作。

这里只提供配置结构。示例路径和标识均为占位值。真实验收由编排者在同一冻结候选的两层独立审核和当次放行后操作。

```sh
python -m qianshu.mingdao.create_order --config .private/占位配置.json --registration 占位登记号 .private/占位草稿.json
```

在本 Trace 独占的运行目录执行。固定使用该目录下权限为 `0700` 的 `.private/`，运行之间保留整个目录及其资格锁。配置、草稿、登录态和授权登记使用 `0600`。会话取自登录检查脚本生成的 `.private/mingdao-state.json`。飞书沿用 #8 的私有配置，默认读当前目录的 `.env`，也可用 `--env-file` 指定私有文件。官方接口个人令牌和应用 ID 也从该文件取值，配置只登记键名。官方请求使用 `Authorization: Bearer <占位个人令牌>` 和 `HAP-Appid: <占位应用标识>`。台账、摘要、报告和错误信息均不记录这些值或网页会话令牌。

## 授权登记

`.private/create-authorizations.json` 按登记号索引，每项登记以下记录。

| 项 | 内容 |
| --- | --- |
| `is_test` | 固定为 `true` |
| `window.start`、`window.end` | 带时区的 ISO 8601 时刻，开始包含、结束不包含 |
| `config_sha256`、`draft_sha256`、`login_sha256` | 三份私有文件的完整原始字节 SHA-256 |
| `create_endpoint` | 与配置逐字一致的 `host`、`method`、`path`，逐项批准 |
| `owner_approval`、`readiness_confirmation`、`owner_login_confirmation` | 产品负责人当次建单授权、就绪自检确认和登录会话确认的私有回执 |
| `notices` | 按 `notice_recipients` 的 `key` 登记回执，例如 `{"placeholder_owner": "占位免通知回执"}`。名单逐人核对，回执须非空且互异，名单外的键也会导致停止。按当次裁定执行事前通知或登记免通知回执 |
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
| `notice_recipients` | 非空列表，每项含唯一的稳定标识 `key` 和显示名 `label`，例如 `[{"key": "placeholder_owner", "label": "占位通知对象"}]`。按名单在建单预检和删除前逐人核对。G173 免除当次 X5 测试单的事前通知，私有名单只登记产品负责人，对应回执登记为产品负责人免通知 |
| `template.row_id` | 同国家模板记录 UUID |
| `template.country` | 国家字段的 `control` 及预期原始 `value` |
| `fields` | 以控件 ID 为键，每项登记 `label`、数值 `type`。私有配置按 11 月 18 日 UTC 录制的 X5 建单请求登记全部 55 个字段，写入仅限这些字段。五个可变字段（政策名称、起止日期、充值与赠送天数）从草稿填写。标签和节目包核对草稿后照抄模板，任务审批链接、公司、互斥组及其他确定的草稿值也须映射到 `draft`。固定值之外的配置字段须在模板中存在。模板的系统字段、自动计算字段及其他未配置的键均跳过 |
| `fixed`、`applicant_control` | `fixed` 按控件 ID 登记固定值，例如 `{"placeholder_status_control": ["placeholder_new_option"], "placeholder_applicant_control": ["placeholder_member"]}`。须包含 `official.status_control` 对应的「新建」选项键，及 `applicant_control` 对应的登录账号成员 ID。固定字段直接取配置值，跳过草稿与模板的冲突比对。固定字段须在 `fields` 内，且须避开五个可变字段和任务审批链接 |
| `fields.<控件>.draft_values` | 枚举、关系等字段把草稿值映射到平台类型。键为草稿值的紧凑 JSON 字符串，例如文本值的键为 `"\"占位选项\""`，值形如 `["占位选项键"]`。字段比较时仅用该映射核对模板 |
| `fields.<子表控件>` | `children` 以子控件 ID 为键，各含 `type`；`current`、`gift` 各含 `control`、`name_path`，用于从模板原始单元格读精确包名。可用 `row_metadata` 指定子行的非控件元数据键 |
| `readback_skip` | 经实测确认由工作流回填的控件 ID 列表，初次用空数组。可变字段和审批链接必须比对 |
| `official.origin`、`official.token_key`、`official.app_id_key` | 官方 HTTPS 源及私有环境文件中的个人令牌、应用 ID 键名，例如 `https://placeholder.invalid`、`PLACEHOLDER_HAP_TOKEN`、`PLACEHOLDER_HAP_APP_ID` |
| `official.search`、`official.tags` | 各含 `path`、`name_control`、`view_ids`。路径为 `/v3/app/worksheets/placeholder_worksheet/rows/list`，两个查询使用各自的表和名称字段 controlId。视图列表形如 `["placeholder_view"]`，每张表至少登记一个视图。政策同名检查须登记覆盖所有政策记录的全部视图 |
| `official.page_size`、`official.page_limit` | 每页条数与每视图页数上限，正整数，默认分别为 50、20。查询逐个视图从第 1 页开始，用携带 `viewId` 的官方列表 POST 和名称等值筛选。每个视图读到短页才视为读完，任一页失败或满页达到上限均停止。按返回行的名称精确比对，政策同名检查合计须为 0。标签查询读完全部视图后，按记录 `_id` 去重，须恰好匹配 1 条 |
| `official.read` | `path` 为政策表的 `/v3/app/worksheets/placeholder_worksheet/rows/{record_id}`。模板、建单回读和删前回读使用同一详情解析，取平铺的 controlId 字段及系统键 `_id`。用 `missing_status`、`missing_key`、`missing_value` 登记经平台确认的记录不存在信号，例如在私有登记中填写实际错误码的取值路径 `["error_code"]` |
| `official.status_control`、`official.status_map` | 政策状态字段的 controlId，如 `placeholder_status_control`。状态映射以平台原始状态的字符串为键，值为产品文档所列状态。回读和日志用同一个状态控件 |
| `official.logs` | `path` 为政策记录读取路径加 `/logs`。请求显式取第 1 页、每页 50 条。可用 `page_limit` 设置更严格的停止阈值，须为 1～50 的整数，默认 50，日志条数达到阈值即停止 |
| `official.delete` | `path` 与政策记录读取一致，`method` 为 `DELETE`。固定 JSON 请求体含 `triggerWorkflow: false` 和 `permanent: false`。若平台支持按状态条件删除，`condition` 登记其请求键，传入即时回读的原始状态。仅当守望者确认平台不支持条件校验时登记 `condition: null` |

取值路径用 JSON 数组，例如 `["占位外层", "占位字段"]`；空数组取当前位置。读取源须在放行前确认真实响应形态。单选、下拉及多选使用选项键；关联和成员分别写成含 `sid` 或 `accountId` 的对象数组。若官方读接口的对象标识键不同，可为对应控件登记 `relation_id_key` 或 `member_id_key`。节目包当前包和赠送包按行与草稿完全一致时，照抄整行的可写控件。子表写成二维数组 JSON 字符串，各内层单元格含 `controlId`、`value`。

## 回读、停止与台账

- `trial-orders.jsonl` 和 `trace-12-create.lock` 沿用 Trace 既有名称。台账排他加锁、追加并同步落盘。曾发出或无法证明未发出的建单永久占用该 Trace 资格。仅在台账持久记录确未交给传输时，才可凭另一份当次授权重新登记。原登记号始终不可再用。
- `create-fields-*.json` 记录字段名、类型、来源及值的 12 位 SHA-256 前缀。`create-preflight-*.json` 记录需要人工处理的预检停止原因。两份材料均只列出通知对象显示名和是否已回执。回执缺失时，错误只列缺失的 `key`。`create-readback-*.json` 记录字段名和比对结果，并在「可能由工作流回填」中列出回读与写入不一致的字段名及类型。各文件以 `0600` 一次性写入。群内回读汇报只给计数。
- 发送前复核登录态摘要、审批认领和连接代次。在同步边界内核对共用截止时间，至少剩余 10 秒才交给传输。截止前余量不足或交接前关闭审批，台账记录 `aborted_before_write`。
- 建单与官方接口的连接超时为 10 秒、读响应超时为 30 秒，均由代码常量控制。建单响应必须完整收到声明的正文或分块结束标记，再解析 JSON。建单结果未知时，台账追加 `create_unknown`，只按名称用官方接口回读供人工对账。记录匹配也不能恢复清理资格。
- 逐字段比对后，以 5 秒为间隔回读。删前每次回读都要求当前为「新建」，且连续两次政策状态和 `_updatedAt` 一致才继续 D15。稳定等待上限为 120 秒，临近上限会缩短回读超时，超过上限的回读结果作废。无法在限时内确认稳定时停止删除，群内 @ 配置负责人并交人工处理。
- D15 要求日志接口明确成功，返回非空短页，日志全部可解析，且 `flag` 为假或缺失、`lastMark` 为空或缺失。遍历全部日志的全部改动，以配置的 controlId 识别政策状态改动，`newText` 只允许「新建」或「待审批」。`oldValue`、`oldText` 允许为空值，非状态字段的 `newValue`、`newText` 也允许为空值。无时区的日志及更新时间按 UTC+8 解释。删除前立即再核对名称、记录 ID、当前「新建」状态和已核对过的更新时间。删除前后在群里 @ 配置负责人，仅凭官方回读的已登记不存在信号追加 `delete_verified`。
- 退出码 `0` 表示回读全部通过且删除已核验。`1` 表示停止、回读差异或流程未确认，以私有报告和台账为准。`2` 表示调用参数格式无效。

## 残余风险

最后检查到写出字节之间的同线程瞬时窗口无法原子化，守望者 G172 接受该风险，发生时测试单仍在台账内并按 D15 清理。
