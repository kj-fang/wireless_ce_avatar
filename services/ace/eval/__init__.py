"""
ACE evaluation harness — automated before/after validation of playbook updates.

Flow:  feedback snapshots (ground truth)  →  case registry (cases.py)
       →  headless agent replay against before/after playbooks (replay.py)
       →  deterministic scoring + LLM judge (scoring.py / judge.py)
       →  gate: keep or auto-rollback (harness.py)
       →  persisted eval reports (store.py)

The curated golden set (golden.py) pins which cases the refine loop runs on,
so recurring cost is bounded and runs are comparable over time.
"""

from .cases import EvalCase, list_cases, select_cases
from .golden import GoldenSet
from .store import EvalStore
from .harness import EvalHarness, EvalConfig
