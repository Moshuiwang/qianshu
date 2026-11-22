"""保存每篇申请的模型调用证据，并从私有录制重放。"""

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from re import fullmatch
from uuid import uuid4

from qianshu.parse.model import REPLY_FORMAT_ERRORS, CallResult, ModelError, ModelReplyError, ParseModel
from qianshu.parse.protocol import PROTOCOL_VERSION

from .business import measure_fingerprint, pairing_fingerprint
from .runner import RULES, IncompleteReply
from .storage import Output, PrivateFiles, RegressionError, digest, json_bytes, read_json

MANIFEST = "recording.json"
FAILURES = {None, "model_call", "model_incomplete", "model_invalid", "reply_truncated", *REPLY_FORMAT_ERRORS}
USAGE_KEYS = ("input_tokens", "output_tokens", "elapsed_ms")


def _interrupted_with_text(attempt: dict) -> bool:
    return attempt["failure_stage"] == "reply_truncated" or (
        attempt["failure_stage"] == "model_incomplete" and attempt["stop_reason"] is None and bool(attempt["reply"])
    )


def _retryable(attempt: dict) -> bool:
    return attempt["failure_stage"] == "model_call" or _interrupted_with_text(attempt)


def _reply_failure(attempt: dict) -> str | None:
    if attempt["failure_stage"] == "model_incomplete":
        return {"max_tokens": "reply_truncated", "refusal": "reply_refused"}.get(
            attempt["stop_reason"], "reply_incomplete",
        )
    return None


def configuration(snapshot: dict, rules_dir: Path, model_id: str, threshold: int, legacy: bool,
                  release: bool, retries: int) -> dict:
    source = Path(__file__).resolve().parents[1]
    code = {file.relative_to(source).as_posix(): digest(file.read_bytes()) for file in source.rglob("*.py")}
    rules = {name: digest((rules_dir / name).read_bytes()) for name in RULES}
    return {
        "freeze_sha256": snapshot["fingerprint"],
        "code_sha256": digest(json_bytes(code)),
        "rules_sha256": digest(json_bytes(rules)),
        "companies_sha256": digest(snapshot["companies_text"].encode()),
        "prompt_sha256": rules["parse-prompt.md"],
        "model_sha256": digest(model_id.encode()),
        "pairing_sha256": pairing_fingerprint(threshold),
        "measure_sha256": measure_fingerprint(threshold),
        "legacy_companies": legacy, "release": release, "retries": retries,
    }


def doc_names(snapshot: dict) -> dict[str, str]:
    return {token: f"document-{index:05d}.json" for index, token in enumerate(snapshot["documents"], 1)}


def _untimed_attempt(model_id: str, reply: str | None, failure_stage: str | None) -> dict:
    return {
        "stop_reason": None, "blocks": None, "model": model_id, "reply": reply, "failure_stage": failure_stage,
        **dict.fromkeys(USAGE_KEYS),
    }


def _call_data(result: CallResult) -> dict:
    data = asdict(result)
    data["blocks"] = [{"type": kind, "length": length} for kind, length in result.blocks]
    return data


class SavedReplies:
    def __init__(self, files: PrivateFiles, directory: Path):
        self.files = files
        self.directory = directory
        try:
            self.manifest = read_json(files.read(directory / MANIFEST))
        except FileNotFoundError:
            raise RegressionError("找不到完整录制的清单。") from None
        if self.manifest["schema"] != 1:
            raise RegressionError("录制清单版本不受支持。")

    def for_attempts(self, token: str, user_sha: str, system_sha: str) -> list[dict]:
        by_document = self.manifest["documents_by_user_sha256"].get(user_sha, {})
        name = by_document.get(digest(token.encode()))
        if not isinstance(name, str) or not fullmatch(r"document-[0-9]{5,}\.json", name):
            raise RegressionError(f"录制缺少对应的回复（用户消息指纹 {user_sha}）。")
        try:
            raw = self.files.read(self.directory / name)
        except FileNotFoundError:
            raise RegressionError(f"录制缺少对应的回复文件（用户消息指纹 {user_sha}）。") from None
        if digest(raw) != self.manifest["artifact_sha256"][name]:
            raise RegressionError("录制的回复文件指纹不一致。")
        record = read_json(raw)
        if record["user_sha256"] != user_sha or record["system_sha256"] != system_sha:
            raise RegressionError("录制的用户消息或系统消息指纹与本次解析不一致。")
        return record["attempts"]


def probe_replies(files: PrivateFiles, directory: Path, model_id: str) -> Callable:
    def load(_token: str, user_sha: str, system_sha: str) -> list[dict]:
        try:
            probe = read_json(files.read(directory / f"{user_sha}.json"))
        except FileNotFoundError:
            raise RegressionError(f"导入缺少 probe 回复（用户消息指纹 {user_sha}）。") from None
        if probe.get("user_sha") != user_sha or probe.get("system_sha") != system_sha:
            raise RegressionError("probe 的用户消息或系统消息指纹与本次解析不一致。")
        reply, error = probe.get("reply"), probe.get("error")
        if isinstance(reply, str) and error is None:
            return [_untimed_attempt(model_id, reply, None)]
        if reply is None and isinstance(error, str) and error:
            return [_untimed_attempt(model_id, None, "model_call")]
        raise RegressionError("probe 的回复或失败记录格式无效。")
    return load


