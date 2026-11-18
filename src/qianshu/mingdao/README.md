# 明道云填表演练

**总原则：演练流程中任何一步没有确证执行，冒烟就判失败。**行数前后一致只是辅证，不能替代拦截层与各步执行证据。

X2 固定入口为 `python -m qianshu.mingdao.rehearsal`。编排者按 Trace #12 合同，在候选 SHA 对应的干净 worktree 上执行。开始前核对两层审核、裁定项 5 的登记与当次守望者就绪自检。现场用预先保存的 Chromium 登录状态和私有 UI 配置。

```bash
uv run --frozen --extra mingdao-check python -m qianshu.mingdao.rehearsal \
  --env-file .env --ui-config .private/ui-example.json .private/draft-example.json
```

每个位置参数对应一份 `PolicyDraft.to_dict()` 生成的 JSON。可以顺序传多份草稿。草稿所需字段未确定、拦截出错或冒烟证据不齐时退出码为 1，批次随即停止。五个视图的基线必须全部读齐，任一读取失败就停止，表单保持关闭。预载「新建」视图失败时冒烟判失败。表单打开并完成写控件检查后，个别控件的加载缺口单独记在报告中。审批件附件按产品文档的待定规则留空。登录状态失效时由编排者核对，演练入口只使用保存的登录状态。

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

X2、X3 和整合入口复用 `qianshu.mingdao.readonly.readonly_context`。第一层在创建任何页面前安装 `context.route`，默认拒绝，覆盖弹窗的首个请求。在线阶段只放行同时匹配配置源和路径的视图文档 GET，以及匹配静态资源类型、路径和源的 GET。重定向在首跳截断。带预取或预渲染标记的 `Sec-Purpose` 或 `Purpose` 请求直接中止。上下文禁用 Service Worker 注册和下载，所有 WebSocket 都在握手前关闭并记录。

X2 读齐五个基线行数后，在线预载「新建」视图。页面就绪后调用 `context.set_offline(True)`，且禁止路由通过 `route.fetch` 转发，此后才打开表单并填写。离线期间 Playwright 上报的网络尝试记为 `offline_attempt`，路由和脚本的拒绝仍另记 `blocked`。同一请求可以产生一条尝试记录和一条拒绝记录。联网取数型控件可能无法在离线状态加载，观察缺口记入报告。演练结束后受控关闭演练上下文里的页面及弹窗，销毁该上下文，再使用同一登录状态文件、同一拦截规则创建新的联网上下文，独立回读五个视图。

第二层在页面加载前注入脚本，移除解析或动态插入的 `script[type=speculationrules]`，拒绝并记录 `sendBeacon`、`fetchLater`、带 `keepalive` 的 fetch，以及非 GET 的 fetch 和 XHR。脚本在延后请求登记前拒绝 `fetchLater`。固定演练入口另以 `--disable-blink-features=FetchLaterAPI` 启动 Chromium，本机 Chromium 159.0.8154.6 已验证启动后该接口不可用。

上下文监听每个新页面，含应用弹窗。每页启用 CDP 的 DOMStorage 监听，顶层文档导航期间的脚本拒绝通过会话存储事件写入同一份清单。页面审计接收未就绪时观察到请求或拒绝，即判冒烟失败。弹窗首个导航若早于页面审计就绪，会被阻断并判失败。受控关闭会先在网页文档和空白帧屏蔽离开回调，再调用 `close(run_before_unload=False)`。拦截初始化、离线切换、拦截和审计回调以及受控关闭出错均使冒烟失败。

直接关页时，独立 CDP 拦截和离线模式均出现 POST 到达替身的轮次。正式流程保留脚本拦截与受控关闭来保护页面离开阶段，离线用于削减填表期间的网络请求，CDP 用于接收审计事件。若脚本未能拦下延后请求，仅切离线或屏蔽离开回调无法消除浏览器已登记的请求。弹窗在审计接收建立前意外销毁时，旧文档事件仍可能丢失。Web Worker 和浏览器自发上报（例如 Reporting API、Attribution Reporting）均未验证。放行的 GET 无副作用是按网页惯例作出的假设，尚未逐个证实。新增非 GET 放行条目须由守望者逐条审查目标、操作、参数及无副作用依据，写入代码并经审核合并后生效。

