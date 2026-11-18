#!/usr/bin/env python3
"""
Local-model benchmark for Buddy: how well a model on your own server (vLLM, Ollama, llama.cpp, LM Studio) operates a
real Android phone through Buddy's phone tools, with the full tool list or lean mode's short one (Tools.LEAN).

It runs on a computer of your tailnet, plays the agent itself (a plain tool-calling loop against any OpenAI-compatible
/v1/chat/completions) and drives the phone over "Phone tools on Tailscale" (Buddy: Settings > Tools and connections).
Only the Python standard library is needed.

  python3 benchmark/local/bench.py --phone 100.101.102.103 --base-url http://localhost:8000/v1 \
      --model Qwen/Qwen3-32B-AWQ --mode both --runs 2

The first run pairs this computer with Buddy: approve the card on the phone. The token is kept in
~/.config/buddy-bench/<phone>.json. Results go to benchmark/local/results-local.jsonl (one line per run) and a summary
is printed at the end.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PORT = 8765  # McpServer.PORT

# Keep in step with Tools.LEAN in app/src/main/java/app/buddy/assistant/Tools.java
LEAN = [
    "read_screen", "tap", "type_text", "scroll", "press", "open_app", "open_url", "compose_message", "find_contact",
    "wait_for", "find", "do_steps", "confirm", "ask", "notify", "set_alarm", "set_timer", "device_status",
    "calendar_events", "add_calendar_event",
]
# The benchmark never sends, buys or changes anything: a tool that would ask the user answers for them here
NO_USER = {"confirm": "DENIED: this is a benchmark run, nothing may be sent or changed. Stop and report what you would "
                      "have done.",
           "ask": "NO ANSWER: this is a benchmark run, nobody is there to answer. Decide yourself or stop."}


def http(url, body=None, headers=None, timeout=900):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace")
    if text.lstrip().startswith(("event:", "data:")):  # a server answering as SSE: the last data line is the reply
        lines = [l[5:].strip() for l in text.splitlines() if l.startswith("data:")]
        text = lines[-1] if lines else "{}"
    return json.loads(text) if text.strip() else {}


class Phone:
    """Buddy's phone tools over Tailscale (Tailnet.java): POST /pair once, then JSON-RPC on /mcp with the token."""

    def __init__(self, host):
        self.host = host
        self.url = f"http://{host}:{PORT}/mcp"
        self.headers = {"Authorization": "Bearer " + self._token()}
        self.id = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "buddy-bench", "version": "1"}})

    def _token(self):
        path = os.path.expanduser(f"~/.config/buddy-bench/{re.sub(r'[^A-Za-z0-9._-]', '_', self.host)}.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)["headers"]["Authorization"][len("Bearer "):]
        print(f"Pairing with Buddy on {self.host}: approve the card on the phone…", file=sys.stderr)
        cfg = http(f"http://{self.host}:{PORT}/pair?name=" + urllib.parse.quote("buddy-bench"), body={}, timeout=180)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(cfg, f)
        os.chmod(path, 0o600)
        self.url = cfg.get("url", self.url)
        return cfg["headers"]["Authorization"][len("Bearer "):]

    def rpc(self, method, params=None):
        self.id += 1
        r = http(self.url, {"jsonrpc": "2.0", "id": self.id, "method": method, "params": params or {}}, self.headers)
        if "error" in r:
            raise RuntimeError(r["error"].get("message", str(r["error"])))
        return r.get("result", {})

    def tools(self):
        return self.rpc("tools/list")["tools"]

    def call(self, name, args):
        if name in NO_USER:
            return NO_USER[name]
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        parts = []
        for c in r.get("content", []):
            parts.append(c.get("text", "") if c.get("type") == "text" else f"[{c.get('type')} omitted]")
        return "\n".join(parts) or "(no result)"


def chat(base, key, model, messages, tools, timeout):
    body = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.2}
    headers = {"Authorization": "Bearer " + key} if key else {}
    return http(base.rstrip("/") + "/chat/completions", body, headers, timeout)


