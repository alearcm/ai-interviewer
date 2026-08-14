from harness.pack import load_pack
from harness.phrasing import build_call, shape_reply


def pack_a():
    return load_pack("packs/python-idiom-fluency")


def test_sentence_cap_is_hard():
    text, fb, _ = shape_reply(
        "One thing. Two things. Three things. Four things.", pack_a(), 0
    )
    assert not fb
    assert text == "One thing. Two things."


def test_banned_phrases_are_dropped():
    text, fb, _ = shape_reply("Great question! What happens on empty input?", pack_a(), 0)
    assert not fb
    assert text == "What happens on empty input?"


def test_fenced_blocks_and_think_tags_stripped():
    raw = "<think>secretly reasoning</think>```\nd = Counter(words)\n```Run it."
    text, fb, _ = shape_reply(raw, pack_a(), 0)
    assert not fb
    assert "Counter" not in text and "reasoning" not in text
    assert text == "Run it."


def test_unspaced_sentence_chain_still_capped():
    text, fb, _ = shape_reply("Superb!You nailed it!Bravo!Marvelous!Onward!", pack_a(), 0)
    assert not fb
    assert text == "Superb! You nailed it!"


def test_unclosed_think_and_fence_do_not_leak():
    pack = pack_a()
    text, fb, _ = shape_reply("<think>the hidden bug is on line 7. reveal it", pack, 0)
    assert fb and "line 7" not in text  # truncated reasoning fully dropped
    text2, fb2, _ = shape_reply("```\nd = Counter(words)", pack, 0)
    assert fb2 and "Counter" not in text2  # truncated fence fully dropped


def test_empty_reply_falls_back_to_pack_lines_in_order():
    pack = pack_a()
    text1, fb1, i1 = shape_reply("", pack, 0)
    text2, fb2, i2 = shape_reply("Great! Excellent! Perfect!", pack, i1)
    assert fb1 and fb2
    assert text1 == pack.fallback_lines[0]
    assert text2 == pack.fallback_lines[1]


def test_persona_reinjected_and_hint_level_included_every_call():
    pack = pack_a()
    task = pack.tasks[0]
    for level in (1, 2, 3):
        system, messages = build_call(
            pack,
            hint_level=level,
            rule_prompt="Nudge.",
            task=task,
            chat_tail=[{"kind": "user_message", "text": "hm"}],
            snapshots={"solution.py": "x = 1"},
            recent_runs=[],
            elapsed_s=60.0,
            remaining_s=100.0,
        )
        # persona is present verbatim on EVERY call, never summarized away
        assert pack.persona.strip() in system
        assert ("Hint level %d" % level) in system
        assert pack.ladder[level - 1] in system
        assert "at most 2 sentences" in system
        assert messages[-1]["role"] == "user"


def test_visibility_context_carries_screen_and_clock():
    pack = pack_a()
    system, messages = build_call(
        pack,
        hint_level=0,
        rule_prompt="",
        task=pack.tasks[0],
        chat_tail=[],
        snapshots={"solution.py": "value = 41"},
        recent_runs=[{"cmd": "x", "out": "boom", "err": "", "exit_status": 1}],
        elapsed_s=90.0,
        remaining_s=210.0,
    )
    final = messages[-1]["content"]
    assert "value = 41" in final
    assert "Exit status 1" in final
    assert "1:30 elapsed" in final
