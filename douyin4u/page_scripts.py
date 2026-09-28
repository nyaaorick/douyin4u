# encoding:utf-8

"""The browser-side half of douyin4u: JS evaluated in the page.

Kept apart from ``client.py`` because it is a different language with
different constraints, and because keeping it inline pushed that file past
this project's 800-line ceiling. Nothing here executes in Python; each
constant is a string handed to ``page.evaluate()``.

The page is Douyin's main-site chat, ``www.douyin.com/chat?isPopup=1`` (the
message window opened from the right-hand sidebar of ``www.douyin.com/user/self``).
It was the creator-center chat (``creator.douyin.com``) until 2026-09-21, when
every message sent from there turned out to be stored as visible only to the
sender -- see ``JS_READ_PANEL``'s ``self_only``.

Three conventions hold across the scripts, and all are load-bearing:

1. **Each returns a JSON string, never an object.** React fiber nodes are
   full of back-references (``.return``, ``coreInfo.parent``), and letting
   Playwright transport a live object graph across the CDP boundary once
   produced a "circular reference detected" failure in Python. Stringifying
   inside the browser means only inert text ever crosses that boundary.
2. **``data-e2e`` hooks first, class names second.** This page ships test
   hooks (``conversation-item``, ``msg-input``) that are the most stable
   handle it offers, and they are used wherever one exists. Where none does,
   the page's CSS-module class names are *unhashed* component paths
   (``messageMessageBoxmessageBox``), matched as whole class tokens -- a
   substring match would also catch their siblings
   (``conversationConversationItemtitleWrapper`` contains ``...title``).
3. **Identity comes from the objects React renders, not from the DOM.** The
   fan's ``secUid``, each message's ``serverId`` and sequence reach no DOM
   attribute; they sit on the conversation and message objects in the fiber
   props one level above a row. Those objects are class instances whose
   fields are prototype getters, so ``Object.keys`` shows almost nothing --
   read the fields by name.

See ``client.py``'s module docstring for why the fiber tree has to be
walked at all.
"""

# Read the visible conversation roster. Each row's conversation object sits
# one fiber level above its `data-e2e="conversation-item"` element (found by a
# read-only probe, 2026-09-21; searched over a few levels here so a shallow
# future refactor does not silently break this). `index` is the row's position
# among every match, which is what the channel's click targets with nth().
#
# `created_time_ms` is the newest message's time -- what tells "this fan said
# something" from "this fan said the same thing again", which the preview text
# alone cannot (see inbound.InboundScanMixin._changed). `type` 1 is a one-to-one chat;
# anything else is treated as a group and never opened (fail closed).
JS_READ_ROSTER = r"""
() => {
  const findFiberKey = (el) => Object.keys(el).find((k) => k.startsWith('__reactFiber$'));
  const conversationOf = (el) => {
    const key = findFiberKey(el);
    let fiber = key ? el[key] : null;
    for (let i = 0; i < 6 && fiber; i++) {
      const props = fiber.memoizedProps;
      const convo = props && typeof props === 'object' ? props.conversation : null;
      if (convo && typeof convo === 'object' && typeof convo.toParticipantSecUserId === 'string') return convo;
      fiber = fiber.return;
    }
    return null;
  };
  const textOf = (message) => {
    try { return String(JSON.parse(message.content || '{}').text || ''); } catch (e) { return ''; }
  };
  // Same reading of a protobuf Long as JS_READ_PANEL's.
  const longText = (v) => {
    if (v === null || v === undefined) return '';
    if (typeof v !== 'object') return String(v);
    const s = String(v);
    if (/^\d+$/.test(s)) return s;
    if (v.high === 0 && typeof v.low === 'number') return String(v.low >>> 0);
    return '';
  };
  const results = [];
  document.querySelectorAll('[data-e2e="conversation-item"]').forEach((row, index) => {
    // A row with no layout cannot be clicked. The creator-center page had
    // zero-area phantom duplicates of real rows (2026-09-16); none have been
    // seen here, but a click aimed at one fails the same way, so skip it.
    const rect = row.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const convo = conversationOf(row);
    if (!convo) return; // shape not found -- fail closed, skip this entry
    const last = convo.lastMessage || null;
    const created = last && last.createdAt ? new Date(last.createdAt).getTime() : NaN;
    const title = row.querySelector('.conversationConversationItemtitle');
    results.push({
      index,
      sec_uid: convo.toParticipantSecUserId || '',
      is_group: convo.type !== 1,
      is_stranger: Boolean(convo.isStrangerConversation),
      is_from_me: Boolean(last && last.isFromMe),
      preview_text: last ? textOf(last) : '',
      nickname: title ? String(title.innerText || '').trim() : '',
      is_active: /(^|\s)conversationConversationItemcurConversation(\s|$)/.test(String(row.className)),
      created_time_ms: Number.isFinite(created) ? created : null,
      // The newest message's sequence: what the open panel must be showing
      // before a drain of it may count as complete (inbound_tracker.panel_is_behind).
      last_seq: last ? longText(last.indexInConversationV2) : '',
      badge_count: typeof convo.badgeCount === 'number' ? convo.badgeCount : null,
    });
  });
  return JSON.stringify(results);
}
"""

