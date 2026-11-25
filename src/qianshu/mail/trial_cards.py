"""同次解析的草稿、逐条放行结论和实际送出的预览。"""

import re
from dataclasses import asdict
from datetime import date
from hashlib import sha256
import json
from uuid import uuid4

from qianshu.draft import SourceExcerpt
from qianshu.feishu.archive import ArchivePolicy, VerifiedArchiveBatch, _FIELDS, preview_archive_batch
from qianshu.feishu.cards import CardLine, detail_pages, result_card
from qianshu.feishu.trial_approval import ARCHIVE_NOTICES, CARD_FORMAT, Journal
from qianshu.mingdao.create_order import UnverifiedCombination, require_verified_combination
from qianshu.parse.evidence import verified_value
from qianshu.pipeline import _FIELD_LABELS


def confirmed_tag_dates(snapshot):
    """R8 只接受同名唯一记录，且该记录的所有快照行均有同一截止日。"""
    tags = snapshot.get("tags", [])
    names, dates = {}, {}
    if not isinstance(tags, list):
        return {}
    for item in tags:
        if not isinstance(item, dict):
            return {}
        name, identity = item.get("name"), item.get("id")
        if not isinstance(name, str) or not name or not isinstance(identity, str) or not identity:
            return {}
        names.setdefault(name, set()).add(identity)
        try:
            end = date.fromisoformat(item["end"])
        except (KeyError, TypeError, ValueError):
            end = None
        dates.setdefault(identity, set()).add(end)
    result = {}
    for name, identities in names.items():
        if len(identities) == 1:
            ends = dates[next(iter(identities))]
            if len(ends) == 1 and None not in ends:
                result[name] = next(iter(ends))
    return result


def archive_policy(parsed, body):
    """仅取模型已归属段内带明确列名的原文。缺列、重复列或跨段分组均交人工。"""
    span = parsed.source_span
    if not span or not 0 <= span[0] < span[1] <= len(body):
        raise ValueError
    groups, group = [], {}
    for match in re.finditer(r"(?m)^([^：:\r\n]+)[：:][ \t]*([^\r\n]+)", body[span[0]:span[1]]):
        field = match[1].strip()
        if field not in _FIELDS:
            continue
        if field in group:
            raise ValueError
        group[field] = SourceExcerpt(text=match[2], start=span[0] + match.start(2))
        if set(group) == set(_FIELDS):
            groups.append(group)
            group = {}
    if group or not groups:
        raise ValueError
    return ArchivePolicy(parsed.policy_type.value, tuple(groups))


def handed_text(reason, held=False):
    """未拟建条目的卡面文案：原因后写明「已转人工」；同名政策已有机器人建单记录（held）时改写「只读核对，请勿重复建单」。"""
    from qianshu.feishu.trial_order import ROBOT_HELD
    suffix = ROBOT_HELD if held else "已转人工"
    if reason in ("本次拟建", "转人工"):
        return suffix
    return reason.removesuffix("，转人工") + "，" + suffix


