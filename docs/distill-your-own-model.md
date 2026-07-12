# Distill your own model

Distillation on Manifold: a big open teacher model generates training data
from your raw material, and a small student model is fine-tuned on it. You
end up with a model that is yours, does your one task well, and serves on a
cheap GPU. Everything runs through the same guarded pipeline as every other
job: budget cap, concurrency limit, safety-hook termination, full audit.

The whole loop is four jobs on one instance plus one fine-tune run:

```
your data (JSONL/CSV on the filesystem)
   |
   v
vllm-serve  (the TEACHER, e.g. Qwen3.6 27B on an H100)
   |
   v
llm-synthesize  output_format=alpaca   ->  synthesized/train.jsonl
   |
   v
axolotl-finetune  (the STUDENT, e.g. Qwen3-4B + LoRA)
   |
   v
outputs/<run>/  ->  serve it, or download the adapter
```

## 0. What you need

- Source data: one JSONL or CSV file where each line/row is one example of
  the thing you care about (support tickets, contracts, transcripts...).
  Upload it via Storage (S3-adapter regions) or the instance file browser.
- One GPU big enough for the teacher. An H100 80GB runs the Qwen3.6 27B
  preset comfortably. The student trains on the same box afterwards.

## 1. Serve the teacher

Jobs -> vllm-serve -> pick a preset sized to your GPU (the preset list is
tiered A10 / A100 / H100 / clusters). Wait for the chat panel to open; that
means the teacher is answering.

## 2. Generate the training set

Jobs -> llm-synthesize:

- `input_path`: your raw file, e.g. `raw/tickets.jsonl`
- `instruction`: the task you want the student to LEARN, written as if to
  an employee. Example: "Classify this support ticket's urgency and write a
  two-sentence triage summary as JSON with keys urgency, summary."
- `output_format`: **`alpaca`** - this is the distillation switch. Rows come
  out as `{"instruction", "input", "output"}`, which axolotl trains on
  directly with no conversion step.
- `limit`: 25 first. Read the output in Files, tighten the instruction,
  then rerun with `0` (= all records).

Result: `synthesized/train.jsonl` on the persistent filesystem. Quality
check it before training - the student can only learn what the teacher
wrote. Garbage rows in, garbage model out.

## 3. Write the axolotl config

Upload this (adjusted) as `configs/distill.yaml` on the filesystem:

```yaml
base_model: Qwen/Qwen3-4B-Instruct-2507   # the student (ungated)
load_in_8bit: false
strict: false

datasets:
  - path: /data/synthesized/train.jsonl   # llm-synthesize's output, mounted read-only
    type: alpaca
dataset_prepared_path: /tmp/axolotl/prepared
val_set_size: 0.05
output_dir: /data/output/distill-v1

adapter: lora
lora_r: 16
lora_alpha: 32
lora_dropout: 0.05
lora_target_linear: true

sequence_len: 2048
micro_batch_size: 2
gradient_accumulation_steps: 8
num_epochs: 3
learning_rate: 0.0002
optimizer: adamw_torch
lr_scheduler: cosine
bf16: true
logging_steps: 5
save_strategy: epoch
```

Path note: axolotl-finetune mounts `configs/` at `/data/config` (your yaml),
`synthesized/` at `/data/synthesized` read-only (the training set, exactly
where llm-synthesize wrote it), and `outputs/` at `/data/output`. No file
shuffling between steps.

## 4. Train the student

Jobs -> axolotl-finetune:

- `config_path`: `distill.yaml`
- `output_dir`: `distill-v1`

Watch loss in the job logs. The LoRA adapter lands in
`outputs/distill-v1/` on persistent storage - it survives the instance.

## 5. Use it

- Download the adapter folder from Files (tar.gz) and run it anywhere, or
- merge and serve on the box: vllm-serve accepts a local HF-format model
  path once merged. Merging is one `script-run` job with peft; ask the
  in-dashboard chat (Tools on) to queue it for you.

## Costs, honestly

The teacher hour dominates. Synthesizing 10k records at ~2s each is ~5.5
GPU-hours on the teacher; the LoRA fine-tune of a 4B student is typically
under an hour on the same H100. Use the pre-launch estimate on the Jobs
page - it learns from your actual runs. Auto-manage the instance if you
want the box gone the moment the last job finishes.

## Honest caveats

- Gated students (Llama, Gemma) will not pull: Manifold does not pass a
  HuggingFace token yet. Every preset and the config above are ungated.
- First axolotl run pulls a large image (several GB) - expect the job to
  sit in image-pull for a few minutes before logs move.
- Distillation quality is bounded by the teacher and your instruction.
  Iterate on step 2 with `limit` before spending on the full set.
