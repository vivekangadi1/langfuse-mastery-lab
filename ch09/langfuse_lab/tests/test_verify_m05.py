"""Module 05 verify checks against fakes shaped like the live API (server 4.77 / SDK 5.2).

ObservationV2 has snake_case attributes and .dict() with camelCase keys; input/output come back as JSON strings;
metadata values are cut at 200 chars unless `expand_metadata` names the key (verified live)."""
import hashlib
import json
from types import SimpleNamespace as NS

from opentelemetry.sdk.trace.sampling import Decision, TraceIdRatioBased

from langfuse_lab.verify import (NotYet, PLACEHOLDERS, card_numbers, check_module_05, module_05_checks)

CARD, EMAIL, PHONE = PLACEHOLDERS["card"], PLACEHOLDERS["email"], PLACEHOLDERS["phone"]
PNG, PDF = b"\x89PNG fake photo", b"%PDF-1.4 fake receipt"
SHA = {"image/png": hashlib.sha256(PNG).hexdigest(), "application/pdf": hashlib.sha256(PDF).hexdigest()}
MEDIA = {"PNGID": ("image/png", PNG), "PDFID": ("application/pdf", PDF)}
SAMPLING_TAG = "run:x:sampling"
RATE = 0.25


class Obs:
    def __init__(self, **kw):
        base = dict(id="o", trace_id="t1", type="SPAN", name="n", level="DEFAULT", input=None, output=None,
                    metadata=None, is_root_observation=False, parent_observation_id=None, tags=["run:x"],
                    status_message="")
        base.update(kw)
        self.__dict__.update(base)

    def dict(self):
        camel = {"trace_id": "traceId", "is_root_observation": "isRootObservation",
                 "parent_observation_id": "parentObservationId", "status_message": "statusMessage"}
        return {camel.get(k, k): v for k, v in self.__dict__.items()}


def truncated(o, expand):
    if not isinstance(o.metadata, dict):
        return o
    meta = {k: (v if k in expand or len(str(v)) <= 200 else str(v)[:200]) for k, v in o.metadata.items()}
    return Obs(**{**o.__dict__, "metadata": meta})


class FakeObservations:
    def __init__(self, items, sampled=()):
        self.items, self.sampled = items, list(sampled)

    def get_many(self, trace_id=None, filter=None, expand_metadata=None, **kw):
        expand = set((expand_metadata or "").split(","))
        if trace_id:
            return NS(data=[truncated(o, expand) for o in self.items if o.trace_id == trace_id], meta=NS(cursor=None))
        flt = json.loads(filter)
        tag = next(f["value"][0] for f in flt if f["column"] == "traceTags")
        if any(f["column"] == "isRootObservation" for f in flt):
            return NS(data=[o for o in self.items if o.is_root_observation and tag in o.tags], meta=NS(cursor=None))
        return NS(data=[o for o in self.sampled if tag in o.tags], meta=NS(cursor=None))


class FakeMedia:
    def get(self, media_id):
        ct, data = MEDIA[media_id]
        return NS(media_id=media_id, content_type=ct, content_length=len(data), url=f"http://minio/{media_id}")


def download(url):
    return MEDIA[url.rsplit("/", 1)[1]][1]


def ref(ct, media_id):
    return f"@@@langfuseMedia:type={ct}|id={media_id}|source=bytes@@@"


def masked(text):
    return (text.replace("4111 1111 1111 1111", CARD).replace("+1 415-555-0132", PHONE)
            .replace("farah@example.com", EMAIL).replace("chen@example.com", EMAIL))


def turn(tid, intent, message, *, pii=False, history=(), order="8129", tool_output=None, root_input=None):
    tags = ["run:x", intent, *(["pii"] if pii else [])]
    msgs = [{"role": "system", "content": "You are Acme's support agent.\nRules..."}, *history,
            {"role": "user", "content": masked(message)}]
    flags = ["pii_card_number"] if "4111" in message else []
    items = [
        Obs(id=f"{tid}-r", trace_id=tid, type="AGENT", name="support-request", is_root_observation=True, tags=tags,
            input=root_input if root_input is not None else masked(message), output="Escalated to billing.",
            metadata={"scope.name": "langfuse-sdk"}),
        Obs(id=f"{tid}-g", trace_id=tid, type="GUARDRAIL", name="guardrail",
            input=json.dumps({"args": [masked(message)], "kwargs": {}}),
            output=json.dumps({"allowed": True, "flags": flags})),
        Obs(id=f"{tid}-gen", trace_id=tid, type="GENERATION", name="AzureChatOpenAI", input=json.dumps(msgs),
            output=json.dumps({"role": "assistant", "content": "", "tool_calls": []}),
            metadata={"langgraph_checkpoint_ns": "agent:08dc598d-3d95-edd5-1143-459802812a86", "ls_model_name": "x"}),
        Obs(id=f"{tid}-t", trace_id=tid, type="TOOL", name="lookup_order", input=json.dumps({"order_id": order}),
            output=json.dumps({"content": tool_output or '{"order_id": "%s"}' % order})),
    ]
    return items


