"""从正式入口运行替身浏览器，并独立记录请求是否发出、写按钮是否点击。"""

import json
from contextlib import contextmanager
from dataclasses import fields
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from qianshu.draft import DraftField, PackageRow, PolicyDraft
from qianshu.mingdao import rehearsal
from qianshu.mingdao.readonly import ReadOnlyPolicy, readonly_context

ORIGIN = "https://app.example.invalid"
STATIC = "https://assets.example.invalid"
SHEET = "/app/example-app/example-sheet"
SECRET = "example-private-value"


class FakeRoute:
    def __init__(self, browser, method, url, resource):
        self.browser = browser
        self.request = SimpleNamespace(method=method, url=url, resource_type=resource)
        self.terminal = None

    def fetch(self, *, max_redirects=20, max_retries=0, timeout=None):
        request = self.request
        self.browser.sent.append((request.method, request.url, request.resource_type))
        status = 307 if "redirect=1" in request.url else 200
        if status == 307 and max_redirects:
            self.browser.sent.append((request.method, "https://other.example.invalid/write", "document"))
        return SimpleNamespace(status=status, dispose=lambda: None)

    def abort(self):
        assert self.terminal is None
        self.terminal = "blocked"

    def fulfill(self, *, response):
        assert self.terminal is None
        self.terminal = "fulfilled"
        if 300 <= response.status < 400:
            self.browser.sent.append(("GET", "https://other.example.invalid/write", "document"))


class FakeBrowser:
    def __init__(self):
        self.sent = []
        self.actions = []
        self.options = {}
        self.changed_after_form = False
        self.unready = None
        self.routes = []

    def new_context(self, **options):
        self.options = options
        return FakeContext(self)


class FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self.handler = None
        self.socket_handler = None
        self.form_closed = False
        self.pages = []

    def route(self, pattern, handler):
        self.handler = (pattern, handler)

    def route_web_socket(self, pattern, handler):
        self.socket_handler = (pattern, handler)

    def set_default_timeout(self, timeout):
        pass

    def new_page(self):
        assert self.browser.options["service_workers"] == "block"
        page = FakePage(self, form=bool(self.pages))
        self.pages.append(page)
        return page

    def request(self, method, url, resource="xhr"):
        route = FakeRoute(self.browser, method, url, resource)
        self.browser.routes.append(route)
        if self.handler and self.handler[0].fullmatch(url):
            self.handler[1](route)
        if route.terminal is None:
            self.browser.sent.append((method, url, resource))
        return route.terminal != "blocked"

    def close(self):
        pass


class FakePage:
    def __init__(self, context, *, form):
        self.context = context
        self.form = form
        self.url = ""
        self.rows = 0

    def goto(self, url, *, wait_until):
        self.url = url
        if self.context.form_closed:
            self.context.browser.actions.append(("after_view", url))
        ok = self.context.request("GET", url, "document")
        return SimpleNamespace(ok=ok)

    def locator(self, selector):
        return FakeLocator(self, selector)

    def close(self, *, run_before_unload):
        assert self.form and run_before_unload is False
        self.context.browser.actions.append(("close_form",))
        self.context.form_closed = True


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def locator(self, selector):
        return self.page.locator(selector)

    def wait_for(self, *, state):
        assert state == "visible"
        if self.selector == self.browser.unready:
            raise TimeoutError(SECRET)

    @property
    def browser(self):
        return self.page.context.browser

    def inner_text(self):
        index = int(urlsplit(self.page.url).path.rsplit("-", 1)[-1])
        assert self.selector == f"#total-{index}"
        if self.page.context.form_closed and self.browser.changed_after_form and index == 3:
            return "共 12 行"
        return f"共 {index} 行"

    def count(self):
        assert self.selector == ".package-row"
        return self.page.rows

    def nth(self, index):
        assert index < self.page.rows
        return self.page.locator(f".package-row-{index}")

    def get_by_text(self, value, *, exact):
        assert exact
        return self.page.locator(value)

    def and_(self, other):
        assert self.selector == ".example-option"
        return other

    def fill(self, value):
        self.browser.actions.append(("fill", self.selector, value))

    def select_option(self, *, label):
        self.browser.actions.append(("select", self.selector, label))

    def click(self):
        self.browser.actions.append(("click", self.selector))
        if self.selector == "#open-example":
            context = self.page.context
            context.request("GET", STATIC + "/example.js", "script")
            context.request("POST", ORIGIN + f"/example/add-record?private={SECRET}")
            context.request("POST", ORIGIN + f"/example/unknown/{SECRET}", "xhr")
            context.request("GET", ORIGIN + f"/example/unknown/{SECRET}?value={SECRET}")
            context.request("GET", ORIGIN + "/example/unknown.js", "xhr")
        if self.selector == "#add-example":
            self.page.rows += 1

    def evaluate(self, script, forbidden):
        assert script == rehearsal._CLICK
        if self.selector in forbidden:
            return False
        self.click()
        return True

    def evaluate_all(self, script):
        assert script == rehearsal._READ_VALUES
        if self.selector == ".example-error":
            return ["合成报错"]
        if self.selector == ".example-calculated":
            return ["合成价格"]
        return []


