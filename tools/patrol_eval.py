"""AI 伪人巡检规则 / AI 甄别效果评估脚本。

用法：
    python tools/patrol_eval.py --labeled data/patrol_eval/labeled.jsonl
    python tools/patrol_eval.py --labeled tests/fixtures/patrol_negative_samples.json --assume-label human
    python tools/patrol_eval.py --labeled data/patrol_eval/labeled.jsonl --with-ai

输入：JSON 数组或 JSONL，每条含 name/sign/text/level/is_vip，标注文件另需 label ∈ bot/human/unsure。
输出：各档位精确率 / 召回率、混淆矩阵、「驱赶类」误判率。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules.lottery.patrol.contracts import build_settings
from modules.lottery.patrol.fusion import rescore
from modules.lottery.patrol.rules import apply_cross_features, compile_rules, score_comment

SETTINGS = build_settings({})


def load_samples(path: Path):
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def evaluate(samples, with_ai=False):
    compiled = compile_rules()
    items = []
    for idx, sample in enumerate(samples):
        comment = {
            "mid": idx + 1,
            "rpid": idx + 1,
            "uname": sample.get("name", ""),
            "sign": sample.get("sign", ""),
            "level": sample.get("level", 4),
            "is_vip": sample.get("is_vip", False),
            "official_type": sample.get("official_type", -1),
            "content": sample.get("text", ""),
            "is_reply": False,
            "at_users": [],
        }
        comment.update(score_comment(comment, compiled, SETTINGS))
        comment["ai_score"] = None
        comment["ai_reason"] = None
        comment["_label"] = sample.get("label")
        comment["_note"] = sample.get("note", "")
        items.append(comment)

    apply_cross_features(items, SETTINGS)

    if with_ai:
        from modules.lottery.patrol.judge import judge

        async def _run():
            return await judge(items, {}, focus="", s=SETTINGS,
                               progress=lambda *a: None, is_cancelled=lambda: False)

        result = asyncio.run(_run())
        for rpid, value in (result.get("results") or {}).items():
            for it in items:
                if it["rpid"] == rpid:
                    it["ai_score"] = value.get("ai_score")
                    it["ai_reason"] = value.get("reason")

    rows = []
    for it in items:
        scored = rescore(it, SETTINGS)
        it.update(scored)
        rows.append(it)

    tiers = ["bot", "suspect", "human", "unknown", "whitelist"]
    labeled = [r for r in rows if r["_label"] in ("bot", "human")]
    print(f"样本 {len(rows)} 条，其中有标注 {len(labeled)} 条（with_ai={with_ai}）")
    print(f"{'档位':<10}{'判为该档':>8}{'真 bot':>8}{'真 human':>8}{'精确率':>8}")
    for tier in tiers:
        predicted = [r for r in labeled if r["tier"] == tier]
        tp = sum(1 for r in predicted if r["_label"] == "bot")
        fp = sum(1 for r in predicted if r["_label"] == "human")
        precision = tp / len(predicted) if predicted else 0
        print(f"{tier:<10}{len(predicted):>8}{tp:>8}{fp:>8}{precision:>8.1%}")

    actual_bot = [r for r in labeled if r["_label"] == "bot"]
    if actual_bot:
        recall = sum(1 for r in actual_bot if r["tier"] == "bot") / len(actual_bot)
        print(f"\nbot 档召回率: {recall:.1%}（{sum(1 for r in actual_bot if r['tier']=='bot')}/{len(actual_bot)}）")

    drive = [r for r in rows if r["_note"] == "驱赶类"]
    if drive:
        bad = [r for r in drive if r["tier"] in ("bot", "suspect")]
        print(f"驱赶类误判率: {len(bad)}/{len(drive)} = {len(bad)/len(drive):.1%}")

    suspects = [r for r in labeled if r["_label"] == "human" and r["tier"] in ("bot", "suspect")]
    if suspects:
        print("\n被误判为机器人 / 疑似机器人的真人样本（前 10 条）:")
        for r in suspects[:10]:
            print(f"  - [{r['tier']} {r['final_score']}] {r['content'][:40]}")
    return 0 if not suspects else 1


def main():
    parser = argparse.ArgumentParser(description="AI 伪人巡检效果评估")
    parser.add_argument("--labeled", required=True, help="JSON / JSONL 样本文件")
    parser.add_argument("--assume-label", choices=["bot", "human"], help="给无 label 的样本统一打标")
    parser.add_argument("--with-ai", action="store_true", help="调用已配置的 LLM 做甄别")
    args = parser.parse_args()
    samples = load_samples(Path(args.labeled))
    if args.assume_label:
        for s in samples:
            s.setdefault("label", args.assume_label)
    raise SystemExit(evaluate(samples, with_ai=args.with_ai))


if __name__ == "__main__":
    main()
