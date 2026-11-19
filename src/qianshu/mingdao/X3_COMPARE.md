# X3 官方接口只读比对

X3 将解析器的 6 条原始政策草稿与人工录入的 6 条明道云记录逐字段比对。使用解析器输出的 `PolicyDraft.to_dict()` JSON 数组。人工覆盖会改变归因，输入应使用覆盖前的草稿。

真机读取由编排者按 Trace #12 的当次授权与就绪自检执行。本卡验证使用 HTTP 模拟传输和合成数据。

```sh
python -m qianshu.mingdao.x3_compare \
  --config .private/占位配置.json \
  --drafts .private/占位草稿.json \
  --output .private/占位报告.json \
  --env-file .env
```

输入配置、草稿和报告存于已确认被 Git 忽略且未跟踪的私有目录，配置和草稿文件权限为 `0600`。`--env-file` 默认取当前目录的 `.env`，凭据仅从该文件取值。配置登记个人令牌与应用 ID 的键名，读取时分别用 `Authorization: Bearer …` 和 `HAP-Appid`。

## 私有配置

以下结构中的标识均为占位。为六条记录填写各自的 `rowId`（UUID）。草稿序号从 1 开始，六个序号和六个 UUID 各出现一次。支持按任意顺序登记配对。

```json
{
  "official": {
    "origin": "https://placeholder.invalid",
    "token_key": "PLACEHOLDER_HAP_TOKEN",
    "app_id_key": "PLACEHOLDER_HAP_APP_ID"
  },
  "worksheet_id": "placeholder_worksheet",
  "records": [
    {"rowId": "占位记录UUID_1", "draft": 1},
    {"rowId": "占位记录UUID_2", "draft": 2},
    {"rowId": "占位记录UUID_3", "draft": 3},
    {"rowId": "占位记录UUID_4", "draft": 4},
    {"rowId": "占位记录UUID_5", "draft": 5},
    {"rowId": "占位记录UUID_6", "draft": 6}
  ],
  "fields": {
    "placeholder_text_control": {"label": "政策名称", "type": 2, "draft": "policy_name"},
    "placeholder_number_control": {"label": "赠送天数", "type": 6, "draft": "gift_days"},
    "placeholder_date_control": {"label": "开始日期", "type": 15, "draft": "start_date"},
    "placeholder_select_control": {
      "label": "是否只触发一次",
      "type": 11,
      "draft": "trigger_once",
      "status_map": {"placeholder_no": "否", "占位否选项": "否"}
    },
    "placeholder_tags_control": {
      "label": "用户标签",
      "type": 29,
      "draft": "user_tags",
      "relations": {"name_path": ["placeholder_tag_name_control"]}
    },
    "placeholder_packages_control": {
      "label": "节目包",
      "type": 34,
      "draft": "packages",
      "relations": {
        "current": {"control": "placeholder_current_control", "name_path": [0, "name"]},
        "gift": {"control": "placeholder_gift_control", "name_path": [0, "name"]}
      }
    }
  }
}
```

| 类型 | 比较口径 |
| --- | --- |
| `text`（`2`） | 去首尾空白后比较 |
| `number`（`6`、`8`） | 数值比较，支持数字和数字字符串 |
| `date`（`15`、`16`） | 比较日期部分 `YYYY-MM-DD`，保留时间戳所带的日历日期 |
| `single_select`（`9`、`11`） | 草稿可使用映射的标准值或已登记的别名。人工选项的 key 与显示文本各自查 `status_map`。至少命中一项，两项均命中时须指向同一标准值 |
| `user_tags`（`29`） | 逐行提取标签名称后忽略大小写，首尾去空白，空格与下划线等价。忽略行顺序，保留重复项 |
| `packages`（`34`） | 每行按当前包与赠送包配对，按标签规则归一化。忽略行顺序，保留重复行。允许明确留空的赠送包 |
| `unsupported` | 可呈现人工值和草稿值，两边有值时记「无法比较」。其他正整数类型也按此处理 |

`name_path` 从标签行或节目包单元格开始取值。字符串为对象的键，非负整数为数组下标，空数组表示直接使用当前值。节目包的 `current` 和 `gift` 分别指定关系行中的子控件 ID，支持单元格内 JSON 数组的取值。

## 读取与报告

- 每条记录先请求一次 `GET /v3/app/worksheets/{worksheet_id}/rows/{rowId}`。详情须返回 `success: true` 与平铺的 `data`，其中 `data.rowId` 须为 UUID，并与请求中的 `rowId` 忽略大小写后相同。非关联控件须存在，空值可显式返回。
- 用户标签和节目包各使用相应控件的 `GET …/rows/{rowId}/relations/{controlId}?pageIndex=1&pageSize=50`。接口须明确成功，`data.rows` 为对象数组，且整数 `data.total` 等于行数并小于 50。
- 任一请求失败、响应结构不符或详情身份不匹配，则该记录记「读取失败」，跳过该记录剩余请求并继续下一条。重定向按读取失败处理。
- 草稿缺值或带不确定标记时记「草稿不确定」。草稿确定且人工缺值时记「人工为空」。关联两边均为明确的空数组时记「一致」。双方有值但无法归一化时记「无法比较」，可比较值按相等与否分别记「一致」或「不一致」。
- `--output` 是 JSON 报告路径。文件名以 `.json` 结尾时，同目录 Markdown 使用同名的 `.md` 后缀；其他文件名加 `.md`。两条路径均须是新路径。报告以 `0600` 完整写入并排他发布。每个字段的一致率是「一致」条数除以 6，涵盖空值、未知和读取失败的记录。
- 终端只显示读取成功与失败条数，入口错误只显示失败计数。差异值只存私有报告，对群或 Issue 只转报计数和比例。
- 退出码 `0` 表示 6 条均读到，字段差异不影响退出码。退出码 `1` 表示存在读取失败或入口、输出失败。退出码 `2` 表示命令行参数格式无效。
