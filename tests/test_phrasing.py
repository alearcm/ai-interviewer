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


def test_char_budget_drops_whole_sentences_never_cuts_mid_thought():
    """The live bug: a real teaching answer got chopped to '…clean, rea'
    by the char cap. Over budget must shed trailing sentences; only a
    single monster sentence may be word-cut, and then visibly."""
    pack = pack_a()
    pack.max_sentences = 10
    pack.max_chars = 60
    text, fb, _ = shape_reply(
        "Short first sentence here. This second sentence is long enough that it cannot fit. Third.",
        pack, 0,
    )
    assert not fb
    assert text == "Short first sentence here."
    long_one = "word " * 40
    text2, fb2, _ = shape_reply(long_one.strip() + ".", pack, 0)
    assert not fb2
    assert len(text2) <= pack.max_chars
    assert text2.endswith("…")
    assert not text2[:-2].endswith(" wor")  # never a mid-word cut


def test_previous_task_and_position_shown_when_pack_opts_in():
    from harness.pack import load_pack

    rehab = load_pack("packs/python-rehab")
    assert rehab.include_previous_task
    system, messages = build_call(
        rehab,
        hint_level=0,
        rule_prompt="",
        task=rehab.tasks[1],
        chat_tail=[],
        snapshots={},
        recent_runs=[],
        elapsed_s=60.0,
        remaining_s=120.0,
        prev_task=rehab.tasks[0],
        task_pos=(2, 12),
    )
    assert "The PREVIOUS task, already finished" in system
    assert rehab.tasks[0]["statement"] in system
    assert "Task 2 of 12." in messages[-1]["content"]


def test_previous_task_hidden_without_opt_in():
    pack = pack_a()  # include_previous_task defaults to off
    assert not pack.include_previous_task
    system, messages = build_call(
        pack,
        hint_level=0,
        rule_prompt="",
        task=pack.tasks[1],
        chat_tail=[],
        snapshots={},
        recent_runs=[],
        elapsed_s=60.0,
        remaining_s=120.0,
        prev_task=pack.tasks[0],
        task_pos=None,
    )
    assert "PREVIOUS task" not in system
    assert " of " not in messages[-1]["content"].split("Clock:")[1].split("\n")[0]


def test_dotted_identifiers_and_abbreviations_survive_splitting():
    """The verified live gutting cases: dotted module paths split at
    '.Capital', and 'e.g.' / list markers counted as sentences."""
    from harness.phrasing import split_sentences

    assert split_sentences("collections.Counter(words) does the whole tally here.") == [
        "collections.Counter(words) does the whole tally here."
    ]
    assert split_sentences("Reach for pathlib.Path instead. It reads better.") == [
        "Reach for pathlib.Path instead.",
        "It reads better.",
    ]
    assert split_sentences("Use built-ins, e.g. sum and max. They beat manual loops.") == [
        "Use built-ins, e.g. sum and max.",
        "They beat manual loops.",
    ]
    # glued !? still split (the case the no-space branch exists for)
    assert split_sentences("Superb!You nailed it!") == ["Superb!", "You nailed it!"]


def test_banned_phrases_match_on_word_boundaries():
    class P:
        banned_phrases = ["love it"]
        strip_fenced_blocks = True
        max_sentences = 5
        max_chars = 500
        fallback_lines = ["Okay."]

    text, fb, _ = shape_reply("You will love itertools for this.", P(), 0)
    assert not fb and text == "You will love itertools for this."
    text2, _, _ = shape_reply("Love it! Also, slices copy.", P(), 0)
    assert text2 == "Also, slices copy."


def test_inline_backtick_markers_do_not_gut_the_sentence():
    """A stray or inline ``` in prose drops the marker, not the words;
    only a fence that starts a line is stripped as a block."""
    pack = pack_a()
    text, fb, _ = shape_reply(
        "Replace the loop with ```zip``` here, then unpack each pair.", pack, 0
    )
    assert not fb and text == "Replace the loop with zip here, then unpack each pair."
    text2, fb2, _ = shape_reply("Here.\n```\nd = Counter(w)\n```\nRun it.", pack, 0)
    assert not fb2 and "Counter" not in text2 and "Run it." in text2


def test_api_truncated_tail_never_displayed_raw():
    """When max_tokens runs out server-side, the reply arrives cut
    mid-sentence. The dangling fragment is dropped when complete
    sentences precede it, and visibly marked when it is all we have."""
    pack = pack_a()
    pack.max_sentences = 10
    text, fb, _ = shape_reply(
        "Zip pairs items in lockstep. It stops at the shorter inp", pack, 0
    )
    assert not fb and text == "Zip pairs items in lockstep."
    text2, fb2, _ = shape_reply(
        "Zip pairs items in lockstep and stops at the shorter inp", pack, 0
    )
    assert not fb2 and text2 == "Zip pairs items in lockstep and stops at the shorter inp …"
