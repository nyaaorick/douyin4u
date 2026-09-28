# Douyin Channel: RPA Design Notes

> **Provenance.** Written in CowAgent-Rev (`docs/DOUYIN_CHANNEL_RPA_NOTES.md`)
> while the browser code lived in its `channel/douyin/`, and moved here with
> that code on 2026-09-28 when it became this package. Paths and names in the
> dated sections below are as they were at the time; §7 maps them to where
> the code lives now. `DouyinChannel._send_now`, `_detect_page_state` and the
> page loop are now `douyin4u.Douyin`'s; CowAgent's `DouyinChannel` keeps
> only the reply policy (who is answered, quota, quiet hours, delivery notes).

Technical record of how `channel/douyin/` was built, the page-structure
findings it relies on, and the risk-control incident that shaped its
anti-detection design. This is the Douyin counterpart to
[`WCF_WECHAT_3.9.12.56_REPAIR.md`](WCF_WECHAT_3.9.12.56_REPAIR.md) — the
kind of durable technical knowledge that belongs in the repo, not only in a
development session's own notes. `ROADMAP.md` is still the authority on
channel *state* and forward milestones; this document is the *why* behind
the current implementation.

---

## 1. Why RPA, not the official API

Douyin's official private-message API requires an authenticated
enterprise/blue-V account and a review process. This product is a personal
creator's digital avatar — it has neither, and cannot get either. The
channel instead drives the real `creator.douyin.com` chat UI over Playwright,
**attached** (never launched) via Chrome DevTools Protocol to a browser the
operator already logged into by hand. It never owns login, exactly the same
boundary `WcfChannel` keeps around the WeChat client login.

