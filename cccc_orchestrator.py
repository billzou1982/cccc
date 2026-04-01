import re
#!/usr/bin/env python3
"""
cccc_orchestrator.py — Multi-agent loop for cccc groups.

Modes:
  review   Watch ledger, auto-route architect→reviewer→architect until LGTM
  debate   Bill triggers @debate, both agents discuss N rounds
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

GROUP_ID = "g_8c2658ba4790"
CCCC_DIR = Path.home() / ".cccc" / "groups" / GROUP_ID
LEDGER = CCCC_DIR / "ledger.jsonl"

ARCHITECT = "architect"
REVIEWER = "reviewer"

LGTM_SIGNALS = ["lgtm", "approved", "looks good", "no issues", "✅", "approve"]
STOP_SIGNALS = ["[stop]", "[done]", "[end]"]

MAX_ROUNDS = 6  # safety cap


def send(text: str, to: str, by: str = "user"):
    """Inject a message into cccc via CLI."""
    cmd = ["cccc", "send", text, "--to", to, "--by", by, "--group", GROUP_ID]
    print(f"  → [{by}→{to}] {text[:80]}...")
    subprocess.run(cmd, check=True)


def tail_ledger(cursor: int):
    """Read new lines from ledger since cursor. Returns (new_events, new_cursor)."""
    events = []
    with open(LEDGER, "r", encoding="utf-8", errors="replace") as f:
        f.seek(cursor)
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        new_cursor = f.tell()
    return events, new_cursor


def get_cursor():
    with open(LEDGER, "rb") as f:
        f.seek(0, 2)
        return f.tell()


def wait_for_reply(from_actor: str, cursor: int, timeout: int = 120):
    """Wait for a chat.message from from_actor. Returns (text, new_cursor) or (None, cursor)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        events, cursor = tail_ledger(cursor)
        for ev in events:
            if ev.get("kind") == "chat.message" and ev.get("by") == from_actor:
                text = ev.get("data", {}).get("text", "") or ""
                return text, cursor
        time.sleep(2)
    return None, cursor


def has_code(text: str) -> bool:
    return "```" in text or "def " in text or "class " in text or "function " in text


def is_lgtm(text: str) -> bool:
    tl = text.lower()
    return any(s in tl for s in LGTM_SIGNALS)


def is_stop(text: str) -> bool:
    tl = text.lower()
    return any(s in tl for s in STOP_SIGNALS)


# ─── MODE: REVIEW ─────────────────────────────────────────────────────────────

def mode_review(task: str, max_rounds: int):
    """
    1. Send task to architect
    2. Wait for architect output
    3. If output has code → send to reviewer
    4. If reviewer says LGTM → done; else send feedback to architect
    5. Repeat up to max_rounds
    """
    print(f"\n[review] Starting code review loop (max {max_rounds} rounds)")
    print(f"[review] Task: {task}\n")

    cursor = get_cursor()
    send(task, to=ARCHITECT)

    for round_n in range(1, max_rounds + 1):
        print(f"\n--- Round {round_n} ---")

        # Wait for architect
        print(f"[wait] architect thinking...")
        arch_text, cursor = wait_for_reply(ARCHITECT, cursor, timeout=180)
        if arch_text is None:
            print("[timeout] architect did not reply in time")
            break

        print(f"[architect] {arch_text[:200]}")

        if is_stop(arch_text):
            print("[stop] architect signaled done")
            break

        # Send to reviewer
        review_prompt = (
            f"请审核以下实现，指出问题并给出修改建议。"
            f"如果没有问题，请回复 LGTM。\n\n{arch_text}"
        )
        send(review_prompt, to=REVIEWER)

        # Wait for reviewer
        print(f"[wait] reviewer thinking...")
        rev_text, cursor = wait_for_reply(REVIEWER, cursor, timeout=180)
        if rev_text is None:
            print("[timeout] reviewer did not reply in time")
            break

        print(f"[reviewer] {rev_text[:200]}")

        if is_lgtm(rev_text):
            print("\n✅ LGTM — code review complete!")
            break

        if round_n == max_rounds:
            print(f"\n⚠️  Max rounds ({max_rounds}) reached")
            break

        # Send reviewer feedback back to architect
        fix_prompt = (
            f"Reviewer 的审核意见如下，请修复后重新实现：\n\n{rev_text}"
        )
        send(fix_prompt, to=ARCHITECT)

    print("\n[review] Loop ended")