# Read the currently-open panel: which conversation it shows and every message
# on it, oldest first, classified so the channel can tell a text message apart
# from a photo or a sticker without ever constructing a DouyinMessage for the
# wrong kind.
#
# Every row carries the real message's identity, read off the message object
# React keeps one fiber level above the row: `serverId`, the server-assigned
# per-conversation sequence `indexInConversationV2` (a protobuf Long, not a
# number), `createdAt`, and `conversationShortId`. These -- not a row's
# position -- are what tell a new message from an old one (see
# inbound_tracker.py). They are the same ids the creator-center page exposed,
# so marks saved before the move stayed valid.
#
# The list is virtualized and reversed: DOM order is newest first. Rows are
# put back in on-screen order (top to bottom = oldest to newest), because the
# tracker stops at the first row without ids -- in DOM order that would be the
# bubble still being sent, and nothing behind it would ever be read.
#
# `self_only` is why this page is used at all. Douyin can store a message as
# visible to its sender only -- the message object's `ext` then names the
# sender's uid in `s:visible` and sets `visible_code` 1, with
# `im_callback_status_code` 8101. The sender's own clients, phone included,
# show it as sent; the recipient never receives it. Every message sent from
# the creator-center chat on 2026-09-21 came back like this, while the same
# account's phone messages in the same thread did not.
JS_READ_PANEL = r"""
() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const findFiberKey = (el) => Object.keys(el).find((k) => k.startsWith('__reactFiber$'));
  const idText = (v) => (v === null || v === undefined) ? '' : String(v);
  const longText = (v) => {
    if (v === null || v === undefined) return '';
    if (typeof v !== 'object') return String(v);
    const s = String(v);
    if (/^\d+$/.test(s)) return s;
    // A Long whose toString is not decimal: only a small non-negative value
    // can be read without guessing at the high word's sign.
    if (v.high === 0 && typeof v.low === 'number') return String(v.low >>> 0);
    return '';
  };
  const propOf = (el, name, depth) => {
    const key = findFiberKey(el);
    let fiber = key ? el[key] : null;
    for (let i = 0; i < depth && fiber; i++) {
      const props = fiber.memoizedProps;
      const value = props && typeof props === 'object' ? props[name] : null;
      if (value && typeof value === 'object') return value;
      fiber = fiber.return;
    }
    return null;
  };

  // Which conversation these rows actually belong to, resolved in the *same*
  // evaluate() as the rows themselves, from the message list's own
  // `curConversation` prop. The operator shares this browser and can switch
  // conversations at any moment; an id read separately (a beat earlier, in
  // the roster call) could already be stale by the time these rows are
  // collected, which would file one fan's messages under another fan's id.
  // The roster's highlighted row is only the fallback.
  const listEl = document.querySelector('.messageMessageListwrapper');
  let current = listEl ? propOf(listEl, 'curConversation', 6) : null;
  if (!current || typeof current.toParticipantSecUserId !== 'string') {
    const activeRow = document.querySelector('[data-e2e="conversation-item"].conversationConversationItemcurConversation');
    current = activeRow ? propOf(activeRow, 'conversation', 6) : null;
  }
  const activeSecUid = current && typeof current.toParticipantSecUserId === 'string' ? current.toParticipantSecUserId : '';
  const activeShortId = current ? idText(current.shortId) : '';

  const bubbleText = (contentEl) => {
    let out = '';
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) { out += node.nodeValue; return; }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      if (node.tagName === 'IMG') { out += node.getAttribute('alt') || ''; return; }
      node.childNodes.forEach(walk);
    };
    walk(contentEl);
    return out;
  };
  const textOf = (msg, el) => {
    try {
      const parsed = JSON.parse((msg && msg.content) || '{}');
      if (typeof parsed.text === 'string') return parsed.text;
    } catch (e) { /* fall back to what is on screen */ }
    const textEl = el.querySelector('[class*="TextMessageTexttextInnerContent"]');
    return textEl ? bubbleText(textEl) : '';
  };

  const boxes = Array.from(document.querySelectorAll('.messageMessageBoxmessageBox'))
    .map((el) => ({ el, top: el.getBoundingClientRect().top }))
    .sort((a, b) => a.top - b.top);

  const items = boxes.map(({ el }, index) => {
    const msg = propOf(el, 'message', 4);
    const cls = String(el.className);
    const isMe = msg ? Boolean(msg.isFromMe) : /(^|\s)messageMessageBoxisFromMe(\s|$)/.test(cls);
    let kind = 'unknown';
    if (el.querySelector('[class*="MessageItemText"]')) kind = 'text';
    else if (el.querySelector('[class*="MessageItemImage"]')) kind = 'image';
    const ext = (msg && msg.ext) || {};
    const created = msg ? new Date(msg.createdAt).getTime() : NaN;
    return {
      index,
      kind,
      is_me: isMe,
      text: kind === 'text' ? norm(textOf(msg, el)) : '',
      server_id: msg ? idText(msg.serverId) : '',
      seq: msg ? longText(msg.indexInConversationV2) : '',
      created_at_ms: Number.isFinite(created) ? created : null,
      short_id: msg ? idText(msg.conversationShortId) : '',
      self_only: Boolean(ext['s:visible']) || String(ext['visible_code'] || '') === '1',
      callback_code: idText(ext['im_callback_status_code']),
    };
  });

  // How far the list is scrolled from its newest end. The scroller is
  // rotated 180deg, so scrollTop 0 is the newest message (at the visual
  // bottom) and anything more is the operator reading history -- in which
  // case the newest messages are not rendered at all.
  const scroller = document.querySelector('.messageMessageListlist');
  const scrolledPx = scroller ? Math.abs(scroller.scrollTop) : null;

  const headerEl = document.querySelector('.RightPanelHeadertitle');
  return JSON.stringify({
    header_text: headerEl ? norm(headerEl.textContent) : null,
    active_sec_uid: activeSecUid,
    active_short_id: activeShortId,
    scrolled_px: scrolledPx,
    items,
  });
}
"""