Two candidate reference projects were evaluated and rejected as templates:
`pen9un/douyin-chatgpt-bot` ships no source (screenshots only, paid, and its
own README says it needs the enterprise API this product doesn't have);
`yangmaoxin/social-harvest` is read-only (no send path) and calibrates
message direction from screen geometry against an undocumented protobuf
schema — fragile, and Phase 0 found a cleaner source of truth (§3 below).

---

## 2. Page structure: the iframe red herring

The page has a `#summon-web-iframe` (`summon.bytedance.com`) that looks, at
first glance, like it could host the DM panel. **It does not** — it is an
unrelated, hidden floating customer-service widget
(`visibility:hidden`/`opacity:0` on a parent element). The real conversation
list, message panel, and composer are direct DOM on the top-level page.

This was still a useful Playwright/CDP lesson: `connect_over_cdp` attaching
to an *already-loaded* page can miss an out-of-process iframe that existed
before the CDP session started watching (Playwright's auto-attach only
observes frames created afterward). Confirming an iframe is genuinely absent
— not just not-yet-observed — requires `page.reload(wait_until="load")` (not
`"networkidle"`; this page holds a live WebSocket open for real-time push, so
network never goes idle) followed by polling for the frame to reappear.

The same "presence isn't visibility" lesson recurred later and independently
in the risk-control work — see §5.

---

## 3. Identity and message structure

- **CSS-module classes** carry a stable *semantic prefix* plus a build hash
  that changes on redeploy (`item-header-name-eukBdz`, `box-item-W0TV01`,
  `chat-input-nSWBco`). Every selector in `page_scripts.py` matches on the
  prefix (`[class*="item-header-name-"]`), never the full hashed string. The
  top-level tab bar (全部/朋友私信/陌生人私信/群消息) is the one exception —
  it's unhashed [Semi Design](https://semi.design/) (`semi-tabs-tab`),
  ByteDance's open-source UI kit.
- **The composer is a plain `contenteditable="true"` div**, confirmed via
  `outerHTML` after opening a real thread — not Draft.js. No
  `EditorState`-style fighting is needed to type into it.
- **Direction is a literal CSS modifier class**: `is-me-<hash>` on an
  outbound bubble, absent on inbound. No geometry heuristics needed.
  `time-<hash>` siblings are date/system dividers, not messages, and are
  filtered out.
- **No `<a href>` or `data-*` attribute anywhere on the page exposes a fan's
  stable id** (verified against two full-page DOM dumps: zero matches).
  However, **the id (`secUid`) is reachable through React's own fiber tree**
  on already-mounted DOM nodes — `el['__reactFiber$...']`, walk `.return`
  repeatedly, read `.memoizedProps` — landing on a `conversation` object
  shaped roughly like:
  ```
  { id, secUid, isGroupChat, isStrangerChat, onTop, mute,
    content: { text, isFromMe, richTextInfos, mention_users }, createdTime }
  ```
  plus a separate `nickname`/`avatar_thumb` lookup. `isStrangerChat` is a
  plain boolean on this object — friend-vs-stranger classification needs no
  separate per-tab DOM scraping. This is an internal implementation detail,
  not a public contract, and can break on any Douyin front-end deploy — see
  `page_scripts.py`'s own module docstring for the fail-closed handling this
  implies (an unrecognised shape means "skip this conversation," never a
  guess).
- **The selector matches more DOM elements than there are conversations —
  confirmed live 2026-09-16.** `[class*="item-header-name-"]` matched THREE
  elements for one real conversation: the actual roster-list row (real
  layout, e.g. `x:297,y:228,w:98,h:20`) plus two phantom duplicates
  elsewhere in the page's React tree, both measuring `0×0` with no
  CSS-hidden ancestor detectable within 12 levels — a *different* trap than
  §5's presence-vs-visibility one: these duplicates' fiber ancestry
  genuinely exposes `header.props.conversation` too, so they pass the
  existing "shape not found → skip" check and get reported as extra,
  equally-"valid" roster rows for the same fan. Left unfiltered, whichever
  duplicate happens to sit at the lowest raw DOM index gets clicked by
  `_open_conversation`'s `.nth(index)` — deterministically the phantom,
  every time, failing with Playwright's "element is not visible" no matter
  how many retries. `JS_READ_ROSTER` now checks
  `span.getBoundingClientRect()` and skips any zero-area match the same way
  it already skips a missing fiber shape. A scrolled-out-of-view row still
  has a real, non-zero box, so this does not risk dropping a legitimately
  off-screen conversation — only a truly collapsed/phantom match.
- Crossing the CDP boundary safely: `json.dumps()` on the Python side will
  raise "Circular reference detected" on a live fiber-tree read unless the
  JS side stringifies the result itself. React fiber props are full of
  back-references (`.return`, `coreInfo.parent`); a naive depth-limited
  clone that returns the *raw* object past its cutoff still carries those
  references into the final structure. The fix: track visited objects in a
  JS-side `Set` during the walk, return a placeholder string (not the raw
  object) past the depth limit, and call `JSON.stringify()` **inside** the
  browser — only inert text ever crosses `page.evaluate()`'s return value.

Production implementation: `douyin4u/page_scripts.py`
(`JS_READ_ROSTER`, `JS_READ_PANEL`, `JS_DETECT_STATE`), consumed by
`channel/douyin/douyin_channel.py`.

---

## 4. Confirmed send behavior, and what's still open

Confirmed by one real, operator-approved send during Phase 0 (to a
throwaway test-account thread): `page.keyboard.type()` lands cleanly in the
composer with no corruption, and **Enter sends** (does not insert a
newline) — the composer empties and a new `is-me-<hash>` bubble appears
within ~2s. A visible "发送" button exists as a fallback path.

Left deliberately unverified — each would need many real sends or extended
observation, and Phase 0 was scoped to be minimally invasive:
- Exact length caps and per-conversation/per-day send limits, **especially
  in a stranger-tab conversation specifically** — the one thread tested had
  `isStrangerChat: false` (an existing friends-style thread), and a
  fan-avatar's real traffic is mostly strangers, which is exactly where a
  platform most plausibly applies stricter anti-spam limits.
- Real-time push (an incoming message arriving via the open WebSocket
  without a manual poll) — architecturally likely, never directly witnessed.
- The unread-badge's exact DOM/class shape (cosmetic only).

---

## 5. The 2026-09-15 risk-control incident

During Phase 0 probing, the test account was logged out mid-session —
the page reverted to its login screen, and a `rc-verifycenter/rmc-nocaptcha`
iframe (ByteDance's bot-verification widget) had appeared just before/at the
logout, confirmed by a read-only check afterward. This is **not** a
cookie-persistence bug — the browser profile's cookies were intact; this was
the *server* invalidating the session.

**Near-certain cause**: `page.keyboard.type(text, delay=25)` — a perfectly
uniform 25ms-per-character interval, a textbook automation fingerprint,
visible to the page's own input telemetry. Contributing/unconfirmed:
`connect_over_cdp`'s protocol footprint (`Runtime.enable`,
`navigator.webdriver`), and account novelty (a fresh test account with no
organic history, a threshold multiplier rather than a root cause on its
own). What is **not** server-visible at all: the fiber-tree reads in §3 —
`page.evaluate()` execution is 100% client-side JS with zero network
signature; the server cannot know this process read `__reactFiber$`.

This is the HIGH-likelihood "account gets risk-controlled" risk flagged in
the channel's original design discussion, materializing during passive Phase
0 probing alone — not even under sustained automated traffic. It is direct,
practical evidence against the naive "just automate it" version of this
channel, not a theoretical concern.

### Detecting a logged-out or challenged page

Before this incident, nothing in the design detected a logout — the
roster selectors would simply stop matching, `_scan_inbound` would return
early on an empty roster every tick, forever, with no log line at all, while
any queued reply failed with an error that blamed the fan ("not in the
currently visible conversation list") instead of naming the real cause.

Two forensic probes against the still-open, now-logged-out browser (free,
zero-risk evidence) found:
- The URL does **not** change on logout — useless as a signal.
- The roster/composer/panel selectors all drop to zero matches.
- `.douyin_login_new_class` and `[class*="douyin-login-container-"]` are
  reliable logged-out markers — the former is one of the few **unhashed**
  class names on the entire site.
- **A second "presence isn't visibility" trap, same shape as §2's iframe**:
  `#nocaptcha-container` is *already present in the DOM* on an ordinary,
  merely-logged-out page — as a `display:none`, 0×0 iframe with no actual
  challenge on screen. A presence-only check for a captcha/verification
  element would report a challenge on every single tick, forever. Detecting
  a real challenge requires checking actual visibility: a non-zero
  bounding-rect **and** `checkVisibility({checkOpacity, checkVisibilityCSS})`
  — `checkVisibility` is used ahead of an `offsetParent` check on purpose,
  since `offsetParent` is `null` for `position: fixed` elements, and a
  challenge overlay is very plausibly fixed.

Production implementation: `JS_DETECT_STATE` in `page_scripts.py`, consumed
by `DouyinChannel._detect_page_state()` — checked every tick; the channel
suspends all sending immediately on a logged-out or challenge state, logs
once (not every tick) on the transition, and backs off to a slow, fixed
30-second poll until a human resolves it.

---

## 6. Anti-detection mitigation

**Tier A — timing/volume, implemented (2026-09-16).** Directly evidenced by
§5; cheap; done. `douyin4u/humanize.py` replaced every fixed delay
with a sampled distribution: per-character typing delay (Gaussian, ~110ms
mean, clamped 45–320ms — Playwright's own `keyboard.type(delay=)` can only
apply one fixed interval to an entire string, which is *why* the typing
loop had to move into Python as `_type_like_a_person()`), an occasional
longer "thinking" pause conditioned on the character just typed (35% chance
after punctuation vs. 5% mid-text), a jittered pre-Enter pause, a reading
pause after opening a conversation (scaled to reply length), length-scaled
gaps between the lines of a multi-line reply, and a randomized poll tick
(`douyin_tick_min_seconds`/`douyin_tick_max_seconds`) instead of a fixed
interval — a perfectly regular poll cadence is itself a bot signature,
independent of anything the channel types. On top of timing: a rolling
per-hour/per-day send quota (`SendBudget`) and an optional quiet-hours
window, so the avatar doesn't reply instantly around the clock the way no
real person would.

**Tier B — reducing the CDP fingerprint itself, not implemented.**
Deliberately not pursued yet. A light option (`playwright-stealth`, maturity
unverified) and a heavier one (moving keystrokes/clicks off CDP's
`Input.dispatchKeyEvent` onto OS-level input injection — Windows `SendInput`
/ `pyautogui` / `pynput` — so they're indistinguishable from real hardware
events at the protocol level) were both scoped but not built. The heavy
option directly conflicts with §3's CDP-based fiber-walk identity mechanism,
so if it's ever pursued the right shape is a **split**: keep reads on CDP
`evaluate()` (invisible to the server regardless of CDP fingerprinting
concerns), move only writes (typing/clicking) to OS-level input. That adds
real complexity — translating a DOM element's `getBoundingClientRect()` into
screen coordinates, handling window position and DPI scaling — that
`locator.click()` currently gets for free.

**Trigger condition for Tier B**: only if, once Tier A has run stably for a
while, the account still gets logged out **and** `_detect_page_state`'s
transition log shows no time correlation between logouts and send activity.
Until then, the evidence does not distinguish a timing signature from a CDP
protocol fingerprint as the actual cause, and Tier B is expensive
speculation rather than a targeted fix.

**Live verification, 2026-09-16**: after re-logging in, one real send was
driven through the actual production path (`DouyinChannel._send_now` ->
`_type_and_send_line` -> `_type_like_a_person`, the same char-by-char
`humanize.py` sampling described above — not a bypass) to a real
friends-style conversation, with the text
`[测试] 逐字打字测试，你好呀～😊` (deliberately chosen to exercise the
punctuation-triggered pause branch and the emoji-tolerant
`_composer_holds()` check in the same message). Reading the bubble back
afterward found it byte-for-byte identical to the source text — no
corruption, no dropped characters, no IME/composition artifacts. This closes
the verification gap: per-character typing is safe against this composer for
Chinese text. Not yet exercised: the same test against a stranger-tab
conversation (this account's only thread is `isStrangerChat: false`).

**Explicit ceiling, not solved by any of the above**: this is an
adversarial, well-resourced detection system. Server-side signals beyond
client-side timing and protocol fingerprints plausibly exist and cannot be
observed or ruled out from here. Working around Douyin's anti-automation
measures is also an independent business/ToS risk, separate from whatever is
technically achievable — worth weighing on its own terms, not folded into
"is it detectable."

---

## 7. Where the code lives

| Concern | File |
|---|---|
| The client: attach, page thread, page-state detection, `get_msg`/`send_text` | `douyin4u/client.py` |
| Which messages are new (message ids, marks), and the inbound scan order | `douyin4u/tracker.py`, `douyin4u/inbound.py` |
| Humanized typing, send confirmation on the panel | `douyin4u/outbound.py` |
| Timing samplers, send quota (`SendBudget`) | `douyin4u/humanize.py` |
| Browser-side JS (roster/panel reads, page-state detection) | `douyin4u/page_scripts.py` |
| Starting the debug browser | `douyin4u/browser.py` |
| CowAgent: the gateway (settings, account quota, fan catalog feed) | `CowAgent/channel/douyin/gateway.py` |
| CowAgent: reply policy, contact gate, console mirror, operator takeover | `CowAgent/channel/douyin/douyin_channel.py` (on `channel/gateway_channel.py`, shared with WeChat) |
| CowAgent: `DyMsg` → `ChatMessage` mapping | `CowAgent/channel/douyin/douyin_message.py` |
| CowAgent: fan catalog for the console (accumulated from roster reads, not a scan — see §8) | `CowAgent/channel/douyin/roster_scanner.py` |
| Phase 0 reconnaissance scripts — removed 2026-09-21, recoverable from git history (`git show dff14aca:CowAgent/channel/douyin/_phase0_probes/README.md`); `probe_4`/`probe_6` click and send on a real account, and a re-run's output captures real fan content, so never commit it | — |
| Live, read-only diagnostic tool (the maintained successor to the Phase 0 probes) | `tools/smoke_test.py` |
| CowAgent: console address-book endpoints, shared with WCF by dispatch, not by code | `CowAgent/channel/web/contacts_api.py` |
| CowAgent: console switch store, channel-parameterized (`get_contact_state("douyin")`) | `CowAgent/channel/wcf/contact_state.py` |
| CowAgent: console frontend (channel switch tab, address-book rendering) | `CowAgent/channel/web/static/js/contacts.js` |
| Tests (no real browser required — a `FakePage` double covers all logic in this document) | `tests/test_douyin_channel.py`, `tests/test_douyin_humanize.py`, `tests/test_douyin_outbound.py`, `tests/test_douyin_scanner.py` |

---

## 8. Console integration (2026-09-16)

The console now treats Douyin the way it already treats WeChat — same
session list, same fail-closed switch store, same operator-takeover path —
but the console-facing code got there by **dispatch, not duplication**: one
handler per endpoint picking a channel-specific backend, rather than a
parallel `douyin_contacts_api.py`.

**The fan catalog is not a scan.** `channel/wcf/scanner.py`'s
`ContactScanner` reads a real address-book database in one pass — nothing
equivalent exists for Douyin. The creator-center roster
(`page_scripts.py`'s `JS_READ_ROSTER`) only ever shows conversations
currently rendered on screen, with no scroll, search, or pagination in
Milestone 1. `channel/douyin/roster_scanner.py`'s `RosterScanner` mirrors
`ContactScanner`'s row shape and its atomic on-disk cache, but its discovery
mechanism is accumulation: every roster read `_scan_inbound` already does
for its own inbound polling is folded into the catalog
(`RosterScanner.observe_roster`), so the console's fan list grows toward
completeness over many ticks instead of being filled by an on-demand scan
button. A fan does not drop out of the catalog once they scroll out of the
visible roster.

**`send_as_operator` cannot be a synchronous RPC call the way WCF's is.**
`WcfChannel.send_as_operator` calls a thread-safe client method directly and
returns its status. Every Douyin send has to happen on the one thread that
owns `self._page` (§0 / the module docstring's threading contract), so
`DouyinChannel.send_as_operator` hands the message to that thread through a
dedicated `_operator_outbox` queue and blocks on a per-call
`queue.Queue(maxsize=1)` result channel for the loop thread to actually type
it and report back — claiming success the instant a message is queued would
let the console tell the operator "sent" before the browser had done
anything. It deliberately bypasses quiet hours and the send quota (a human
directly choosing to send one message right now is not the
automated-around-the-clock pattern those exist to prevent) but **not** the
humanized per-character pacing from §6 — skipping that for an
operator-typed message would reintroduce the exact keystroke-uniformity
signature that caused the incident in §5, regardless of who decided the
content. A timeout (`_OPERATOR_SEND_TIMEOUT_SECONDS`, one blocked-page tick
plus margin) is reported as failure, but the queued entry is not cancelled —
there is no cheap way to cancel a handoff already made to another thread
via a plain `queue.Queue` — so a timeout carries a disclosed, rare
double-send risk if the operator retypes the message by hand and the
original entry goes out anyway once the loop recovers.

**The switch store (`contact_state.py`) turned out to already be
channel-agnostic** apart from one WeChat-specific detail (`@chatroom` suffix
matching for rooms, which simply never matches a sec_uid). Parameterizing
`get_contact_state(channel)` by a data-root subdirectory was enough to give
Douyin its own document (`data/douyin/contacts_state.json`) without
touching a single existing call site's behavior.

**Live channel status, added the same day.** `DouyinChannel.status_summary()`
exposes a plain-attribute snapshot (`_running`, `_page_state`) for the
console's Channels card to poll every 5s. Reached through
`DouyinChannel()` (the `@singleton` instance), not `app.get_channel_manager()`
— `app.py` is the process entry point, so it runs as `sys.modules["__main__"]`;
any later `from app import ...` elsewhere imports the file a *second* time
under the name `"app"`, a distinct module object whose own `_channel_mgr`
module global never gets set. `channel/web/contacts_api.py`'s
`live_wcf_client()` established the working pattern first (reach a channel
through its own singleton class); `ChannelsHandler._live_status()` now
mirrors it for both `wcf` and `douyin`.
