# Serving Qwen on vLLM for Claude Code: keep the prefix cache

The tools work with any server that speaks the Anthropic Messages API (see
[`configuration.md`](configuration.md#model-servers) for what they read from
`/v1/models`). This page is about one vLLM-specific problem that costs most of the
server's prefix cache under Claude Code, and the one-line chat-template change that
fixes it.

**Symptom.** Claude Code against vLLM's `/v1/messages` gets near-zero prefix-cache
hits. At startup vLLM logs a warning that the chat template "requires system-first
ordering" and that the conversation "misses the prefix cache".

**Cause.** Claude Code sends per-turn reminders as inline system messages. When the
model's chat template rejects non-leading system messages (Qwen templates raise
"System message must be at the beginning."), vLLM merges them into the leading
system prompt, so the prompt prefix changes every turn.

**Fix.** Copy your model's `chat_template.jinja`, change the raise into rendering
the message in place as a user turn, and start vLLM with `--chat-template` pointing
at the copy:

```diff
     {%- if message.role == "system" %}
         {%- if not loop.first %}
-            {{- raise_exception('System message must be at the beginning.') }}
+            {{- '<|im_start|>user\n' + content + '<|im_end|>\n' }}
         {%- endif %}
```

**Verify.** The startup warning disappears; prompts without inline system messages
render unchanged.

**Measured.** One Claude Code review task went from 0% to 53-56% of prompt tokens
served from cache; a long `qwen-agent --until-done` session reached ~94%. On hybrid
(linear-attention) models the cache reuses whole blocks only, so short prompts can
still show 0%.

**Upstream.** [vllm-project/vllm#53393](https://github.com/vllm-project/vllm/issues/53393)
and [vllm-project/vllm#58772](https://github.com/vllm-project/vllm/pull/58772) (a
configurable fix; retire this workaround once it lands and you have measured it).

**Related.** `--until-done` resumes the same session each round with a precise "not
done" prompt rather than starting over, so the prompt prefix stays stable and the
server's cache warm. Concurrency figures for one server are in
[`benchmark.md`](benchmark.md).
