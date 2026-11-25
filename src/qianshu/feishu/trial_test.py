"""#154 段 3：首张【测试】单在建单开关关闭时的唯一例外判定（守望者 G505 / G514 / G515）。

出卡、2b 审批门禁与扫描、2c 存档、2d 建单都只调用 `trial_test_exception` 这一个函数，不另写分支。

- 授权来源：明道云配置同目录的私有授权登记 `create-authorizations.json` 中 `is_test` 为真的条目，
  条目键即授权登记号；条目写明收信登记号与版本（`trial_registration`、`trial_version`）、【测试】名称
  （`test_name`）、精确组合（`policy_type`、`benefit_type`）、UTC 截止（`expires`）与产品负责人回执
  （`owner_receipt`）。
- 卡片标识 = 收信登记号 × 版本（G515）。出卡时命中一条有效测试授权，才把授权登记号冻结进卡片快照
  （`test_authorization`）并在卡面回显；同一授权、同一「登记 × 版本」至多绑定一张卡（`trial_test_binding`）。
- 一次性：X8a 存档认领时调用 `TrialRegistration.consume`，存档与建单共用这一次资格；此后绑定状态只前进
  （bound → consumed → sending → verified / closed），失败、到期或发送尝试后不再放行。
- 任何读不准（登记文件不可读、绑定缺失、截止不可解析）一律不放行。
"""

import json
from pathlib import Path

from qianshu.mingdao.create_order import TrialRegistration, _PREFIX, decode, digest, private_bytes, receipt, utc

AUTHORIZATIONS_FILE = "create-authorizations.json"
SNAPSHOT_KEY = "test_authorization"
READY = "测试单回读一致，可由守望者登记组合"
# 各阶段接受的绑定状态。issue 为出卡（尚无绑定）；active 供审批扫描判断「例外流程在途、不按关开关收口」。
STAGES = {"approve": ("bound",), "archive": ("bound",), "active": ("bound", "consumed", "sending"),
          "order": ("consumed",), "handoff": ("sending",)}
_FIELDS = ("trial_registration", "trial_version", "test_name", "policy_type", "benefit_type", "expires",
           "owner_receipt")


def registry_of(directory):
    """明道云配置目录下的测试授权登记路径；未配置明道云返回 None。"""
    return Path(directory) / AUTHORIZATIONS_FILE if directory is not None else None


def test_entries(registry):
    """读出 is_test 为真的条目。不可读或格式不符时抛出，由调用方按不放行处理。"""
    data = decode(private_bytes(registry))
    if not isinstance(data, dict):
        raise ValueError("测试授权登记格式不符")
    return {key: entry for key, entry in data.items() if isinstance(entry, dict) and entry.get("is_test") is True}


def has_test_entries(registry):
    """装配门槛：登记可读且至少有一条测试授权。具体某张卡能否放行只由 trial_test_exception 判定。"""
    try:
        return registry is not None and bool(test_entries(registry))
    except Exception:
        return False


def _closed_path(directory, key):
    return directory / ("trial-closed-" + digest(key.encode()) + ".json")


def _directory(db):
    return Path(db.execute("PRAGMA database_list").fetchone()[2]).parent


def _value(reviewed, name):
    field = reviewed.get(name)
    return field.get("value") if isinstance(field, dict) else None


def trial_test_exception(db, registry, *, registration, version, card, snapshot, now, stage,
                         policy=None, benefit=None):
    """唯一例外判定。放行返回当次 TrialRegistration，否则返回 None（含任何读不准的情况）。

    G505 六条：is_test、有产品负责人回执、名称以【测试】开头且与登记逐字相同、精确组合、UTC 截止未过、未消耗；
    G515 双向核对：卡片快照里的授权登记号等于本次绑定的授权，「登记 × 版本」等于授权条目里那一组。
    policy / benefit 供建单阶段传入实际取值，须与卡面及授权同时一致。
    """
    try:
        if registry is None or type(registration) is not int or type(version) is not int:
            return None
        entries = test_entries(registry)
        reviewed = snapshot.get("reviewed")
        if not isinstance(reviewed, list) or len(reviewed) != 1 or not isinstance(reviewed[0], dict):
            return None
        if stage == "issue":
            matched = [key for key, entry in entries.items()
                       if entry.get("trial_registration") == registration and entry.get("trial_version") == version]
            if len(matched) != 1:
                return None
            key, status = matched[0], None
            if db.execute("""SELECT 1 FROM trial_test_binding WHERE authorization=?
                          OR (registration=? AND version=?)""", (key, registration, version)).fetchone():
                return None  # 同一授权或同一「登记 × 版本」已绑定过一张卡：重发、补发、重出一律不绑定
        else:
            key = snapshot.get(SNAPSHOT_KEY)
            if not receipt(key) or snapshot.get("items") != [1] or snapshot.get("eligible") != [1]:
                return None
            binding = db.execute("""SELECT card, registration, version, status FROM trial_test_binding
                                 WHERE authorization=?""", (key,)).fetchone()
            if (binding is None or binding[0] != card or (binding[1], binding[2]) != (registration, version)
                    or binding[3] not in STAGES[stage]):
                return None
            status = binding[3]
        entry = entries.get(key)
        if (not isinstance(entry, dict) or not all(field in entry for field in _FIELDS)
                or (entry["trial_registration"], entry["trial_version"]) != (registration, version)):
            return None
        grant = TrialRegistration(key, entry["owner_receipt"], entry["test_name"], entry["policy_type"],
                                  entry["benefit_type"], entry["expires"])
        name = _value(reviewed[0], "policy_name")
        shown = (_value(reviewed[0], "policy_type"), _value(reviewed[0], "benefit_type"))
        if not (all(receipt(value) for value in (grant.owner_receipt, grant.test_name, grant.policy, grant.benefit))
                and grant.test_name.startswith(_PREFIX) and name == grant.test_name
                and shown == (grant.policy, grant.benefit)
                and (policy is None or policy == grant.policy) and (benefit is None or benefit == grant.benefit)
                and now < utc(grant.expires).timestamp()):
            return None
        # 未消耗：消耗前的阶段（出卡、审批、存档认领）另核一次性关闭记录不存在。
        if status in (None, "bound") and _closed_path(_directory(db), key).exists():
            return None
        return grant
    except Exception:
        return None


