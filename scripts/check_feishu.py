"""飞书连通检查：只读，不发消息、不改任何东西。

用法：uv run python scripts/check_feishu.py
读取仓库根目录的 .env；输出每一步的状态、错误码和计数。
"""

import json
import pathlib
import sys
import urllib.error
import urllib.request

from qianshu.config import ConfigError, load_config

BASE = "https://open.feishu.cn/open-apis"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def call(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except (ValueError, OSError):
            return {"code": e.code}
    except (OSError, ValueError):
        return {}


def report(name: str, resp: dict, detail: str = "") -> bool:
    ok = resp.get("code") == 0
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"：{detail}" if ok and detail else ""))
    if not ok and type(resp.get("code")) is int:
        print(f"        code={resp['code']}")
    return ok


def main() -> int:
    try:
        config = load_config("FEISHU_APP_ID", "FEISHU_APP_SECRET", env_file=ROOT / ".env")
    except ConfigError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    r = call("POST", "/auth/v3/tenant_access_token/internal",
             body={"app_id": config["FEISHU_APP_ID"], "app_secret": config["FEISHU_APP_SECRET"]})
    expire = r.get("expire")
    detail = f"有效期 {expire} 秒" if type(expire) is int else ""
    if not report("用 App ID / Secret 换取应用令牌", r, detail):
        return 1
    token = r["tenant_access_token"]

    r = call("GET", "/bot/v3/info", token)
    report("读取机器人信息", r)

    chat = config.get("FEISHU_CHAT_ID", "")
    if chat:
        r = call("GET", f"/im/v1/chats/{chat}", token)
        report("读取汇报群信息（需机器人已在群内）", r)
    else:
        print("[跳过] 读取汇报群信息：.env 未填 FEISHU_CHAT_ID")

    node = config.get("FEISHU_WIKI_CHECK_NODE", "")
    if node:
        r = call("GET", f"/wiki/v2/spaces/get_node?token={node}", token)
        n = (r.get("data") or {}).get("node") or {}
        if report("读取知识库存档页（需机器人是知识库成员）", r) \
                and n.get("obj_type") == "docx":
            r = call("GET", f"/docx/v1/documents/{n['obj_token']}/raw_content", token)
            text = (r.get("data") or {}).get("content") or ""
            report("读取存档页正文", r, f"{len(text)} 字，含「标签名称」{text.count('标签名称')} 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
