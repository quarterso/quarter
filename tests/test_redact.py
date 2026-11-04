import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, PRICES, call, start

from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.redact import StreamRestorer, redactor, restore_value
from quarter_ledger.rules import load_rules
from quarter_ledger.store import Ledger

KEY = b"k" * 32
CARD, EMAIL, IBAN = "4111 1111 1111 1111", "ana@example.com", "DE89 3704 0044 0532 0130 00"


def test_each_kind_is_masked_and_only_when_it_checks_out():
    r = redactor(["email", "card", "iban", "ssn", "phone"], {}, KEY)
    text = (f"mail {EMAIL}, card {CARD}, iban {IBAN} or GB82WEST12345698765432, ssn 078-05-1120,"
            " call +1 (415) 555-0100. Not: order 4111 1111 1111 1112, at 1730000000000, DE00 1234 5678 9012 34")
    masked = r.text(text)
    for value in (EMAIL, CARD, IBAN, "GB82WEST12345698765432", "078-05-1120", "+1 (415) 555-0100"):
        assert value not in masked
    for value in ("4111 1111 1111 1112", "1730000000000", "DE00 1234 5678 9012 34"):
        assert value in masked
    assert r.summary() == "card:1,email:1,iban:2,phone:1,ssn:1"
    assert restore_value(masked, r.found) == text


def test_a_value_gets_the_same_placeholder_every_time_and_the_vendor_cannot_guess_it():
    one, two = redactor(["email"], {}, KEY), redactor(["email"], {}, KEY)
    assert one.text(EMAIL) == two.text(f"{EMAIL}").strip() == one.text(EMAIL)  # the prompt cache still hits
    assert redactor(["email"], {}, b"x" * 32).text(EMAIL) != one.text(EMAIL)
    custom = redactor([], {"patient": r"P\d{8}"}, KEY)
    masked = custom.text("P12345678 and again P12345678")
    assert masked.startswith("[PATIENT_") and len(custom.found) == 1
    assert custom.text(masked) == masked  # a placeholder is never masked again


