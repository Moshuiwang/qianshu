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

import pytest

from qianshu.mingdao.readonly import _ARM_CLOSE, _SCRIPT, _SCRIPT_BINDING, ReadOnlyPolicy, RequestAudit

TRIALS = 10
EVENTS = ("pagehide", "visibilitychange")
APIS = ("beacon", "keepalive")
WRITE_PATHS = {f"/example-write/{event}/{api}" for event in EVENTS for api in APIS}


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
    page_path = "/app/example-app/example-sheet/example-view"
    done_path = page_path + "-end"
    posts = set()
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
        def reply(self, status, content=b""):
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            path = self.path.partition("?")[0]
            if path == page_path and self.path.endswith("?redirect=1"):
                self.send_response(307)
                self.send_header("Location", done_path)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == page_path:
                self.reply(200, f"<script>{script}</script>本机替身".encode())
            elif path == done_path:
                self.reply(200, b"done")
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
            yield f"https://127.0.0.1:{httpd.server_port}", page_path, done_path, posts
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
    origin, page_path, done_path, posts = server
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
    origin, page_path, _, posts = server
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