def ui_config():
    def picker(control):
        return {
            "kind": "picker", "control": control, "panel": "#example-picker",
            "search": "#example-search", "option": ".example-option",
        }

    return {
        "views": {
            name: {"path": f"{SHEET}/example-view-{index}", "ready": f"#ready-{index}",
                   "total": f"#total-{index}"}
            for index, name in enumerate(rehearsal.VIEWS)
        },
        "static_origins": [STATIC],
        "form": {
            "open": "#open-example", "root": "#example-form", "errors": ".example-error",
            "write_controls": ["#example-save", "#example-submit", "#example-retain"],
            "fields": {
                **{name: {"control": f"#example-{name}", "kind": "fill"}
                   for name in rehearsal.MAIN_FIELDS},
                "company": picker("#example-company"),
                "policy_type": {"control": "#example-policy-type", "kind": "select"},
                "user_scope": {"tab": "#example-scope-tab", "control": "#example-scope", "kind": "fill"},
                "approval_url": {"tab": "#example-link-tab", "control": "#example-link", "kind": "fill"},
            },
            "tags": picker("#example-tags"),
            "packages": {
                "rows": ".package-row", "add": "#add-example",
                "current": picker("#example-current"), "gift": picker("#example-gift"),
            },
            "observations_ready": "#example-form-ready",
            "auto_fields": {"价格计划": {"control": ".example-calculated", "tab": "#example-price-tab"}},
        },
    }


@pytest.fixture
def draft():
    values = {field.name: DraftField(value="合成值") for field in fields(PolicyDraft)}
    values.update(
        user_tags=DraftField(value=["合成标签一", "合成标签二"]),
        packages=DraftField(value=[
            PackageRow(current_package="合成节目包一", gift_package="合成附属包"),
            PackageRow(current_package="合成节目包二"),
        ]),
        user_scope=DraftField(value="合成用户范围"),
        approval_url=DraftField(value="https://approval.example.invalid"),
        recharge_days=DraftField(value=0),
    )
    return PolicyDraft(**values)


@pytest.fixture
def browser(monkeypatch):
    fake = FakeBrowser()

    @contextmanager
    def launch():
        yield fake

    monkeypatch.setattr(rehearsal, "_launch_browser", launch)
    return fake


def run(tmp_path, draft, raw):
    config = rehearsal.RehearsalConfig.from_mapping(ORIGIN, raw, private_dir=tmp_path)
    return rehearsal.run_rehearsal(draft, config, private_dir=tmp_path)


def test_form_network_and_readbacks(tmp_path, draft, browser):
    result = run(tmp_path, draft, ui_config())
    assert result.smoke_passed and result.form_complete
    assert result.before == result.after == dict(zip(rehearsal.VIEWS, range(5), strict=True))
    assert result.auto_fields == {"价格计划": ["合成价格"]}
    assert result.form_errors == ["合成报错"]
    actions = browser.actions
    for action in (
        ("fill", "#example-recharge_days", "0"),
        ("select", "#example-policy-type", "合成值"),
        ("click", "合成标签一"), ("click", "合成标签二"),
        ("click", "合成节目包一"), ("click", "合成节目包二"), ("click", "合成附属包"),
        ("fill", "#example-scope", "合成用户范围"),
        ("fill", "#example-link", "https://approval.example.invalid"),
    ):
        assert action in actions
    assert actions.count(("click", "#add-example")) == 2
    assert all(action[0] == "after_view" for action in actions[actions.index(("close_form",)) + 1:])
    assert len([action for action in actions if action[0] == "after_view"]) == 5
    assert browser.options["storage_state"] == str(tmp_path / "mingdao-state.json")

    assert ("POST", ORIGIN + f"/example/add-record?private={SECRET}", "xhr") not in browser.sent
    assert ("POST", ORIGIN + f"/example/unknown/{SECRET}", "xhr") not in browser.sent
    assert all(method == "GET" and (SHEET in url or url == STATIC + "/example.js")
               for method, url, _ in browser.sent)
    assert result.requests == {"allowed": 12, "blocked": 4}

    audit_file = result.private_report.with_name("requests.jsonl")
    audit = audit_file.read_text()
    assert SECRET not in audit and ORIGIN not in audit and "/example/" not in audit
    records = [json.loads(line) for line in audit.splitlines()]
    assert {tuple(sorted(record)) for record in records} == {("method", "outcome", "path_pattern")}
    assert sum(record["method"] == "POST" and record["outcome"] == "blocked" for record in records) == 2
    assert audit_file.stat().st_mode & 0o777 == 0o600
    assert result.private_report.stat().st_mode & 0o777 == 0o600
    assert result.private_report.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("control", ("#example-save", "#example-submit", "#example-retain"))
