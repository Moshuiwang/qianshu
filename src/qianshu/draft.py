"""政策草稿接口，字段对应产品文档 6.2。"""

from dataclasses import asdict, dataclass, field, fields
from typing import Literal

type YesNo = Literal["是", "否"]


@dataclass(kw_only=True, repr=False)
class SourceExcerpt:
    """原文依据。text 是逐字摘录，start 是在申请正文中从零起算的字符下标。

    未定位到字符下标时，start 为 None。
    """

    text: str
    start: int | None = None


@dataclass(kw_only=True, repr=False)
class Candidate[T]:
    """候选值 value 与它对应的原文摘录 evidence。"""

    value: T
    evidence: list[SourceExcerpt] = field(default_factory=list)


@dataclass(kw_only=True, repr=False)
class Uncertainty[T]:
    """reason 是不确定的原因，candidates 是同字段类型的候选列表，可为空。"""

    reason: str
    candidates: list[Candidate[T]] = field(default_factory=list)


@dataclass(kw_only=True, repr=False)
class DraftField[T]:
    """value 是已确定的值，evidence 是字段相关的原文摘录，可有多段。

    无原文摘录时 evidence 为空。取值缺失或待确认时，value 为 None，
    并用 uncertainty 记录原因及候选。已确定的字段将 uncertainty 留为 None，
    已确认的空列表用 [] 表示。
    """

    value: T | None = None
    evidence: list[SourceExcerpt] = field(default_factory=list)
    uncertainty: Uncertainty[T] | None = None


@dataclass(kw_only=True, repr=False)
class PackageRow:
    """节目包子表的一行。

    current_package 是当前节目包名称，gift_package 是赠送节目包名称，
    确认无赠送包时为 None。页面根据当前节目包带出价格计划。
    """

    current_package: str
    gift_package: str | None = None


@dataclass(kw_only=True, repr=False)
class PolicyDraft:
    """一条政策对应一份草稿。各字段均以 DraftField 保存取值、依据和不确定性。

    policy_name: 优惠政策名称，字符串，命名规则待确认。
    maintain_mutual_exclusion_group: 是否需要维护互斥组，选项「是」或「否」。
    company: 公司名称，字符串。
    policy_type: 政策类型，明道云选项名称。
    start_date: 开始日期，YYYY-MM-DD 字符串。
    end_date: 结束日期，YYYY-MM-DD 字符串。
    benefit_type: 优惠类型，明道云选项名称。
    trigger_once: 是否只触发一次，选项「是」或「否」，取值规则待确认。
    cancel_on_downgrade: 降包用户取消优惠，选项「是」或「否」。
    recharge_days: 充值天数，整数。
    gift_days: 赠送天数，整数。
    repeat_bonus: 多买多送，选项「是」或「否」，取值规则待确认。
    effective_multiplier: 生效倍数，有限数值，取值规则待确认。
    user_tags: 用户标签，标签名称列表。
    packages: 节目包子表，PackageRow 列表。
    user_scope: 用户范围，自由文本。
    approval_url: 任务审批链接，本批存档文档的网址字符串。
    approval_attachments: 审批件附件，申请邮件截图的引用字符串列表，上传规则待确认。
    """

    policy_name: DraftField[str]
    maintain_mutual_exclusion_group: DraftField[YesNo]
    company: DraftField[str]
    policy_type: DraftField[str]
    start_date: DraftField[str]
    end_date: DraftField[str]
    benefit_type: DraftField[str]
    trigger_once: DraftField[YesNo]
    cancel_on_downgrade: DraftField[YesNo]
    recharge_days: DraftField[int]
    gift_days: DraftField[int]
    repeat_bonus: DraftField[YesNo]
    effective_multiplier: DraftField[int | float]
    user_tags: DraftField[list[str]]
    packages: DraftField[list[PackageRow]]
    user_scope: DraftField[str]
    approval_url: DraftField[str]
    approval_attachments: DraftField[list[str]]

    def uncertain_fields(self) -> list[str]:
        """按字段声明顺序列出带有不确定信息的字段名。"""
        return [
            item.name
            for item in fields(self)
            if getattr(self, item.name).uncertainty is not None
        ]

    def to_dict(self) -> dict[str, object]:
        """生成可交给 json.dumps 的字典，包含原文摘录，供私有比对报告使用。"""
        return asdict(self)
