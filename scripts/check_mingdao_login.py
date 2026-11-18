"""明道云登录检查：用 .env 里的账号登录，打开「优惠政策申请」表确认可见，只读不改。

用法（首次需安装浏览器内核，约 150MB）：
    uv run --with playwright python -m playwright install chromium
    uv run --with playwright python scripts/check_mingdao_login.py          # 无界面
    uv run --with playwright python scripts/check_mingdao_login.py --show   # 弹出浏览器，遇到验证码可手动处理

登录状态保存到 .private/mingdao-state.json（已被 git 忽略），之后的建单程序复用它。
不输出账号、密码或任何 Cookie。
"""

import pathlib
import re
import sys

from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / ".private" / "mingdao-state.json"


def load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> int:
    env = load_env()
    base = env.get("MINGDAO_BASE_URL", "https://www.mingdao.com")
    sheet_url = f"{base}/app/{env['MINGDAO_APP_ID']}/{env['MINGDAO_WORKSHEET_ID']}"
    show = "--show" in sys.argv
    STATE.parent.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not show)
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.locator("input[type=text], input[type=tel], input[type=email]").first.fill(env["MINGDAO_ACCOUNT"])
        page.locator("input[type=password]").first.fill(env["MINGDAO_PASSWORD"])
        page.get_by_text(re.compile(r"^\s*登\s*录\s*$")).first.click()
        try:
            page.wait_for_url(lambda u: "/login" not in u, timeout=(120_000 if show else 30_000))
        except PwTimeout:
            body = page.inner_text("body")
            hint = "页面要求验证码或安全验证，请加 --show 手动完成" if re.search("验证|captcha|滑块", body, re.I) \
                else "仍停留在登录页，请检查账号密码，或加 --show 查看"
            print(f"[失败] 登录：{hint}")
            browser.close()
            return 1
        print("[通过] 登录")
        ctx.storage_state(path=str(STATE))
        STATE.chmod(0o600)
        print("[通过] 已保存登录状态到 .private/（git 忽略）")

        page.goto(sheet_url, wait_until="domcontentloaded")
        try:
            page.get_by_text(re.compile(r"共\s*\d+\s*行")).first.wait_for(timeout=30_000)
            total = re.search(r"共\s*(\d+)\s*行", page.inner_text("body")).group(1)
            print(f"[通过] 打开「优惠政策申请」表：当前视图共 {total} 行")
        except PwTimeout:
            print("[失败] 打开「优惠政策申请」表：30 秒内没有看到记录列表")
            browser.close()
            return 1
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
