"""明道云登录检查：用 .env 里的账号登录，打开「优惠政策申请」表确认可见，只读不改。

用法（首次需安装浏览器内核，约 150MB）：
    uv run --with playwright python -m playwright install chromium
    uv run --with playwright python scripts/check_mingdao_login.py          # 无界面
    uv run --with playwright python scripts/check_mingdao_login.py --show   # 弹出浏览器，遇到验证码可手动处理（需要有显示器，服务器上不可用）

登录状态保存到 .private/mingdao-state.json（已被 git 忽略），之后的建单程序复用它。
登录失败时截图保存到 .private/mingdao-login-fail.png，便于判断原因。
只尝试一次，不自动重试（避免多次失败触发账号锁定）。不输出账号、密码或任何 Cookie。
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
        # 页面右上角的语言选择也是文本框，所以账号框取「密码框之前最近的输入框」，不取第一个文本框
        password = page.locator("input[type=password]").first
        password.wait_for(timeout=30_000)
        password.locator("xpath=preceding::input[not(@type='checkbox') and not(@type='hidden')][1]").fill(
            env["MINGDAO_ACCOUNT"])
        password.fill(env["MINGDAO_PASSWORD"])
        # 页面标题也叫「登录」，按钮是 .btnForLogin；找不到按钮时在密码框按回车
        button = page.locator(".btnForLogin")
        if button.count():
            button.first.click()
        else:
            password.press("Enter")
        try:
            page.wait_for_url(lambda u: "/login" not in u, timeout=(120_000 if show else 30_000))
        except PwTimeout:
            fail_shot = STATE.parent / "mingdao-login-fail.png"
            page.screenshot(path=str(fail_shot))
            fail_shot.chmod(0o600)
            # 登录页本身就有「使用验证码登录」字样，判断前先去掉
            body = page.inner_text("body").replace("使用验证码登录", "")
            captcha = any("captcha" in (f.url or "").lower() for f in page.frames) \
                or re.search(r"安全验证|拖动|滑块|图形验证|短信验证|新设备|captcha", body, re.I)
            hint = "页面要求验证码或安全验证" if captcha else "仍停留在登录页，请看截图判断（账号密码错误或页面结构变化）"
            print(f"[失败] 登录：{hint}；截图在 .private/mingdao-login-fail.png")
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
