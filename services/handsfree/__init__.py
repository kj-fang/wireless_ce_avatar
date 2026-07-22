"""
Handsfree Replyer — autonomous IPS case analysis with reviewed comment posting.

Flow:  find_new_cases (SOQL, owner + today)          ips_client.py
       → headless analysis (download → decode → agent) runner.py
       → comment draft                                 composer.py
       → review queue (human Approve in the UI)        queue.py / orchestrator.py
       → post to IPS (REST first, Selenium fallback)   ips_client.py / ui_commenter.py

v1 safety: nothing is ever posted without an explicit Approve click; every
comment is Private-to-Intel and marked AI-generated; a ledger prevents
double-processing a case.
"""
