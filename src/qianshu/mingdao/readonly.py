"""X2、X3 与整合入口共用的浏览器请求拦截。"""

import hashlib
import json
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TextIO
from urllib.parse import urlsplit

# 静态资源同时匹配路径扩展名和浏览器申报的资源类型。
_ASSETS = {
    "script": re.compile(r"/[a-zA-Z0-9_./@+-]+\.(?:js|mjs)"),
    "stylesheet": re.compile(r"/[a-zA-Z0-9_./@+-]+\.css"),
    "image": re.compile(r"/[a-zA-Z0-9_./@+-]+\.(?:png|jpe?g|gif|svg|webp|ico|avif)"),
    "font": re.compile(r"/[a-zA-Z0-9_./@+-]+\.(?:woff2?|ttf|otf)"),
}
_SCRIPT = Path(__file__).with_name("readonly.js")
_SCRIPT_BINDING = "__qianshuReadonlyBlocked"
_SCRIPT_UNLOAD_KEY = "__qianshuReadonlyUnload"
_SCRIPT_READY = """
Object.defineProperty(window, "__qianshuReadonlyAuditReady", {value: true});
"""
_ARM_CLOSE = "() => window.__qianshuReadonlyClose?.() === true"
_DISABLE_FETCH_LATER = "--disable-blink-features=FetchLaterAPI"
_CLOSE_SCHEMES = ("http://", "https://", "about:blank", "about:srcdoc", "blob:", "data:")
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"})
_SPECULATIVE = re.compile(r"(?:^|[;,\s])(?:prefetch|prerender)(?=$|[;,\s])", re.IGNORECASE)


def https_origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("明道云浏览器只接受 HTTPS 地址")
    port = parts.port
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"


def _origin_setting(url: str) -> str:
    origin = https_origin(url)
    parts = urlsplit(url)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("源配置只接受 HTTPS 源")
    return origin


@dataclass(frozen=True, repr=False)
class ReadOnlyPolicy:
    """配置限定部署目标，代码限定可通行的方法和路径类别。"""

    app_origin: str
    document_paths: tuple[str, ...]
    static_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "app_origin", _origin_setting(self.app_origin))
        object.__setattr__(self, "document_paths", tuple(self.document_paths))
        object.__setattr__(self, "static_origins", tuple(map(_origin_setting, self.static_origins)))
        segment = r"[A-Za-z0-9_-]+"
        document_pattern = re.compile(
            rf"/app/{segment}/(?:"
            rf"{segment}(?:/{segment})?"
            rf"|newrecord/{segment}/{segment}/?"
            rf"|{segment}/{segment}/row/{segment}"
            r")"
        )
        if not self.document_paths or any(
            not document_pattern.fullmatch(path) for path in self.document_paths
        ):
            raise ValueError("文档路径须为应用下的工作表、视图、新建记录页或记录详情页路径")

    def classify(self, method: str, url: str, resource: str) -> tuple[bool, str]:
        try:
            origin = https_origin(url)
        except ValueError:
            return False, "invalid_url"
        path = urlsplit(url).path
        if origin == self.app_origin and path in self.document_paths:
            return method == "GET" and resource == "document", "worksheet_view"
        asset_pattern = _ASSETS.get(resource)
        if (
            (origin == self.app_origin or origin in self.static_origins)
            and asset_pattern
            and asset_pattern.fullmatch(path)
            and not {".", ".."}.intersection(path.split("/"))
        ):
            return method == "GET", f"static_{resource}"
        # 对未知端点按不含查询的路径归类，并用指纹保留去重能力。
        fingerprint = hashlib.sha256(path.encode("utf-8")).hexdigest()
        return False, f"unlisted_path:{fingerprint}"


