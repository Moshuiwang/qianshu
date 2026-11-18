"""从正式入口运行替身浏览器，并独立记录请求是否发出、写按钮是否点击。"""

import json
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import fields
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from qianshu.draft import DraftField, PackageRow, PolicyDraft
from qianshu.mingdao import rehearsal
from qianshu.mingdao.readonly import _SCRIPT_UNLOAD_KEY, ReadOnlyContext, ReadOnlyPolicy, readonly_context

ORIGIN = "https://app.example.invalid"
STATIC = "https://assets.example.invalid"
SHEET = "/app/example-app/example-sheet"
SECRET = "example-private-value"


class FakeRoute:
    def __init__(self, browser, method, url, resource):
        self.browser = browser
        self.request = SimpleNamespace(method=method, url=url, resource_type=resource)
        self.request.all_headers = self.all_headers
        self.terminal = None

    def all_headers(self):
        if self.browser.route_error and self.request.resource_type == "script":
            raise RuntimeError(SECRET)
        return {}

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
        self.contexts = []
        self.form_closed = False
        self.changed_after_form = False
        self.unready = None
        self.routes = []
        self.blocked_read = None
        self.route_error = False
        self.cdp_error = False
        self.bad_cdp_event = False
        self.offline_error = False
        self.preload_unready = False

    def new_context(self, **options):
        self.options = options
        self.context = FakeContext(self)
        self.contexts.append(self.context)
        return self.context


class FakeSession:
    def __init__(self, browser):
        self.browser = browser

    def on(self, event, handler):
        assert event == "DOMStorage.domStorageItemAdded"
        self.handler = handler

    def send(self, command, params=None):
        assert command in {"DOMStorage.enable", "Page.enable", "Page.addScriptToEvaluateOnNewDocument"}
        if self.browser.cdp_error:
            raise RuntimeError(SECRET)


class FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self.handler = None
        self.socket_handler = None
        self.all_pages = []
        self.offline = False
        self.closed = False

    @property
    def pages(self):
        return [page for page in self.all_pages if not page.closed]

    def route(self, pattern, handler):
        self.handler = (pattern, handler)

    def route_web_socket(self, pattern, handler):
        self.socket_handler = (pattern, handler)

    def on(self, event, handler):
        if event == "request":
            self.request_handler = handler
        else:
            assert event == "page"
            self.page_handler = handler

    def set_offline(self, offline):
        assert offline
        self.offline = offline
        self.browser.actions.append(("offline",))
        if self.browser.offline_error:
            raise RuntimeError(SECRET)

    def set_default_timeout(self, timeout):
        pass

    def add_init_script(self, *, path):
        assert path.is_file()

    def expose_binding(self, name, handler):
        self.script_handler = handler

    def new_cdp_session(self, page):
        page.session = FakeSession(self.browser)
        return page.session

    def new_page(self):
        assert self.browser.options["service_workers"] == "block"
        page = FakePage(self)
        self.all_pages.append(page)
        self.page_handler(page)
        return page

    def request(self, method, url, resource="xhr"):
        route = FakeRoute(self.browser, method, url, resource)
        route.request.frame = SimpleNamespace(page=self.pages[-1] if self.pages else None)
        self.browser.routes.append(route)
        self.request_handler(route.request)
        if self.handler and self.handler[0].fullmatch(url):
            self.handler[1](route)
        if route.terminal is None:
            self.browser.sent.append((method, url, resource))
        return route.terminal != "blocked"

    def close(self):
        assert not self.pages
        self.closed = True
        self.browser.actions.append(("close_context",))


