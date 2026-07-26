"""Print the training venv's torch + CUDA status. Called by setup_train.ps1."""
try:
    import torch
    print("  torch", torch.__version__, "| CUDA", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("  gpu", torch.cuda.get_device_name(0))
except Exception as exc:  # noqa: BLE001
    print("  torch not importable:", exc)