class RequestAudit:
    def __init__(self, stream: TextIO, policy: ReadOnlyPolicy) -> None:
        self._stream = stream
        self._policy = policy
        self.counts: Counter[str] = Counter()
        self.failed = False
        self.offline = False
        self.active_fetches = 0

    def _record(self, method: str, pattern: str, outcome: str) -> None:
        self._stream.write(json.dumps({
            "method": method, "path_pattern": pattern, "outcome": outcome,
        }) + "\n")
        self._stream.flush()
        self.counts[outcome] += 1

    def _error(self, method: str, pattern: str) -> None:
        self.failed = True
        try:
            self._record(method, pattern, "error")
        except Exception:
            # 清单写入失败本身也使冒烟失败，拦截回调仍须收尾。
            pass

    def script(self, _source, method=None, url=None, *extra) -> None:
        try:
            if extra or not isinstance(method, str) or not isinstance(url, str):
                raise ValueError("脚本拦截未就绪")
            method = method if method in _METHODS else "OTHER"
            _, pattern = self._policy.classify(method, url, "other")
            self._record(method, pattern, "blocked")
        except Exception:
            self._error("OTHER", "script")

    def script_unload(self, event: dict) -> None:
        try:
            if event.get("key") == _SCRIPT_UNLOAD_KEY:
                self.script(None, *json.loads(event["newValue"]))
        except Exception:
            self._error("OTHER", "script")

    def offline_request(self, request) -> None:
        if not self.offline:
            return
        try:
            method = request.method if request.method in _METHODS else "OTHER"
            _, pattern = self._policy.classify(method, request.url, request.resource_type)
            self._record(method, pattern, "offline_attempt")
        except Exception:
            self._error("OTHER", "request_observer")

    def http(self, route) -> None:
        method = "OTHER"
        pattern = "unknown"
        try:
            request = route.request
            method = request.method if request.method in _METHODS else "OTHER"
            allowed, pattern = self._policy.classify(method, request.url, request.resource_type)
            headers = request.all_headers()
            if any(_SPECULATIVE.search(headers.get(name, "")) for name in ("sec-purpose", "purpose")):
                allowed, pattern = False, "speculative"
            if not allowed or self.failed or self.offline:
                route.abort()
                self._record(method, pattern, "blocked")
                return
            # 单跳取响应，拦截重定向，避免后续跳转脱离上下文路由。
            self._record(method, pattern, "allowed")
            self.active_fetches += 1
            try:
                response = route.fetch(max_redirects=0, max_retries=0, timeout=15_000)
            finally:
                self.active_fetches -= 1
            try:
                if 300 <= response.status < 400 and response.status != 304:
                    route.abort()
                    self._record(method, "redirect", "blocked")
                elif self.offline or self.failed:
                    route.abort()
                    self._record(method, pattern, "blocked")
                else:
                    route.fulfill(response=response)
            finally:
                response.dispose()
        except Exception:
            self._error(method, pattern)
            try:
                route.abort()
            except Exception:
                pass

    def websocket(self, route) -> None:
        try:
            route.close()
            self._record("WEBSOCKET", "websocket", "blocked")
        except Exception:
            self._error("WEBSOCKET", "websocket")


