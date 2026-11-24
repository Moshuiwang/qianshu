"""回归批量请求字节核对及逐篇结果取回。"""

import json
from io import BytesIO
import time
from collections import Counter
from contextlib import closing, contextmanager
from unittest.mock import patch

import anthropic
import httpx
from pydantic import ValidationError

from qianshu.parse import parse_application
from qianshu.parse.model import (
    ModelError, _REPLY_TOOL, _missing_shape, configured_model, validate_reply_json,
)
from qianshu.parse.protocol import ApplicationReply

from .ledger import transaction
from .recording import reference_cost
from .storage import RegressionError, digest, json_bytes, read_json


def _members(raw):
    """给出外层成员的键、成员起点和值的范围，嵌套 JSON 保留原字节写法。"""
    def skip_space(i):
        while i < len(raw) and raw[i] in " \r\n\t":
            i += 1
        return i

    decoder = json.JSONDecoder()
    start = skip_space(0)
    close = {"{": "}", "[": "]"}[raw[start]]
    pos = skip_space(start + 1)
    keys = set()
    while raw[pos] != close:
        member = pos
        if close == "}":
            key, end = decoder.raw_decode(raw, pos)
            colon = skip_space(end)
            if not isinstance(key, str) or key in keys or raw[colon] != ":":
                raise RegressionError("JSON 成员无效。")
            pos = skip_space(colon + 1)
        else:
            key = len(keys)
        keys.add(key)
        _, end = decoder.raw_decode(raw, pos)
        yield key, member, pos, end
        pos = skip_space(end)
        if raw[pos] == close:
            break
        if raw[pos] != ",":
            raise RegressionError("JSON 分隔符无效。")
        pos = skip_space(pos + 1)
        if raw[pos] == close:
            raise RegressionError("JSON 含多余分隔符。")
    if skip_space(pos + 1) != len(raw):
        raise RegressionError("JSON 含多余内容。")


def parts(raw):
    return {key: raw[start:end] for key, _, start, end in _members(raw)}


class Captured(Exception):
    pass


def capture_requests(snapshot, rules, legacy, settings):
    wires = {}
    def capture(request):
        wires[token] = request.content.decode("utf-8")
        raise Captured
    def http_client(*, transport=None, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(capture), **kwargs)
    # 仅替换本地回归进程中这一轮 SDK 的传输，离开上下文即恢复。
    with patch.object(anthropic, "DefaultHttpxClient", http_client):
        model = configured_model({**settings, "ANTHROPIC_API_KEY": "offline-capture"}, max_retries=0)
        for token, doc in snapshot["documents"].items():
            try:
                parse_application(doc["body"], rules_dir=rules, companies_text=snapshot["companies_text"],
                                  legacy_companies=legacy, model=model)
            except (ModelError, Captured):
                if token in wires:
                    continue
                raise RegressionError("未截获即时 SDK 请求。") from None
            raise RegressionError("未截获篇目请求。")
    return wires


def common_body(raw):
    members = list(_members(raw))
    for index, (key, member, start, end) in enumerate(members):
        if key == "stream" and raw[start:end] == "true":
            if index + 1 < len(members):
                comma = raw.index(",", end, members[index + 1][1])
                return raw[:member] + raw[end:comma] + raw[comma + 1:]
            if index:
                comma = raw.index(",", members[index - 1][3], member)
                return raw[:comma] + raw[comma + 1:member] + raw[end:]
            return raw[:member] + raw[end:]
    raise RegressionError("即时请求缺少流式开关。")


def verify_envelope(raw, expected):
    envelope = parts(raw)
    if envelope.keys() != {"requests"}:
        raise RegressionError("批量信封字段不符。")
    found = {}
    for item in parts(envelope["requests"]).values():
        fields = parts(item)
        if fields.keys() != {"custom_id", "params"}:
            raise RegressionError("批量关联字段不符。")
        key = json.loads(fields["custom_id"])
        if key in found:
            raise RegressionError("批量关联键重复。")
        found[key] = fields["params"]
    if found != expected:
        raise RegressionError("批量与即时业务请求字节不同。")


