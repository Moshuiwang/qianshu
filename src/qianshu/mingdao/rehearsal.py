"""明道云填表演练入口。真实输入和页面观察结果只供私下使用。"""

import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from time import sleep

from qianshu.config import load_config
from qianshu.draft import DraftField, PackageRow, PolicyDraft
from qianshu.mingdao.readonly import ReadOnlyPolicy, readonly_context

VIEWS = ("新建", "待审批", "待配置", "配置完成", "生效政策")
MAIN_FIELDS = (
    "policy_name", "maintain_mutual_exclusion_group", "company", "policy_type",
    "start_date", "end_date", "benefit_type", "trigger_once", "cancel_on_downgrade",
    "recharge_days", "gift_days", "repeat_bonus", "effective_multiplier",
)
FORM_FIELDS = MAIN_FIELDS + ("user_tags", "packages", "user_scope", "approval_url")

# 点击与检查同一个节点，避免两次解析定位器之间页面把它换成写按钮。
_CLICK = r"""(element, forbidden) => {
    const protectedNodes = element.ownerDocument.querySelectorAll(forbidden.join(","));
    const writeLabel = /保存|提交|保留本次|\bsave\b|\bsubmit\b/i;
    for (let node = element; node; node = node.parentElement) {
        if (!node.matches("button, input, a, [role=button]") && node !== element) continue;
        const label = [node.innerText, node.value, node.title, node.getAttribute("aria-label")].join(" ");
        if ((node.matches("button, input") && (node.form || node.hasAttribute("type"))
                && ["submit", "reset"].includes(node.type))
                || writeLabel.test(label)) return false;
    }
    for (const node of protectedNodes) {
        if (element.contains(node) || node.contains(element)) return false;
    }
    if (element.matches(":disabled, [aria-disabled=true]")) return false;
    element.click();
    return true;
}"""

_READ_VALUES = """nodes => nodes
    .filter(node => node.getClientRects().length && getComputedStyle(node).visibility === "visible")
    .map(node => node.value ?? node.innerText ?? node.textContent)"""


class UnsafeControl(ValueError):
    """操作目标与写控件重叠，或无法确认可安全点击。"""


@dataclass(frozen=True, repr=False)
class View:
    path: str
    ready: str
    total: str


@dataclass(frozen=True, repr=False)
class RehearsalConfig:
    policy: ReadOnlyPolicy
    storage_state: Path
    views: dict[str, View]
    form: dict

    @classmethod
    def from_mapping(cls, origin: str, raw: dict, *, private_dir: Path) -> "RehearsalConfig":
        views = {name: View(**raw["views"][name]) for name in VIEWS}
        if len({view.path for view in views.values()}) != len(VIEWS):
            raise ValueError("五个视图须分别配置路径")
        if any(not view.ready.strip() or not view.total.strip() for view in views.values()):
            raise ValueError("每个视图须配置加载完成与总数定位器")
        policy = ReadOnlyPolicy(origin, tuple(view.path for view in views.values()),
                                tuple(raw.get("static_origins", ())))
        state = Path(raw.get("storage_state", private_dir / "mingdao-state.json"))
        form = raw["form"]
        for key in ("open", "root", "errors"):
            if not form[key].strip():
                raise ValueError("演练表单定位器不能为空")
        if not form["write_controls"] or any(not selector.strip() for selector in form["write_controls"]):
            raise ValueError("须配置写控件选择器")
        if not form["auto_fields"]:
            raise ValueError("须配置自动带出字段的读回定位器")
        return cls(policy, state, views, form)


@dataclass(frozen=True, repr=False)
class RehearsalResult:
    before: dict[str, int]
    after: dict[str, int]
    form_complete: bool
    auto_fields: dict[str, list[str]]
    form_errors: list[str]
    issues: dict[str, str]
    requests: dict[str, int]
    guard_failed: bool
    private_report: Path

    @property
    def smoke_passed(self) -> bool:
        return (
            not self.guard_failed
            and "unsafe_control" not in self.issues
            and "close_form" not in self.issues
            and set(self.before) == set(VIEWS)
            and self.before == self.after
        )


@contextmanager
def _launch_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=[
            "--disable-quic", "--disable-background-networking",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ])
        try:
            yield browser
        finally:
            browser.close()


def _open_view(page, config: RehearsalConfig, view: View) -> None:
    url = config.policy.app_origin + view.path
    response = page.goto(url, wait_until="domcontentloaded")
    if response is None or not response.ok or page.url != url:
        raise ValueError("视图导航未成功")
    page.locator(view.ready).wait_for(state="visible")


