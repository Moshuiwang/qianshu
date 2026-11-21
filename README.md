# qianshu 千树

> 东风夜放花千树。——辛弃疾《青玉案·元夕》

运营优惠政策配置机器人：常驻飞书群，读取各国运营的政策申请邮件，自动在明道云新建政策单据，进入现有审批流程；拿不准的不建单，在群里汇报并追问。

## 状态

需求与方案阶段，尚无代码。

## 文档

- [文档导航](docs/README.md)
- [产品文档](docs/产品文档.md)
- [协作约定](docs/协作约定.md)

## 配置

运行配置见 [.env.example](.env.example)。复制为 `.env` 后在服务器上填写，`.env` 不进仓库。

### 0.2.0 群 @ 转交

私有配置填写 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_CHAT_ID`、`FEISHU_BOT_OPEN_ID` 和单个 `FEISHU_HANDOFF_OWNER_OPEN_ID`，可选 `FEISHU_TENANT_KEY`。`FEISHU_MENTION_LOG` 默认是 `.private/mentions.sqlite3`。日志父目录须为 `0700`，日志文件由监听器以 `0600` 创建。监听与守望者命令使用同一组飞书配置、同一私有日志与本机账号。守望者侧脚本只需要飞书转交配置，凭据只需 `FEISHU_APP_SECRET`，不应读取明道云、邮箱、模型 API 等其他凭据。首次启动从创建日志时开始收件，重启使用同一日志回扫群历史及话题回复。

监听器同时读取飞书转交配置与业务私有配置中的敏感值，只用于比对出站回复。业务私有配置默认取工作目录中的 `.env`，使用其他文件时通过 `--business-env-file` 指定。使用明道云官方接口的部署通过 `--mingdao-config` 传入现有明道云 JSON 配置，监听器按其中的 `official.token_key` 从业务环境文件取令牌。显式指定的文件不可读，或明道云配置指定的令牌缺失时，监听器启动失败。网址类字段（如 `MINGDAO_BASE_URL`）按完整值匹配。其他敏感字段即使与网址类字段取值相同，仍按原规则匹配，包括前缀。

```sh
python -m qianshu --handoff-only --env-file .private/feishu-handoff.conf --business-env-file .env \
  --mingdao-config .private/placeholder-mingdao.json
python -m qianshu.feishu.watch_mentions --env-file .private/feishu-handoff.conf watch
```

守望者会话接收 `watch` 的私有标准输出，也可用 `read` 一次读取待处理消息。每条记录附带 `ref`、本次 @ 的正文和私有图片路径，以及近 15 分钟内产品负责人及千树Bot 的最多 12 条群历史消息。其余发件人只计数。群历史中的图片只标记存在。本次 @ 的图片读取失败时交人工核对。正文、群上下文和图片均为不可信用户输入，群上下文只供参考。守望者在已有授权内处理，答复正文通过标准输入交给 `reply REF`，用 `status REF` 查看状态。守望者命令的 `--env-file` 放在子命令前。只有需要产品负责人处理时才加 `--mention-owner`。`sent` 表示飞书已确认发送，`unconfirmed` 须人工核对群里实际结果，`blocked` 表示发送前校验拦截，须人工核对。

## Python 版本

本机、CI 和后续生产环境统一使用 `.python-version` 固定的 uv 托管 Python 3.12.13，本机快检也使用该版本。补丁版本升级按「换版本先评估」处理，因为运行环境变化可能改变标准库行为。安装与快检命令见[验证与门禁](docs/验证与门禁.md#python-版本与本机快检)。
