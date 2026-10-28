"""Optional LoRA training and local drafting. No hosted Codex weight access."""
import argparse
import json
from pathlib import Path

from .core import draft_rule, text

PREFIX = "Rewrite the user preference as one clear agent instruction. Preserve every condition and exception. Do not add permissions or requirements. Output only the instruction.\n\nPreference: "


def prompt(source):
    return PREFIX + source + "\n\nInstruction: "


def load_examples(path):
    examples = []
    for line_no, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"Line {line_no}: expected an object")
        examples.append({"source": text(row.get("source"), "Source"),
                         "instruction": text(row.get("instruction"), "Instruction")})
    if not examples:
        raise ValueError("Training dataset is empty")
    return examples


def encode_example(tokenizer, row, max_length):
    inputs = tokenizer.encode(prompt(row["source"]), add_special_tokens=True)
    target = tokenizer.encode(row["instruction"], add_special_tokens=False)
    if tokenizer.eos_token_id is not None:
        target.append(tokenizer.eos_token_id)
    # Reject rather than silently losing a condition or the entire answer.
    if len(inputs) + len(target) > max_length:
        raise ValueError("An example exceeds max_length; shorten it or increase the limit")
    if not target:
        raise ValueError("Empty target token sequence")
    return {"input_ids": inputs + target,
            "attention_mask": [1] * (len(inputs) + len(target)),
            "labels": [-100] * len(inputs) + target}


class LocalRuleWriter:
    def __init__(self, base, adapter):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            from peft import PeftModel
        except ImportError as exc:
            raise RuntimeError("Install the lora extra first: pip install -e '.[lora]'") from exc
        # Serving is offline-only. Explicit model downloads belong in setup/training.
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        self.model = PeftModel.from_pretrained(
            AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False),
            adapter, local_files_only=True).eval()
        self.model.to("cuda" if torch.cuda.is_available() else "cpu")

    def draft(self, source):
        base = draft_rule(source)
        encoded = self.tokenizer(prompt(base["source"]), return_tensors="pt")
        context = getattr(self.model.config, "max_position_embeddings", 2048)
        if encoded["input_ids"].shape[1] + 192 > min(context, 2048):
            raise ValueError("Instruction is too long for the local writer; shorten it")
        encoded = {k: v.to(self.model.device) for k, v in encoded.items()}
        with self.torch.inference_mode():
            output = self.model.generate(**encoded, max_new_tokens=192, do_sample=False,
                pad_token_id=self.tokenizer.eos_token_id)
        generated = self.tokenizer.decode(output[0, encoded["input_ids"].shape[1]:], skip_special_tokens=True)
        base["instruction"] = text(generated, "Generated rule")
        base["note"] = "Drafted by your local LoRA model. Check conditions, exceptions and meaning before saving."
        return base


def train(args):
    import torch
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if args.epochs < 1 or args.max_length < 64 or args.rank < 1:
        raise ValueError("Invalid epochs, max_length or rank")
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be new or empty")
    examples = load_examples(args.data)
    torch.manual_seed(42)
    # --base can be a local directory or an explicitly chosen Hub model ID.
    tokenizer = AutoTokenizer.from_pretrained(args.base, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(args.base, trust_remote_code=False)
    context = getattr(model.config, "max_position_embeddings", args.max_length)
    if args.max_length > context:
        raise ValueError("max_length exceeds base model context")
    encoded = [encode_example(tokenizer, row, args.max_length) for row in examples]
    config = LoraConfig(task_type=TaskType.CAUSAL_LM, r=args.rank,
                        lora_alpha=args.rank * 2, lora_dropout=0.05,
                        target_modules=[x.strip() for x in args.targets.split(",")])
    model = get_peft_model(model, config)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device).train()
    model.print_trainable_parameters()
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=args.learning_rate)
    losses = []
    # One example per step avoids padding accidentally contributing to the loss.
    for epoch in range(args.epochs):
        total = 0.0
        for index in torch.randperm(len(encoded)).tolist():
            batch = {k: torch.tensor([v], device=device) for k, v in encoded[index].items()}
            optimizer.zero_grad(set_to_none=True)
            loss = model(**batch).loss
            if not torch.isfinite(loss):
                raise RuntimeError("Non-finite loss; training stopped")
            loss.backward()
            torch.nn.utils.clip_grad_norm_((p for p in model.parameters() if p.requires_grad), 1.0)
            optimizer.step()
            total += loss.item()
        losses.append(total / len(encoded))
        print(f"epoch {epoch+1}: training loss {losses[-1]:.4f}", flush=True)
    output.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(output)
    tokenizer.save_pretrained(output)
    (output / "rulebox-training.json").write_text(json.dumps({
        "base": args.base, "examples": len(encoded), "epochs": args.epochs,
        "rank": args.rank, "targets": args.targets, "training_loss": losses,
        "quality_validated": False,
        "note": "Training loss is not evidence of instruction fidelity. Evaluate on held-out cases."}, indent=2))
    print(f"Adapter saved to {output}. Evaluate before use.")


def main():
    parser = argparse.ArgumentParser(description="Train a local Rulebox rule writer with LoRA")
    parser.add_argument("--base", required=True, help="Compatible causal model directory or Hub ID; may download weights")
    parser.add_argument("--data", required=True, help="Reviewed source/instruction JSONL pairs")
    parser.add_argument("--output", required=True)
    parser.add_argument("--targets", default="q_proj,v_proj", help="Module names must match the chosen architecture")
    parser.add_argument("--rank", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--learning-rate", type=float, default=0.0002)
    args = parser.parse_args()
    train(args)


if __name__ == "__main__":
    main()
