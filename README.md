# douyin4u

Douyin private messages behind a client shaped like WeChatFerry's
`wcferry.Wcf` — so a host that already speaks WeChatFerry reaches Douyin with
the same code.

> **Not for use in mainland China.** This project is not intended for, and
> must not be used within, the People's Republic of China (excluding Hong Kong,
> Macau and Taiwan). It automates a web page in a way Douyin's terms of service
> do not permit, and using it may also violate local laws and regulations,
> including those on data, cybersecurity and automated access. It is published
> for research and education; you are solely responsible for how you use it,
> and the authors accept no liability.

There is no vendor SDK for this: Douyin's private-message API needs an
enterprise / blue-V account and a review. douyin4u instead drives the same
page a person would use — the main-site chat, `www.douyin.com/chat?isPopup=1`
— with Playwright attached over the Chrome DevTools Protocol to a real Edge
that the operator logged into by hand.

## The interface

| `wcferry.Wcf` | `douyin4u.Douyin` | Notes |
|---|---|---|
| `Wcf(port=...)` | `Douyin(cdp_url=..., marks_path=..., ...)` | Every setting is an argument; the package reads no config. |
| — | `start()` | Attaches (starting the debug browser if nothing listens). Never raises; `False` + `last_error` says why. |
| `is_login()` | `is_login()` / `is_ready()` / `get_status()` | `get_status()` names what the tab shows: `ready`, `logged_out`, `challenge`, `wrong_page`, ... |
| `enable_receiving_msg()` | `enable_receiving_msg()` | Nothing is scanned — and no mark moves — until a host is reading. |
| `get_msg()` → `WxMsg` | `get_msg()` → `DyMsg` | Raises `queue.Empty` after a second, like wcferry. Each message is handed out once. |
| `WxMsg.id/type/sender/content/ts`, `from_self()`, `from_group()`, `is_text()` | same on `DyMsg` | Plus `peer` (the fan, in both directions), `seq`, `self_only`. |
| `send_text(msg, receiver)` → `int` | `send_text(msg, receiver, timeout)` → `SendResult` | `status` 0 is delivered in full, as with wcferry; `sent`/`total` count lines, since a reply goes out one line per message. |
| `get_contacts()` | `get_contacts()` + `roster_version` | Only the conversations on screen; there is no address book behind the page. |
| `cleanup()` | `cleanup()` | Detaches. The operator's browser stays open. |

```python
import queue

from douyin4u import Douyin

dy = Douyin("http://127.0.0.1:9222", marks_path="data/inbound_marks.json")
if not dy.start():
    raise SystemExit(dy.last_error)
dy.enable_receiving_msg()
dy.add_sent_listener(lambda sec_uid, line: ...)  # every line, the moment it is on the page

while dy.is_running:
    try:
        msg = dy.get_msg()
    except queue.Empty:
        continue
    if not msg.from_self() and msg.is_text():
        result = dy.send_text("收到", msg.peer, timeout=45)
```

## What it guarantees

- **One thread touches the page.** Playwright's sync API is thread-bound, so
  the client owns a page thread for its whole life; `send_text` is safe from
  any thread and waits for the outcome. A send not started within `timeout`
  is withdrawn for certain — it will never go out later.
- **New means new by message id**, never by position in a list that
  re-renders (see `tracker.py`); marks persist across restarts, and a fan
  seen for the first time is never answered for their backlog.
- **A line is sent only when the page shows it** — an empty composer is not
  proof (`outbound.py`).
- **Humanized timing.** Keystrokes, pauses and the poll cadence are sampled
  from distributions (`humanize.py`); the account that prompted this was
  logged out by risk control on 2026-09-15 after fixed 25ms keystrokes.
  Nothing here makes automation undetectable, and driving the site this way
  is outside Douyin's terms of service.
- **Fail closed.** A page it does not recognise is left alone; a logout or a
  bot-verification challenge suspends all work and waits for a human.

What it deliberately does **not** do: log in, answer anyone (who may be
answered is the host's decision), or apply quotas and quiet hours on its own —
`SendBudget`, `parse_quiet_hours` and `in_quiet_hours` are provided for the
host, which alone knows which sends are autonomous.

## Install and test

```bash
pip install -e .            # playwright is the one dependency (driver only)
pip install pytest && pytest
```

No `playwright install` is needed: the client only ever attaches to Edge.

`tools/smoke_test.py` is a read-only live diagnostic — it attaches to the
open chat tab and prints what the client would see, without clicking,
typing or sending.

## Used by

[CowAgent-Rev](https://github.com/nyaaorick/CowAgent-Rev) mounts this repository
as a git submodule beside `WeChatFerry/`; its `channel/douyin/gateway.py` is
the host side. See `docs/RPA_NOTES.md` for the page research behind all of
the above.
