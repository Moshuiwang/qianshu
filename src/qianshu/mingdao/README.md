# 明道云填表演练

X2 固定入口为 `python -m qianshu.mingdao.rehearsal`。编排者按 Trace #12 合同，在候选 SHA 对应的干净 worktree 上执行。开始前核对两层审核、裁定项 5 的登记与当次守望者就绪自检。现场用预先保存的 Chromium 登录状态和私有 UI 配置。

```bash
uv run --frozen --extra mingdao-check python -m qianshu.mingdao.rehearsal \
  --env-file .env --ui-config .private/ui-example.json .private/draft-example.json
```

每个位置参数对应一份 `PolicyDraft.to_dict()` 生成的 JSON。可以顺序传多份草稿。草稿所需字段未确定、拦截出错或行数证据不齐时退出码为 1，批次随即停止。表单加载不完整单独记在报告中。审批件附件按产品文档的待定规则留空。登录状态失效时由编排者核对，演练入口只使用保存的登录状态。

## 私有配置

`.env` 的 `MINGDAO_BASE_URL` 为 HTTPS 源。UI 配置为 JSON，定位器和标识从私有文件读取。相对文件路径以命令执行目录为基准。

- `storage_state` 为登录状态文件路径，缺省取 `--private-dir` 下的 `mingdao-state.json`。私有根目录缺省为 `.private`。
- `static_origins` 是经核对的 HTTPS 静态资源源列表，可省略。
- `views` 包含「新建」「待审批」「待配置」「配置完成」「生效政策」五项，每项包含独立的工作表视图 `path`、加载完成定位器 `ready` 和总行数定位器 `total`。示意路径为 `/app/example-app/example-sheet/example-view`。守望者需核对 `ready` 在当前视图的真实数据加载完成后才出现，空视图也须有可验证的完成状态。总数须定位到当前视图的总行数。
- `form.open` 定位新建入口，`form.root` 定位新建表单，`form.errors` 定位当前可见的验证提示。 `form.write_controls` 填写保存、提交申请、保留本次提交并继续创建及其他写控件的 CSS 选择器。只配置这些控件本身或其内层节点。
- `form.fields` 以草稿字段名为键，包含从 `policy_name` 到 `effective_multiplier` 的 13 项主表字段，以及 `user_scope` 与 `approval_url`。每个控件配置包含 `control` 和 `kind`。`fill` 填输入框，`select` 选原生下拉选项，`picker` 通过 `panel` 和 `option` 按完整名称选择，可配置 `search` 搜索框。用户范围、任务审批链接另配 `tab`。
- `form.tags` 使用相同的控件配置，每个标签单独选取。`form.packages` 包含行定位器 `rows`、只在表单内新增空白行的 `add`，以及 `current`、`gift` 两个行内控件配置。
- `form.auto_fields` 以报告字段名为键，每项以 `control` 读可见值，可配置 `tab`。重复节点按页面顺序读取，例如节目包价格。条件性字段可配置 `required: false`。页面有明确的计算完成标志时，用 `form.observations_ready` 等待该标志。

## 拦截与证据

X3 和整合入口复用 `qianshu.mingdao.readonly.readonly_context`，在创建任何页面前对整个浏览器上下文装载拦截。首版只放行配置视图文档的 GET，以及匹配静态资源类型、路径和源的 GET。重定向在首跳截断，Service Worker、WebSocket 和下载均关闭。放行的 GET 无副作用是按网页惯例作出的假设，尚未逐个证实。新增非 GET 放行条目须由守望者逐条审查目标、操作、参数及无副作用依据，写入代码并经审核合并后生效。

每条草稿生成权限为 0700 的私有目录。 `requests.jsonl` 逐条记录请求方法、路径类别及拦截结果。未列入白名单的地址按去掉查询参数的路径生成指纹，同一路径可归并。 `report.json` 保存前后总行数和差值、自动带出的字段、表单报错、观察缺口与请求计数。冒烟通过需要两次各读齐五个视图、总行数分别相等，以及拦截和写控件检查通过。终端摘要只含计数和报告编号，Issue 只回填计数。自动带出字段和报错回写产品文档时只写核实后的字段行为和经脱敏的规则结论。
