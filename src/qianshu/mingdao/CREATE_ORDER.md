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
| `notices` | 按 `notice_recipients` 的 `key` 逐人登记回执。每份回执须为非空字符串，回执须互异，名单外的键会导致停止。免通知时也须登记产品负责人的回执，例如 `{"placeholder_owner": "占位免通知回执"}` |
| `notice_waiver` | 产品负责人批准免事前通知的留痕，有免通知时填写非空字符串，例如 `"占位免通知批准登记"`。正常通知时省略此项 |
| `two_reviews.internal`、`two_reviews.external` | 同一冻结候选的两份独立私有审核回执 |
| `trace_decision`、`comparison_report`、`same_country_count_evidence` | 当次放行、拟写字段比对材料、同国家生效政策只读计数的私有证据 |

草稿使用 `PolicyDraft.to_dict()` 的字段结构，每项为含 `value` 的对象。只传一条。名称以「【测试】」开头，起止日期用 `YYYY-MM-DD`，充值和赠送天数是非负整数，标签值是确定的名称列表，节目包每行给出 `current_package` 和 `gift_package`（无赠送包时用 `null`）。草稿的待确认项应交人工。未给定的互斥组按 R7 核对模板为「是」，在审批卡片注明默认值。采用模板值的字段须与草稿中的确定值一致，任务审批链接采用模板值。

## 配置项

