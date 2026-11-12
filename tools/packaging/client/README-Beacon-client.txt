Beacon Client - trial build (IntelAvatar_Client)
=================================================

What it is
  IntelAvatar with the Beacon (Handsfree) review page, for a case owner's
  own PC. It lists the AI-drafted IPS replies for YOUR cases, lets you edit
  them, and posts the one you approve to IPS under your own account.
  The analysis rounds themselves run on the Beacon server; this client
  never schedules them.

Before you start
  - Windows 11 on the Intel network (or VPN): the app reads the shared
    Beacon queue and keys from \\infs089b.iil.intel.com\HOME\WirelessCE.
  - Google Chrome installed. The app signs you in to IPS (Salesforce) with
    Intel SSO through Chrome; the matching ChromeDriver is downloaded on
    first start.
  - Unzip the whole folder somewhere local (e.g. C:\Tools\IntelAvatar_Client).
    Do not run the exe from inside the zip.

Start
  Double-click "Beacon Client.bat"
  (same as running IntelAvatar_Client_v*.exe --handsfree).
  The first start takes about a minute: it fetches keys, Chrome driver and
  skills, then opens the Beacon page in its own Chrome window. A console
  window stays open in the background; close it to stop the app.

On the page
  - Top right of "Review queue": a green badge "IPS: <your name>" means the
    sign-in worked. If it says "not signed in", click "Sign in to IPS" and
    complete Intel SSO in the Chrome window that opens.
  - The queue shows only the cases IPS lists under your name. Tick
    "everyone's cases" to see all owners.
  - "Analyze this case": type a case number and the request goes to the
    Beacon server, which runs the analysis. The line under the button
    follows the request (pending / running / done) and the draft appears
    in your queue when it is finished.
  - Open a draft and edit the text if needed. Under the text, "Post as"
    decides who sees the comment: "Internal to Intel" is preselected on
    every draft; pick "External to customer" only when the reply is meant
    for the customer. Then Approve. The confirm names the account the
    comment will be posted as; afterwards the panel shows
    "posted to IPS as <name>" read back from IPS, with the visibility.
  - "Open in Avatar analysis" opens the Avatar chatbot with the case's
    decoded log preloaded and the issue filled in. It is greyed out with
    "no log to analysis" when the server analysed the case without a log.
  - "Start automatic analysis" shows the server's schedule read-only
    (host, next round, last result). Start/Stop exist only on the server.

Where things are
  Logs and local data: %USERPROFILE%\Downloads\IntelAvatar_files\logs
  Shared queue:        \\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\handsfree

Feedback
  Charles P Chu (charles.p.chu@intel.com)
