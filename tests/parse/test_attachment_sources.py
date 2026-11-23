"""合成截图、内嵌邮件及新回复的删项重放。"""

import base64
import json
from copy import deepcopy
from email.message import EmailMessage
from pathlib import Path

import pytest

from qianshu.mail.message import application_attachments, attachment_block
from qianshu.parse import parse_application
from qianshu.parse.protocol import ApplicationReply
from tests.protocol_fixtures import policy, span, statement

FIXTURES = Path(__file__).parent / "fixtures"
PNG = (FIXTURES / "attachment-policies.png").read_bytes()


def parsed(body, blocks, rows):
    return parse_application(
        body, additional_blocks=blocks, companies_text="", model=lambda **kw: json.dumps({"policies": rows}),
    )


def image_policy(number=1):
    ref = {"source_id": "part-1", "region": [0, 0, 1, 0.5]}
    field = statement("确定", value=f"2099-0{number}-01")
    field["image_evidence"] = ref.copy()
    return {**policy(f"p{number}", None, {"start_date": field}), "image_main": ref.copy()}


@pytest.mark.parametrize("body", ["", "正文仅供参考"])
def test_image_sources_are_optional_and_local(body):
    picture = attachment_block("1", "image/png", PNG, original_id="synthetic-mail")
    rows = [image_policy(1), image_policy(2)]
    if body:
        rows[0]["fields"]["user_scope"] = statement("确定", value=body, evidence=[
            {"span": span(0, len(body)), "text": body},
        ])
    baseline = parsed(body, [picture], rows)
    assert len(baseline) == 2 and not baseline.requires_full_review
    assert baseline.evidence_locations == ({"exact": 1, "image_pending": 2} if body else {"image_pending": 2})
    for draft in baseline:
        assert draft.start_date.value is None
        assert "synthetic-mail / part-1" in draft.start_date.uncertainty.reason
        assert len(draft.start_date.uncertainty.candidates) == 1
    for field in ("image_evidence", "image_main"):
        for child in (None, "source_id", "region"):
            altered = deepcopy(rows)
            owner = altered[0]["fields"]["start_date"] if field == "image_evidence" else altered[0]
            if child:
                del owner[field][child]
            else:
                del owner[field]
            ApplicationReply.model_validate_json(json.dumps({"policies": altered}))
            drafts = parsed(body, [picture], altered)
            assert drafts[0].user_scope == baseline[0].user_scope
            if body:
                assert drafts[0].user_scope.value == body
            assert drafts[0].end_date == baseline[0].end_date
            assert drafts[1].start_date == baseline[1].start_date
            assert drafts[0].start_date.value is None
    for bad in ({"region": [0, 0, 2, 1]}, {"source_id": "part-2"}, {"region": "invalid"}):
        altered = deepcopy(rows)
        altered[0]["fields"]["start_date"]["image_evidence"].update(bad)
        drafts = parsed(body, [picture], altered)
        assert "无法定位" in drafts[0].start_date.uncertainty.reason
        assert drafts[1].start_date == baseline[1].start_date
    rows[0]["main"] = {**span(0, 12), "text": "不存在的旧锚点"}
    assert parsed(body, [picture], rows).requires_full_review


