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

This package never mutates the playbook or any existing service code.
"""