class FakePage:
    def __init__(self, context):
        self.context = context
        self.form = False
        self.url = ""
        self.rows = 0
        self.frames = [self]
        self.closed = False
        self.armed = False

    def on(self, event, handler):
        assert event == "close"
        self.close_handler = handler

    def is_closed(self):
        return self.closed

    def route_web_socket(self, pattern, handler):
        self.socket_handler = (pattern, handler)

    def evaluate(self, expression):
        assert "__qianshuReadonlyClose" in expression
        self.armed = True
        return True

    def goto(self, url, *, wait_until):
        assert not self.url
        self.url = url
        if self.context.browser.form_closed:
            assert not self.context.offline and self.context.browser.contexts[0].closed
            self.context.browser.actions.append(("after_view", url))
        ok = self.context.request("GET", url, "document")
        if self.context.browser.bad_cdp_event:
            self.session.handler({"key": _SCRIPT_UNLOAD_KEY, "newValue": "example-invalid-json"})
        if url == self.context.browser.blocked_read:
            assert not self.context.request("GET", ORIGIN + "/example/count")
            self.missing_counts = True
        return SimpleNamespace(ok=ok)

    def wait_for_load_state(self, state):
        assert state == "load"

    def locator(self, selector):
        return FakeLocator(self, selector)

    def close(self, *, run_before_unload):
        assert run_before_unload is False and not self.closed
        assert not self.url or self.armed
        self.closed = True
        self.close_handler(self)
        if self.form:
            self.context.browser.actions.append(("close_form",))
            self.context.browser.form_closed = True


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def locator(self, selector):
        return self.page.locator(selector)

    def wait_for(self, *, state):
        assert state == "visible"
        context = self.page.context
        if (self.browser.preload_unready and self.selector == "#ready-0"
                and len(self.browser.contexts) == 1 and len(context.all_pages) == 6):
            raise TimeoutError(SECRET)
        if self.selector == self.browser.unready or getattr(self.page, "missing_counts", False):
            raise TimeoutError(SECRET)

    @property
    def browser(self):
        return self.page.context.browser

    def inner_text(self):
        index = int(urlsplit(self.page.url).path.rsplit("-", 1)[-1])
        assert self.selector == f"#total-{index}"
        if self.browser.form_closed and self.browser.changed_after_form and index == 3:
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
        if self.selector == self.browser.unready:
            raise TimeoutError(SECRET)
        self.browser.actions.append(("fill", self.selector, value))

    def select_option(self, *, label):
        self.browser.actions.append(("select", self.selector, label))

    def click(self):
        self.browser.actions.append(("click", self.selector))
        if self.selector == "#open-example":
            self.page.form = True
            context = self.page.context
            assert context.offline
            context.request("GET", STATIC + "/example.js", "script")
            context.request("POST", ORIGIN + f"/example/add-record?private={SECRET}")
            context.request("POST", ORIGIN + f"/example/unknown/{SECRET}", "xhr")
            context.request("GET", ORIGIN + f"/example/unknown/{SECRET}?value={SECRET}")
            context.request("GET", ORIGIN + "/example/unknown.js", "xhr")
        if self.selector == "#add-example":
            self.page.rows += 1

    def evaluate(self, script, forbidden):
        if script == rehearsal._CHECK_FORM:
            assert self.page.form and self.selector == "#example-form"
            self.browser.actions.append(("check_write_controls",))
            return self.selector not in forbidden
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
    assert result.rehearsal_valid
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
    assert actions.index(("offline",)) < actions.index(("click", "#open-example"))
    assert actions.index(("click", "#open-example")) < actions.index(("check_write_controls",))
    assert actions.index(("check_write_controls",)) < actions.index(("fill", "#example-recharge_days", "0"))
    assert actions.index(("close_form",)) < actions.index(("close_context",))
    assert len([action for action in actions if action[0] == "after_view"]) == 5
    assert [len(context.all_pages) for context in browser.contexts] == [6, 5]
    assert all(context.closed and not context.pages for context in browser.contexts)
    assert browser.options["storage_state"] == str(tmp_path / "mingdao-state.json")

    assert ("POST", ORIGIN + f"/example/add-record?private={SECRET}", "xhr") not in browser.sent
    assert ("POST", ORIGIN + f"/example/unknown/{SECRET}", "xhr") not in browser.sent
    assert all(method == "GET" and SHEET in url for method, url, _ in browser.sent)
    assert result.requests == {"allowed": 11, "blocked": 5, "offline_attempt": 5}

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


