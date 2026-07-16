# Open-Weight Models for Praxis Workers — LM Studio & Hugging Face

> **Source:** [Artificial Analysis](https://artificialanalysis.ai/) Intelligence Index v4.1,
> SWE-bench Verified, and Aider code-editing benchmarks (July 2026). Filtered to models
> available as GGUF on Hugging Face and loadable in [LM Studio](https://lmstudio.ai/).
> Ranked by **agentic coding capability**, not general intelligence.

## Purpose

This list covers **only models you can download and run locally today** via LM Studio or
from Hugging Face. These are the models that fit Praxis's **hands** tier — executing
decomposed tasks from a brain-authored plan.

For the complete list including server-class models, see
[open-weight-models-complete.md](open-weight-models-complete.md).

---

## What Makes a Good Praxis Worker Model

Praxis decomposes plans into small, focused tasks. The worker model needs to:

- ✅ Follow a coding agent's **edit format** (diff / search-replace) — not just reply with code
- ✅ Understand a **narrowly-scoped task description** from the plan
- ✅ Produce **correct, minimal patches** against existing code
- ✅ Handle **32K+ context** (the minimum Praxis checks for)
- ❌ Does NOT need to plan, architect, or review — the brain handles that

### Why Coder Models > General Models

A **coder-specialized 14B** (e.g. Qwen2.5-Coder-14B) outperforms a **general-purpose 31B**
(e.g. Gemma 4 31B) on agentic coding tasks. General models often fail at edit-format
compliance — they reply with code in chat instead of producing structured diffs. Coder
models are trained specifically on code editing patterns and tool-use workflows.

---

## Minimum Recommended Parameters

| Task Scope (after brain decomposition) | Minimum Model | VRAM Needed |
|---------------------------------------|--------------|-------------|
| Single-function patches | **Qwen2.5-Coder-7B** | ~4GB |
| Single-file changes | **Qwen2.5-Coder-14B** ← recommended minimum | ~8GB |
| Multi-file changes | **Qwen3.6-27B** | ~16GB |
| Complex refactoring | **Qwen3.6-27B** or larger | ~24GB |

---

## Recommended Models — Ranked by Agentic Coding Performance

### 🥇 Tier 1: Recommended (24GB VRAM — Reliable First-Pass Success)

These models reliably pass review on the first attempt for most decomposed tasks.

| Rank | Model | Params | Arch | VRAM (Q4) | AA Index | SWE-bench | Aider | Praxis Tested |
|------|-------|--------|------|-----------|----------|-----------|-------|---------------|
| 1 | **Qwen3.6-27B** | 27B | Dense | ~16GB | — | 77.2% | — | ✅ Validated |
| 2 | **Qwen3.6-35B-A3B** | 35B/3B active | MoE | ~14GB | — | 73.4% | — | ✅ Validated |
| 3 | **Qwen2.5-Coder-32B-Instruct** | 32B | Dense | ~18GB | — | Strong | 73.7% | |
| 4 | **Qwen3-Coder-Next (80B/3B)** | 80B/3B active | MoE | ~16GB | 21 | 70.6% | — | |
| 5 | **DeepSeek-V3.2-Lite** | ~27B | Dense | ~16GB | — | Good | — | |

**LM Studio setup:** Search the model name → pick `Q4_K_M` or `Q5_K_M` quantization →
verify loaded context window ≥ 32,768 tokens.

---

### 🥈 Tier 2: Mid-Size (8–16GB VRAM — The Accessibility Sweet Spot)

These run on **mainstream consumer GPUs** (RTX 4060 Ti 16GB, RTX 4070, Apple M-series 16GB+).
This is the most important tier for users who can't load 27B+ models.

| Rank | Model | Params | Arch | VRAM (Q4) | SWE-bench | Aider | Notes |
|------|-------|--------|------|-----------|-----------|-------|-------|
| 6 | **Qwen2.5-Coder-14B-Instruct** | 14B | Dense | ~8GB | Good | ~55–60% | **Recommended minimum** — matches general 27B on coding |
| 7 | **Qwen3-14B-Instruct** | 14B | Dense | ~8GB | Good | — | Strong general + coding, multilingual |
| 8 | **Phi-4 14B** | 14B | Dense | ~8GB | Good | — | Best reasoning at 14B size |
| 9 | **DeepSeek-R1-Distill-Qwen-14B** | 14B | Dense | ~8GB | Good | — | CoT reasoning, strong debugging |
| 10 | **Mistral Small 3.1 24B** | 24B | Dense | ~14GB | Moderate | — | Good instruction following |

#### How to maximize success with 14B models:
- Decompose tasks to **single-file, single-concern** scope
- Use explicit, short task descriptions (avoid nuanced specs)
- Set `verify_cmd` (e.g. `pytest`) so broken patches fail fast via the verify gate
- Expect ~1.5 retries per task vs ~1.1 for 27B models

---

### 🥉 Tier 3: Small (4–8GB VRAM — Resource-Constrained)

⚠️ **High-risk tier but still viable** with proper strategies. These matter because many
developers run on 8GB GPUs, laptops, or even CPU-only setups.

| Rank | Model | Params | Arch | VRAM (Q4) | Edit Format | Notes |
|------|-------|--------|------|-----------|-------------|-------|
| 11 | **Qwen2.5-Coder-7B-Instruct** | 7B | Dense | ~4GB | ⚠️ Partial | **Best small coder** — works for single-function patches |
| 12 | **Qwen3-8B** | 8B | Dense | ~5GB | ⚠️ Partial | Better general reasoning than Coder-7B |
| 13 | **DeepSeek-R1-Distill-Qwen-7B** | 7B | Dense | ~4GB | ⚠️ Partial | CoT helps debugging tasks |
| 14 | **IBM Granite 4.1 8B** | 8B | Dense | ~5GB | ⚠️ Partial | Competitive with Qwen at this size |
| 15 | **Phi-4-mini 3.8B** | 3.8B | Dense | ~2GB | ❌ Poor | Edge-only; boilerplate generation only |
| 16 | **Qwen2.5-Coder-3B** | 3B | Dense | ~2GB | ❌ Poor | Smallest viable coder; trivial patches |

#### How to make small models work in Praxis:

1. **Ultra-fine decomposition** — break each task into single-function changes
2. **Increase max retries** — set retry limit to 4–5 (default is 3)
3. **Simplify task descriptions** — short, explicit instructions beat nuanced specs
4. **Use the OpenCode harness** — its agentic loop reads files in bounded chunks and
   auto-compacts, so it tolerates weaker models better than a single-shot pass
5. **Set verify_cmd** — `pytest` or linter catches broken patches early
6. **Know the break-even** — at 3+ retries per task, the planner review cost may
   exceed the savings from running a free local model

#### Expected retry rates by size:

| Model Size | Avg Retries/Task | First-Pass Success | Planner Cost |
|-----------|-----------------|-------------------|-------------|
| 27B+ coder | ~1.1 | ~85% | Low |
| 14B coder | ~1.5 | ~70% | Moderate |
| 7–8B coder | ~2.5 | ~45% | High |
| 3–4B | ~4+ | ~20% | Very High |

---

### ❌ Tier 4: Not Recommended (Known Failures)

These models consistently fail Praxis's edit-format and agentic requirements.

| Model | Params | Why It Fails |
|-------|--------|-------------|
| **Gemma 4 31B IT** | 31B | Tool-call schema violations, infinite agent loops, format non-compliance |
| **Gemma 3n E4B** | ~4B eff | Gemma format issues + too small compounds failure |
| Qwen3.5-9B (non-coder) | 9B | Chat-style code replies, no edit compliance |
| Llama 3.x-8B (any variant) | 8B | Inconsistent instruction following for edits |
| CodeLlama 7B/13B | 7B/13B | Superseded; poor agentic compliance |
| StarCoder2 3B/7B/15B | 3–15B | Code completion, not instruction-tuned for edits |
| Mistral 7B (original) | 7B | Weak instruction following for agent use |

> **Why Gemma fails despite high benchmark scores:** Gemma models score well on general
> intelligence (AA Intelligence Index) but have documented issues with agentic coding:
> tool-call schema violations (missing mandatory fields), infinite tool-call loops
> (repeating `read_file` without progress), and chat-template sensitivity that breaks
> agent scaffolds. These are structural problems, not intelligence gaps.

---

## Quick-Start: Loading in LM Studio

1. **Open LM Studio** → go to the **Search** tab
2. Search for your chosen model (e.g., `Qwen2.5-Coder-14B-Instruct`)
3. Download a **GGUF quantization** that fits your VRAM:

   | Your VRAM | Recommended Quant | Quality |
   |-----------|-------------------|---------|
   | 24GB+ | `Q5_K_M` or `Q6_K` | Best local quality |
   | 16GB | `Q4_K_M` | Good balance |
   | 8–12GB | `Q4_K_M` (14B) or `Q3_K_M` | Acceptable |
   | 4–6GB | `Q4_K_M` (7B) | Minimum viable |
   | <4GB | `Q3_K_S` (3B) | Last resort |

4. **Load the model** → verify the context window shows **≥32,768 tokens**
   (Praxis checks this and rejects models with smaller windows)
5. Start the **local server** (default: `http://localhost:1234`)
6. In Praxis, set `LM_STUDIO_URL` to `http://host.docker.internal:1234`

---

## Hardware Recommendations

| GPU / System | Best Model Choice | Expected Quality |
|-------------|-------------------|-----------------|
| **RTX 4090 / A6000 (24GB+)** | Qwen3.6-27B Q4_K_M | ⭐⭐⭐⭐⭐ Excellent |
| **RTX 4080 (16GB)** | Qwen3.6-35B-A3B Q4_K_M | ⭐⭐⭐⭐ Very Good |
| **RTX 4070 / 4060 Ti 16GB** | Qwen2.5-Coder-14B Q4_K_M | ⭐⭐⭐ Good |
| **RTX 4060 (8GB)** | Qwen2.5-Coder-7B Q4_K_M | ⭐⭐ Fair (fine decomp required) |
| **GTX 1080 / older (8GB)** | Qwen2.5-Coder-7B Q3_K_M | ⭐⭐ Fair |
| **Apple M2/M3 (32GB unified)** | Qwen3.6-27B Q4_K_M | ⭐⭐⭐⭐ Very Good |
| **Apple M1/M2 (16GB unified)** | Qwen2.5-Coder-14B Q4_K_M | ⭐⭐⭐ Good |
| **Apple M1 (8GB unified)** | Qwen2.5-Coder-7B Q4_K_M | ⭐⭐ Fair |
| **CPU only (16GB+ RAM)** | Qwen2.5-Coder-7B Q3_K_S | ⚠️ Slow but works |

---

## Capabilities That Matter for Decomposed-Plan Execution

The brain does the hard thinking. The worker just needs to execute reliably:

| Capability | Importance | Why |
|-----------|-----------|-----|
| **Edit-format compliance** | 🔴 Critical | Must output diffs/search-replace, not chat-style code |
| **Instruction following** | 🔴 Critical | Must respect task boundaries, not over-engineer |
| **Code understanding** | 🟡 High | Needs to read existing files and make targeted changes |
| **Reasoning** | 🟡 High | Must understand *why* to change code |
| **Long context** | 🟢 Medium | 32K minimum; most decomposed tasks fit in 16K |
| **Multi-language** | 🟢 Medium | Python is primary; JS/TS for web frontends |
| **Planning / architecture** | ⚪ Not needed | Brain handles this entirely |
| **Code review** | ⚪ Not needed | Brain handles this entirely |

---

## Model Selection Decision Tree

```
What's your GPU VRAM?

  24GB+ → Qwen3.6-27B (Q4_K_M) — best overall
  │       ├── Want faster inference? → Qwen3.6-35B-A3B (MoE)
  │       └── Want proven edit-format compatibility? → Qwen2.5-Coder-32B
  │
  16GB  → Qwen3.6-35B-A3B (Q4_K_M) or Qwen2.5-Coder-14B (Q5_K_M)
  │       └── The MoE model is faster; the 14B coder is smaller + solid
  │
  8-12GB → Qwen2.5-Coder-14B (Q4_K_M) — recommended minimum
  │        └── Decompose tasks to single-file scope
  │
  4-8GB → Qwen2.5-Coder-7B (Q4_K_M)
  │       └── Decompose to single-function scope, increase retries to 5
  │
  <4GB  → Qwen2.5-Coder-3B — trivial patches only
  │       └── Honestly consider a cloud GPU or API-served model
  │
  CPU   → Qwen2.5-Coder-7B (Q3_K_S) — slow but functional
          └── Expect ~10x slower inference than GPU
```

---

## Compatibility with Praxis Harnesses

| Harness | Best Model Fit | Notes |
|---------|---------------|-------|
| **OpenCode** (default) | Qwen3.6-27B, Qwen2.5-Coder-14B+ | Best tested combination |
| **agy** (Gemini) | n/a — Gemini only | Does not use local open-weight models; authenticates to Gemini via OAuth |

---

## Data Sources

- [Artificial Analysis](https://artificialanalysis.ai/models) — Intelligence Index v4.1
- [SWE-bench](https://swebench.com/) — Agentic coding benchmark
- [Edit-format Leaderboard](https://aider.chat/docs/leaderboards/) — Edit-format benchmark
- [Hugging Face](https://huggingface.co/) — Model availability
- [LM Studio](https://lmstudio.ai/) — Local model serving
- Praxis live run validation — documented in project specs

> **Last updated:** 2026-07-05
