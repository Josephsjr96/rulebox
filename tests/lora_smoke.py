"""Optional real training test. Tiny RANDOM model: validates plumbing, not quality.

Run after installing .[lora]: python tests/lora_smoke.py
No model downloads and no persistent adapters.
"""
from argparse import Namespace
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.trainers import WordLevelTrainer
from transformers import LlamaConfig, LlamaForCausalLM, PreTrainedTokenizerFast
from rulebox.lora import train, prompt, LocalRuleWriter

torch.set_num_threads(2)
with tempfile.TemporaryDirectory(prefix="rulebox-lora-smoke-") as folder:
    root = Path(folder)
    base = root / "base"
    pairs = [{"source":"Keep answers short unless I ask for detail.",
              "instruction":"Be concise unless asked for detail."},
             {"source":"Ask before adding dependencies.",
              "instruction":"Ask before adding dependencies."}]
    tokenizer = Tokenizer(WordLevel(unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.train_from_iterator([prompt(p["source"]) + p["instruction"] for p in pairs],
        WordLevelTrainer(special_tokens=["[UNK]","[PAD]","[EOS]"]))
    wrapped = PreTrainedTokenizerFast(tokenizer_object=tokenizer,
        unk_token="[UNK]",pad_token="[PAD]",eos_token="[EOS]")
    wrapped.save_pretrained(base)
    config = LlamaConfig(vocab_size=len(wrapped),hidden_size=32,intermediate_size=64,
        num_hidden_layers=1,num_attention_heads=2,num_key_value_heads=2,
        max_position_embeddings=512,eos_token_id=wrapped.eos_token_id,
        pad_token_id=wrapped.pad_token_id)
    original = LlamaForCausalLM(config)
    expected = original.model.layers[0].self_attn.q_proj.weight.detach().clone()
    original.save_pretrained(base)
    data = root / "pairs.jsonl"
    data.write_text("\n".join(json.dumps(p) for p in pairs))
    output = root / "adapter"
    train(Namespace(base=str(base),data=str(data),output=str(output),
        rank=2,epochs=1,max_length=256,targets="q_proj,v_proj",learning_rate=0.001))
    writer = LocalRuleWriter(str(base),str(output))
    layer = writer.model.base_model.model.model.layers[0].self_attn.q_proj
    assert torch.equal(expected,layer.base_layer.weight.detach().cpu())
    assert torch.count_nonzero(layer.lora_B["default"].weight).item() > 0
    result = writer.draft(pairs[0]["source"])
    assert result["source"] == pairs[0]["source"]
    assert result["instruction"]
    assert (output / "adapter_model.safetensors").exists()
    print("PASS: LoRA updated adapter weights, preserved base weights, saved/reloaded and generated a draft.")
    print("Random tiny model only. This does NOT establish rule quality.")
