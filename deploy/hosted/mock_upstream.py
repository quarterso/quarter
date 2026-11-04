"""A stand-in for OpenAI and Anthropic, for the hosted sandbox: no vendor key, no network call.

POST /v1/chat/completions (OpenAI) and POST /v1/messages (Anthropic) answer a canned reply,
streamed or not, with usage counted the way the vendor counts it, so the gateway prices it.
Tokens are estimated at four characters each. PORT sets the port (default 8790).
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = "This is the Quarter sandbox. No model was called; this reply is canned."


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


def openai_reply(request: dict) -> tuple[dict, list[dict]]:
    """The reply, and the same reply as stream chunks."""
    model = request.get("model", "gpt-4o-mini")
    usage = {"prompt_tokens": tokens(json.dumps(request.get("messages", []))),
             "completion_tokens": tokens(REPLY)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    reply = {
        "id": "chatcmpl-sandbox", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": REPLY}}],
        "usage": usage,
    }
    chunk = {"id": "chatcmpl-sandbox", "object": "chat.completion.chunk", "model": model}
    chunks = [chunk | {"choices": [{"index": 0, "delta": {"role": "assistant", "content": REPLY}}]},
              chunk | {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
    if (request.get("stream_options") or {}).get("include_usage"):
        chunks.append(chunk | {"choices": [], "usage": usage})
    return reply, chunks


def anthropic_reply(request: dict) -> tuple[dict, list[dict]]:
    """The reply, and the same reply as stream events."""
    prompt = json.dumps([request.get("system", ""), request.get("messages", [])])
    usage = {"input_tokens": tokens(prompt), "output_tokens": tokens(REPLY)}
    message = {"id": "msg_sandbox", "type": "message", "role": "assistant",
               "model": request.get("model", "claude-haiku-4-5"), "stop_reason": "end_turn"}
    reply = message | {"content": [{"type": "text", "text": REPLY}], "usage": usage}
    events = [
        {"type": "message_start",
         "message": message | {"content": [], "usage": usage | {"output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": REPLY}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": usage["output_tokens"]}},
        {"type": "message_stop"},
    ]
    return reply, events


ROUTES = {"/v1/chat/completions": openai_reply, "/v1/messages": anthropic_reply}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, "application/json", b'{"ok": true}')
        self._send(404, "application/json", b'{"error": "not found"}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        route = ROUTES.get(self.path.split("?")[0])
        if route is None:
            return self._send(404, "application/json", b'{"error": "the sandbox answers chat only"}')
        try:
            request = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._send(400, "application/json", b'{"error": "body is not JSON"}')
        reply, events = route(request)
        if not request.get("stream"):
            return self._send(200, "application/json", json.dumps(reply).encode())
        stream = "".join(
            (f"event: {e['type']}\n" if "type" in e else "") + f"data: {json.dumps(e)}\n\n"
            for e in events
        )
        if route is openai_reply:
            stream += "data: [DONE]\n\n"
        self._send(200, "text/event-stream", stream.encode())

    def _send(self, status: int, kind: str, data: bytes):
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8790"))), Handler).serve_forever()
