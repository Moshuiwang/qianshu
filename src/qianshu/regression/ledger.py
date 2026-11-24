"""私有 Trace 台账。在同一个锁内检查预算和并发、持久化提交意图。"""

import fcntl
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .storage import RegressionError, digest, json_bytes, read_json


@contextmanager
def locked_directory(files, path):
    files._check_ignore(Path(".private") / path)
    files._check_ignore(Path(".private") / (str(path) + ".lock"))
    with files._directory(path.parent) as folder:
        fd = os.open(path.name + ".lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=folder)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RegressionError("台账锁文件无效。")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield folder
        finally:
            os.close(fd)


@contextmanager
def transaction(files, path):
    with locked_directory(files, path) as folder:
        try:
            original = files.read(path)
        except FileNotFoundError:
            original = None
        data = {} if original is None else read_json(original)
        yield data
        raw = json_bytes(data)
        if raw == original:
            return
        with files._directory(path.parent) as checked:
            if (not os.path.samestat(os.fstat(folder), os.fstat(checked))
                or not os.path.samestat((files.repo / ".private").lstat(), os.fstat(files.root))):
                raise RegressionError("台账目录在运行期间发生了变化。")
        temp = ".regression-" + uuid4().hex
        files._check_ignore(Path(".private") / path.parent / temp)
        handle = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=folder)
        try:
            with os.fdopen(handle, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, path.name, src_dir_fd=folder, dst_dir_fd=folder)
            os.fsync(folder)
        finally:
            try:
                os.unlink(temp, dir_fd=folder)
            except FileNotFoundError:
                pass