def _read_counts(page, config: RehearsalConfig, issues: dict[str, str], phase: str) -> dict[str, int]:
    counts = {}
    for name, view in config.views.items():
        try:
            _open_view(page, config, view)
            total = page.locator(view.total)
            total.wait_for(state="visible")
            match = re.fullmatch(r"\s*(?:共\s*)?(\d+(?:,\d{3})*)(?:\s*行)?\s*", total.inner_text())
            if not match:
                raise ValueError("视图总行数不可读")
            counts[name] = int(match[1].replace(",", ""))
        except Exception as exc:
            issues[f"{phase}_{name}"] = type(exc).__name__
    return counts


def _click(locator, form: dict) -> None:
    locator.wait_for(state="visible")
    try:
        clicked = locator.evaluate(_CLICK, form["write_controls"])
    except Exception:
        raise UnsafeControl("无法确认演练控件可安全点击") from None
    if not clicked:
        raise UnsafeControl("演练禁止点击写控件")


def _enter(root, page, spec: dict, value: str, form: dict) -> None:
    control = root.locator(spec["control"])
    kind = spec["kind"]
    if kind == "fill":
        control.fill(value)
    elif kind == "select":
        control.select_option(label=value)
    elif kind == "picker":
        _click(control, form)
        panel = page.locator(spec["panel"])
        if search := spec.get("search"):
            panel.locator(search).fill(value)
        options = panel.locator(spec["option"]).and_(panel.get_by_text(value, exact=True))
        _click(options, form)
    else:
        raise ValueError("未知的演练表单控件类型")


def _draft_values(draft: PolicyDraft) -> dict:
    values = {}
    for name in FORM_FIELDS:
        field = getattr(draft, name)
        if field.value is None or field.uncertainty is not None:
            raise ValueError(f"草稿字段未确定：{name}")
        if name == "user_tags" and (
            not isinstance(field.value, list)
            or any(not isinstance(tag, str) or not tag.strip() for tag in field.value)
        ):
            raise ValueError("用户标签须为名称列表")
        if name == "packages" and (
            not isinstance(field.value, list)
            or any(
                not isinstance(row, PackageRow)
                or not isinstance(row.current_package, str) or not row.current_package.strip()
                or (row.gift_package is not None
                    and (not isinstance(row.gift_package, str) or not row.gift_package.strip()))
                for row in field.value
            )
        ):
            raise ValueError("节目包须为草稿子表行")
        values[name] = field.value
    return values


def _fill_form(page, root, form: dict, values: dict) -> None:
    for name in MAIN_FIELDS:
        _enter(root, page, form["fields"][name], str(values[name]), form)
    for tag in values["user_tags"]:
        _enter(root, page, form["tags"], tag, form)

    packages = form["packages"]
    rows = root.locator(packages["rows"])
    for index, package in enumerate(values["packages"]):
        if rows.count() <= index:
            _click(root.locator(packages["add"]), form)
        row = rows.nth(index)
        row.wait_for(state="visible")
        _enter(row, page, packages["current"], package.current_package, form)
        if package.gift_package is not None:
            _enter(row, page, packages["gift"], package.gift_package, form)

    for name in ("user_scope", "approval_url"):
        spec = form["fields"][name]
        _click(root.locator(spec["tab"]), form)
        _enter(root, page, spec, values[name], form)


def _observe(page, root, form: dict, auto_fields: dict, form_errors: list, issues: dict) -> None:
    # 切页签之前保留填写结束时可见的报错，再补读观察页签上的错误。
    def read_errors() -> None:
        for message in page.locator(form["errors"]).evaluate_all(_READ_VALUES):
            if message and message not in form_errors:
                form_errors.append(message)

    try:
        read_errors()
    except Exception as exc:
        issues["observe_errors"] = type(exc).__name__
    for name, spec in form["auto_fields"].items():
        try:
            if tab := spec.get("tab"):
                _click(root.locator(tab), form)
            observed = root.locator(spec["control"]).evaluate_all(_READ_VALUES)
            auto_fields[name] = observed
            if not observed and spec.get("required", True):
                issues[f"observe_{name}"] = "NotVisible"
            read_errors()
        except UnsafeControl:
            raise
        except Exception as exc:
            issues[f"observe_{name}"] = type(exc).__name__