def test_blocked_baseline_read_stops_before_opening_form(tmp_path, draft, browser):
    raw = ui_config()
    browser.blocked_read = ORIGIN + raw["views"]["待配置"]["path"]
    result = run(tmp_path, draft, raw)
    assert result.before == {"新建": 0, "待审批": 1}
    assert result.issues == {"before_待配置": "TimeoutError"}
    assert result.after == {}
    assert result.requests == {"allowed": 3, "blocked": 1}
    assert ("click", "#open-example") not in browser.actions
    assert len(browser.context.all_pages) == 3
    assert not browser.context.pages
    assert not result.smoke_passed


def test_cdp_error_stops_smoke(tmp_path, draft, browser):
    browser.cdp_error = True
    result = run(tmp_path, draft, ui_config())
    assert result.guard_failed and not result.smoke_passed
    assert result.requests["error"] == 1
    assert browser.sent == []
    assert len(browser.context.all_pages) == 1
    assert not browser.context.pages


def test_invalid_cdp_audit_event_fails_smoke(tmp_path, draft, browser):
    browser.bad_cdp_event = True
    result = run(tmp_path, draft, ui_config())
    assert result.guard_failed and not result.smoke_passed
    assert result.requests == {"allowed": 1, "error": 1}
    assert len(browser.context.all_pages) == 1
    assert ("click", "#open-example") not in browser.actions


def test_route_callback_error_fails_smoke(tmp_path, draft, browser):
    browser.route_error = True
    result = run(tmp_path, draft, ui_config())
    assert set(result.before) == set(rehearsal.VIEWS)
    assert result.guard_failed and not result.smoke_passed
    assert result.requests["error"] == 1


def test_offline_setup_error_keeps_form_closed(tmp_path, draft, browser):
    browser.offline_error = True
    result = run(tmp_path, draft, ui_config())
    assert result.guard_failed and not result.smoke_passed
    assert result.requests["error"] == 1
    assert result.after == {}
    assert ("click", "#open-example") not in browser.actions
    assert len(browser.contexts) == 1 and browser.context.closed


def test_preload_ready_timeout_fails_smoke_even_with_matching_counts(tmp_path, draft, browser):
    browser.preload_unready = True
    result = run(tmp_path, draft, ui_config())
    assert result.before == result.after and set(result.before) == set(rehearsal.VIEWS)
    assert result.issues == {"preload": "TimeoutError"}
    assert not result.smoke_passed
    assert result.evidence == rehearsal.RehearsalEvidence()
    assert ("offline",) not in browser.actions and ("click", "#open-example") not in browser.actions


def test_skipped_offline_switch_fails_smoke_even_with_matching_counts(tmp_path, draft, browser, monkeypatch):
    monkeypatch.setattr(ReadOnlyContext, "go_offline", lambda self: None)
    result = run(tmp_path, draft, ui_config())
    assert result.before == result.after and set(result.before) == set(rehearsal.VIEWS)
    assert result.issues == {"offline": "RuntimeError"}
    assert not result.smoke_passed
    assert result.evidence == rehearsal.RehearsalEvidence()
    assert ("offline",) not in browser.actions and ("click", "#open-example") not in browser.actions


def test_unopened_form_fails_smoke(tmp_path, draft, browser):
    browser.unready = "#example-form"
    result = run(tmp_path, draft, ui_config())
    assert result.before == result.after and not result.smoke_passed
    assert result.evidence == rehearsal.RehearsalEvidence(offline_switched=True)
    assert ("check_write_controls",) not in browser.actions


def test_write_controls_check_must_succeed(tmp_path, draft, browser):
    raw = ui_config()
    raw["form"]["write_controls"].append(raw["form"]["root"])
    result = run(tmp_path, draft, raw)
    assert result.before == result.after and not result.smoke_passed
    assert result.evidence == rehearsal.RehearsalEvidence(offline_switched=True, form_opened=True)
    assert ("check_write_controls",) in browser.actions
    assert not any(action[0] == "fill" for action in browser.actions)


def test_guard_installation_error_closes_context_and_records_failure(monkeypatch):
    def unavailable(*args):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(FakeContext, "route_web_socket", unavailable)
    fake = FakeBrowser()
    stream = StringIO()
    policy = ReadOnlyPolicy(ORIGIN, (SHEET,))
    with pytest.raises(RuntimeError):
        with readonly_context(fake, policy, Path("example.json"), stream):
            pytest.fail("拦截未就绪时不能提供上下文")
    assert fake.context.closed and not fake.context.all_pages
    assert json.loads(stream.getvalue()) == {
        "method": "OTHER", "path_pattern": "guard_setup", "outcome": "error",
    }


