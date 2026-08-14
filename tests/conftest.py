import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def mini_pack_raw(**over):
    """A tiny, self-contained pack for unit tests. Scenario vocabulary
    is allowed here — tests are not engine."""
    pack = {
        "pack": {"name": "mini", "title": "Mini"},
        "session": {
            "minutes": 0.05,
            "workspace": False,
            "idle_threshold_s": 0.6,
        },
        "interviewer": {
            "persona": "You are a terse test interviewer.",
            "max_sentences": 2,
            "interjection_budget": 4,
            "fallback_lines": ["Okay.", "Go on."],
            "opening_line": "Begin.",
        },
        "hints": {"ladder": ["ask open", "ask pointed", "name it"]},
        "rules": [
            {
                "id": "reply",
                "on": ["user_message"],
                "when": "true",
                "action": "speak",
                "hint": "none",
                "priority": 100,
            },
            {
                "id": "idle-nudge",
                "on": ["idle"],
                "when": "task_open and idle_s >= 0.5",
                "action": "speak",
                "hint": "escalate",
                "priority": 50,
            },
        ],
        "tasks": {"order": "sequential"},
        "report": {
            "title": "Mini report",
            "sections": [
                {
                    "type": "timeline",
                    "milestones": [
                        {"label": "first clean run", "when": "ok"},
                    ],
                },
                {"type": "messages"},
            ],
        },
    }
    tasks = [
        {"id": "t1", "title": "Task one", "statement": "Say things.", "notes": "n1"},
        {"id": "t2", "title": "Task two", "statement": "Say more.", "notes": "n2"},
    ]
    raw = {"pack": pack, "tasks": tasks}
    for key, value in over.items():
        section, _, leaf = key.partition("__")
        if leaf:
            raw["pack"].setdefault(section, {})[leaf] = value
        else:
            raw["pack"][section] = value
    return raw