class Recording:
    def __init__(
        self, output: Output, names: dict[str, str], config: dict, model_id: str, *,
        model: ParseModel | None = None, source: Callable | None = None,
        origin: str = "live", origin_run_id: str | None = None,
        replaces_run_id: str | None = None,
    ):
        self.output = output
        self.names = names
        self.config = config
        self.model_id = model_id
        self.model = model
        self.source = source
        self.origin = origin
        self.origin_run_id = origin_run_id
        self.replaces_run_id = replaces_run_id
        self.run_id = uuid4().hex
        self.by_user = {}
        self.artifacts = {}
        self.unfinished_calls = False
        self.attempt_count = 0
        self.usage_counts = Counter()
        self.usage_totals = Counter()
        self.probe_fingerprints = []

    def on_result(self, result: CallResult) -> None:
        self.call_result = _call_data(result)

    def for_document(self, token: str) -> ParseModel:
        self.document = {"user_sha256": None, "system_sha256": None, "attempts": []}

        def call(*, system: str, user: str) -> str:
            user_sha, system_sha = digest(user.encode()), digest(system.encode())
            self.document.update(user_sha256=user_sha, system_sha256=system_sha)
            if self.source is not None:
                self.document["attempts"] = self.source(token, user_sha, system_sha)
            else:
                for _ in range(self.config["retries"] + 1):
                    attempt = self._invoke(system=system, user=user)
                    self.document["attempts"].append(attempt)
                    if not _retryable(attempt):
                        break
            attempts = self.document["attempts"]
            if not attempts or any(item["failure_stage"] not in FAILURES for item in attempts):
                raise RegressionError("录制的调用尝试记录无效。")
            result = attempts[-1]
            if _retryable(result):
                raise ModelError("回归模型调用失败。")
            if result["failure_stage"] in REPLY_FORMAT_ERRORS:
                raise ModelReplyError(
                    result["failure_stage"], result["reply"], "申请解析模型回复解析失败。",
                    path=tuple(result.get("structure_path", ())),
                )
            if reason := _reply_failure(result):
                raise IncompleteReply(reason)
            if result["failure_stage"] is not None:
                raise ModelError("回归模型调用失败。")
            if not isinstance(result["reply"], str):
                raise RegressionError(f"录制缺少有效回复（用户消息指纹 {user_sha}）。")
            return result["reply"]

        return call

    def _invoke(self, *, system: str, user: str) -> dict:
        self.call_result = None
        try:
            reply = self.model(system=system, user=user)
        except Exception as error:
            if isinstance(error, ModelReplyError) and error.code in REPLY_FORMAT_ERRORS:
                data = self.call_result or _untimed_attempt(self.model_id, error.reply, error.code)
                return {**data, "structure_path": error.path}
            if self.call_result is None:
                return _untimed_attempt(self.model_id, None, "model_call")
            return {**self.call_result, "failure_stage": self.call_result["failure_stage"] or "model_call"}
        if not isinstance(reply, str):
            return _untimed_attempt(self.model_id, None, "model_invalid")
        data = self.call_result or _untimed_attempt(self.model_id, reply, None)
        return {**data, "reply": reply}

    def finish(
        self, token: str, status: str, received: bool, *,
        code: str | None = None, path: tuple = (),
    ) -> bool:
        """写入本篇录制，返回是否有传输中断造成的截断。"""
        attempts = self.document["attempts"]
        if status == "model_failed":
            failure = attempts[-1]["failure_stage"] if attempts else "model_call"
        elif status == "parse_failed":
            failure = "parse_setup"
            if received:
                failure = (_reply_failure(attempts[-1]) if attempts else None) or "reply_parse"
        else:
            failure = None
        self.unfinished_calls |= status == "model_failed" or failure == "parse_setup"
        record = {
            **self.document, "run_id": self.run_id, "configuration": self.config,
            "model": self.model_id, "failure_stage": failure, "protocol_version": PROTOCOL_VERSION,
            "reason_code": code, "structure_path": path,
        }
        name = self.names[token]
        raw = json_bytes(record)
        self.output.write(name, raw)
        self.artifacts[name] = digest(raw)
        if user_sha := record["user_sha256"]:
            self.by_user.setdefault(user_sha, {})[digest(token.encode())] = name
            if self.origin == "probe":
                self.probe_fingerprints.append(digest(json_bytes([
                    user_sha, record["system_sha256"],
                    [{key: attempt[key] for key in ("reply", "failure_stage")} for attempt in attempts],
                ])))
        self.attempt_count += len(attempts)
        for attempt in attempts:
            for key in USAGE_KEYS:
                if type(attempt[key]) is int:
                    self.usage_counts[key] += 1
                    self.usage_totals[key] += attempt[key]
        return any(_interrupted_with_text(attempt) for attempt in attempts)

    def complete(self, report_digest: str, threshold: int) -> None:
        origin_run_id = self.origin_run_id
        if self.origin == "probe":
            origin_run_id = digest(json_bytes(sorted(self.probe_fingerprints)))
        self.output.write(MANIFEST, json_bytes({
            "schema": 1, "run_id": self.run_id, "origin": self.origin, "origin_run_id": origin_run_id,
            "replaces_run_id": self.replaces_run_id,
            "source_code_measured": self.origin == "live", "model": self.model_id, "configuration": self.config,
            "threshold": threshold,
            "artifact_sha256": self.artifacts, "documents_by_user_sha256": self.by_user,
            "public_report_sha256": report_digest,
        }))

    def public_usage(self) -> dict:
        return {
            "attempts": self.attempt_count,
            **{key: {
                "captured_attempts": self.usage_counts[key],
                "total": self.usage_totals[key] if self.usage_counts[key] else None,
            } for key in USAGE_KEYS},
        }
