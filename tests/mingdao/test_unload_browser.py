"""用本机 Chromium 和 HTTPS 替身验证文档离开时的拦截。"""

import hashlib
import json
import os
import select
import shutil
import ssl
import subprocess
from io import StringIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic
from types import SimpleNamespace

import pytest

from qianshu.mingdao.readonly import (
    _ARM_CLOSE, _SCRIPT, _SCRIPT_BINDING, ReadOnlyPolicy, RequestAudit, readonly_context,
)

TRIALS = 10
EVENTS = ("pagehide", "visibilitychange")
APIS = ("beacon", "keepalive")
WRITE_PATHS = {f"/example-write/{event}/{api}" for event in EVENTS for api in APIS}
LOCAL_ARGS = [
    "--disable-background-networking", "--no-proxy-server",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--disable-quic",
]


@pytest.fixture
def playwright_browser():
    sync_api = pytest.importorskip("playwright.sync_api", reason="需要 Playwright Python 可选依赖")
    with sync_api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("需要本机 Playwright Chromium")
        browser = playwright.chromium.launch(args=[*LOCAL_ARGS, "--ignore-certificate-errors"])
        try:
            yield browser
        finally:
            browser.close()


class Chromium:
    """通过调试管道驱动挂载的浏览器，测试无需下载驱动。"""

    def __init__(self, process):
        self.process = process
        self.buffer = b""
        self.counter = 0
        self.handlers = {}
        self.responses = {}

    def send(self, method, params, session=None):
        self.counter += 1
        command = {"id": self.counter, "method": method, "params": params}
        if session:
            command["sessionId"] = session
        self.process.stdin.write(json.dumps(command).encode() + b"\0")
        self.process.stdin.flush()
        return self.counter

    def receive(self, timeout):
        if b"\0" not in self.buffer:
            if not select.select([self.process.stdout], [], [], max(0, timeout))[0]:
                return False
            chunk = os.read(self.process.stdout.fileno(), 65536)
            assert chunk, "Chromium 调试管道已关闭"
            self.buffer += chunk
        if b"\0" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\0", 1)
            message = json.loads(raw)
            if "id" in message:
                self.responses[message["id"]] = message
            elif handler := self.handlers.get(message["method"]):
                handler(message["params"], message.get("sessionId"))
        return True

    def wait(self, predicate, seconds=5):
        deadline = monotonic() + seconds
        while not predicate() and monotonic() < deadline:
            self.receive(deadline - monotonic())
        return predicate()

    def call(self, method, params=None, session=None):
        command = self.send(method, params or {}, session)
        assert self.wait(lambda: command in self.responses), method
        response = self.responses.pop(command)
        assert "error" not in response, response.get("error")
        return response["result"]

    def drain(self, seconds):
        self.wait(lambda: False, seconds)