def _rehearse(page, config: RehearsalConfig, values: dict, auto_fields: dict,
              form_errors: list, issues: dict) -> bool:
    form = config.form
    root = page.locator(form["root"])
    complete = False
    try:
        _open_view(page, config, config.views["新建"])
        _click(page.locator(form["open"]), form)
        root.wait_for(state="visible")
        _fill_form(page, root, form, values)
        if ready := form.get("observations_ready"):
            root.locator(ready).wait_for(state="visible")
        complete = True
    except UnsafeControl:
        issues["unsafe_control"] = "UnsafeControl"
        return False
    except Exception as exc:
        issues["form"] = type(exc).__name__
    try:
        _observe(page, root, form, auto_fields, form_errors, issues)
    except UnsafeControl:
        issues["unsafe_control"] = "UnsafeControl"
        return False
    return complete and not any(key.startswith("observe_") for key in issues)


def run_rehearsal(draft: PolicyDraft, config: RehearsalConfig, *,
                  private_dir: Path = Path(".private")) -> RehearsalResult:
    """演练一条草稿并写私有证据。表单失败时仍关闭该页并回读各视图。"""
    values = _draft_values(draft)
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir = Path(mkdtemp(prefix="mingdao-rehearsal-", dir=private_dir))
    issues = {}
    auto_fields = {}
    form_errors = []
    complete = False
    with (
        open(run_dir / "requests.jsonl", "x", encoding="utf-8", opener=_private_file) as stream,
        _launch_browser() as browser,
    ):
        with readonly_context(browser, config.policy, config.storage_state, stream) as (context, audit):
            view_page = context.new_page()
            before = _read_counts(view_page, config, issues, "before")
            # 基线不完整也允许只读骨架取证，最终冒烟判定要求五个视图证据齐备。
            form_page = context.new_page()
            try:
                complete = _rehearse(form_page, config, values, auto_fields, form_errors, issues)
            finally:
                try:
                    form_page.close(run_before_unload=False)
                except Exception as exc:
                    issues["close_form"] = type(exc).__name__
            after = _read_counts(view_page, config, issues, "after")

    report = run_dir / "report.json"
    result = RehearsalResult(before, after, complete, auto_fields, form_errors, issues,
                             dict(audit.counts), audit.failed, report)
    with open(report, "x", encoding="utf-8", opener=_private_file) as stream:
        json.dump({
            "before": before, "after": after,
            "delta": {name: after[name] - before[name] for name in before.keys() & after.keys()},
            "smoke_passed": result.smoke_passed, "form_complete": complete,
            "auto_fields": auto_fields, "form_errors": form_errors,
            "issues": issues, "requests": result.requests, "guard_failed": audit.failed,
        }, stream, ensure_ascii=False, indent=2)
    return result


def _private_file(path, flags):
    return os.open(path, flags, 0o600)


def _load_draft(path: Path) -> PolicyDraft:
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = {}
    for name in FORM_FIELDS:
        field = raw[name]
        if field.get("uncertainty") is not None:
            raise ValueError(f"草稿字段未确定：{name}")
        value = field["value"]
        if name == "packages" and isinstance(value, list):
            value = [PackageRow(**row) for row in value]
        fields[name] = DraftField(value=value)
    return PolicyDraft(**fields, approval_attachments=DraftField())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="明道云填表演练")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--private-dir", type=Path, default=Path(".private"))
    parser.add_argument("--ui-config", type=Path, required=True)
    parser.add_argument("draft", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        config = load_config("MINGDAO_BASE_URL", env_file=args.env_file)
        ui = json.loads(args.ui_config.read_text(encoding="utf-8"))
        settings = RehearsalConfig.from_mapping(config["MINGDAO_BASE_URL"], ui,
                                                 private_dir=args.private_dir)
        drafts = [_load_draft(path) for path in args.draft]
        for index, draft in enumerate(drafts, 1):
            if index > 1:
                sleep(3)
            result = run_rehearsal(draft, settings, private_dir=args.private_dir)
            print(json.dumps({
                "policy_index": index, "smoke_passed": result.smoke_passed,
                "form_complete": result.form_complete, "requests": result.requests,
                "before": result.before, "after": result.after,
                "form_error_count": len(result.form_errors),
                "auto_field_count": sum(any(values) for values in result.auto_fields.values()),
                "report_id": result.private_report.parent.name,
            }, ensure_ascii=False))
            if not result.smoke_passed:
                return 1
    except Exception as exc:
        print(f"演练执行失败（{type(exc).__name__}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
