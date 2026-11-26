"""守望者按开放移交请求只读查重。官方鉴权与请求仅在审批进程内使用。"""

import json

from qianshu.mingdao import create_order as O

from .trial_approval import journal_at
from .trial_order import _card_snapshot, _reviewed_name, group_of, name_fingerprint, name_key, robot_held


EXACT_NAME_MISS = "精确名未命中（全半角 / 空白变体未覆盖，需人工比对）"


def binding(db, registration, version, fingerprint):
    card, snapshot = _card_snapshot(db, registration, version)
    groups = group_of(snapshot or {})
    members = set()
    keys = set()
    for item, key in db.execute("""SELECT item, name_key FROM trial_handoff
            WHERE registration=? AND version=? AND cleared IS NULL""", (registration, version)):
        if name_fingerprint(key) == fingerprint:
            keys.add(key)
            members.update(groups.get(item, [item]))
    if len(keys) != 1 or not card:
        raise ValueError("未找到唯一开放移交")
    key = keys.pop()
    names = tuple(dict.fromkeys(_reviewed_name(snapshot, item) for item in sorted(members)))
    if not key or any(name_key(name) != key for name in names):
        raise ValueError("快照政策名未确认")
    return key, card, sorted(members), names


def status(db, *, registration=None, request_id=None):
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='trial_dedupe'").fetchone():
        return []
    rows = db.execute("""SELECT id, registration, version, name_fp, status, matches, order_states, robot_record
                         FROM trial_dedupe WHERE (? IS NULL OR registration=?) AND (? IS NULL OR id=?) ORDER BY id""",
                      (registration, registration, request_id, request_id))
    results = []
    for row in rows:
        result = dict(row)
        # 旧结论无法证明当次查询有分页结束信号。
        if result["status"] == "无同名":
            result.update(status="查重失败", matches=None)
        if result["order_states"] is not None:
            result["order_states"] = json.loads(result["order_states"])
            result["robot_record"] = bool(result["robot_record"])
        results.append(result)
    return results


def request(journal, registration, version, fingerprint, reason):
    db = journal.db
    with db:
        db.execute("BEGIN IMMEDIATE")
        binding(db, registration, version, fingerprint)
        journal.initialize()
        request_id = db.execute("""INSERT INTO trial_dedupe(registration,version,name_fp,reason,status)
            VALUES(?,?,?,?,'待查重')""", (registration, version, fingerprint, reason)).lastrowid
    return status(db, request_id=request_id)[0]


def official_matches(config_path, settings, sensitive, names):
    if not settings.trial_credentials_complete:
        raise ValueError("查重凭据未确认")
    config = O.decode(O.private_bytes(config_path))
    official = config["official"]
    O.check_search_binding(config, official)
    credentials = dict(settings.trial_credentials)
    token_key, app_key = official["token_key"], official["app_id_key"]
    token, app = credentials[token_key], credentials[app_key]
    if token_key == app_key or min(len(token.strip()), len(app.strip())) < 8:
        raise O.OutletConflict()
    guard = O.Outlet()
    guard.configure(credentials)
    guard.configure({("private", i): value for i, value in enumerate(sensitive)
                     if value not in credentials.values()})
    guard.configuration_checked = True
    scope = O._outlet.set(guard)
    try:
        ids = set()
        with O.official_reader(official, token, app) as api:
            for name in names:
                ids.update(record.lower() for record in api.find(name, fold=name_key, require_page_signal=True))
        return len(ids)
    finally:
        O._outlet.reset(scope)


def recover(path):
    """审批租约的新持有者接手时，上一进程已领取的请求只能记失败。"""
    with journal_at(path) as j, j.db:
        j.db.execute("UPDATE trial_dedupe SET status='查重失败' WHERE status='查重中'")


def scan(path, lookup):
    """先持久领取再查，网络调用期间释放登记库写锁。"""
    with journal_at(path) as j:
        db = j.db
        if not db.execute("SELECT 1 FROM trial_dedupe WHERE status='待查重' LIMIT 1").fetchone():
            return
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM trial_dedupe WHERE status='待查重' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return
            try:
                key, card, members, names = binding(db, row["registration"], row["version"], row["name_fp"])
            except (ValueError, KeyError, TypeError, IndexError):
                db.execute("UPDATE trial_dedupe SET status='查重失败' WHERE id=?", (row["id"],))
                return
            states = [r[0] for r in db.execute(
                "SELECT status FROM trial_order WHERE name_key=? ORDER BY sent", (key,))]
            held = robot_held(db, key, card, members)
            db.execute("UPDATE trial_dedupe SET status='查重中', order_states=?, robot_record=? WHERE id=?",
                       (json.dumps(states), held, row["id"]))
        try:
            count = lookup(names)
            if type(count) is not int or count < 0:
                raise ValueError("官方查重计数不明")
            conclusion = "平台有同名" if count else EXACT_NAME_MISS
        except Exception:
            conclusion, count = "查重失败", None
        with db:
            db.execute("UPDATE trial_dedupe SET status=?, matches=? WHERE id=? AND status='查重中'",
                       (conclusion, count, row["id"]))