def run_task(phone, args, task, mode, all_tools, prompt):
    tools = [t for t in all_tools if mode == "full" or t["name"] in LEAN]
    if mode == "lean":
        tools.sort(key=lambda t: LEAN.index(t["name"]))
    fns = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                             "parameters": t.get("inputSchema", {"type": "object", "properties": {}})}}
           for t in tools]
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": task["prompt"]}]
    phone.call("press", {"button": "home"})  # every task starts on the home screen
    time.sleep(1)
    start, steps, tokens, reply, error = time.time(), 0, 0, "", ""
    try:
        for _ in range(args.max_turns):
            r = chat(args.base_url, args.api_key, args.model, messages, fns, args.timeout)
            tokens += (r.get("usage") or {}).get("prompt_tokens", 0)
            msg = r["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                reply = (msg.get("content") or "").strip()
                break
            for c in calls:
                steps += 1
                fn = c["function"]
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    out = "Error: the arguments aren't valid JSON."
                else:
                    try:
                        out = phone.call(fn["name"], a) if any(t["name"] == fn["name"] for t in tools) \
                            else f"Error: there is no tool {fn['name']}."
                    except Exception as e:  # a tool that failed is the model's to handle, like in Buddy
                        out = f"Error: {e}"
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out[:12000]})
        else:
            error = f"no answer after {args.max_turns} turns"
    except (urllib.error.URLError, RuntimeError, KeyError, TimeoutError) as e:
        error = str(e)
    ok = None
    if task.get("expect") and not error:
        ok = re.search(task["expect"], reply, re.I) is not None
    return {"task": task["id"], "model": args.model, "mode": mode, "ms": int((time.time() - start) * 1000),
            "tools": steps, "prompt_tokens": tokens, "reply": reply, "ok": ok, "error": error}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phone", required=True, help="the phone's Tailscale address or MagicDNS name")
    ap.add_argument("--base-url", required=True, help="OpenAI-compatible address, e.g. http://localhost:8000/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    ap.add_argument("--mode", choices=["lean", "full", "both"], default="both")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--tasks", default=os.path.join(HERE, "tasks.json"))
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--contact", help="a contact's name for the compose task (it opens the chat, never sends)")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=900, help="seconds per model call")
    ap.add_argument("--out", default=os.path.join(HERE, "results-local.jsonl"))
    args = ap.parse_args()

    with open(args.tasks) as f:
        tasks = json.load(f)
    if args.only:
        keep = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep]
    if args.contact:
        for t in tasks:
            t["prompt"] = t["prompt"].replace("{contact}", args.contact)
    else:
        tasks = [t for t in tasks if "{contact}" not in t["prompt"]]
    with open(os.path.join(REPO, "linux", "system-prompt.md")) as f:
        full_prompt = f.read()
    with open(os.path.join(REPO, "linux", "system-prompt-lean.md")) as f:
        lean_prompt = f.read()

    phone = Phone(args.phone)
    all_tools = phone.tools()
    modes = ["lean", "full"] if args.mode == "both" else [args.mode]
    results = []
    with open(args.out, "a") as out:
        for rnd in range(args.runs):
            for task in tasks:
                for mode in modes:
                    r = run_task(phone, args, task, mode, all_tools, lean_prompt if mode == "lean" else full_prompt)
                    r["round"] = rnd
                    results.append(r)
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
                    out.flush()
                    mark = "?" if r["ok"] is None else ("✓" if r["ok"] else "✗")
                    print(f"[{rnd}] {task['id']:<10} {mode:<5} {mark} {r['ms'] / 1000:6.1f}s {r['tools']:3d} steps"
                          f"{'  ERROR ' + r['error'] if r['error'] else ''}", file=sys.stderr)

    print(f"\n{'mode':<6}{'correct':>10}{'median s':>10}{'median steps':>14}{'prompt tok/run':>16}")
    for mode in modes:
        rs = [r for r in results if r["mode"] == mode]
        judged = [r for r in rs if r["ok"] is not None]
        good = sum(1 for r in judged if r["ok"])
        print(f"{mode:<6}{f'{good}/{len(judged)}':>10}{statistics.median(r['ms'] for r in rs) / 1000:>10.1f}"
              f"{statistics.median(r['tools'] for r in rs):>14.0f}"
              f"{statistics.median(r['prompt_tokens'] for r in rs):>16.0f}")
    print(f"\nRuns without an expected answer (marked ?) need checking by hand: {args.out}")


if __name__ == "__main__":
    main()
