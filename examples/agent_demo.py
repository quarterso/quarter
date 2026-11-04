"""A small tool-using agent that sends every model call through Quarter.

It plans a team offsite with two tools, and reacts to what Quarter answers:
a refused call stops it, an approval makes it wait, an approved call goes on.

    python examples/agent_demo.py --gateway http://127.0.0.1:8780/local/v1 --key <agent key>

Any OpenAI-compatible model works; run one locally with llama-server --jinja.
"""

import argparse
import json
import urllib.error
import urllib.request

VENUES = {"lisbon": 4200, "porto": 3100, "madrid": 5300}
FLIGHTS = {"lisbon": 180, "porto": 150, "madrid": 140}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "venue_price",
            "description": "Price in EUR to hire an offsite venue for two days in a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flight_price",
            "description": "Return flight price in EUR per person to a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    },
]


def run_tool(name: str, args: dict) -> str:
    city = str(args.get("city", "")).strip().lower()
    table = VENUES if name == "venue_price" else FLIGHTS
    return json.dumps({"city": city, "eur": table[city]}) if city in table else json.dumps({"error": f"no data for {city}"})


def call(gateway: str, key: str, body: dict, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        gateway.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"authorization": f"Bearer {key}", "content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", required=True, help="Quarter address ending in the vendor's /v1")
    p.add_argument("--key", required=True, help="the agent's Quarter key")
    p.add_argument("--model", default="local")
    p.add_argument("--task", default="offsite")
    p.add_argument("--customer")
    p.add_argument("--wait", type=int, default=0, help="seconds to wait for an approval")
    p.add_argument("--steps", type=int, default=8)
    a = p.parse_args()

    headers = {"x-quarter-task": a.task}
    if a.customer:
        headers["x-quarter-customer"] = a.customer
    if a.wait:
        headers["x-quarter-wait"] = str(a.wait)
    messages = [
        {"role": "system", "content": "You plan company offsites. Use the tools for every price. Be brief."},
        {
            "role": "user",
            "content": "Six people, two days. Compare Lisbon and Porto: venue plus flights for everyone. Which is cheaper, and by how much?",
        },
    ]
    for step in range(1, a.steps + 1):
        status, reply = call(a.gateway, a.key, {"model": a.model, "messages": messages, "tools": TOOLS, "max_tokens": 400}, headers)
        if status != 200:
            print(f"step {step}: Quarter answered {status}: {reply.get('error')} {reply.get('reason', '')}".rstrip())
            return
        message = reply["choices"][0]["message"]
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            print(f"step {step}: answer: {message.get('content', '').strip()}")
            return
        for tc in calls:
            args = json.loads(tc["function"]["arguments"] or "{}")
            result = run_tool(tc["function"]["name"], args)
            print(f"step {step}: {tc['function']['name']}({args.get('city')}) -> {result}")
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
    print("stopped: step limit reached")


if __name__ == "__main__":
    main()
