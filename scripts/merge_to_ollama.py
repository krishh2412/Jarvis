"""Fold the trained LoRA adapter into the base model and import it into Ollama.

Run after train_lora.py, in the training venv:
    .venv-train\\Scripts\\python.exe scripts\\merge_to_ollama.py

Produces a merged model under data/finetune/merged, then asks Ollama to create
a model named "jarvis" from it. Point the app at it by setting llm.model to
"jarvis" in config.json.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "data" / "finetune" / "adapter"
MERGED = ROOT / "data" / "finetune" / "merged"
MODELFILE = ROOT / "data" / "finetune" / "Modelfile"


def main() -> int:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if not ADAPTER.exists():
        print(f"no adapter at {ADAPTER} — run train_lora.py first.")
        return 1

    base_id = (ADAPTER / "base_model.txt").read_text(encoding="utf-8").strip()
    print(f"base   : {base_id}")
    print(f"adapter: {ADAPTER}")

    # Merge entirely on CPU. device_map="auto" would offload to disk when VRAM
    # is short (the 3060 rarely has 8 GB free), turning the merge glacial; the
    # CPU path uses system RAM (32 GB here), runs in minutes, and produces
    # identical weights.
    base = AutoModelForCausalLM.from_pretrained(
        base_id,
        dtype=torch.float16,
        device_map={"": "cpu"},
        low_cpu_mem_usage=True,
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(base, str(ADAPTER), device_map={"": "cpu"})
    print("merging adapter into base (CPU)…")
    model = model.merge_and_unload()

    MERGED.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(MERGED), safe_serialization=True)
    AutoTokenizer.from_pretrained(base_id, trust_remote_code=True).save_pretrained(str(MERGED))
    print(f"merged -> {MERGED}")

    # Ollama can import a safetensors directory for supported architectures
    # (Qwen included on recent versions). If your Ollama is too old, convert to
    # GGUF with llama.cpp's convert_hf_to_gguf.py and use FROM ./model.gguf.
    MODELFILE.write_text(
        f'FROM {MERGED.as_posix()}\n'
        f'SYSTEM "You are J.A.R.V.I.S., a dry, formal, unflappable British AI '
        f'butler. Address the user as Sir, sparingly. Report outcomes, not '
        f'process. One or two plain sentences. No markdown."\n'
        f'PARAMETER temperature 0.6\n',
        encoding="utf-8",
    )
    print(f"modelfile -> {MODELFILE}")

    print("importing into Ollama as 'jarvis'…")
    result = subprocess.run(
        ["ollama", "create", "jarvis", "-f", str(MODELFILE)],
        capture_output=True, text=True,
    )
    print(result.stdout or "")
    if result.returncode != 0:
        print("ollama create failed:")
        print(result.stderr[:1500])
        print("\nIf the error is about the architecture, convert to GGUF via "
              "llama.cpp first (convert_hf_to_gguf.py), then FROM the .gguf.")
        return 1

    print("\nDone. Set llm.model to \"jarvis\" in config.json to use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
