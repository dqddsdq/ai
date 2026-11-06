"""내 대화 기록으로 로컬 모델 파인튜닝 (QLoRA) → Ollama 모델로 등록.

대상 PC: NVIDIA GPU 16GB (예: RTX 4070 Ti SUPER). Windows 는 WSL2(Ubuntu) 안에서 실행 권장.

준비 (WSL2 Ubuntu 안에서, 한 번만):
  pip install unsloth          # torch, trl, transformers, peft, datasets 함께 설치됨

실행:
  cd _company
  python -m engine export-training           # sessions → training/dataset.jsonl
  python -m engine.finetune                  # 기본: Gemma 3 12B, 2 epoch
  python -m engine.finetune --base unsloth/gemma-3-4b-it --epochs 3   # 더 빠르게

끝나면 training/out/ 에 GGUF 와 Modelfile 이 생기고, 마지막에 출력되는
`ollama create ...` 명령으로 등록하면 `python -m engine set-model ceo ollama:my-company` 로 쓸 수 있습니다.

⚠️ 이 스크립트는 GPU 없는 환경에서 작성되어 아직 실제 학습으로 검증되지 않았습니다.
   Unsloth 버전에 따라 함수 이름이 다를 수 있으니, 첫 실행에서 오류가 나면 메시지를 그대로 알려주세요.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agents import COMPANY_DIR

DATASET = COMPANY_DIR / "training" / "dataset.jsonl"
OUT_DIR = COMPANY_DIR / "training" / "out"
MIN_EXAMPLES = 50


def load_examples(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        msgs = json.loads(line)["messages"]
        # Gemma 는 system 역할이 없어서 첫 user 메시지 앞에 합칩니다 (다른 모델에도 무해)
        if msgs and msgs[0]["role"] == "system" and len(msgs) > 1:
            sys_text = msgs[0]["content"]
            msgs = msgs[1:]
            msgs[0] = {"role": "user", "content": f"{sys_text}\n\n{msgs[0]['content']}"}
        rows.append({"messages": msgs})
    return rows


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="QLoRA 파인튜닝 → Ollama")
    p.add_argument("--base", default="unsloth/gemma-3-12b-it", help="기반 모델 (Hugging Face 이름)")
    p.add_argument("--name", default="my-company", help="Ollama 에 등록할 이름")
    p.add_argument("--epochs", type=float, default=2)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--max-len", type=int, default=4096)
    p.add_argument("--rank", type=int, default=16, help="LoRA rank (클수록 많이 배우지만 과적합 위험)")
    p.add_argument("--quant", default="q4_k_m", help="GGUF 양자화 방식")
    p.add_argument("--force", action="store_true", help=f"예시가 {MIN_EXAMPLES}개 미만이어도 진행")
    args = p.parse_args(argv)

    if not DATASET.exists():
        sys.exit("학습 데이터가 없어요. 먼저: python -m engine export-training")
    rows = load_examples(DATASET)
    print(f"📚 학습 예시 {len(rows)}개")
    if len(rows) < MIN_EXAMPLES and not args.force:
        sys.exit(f"예시가 너무 적어요 ({len(rows)}개). 최소 {MIN_EXAMPLES}개, 권장 200개 이상. "
                 "더 모은 뒤 실행하거나 --force 로 강행하세요.")

    try:
        from unsloth import FastLanguageModel  # unsloth 를 transformers 보다 먼저 import
        from datasets import Dataset
        from trl import SFTConfig, SFTTrainer
    except ImportError as e:
        sys.exit(f"학습 패키지가 없어요: {e}\nWSL2 Ubuntu 안에서 `pip install unsloth` 후 다시 실행하세요.")

    print(f"⬇️  기반 모델 불러오는 중: {args.base} (처음엔 다운로드로 오래 걸려요)")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.base, max_seq_length=args.max_len, load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=args.rank,
        lora_alpha=args.rank,
        lora_dropout=0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth",
    )

    def to_text(ex):
        return {"text": tokenizer.apply_chat_template(ex["messages"], tokenize=False)}

    dataset = Dataset.from_list(rows).map(to_text, remove_columns=["messages"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset,
        args=SFTConfig(
            output_dir=str(OUT_DIR / "checkpoints"),
            dataset_text_field="text",
            max_length=args.max_len,
            per_device_train_batch_size=2,
            gradient_accumulation_steps=4,
            num_train_epochs=args.epochs,
            learning_rate=args.lr,
            warmup_ratio=0.05,
            lr_scheduler_type="cosine",
            logging_steps=5,
            save_strategy="epoch",
            bf16=True,
            report_to="none",
        ),
    )
    print("🏋️  학습 시작")
    trainer.train()

    lora_dir = OUT_DIR / "lora"
    model.save_pretrained(str(lora_dir))
    tokenizer.save_pretrained(str(lora_dir))
    print(f"💾 LoRA 저장: {lora_dir}")

    gguf_dir = OUT_DIR / "gguf"
    print(f"📦 GGUF 변환 중 ({args.quant})")
    model.save_pretrained_gguf(str(gguf_dir), tokenizer, quantization_method=args.quant)
    ggufs = sorted(gguf_dir.rglob("*.gguf"), key=lambda f: f.stat().st_size, reverse=True)
    if not ggufs:
        sys.exit(f"GGUF 파일을 못 찾았어요. {gguf_dir} 를 확인하세요.")
    modelfile = OUT_DIR / "Modelfile"
    modelfile.write_text(f"FROM {ggufs[0].resolve()}\n", encoding="utf-8")
    print("\n✅ 완료! Ollama 에 등록:")
    print(f"   ollama create {args.name} -f {modelfile}")
    print(f"   python -m engine set-model ceo ollama:{args.name}")


if __name__ == "__main__":
    main()