def batch_cost(lines, model_id):
    usage = Counter()
    missing = 0
    for line in lines:
        result = json.loads(line)["result"]
        counts = (result.get("message") or {}).get("usage") or result.get("usage") or {}
        known = {k: counts[k] for k in ("input_tokens", "output_tokens")
                 if type(counts.get(k)) is int and counts[k] >= 0}
        missing += len(known) != 2
        usage.update(known)
    return reference_cost(model_id, "batch", usage, missing, live=True)


def batch_attempt(line, *, first_shape_error=None, effort=None):
    raw_result = parts(line)["result"]
    result = json.loads(raw_result)
    outcome = result["type"]
    if outcome not in {"succeeded", "errored", "expired", "canceled"}:
        raise RegressionError("未知批量结局。")
    message = result.get("message") or {}
    usage = message.get("usage") or result.get("usage") or {}
    a = dict(batch_outcome=outcome, elapsed_ms=None, model=message.get("model"), stop_reason=message.get("stop_reason"),
             input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
             blocks=None, reply=None, failure_stage="model_call", effort=effort,
             first_shape_error=first_shape_error, retry_result="failed" if first_shape_error else None,
             partial_input_tokens=None, partial_output_tokens=None)
    if any(v is not None and (type(v) is not int or v < 0) for v in (a["input_tokens"], a["output_tokens"])):
        raise RegressionError("批量用量无效。")
    if outcome != "succeeded":
        if outcome == "errored":
            error = result.get("error") or {}
            kind = (error.get("error") or error).get("type")
            known = {"invalid_request_error", "authentication_error", "permission_error", "not_found_error",
                     "rate_limit_error", "api_error", "overloaded_error", "billing_error"}
            a["error_type"] = kind if kind in known else "other"
        return a
    content = parts(parts(raw_result)["message"])["content"]
    blocks = []
    tools = []
    for raw in parts(content).values():
        block = json.loads(raw)
        kind = block["type"]
        if kind == "tool_use":
            payload = parts(raw).get("input", "")
            tools.append((block["name"], payload))
            length = len(payload)
        else:
            length = len(block.get("text", block.get("thinking", raw)))
        blocks.append({"type": kind, "length": length})
    a.update(blocks=blocks, reply=content)
    if a["stop_reason"] not in {"tool_use", "end_turn"}:
        a["failure_stage"] = "model_incomplete"
    elif not tools:
        a["failure_stage"] = "reply_tool_missing"
    elif len(tools) > 1:
        a["failure_stage"] = "reply_tool_multiple"
        if not first_shape_error and [name for name, _ in tools] == [_REPLY_TOOL, _REPLY_TOOL]:
            a["first_shape_error"] = "reply_tool_multiple"
            a["structure_path"] = ("content",)
    elif tools[0][0] != _REPLY_TOOL:
        a["failure_stage"] = "reply_tool_unexpected"
    else:
        a.update(reply=tools[0][1], failure_stage=None)
        try:
            validate_reply_json(a["reply"])
            ApplicationReply.model_validate_json(a["reply"])
        except ValidationError as error:
            a["failure_stage"] = "reply_schema"
            if not first_shape_error and (missing := _missing_shape(error)):
                a["first_shape_error"], a["structure_path"] = missing
            else:
                a["structure_path"] = error.errors(include_input=False)[0]["loc"]
        except ValueError:
            a["failure_stage"] = "tool_input_json"
    if first_shape_error:
        if a["failure_stage"] is None:
            a["retry_result"] = "succeeded"
        else:
            a.setdefault("structure_path", ())
    return a


def _correction_request(wire, attempt):
    request = json.loads(wire)
    reason = attempt["first_shape_error"]
    feedback = json.dumps({"path": attempt["structure_path"], "reason": reason})
    count = 2 if reason == "reply_tool_multiple" else 1
    calls = [{"type": "tool_use", "id": f"toolu_retry_{i}", "name": _REPLY_TOOL, "input": {}}
             for i in range(count)]
    results = [{"type": "tool_result", "tool_use_id": call["id"], "content": feedback, "is_error": True}
               for call in calls]
    request["messages"].extend([{"role": "assistant", "content": calls}, {"role": "user", "content": results}])
    return json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def raw_lines(response):
    pending = BytesIO()
    for chunk in response.iter_bytes():
        for part in chunk.splitlines(keepends=True):
            pending.write(part)
            if part.endswith(b"\n"):
                yield pending.getvalue().decode("utf-8")
                pending = BytesIO()
    if pending.tell():
        yield pending.getvalue().decode("utf-8")