SUFFIX = " My card number is 4111 1111 1111 1111."


def good_items():
    first = "I was charged twice for order 8129." + SUFFIX
    return [
        *turn("t1", "double_charge", "I was charged twice for order 8129. Please fix it." + SUFFIX, pii=True),
        *turn("t2", "s_refund_followup", first, pii=True),
        *turn("t3", "s_refund_followup", "How long until I see the refund?", pii=True,
              history=[{"role": "user", "content": masked(first)}]),
        *turn("t4", "password_reset", "I can't log in to my account cust_02.", order="8106",
              tool_output=json.dumps({"reset_link_sent": True, "email": EMAIL})),
        *turn("t5", "damaged_item_photo", "My item from order 8115 arrived broken. Call me back at +1 415-555-0132 "
              "or email farah@example.com.", order="8115",
              root_input=json.dumps({"message": masked("Order 8115 broke. +1 415-555-0132 farah@example.com"),
                                     "photo": ref("image/png", "PNGID"), "receipt": ref("application/pdf", "PDFID")})),
    ]


def sample_ids(n=40):
    return [hashlib.md5(f"s-{i}".encode()).hexdigest() for i in range(n)]


def truth(ids, rate=RATE):
    s = TraceIdRatioBased(rate)
    return {t for t in ids if s.should_sample(None, int(t, 16), "x").decision == Decision.RECORD_AND_SAMPLE}


def sampled_obs(ids, keep=None, per=3):
    keep = truth(ids) if keep is None else keep
    return [Obs(id=f"{t}-{k}", trace_id=t, tags=[SAMPLING_TAG]) for t in sorted(keep) for k in range(per)]


def client_for(items=None, sampled=None):
    ids = sample_ids()
    return NS(api=NS(observations=FakeObservations(good_items() if items is None else items,
                                                    sampled_obs(ids) if sampled is None else sampled),
                     media=FakeMedia()), flush=lambda: None)


def results(items=None, sampled=None, **kw):
    ids = sample_ids()
    args = dict(sampling_tag=SAMPLING_TAG, sampled_ids=ids, predicted_ids=truth(ids), sample_rate=RATE,
                attachment_sha256=SHA, download=download)
    args.update(kw)
    out = {}
    for c in module_05_checks("run:x", client_for(items, sampled), **args):
        try:
            out[c.name] = c.fn()
        except (NotYet, AssertionError) as exc:
            out[c.name] = ("retry", str(exc))
    return out


NO_CARD = "planted pii caught: no card number in any field"
PLACEHOLDER = "mask ran: placeholder where the card was"
GUARD = "guardrail saw the raw card, storage did not"
EMAILS = "emails and phone numbers masked"
IDS = "ids not over-masked"
MEDIA_CHECK = "attachment stored and retrievable (media API)"
SAMPLING = "sampling keeps or drops whole traces, by trace id"


def with_change(tid_type, **changes):
    items = good_items()
    for o in items:
        if f"{o.trace_id}-{o.type}" == tid_type:
            o.__dict__.update(changes)
    return items


def test_card_numbers_finds_cards_not_ids():
    assert card_numbers("card 4111 1111 1111 1111.") == ["4111111111111111"]
    assert card_numbers("4111-1111-1111-1111") and card_numbers("378282246310005")
    assert card_numbers("order 8129, cust_02, 2026-11-11 14:40:09, +1 415-555-0132, 4111 1111 1111 1112") == []
    assert card_numbers("agent:08dc598d-3d95-edd5-1143-459802812a86") == []
    assert card_numbers(json.dumps("line\n4111111111111111")) == ["4111111111111111"]


def test_all_pass():
    r = results()
    assert all(v[0] is True for v in r.values()), r
    assert "3 pii trace(s)" in r[NO_CARD][1] and "11/40" not in r[SAMPLING][1]


def test_check_module_05_reports(capsys):
    ids = sample_ids()
    assert check_module_05("run:x", client=client_for(), sampling_tag=SAMPLING_TAG, sampled_ids=ids,
                           predicted_ids=truth(ids), sample_rate=RATE, attachment_sha256=SHA, download=download,
                           retries=1) is True
    assert "MODULE PASSED" in capsys.readouterr().out