# Scroll the open conversation back to its newest message.
#
# The list is virtualized, so while it sits scrolled up the newest messages
# are not on the page at all and nothing new can be read (see
# inbound_tracker.panel_is_behind). On 2026-09-21 an open thread was found
# scrolled up several times, and what arrived meanwhile reached the console
# only once someone scrolled it back by hand. Setting scrollTop is a client-side
# change the server never sees. The scroller is rotated 180deg, so 0 is the
# newest end (see JS_READ_PANEL's scrolled_px).
JS_SCROLL_TO_NEWEST = r"""
() => {
  const scroller = document.querySelector('.messageMessageListlist');
  if (!scroller) return JSON.stringify({ scrolled_back: false, before: null });
  const before = Math.abs(scroller.scrollTop);
  scroller.scrollTop = 0;
  return JSON.stringify({ scrolled_back: true, before: before });
}
"""

# What the composer holds, and whether it still owns the caret.
#
# Both in one read because they are needed at the same moments (before typing,
# and again after), and because a read costs nothing this channel has to
# budget for: page.evaluate() runs entirely inside the browser and is the one
# thing here with no server-visible signature at all.
#
# The composer is a Slate editor (`data-slate-editor`). Empty, it still holds
# one line containing a single zero-width space, so innerText is never ""; that
# character is stripped, or every send would read as "Enter left text behind".
#
# `focused` is the part that matters. Typing goes wherever the caret is, so a
# composer that lost it mid-reply sends the rest of the line somewhere else --
# on 2026-09-17 a fan received only the tail of a reply for what looks like
# exactly this reason, while the operator was on the same page. activeElement
# is the authority (the contenteditable itself takes focus, but a child node is
# tolerated too); document.hasFocus() is reported alongside as advisory only --
# a browser window that is merely behind another one still types fine, so it
# must never gate a send.
JS_COMPOSER_STATE = r"""
() => {
  const el = document.querySelector('[data-e2e="msg-input"] [contenteditable="true"]');
  const pageFocused = document.hasFocus();
  if (!el) return JSON.stringify({ present: false, text: '', focused: false, page_focused: pageFocused });
  const active = document.activeElement;
  return JSON.stringify({
    present: true,
    text: String(el.innerText || '').replace(/[​-‍﻿]/g, ''),
    focused: Boolean(active && (active === el || el.contains(active))),
    page_focused: pageFocused,
  });
}
"""