def test_write_controls_never_clicked(tmp_path, draft, browser, control):
    raw = ui_config()
    raw["form"]["fields"]["approval_url"]["tab"] = control
    result = run(tmp_path, draft, raw)
    assert ("click", control) not in browser.actions
    assert not result.smoke_passed
    assert not result.form_complete
    assert result.issues["unsafe_control"] == "UnsafeControl"


def test_after_readback_change_fails_smoke(tmp_path, draft, browser):
    browser.changed_after_form = True
    result = run(tmp_path, draft, ui_config())
    assert result.before["配置完成"] == 3 and result.after["配置完成"] == 12
    assert not result.smoke_passed


def test_unready_view_cannot_pass_smoke_with_matching_partial_counts(tmp_path, draft, browser):
    browser.unready = "#ready-0"
    result = run(tmp_path, draft, ui_config())
    assert "新建" not in result.before and "新建" not in result.after
    assert not result.smoke_passed


def test_incomplete_form_is_recorded_without_failing_smoke(tmp_path, draft, browser):
    browser.unready = "#example-form-ready"
    result = run(tmp_path, draft, ui_config())
    assert result.smoke_passed
    assert not result.form_complete
    assert result.issues["form"] == "TimeoutError"
    assert result.form_errors == ["合成报错"]


def test_context_blocks_redirect_and_websocket():
    fake = FakeBrowser()
    path = SHEET + "/example-view-0"
    allowed_url = ORIGIN + path + "?redirect=1"
    with readonly_context(fake, ReadOnlyPolicy(ORIGIN, (path,)), Path("example.json"),
                          StringIO()) as (context, audit):
        assert not context.request("GET", allowed_url, "document")
        assert fake.sent == [("GET", allowed_url, "document")]
        pattern, handler = context.socket_handler
        events = []
        assert pattern.fullmatch("wss://example.invalid")
        handler(SimpleNamespace(close=lambda: events.append("closed")))
        assert events == ["closed"]
        assert audit.counts == {"allowed": 1, "blocked": 2}
        assert not audit.failed


def test_non_get_is_denied_on_an_allowed_address():
    path = SHEET + "/example-view-0"
    policy = ReadOnlyPolicy(ORIGIN, (path,), (STATIC,))
    for method in ("POST", "HEAD", "PUT", "PATCH", "OPTIONS", "DELETE"):
        assert not policy.classify(method, ORIGIN + path, "document")[0]
        assert not policy.classify(method, STATIC + "/example.js", "script")[0]
    assert not policy.classify("GET", STATIC + "/example.js", "xhr")[0]
    assert not policy.classify("GET", "https://other.example.invalid/example.js", "script")[0]
    assert not policy.classify("GET", ORIGIN + "/other/app/example.js/../example.js", "script")[0]


def test_fixed_cli_reports_counts_only(tmp_path, draft, browser, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(f"MINGDAO_BASE_URL={ORIGIN}\n")
    ui_file = tmp_path / "ui.json"
    ui_file.write_text(json.dumps(ui_config()))
    draft_file = tmp_path / "draft.json"
    draft_file.write_text(json.dumps(draft.to_dict()))
    result = rehearsal.main([
        "--env-file", str(env_file), "--private-dir", str(tmp_path),
        "--ui-config", str(ui_file), str(draft_file),
    ])
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert result == 0 and not captured.err
    assert summary["smoke_passed"] and summary["before"] == summary["after"]
    report = json.loads((tmp_path / summary["report_id"] / "report.json").read_text())
    assert report["auto_fields"] == {"价格计划": ["合成价格"]}
    assert report["form_errors"] == ["合成报错"]
    assert "合成价格" not in captured.out and "合成报错" not in captured.out