@pytest.fixture
def chromium(tmp_path):
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache/ms-playwright"))
    shells = sorted(cache.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    if not shells or not shutil.which("openssl"):
        pytest.skip("需要本机 Chromium headless shell 和 openssl")
    with subprocess.Popen(
        ["sh", "-c", 'exec "$@" 3<&0 4>&1', "chromium", str(shells[-1]),
         "--remote-debugging-pipe", f"--user-data-dir={tmp_path / 'profile'}",
         "--no-sandbox", "--disable-background-networking", "--no-proxy-server",
         "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
         "--ignore-certificate-errors", "--disable-quic"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        try:
            yield Chromium(process)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture
def server(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("本机 HTTPS 替身需要 openssl")
    page_path = "/app/example-app/example-sheet/example-view"
    done_path = page_path + "-end"
    posts = set()
    gets = []
    script = """
        for (const event of ["pagehide", "visibilitychange"]) {
            window.addEventListener(event, () => {
                if (event === "visibilitychange" && document.visibilityState !== "hidden") return;
                localStorage.setItem("example-listener-" + event + location.search, "ran");
                navigator.sendBeacon("/example-write/" + event + "/beacon" + location.search, "example");
                fetch("/example-write/" + event + "/keepalive" + location.search,
                      {method: "POST", keepalive: true, body: "example"}).catch(() => {});
            });
        }
    """

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, content=b"", content_type="text/html"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            gets.append(self.path)
            path = self.path.partition("?")[0]
            if path == page_path and self.path.endswith("?redirect=1"):
                self.send_response(307)
                self.send_header("Location", done_path)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == page_path and self.path.endswith("?speculation=1"):
                self.reply(200, b"""<script type="speculationrules">
                    {"prefetch":[{"urls":["/example-prefetch"]}],
                     "prerender":[{"urls":["/example-prerender"]}]}
                    </script>speculation example""")
            elif path == page_path:
                self.reply(200, f"<script>{script}</script>本机替身".encode())
            elif path == done_path:
                self.reply(200, b"done")
            elif path == "/example-worker.js":
                self.reply(200, b"self.addEventListener('fetch', event => "
                           b"event.respondWith(new Response('worker')))", "text/javascript")
            else:
                self.reply(404)

        def do_POST(self):
            posts.add(self.path)
            self.reply(204)

        def log_message(self, *args):
            pass

    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj",
         "/CN=127.0.0.1", "-days", "1", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as httpd:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
        thread = Thread(target=httpd.serve_forever)
        thread.start()
        try:
            yield f"https://127.0.0.1:{httpd.server_port}", page_path, done_path, posts, gets
        finally:
            httpd.shutdown()
            thread.join()


def open_target(chromium):
    target = chromium.call("Target.createTarget", {"url": "about:blank"})["targetId"]
    session = chromium.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    chromium.call("Page.enable", session=session)
    return target, session


def enable_cdp(chromium, session, audit):
    def intercept(params, owner):
        request = params["request"]
        method = request["method"]
        allowed, pattern = audit._policy.classify(method, request["url"], params["resourceType"].lower())
        args = {"requestId": params["requestId"]}
        try:
            if allowed:
                chromium.call("Fetch.continueRequest", args, owner)
            else:
                chromium.call("Fetch.failRequest", {**args, "errorReason": "BlockedByClient"}, owner)
            audit._record(method, pattern, "allowed" if allowed else "blocked")
        except AssertionError:
            audit._error(method, pattern)

    chromium.handlers["Fetch.requestPaused"] = intercept
    chromium.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session)


def enable_script(chromium, session, audit):
    def record(params, _owner):
        assert params["name"] == binding
        audit.script(None, *json.loads(params["payload"]))

    # Runtime binding 接收序列化参数，生产环境由 Playwright 的 expose_binding 转接。
    binding = "exampleReadonlyAudit"
    chromium.handlers["Runtime.bindingCalled"] = record
    chromium.handlers["DOMStorage.domStorageItemAdded"] = lambda params, _owner: audit.script_unload(params)
    chromium.call("DOMStorage.enable", session=session)
    chromium.call("Runtime.enable", session=session)
    chromium.call("Runtime.addBinding", {"name": binding}, session)
    chromium.call("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"window.{_SCRIPT_BINDING} = (...args) => window.{binding}(JSON.stringify(args));\n"
                  + _SCRIPT.read_text(encoding="utf-8"),
    }, session)


@pytest.mark.parametrize(("departure", "layer"), [
    ("navigate", "unprotected"), ("navigate", "cdp"), ("navigate", "script"),
    ("close", "unprotected"), ("close", "cdp"), ("close", "script"), ("close", "quiet"),
])
def test_unload(chromium, server, departure, layer):
    origin, page_path, done_path, posts, _ = server
    stream = StringIO()
    audit = RequestAudit(stream, ReadOnlyPolicy(origin, (page_path, done_path)))
    loaded = []
    chromium.handlers["Page.loadEventFired"] = lambda params, session: loaded.append(session)
    for trial in range(TRIALS):
        target, session = open_target(chromium)
        if layer == "cdp":
            enable_cdp(chromium, session, audit)
        if layer in {"script", "quiet"}:
            enable_script(chromium, session, audit)
        paths = [f"{page_path}?trial={trial}"]
        if departure == "navigate":
            paths.append(done_path)
        for path in paths:
            loaded.clear()
            result = chromium.call("Page.navigate", {"url": origin + path}, session)
            assert "errorText" not in result
            assert chromium.wait(lambda: session in loaded)
        if layer == "quiet":
            result = chromium.call("Runtime.evaluate", {
                "expression": f"({_ARM_CLOSE})()", "returnByValue": True,
            }, session)
            assert result["result"]["value"] is True
        if departure == "navigate":
            chromium.drain(0.15)
        chromium.call("Target.closeTarget", {"targetId": target})
        if departure == "close":
            chromium.drain(0.20)

    if departure == "close":
        _, session = open_target(chromium)
        result = chromium.call("Page.navigate", {"url": origin + done_path}, session)
        assert "errorText" not in result
        assert chromium.wait(lambda: session in loaded)
        result = chromium.call("Runtime.evaluate", {
            "expression": "Object.keys(localStorage)", "returnByValue": True,
        }, session)
        listeners = set(result["result"]["value"])
        if layer == "quiet":
            assert listeners == set()
        else:
            assert listeners == {
                f"example-listener-{event}?trial={trial}" for event in EVENTS for trial in range(TRIALS)
            }

    if (departure, layer) != ("close", "cdp"):
        assert not audit.failed
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    blocked = [entry["path_pattern"] for entry in records if entry["method"] == "POST"
               and entry["outcome"] == "blocked"]
    print(f"{departure}/{layer}：{TRIALS} 轮，拦截 {len(blocked)} 条，"
          f"拦截错误 {audit.counts['error']} 条，服务端收到 {len(posts)} 条")
    expected = {f"{path}?trial={trial}" for trial in range(TRIALS) for path in WRITE_PATHS}
    if layer == "unprotected":
        assert posts == expected
    elif (departure, layer) == ("close", "cdp"):
        # 能力探测。不同 Chromium 版本可能在目标销毁后给出不同结果。
        assert posts <= expected
    elif departure == "close":
        assert posts == set()
        if layer == "quiet":
            assert blocked == []
    else:
        assert posts == set()
        assert sorted(blocked) == sorted(
            "unlisted_path:" + hashlib.sha256(path.encode()).hexdigest()
            for path in WRITE_PATHS for _ in range(TRIALS)
        )


def test_script_blocks_non_get_fetch_and_xhr_and_keepalive_get(chromium, server):
    origin, page_path, _, posts, _ = server
    stream = StringIO()
    audit = RequestAudit(stream, ReadOnlyPolicy(origin, (page_path,)))
    _, session = open_target(chromium)
    enable_script(chromium, session, audit)
    result = chromium.call("Page.navigate", {"url": origin + page_path}, session)
    assert "errorText" not in result
    result = chromium.call("Runtime.evaluate", {"expression": """
        (async () => {
            for (const request of [
                new Request("/example-write/fetch", {method: "POST"}),
                new Request("/example-write/fetch", {keepalive: true}),
            ]) {
                try { await fetch(request); return false; }
                catch (error) { if (!(error instanceof TypeError)) throw error; }
            }
            const xhr = new XMLHttpRequest();
            xhr.open("POST", "/example-write/xhr");
            try { xhr.send(); return false; }
            catch (error) { return error.name === "SecurityError"; }
        })()
    """, "awaitPromise": True, "returnByValue": True}, session)
    assert result["result"]["value"] is True
    assert posts == set()
    assert not audit.failed
    assert [json.loads(line)["method"] for line in stream.getvalue().splitlines()] == ["POST", "GET", "POST"]


@pytest.mark.parametrize("layer", ["independent_cdp", "offline"])
def test_playwright_direct_close(playwright_browser, server, layer):
    from playwright.sync_api import Error

    origin, page_path, done_path, posts, gets = server
    audit = RequestAudit(StringIO(), ReadOnlyPolicy(origin, (page_path, done_path)))
    paused = failed = close_races = 0

    for trial in range(TRIALS):
        context = playwright_browser.new_context(ignore_https_errors=True)
        try:
            # 保留 Playwright 的路由，验证额外 CDP 会话与它并用时的表现。
            if layer == "independent_cdp":
                context.route("**/*", audit.http)
            witness = context.new_page()
            witness.goto(origin + done_path)
            page = context.new_page()
            page.goto(f"{origin}{page_path}?trial={trial}")
            if layer == "offline":
                context.set_offline(True)
                assert not page.evaluate("""async () => {
                    try { await fetch('/example-offline-check'); return true; }
                    catch { return false; }
                }""")
            else:
                session = context.new_cdp_session(page)

                def intercept(event):
                    nonlocal paused, failed, close_races
                    args = {"requestId": event["requestId"]}
                    if event["request"]["method"] == "GET":
                        session.send("Fetch.continueRequest", args)
                        return
                    paused += 1
                    try:
                        session.send("Fetch.failRequest", {**args, "errorReason": "BlockedByClient"})
                    except Error as exc:
                        if not any(reason in str(exc) for reason in (
                            "No resource with given identifier", "Invalid InterceptionId",
                            "Target page, context or browser has been closed",
                        )):
                            raise
                        close_races += 1
                    else:
                        failed += 1

                session.on("Fetch.requestPaused", intercept)
                session.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
            page.close(run_before_unload=False)
            witness.wait_for_function(
                "keys => keys.every(key => localStorage.getItem(key) === 'ran')",
                arg=[f"example-listener-{event}?trial={trial}" for event in EVENTS],
                timeout=2_000,
            )
            # 给已脱离标签页的请求留出到达本机替身的时间。
            witness.wait_for_timeout(200)
        finally:
            context.close()
    assert not audit.failed
    assert "/example-offline-check" not in gets
    expected = {f"{path}?trial={trial}" for path in WRITE_PATHS for trial in range(TRIALS)}
    assert posts <= expected
    protected = TRIALS - len({path.partition("?")[2] for path in posts})
    counts = [sum(path.partition("?")[0] == f"/example-write/{event}/{api}" for path in posts)
              for event in EVENTS for api in APIS]
    print(f"Playwright/{layer}：{protected}/{TRIALS} 轮无 POST，"
          f"四类到达数 {counts}，CDP 暂停 {paused} 条，拒绝成功 {failed} 条，"
          f"关页时失效 {close_races} 条")


@pytest.fixture
def guarded_context(playwright_browser, server, tmp_path):
    origin, page_path, _, _, _ = server
    state = tmp_path / "example-state.json"
    state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    stream = StringIO()
    # 本机 HTTPS 使用临时证书。route.fetch 所用的客户端也须接受它。
    browser = SimpleNamespace(new_context=lambda **options: playwright_browser.new_context(
        **options, ignore_https_errors=True,
    ))
    with readonly_context(browser, ReadOnlyPolicy(origin, (page_path,)), state, stream) as (context, audit):
        yield context, audit, stream
    assert not audit.failed


def test_popup_first_post_is_denied(guarded_context, server):
    origin, path, _, posts, _ = server
    context, audit, stream = guarded_context
    page = context.new_page()
    assert page.goto(origin + path).ok
    with page.expect_popup() as popup:
        page.evaluate("""() => {
            const form = document.createElement('form');
            form.target = '_blank';
            form.method = 'POST';
            form.action = location.href;
            document.body.append(form);
            form.submit();
        }""")
    assert popup.value is not page
    assert posts == set()
    assert audit.counts["blocked"] == 1
    assert {"method": "POST", "path_pattern": "worksheet_view", "outcome": "blocked"} in [
        json.loads(line) for line in stream.getvalue().splitlines()
    ]


def test_service_worker_cannot_register_or_control_page(guarded_context, server):
    origin, path, _, _, gets = server
    context, _, _ = guarded_context
    page = context.new_page()
    assert page.goto(origin + path).ok
    result = page.evaluate("""async () => {
        const sw = navigator.serviceWorker;
        const result = await Promise.race([
            sw.register('/example-worker.js').then(
                registration => registration ? 'registered' : 'blocked', () => 'rejected'),
            new Promise(resolve => setTimeout(() => resolve('pending'), 300)),
        ]);
        return [result, (await sw.getRegistrations()).length, sw.controller === null];
    }""")
    assert result[0] in {"blocked", "pending", "rejected"}
    assert result[1:] == [0, True]
    assert "/example-worker.js" not in gets


def test_websocket_is_denied_before_handshake(guarded_context, server):
    origin, path, _, _, gets = server
    context, audit, stream = guarded_context
    page = context.new_page()
    assert page.goto(origin + path).ok
    assert page.evaluate("""() => new Promise((resolve, reject) => {
        const socket = new WebSocket('wss://' + location.host + '/example-socket');
        socket.onopen = () => { socket.send('example'); reject('opened'); };
        socket.onclose = () => resolve(true);
        setTimeout(() => reject('did not close'), 2000);
    })""")
    # 浏览器收到关闭事件时，路由回调可能尚未从 close() 返回。
    page.wait_for_timeout(50)
    assert "/example-socket" not in gets
    assert audit.counts["blocked"] == 1
    assert {"method": "WEBSOCKET", "path_pattern": "websocket", "outcome": "blocked"} in [
        json.loads(line) for line in stream.getvalue().splitlines()
    ]


@pytest.mark.parametrize("headers", [
    {"Sec-Purpose": "prefetch"}, {"Sec-Purpose": "prerender"},
    {"Sec-Purpose": "prefetch;prerender"}, {"Purpose": "prefetch"},
])
def test_speculative_headers_override_document_allowlist(guarded_context, server, headers):
    from playwright.sync_api import Error

    origin, path, _, _, gets = server
    context, audit, stream = guarded_context
    page = context.new_page()
    # CDP 设置浏览器额外头，覆盖 Chromium 在预取和预渲染中发送的形式。
    session = page.context.new_cdp_session(page)
    session.send("Network.enable")
    session.send("Network.setExtraHTTPHeaders", {"headers": headers})
    with pytest.raises(Error, match="ERR_FAILED"):
        page.goto(origin + path)
    assert gets == []
    assert audit.counts["allowed"] == 0
    assert {"method": "GET", "path_pattern": "speculative", "outcome": "blocked"} in [
        json.loads(line) for line in stream.getvalue().splitlines()
    ]


def test_speculation_rules_removed_from_parsed_and_dynamic_dom(guarded_context, server):
    origin, path, _, _, gets = server
    context, _, _ = guarded_context
    page = context.new_page()
    assert page.goto(origin + path + "?speculation=1").ok
    rules = page.locator('script[type="speculationrules"]')
    assert rules.count() == 0
    assert page.evaluate("""async () => {
        const script = document.createElement('script');
        script.type = 'speculationrules';
        script.textContent = JSON.stringify({prefetch: [{urls: ['/example-dynamic-prefetch']}]});
        document.body.append(script);
        await Promise.resolve();
        return !script.isConnected;
    }""")
    page.wait_for_timeout(200)
    assert not any(path.startswith(("/example-prefetch", "/example-prerender", "/example-dynamic-prefetch"))
                   for path in gets)


def test_browser_prefetch_is_denied(guarded_context, server):
    origin, path, _, _, gets = server
    context, _, stream = guarded_context
    page = context.new_page()
    assert page.goto(origin + path).ok
    event = page.evaluate("""() => new Promise(resolve => {
        const link = document.createElement('link');
        link.rel = 'prefetch';
        link.href = location.pathname + '?prefetch=1';
        link.onload = () => resolve('load');
        link.onerror = () => resolve('error');
        document.head.append(link);
        setTimeout(() => resolve('timeout'), 2000);
    })""")
    assert event == "error"
    assert path + "?prefetch=1" not in gets
    assert {"method": "GET", "path_pattern": "speculative", "outcome": "blocked"} in [
        json.loads(line) for line in stream.getvalue().splitlines()
    ]


def test_offline_disables_route_fetch_and_logs_attempt(guarded_context, server):
    origin, path, _, _, gets = server
    context, audit, _ = guarded_context
    page = context.new_page()
    assert page.goto(origin + path).ok
    context.go_offline()
    assert not page.evaluate("""async () => {
        const script = document.createElement('script');
        script.src = '/example-worker.js';
        return await new Promise(resolve => {
            script.onload = () => resolve(true);
            script.onerror = () => resolve(false);
            document.body.append(script);
        });
    }""")
    assert "/example-worker.js" not in gets
    assert audit.counts["offline_attempt"] == audit.counts["blocked"] == 1
