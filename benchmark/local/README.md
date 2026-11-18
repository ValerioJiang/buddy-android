# Local-model benchmark

How well a model on your own server (vLLM, Ollama, llama.cpp, LM Studio) operates a real phone through Buddy's phone
tools, and what lean mode changes: the same tasks with the full tool list and with lean mode's short one
(`Tools.LEAN`, 20 tools instead of 54, and `linux/system-prompt-lean.md` instead of the full instructions).

`bench.py` runs on a computer of your tailnet. It plays the agent itself, a plain tool-calling loop against any
OpenAI-compatible `/v1/chat/completions`, and drives the phone over **Phone tools on Tailscale** (Buddy: Settings >
Tools and connections). Python 3 standard library only.

## Run

1. In Buddy, turn on **Phone tools on Tailscale**.
2. On the computer:

   ```bash
   python3 benchmark/local/bench.py \
     --phone <phone's Tailscale IP or name> \
     --base-url http://localhost:8000/v1 \
     --model <model id as your server lists it> \
     --mode both --runs 2 --contact "Karim"
   ```

   The first run pairs the computer with Buddy: approve the card on the phone. `--contact` adds the two contact tasks
   (look up a number; open a WhatsApp chat with a message typed in). Leave it out to skip them.

Each run starts from the home screen. `confirm` and `ask` never reach you: the script answers DENIED / NO ANSWER, so
nothing is ever sent, bought or changed. Every run is appended to `results-local.jsonl`, and a summary per mode is
printed at the end:

```
mode     correct  median s  median steps  prompt tok/run
lean        9/10      41.2            11           38000
full        6/10      63.5            15           91000
```

(Illustrative layout, not a measurement.)

## Tasks

`tasks.json`: the five tasks of the main [benchmark](../README.md) (Settings, Calculator, Chrome, Maps, YouTube), plus
`contact` and `compose`. A task with `expect` is checked against the reply with that regular expression; the others
(YouTube, compose) are marked `?` and need a look at the phone or the reply.

## Limits

This is the model and the tool list on their own, without Buddy's agents (OpenCode, Claude Code…), which add their own
instructions, retries and context handling. Use it to compare models and lean against full on your server, not as a
prediction of a chat's exact timing.