def decision_pages(trial, row, text, details, parsed, policies, full_review, setup, blocked):
    settings, snapshot, contact = setup
    countries = [d.field_results.get("country") for d in parsed]
    country = settings.get("MAIL_TRIAL_CREATE_COUNTRY", "")
    domestic = bool(country and countries and all(
        field and verified_value(field) and field.value == country for field in countries))
    reg, version = row["id"], row["version"]
    card = str(uuid4())
    journal = Journal(trial.store.db)
    journal.initialize()
    session = journal.session()
    lines, eligible, missing = [], [], []
    items = list(range(1, len(policies) + 1))
    if not domestic:
        lines.append(CardLine("核对卡", "本国未开建单，仅供核对"))
    for n, policy in enumerate(policies, 1):
        draft = policy.draft
        errors = list(policy.parsing_errors())
        fields = list(dict.fromkeys([e.field for e in errors] + draft.uncertain_fields()))
        if fields:
            missing.append(f"条目 {n} · " + "、".join(_FIELD_LABELS.get(f, f) for f in fields))
        try:
            require_verified_combination(snapshot, draft.policy_type.value, draft.benefit_type.value)
        except UnverifiedCombination:
            reason = "该类型尚未经真实回读验证"
        else:
            if (draft.policy_type.value != "定向政策" or draft.trigger_once.value != "否"
                    or draft.effective_multiplier.value != 1 or draft.maintain_mutual_exclusion_group.value != "是"):
                reason = "超出试运行建单范围，转人工"
            elif not policy.validation or fields or not policy.approval_url_deferred:
                reason = "待人工核对字段"
            elif not domestic or blocked or full_review:
                reason = "转人工"
            else:
                eligible.append(n)
                reason = "本次拟建"
        lines.append(CardLine(f"条目 {n}", reason))
    preview = material = None
    if (domestic and row["attachments"] == 0 and not blocked and not full_review and policies
            and settings.get("MAIL_TRIAL_ARCHIVE_PURPOSE") == "X8a"):
        # X8a 只授权【测试】单的合成试写，普通申请不得据此发布：不出预览、卡片只读，整批转人工。
        lines.append(CardLine("存档预览", ARCHIVE_NOTICES["unconfirmed"]))
    elif domestic and row["attachments"] == 0 and not blocked and not full_review and policies:
        try:
            # 普通申请只按首次真实发布（X8b）展示；执行时未登记 X8b 即不发布。
            batch = VerifiedArchiveBatch(str(reg), str(version), trial.clock().date(), country, "X8b",
                                         text, tuple(archive_policy(d, text) for d in parsed), True)
            preview = preview_archive_batch(batch, config=trial.config, sensitive_values=trial.sensitive)
            material = asdict(batch) | {"utc_date": batch.utc_date.isoformat()}
        except ValueError:
            lines.append(CardLine("存档预览", "本批明细原文未能完整定位，整批转人工"))
        else:
            lines.extend([CardLine("存档标题", preview.title), CardLine("存档预览", preview.body),
                          *(CardLine(kind + "汇总行", summary) for kind, summary in preview.summaries)])
    readonly = not (preview and eligible and session and contact and settings.get("MAIL_TRIAL_APPROVER_OPEN_IDS"))
    if readonly:
        eligible = []
    if readonly:
        lines = [CardLine(line.label, "转人工") if line.text == "本次拟建" else line for line in lines]
    # 未列为拟建的条目出卡即登记人工移交（Journal.prepare 同一事务），卡面逐条写明「已转人工」；同名政策已有机器人
    # 建单记录的（held，由 prepare 在登记移交的同一写事务里裁决）改写「只读核对，请勿重复建单」（复核 rv-2d4 P1）。
    handed = {f"条目 {n}": n for n in items if n not in eligible}

    def render(held):
        shown = [CardLine(line.label, handed_text(line.text, handed[line.label] in held))
                 if line.label in handed else line for line in lines]
        details.extend(result_card(page) for page in detail_pages("试运行邮件", shown))
        final = result_card(detail_pages("试运行邮件", [CardLine("本批处理",
            "整批转人工" if readonly else "请在已发送中核对关联申请及本次无条件同意回复，再核验并决定。")])[0])
        if not readonly:
            binding = dict(card=card, registration=reg, version=version, format=CARD_FORMAT)
            buttons = [{"tag": "button", "name": action, "action_type": "form_submit",
                        "text": {"tag": "plain_text", "content": label}, "value": dict(binding, action=action)}
                       for action, label in (("verify", "邮件审批已核验"), ("approve", "同意"), ("reject", "驳回"))]
            final["elements"].append({"tag": "form", "name": "trial_review", "elements": [
                {"tag": "input", "name": "reason", "placeholder": {"tag": "plain_text", "content": "驳回原因或待补内容"}},
                {"tag": "action", "actions": buttons}]})
        details.append(final)
        for i, page in enumerate(details, 1):
            page["header"]["title"]["content"] = f"试运行邮件 · 第 {i} / {len(details)} 张"
        return details

    journal.prepare(card, reg, version, session, country, {
        "items": items, "eligible": eligible, "missing": missing, "contact": contact,
        "preview": preview.fingerprint if preview else None,
        "order_snapshot_sha256": sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        "material": material, "drafts": [d.to_dict() for d in parsed],
        "sources": [{"id": "body", "source_id": "body", "start": 0, "text": text}],
        "requires_full_review": full_review,
        "structure_issues": [{"code": i.code, "policy_ids": i.policy_ids,
                              "span": i.span.model_dump(mode="json") if i.span else None}
                             for i in getattr(parsed, "structure_issues", ())],
        "reviewed": [asdict(p.draft) for p in policies],
    }, readonly, trial.clock().timestamp(), render=render)
    return card