| 项 | 内容 |
| --- | --- |
| `create.endpoint` | `host` 填逐条批准的明道云网页主机，`method` 为 `POST`，`path` 填批准的 `AddWorksheetRow` 完整路径。入口核对固定平台目标及授权登记 |
| `create.identifiers` | 目标 `worksheetId`、`appId`、`viewId`、`projectId` |
| `create.session_cookie`、`create.response_id` | 私有登录态中的 cookie 名，建单响应中与官方 `rowId` 对应的 UUID 取值路径 |
| `configurator.id`、`configurator.label` | 用于群内 @ 配置负责人的开放标识和显示名 |
| `notice_recipients` | 非空列表，每项含唯一的稳定标识 `key`、显示名 `label` 和角色 `role`。建单预检和删除前逐人核对。正常通知的名单须至少覆盖一名审批人（`approver`）和一名配置人（`configurator`）。G173 免除当次 X5 测试单的事前通知，授权登记须有有效的 `notice_waiver`，私有名单仅登记产品负责人（`owner`），例如 `[{"key": "placeholder_owner", "label": "占位通知对象", "role": "owner"}]` |
| `template.row_id` | 同国家模板记录的 `rowId`，须为 UUID |
| `template.country` | 国家字段的 `control` 及预期原始 `value`。国家控件须避开从草稿写入的字段 |
| `fields` | 以控件 ID 为键，每项登记 `label`、数值 `type`。私有配置按 11 月 18 日 UTC 录制的 X5 建单请求登记全部 55 个字段，写入仅限这些字段。五个可变标量字段（政策名称、起止日期、充值与赠送天数）从草稿填写。用户标签和节目包从模板各自的官方关联行挑选。0.1.0 只建定向政策测试单，草稿无用户标签时停止并交人工。节目包按草稿逐行挑选并照抄匹配的模板行，任务审批链接、公司、互斥组及其他确定的草稿值也须映射到 `draft`。除固定字段外，配置字段须在模板中存在。模板的系统字段、自动计算字段及其他未配置的键均跳过 |
| `fixed`、`applicant_control` | `fixed` 的键须恰好是 `official.status_control` 与 `applicant_control`，分别登记「新建」选项键与登录账号成员 ID，例如 `{"placeholder_status_control": ["placeholder_new_option"], "placeholder_applicant_control": ["placeholder_member"]}`。两个控件均须在 `fields` 内，且须避开五个可变标量字段和任务审批链接 |
| `fields.<控件>.draft_values` | 枚举、关系等字段把草稿值映射到平台类型。键为草稿值的紧凑 JSON 字符串，例如文本值的键为 `"\"占位选项\""`，值形如 `["占位选项键"]`。字段比较时仅用该映射核对模板 |
| `fields.<子表控件>` | `children` 以子控件 ID 为键，各含 `type`。`current`、`gift` 各含在 `children` 中登记的 `control` 及 `name_path`，用于从对应关联行的子格读取包名。子表关联行按 `children` 白名单取值。其他子表单元格解析可用 `row_metadata` 指定非控件元数据键 |
| `readback_skip` | 经实测确认由工作流回填的控件 ID 列表，初次用空数组。可变字段和审批链接必须比对 |
| `official.origin`、`official.token_key`、`official.app_id_key` | 官方 HTTPS 源及私有环境文件中的个人令牌、应用 ID 键名，例如 `https://placeholder.invalid`、`PLACEHOLDER_HAP_TOKEN`、`PLACEHOLDER_HAP_APP_ID` |
| `official.search` | 含 `path`、`name_control`、`view_ids`。路径为 `/v3/app/worksheets/placeholder_worksheet/rows/list`，名称字段为政策名称的 controlId。视图列表形如 `["placeholder_view"]`，须登记覆盖所有政策记录的全部视图 |
| `official.tag_relation.name_keys` | 登记 1～2 个不同的候选标签名键，例如 `["placeholder_tag_title_control", "placeholder_tag_alias"]`，用于从模板用户标签的关联行取名称 |
| `official.page_size`、`official.page_limit` | 每页条数与每视图页数上限，正整数，默认分别为 50、20。查询逐个视图从第 1 页开始，用携带 `viewId` 的官方列表 POST 和名称等值筛选。每个视图读到短页才视为读完，任一页失败或满页达到上限均停止。每行须有 UUID 格式的 `rowId`。名称精确匹配后按 `rowId` 忽略大小写去重。政策同名检查合计须为 0。 |
| `official.read` | `path` 为政策表的 `/v3/app/worksheets/placeholder_worksheet/rows/{record_id}`。详情、日志和删除路径中的 `{record_id}` 均传入 `rowId`。模板、建单回读和删前回读使用同一详情解析，取平铺的 controlId 字段并按 UUID 格式核验 `rowId`，比较忽略大小写。详情或列表缺少有效 `rowId` 即停止，`_id` 仅是系统元数据。记录不存在信号须同时满足私有配置登记的 HTTP 状态、响应 `success: false`、指定路径的值及类型。用 `missing_status`、`missing_key`、`missing_value` 登记，例如错误码的取值路径为 `["error_code"]` |
| `official.status_control`、`official.status_map` | 政策状态字段的 controlId，如 `placeholder_status_control`。键须同时登记选项 key 与平台显示文本（日志里存的是显示文本），值为产品文档所列状态。官方读记录的政策状态形如 `[{"key": "placeholder_new_option", "value": "新建（占位部门）"}]`。回读至少须命中一项映射，两项均命中时结果须相同。回读和日志用同一个状态控件 |
| `official.logs` | `path` 为政策记录读取路径加 `/logs`。请求显式取第 1 页、每页 50 条。可用 `page_limit` 设置更严格的停止阈值，须为 1～50 的整数，默认 50，日志条数达到阈值即停止 |
| `official.delete` | `path` 与政策记录读取一致，`method` 为 `DELETE`。固定 JSON 请求体含 `triggerWorkflow: false` 和 `permanent: false`。若平台支持按状态条件删除，`condition` 登记其请求键，传入即时回读的状态选项 key。旧版整数或字符串回读传入原始值。仅当守望者确认平台不支持条件校验时登记 `condition: null` |

取值路径用 JSON 数组，例如 `["占位外层", "占位字段"]`；空数组取当前位置。读取源须在放行前确认真实响应形态。单选、下拉及多选使用选项键；关联和成员分别写成含 `sid` 或 `accountId` 的对象数组。若官方读接口的对象标识键不同，可为对应控件登记 `relation_id_key` 或 `member_id_key`。

节目包与用户标签的模板详情格仅含整数计数。读取关联行时，在模板官方详情路径后追加 `/relations/{字段控件 ID}`，通过 GET 显式请求 `pageIndex=1&pageSize=50`。仅接受 `success: true`、列表 `data.rows`、与返回行数相等且小于 50 的整数 `data.total`。每行须有 UUID 格式的 `rowId`，对应模板格的整数须等于 `total`，否则在发布群审批卡前停止。

