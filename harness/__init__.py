"""Interview-simulation engine.

The engine owns: session lifecycle and clock, workspace observation,
a fixed event taxonomy, the append-only JSONL transcript, the chat
pane, the model adapter, the deterministic interjection gate, and
report generation from a pack's template.

Everything scenario-specific — persona, tasks, wake rules, thresholds,
hint ladder, visibility, report rubric — lives in a pack as data and
prompts. The engine never contains scenario vocabulary; a test greps
this package to enforce that.
"""

__version__ = "0.1.0"
