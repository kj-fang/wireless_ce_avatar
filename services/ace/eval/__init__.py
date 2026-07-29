"""
ACE playbook quality evaluation service.

Independent, manually-triggered. Replays a fixed set of "golden" cases
through the live `WifiLogAgentSystem` (with whatever playbook is currently
on disk) and uses an LLM-as-judge to score how close each new answer is
— in meaning, not wording — to the recorded correct answer.

Entry point:
    python -m services.ace.eval                  # run every case in cases/
    python -m services.ace.eval --case <id>      # run a single case
    python -m services.ace.eval --cases-dir <p>  # use a different folder
    python -m services.ace.eval --review         # chain eval -> review
    python -m services.ace.eval --auto-fix       # chain eval -> review -> corrupted_bullet -y

Output layout:
    runs/<stamp>/eval_<stamp>.json
    runs/<stamp>/answers_<stamp>.json
    runs/<stamp>/review_<stamp>.json

When a bare filename is passed to review/corrupted_bullet, the tools look
under runs/ recursively and resolve the newest matching stamp directory.

This package never mutates the playbook or any existing service code.
"""