比较节目包和用户标签名称时去掉首尾空白、忽略大小写，连续的空格和下划线统一按一个下划线比较。例如草稿 `PLACEHOLDER Basic` 可匹配模板 `PLACEHOLDER_Basic`。草稿每行的当前包和赠送包须同时且唯一匹配一条模板行，空赠送包须与空赠送包匹配。任一行无匹配或有多条匹配，按「节目包与模板不同，需人工」停止。按草稿顺序写入匹配行，只取 `children` 登记的子控件。关联行缺失任一已登记子控件时停止。关联对象数组写成仅含 `sid` 的 JSON 数组，字符串原样写入，空数组写成 `[]`。子表写成二维数组 JSON 字符串，各内层单元格含 `controlId`、`value`。

草稿每个用户标签须唯一匹配一条模板关联行，任一候选名称键命中即匹配。缺失或歧义按「用户标签与模板不同，需人工」停止。同一 `rowId`（忽略大小写）被两个草稿标签命中时停止。按草稿顺序写入匹配行的 `rowId`，保留原始大小写。「按名称查任意标签」不在本版（#38）。

单选写入形如 `["placeholder_option"]`，官方读记录形如 `[{"key": "placeholder_option", "value": "占位显示文本"}]`，按 `key` 核对写入与回读是否一致。

## 回读、停止与台账

- `trial-orders.jsonl` 和 `trace-12-create.lock` 沿用 Trace 既有名称。台账排他加锁、追加并同步落盘。曾发出或无法证明未发出的建单永久占用该 Trace 资格。仅在台账持久记录确未交给传输时，才可凭另一份当次授权重新登记。原登记号始终不可再用。
- `create-fields-*.json` 记录字段名、类型、来源及值的 12 位 SHA-256 前缀。`create-preflight-*.json` 记录需要人工处理的预检停止原因。两份材料均只列出通知对象显示名和是否已回执。回执缺失时，错误只列缺失的 `key`。`create-readback-*.json` 记录字段名和比对结果，并在「可能由工作流回填」中列出回读与写入不一致的字段名及类型。各文件以 `0600` 一次性写入。群内回读汇报只给计数。
- 发送前复核登录态摘要、审批认领和连接代次。在同步边界内核对共用截止时间，至少剩余 10 秒才交给传输。截止前余量不足或交接前关闭审批，台账记录 `aborted_before_write`。
- 建单与官方接口的连接超时为 10 秒、读响应超时为 30 秒，均由代码常量控制。建单响应必须完整收到声明的正文或分块结束标记，再解析 JSON。建单结果未知时，台账追加 `create_unknown`，只按名称用官方接口回读供人工对账。记录匹配也不能恢复清理资格。
- 逐字段比对后，以 5 秒为间隔回读。删前每次回读都要求当前为「新建」，且连续两次政策状态和 `_updatedAt` 一致才继续 D15。稳定等待上限为 120 秒，临近上限会缩短回读超时，超过上限的回读结果作废。无法在限时内确认稳定时停止删除，群内 @ 配置负责人并交人工处理。
- D15 要求日志接口明确成功，返回非空短页，日志全部可解析，且 `flag` 为假或缺失、`lastMark` 为空或缺失。遍历全部日志的全部改动，以配置的 controlId 识别政策状态改动。状态改动的旧端与新端均核对原值和显示文本。非空原值须经 `status_map` 映射为「新建」或「待审批」，非空显示文本也须为其中之一。同一端两者均非空时须指向同一状态。旧端两者均为空字符串或 `null` 时按创建时空旧值接受，新端至少一项非空。任一端有未知状态、待配置、后续状态或冲突时停止删除。非状态字段允许空值。无时区的日志及更新时间按 UTC+8 解释。删除前立即再核对名称、`rowId`、当前「新建」状态和已核对过的更新时间。删除前后在群里 @ 配置负责人。
- 删除后，须同时收到详情接口已登记的记录不存在信号，并读完配置中全部视图的所有页、确认精确同名记录为 0 条，才追加 `delete_verified`。任一条件未确认则追加 `delete_unconfirmed`，在群里 @ 配置负责人上报并交人工核对。
- 退出码 `0` 表示回读全部通过且删除已核验。`1` 表示停止、回读差异或流程未确认，以私有报告和台账为准。`2` 表示调用参数格式无效。

## 残余风险

最后检查到写出字节之间的同线程瞬时窗口无法原子化，守望者 G172 接受该风险，发生时测试单仍在台账内并按 D15 清理。
