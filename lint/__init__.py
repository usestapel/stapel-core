"""Static checks shipped with stapel-core (CI gates for module repos).

Run: ``python -m stapel_core.lint.emit_check [paths]`` — outbox atomicity;
     ``python -m stapel_core.lint.realtime_check [paths]`` — the realtime
     border (no fifth hand-rolled WebSocket stack).
"""