# Is the attached tab still a usable chat page?
#
# This exists because of a real incident: on 2026-09-15 the account was
# logged out mid-session by risk control. Nothing detected it. The roster
# selectors simply stopped matching, _scan_inbound returned early on an empty
# roster every tick, and the channel spun silently forever -- while queued
# replies failed with "not in the currently visible conversation list", which
# blamed the fan instead of naming the logout.
#
# Presence is deliberately NOT the visibility test. `#nocaptcha-container` is
# in the DOM of an ordinary, logged-in chat page as a 0x0 iframe with no
# challenge on screen (seen on both the creator-center page and this one), so
# a presence-only check would report a challenge on every tick forever. An
# element counts only if it occupies real space AND passes checkVisibility();
# checkVisibility is used ahead of an offsetParent test on purpose, because
# offsetParent is null for position:fixed elements and a challenge overlay is
# very plausibly fixed.
#
# The logged-out markers are the creator-center ones; this page's own login
# screen has not been seen yet. A logout it does not recognise still stops the
# channel -- no roster and no composer reads as `chat_not_rendered` -- it is
# just named less precisely. So is an inbox with no conversations at all.
JS_DETECT_STATE = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    if (typeof el.checkVisibility === 'function') {
      return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    }
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden'
        && Number(style.opacity) > 0.01;
  };

  const challenge = Array.from(document.querySelectorAll(
    '[id*="verifycenter"], [class*="verifycenter"], [id*="nocaptcha"],'
    + ' [class*="nocaptcha"], [class*="captcha"]'
  )).some(visible);

  const loggedOut = document.querySelectorAll(
    '.douyin_login_new_class, [class*="douyin-login-container-"]'
  ).length > 0;

  const chatReady = document.querySelectorAll('[data-e2e="conversation-item"]').length > 0
    || document.querySelectorAll('[data-e2e="msg-input"]').length > 0;

  return JSON.stringify({
    challenge: challenge,
    logged_out: loggedOut,
    chat_ready: chatReady,
    ready_state: document.readyState,
  });
}
"""
