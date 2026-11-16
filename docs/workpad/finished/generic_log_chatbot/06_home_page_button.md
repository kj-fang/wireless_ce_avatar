# Sub-task 06: Home page button split

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: 04 (route must exist before linking to it; 05 isn't strictly required for the link to
render, but end-to-end validation needs it too).

## Goal

Today the home page's "Already got logs? Try these" row has one link covering both Linux and
"generic" (mislabeled as generic, pointing at `/linux_chatbot/`). Split it into two links.

## Where

- `templates/index.html:1452-1461` (the `#chatbot-shortcut-links` block):
  - Existing line 1459:
    ```html
    <a href="/linux_chatbot/" class="chatbot-tech-btn"><img src="{{ url_for('static', filename='lucide-file-text.svg') }}" class="shortcut-icon" alt="" aria-hidden="true">Generic Log Analysis</a>
    ```
    change the label text to `Linux Log Analysis` (href unchanged).
  - Add a new line immediately after it:
    ```html
    <a href="/generic_chatbot/" class="chatbot-tech-btn"><img src="{{ url_for('static', filename='lucide-file-text.svg') }}" class="shortcut-icon" alt="" aria-hidden="true">Generic Log Analysis</a>
    ```
    (same icon reused — no dedicated icon asset exists for either profile; see Rules in
    [README.md](README.md)).

## Steps

1. Edit the Linux link's label text.
2. Add the new Generic link directly below it, in the same `#chatbot-shortcut-links` div.

## Validate

No frontend test suite covers `index.html` — validate manually:

- Load the home page and confirm five shortcut buttons total: Wi-Fi Analysis, Network Experience,
  Bluetooth Analysis, Linux Log Analysis, Generic Log Analysis.
- Click "Linux Log Analysis" → lands on `/linux_chatbot/`, page title reads "Linux Log Chatbot"
  (per sub-task 03), everything else behaves as it did before this workpad.
- Click "Generic Log Analysis" → lands on `/generic_chatbot/`, page title reads "Generic Log
  Analysis Agent", and the page loads without a 404/500 (confirms sub-tasks 01-05 are wired
  correctly end-to-end).

## Done when

Both links render, route correctly, and the PROGRESS.md row for 06 is updated. At this point
every sub-task in the workpad is done — update `README.md`'s Status to `shipped` and move the
folder to `docs/workpad/finished/generic_log_chatbot/` per the skill's "Ongoing use" step 7.