class ReadOnlyContext:
    def __init__(self, context, audit: RequestAudit) -> None:
        self._context = context
        self._audit = audit
        self._ready_pages = {}

    def install(self) -> None:
        self._context.on("page", self._prepare_page)
        self._context.route_web_socket(re.compile(r".*"), lambda route: self._websocket(None, route))
        self._context.route(re.compile(r".*"), self._audit.http)
        self._context.on("request", self._request)
        self._context.expose_binding(_SCRIPT_BINDING, self._script)
        self._context.add_init_script(path=_SCRIPT)
        self._context.set_default_timeout(8_000)

    def _check_ready(self, page) -> None:
        if page not in self._ready_pages:
            self._audit._error("OTHER", "page_audit_not_ready")

    def _request(self, request) -> None:
        try:
            page = request.frame.page
        except Exception:
            # 弹窗的首个导航请求可能早于 Playwright 创建其 Frame。
            page = None
        self._check_ready(page)
        self._audit.offline_request(request)

    def _script(self, source, *args) -> None:
        try:
            self._check_ready(source["page"])
            self._audit.script(source, *args)
        except Exception:
            self._audit._error("OTHER", "script")

    def _script_unload(self, page, event) -> None:
        if event.get("key") == _SCRIPT_UNLOAD_KEY:
            self._check_ready(page)
            self._audit.script_unload(event)

    def _websocket(self, page, route) -> None:
        self._check_ready(page)
        self._audit.websocket(route)

    def _prepare_page(self, page) -> None:
        try:
            page.on("close", lambda _: self._ready_pages.pop(page, None))
            session = self._context.new_cdp_session(page)
            session.on("DOMStorage.domStorageItemAdded", lambda event: self._script_unload(page, event))
            session.send("DOMStorage.enable")
            session.send("Page.enable")
            page.route_web_socket(re.compile(r".*"), lambda route: self._websocket(page, route))
            session.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": _SCRIPT_READY, "runImmediately": True,
            })
            if page.is_closed():
                raise RuntimeError("页面在审计就绪前关闭")
            self._ready_pages[page] = session
        except Exception:
            self._audit._error("OTHER", "cdp_setup")

    def go_offline(self) -> None:
        if self._audit.failed:
            raise RuntimeError("请求拦截已失败")
        # route.fetch 通过 APIRequestContext 转发，不受浏览器离线状态约束。
        self._audit.offline = True
        try:
            self._context.set_offline(True)
            if self._audit.active_fetches:
                raise RuntimeError("仍有在线读取尚未结束")
        except Exception:
            self._audit._error("OTHER", "offline_setup")
            raise

    def new_page(self):
        if self._audit.failed:
            raise RuntimeError("请求拦截已失败")
        page = self._context.new_page()
        # Playwright 可在 page 监听仍等待 CDP 应答时返回 new_page。
        deadline = monotonic() + 8
        while page not in self._ready_pages and not self._audit.failed and monotonic() < deadline:
            page.wait_for_timeout(10)
        if page not in self._ready_pages or self._audit.failed:
            if not self._audit.failed:
                self._check_ready(page)
            raise RuntimeError("页面审计未就绪")
        return page

    def close_page(self, page) -> None:
        if not self._audit.failed:
            self._check_ready(page)
        try:
            for frame in page.frames:
                if not self._arm_frame(frame):
                    raise RuntimeError("关闭前未能启用脚本拦截")
        except Exception:
            self._audit._error("OTHER", "script_close")
            raise
        finally:
            try:
                page.close(run_before_unload=False)
            except Exception:
                self._audit._error("OTHER", "page_close")
                raise

    @staticmethod
    def _arm_frame(frame) -> bool:
        deadline = monotonic() + 1
        while frame.url.startswith(_CLOSE_SCHEMES):
            try:
                return frame.evaluate(_ARM_CLOSE)
            except Exception as exc:
                # 被拒绝的导航可能正在从空白页切换到 Chromium 错误页。
                if "Execution context was destroyed" not in str(exc) or monotonic() >= deadline:
                    raise
        return True

    def close_pages(self) -> None:
        # 包括页面自行创建的弹窗；关闭失败由外层关闭整个上下文并判失败。
        while pages := self._context.pages:
            self.close_page(pages[0])


@contextmanager
def readonly_context(browser, policy: ReadOnlyPolicy, state: Path, audit_stream: TextIO):
    """先安装整个上下文的拦截，再提供能打开页面的上下文。"""
    audit = RequestAudit(audit_stream, policy)
    try:
        context = browser.new_context(
            storage_state=str(state), service_workers="block", accept_downloads=False,
            locale="zh-CN", viewport={"width": 1280, "height": 900},
        )
    except Exception:
        audit._error("OTHER", "context_setup")
        raise
    try:
        guarded = ReadOnlyContext(context, audit)
        try:
            guarded.install()
        except Exception:
            audit._error("OTHER", "guard_setup")
            raise
        try:
            yield guarded, audit
        finally:
            guarded.close_pages()
    finally:
        try:
            context.close()
        except Exception:
            audit._error("OTHER", "context_close")
            raise