# ─── MODE: DEBATE ──────────────────────────────────────────────────────────────

def mode_debate(question: str, rounds: int):
    """
    1. Send question to both agents simultaneously
    2. Collect both responses
    3. Send each agent's response to the other for rebuttal
    4. Repeat rounds times
    """
    print(f"\n[debate] Starting debate (rounds={rounds})")
    print(f"[debate] Question: {question}\n")

    cursor = get_cursor()

    # Round 0: both get the question
    send(question, to=ARCHITECT)
    send(question, to=REVIEWER)

    for round_n in range(1, rounds + 1):
        print(f"\n--- Debate Round {round_n} ---")

        print("[wait] architect...")
        arch_text, cursor = wait_for_reply(ARCHITECT, cursor, timeout=180)
        if arch_text is None:
            print("[timeout] architect silent")
            break

        print(f"[architect] {arch_text[:300]}")

        print("[wait] reviewer...")
        rev_text, cursor = wait_for_reply(REVIEWER, cursor, timeout=180)
        if rev_text is None:
            print("[timeout] reviewer silent")
            break

        print(f"[reviewer] {rev_text[:300]}")

        if round_n == rounds:
            break

        # Cross-inject: each sees the other's argument
        send(
            f"Codex 的观点：\n{rev_text}\n\n请回应并进一步阐述你的立场。",
            to=ARCHITECT
        )
        send(
            f"Claude 的观点：\n{arch_text}\n\n请回应并进一步阐述你的立场。",
            to=REVIEWER
        )

    print("\n[debate] Discussion ended")


# ─── WATCH MODE (passive) ──────────────────────────────────────────────────────

def mode_watch(trigger_word: str, auto_review: bool):
    """
    Watch ledger passively. Auto-trigger when user message contains trigger_word.
    """
    print(f"[watch] Watching for trigger: '{trigger_word}' (Ctrl+C to stop)")
    cursor = get_cursor()

    while True:
        events, cursor = tail_ledger(cursor)
        for ev in events:
            if ev.get("kind") != "chat.message":
                continue
            by = ev.get("by", "")
            text = ev.get("data", {}).get("text", "") or ""
            if by == "user" and trigger_word.lower() in text.lower():
                question = text.replace(trigger_word, "").strip()
                _m = re.search(r"(\d+)\s*(?:轮|rounds?)", question, re.IGNORECASE)
                rounds = int(_m.group(1)) if _m else 3
                if _m: question = question[:_m.start()].strip()
                question = text.replace(trigger_word, "").strip()
                import shlex, re
                rounds = 3
                m_flag = re.search(r"--rounds\s+(\d+)", question)
                if m_flag:
                    rounds = int(m_flag.group(1))
                    question = (question[:m_flag.start()] + question[m_flag.end():]).strip()
                else:
                    m_cn = re.search(r"(\d+)\s*(?:轮|rounds?)", question, re.IGNORECASE)
                    if m_cn:
                        rounds = int(m_cn.group(1))
                        question = question[:m_cn.start()].strip()
                print("[watch] Triggered! Question: " + repr(question) + " rounds=" + str(rounds))
                if auto_review:
                    mode_review(question, rounds)
                else:
                    mode_debate(question, rounds=rounds)


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="cccc multi-agent orchestrator")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_review = sub.add_parser("review", help="Code review loop")
    p_review.add_argument("task", help="Task/prompt for architect")
    p_review.add_argument("--rounds", type=int, default=MAX_ROUNDS)

    p_debate = sub.add_parser("debate", help="Technical debate between agents")
    p_debate.add_argument("question", help="Question/topic to debate")
    p_debate.add_argument("--rounds", type=int, default=3)

    p_watch = sub.add_parser("watch", help="Watch and auto-trigger")
    p_watch.add_argument("--trigger", default="@debate", help="Trigger word in user messages")
    p_watch.add_argument("--review", action="store_true", help="Use review mode instead of debate")

    args = parser.parse_args()

    if args.mode == "review":
        mode_review(args.task, args.rounds)
    elif args.mode == "debate":
        mode_debate(args.question, args.rounds)
    elif args.mode == "watch":
        mode_watch(args.trigger, args.review)


if __name__ == "__main__":
    main()