def _fetch_round(ledger, run_id, estimate, config, wires, settings, *, run_day, resume, reconcile_id, correction):
    """每轮提交前登记预占与提交意图，按篇取回原始结果。"""
    files = ledger.files
    prefix = [run_id, "shape_retry"] if correction else [run_id]
    keys = {token: digest(json_bytes([*prefix, token])) for token in wires}
    expected = {keys[token]: common_body(raw) for token, raw in wires.items()}
    requests = [{"custom_id": k, "params": json.loads(v)} for k, v in expected.items()]
    identity = digest(json_bytes([config, expected]))
    journal = ledger.journal(run_id)
    @contextmanager
    def round_state():
        with transaction(files, journal) as run:
            yield run.setdefault("shape_retry", {}) if correction else run

    def verify(request):
        if (request.url.scheme, request.url.host, request.url.port) != origin:
            raise RegressionError("批量接口返回了不同来源的地址。")
        if request.method == "POST":
            if request.content != captured[0].encode():
                raise RegressionError("待发送批量字节与预先登记的信封不同。")
    # 核对实际 SDK 序列化的批量信封，并在发出真实 POST 前再次核对。
    captured = []
    def intercept(request):
        verify_envelope(request.content.decode(), expected)
        captured.append(request.content.decode())
        return httpx.Response(200, json={"id": "offline"})
    with anthropic.Anthropic(api_key="offline", http_client=httpx.Client(
        transport=httpx.MockTransport(intercept)), max_retries=0) as offline:
        offline.messages.batches.create(requests=requests)
    http = anthropic.DefaultHttpxClient(follow_redirects=False, event_hooks={"request": [verify]})
    with anthropic.Anthropic(api_key=settings["ANTHROPIC_API_KEY"], base_url=settings.get("ANTHROPIC_BASE_URL"),
                             http_client=http, max_retries=0, timeout=60) as client:
        origin = (client.base_url.scheme, client.base_url.host, client.base_url.port)
        batches = client.messages.batches
        if not correction:
            if not resume:
                ledger.reserve(run_id, "batch", estimate, identity, today=config["today"], run_day=run_day,
                               config_sha256=digest(json_bytes(config)))
            with transaction(files, ledger.path) as runs:
                if runs.get(run_id, {}).get("identity") != identity:
                    raise RegressionError("续取与原提交配置不一致。")
        if correction and not resume:
            ledger.reserve_correction(run_id, identity)
        if not resume:
            with round_state() as state:
                if state:
                    raise RegressionError("批次已登记。")
                state.update(identity=identity, run_id=run_id, freeze_sha256=config["freeze_sha256"],
                             expected_count=len(keys), submitted=time.time(), deadline=time.time() + 25 * 3600,
                             batch_sha256=digest(captured[0].encode()),
                             common_sha256={k: digest(v.encode()) for k, v in expected.items()},
                             instant=wires, batch=captured[0], custom_ids=keys)
            try:
                batch_id = batches.create(requests=requests).id
                if not isinstance(batch_id, str) or not batch_id:
                    raise ValueError
            except (anthropic.APIError, httpx.HTTPError, AttributeError, ValueError):
                raise RegressionError("批量创建受理未知，保留预占，须按原运行号只读对账。") from None
            with round_state() as state:
                state["batch_id"] = batch_id
        with round_state() as state:
            if (state.get("identity") != identity or state.get("conflict") or state.get("instant") != wires
                or state.get("batch_sha256") != digest(captured[0].encode()) or state.get("batch") != captured[0]):
                raise RegressionError("批次登记与请求不符或有冲突，须人工对账。")
            bound = state.get("batch_id")
            if bound and reconcile_id and bound != reconcile_id:
                raise RegressionError("登记的批次 ID 与对账号不同。")
            batch_id, deadline = bound or reconcile_id, state["deadline"]
            finished = state.get("complete")
            if not batch_id:
                raise RegressionError("批次受理未知，须只读对账。")
        def row_path(key):
            return journal.with_name(journal.stem + "-" + key + ".json")
        model_id = json.loads(next(iter(wires.values())))["model"]
        if not finished:
            delay = 10
            while True:
                status = batches.retrieve(batch_id)
                if status.processing_status == "ended":
                    break
                if time.time() >= deadline:
                    raise RegressionError("批次仍在途，可按原运行号续取。")
                time.sleep(min(delay, max(0, deadline - time.time())))
                delay = min(delay * 2, 60)
            with round_state() as state:
                state.setdefault("observed_seconds", time.time() - state["submitted"])
            seen = {}
            with closing(batches.results(batch_id)) as result:
                for line in raw_lines(result.http_response):
                    key = json.loads(parts(line)["custom_id"])
                    if key not in expected:
                        raise RegressionError("结果含未登记关联键。")
                    with transaction(files, row_path(key)) as item:
                        conflict = bool(item) and item.get("raw") != line
                        if not conflict:
                            checksum = digest(line.encode())
                            if key not in seen:
                                ledger.record_batch_item(run_id, key, checksum, batch_cost([line], model_id),
                                                         correction=correction)
                                seen[key] = line
                            item.update(raw=line, sha256=checksum)
                    if conflict:
                        with round_state() as state:
                            state["conflict"] = True
                        raise RegressionError("同一批量结果的字节冲突。")
            if seen.keys() != expected.keys():
                raise RegressionError("取回结果缺篇，请从原批次续取。")
            with round_state() as state:
                if state.get("conflict"):
                    raise RegressionError("结果有冲突，须人工对账。")
                state.update(complete=True, batch_id=batch_id)
        rows = {}
        for key in expected:
            item = read_json(files.read(row_path(key)))
            if digest(item["raw"].encode()) != item["sha256"]:
                raise RegressionError("逐篇原始结果指纹不符。")
            rows[key] = item["raw"]
    return {token: rows[key] for token, key in keys.items()}, state["observed_seconds"]


