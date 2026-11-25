"""生产子进程的离线启动夹具。继承真实端点、内存限制、父亡信号及租约。"""

import asyncio
import builtins
import ctypes
import logging
import json
import os
import resource
import signal
import socket
import sys
import threading
import time
from pathlib import Path


def mark(name):
    with Path("child-events").open("a") as stream:
        stream.write(name + "\n")


def no_network(event, args):
    if (event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr")
            or event == "socket.connect" and args[0].family in (socket.AF_INET, socket.AF_INET6)):
        mark("external")
        raise AssertionError("离线子进程禁止拨号")


def boot():
    sys.addaudithook(no_network)
    source = Path("candidate").read_text()
    sys.path.insert(0, source)
    # 夹具装上合成边界后，执行同一个隔离脚本入口及端点鉴别。
    import runpy
    def execute_cli():
        runpy.run_path(str(Path(source) / "qianshu/__main__.py"), run_name="__main__")

    from contextlib import contextmanager
    from unittest.mock import patch

    case = Path.cwd()
    fault = (case / "scenario").read_text()
    sys.path.append(str(Path(sys.executable).absolute().parents[1] / "lib"
                        / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"))
    if fault == "import" and (case / "fault-on").exists():
        real_import = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == "feishu.trial_consumer":
                (case / "hit").touch()
                raise ImportError("合成导入故障")
            return real_import(name, *args, **kwargs)
        with patch.object(builtins, "__import__", importing):
            return execute_cli()
    import gc
    # 仅回放夹具：载入依赖期间暂停循环垃圾回收以缩短用例耗时，生产入口不做此处理。
    gc.disable()
    try:
        from qianshu.feishu import trial_consumer, trial_listener
        from qianshu.feishu.trial_approval import Journal
        from qianshu.mail import trial
        import httpx
    finally:
        gc.enable()

    native = trial_consumer.Consumer
    fail = lambda: (case / "fault-on").exists()

    def hit():
        (case / "hit").touch()
        raise ValueError("合成故障")

    class Consumer(native):
        def __init__(self, *args):
            if fail() and fault == "recursion":
                def recurse():
                    return recurse()
                (case / "hit").touch()
                recurse()
            if fail() and (fault == "constructor" or
                           fault in ("read_disk", "native_gil", "malformed") and (case / "hit").exists()):
                hit()
            super().__init__(*args)
            if fault in ("patch_no_checker", "first_no_checker", "recovery_no_checker"):
                self.listener.transport.before_send = None

        def consume(self, payload, deadline):
            if fault == "hold_consume" and not (case / "release").exists():
                (case / "hit").touch()
                while not (case / "release").exists() and self.connected():
                    time.sleep(.01)
            if fault == "reconnect" and not (case / "hit").exists():
                (case / "hit").touch()
                while self.connected():
                    time.sleep(.01)
            if fail() and fault == "malformed":
                (case / "hit").touch()
                payload = b"{"
            if fail() and fault == "native_gil":
                (case / "hit").touch()
                ctypes.PyDLL(None).sleep(30)
            if fail() and fault == "kill_consumer":
                (case / "hit").touch()
                os.kill(os.getpid(), signal.SIGKILL)
            if fail() and fault == "read_disk":
                # 在实际开关文件的读入口暂停。记录阻塞时长，再使本代退出。
                original_open = Path.open
                def open_file(path, *args, **kwargs):
                    if path == self.args.trial_switches:
                        started = time.monotonic()
                        time.sleep(3.4)
                        (case / "read-seconds").write_text(str(time.monotonic() - started))
                        hit()
                    return original_open(path, *args, **kwargs)
                with patch.object(Path, "open", open_file):
                    self.switches()
            return super().consume(payload, deadline)

        def tick(self):
            if fail() and fault.startswith("close_") and (case / "mail-started").exists():
                raise RuntimeError("合成停用")
            super().tick()

    def publish_registration(reg):
        pending = case / "mail-started.next"
        pending.write_text(str(reg))
        pending.replace(case / "mail-started")
        pending.write_text(json.dumps([os.getpid(), reg]))
        pending.replace(case / "mail-ready")

    def mail_loop(worker):
        if fail() and fault == "mail_poll":
            hit()
        store = worker.store
        session = Journal(store.db).session()
        if fault.startswith("first_"):
            if not store.row("SELECT 1 FROM budget"):
                store.import_spend("0")
            with store.write() as db:
                db.execute("INSERT OR IGNORE INTO mailbox(scope,state) VALUES(?,'active')", (worker.scope,))
                entry = db.execute("""INSERT INTO registration(scope,version,status,attachments)
                                      VALUES(?,1,'parse',0)""", (worker.scope,)).lastrowid
            worker.parse(store.row("SELECT * FROM registration WHERE id=?", entry), 1, "")
            publish_registration(entry)
            worker.stop.wait()
            return
        with store.write() as db:
            db.execute("INSERT INTO mailbox(scope,state) VALUES(?,'active')", (session,))
            reg = db.execute("SELECT COALESCE(max(id),0)+1 FROM registration").fetchone()[0]
            db.execute("""INSERT INTO registration(id,scope,version,status,attachments)
                       VALUES(?,?,1,'done',0)""", (reg, session))
        card = f"card{reg}"
        j = Journal(store.db)
        now = time.time() - .05
        j.prepare(card, reg, 1, session, "区域甲", dict(preview="sample", eligible=[1], items=[1], missing=[],
                  contact="ou_config", pages=[{"elements": []}]), False, now)
        with store.write() as db:
            db.execute("INSERT INTO card_intent VALUES(?,?,1,1,'sent','')", (card, reg))
            j.received(card, f"msg{reg}")
            j.bind(card, now)
        if fault == "kill_consumer" and not (case / "hit").exists():
            with store.write() as db:
                # 模拟前代已登记的尝试、已知记录，以及未完成绑定的一页。
                for status in ("create_unknown", "readback_pending", "partial"):
                    nr = db.execute("""INSERT INTO registration(scope,version,status,attachments)
                                       VALUES(?,1,'done',0)""", (session,)).lastrowid
                    key = f"prepared{nr}"
                    source = db.execute("SELECT * FROM trial_card WHERE card=?", (card,)).fetchone()
                    db.execute("""INSERT INTO trial_card VALUES(?,?,1,1,?,?,?,?,0,?,?,?)""",
                               (key, nr, session, source["country"], source["snapshot"], source["fingerprint"],
                                now, source["expires"], "partial" if status == "partial" else "accepted"))
                    if status == "partial":
                        db.execute("INSERT INTO card_intent VALUES(?,?,1,1,'unknown','')", (key, nr))
                    else:
                        db.execute("INSERT INTO trial_item_task VALUES(?,1,1,'formal',?,?)", (nr, key, status))
                    if status == "readback_pending":
                        db.execute("INSERT INTO trial_known_record VALUES(?,1,'offline-record')", (key,))
        publish_registration(reg)
        worker.stop.wait()

    original = trial_consumer.running

    @contextmanager
    def running(*args):
        mark("start")
        facts = case / "child-facts.next"
        facts.write_text(json.dumps([os.getpid(), os.getpgrp(), resource.getrlimit(resource.RLIMIT_AS)[0]]))
        facts.replace(case / "child-facts")
        if fail() and fault in ("startup", "child_log", "pool_exit", "crash"):
            (case / "hit").touch()
            if fault == "startup":
                threading.Event().wait()
            elif fault == "child_log":
                logger = logging.getLogger("approval")
                logger.handlers[0].emit = lambda record: threading.Event().wait()
                logger.critical("CRITICAL E-TRIAL-DISABLED 1")
            elif fault == "pool_exit":
                async def stuck():
                    asyncio.get_running_loop().run_in_executor(None, threading.Event().wait)
                asyncio.run(stuck())
            else:
                os.kill(os.getpid(), signal.SIGKILL)
        if fail() and fault == "memory":
            (case / "hit").touch()
            bytearray(resource.getrlimit(resource.RLIMIT_AS)[0])
            raise AssertionError("合成分配应超过子进程地址空间上限")
        with original(*args) as consumer:
            yield consumer

    def http(request):
        path = request.url.path
        if path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "fake-token", "expire": 7200})
        if path.endswith("/members"):
            mark("member-lookup")
            if fault in ("lookup_failure", "feedback_bytes"):
                raise TimeoutError
            # 此端点只返回即刻群成员。此前加入、退出及再次加入均不会随查询交付。
            history = {"joined_late": [False, True], "rejoined": [True, False, True],
                       "current_absent": [True, False]}.get(fault, [True])
            body = {"has_more": False, "items": [] if not history[-1] else
                    [{"member_id_type": "open_id", "member_id": "ou_actor"}]}
        elif request.method == "PATCH":
            mark("patch")
            if fault in ("patch_fail", "patch_timeout", "notice_timeout"):
                raise (OSError if fault == "patch_fail" else TimeoutError)()
            if fault == "patch_lost":
                return httpx.Response(200, content=b"")
            body = {}
        elif path.endswith("/messages"):
            content = json.loads(json.loads(request.content)["content"])
            if json.loads(request.content)["msg_type"] == "interactive":
                mark("interactive")
                if fault in ("first_timeout", "first_fail"):
                    raise (TimeoutError if fault == "first_timeout" else OSError)()
            else:
                manual = content.get("text") == trial_listener.MANUAL_NOTICE
                mark("manual" if manual else "notice")
                if fault == "kill_consumer":
                    text = content["text"]
                    assert "offline-record" not in text
                    for phrase, tag in (("未建单", "unsent"), ("结果未知", "uncertain"),
                                        ("已有记录待回读", "readback")):
                        if phrase in text:
                            assert '<at user_id="ou_config">' in text
                            mark(tag)
                if manual and fault == "notice_timeout":
                    raise TimeoutError
            body = {"message_id": "sent"}
        else:
            raise AssertionError("未登记的合成 HTTP")
        return httpx.Response(200, json={"code": 0, "data": body})

    real_client = httpx.AsyncClient
    import sqlite3
    real_connect = sqlite3.connect

    class CloseFault(sqlite3.Connection):
        def close(self):
            if fail() and fault.startswith("close_"):
                (case / "hit").touch()
                if fault == "close_hang":
                    threading.Event().wait()
                raise OSError
            return super().close()

    class DelayedCommit(sqlite3.Connection):
        candidate = False

        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT INTO trial_click_candidate"):
                self.candidate = parameters[2] == ("approve" if fault == "late_approve" else "verify")
            return super().execute(sql, parameters)

        def __exit__(self, *exc):
            if self.candidate and exc[0] is None:
                self.candidate = False
                if fault.startswith("late_"):
                    time.sleep(2.65)
                result = super().__exit__(*exc)
                (case / "committed").touch()
                if fault == "kill_after_commit":
                    os.kill(os.getpid(), signal.SIGKILL)
                return result
            return super().__exit__(*exc)

    def db_connect(*args, **kw):
        if threading.current_thread().name == "trial-mail":
            kw["factory"] = CloseFault
        elif kw.get("uri") and (fault.startswith("late_") or fault == "kill_after_commit"):
            kw["factory"] = DelayedCommit
        return real_connect(*args, **kw)

    with (patch.object(trial_consumer, "Consumer", Consumer), patch.object(trial_consumer, "running", running),
          patch.object(trial.Trial, "run", mail_loop), patch.object(sqlite3, "connect", db_connect),
          patch.object(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(http), **kw))):
        from contextlib import ExitStack
        with ExitStack() as faults:
            from qianshu.feishu import sdk
            build_request = sdk._OrderHTTP.request
            def final_bytes(obj, client, model, purpose, token=None):
                request = build_request(obj, client, model, purpose, token)
                target = fault == "patch_bytes" and purpose == "patch"
                if purpose == "message":
                    body = json.loads(request.content)
                    target = ((fault == "first_bytes" and body["msg_type"] == "interactive")
                              or (fault == "feedback_bytes" and trial_listener.MANUAL_NOTICE
                                  in json.loads(body["content"]).get("text", ""))
                              or (fault == "recovery_bytes" and "未建单" in
                                  json.loads(body["content"]).get("text", "")))
                if target:
                    body = json.loads(request.content)
                    body["content"] = json.dumps({"text": "outlet-sentinel-7"})
                    request = client.build_request(request.method, request.url, json=body,
                                                   headers={"Authorization": "Bearer " + token})
                    request.extensions["order_approval"] = purpose, token
                    mark("tampered")
                return request
            faults.enter_context(patch.object(sdk._OrderHTTP, "request", final_bytes))
            if fail() and fault == "members":
                from qianshu import config
                parser = config._approval_switches
                def malformed(values, result):
                    (case / "hit").touch()
                    return parser(dict(values, MAIL_TRIAL_APPROVER_OPEN_IDS=","), result)
                faults.enter_context(patch.object(config, "_approval_switches", malformed))
            if fail() and fault == "stop_event":
                original_event = threading.Event
                def stop_event():
                    if sys._getframe(1).f_code.co_name == "_polling_worker":
                        hit()
                    return original_event()
                faults.enter_context(patch.object(threading, "Event", stop_event))
            if fail() and fault == "maintain":
                faults.enter_context(patch.object(trial_listener.Listener, "maintain", lambda *_: hit()))
            if fail() and fault == "rotate":
                faults.enter_context(patch.object(Journal, "rotate", lambda *a, **kw: hit()))
            if fail() and fault == "dispatcher":
                builder = type(trial_consumer.lark.EventDispatcherHandler.builder("", ""))
                faults.enter_context(patch.object(builder, "register_p2_card_action_trigger", lambda *_: hit()))
            return execute_cli()


if __name__ == "__main__":
    raise SystemExit(boot())