def test_missing_traces_are_retried():
    r = results(items=good_items()[:8])
    assert r[NO_CARD][0] == "retry" and "2/5" in r[NO_CARD][1]


def test_unmasked_generation_input_is_caught():
    items = with_change("t1-GENERATION", input=json.dumps([{"role": "user", "content": "x" + SUFFIX}]))
    ok, detail = results(items)[NO_CARD]
    assert not ok and "GENERATION" in detail and "4111…1111" in detail


def test_card_hidden_past_200_chars_of_metadata_is_caught():
    items = with_change("t3-TOOL", metadata={"note": "y" * 300 + SUFFIX})
    ok, detail = results(items)[NO_CARD]
    assert not ok and "metadata" in detail


def test_card_in_root_output_object_dump_is_caught():
    items = with_change("t2-AGENT", output=json.dumps([{"messages": [{"content": "x" + SUFFIX}]}, "t2"]))
    assert results(items)[NO_CARD][0] is False


def test_card_dropped_instead_of_masked_fails_placeholder_check():
    items = good_items()
    for o in items:
        for field in ("input", "output"):
            if isinstance(getattr(o, field), str):
                setattr(o, field, getattr(o, field).replace(CARD, ""))
    r = results(items)
    assert r[NO_CARD][0] is True
    ok, detail = r[PLACEHOLDER]
    assert not ok and CARD in detail


def test_wrong_placeholder_fails():
    items = good_items()
    for o in items:
        for field in ("input", "output"):
            if isinstance(getattr(o, field), str):
                setattr(o, field, getattr(o, field).replace(CARD, "****"))
    ok, detail = results(items)[PLACEHOLDER]
    assert not ok and "'My card number is ****" in detail


def test_guardrail_without_flag_fails():
    items = with_change("t1-GUARDRAIL", output=json.dumps({"allowed": True, "flags": []}))
    ok, detail = results(items)[GUARD]
    assert not ok and "pii_card_number" in detail


def test_email_in_tool_output_fails():
    items = with_change("t4-TOOL", output=json.dumps({"reset_link_sent": True, "email": "chen@example.com"}))
    ok, detail = results(items)[EMAILS]
    assert not ok and "TOOL 'lookup_order' output has an email" in detail


def test_phone_in_upload_input_fails():
    items = with_change("t5-AGENT", input="Order 8115. Call +1 415-555-0132 " + EMAIL)
    ok, detail = results(items)[EMAILS]
    assert not ok and "phone" in detail


def test_over_masked_order_id_fails():
    items = with_change("t1-AGENT", input="I was charged twice for order [REDACTED]. My card number is " + CARD)
    ok, detail = results(items)[IDS]
    assert not ok and "over-masking" in detail and "8129" in detail
    items = with_change("t4-TOOL", input=json.dumps({"order_id": "[REDACTED_PHONE]"}))
    assert results(items)[IDS][0] is False


def test_attachment_missing_or_different_bytes_fails():
    items = with_change("t5-AGENT", input=masked("Order 8115 broke."))
    ok, detail = results(items)[MEDIA_CHECK]
    assert not ok and "references no media" in detail
    stringified = "<langfuse.media.LangfuseMedia object at 0x1>"  # what a mask that str()s the object stores
    items = with_change("t5-AGENT", input=json.dumps({"message": "x", "photo": stringified}))
    ok, detail = results(items)[MEDIA_CHECK]
    assert not ok and "return them unchanged" in detail
    ok, detail = results(attachment_sha256={"image/png": "0" * 64})[MEDIA_CHECK]
    assert not ok and "image/png" in detail


def test_sampling_wrong_prediction_fails():
    ids = sample_ids()
    wrong = set(list(truth(ids))[1:])
    ok, detail = results(predicted_ids=wrong)[SAMPLING]
    assert not ok and "predict_kept" in detail


def test_sampling_dropped_trace_that_arrived_fails():
    ids = sample_ids()
    extra = next(t for t in ids if t not in truth(ids))
    ok, detail = results(sampled=sampled_obs(ids, keep=truth(ids) | {extra}))[SAMPLING]
    assert not ok and "sampler drops" in detail


def test_sampling_rate_one_or_missing_ids_fails():
    assert results(sample_rate=1.0)[SAMPLING][0] is False
    assert results(sampled_ids=[])[SAMPLING][0] is False


def test_sampling_lag_and_partial_traces_are_retried():
    ids = sample_ids()
    keep = truth(ids)
    assert results(sampled=sampled_obs(ids, keep=set(list(keep)[1:])))[SAMPLING][0] == "retry"
    partial = sampled_obs(ids)[:-1]
    assert results(sampled=partial)[SAMPLING][0] == "retry"