def fetch_batch(ledger, run_id, estimate, config, wires, settings, *, run_day, resume=False, reconcile_id=None):
    journal = ledger.journal(run_id)
    retry_registered = False
    retry_reconcile = None
    if resume:
        with transaction(ledger.files, journal) as state:
            retry_registered = "shape_retry" in state
            if retry_registered and reconcile_id != state.get("batch_id"):
                retry_reconcile, reconcile_id = reconcile_id, None

    rows, seconds = _fetch_round(ledger, run_id, estimate, config, wires, settings, run_day=run_day,
                                 resume=resume, reconcile_id=reconcile_id, correction=False)
    effort = config.get("effort")
    attempts = {token: [batch_attempt(line, effort=effort)] for token, line in rows.items()}
    retry_wires = {token: _correction_request(wires[token], first)
                   for token, (first,) in attempts.items() if first["first_shape_error"]}
    lines = list(rows.values())
    if retry_wires:
        if reconcile_id and not retry_registered:
            raise RegressionError("首轮已完成对账，请用原参数续取以执行形状纠错。")
        retry_rows, retry_seconds = _fetch_round(
            ledger, run_id, estimate, config, retry_wires, settings, run_day=run_day,
            resume=retry_registered, reconcile_id=retry_reconcile, correction=True,
        )
        seconds += retry_seconds
        lines.extend(retry_rows.values())
        for token, line in retry_rows.items():
            final = batch_attempt(line, first_shape_error=attempts[token][0]["first_shape_error"], effort=effort)
            # 即时录制只给最终格式错误保留 structure_path，首轮位置已进入纠错请求。
            del attempts[token][0]["structure_path"]
            final["request"] = retry_wires[token]
            attempts[token].append(final)
    model_id = json.loads(next(iter(wires.values())))["model"]
    ledger.record_cost(run_id, batch_cost(lines, model_id))

    def source(token, user_sha, system_sha):
        request = json.loads(wires[token])
        if (digest(request["system"].encode()) != system_sha
            or digest(request["messages"][0]["content"].encode()) != user_sha):
            raise RegressionError("批量录制与计分输入不同。")
        return attempts[token]
    source.batch_seconds = seconds
    return source


def instant_transport(recording):
    """录下真实即时请求的原始字节，保留 SDK 已配置的传输和重试策略。"""
    factory = anthropic.DefaultHttpxClient
    def client(**options):
        def capture(request):
            raw = request.content.decode("utf-8")
            recording.document["request"] = raw
            recording.document["request_sha256"] = digest(common_body(raw).encode())
        http_client = factory(**options)
        http_client.event_hooks["request"].append(capture)
        return http_client
    return patch.object(anthropic, "DefaultHttpxClient", client)