每条草稿生成权限为 0700 的私有目录。 `requests.jsonl` 逐条记录请求方法、路径类别及拦截结果。未列入白名单的地址按去掉查询参数的路径生成指纹，同一路径可归并。 `report.json` 保存前后总行数和差值、自动带出的字段、表单报错、观察缺口与请求计数。`evidence` 提供「离线切换成功」(`offline_switched`)、「表单已打开」(`form_opened`) 和「写控件检查已执行」(`write_controls_checked`) 三项证据，最后一项在表单可见且通过写控件配置检查后记为真。`report.json` 和终端摘要均输出两个判定。

- `smoke_passed`（安全）要求三项证据齐备、五个视图前后各自读齐且行数相等、白名单外请求均被拦截，以及审计和安全检查均通过。满足这些条件时，表单控件加载或填写不完整只记入报告。先跑 1 条演练确认无写入，按此判定放行。
- `rehearsal_valid`（演练有效）要求 `smoke_passed` 和 `form_complete` 均为真，即安全证据齐备，且所有必填项均已填写完成、过程无中断。中途填写超时或必填控件未出现时，该判定为假。正式 6 条演练逐条只以此判定为准。

终端摘要还报告计数和报告编号，Issue 只回填计数。自动带出字段和报错回写产品文档时只写核实后的字段行为和经脱敏的规则结论。

## 本机浏览器回归

Chromium 和 Playwright Python 已安装时，用以下命令运行真实浏览器回归。测试只访问本机 HTTPS 替身。

```bash
UV_CACHE_DIR=$PWD/.uv-cache uv run --frozen --offline python -m pytest -q -s -rs tests/mingdao/test_unload_browser.py
```

Playwright 1.65.2、Chromium 159.0.8154.6 的一次直接关页对照。每轮用同源见证页确认两个离开事件都执行过，四列请求数均按替身实际收到的 POST 计。

| 直接关页防线 | 无 POST 的轮次 | pagehide beacon | pagehide keepalive | visibilitychange beacon | visibilitychange keepalive |
| --- | ---: | ---: | ---: | ---: | ---: |
| Playwright 上下文路由 + 独立 CDP `Fetch.enable` | 0/10 | 10 | 10 | 10 | 10 |
| 仅 `set_offline(True)` | 4/10 | 0 | 0 | 2 | 6 |

该次独立 CDP 对照收到了 5 个 POST 暂停事件，5 次 `failRequest` 均在关页时失效。离线对照中，关页前的普通 GET 在 10 轮均未到达替身。关页存在竞态，重复测量的漏过数可能变化。

| 调试管道单层对照（各 10 轮，每轮尝试 4 个 POST） | 替身收到 | 清单记录的 POST 拒绝 |
| --- | ---: | ---: |
| 无防线，同页导航／直接关页 | 各 40 | 各 0 |
| 仅 CDP，同页导航 | 0 | 40 |
| 仅 CDP，直接关页 | 34 | 6 |
| 仅脚本，同页导航 | 0 | 40 |
| 仅脚本，直接关页 | 0 | 0，关页时审计事件可能丢失 |
| 脚本 + 受控关闭 | 0 | 0，离开回调未执行 |

上下文回归还验证了弹窗首个 POST 拒绝并记录，Service Worker 注册与控制均为 0，WebSocket 握手与帧均为 0 且有拒绝记录。四种预取或预渲染请求头、浏览器发出的链接预取均被拒绝。解析与动态插入的预取规则节点均被移除，替身未收到规则指向的请求。离线时，原本在白名单内的静态资源 GET 产生尝试和拒绝记录，替身收到 0 条。

在线和离线各调用一次 `fetchLater` 后受控关闭，两例替身各收到 0 条 POST，清单各有 1 条拒绝。应用弹窗正常导航时的 `pagehide` 调用 `sendBeacon`，替身收到 0 条 POST，清单恰有 1 条拒绝。普通链接导航的 `<a ping>` POST 由上下文路由拒绝并记录。当前 Chromium 未暴露早期接口 `PendingBeacon`、`PendingGetBeacon` 或 `PendingPostBeacon`。
