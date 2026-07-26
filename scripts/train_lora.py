"""QLoRA fine-tune of Qwen3 on the JARVIS dataset, for an RTX 3060 (12 GB).

Runs in an ISOLATED venv (.venv-train) so the heavy torch/CUDA/bitsandbytes
stack never touches the lean, torch-free app. Loads the base model in 4-bit,
trains small LoRA adapters on the persona dataset, and saves them to
data/finetune/adapter/.

    .venv-train\\Scripts\\python.exe scripts\\train_lora.py

Then run scripts/merge_to_ollama.py to fold the adapter into the base and
import the result into Ollama as "jarvis".

Notes for a 12 GB card: batch size 1 with gradient accumulation, 512-token
sequences (the examples are short), gradient checkpointing, 4-bit base. Stop
Ollama first to free VRAM. Qwen3-4B is the safe default here; 8B fits but is
tight — set JARVIS_BASE to override.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "finetune" / "jarvis_sft.jsonl"
OUT = ROOT / "data" / "finetune" / "adapter"

# 4B trains comfortably on 12 GB; 8B matches the app's inference model but is
# tight. Override with e.g.  set JARVIS_BASE=Qwen/Qwen3-8B
BASE = os.environ.get("JARVIS_BASE", "Qwen/Qwen3-4B")
EPOCHS = float(os.environ.get("JARVIS_EPOCHS", "3"))
MAX_LEN = int(os.environ.get("JARVIS_MAXLEN", "512"))


def main() -> int:
    import torch
    from datasets import load_dataset
    from peft import LoraConfig
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
    )
    from trl import SFTConfig, SFTTrainer

    if not DATA.exists():
        print(f"dataset missing: {DATA}\nRun scripts/gen_dataset.py first.")
        return 1
    if not torch.cuda.is_available():
        print("CUDA not available in the training venv — cannot fine-tune on GPU.")
        return 1

    print(f"base model : {BASE}")
    print(f"dataset    : {DATA}")
    print(f"gpu        : {torch.cuda.get_device_name(0)}")

    # 4-bit NF4 quantisation — the base fits in ~3-5 GB, leaving room for LoRA.
    # bfloat16 throughout: Qwen3 loads as bf16 and the Ampere-class 3060
    # supports it natively. fp16 would need a GradScaler that cannot unscale
    # bf16 gradients ("_amp_foreach_non_finite_check_and_unscale not
    # implemented for BFloat16"), so bf16 is both correct and simpler.
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )

    tokenizer = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        BASE,
        quantization_config=bnb,
        device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False

    # LoRA on the attention + MLP projections — the standard, effective set.
    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )

    dataset = load_dataset("json", data_files=str(DATA), split="train")

    def to_text(example: dict) -> dict:
        # Render each example through Qwen3's chat template. /no_think keeps the
        # trained responses terse, matching how the app runs the model.
        msgs = example["messages"]
        text = tokenizer.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=False,
        )
        return {"text": text}

    dataset = dataset.map(to_text, remove_columns=dataset.column_names)

    cfg = SFTConfig(
        output_dir=str(OUT),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        gradient_checkpointing=True,
        learning_rate=2e-4,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        logging_steps=10,
        save_strategy="epoch",
        optim="paged_adamw_8bit",
        max_length=MAX_LEN,
        dataset_text_field="text",
        report_to="none",
        bf16=True,
    )

    trainer = SFTTrainer(
        model=model,
        args=cfg,
        train_dataset=dataset,
        peft_config=lora,
        processing_class=tokenizer,
    )

    print("training… (this runs for a while; watch loss trend down)")
    trainer.train()

    OUT.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(OUT))
    tokenizer.save_pretrained(str(OUT))
    (OUT / "base_model.txt").write_text(BASE, encoding="utf-8")
    print(f"\nadapter saved -> {OUT}")
    print("Next: scripts/merge_to_ollama.py to import 'jarvis' into Ollama.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