def consume(db, grant, snapshot, now):
    """存档认领事务内一次性消耗资格（X8a 与建单共用）。调用方持有写事务。失败时登记已永久关闭并抛出。"""
    reviewed = snapshot["reviewed"][0]
    try:
        grant.consume(_directory(db), _value(reviewed, "policy_name"), _value(reviewed, "policy_type"),
                      _value(reviewed, "benefit_type"), now)
    except Exception:
        db.execute("UPDATE trial_test_binding SET status='closed' WHERE authorization=? AND status='bound'",
                   (grant.registration,))
        raise
    if not advance(db, snapshot, "bound", "consumed"):
        raise ValueError("测试授权绑定已变化")


def close(db, snapshot):
    """门禁失败、未发出或到期：同一登记持久关闭（TrialRegistration.close）。读不准时也已无法再放行。"""
    key = snapshot.get(SNAPSHOT_KEY) if isinstance(snapshot, dict) else None
    if not receipt(key):
        return
    db.execute("UPDATE trial_test_binding SET status='closed' WHERE authorization=? AND status!='verified'", (key,))
    try:
        TrialRegistration(key, "", "", "", "", "").close(_directory(db))
    except Exception:
        pass  # 绑定状态已关闭，资格不会再放行


def advance(db, snapshot, before, after):
    """绑定状态只前进；返回是否命中。"""
    key = snapshot.get(SNAPSHOT_KEY) if isinstance(snapshot, dict) else None
    if not receipt(key):
        return False
    return db.execute("UPDATE trial_test_binding SET status=? WHERE authorization=? AND status=?",
                      (after, key, before)).rowcount == 1


def bound(snapshot):
    return isinstance(snapshot, dict) and SNAPSHOT_KEY in snapshot


def card_exception(db, registry, row, now, stage, **extra):
    """按登记库中的卡片行调用唯一判定。"""
    try:
        snapshot = json.loads(row["snapshot"])
    except Exception:
        return None
    return trial_test_exception(db, registry, registration=row["registration"], version=row["version"],
                                card=row["card"], snapshot=snapshot, now=now, stage=stage, **extra)


def active_test_card(db, registry, now):
    """当前有效测试卡（供子进程状态帧）：已绑定、卡片仍待审批、判定放行。恰好一张才返回（登记、版本、授权登记号、卡）。"""
    try:
        if registry is None or not db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='trial_test_binding'").fetchone():
            return None
        found = []
        for row in db.execute("""SELECT c.* FROM trial_test_binding b JOIN trial_card c USING(card)
                              WHERE b.status='bound' AND c.status='open'""").fetchall():
            grant = card_exception(db, registry, row, now, "approve")
            if grant is not None:
                found.append((row["registration"], row["version"], grant.registration, row["card"]))
        return found[0] if len(found) == 1 else None
    except Exception:
        return None


def snapshot_exception(path, registry, snapshot, now, stage, **extra):
    """建单规划只拿到卡片快照：按快照里的授权登记号找到绑定的卡，快照须与该卡登记的快照逐字相同，再调用唯一判定。"""
    from .trial_approval import journal_at
    try:
        key = snapshot.get(SNAPSHOT_KEY)
        with journal_at(path) as j:
            row = j.db.execute("""SELECT c.* FROM trial_test_binding b JOIN trial_card c USING(card)
                               WHERE b.authorization=?""", (key,)).fetchone()
            if row is None or json.loads(row["snapshot"]) != snapshot:
                return None
            return card_exception(j.db, registry, row, now, stage, **extra)
    except Exception:
        return None
