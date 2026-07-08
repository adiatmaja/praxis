# Open-Weight Models for Agentic Coding — Complete List

> **Source:** [Artificial Analysis](https://artificialanalysis.ai/) Intelligence Index v4.1,
> SWE-bench Verified, and Aider code-editing benchmarks (July 2026). Models are ranked by
> **agentic coding capability** — edit-format compliance, SWE-bench resolve rate, and
> reliability in agentic loops — not general intelligence.

## Context: Why This List Exists

Praxis splits work into two tiers: a **brain** (planning + review) and **hands** (coding
implementation). The hands tier runs on an open-weight model served over an OpenAI-compatible
endpoint (LM Studio by default; Ollama or a hosted endpoint work too). Because the brain
decomposes plans into small, focused, single-responsibility tasks, the minimum capability
bar for the coding model is *lower* than if the model had to do everything itself. The
worker only needs to:

1. Follow an edit format (diff / search-replace) reliably
2. Understand a narrowly-scoped task description
3. Read existing code and produce correct, minimal patches
4. Maintain instruction-following discipline over a single coding session

This list ranks **all open-weight models** relevant to agentic coding — including both
API-only and locally-runnable models. For the filtered list of models loadable in LM Studio
/ available on Hugging Face, see
[open-weight-models-lmstudio.md](open-weight-models-lmstudio.md).

---

## Minimum Recommended Parameters

| Decomposition Level | Minimum Params | Why |
|---------------------|---------------|-----|
| **Fine-grained** (single-function tasks) | **7B** (coder-specialized) | Strict task scope + repair loops compensate for lower reasoning |
| **Standard** (single-file tasks) | **14B** (coder-specialized) | Sweet spot — handles most decomposed tasks reliably |
| **Coarse** (multi-file tasks) | **27B+** | Needs broader reasoning to coordinate across files |
| **No decomposition** (full spec) | **70B+** or frontier | Requires full planning + coding + review in one model |

> **Key insight:** Coder-specialized models (Qwen2.5-Coder, Qwen3-Coder) dramatically
> outperform general-purpose models of the same size. A **Qwen2.5-Coder-14B** matches or
> beats a general-purpose 27B model on agentic coding tasks.

---

## Tier 1 — Frontier Open-Weight (Server-Class, Multi-GPU)

These require significant infrastructure (multi-GPU, 80GB+ VRAM per GPU) but represent
the ceiling for open-weight agentic coding.

| Rank | Model | Provider | Parameters | Arch | SWE-bench Verified | Key Strengths |
|------|-------|----------|-----------|------|-------------------|---------------|
| 1 | **Qwen3-Coder-480B-A35B** | Alibaba | 480B / 35B active | MoE | ~70% | Dominant coding MoE, highest open-weight SWE-bench |
| 2 | **GLM-5.2 (max)** | Z.ai (Zhipu) | >150B | Dense | Top-tier | Frontier reasoning, 1M context, strong code |
| 3 | **DeepSeek V4 Pro (max)** | DeepSeek | 1.6T / 49B active | MoE | ~65%+ | Best cost-per-task, strong agentic workflows |
| 4 | **Kimi K2.7 Code** | Moonshot AI | Large MoE | MoE | ~70%+ (multi-attempt) | Optimized for agentic multi-attempt coding |
| 5 | **MiniMax-M3** | MiniMax | >150B | Dense | Competitive | 1M context, multimodal |
| 6 | **NVIDIA Nemotron 3 Ultra** | NVIDIA | 550B / 55B active | MoE | Strong | US-based, reasoning-optimized |

### Praxis Relevance

These are **overkill for Praxis workers** — they shine as brains. If running Praxis on
server infrastructure and wanting a single open-weight model for both brain *and* hands,
DeepSeek V4 Pro or GLM-5.2 are the strongest choices.

---

## Tier 2 — High-Performance (24–48GB VRAM)

The **sweet spot for Praxis workers**: strong enough to follow edit formats reliably on
standard decomposed tasks. These models are validated or expected to pass first-attempt
review for most single-file tasks.

| Rank | Model | Provider | Parameters | Arch | AA Index | SWE-bench | Aider Score | Praxis Tested |
|------|-------|----------|-----------|------|----------|-----------|------------|---------------|
| 1 | **Qwen3.6-27B** | Alibaba | 27B | Dense | — | 77.2% | — | ✅ Validated |
| 2 | **Qwen3.6-35B-A3B** | Alibaba | 35B / 3B active | MoE | — | 73.4% | — | ✅ Validated |
| 3 | **Qwen2.5-Coder-32B-Instruct** | Alibaba | 32B | Dense | — | Strong | 73.7% | |
| 4 | **Qwen3-Coder-Next (80B/3B)** | Alibaba | 80B / 3B active | MoE | 21 | 70.6% | — | |
| 5 | **DeepSeek-V3.2-Lite** | DeepSeek | ~27B | Dense | — | Good | — | |

### Why Coder Models Rank Higher

General-purpose models (like Gemma 4 31B) score well on intelligence benchmarks but
**struggle with agentic coding** — specifically:
- Poor edit-format compliance (outputting chat-style code instead of diffs)
- Tool-call schema violations (missing mandatory fields)
- Infinite loops in agent scaffolds (repeating tool calls without progress)

Coder-specialized models are trained on structured code editing and tool-use patterns,
making them far more reliable in Praxis's agentic loop.

---

## Tier 3 — Mid-Size Models (12–16GB VRAM)

These are **the most important tier for accessibility** — they run on mainstream consumer
GPUs (RTX 4070, RTX 4060 Ti 16GB, Apple M-series 16GB+). Expect 1–2 retries per task
with standard decomposition, or first-pass success with fine-grained decomposition.

| Rank | Model | Provider | Parameters | Arch | SWE-bench | Aider Score | VRAM (Q4) | Notes |
|------|-------|----------|-----------|------|-----------|------------|-----------|-------|
| 1 | **Qwen2.5-Coder-14B-Instruct** | Alibaba | 14B | Dense | Good | ~55–60% | ~8GB | **Best 14B coder**, matches general 27B models on coding |
| 2 | **Qwen3-14B-Instruct** | Alibaba | 14B | Dense | Good | — | ~8GB | Strong general + coding, multilingual |
| 3 | **Phi-4 14B** | Microsoft | 14B | Dense | Good | — | ~8GB | Best reasoning at this size |
| 4 | **DeepSeek-R1-Distill-Qwen-14B** | DeepSeek | 14B | Dense | Good | — | ~8GB | Chain-of-thought reasoning, strong debugging |
| 5 | **Mistral Small 3.1 24B** | Mistral | 24B | Dense | Moderate | — | ~14GB | Good instruction following |

### Praxis Notes for Mid-Size Models

- **Qwen2.5-Coder-14B is the recommended minimum** for reliable Praxis worker use
- Decompose tasks to single-file, single-concern scope for best results
- These models can follow Aider and OpenCode edit formats when properly prompted
- Context window of 128K (Qwen2.5-Coder) is more than enough for decomposed tasks

---

## Tier 4 — Small Models (4–8GB VRAM)

⚠️ **High-risk tier.** These models *can* produce working patches when:
1. Tasks are decomposed to single-function granularity
2. The harness provides strong error-repair loops
3. You accept 2–4x retry rates vs. larger models

This tier matters because **many developers only have 8GB GPUs or run on CPU+RAM**.

| Rank | Model | Provider | Parameters | Arch | Coding Ability | VRAM (Q4) | Edit Format | Notes |
|------|-------|----------|-----------|------|---------------|-----------|-------------|-------|
| 1 | **Qwen2.5-Coder-7B-Instruct** | Alibaba | 7B | Dense | Good for size | ~4GB | ⚠️ Inconsistent | Best small coder; works for simple single-function patches |
| 2 | **Qwen3-8B** | Alibaba | 8B | Dense | Moderate | ~5GB | ⚠️ Inconsistent | Stronger general reasoning than Coder-7B |
| 3 | **DeepSeek-R1-Distill-Qwen-7B** | DeepSeek | 7B | Dense | Moderate | ~4GB | ⚠️ Inconsistent | Chain-of-thought helps with debugging tasks |
| 4 | **IBM Granite 4.1 8B** | IBM | 8B | Dense | Moderate | ~5GB | ⚠️ Inconsistent | Competitive with Qwen at this size |
| 5 | **Phi-4-mini 3.8B** | Microsoft | 3.8B | Dense | Basic | ~2GB | ❌ Unreliable | Edge-only; boilerplate generation only |
| 6 | **Qwen2.5-Coder-3B** | Alibaba | 3B | Dense | Basic | ~2GB | ❌ Unreliable | Smallest viable coder; trivial patches only |

### How to Make Small Models Work in Praxis

If you're constrained to a 7B–8B model, these strategies improve success rates:

1. **Ultra-fine decomposition**: Break each task into single-function changes
2. **Increase max retries**: Set retry limit to 4–5 instead of the default 3
3. **Simplify task descriptions**: Short, explicit instructions > nuanced specs
4. **Use Aider harness**: Aider's repair loop is more forgiving of format errors
5. **Verify gate**: Set a `verify_cmd` (e.g. `pytest`) so broken patches fail fast
6. **Accept the trade-off**: Small model savings only hold if retry rates don't
   consume all your planner review budget

### Success Rates by Model Size (Estimated)

| Model Size | Tasks per Plan-Success | Avg Retries per Task | Planner Reviews Burned |
|-----------|----------------------|---------------------|----------------------|
| 27B+ (coder) | 8–10 ✅ | 1.1 | Low |
| 14B (coder) | 6–8 ✅ | 1.5 | Moderate |
| 7–8B (coder) | 4–6 ⚠️ | 2.5 | High |
| 3–4B | 1–3 ❌ | 4+ | Very High (net negative) |

---

## Tier 5 — Known Failures

These models consistently fail Praxis's edit-format compliance requirements.

| Model | Parameters | Failure Mode |
|-------|-----------|-------------|
| Qwen3.5-9B (non-coder) | 9B | Chat-style code replies, no edit compliance |
| Gemma 4 31B IT | 31B | Tool-call schema violations, infinite agent loops, format non-compliance |
| Gemma 3n E4B | ~4B eff | Too small + Gemma format issues compound |
| Llama 3.x-8B (all variants) | 8B | Inconsistent instruction following for structured edits |
| CodeLlama 7B/13B | 7B/13B | Superseded; poor agentic loop compliance |
| StarCoder2 | 3B/7B/15B | Code completion model, not instruction-tuned for edits |
| Mistral 7B (original) | 7B | Weak instruction following for agent use |

> **Why Gemma fails despite high benchmarks:** Gemma models score well on general
> intelligence (AA Index) but have documented issues with agentic coding — specifically
> tool-call schema violations (missing mandatory fields like `path`), infinite tool loops
> (repeating `read_file` without progress), and sensitivity to chat template
> misconfiguration. These are structural issues, not a matter of intelligence.

---

## Benchmark Definitions

| Benchmark | What It Measures | Why It Matters for Praxis |
|-----------|-----------------|--------------------------|
| **SWE-bench Verified** | Resolving real GitHub issues end-to-end | Closest proxy to Praxis worker tasks |
| **Aider Code Editing** | Edit-format compliance + code repair | Directly tests the skill Praxis needs |
| **SWE-bench Pro** | Multi-file diffs on complex repos | Measures harder decomposed tasks |
| **LiveCodeBench** | Fresh coding problems (contamination-free) | Raw code generation ability |
| **Terminal-Bench** | Shell scripting, DevOps tasks | Relevant for infra-related plans |

---

## Selection Guide by Hardware

| Your GPU VRAM | Recommended Model | Quantization | Expected Quality |
|--------------|-------------------|-------------|-----------------|
| **48GB+** | Qwen3.6-27B (FP16) | FP16 / BF16 | ⭐⭐⭐⭐⭐ Excellent |
| **24GB** | Qwen3.6-27B or Qwen2.5-Coder-32B | Q4_K_M | ⭐⭐⭐⭐⭐ Excellent |
| **16GB** | Qwen3.6-35B-A3B (MoE) or Qwen2.5-Coder-14B | Q4_K_M | ⭐⭐⭐⭐ Very Good |
| **12GB** | Qwen2.5-Coder-14B | Q5_K_M | ⭐⭐⭐ Good |
| **8GB** | Qwen2.5-Coder-7B | Q4_K_M | ⭐⭐ Fair (fine decomposition required) |
| **4GB** | Qwen2.5-Coder-3B | Q4_K_M | ⭐ Marginal (trivial patches only) |
| **CPU only** | Qwen2.5-Coder-7B | Q3_K_S | ⚠️ Very slow, last resort |

---

## Data Sources & Methodology

- **Intelligence Index scores** from [Artificial Analysis](https://artificialanalysis.ai/models)
  Intelligence Index v4.1 (updated June 2026)
- **SWE-bench scores** from [swebench.com](https://swebench.com/) and official model technical reports
- **Aider scores** from [aider.chat/docs/leaderboards](https://aider.chat/docs/leaderboards/)
- **Praxis validation** from live runs documented in `docs/superpowers/specs/`
- Rankings prioritize **agentic coding** (edit-format compliance + SWE-bench) over
  general intelligence, which is the correct metric for Praxis workers
- Rankings are **point-in-time** (July 2026) and will shift as new models release

> **Last updated:** 2026-07-05