def test_incomplete_form_is_recorded_without_failing_smoke(tmp_path, draft, browser):
    browser.unready = "#example-form-ready"
    result = run(tmp_path, draft, ui_config())
    assert result.smoke_passed and not result.rehearsal_valid
    assert not result.form_complete
    assert result.issues["form"] == "TimeoutError"
    assert result.form_errors == ["合成报错"]


def test_context_blocks_redirect_and_websocket():
    fake = FakeBrowser()
    path = SHEET + "/example-view-0"
    allowed_url = ORIGIN + path + "?redirect=1"
    with readonly_context(fake, ReadOnlyPolicy(ORIGIN, (path,)), Path("example.json"),
                          StringIO()) as (guard, audit):
        context = fake.context
        page = guard.new_page()
        assert not context.request("GET", allowed_url, "document")
        assert fake.sent == [("GET", allowed_url, "document")]
        pattern, handler = page.socket_handler
        events = []
        assert pattern.fullmatch("wss://example.invalid")
        handler(SimpleNamespace(close=lambda: events.append("closed")))
        assert events == ["closed"]
        assert audit.counts == {"allowed": 1, "blocked": 2}
        assert not audit.failed


def test_non_get_is_denied_on_an_allowed_address():
    path = SHEET + "/example-view-0"
    policy = ReadOnlyPolicy(ORIGIN, (path,), (STATIC,))
    assert policy.classify("GET", ORIGIN + path, "document")[0]
    assert not policy.classify("GET", STATIC + path, "document")[0]
    assert not policy.classify("GET", "https://other.example.invalid" + path, "document")[0]
    assert not policy.classify("GET", ORIGIN + path + "-other", "document")[0]
    for method in ("POST", "HEAD", "PUT", "PATCH", "OPTIONS", "DELETE"):
        assert not policy.classify(method, ORIGIN + path, "document")[0]
        assert not policy.classify(method, STATIC + "/example.js", "script")[0]
    assert not policy.classify("GET", STATIC + "/example.js", "xhr")[0]
    assert not policy.classify("GET", "https://other.example.invalid/example.js", "script")[0]
    assert not policy.classify("GET", ORIGIN + "/other/app/example.js/../example.js", "script")[0]


@pytest.mark.parametrize("unready, expected_valid", [
    pytest.param(None, True, id="complete"),
    pytest.param("#example-start_date", False, id="mid-fill-timeout"),
])
def test_fixed_cli_reports_counts_and_disables_fetch_later(tmp_path, draft, capsys, monkeypatch,
                                                         unready, expected_valid):
    browser = FakeBrowser()
    browser.unready = unready
    browser.close = Mock()
    launch = Mock(return_value=browser)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(
        sync_playwright=lambda: nullcontext(playwright),
    ))
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
    launch.assert_called_once()
    assert "--disable-blink-features=FetchLaterAPI" in launch.call_args.kwargs["args"]
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert result == 0 and not captured.err
    assert summary["smoke_passed"] is True
    assert summary["before"] == summary["after"] and set(summary["before"]) == set(rehearsal.VIEWS)
    assert summary["form_complete"] is expected_valid
    assert summary["rehearsal_valid"] is expected_valid
    report = json.loads((tmp_path / summary["report_id"] / "report.json").read_text())
    assert report["smoke_passed"] is True and report["rehearsal_valid"] is expected_valid
    assert report["auto_fields"] == {"价格计划": ["合成价格"]}
    assert report["form_errors"] == ["合成报错"]
    assert report["evidence"] == {
        "offline_switched": True, "form_opened": True, "write_controls_checked": True,
    }
    if unready:
        assert report["issues"]["form"] == "TimeoutError"
        assert ("fill", "#example-policy_name", "合成值") in browser.actions
        assert not any(action[:2] == ("fill", "#example-end_date") for action in browser.actions)
    assert "合成价格" not in captured.out and "合成报错" not in captured.out