def test_tool_arguments_sent_as_json_text_still_parse_once_restored():
    r = redactor([], {"name": r'Ana "A" Lee'}, KEY)
    placeholder = r.text('Ana "A" Lee')
    reply = {"choices": [{"message": {"tool_calls": [{"function": {"arguments": json.dumps({"to": placeholder})}}]}}]}
    back = restore_value(reply, r.found)
    assert json.loads(back["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"to": 'Ana "A" Lee'}


def sse(event, name=None):
    return (f"event: {name}\n" if name else "").encode() + f"data: {json.dumps(event)}\n\n".encode()


def pieces(text, size=3):
    return [text[i:i + size] for i in range(0, len(text), size)]


def replay(events, found):
    restorer, out = StreamRestorer(found), []
    for raw in events:
        out += restorer.feed(raw, json.loads(raw.split(b"data: ", 1)[1]))
    return out + restorer.flush()


def test_a_placeholder_split_over_stream_events_is_put_back():
    r = redactor(["email"], {}, KEY)
    said = f"Write to {r.text(EMAIL)} today."
    anthropic = [sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}},
                     "content_block_delta") for p in pieces(said)]
    anthropic.append(sse({"type": "content_block_stop", "index": 0}, "content_block_stop"))
    out = replay(anthropic, r.found)
    assert len(out) == len(anthropic) and all(o.startswith(b"event: content_block_") for o in out)
    text = "".join(json.loads(o.split(b"data: ", 1)[1])["delta"]["text"] for o in out[:-1])
    assert text == f"Write to {EMAIL} today."

    args = json.dumps({"to": r.text(EMAIL)})
    chat = [sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": p}}]}}]})
            for p in pieces(args)]
    out = replay(chat, r.found)
    joined = "".join(json.loads(o[6:])["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] for o in out)
    assert json.loads(joined) == {"to": EMAIL}

    done = {"type": "response.completed", "response": {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": said}]}]}}
    responses = [sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": p},
                     "response.output_text.delta") for p in pieces(said)] + [sse(done, "response.completed")]
    out = replay(responses, r.found)
    assert EMAIL in json.loads(out[-1].split(b"data: ", 1)[1])["response"]["output"][0]["content"][0]["text"]
    assert "".join(json.loads(o.split(b"data: ", 1)[1])["delta"] for o in out[:-1]) == said.replace(r.text(EMAIL), EMAIL)


def test_an_unfinished_placeholder_at_the_end_of_a_stream_still_goes_out():
    r = redactor(["email"], {}, KEY)
    start_of = r.text(EMAIL)[:5]
    event = sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": start_of}})
    restorer = StreamRestorer(r.found)
    assert restorer.feed(event, json.loads(event[6:])) == []
    assert restorer.flush() == [event]


@pytest.mark.parametrize("item, error", [
    ({"redact": ["passport"]}, "unknown kind"),
    ({"redact": []}, "a redaction is"),
    ({"redact": ["email"], "to": "x"}, "a route is"),
    ({"redact": ["email"], "action": "deny"}, "a redaction is"),
    ({"redact": [], "patterns": {"Bad Name": "x"}}, "names in a-z"),
    ({"redact": [], "patterns": {"id": "("}}, "bad pattern id"),
    ({"redact": [], "patterns": {"id": "x*"}}, "matches nothing"),
])
def test_a_bad_redaction_stops_the_gateway_from_starting(item, error):
    with pytest.raises(ValueError, match=error):
        load_rules([item])


class EchoVendor(BaseHTTPRequestHandler):
    """Says back what it was sent, as a model repeating a value would."""

    protocol_version = "HTTP/1.1"
    seen: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        EchoVendor.seen.append(body)
        said = body["messages"][0]["content"]
        usage = {"input_tokens": 10, "output_tokens": 10}
        if body.get("stream"):
            events = [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}}
                      for p in pieces(said)] + [{"type": "message_delta", "usage": usage}]
            data = b"".join(sse(e, e["type"]) for e in events)
            ctype = "text/event-stream"
        else:
            data = json.dumps({"content": [
                {"type": "text", "text": said},
                {"type": "tool_use", "id": "t1", "name": "send_email", "input": {"to": said.split()[-1]}},
            ], "usage": usage}).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_the_vendor_sees_placeholders_and_the_agent_sees_its_data(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), EchoVendor))
    EchoVendor.seen = []
    ledger = Ledger(str(tmp_path / "l.db"))
    rules = load_rules([
        {"redact": ["email", "card"], "customer": "eu-*"},
        {"tool": "send_email", "action": "approve"},
    ])
    gw = Gateway(ledger, [], PRICES, {"fake": f"http://127.0.0.1:{vendor.server_port}"}, {AGENT_KEY}, ADMIN_KEY,
                 rules=rules)
    proxy = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{proxy.server_port}/fake/v1/messages"
        eu = {"x-quarter-key": AGENT_KEY, "x-quarter-agent": "a1", "x-quarter-customer": "eu-acme"}
        said = f"Card {CARD} belongs to {EMAIL}"
        status, body = call(url, {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": said}]}, eu)
        assert status == 200
        sent = json.dumps(EchoVendor.seen[-1])
        assert CARD not in sent and EMAIL not in sent and "[EMAIL_" in sent
        reply = json.loads(body)
        assert reply["content"][0]["text"] == said
        # The tool call was held for a person, who is shown a placeholder, not the address.
        approval = ledger.db.execute("select reason from approvals").fetchone()[0]
        assert "send_email" in approval and EMAIL not in approval and "[EMAIL_" in approval

        stream = {"model": "m", "max_tokens": 10, "stream": True, "messages": [{"role": "user", "content": said}]}
        status, body = call(url, stream, eu)
        events = [json.loads(line[6:]) for line in body.split(b"\n") if line.startswith(b"data: ")]
        assert "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta") == said

        us = {**eu, "x-quarter-customer": "us-acme"}
        assert call(url, {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": said}]}, us)[0] == 200
        assert CARD in json.dumps(EchoVendor.seen[-1])  # no rule for this customer
        masked = ledger.db.execute("select redacted from calls order by at").fetchall()
        assert masked == [("card:1,email:1",), ("card:1,email:1",), (None,)]
        assert "rule 1: redact email, card on * for customer eu-*" in rules.listed()
    finally:
        proxy.shutdown()
        vendor.shutdown()


def test_placeholders_survive_a_restart_but_not_a_new_signing_key():
    from quarter_ledger.cli import redaction_key

    signing = "ed25519:" + "ab" * 32
    first, again = redactor(["email"], {}, redaction_key(signing)), redactor(["email"], {}, redaction_key(signing))
    other = redactor(["email"], {}, redaction_key("ed25519:" + "cd" * 32))
    assert first.text(EMAIL) == again.text(EMAIL) != other.text(EMAIL)


def test_data_and_signed_thinking_go_through_unchanged():
    r = redactor(["iban", "email"], {}, KEY)
    blob = "aGVsbG8" * 40 + "+GB82WEST12345698765432/" + "d29ybGQ" * 40  # an IBAN in base64, by chance
    thinking = {"type": "thinking", "thinking": f"write to {EMAIL}", "signature": "c2ln"}
    request = {"messages": [{"role": "assistant", "content": [thinking, {"type": "text", "text": EMAIL}]},
                            {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": blob}},
                                                         {"type": "image_url", "image_url": {"url": f"data:x;{EMAIL}"}}]}]}
    masked = r.value(request)
    assert masked["messages"][0]["content"][0] == thinking and masked["messages"][1] == request["messages"][1]
    assert masked["messages"][0]["content"][1]["text"].startswith("[EMAIL_")
    said = {"type": "thinking", "thinking": masked["messages"][0]["content"][1]["text"]}
    assert restore_value({"content": [said]}, r.found) == {"content": [said]}  # its signature still holds