@pytest.mark.parametrize("kind", ["message/rfc822", "application/octet-stream"])
@pytest.mark.parametrize("html_only", [True, False])
def test_application_mime_source_order_and_local_eml(kind, html_only):
    attached = EmailMessage()
    attached["Subject"] = "SYNTHETIC_HEADER"
    if html_only:
        attached.set_content("<p>合成引文</p>", subtype="html")
    else:
        attached.set_content("合成引文", charset="gb18030")
        attached.add_alternative("<p>次选内容</p>", subtype="html")
    attached.add_attachment(b"nested", maintype="application", subtype="octet-stream", filename="nested")
    parts = [("image/png", PNG, "a.png"), (kind, attached.as_bytes(), "a.eml"), ("image/png", PNG, "a.png")]
    class Mailbox:
        def structure(self, uid):
            return [[*(piece.encode() for piece in mime.split("/")), [b"NAME", name.encode()],
                     None, None, b"BASE64", str(len(base64.b64encode(data))).encode()]
                    for mime, data, name in parts] + [b"MIXED"]

        def section(self, uid, section):
            number, _, header = section.partition(".")
            mime, data, name = parts[int(number) - 1]
            if header:
                return f'Content-Type: {mime}; name="{name}"\r\nContent-Transfer-Encoding: base64\r\n'.encode()
            return base64.b64encode(data)

    blocks = application_attachments(Mailbox(), 1, b"", "synthetic-mail")
    assert [b.id for b in blocks] == ["part-1", "part-2", "part-3"]
    assert [b.image for b in blocks] == [PNG, b"", PNG]
    assert blocks[1].text == "合成引文"
    q = {"text": "合成引文", "span": span(0, 4, "part-2")}
    row = policy("p", span(0, 4, "part-2"), {"user_scope": statement("确定", value="合成引文", evidence=[q])})
    assert parsed("合成引文", blocks, [row])[0].user_scope.value == "合成引文"
    q["span"]["source_id"] = "part-3"
    assert parsed("合成引文", blocks, [row])[0].user_scope.value is None


def test_card_renders_reference_only_and_old_anchor_stops_batch(monkeypatch):
    from functools import partial
    from types import SimpleNamespace

    from qianshu import parse, validate
    from qianshu.feishu.cards import result_card
    from qianshu.pipeline import Pipeline

    picture = attachment_block("1", "image/png", PNG, original_id="synthetic-mail")
    row = image_policy()
    monkeypatch.setattr(parse, "parse_application", partial(parse_application, additional_blocks=(picture,)))
    pipeline = Pipeline(model=lambda **_: json.dumps({"policies": [row]}), tag_dates={},
                        companies_file=FIXTURES / "companies.tsv")
    app = SimpleNamespace(text="")
    def render():
        return json.dumps([result_card(page) for page in pipeline(app)], ensure_ascii=False)
    cards = render()
    assert "synthetic-mail / part-1" in cards
    assert "需对原图" in cards and base64.b64encode(PNG).decode() not in cards
    assert '"img_key"' not in cards
    row["main"] = {**span(0, 1), "text": "旧锚点"}
    monkeypatch.setattr(validate, "validate_batch", lambda *_a, **_kw: pytest.fail("旧锚点门禁被绕过"))
    assert "政策主段无法定位" in render()


def test_image_redirect_and_wrong_origin_never_receive_image(monkeypatch):
    import anthropic
    import httpx
    from qianshu.config import ConfigError
    from qianshu.parse import ModelError

    picture = attachment_block("1", "image/png", PNG, original_id="fixture")
    sent = []
    def redirect(request):
        sent.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://synthetic.invalid/image"})
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **options: httpx.Client(
        transport=httpx.MockTransport(redirect), **options))
    options = {"additional_blocks": [picture], "companies_text": ""}
    with pytest.raises(ConfigError):
        parse_application("", **options, config={"ANTHROPIC_API_KEY": "synthetic",
                                                 "ANTHROPIC_BASE_URL": "https://synthetic.invalid"})
    assert not sent
    with pytest.raises(ModelError):
        parse_application("", **options, config={"ANTHROPIC_API_KEY": "synthetic"})
    assert sent == ["https://api.anthropic.com/v1/messages"]


def test_unreadable_image_only_stops_with_fixed_reason(caplog):
    class Mailbox:
        def structure(self, uid):
            return [b"IMAGE", b"PNG", None, None, None, b"8BIT", b"15"]

        def section(self, uid, section):
            assert section == "TEXT"
            return b"BAD-SYNTHETIC!!"

    blocks = application_attachments(Mailbox(), 42, b"Content-Type: image/png\r\n", "fixture")
    assert len(blocks) == 1 and blocks[0].missing
    assert blocks[0].original_id == "UID 42（fixture）"
    result = parse_application("", additional_blocks=blocks, model=lambda **_: pytest.fail("仅损坏材料不应送模"))
    assert [issue.code for issue in result.structure_issues] == ["attachment_missing"]
    assert "BAD-SYNTHETIC" not in caplog.text
