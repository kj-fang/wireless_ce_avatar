Beacon Server - trial build (IntelAvatar_Server)
=================================================

What it is
  IntelAvatar in the Beacon SERVER role: the one machine that runs the
  scheduled analysis rounds for the team. Its Beacon page has everything
  the client has, plus "Start automatic analysis" with the full settings
  (source, frequency, Start/Stop) and the live panel that shows each round
  while it runs. It also serves the "Analyze this case" requests that the
  client PCs send.
  The role is built into this package: IntelAvatar_Server_v*.exe always
  starts as the server (handsfree_role.txt in the _internal folder).

Before you start
  - Windows 11 on the Intel network (or VPN): the app reads and writes the
    shared Beacon queue and config on
    \\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\handsfree.
  - Google Chrome installed. The rounds read IPS with the session signed
    in on this machine ("Sign in to IPS" on the page, Intel SSO in Chrome);
    the matching ChromeDriver is downloaded on first start.
  - Unzip the whole folder somewhere local (e.g. C:\Tools\IntelAvatar_Server).
    Do not run the exe from inside the zip.
  - One scheduler per server. If the Task Scheduler job that runs the
    Handsfree CLI (tools/handsfree_server_run.py) is still installed on
    this machine, disable it while the in-app schedule is ON. The page
    warns when the CLI also ran in the last 24 h, Start refuses while the
    CLI's status file is fresh, and the CLI itself steps aside (exit 3)
    when the in-app schedule is ON for this host.

Start
  Double-click "Beacon Server.bat"
  (same as running IntelAvatar_Server_v*.exe; double-clicking the exe
  does the same, the role is built in).
  The first start takes about a minute, then the Beacon page opens in its
  own Chrome window with a SERVER badge. A console window stays open in
  the background; close it to stop the app and the schedule with it.

On the page
  - "Start automatic analysis": pick the source
      * Open cases of case owners - up to 10 owner chips,
      * IPS list views - up to 3, their cases combined,
      * Owners file on the server - every owner in the CLI's owners file,
    the frequency, and press Start. The status line names the host that
    runs the schedule, the next round and the last result. Stop ends it.
    An enabled schedule resumes by itself when the app is restarted on
    the same host.
  - "Run now" runs one round immediately, schedule or not.
  - The live panel shows the running round (case, stage, duration) and
    keeps refreshing on its own while the schedule is ON.
  - Client requests ("Analyze this case" from a client PC) are picked up
    within seconds and run between rounds; the draft lands in that
    owner's queue.
  - Approvals from this page post under the account signed in HERE.
    Reviewing and approving is meant for the owners' client PCs.
  - Settings saved on this page (owner, dry run, trial mode, posting
    backend, REST field map) are the team's shared config on the share
    and are seen by every client.

Where things are
  Logs and local data: %USERPROFILE%\Downloads\IntelAvatar_files\logs
  Shared queue/config: \\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\handsfree

Feedback
  Charles P Chu (charles.p.chu@intel.com)
