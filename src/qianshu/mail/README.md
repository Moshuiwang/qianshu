# Trace #12 邮箱入口

## X7 单封试跑

常驻进程可每 60 秒调一次 `qianshu.mail.poll_once(config, process)`，也可调用
`run_polling(config, process, stop)`，由 `threading.Event` 控制结束。连接参数
`MAIL_HOST`、`MAIL_PORT`、`MAIL_USER`、`MAIL_PASSWORD` 和可选的 `MAIL_FOLDER`
沿用 `load_config`。调用方在配置映射中提供 `MAIL_SENT_FOLDER`（已发送文件夹）、
`MAIL_APPROVER`（产品负责人邮箱）和 `MAIL_TEST_MARKER`（本次验收生成的一次性随机标记，
使用 ASCII 字母、数字、下划线或短横线）。两个入口都可选传 `MAIL_PRIVATE_DIR`，
默认为当前目录下的 `.private/`。

收件箱中恰有一封发件人和主题标记均匹配的邮件时才读正文。去重记录保存在私有目录，
测试标记一旦绑定一封邮件就沿用该绑定，后续筛选会排除对这封邮件的关联回复。
处理回调接收 `(正文, 当前是否已获审批, 去重指纹)`，
正常返回后记为已交接。回调报错或进程在交接结果落盘前中断时，下一轮可能重试同一指纹。
下游有副作用时要按该指纹幂等处理。`poll_once` 返回本轮的 `PollResult`。
使用 `run_polling(..., on_result=回调)` 时，每轮成功轮询都会把 `PollResult` 交给该回调。
调用方通过 `PollResult.approved` 收到正文交接后才到达的审批结果。

审批从已发送文件夹读取产品负责人的关联回复。优先匹配 `In-Reply-To`，该字段缺失时
检查 `References` 最后一项。两个入口都接受可选的 `approval_judge`，它只接收作者本次回复的文字，
返回 `ApprovalDecision` 的四类结论之一。所有关联回复及其正文版本都判为 `APPROVE`（同意）
才算已获邮件审批。「带条件」的回复交人工处理。「不同意」「不确定」、纯文本引文边界不明、
判断器异常或未注入判断器时，审批结果均为未获审批。
试跑窗口和标记发放按 Trace #12 合同执行。调用方对外仅交接获准的测试邮件正文。
真实试跑验收时，编排者可在前后分别用 `qianshu.mail.imap.connect` 只读打开两个文件夹，
用 `seen()` 回读各自的已读 UID 集合并比对，只公开比对结果。

## X8 历史样例查找

编排者按合同在冻结候选两层审核通过后运行。凭据从 `--env-file` 指定的私有配置读取，
默认 `.env`。另外从进程环境提供 `MAIL_SENT_FOLDER` 和 `MAIL_HISTORY_SUBJECT`，
可选配置 `MAIL_PRIVATE_DIR`。选择范围用 IMAP 内部收件日期，包含起始日，截止日不含。
按 UID 从新到旧选择最多 10 封主题匹配的邮件，并查找其已发送关联回复。

```sh
python -m qianshu.mail.history --env-file /path/to/private.env --since 2000-01-01 --before 2000-02-01
```

输出位于私有目录下新建的 `mail-history-*/samples.json`，内含申请和回复的信头及文字正文。
导出前对两个文件夹各做一次读取前后的已读 UID 集合比对。命令成功时只输出申请和关联回复封数。
