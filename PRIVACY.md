# cpcpub privacy policy

Last updated 30 October 2026.

This policy covers the cpcpub results hub at [cpcpub.app](https://cpcpub.app/)
(and at its earlier address, cpcpub.qd.je, which apps before version 0.5.0
upload to), and the cpcpub apps that upload to it: the Android app, the desktop app and the command-line benchmark. They
are run by Łukasz Sobala, who decides what is collected and why. Questions and
requests go to [lsobala1@gmail.com](mailto:lsobala1@gmail.com).

## In short

- The apps send nothing until you choose to upload a result.
- A result describes your processor and how fast it ran, not you. It is
  published on the leaderboard for anyone to see.
- There are no ads, no analytics, no tracking and no third-party code, in the
  apps or on the hub.
- An account is optional and needs no email address.
- Your IP address reaches the server, as it does with any website, and on
  the way it passes through Cloudflare, which delivers the site. It is used to
  stop abuse and deleted from the hub's logs within 14 days.

## What an uploaded result contains

When you upload, the benchmark sends the document it printed. It holds:

- the measurements, per core and for the whole machine, with the clock speed
  each ran at;
- the processor's model name and how many cores of each kind it has, and the
  name of the board or phone model where the system reports one (for example
  “Radxa ROCK 5B” or “Samsung SM-S931B”);
- the operating system's name and kernel version, and the processor
  architecture;
- the memory clock range, where the system reports one;
- how the benchmark was built and run: compiler, build options, settings, and
  a checksum of the program itself.

It does not contain your name, your computer's or phone's name, your user name,
your location, a device or advertising identifier, a serial number, or anything
from your files, contacts or other apps.

You can add a **label** and **notes**. Both are published with the result. The
Android app fills in the phone's make and model as the label, which you can
change. Please do not put anything personal in them.

## Accounts

You can upload without an account. If you make one, the hub keeps:

- the name you choose, which is shown beside your uploads;
- your password, stored only as a salted scrypt hash, never in a form that can
  be read back;
- an upload token, which lets the apps upload as you;
- the date the account was made.

No email address is asked for.

## IP addresses and logs

Requests to cpcpub.app pass through Cloudflare first (see *Who else sees
it*), which hands the server your IP address along with the request. Every
request reaches the server from an IP address. The hub uses it to limit
how often one address can upload, sign in or make accounts. For this it keeps
the address in memory only, and forgets it within two hours of its last
request. The hub's own request log and the web server's access log record the
address, the time and the page requested. They are used only to look into
faults and abuse. The hub's log is deleted within 7 days and the web server's
within 14. IP addresses are never stored with a result or an account.

## Cookies and browser storage

The hub sets two cookies, and only when you sign in. One keeps you signed in
for up to 30 days. The other protects your account's forms from being
submitted by another website. There are no tracking or advertising cookies.

The website also keeps a few things in your browser's own storage, and they
never leave it: the delete tokens of results you uploaded without an account,
and display settings such as how many rows to show.

## The Android app

The benchmark runs on your phone, and nothing is sent until you tap Upload. The
result then goes to the hub address in the app's settings, which is this hub
unless you change it. A different hub is run by someone else, under their own
policy.

On the phone, the app keeps the form you filled in (hub address, upload token,
label and notes), your settings, and a receipt for each upload, including the
token needed to withdraw it. This stays on the phone. It is left out of cloud
backups and of transfers to a new phone, and uninstalling the app removes it.

The app asks for these permissions: internet access, to upload; notifications,
to show a run's progress; and to keep running while the screen is off, so a
run is not cut short. It contains no analytics, crash reporting or advertising
code.

The desktop app and the command-line benchmark work the same way. They send
nothing unless you ask them to upload.

## Why the data is used, and the legal basis

- Results, labels, notes and account names are published because you ask for
  them to be. The basis is your consent, which you can withdraw by removing
  them.
- Logs and IP addresses are used to keep the service working and protect it
  from abuse, by the hub and by Cloudflare in carrying its traffic. The basis
  is legitimate interest.

Nothing is sold, used for advertising, or used to build a profile of anyone.

## Who else sees it

Results, with their labels, notes and account names, are public, both on the
leaderboard and through the hub's API. Apart from that, nothing is shared.
Two companies handle it on the hub's behalf:

- **Mikrus** (mikr.us) rents out the virtual server the hub runs on, in a data
  centre in the European Union. Mikrus stores the data but does not use it.
- **Cloudflare, Inc.** carries traffic to cpcpub.app: every request, with
  your IP address, passes through its network on the way to the server, and
  what you upload passes through with it. It does this to deliver the site and
  protect it from attacks, and may do so outside the European Union, under
  [its privacy policy](https://www.cloudflare.com/privacypolicy/) and its
  data processing terms. Cloudflare sets no cookies on this site. Uploads to
  the earlier address, cpcpub.qd.je, go straight to the server.

## How long it is kept

- Results stay until you remove them.
- An account stays until you close it, and closing it deletes its results too.
- Sign-ins end after 30 days, or when you sign out.
- Logs are deleted within 7 days (the hub's) or 14 days (the web server's).

## Your choices and rights

- Remove a result: on the website, under My uploads (with your account, or in
  the browser you uploaded from), or in the Android app after an upload.
- Hide a result instead: also under My uploads. A hidden result is taken off
  the leaderboard and out of comparisons, but is not deleted. Anyone with its
  link can still open it, and you can show it again.
- Move results you uploaded without an account onto one: under My uploads,
  with their delete tokens. Your account name is then shown beside them.
- Close your account: under Account → Close this account. This deletes the
  account and all its results.
- For anything else, write to [lsobala1@gmail.com](mailto:lsobala1@gmail.com).
  You can ask to see, correct or delete data about you, or object to its use.
  If you lost a delete token, say which result is yours and it can be removed.
  You can also complain to the data protection authority where you live.

## Children

cpcpub is not aimed at children. It collects nothing that identifies anyone,
child or adult.

## Changes

If this policy changes, this page will say so, with a new date at the top, and
the [history of this file](https://github.com/lukaszsobala/cpcpub/commits/main/PRIVACY.md)
shows every change. The apps and the hub are open source, in this repository,
so you can check what they do.

<!-- What this describes is what the code in this repository does: change it
     when that changes, and the date with it. The Android app and the hub's
     footer link here, and so does the Google Play listing. -->
