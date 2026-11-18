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
测试标记一旦绑定一封邮件就沿用该绑定。处理回调接收 `(正文, 当前是否已获审批, 去重指纹)`，
正常返回后记为已交接。回调报错或进程在交接结果落盘前中断时，下一轮可能重试同一指纹。
下游有副作用时要按该指纹幂等处理。每轮的 `PollResult.approved` 反映本轮回读的审批状态，
因此能在正文已交接后确认迟到的回复。

审批从已发送文件夹读取产品负责人的关联回复。优先匹配 `In-Reply-To`，该字段缺失时
检查 `References` 最后一项。接受「同意」「我同意该申请」等独立的明确同意语句，
允许行首带 `>` 的引用，额外措辞或多封关联回复意见不一致时保留为未获审批。
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
