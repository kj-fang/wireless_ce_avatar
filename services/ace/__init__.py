"""
ACE (Agentic Context Engineering) for the Intel Wi-Fi debug agent.

Adapted from Zhang et al. "Agentic Context Engineering: Evolving Contexts for
Self-Improving Language Models" (ICLR 2026).

Pipeline:
    feedback record + turn snapshot
        -> Reflector  (extracts root-cause + key insights)
        -> Curator    (proposes ADD / UPDATE / REMOVE operations)
        -> Playbook   (deterministic delta merge, counter updates, dedup)
        -> reused by the agent's next case via prompts.GENERATOR_PROMPT
"""

from .playbook import Bullet, Playbook
from .roles import Reflector, Curator
from .pipeline import AceRunner