class Ledger:
    def __init__(self, files, path):
        self.files = files
        self.path = path

    @classmethod
    def for_trace(cls, files, argument, stack):
        # 工作树的 Git 公共目录固定了主检出，避免不同工作目录各自开账。
        git_dir = subprocess.check_output(
            ["git", "-C", files.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            stderr=subprocess.DEVNULL).decode().strip()
        shared = stack.enter_context(type(files)(Path(git_dir).parent).open())
        canonical = Path("regression-ledger.json")
        if shared.argument(argument) != canonical:
            raise RegressionError("Trace 只使用主检出的固定台账。")
        return cls(shared, canonical)

    def journal(self, run_id):
        return self.path.with_name("batch-" + digest(run_id.encode()) + ".json")

    def evidence(self):
        return {"path": str(self.files.repo / ".private" / self.path),
                "sha256": digest(self.files.read(self.path))}

    def check_prior_intents(self, run_id, runs):
        # 旧版本可任选台账位置，每份事务文件都有同目录的永久锁。
        journal = self.journal(run_id).name
        known_batches = {self.journal(key).stem for key, row in runs.items() if row["mode"] == "batch"}
        for directory, _, names, folder in os.fwalk(".", dir_fd=self.files.root):
            if journal in names:
                raise RegressionError("运行号已有批次日志，禁止再次提交。")
            names = set(names)
            for name in names:
                if not name.endswith(".lock") or name[:-5] not in names:
                    continue
                candidate = Path(directory) / name[:-5]
                if candidate == self.path:
                    continue
                batch_file = re.fullmatch(r"(batch-[a-f0-9]{64})(?:-[a-f0-9]{64})?\.json", candidate.name)
                if candidate.parent == self.path.parent and batch_file and batch_file[1] in known_batches:
                    continue
                # 旧版未给自定义台账打标。只按旧事务的文件与锁元数据识别
                # 候选后停下对账，避免为辨认台账而打开无关私有文件。
                lock = os.stat(name, dir_fd=folder, follow_symlinks=False)
                target = os.stat(name[:-5], dir_fd=folder, follow_symlinks=False)
                if (stat.S_ISREG(lock.st_mode) and lock.st_size == 0 and lock.st_mode & 0o077 == 0
                    and stat.S_ISREG(target.st_mode) and target.st_mode & 0o077 == 0):
                    raise RegressionError("发现旧位置的事务文件，须先核对历史费用与提交意图。")

    def carry_recording(self, manifest, manifest_sha, usage, estimate):
        """登记启用台账前已发生的真跑，原录制及其重放不能重复核销。"""
        if manifest["origin"] != "live" or not manifest["source_code_measured"]:
            raise RegressionError("历史费用须引用原始真跑录制。")
        run_id = manifest["run_id"]
        amount = Decimal(estimate) * Decimal("1.3")
        if not amount.is_finite() or amount <= 0:
            raise RegressionError("历史预估费用须为正数。")
        with transaction(self.files, self.path) as runs:
            if run_id in runs:
                if runs[run_id].get("source_sha256") != manifest_sha:
                    raise RegressionError("运行号已有登记，请核对原始台账。")
                runs[run_id]["active"] = False
                return
            self.check_prior_intents(run_id, runs)
            missing = usage["known_usd"] is None or bool(usage["missing_attempts"])
            runs[run_id] = {"source_sha256": manifest_sha, "identity": manifest_sha, "mode": usage["mode"],
                            "reserved_usd": str(amount), "spent_usd": usage["known_usd"] or "0",
                            "committed_usd": str(amount) if missing else "0", "active": False}

    def reserve(self, run_id, mode, estimate, identity, **context):
        amount = Decimal(estimate) * Decimal("1.3")
        if not amount.is_finite() or amount <= 0:
            raise RegressionError("预估费用须为正数。")
        with transaction(self.files, self.path) as runs:
            if run_id in runs:
                raise RegressionError("运行号已有提交意图，禁止再次提交。")
            self.check_prior_intents(run_id, runs)
            committed = sum(Decimal(row["spent_usd"]) + Decimal(row["committed_usd"]) for row in runs.values())
            slots = sum(row["active"] and row["mode"] == mode for row in runs.values())
            if committed + amount > 110 or slots >= {"instant": 2, "batch": 3}[mode]:
                raise RegressionError("预算或并发名额不足。")
            runs[run_id] = {**context, "reserved_usd": str(amount), "spent_usd": "0", "committed_usd": str(amount),
                            "active": True, "mode": mode, "identity": identity}

    def reserve_correction(self, run_id, identity):
        with transaction(self.files, self.path) as runs:
            row = runs[run_id]
            if intent := row.get("shape_retry"):
                if intent["identity"] != identity:
                    raise RegressionError("纠错提交意图与原请求不符。")
                return  # 预占已落盘，批次日志负责阻止重复 POST。
            # 首轮已收齐，只保留未知用量的预占。纠错沿用原登记估算，
            # 避免续取命令改变估算；两轮共用同一个运行名额。
            amount = Decimal(row["reserved_usd"])
            first_hold = amount if any(item.get("missing", True) for item in row["item_costs"].values()) else 0
            used = sum(Decimal(r["spent_usd"]) + Decimal(r["committed_usd"]) for r in runs.values())
            allowed = used - Decimal(row["committed_usd"]) + first_hold + amount <= 110
            slots = sum(r["active"] and r["mode"] == "batch" for r in runs.values())
            allowed = allowed and (row["active"] or slots < 3)
            row.update(active=allowed, committed_usd=str(first_hold + amount if allowed else first_hold))
            if allowed:
                row["shape_retry"] = {"identity": identity, "reserved_usd": str(amount),
                                      "first_spent_usd": row["spent_usd"], "first_hold_usd": str(first_hold)}
        if not allowed:
            raise RegressionError("纠错预算或并发名额不足。")

    def record_batch_item(self, run_id, key, checksum, cost, *, correction=False):
        # 费用先于原始行落盘。中途退出后，续取同一行只补齐结果文件。
        with transaction(self.files, self.path) as runs:
            row = runs[run_id]
            items = row.setdefault("item_costs", {})
            if key in items:
                if items[key]["sha256"] != checksum:
                    raise RegressionError("同一批量结果的字节冲突。")
                if "missing" in items[key]:
                    return
            items[key] = {"sha256": checksum, "usd": cost["known_usd"] or "0",
                          "missing": cost["known_usd"] is None or bool(cost["missing_attempts"])}
            if correction:
                items[key]["correction"] = True
            total = sum(Decimal(item["usd"]) for item in items.values())
            # 旧版可能已登记整批费用，续取期间保留该已知金额。
            spent = max(total, Decimal(row["spent_usd"]))
            reserve = Decimal(row["reserved_usd"])
            if retry := row.get("shape_retry"):
                retry_spent = spent - Decimal(retry["first_spent_usd"])
                remaining = Decimal(retry["reserved_usd"])
                if not any(item["missing"] for item in items.values() if item.get("correction")):
                    remaining = max(remaining - retry_spent, 0)
                reserve = Decimal(retry["first_hold_usd"]) + remaining
            elif not any(item.get("missing", True) for item in items.values()):
                reserve = max(reserve - spent, 0)
            row.update(spent_usd=str(spent), committed_usd=str(reserve), active=True)

    def record_cost(self, run_id, cost):
        known = cost["known_usd"]
        self.settle(run_id, Decimal(known or "0"), known is None or cost["missing_attempts"])

    def settle(self, run_id, cost, missing):
        with transaction(self.files, self.path) as runs:
            row = runs[run_id]
            row["spent_usd"] = str(cost)
            if retry := row.get("shape_retry"):
                hold = Decimal(retry["first_hold_usd"])
                if any(item["missing"] for item in row["item_costs"].values() if item.get("correction")):
                    hold += Decimal(retry["reserved_usd"])
                row["committed_usd"] = str(hold)
            else:
                row["committed_usd"] = row["reserved_usd"] if missing else "0"
            row["active"] = False
