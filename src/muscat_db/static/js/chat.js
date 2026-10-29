document.addEventListener("DOMContentLoaded", () => {
    const socket = io();

    const chatWindow = document.getElementById("chat-window");
    const chatHeader = document.getElementById("chat-header");
    const minimizeChat = document.getElementById("minimize-chat");
    const popoutChat = document.getElementById("popout-chat");
    const resizeHandle = document.getElementById("chat-resize-handle");
    const chatMessages = document.getElementById("chat-messages");
    const chatEmpty = document.getElementById("chat-empty");
    const messageInput = document.getElementById("message-input");
    const sendButton = document.getElementById("send-button");
    const statusDot = document.getElementById("chat-status-dot");
    const onlineEl = document.getElementById("chat-online");
    const unreadBadge = document.getElementById("chat-unread");
    const typingEl = document.getElementById("chat-typing");
    const autocompleteEl = document.getElementById("chat-autocomplete");

    if (!chatWindow) return;
    chatWindow.classList.remove("hidden");

    // The standalone /chat/popout window reuses this same markup + script;
    // it fills the whole browser window (see body.chat-popout in styles.css)
    // so resizing/minimizing/popping-out-again don't apply there.
    const isPopout = chatWindow.dataset.popout === "1";
    const MOBILE_BREAKPOINT = 480;
    const isMobileViewport = () => window.innerWidth <= MOBILE_BREAKPOINT;

    const PRESET_EMOJI = ["👍", "🎉", "👀", "✅", "❤️", "😄"];
    const HERE_TOKEN = /@here\b/gi;

    // URLs | relative app links (/target?name=…, /photometry?…, /lco/archive) | target names (TOI-1234 / TIC 12345678) | @mentions.
    // Built at runtime so an environment lacking lookbehind falls back instead
    // of a parse error.
    let TOKEN_SRC;
    try {
        new RegExp("(?<!x)");
        TOKEN_SRC = "(https?://[^\\s]+)|(/(?:target|photometry|transit-fit|lco/archive|jobs)(?:[?#][^\\s]*)?(?![\\w/-]))|(\\bTOI[-\\s]?\\d+(?:\\.\\d+)?\\b|\\bTIC[-\\s]?\\d+\\b)|(?<![\\w@])@([A-Za-z0-9._-]+)";
    } catch (e) {
        TOKEN_SRC = "(https?://[^\\s]+)|(/(?:target|photometry|transit-fit|lco/archive|jobs)(?:[?#][^\\s]*)?(?![\\w/-]))|(\\bTOI[-\\s]?\\d+(?:\\.\\d+)?\\b|\\bTIC[-\\s]?\\d+\\b)|@([A-Za-z0-9._-]+)";
    }

    let isMinimized = false;
    let savedHeight = "";   // inline height stashed while minimized, restored on expand
    let unread = 0;
    let knownUsers = [];
    let acItems = [];
    let acIndex = -1;
    let typingSent = false;
    let typingStopTimer = null;
    const typingUsers = new Map();     // user -> clear-timeout id
    const els = new Map();             // message id -> { el, data }
    const botEls = new Map();          // private @bot entry id -> { el, data }
    const BOT_STORE_PREFIX = "muscat-bot-chat:";
    const BOT_STORE_MAX = 200;         // newest entries kept per user
    // The private @bot exchange: the asker's question echo and every bot note.
    const isBotMessage = (data) => !!(data && data.private && data.kind !== "help");
    const botEntryId = (data) => data.ts + "|" + data.kind;
    const origTitle = document.title;
    let flashTimer = null;

    // ----- identity --------------------------------------------------------
    let currentUser = (chatWindow.dataset.currentUser || "").trim();
    const getCurrentUser = () => currentUser || "Anonymous";
    const isMine = (data) =>
        !!(currentUser && data.user && data.kind !== "system" &&
           data.user.toLowerCase() === currentUser.toLowerCase());

    const onUserResolved = (u) => {
        if (!u || u === currentUser) return;
        currentUser = u;
        refreshOwnership();
        restoreBotLog();
    };
    if (window.MuscatWhoami && typeof window.MuscatWhoami.then === "function") {
        window.MuscatWhoami.then(onUserResolved);
    } else {
        fetch("/whoami", { headers: { "X-Requested-With": "fetch" } })
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => onUserResolved(d && typeof d.user === "string" ? d.user.trim() : ""))
            .catch(() => {});
    }

    fetch("/chat/users", { headers: { "X-Requested-With": "fetch" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { if (d && Array.isArray(d.users) && activeRoom === "chat:public") knownUsers = d.users; })
        .catch(() => {});

    // ----- helpers ---------------------------------------------------------
    const expandHere = (text) => text.replace(HERE_TOKEN, window.location.href);

    const shortenUrl = (href) => {
        let label = href;
        try {
            const u = new URL(href, window.location.origin);
            label = (u.origin === window.location.origin)
                ? (u.pathname + u.search + u.hash) || "/"
                : u.hostname + u.pathname;
        } catch (e) { /* raw href */ }
        return label.length > 64 ? label.slice(0, 61) + "…" : label;
    };

    // History spans several days, so anything not from today carries its date
    // (and the year once it is from a previous year).
    const formatTime = (ts) => {
        const d = ts ? new Date(ts * 1000) : new Date();
        const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        const now = new Date();
        if (d.toDateString() === now.toDateString()) return time;
        const dateOpts = { month: "short", day: "numeric" };
        if (d.getFullYear() !== now.getFullYear()) dateOpts.year = "numeric";
        return d.toLocaleDateString([], dateOpts) + ", " + time;
    };

    const makeLink = (href, textContent, title) => {
        const a = document.createElement("a");
        a.href = href;
        a.textContent = textContent;
        if (title) a.title = title;
        a.className = "chat-link";
        if (/^https?:/i.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
        return a;
    };

    // Sentence punctuation after a URL ("see https://x/y." or "(https://x/y)")
    // is not part of it. A closing bracket is kept only when the URL itself
    // opened one, so wiki-style "/Foo_(bar)" links survive.
    const TRAILING_PUNCT = ".,;:!?'\"";
    const trimUrl = (url) => {
        let end = url.length;
        const count = (ch) => url.slice(0, end).split(ch).length - 1;
        while (end > 0) {
            const c = url[end - 1];
            if (TRAILING_PUNCT.includes(c)) { end--; continue; }
            if (c === ")" && count(")") > count("(")) { end--; continue; }
            if (c === "]" && count("]") > count("[")) { end--; continue; }
            break;
        }
        return url.slice(0, end);
    };

    // Render text as safe DOM nodes, linkifying URLs + target names and
    // highlighting @mentions (self-mentions stand out).
    const renderText = (container, text) => {
        const re = new RegExp(TOKEN_SRC, "gi");
        let last = 0, m;
        while ((m = re.exec(text)) !== null) {
            if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
            if (m[1] || m[2]) {
                const url = trimUrl(m[1] || m[2]);
                if (!url) {  // nothing left but punctuation: plain text
                    container.appendChild(document.createTextNode(m[0]));
                } else {
                    container.appendChild(makeLink(url, shortenUrl(url), url));
                    // Resume right after the link so trimmed punctuation renders as text.
                    re.lastIndex = m.index + url.length;
                }
            } else if (m[3]) {
                container.appendChild(makeLink("/target?name=" + encodeURIComponent(m[3]), m[3], "Open " + m[3]));
            } else if (m[4] !== undefined) {
                const span = document.createElement("span");
                span.className = "chat-mention";
                if (currentUser && m[4].toLowerCase() === currentUser.toLowerCase()) {
                    span.classList.add("mention-self");
                }
                span.textContent = "@" + m[4];
                container.appendChild(span);
            }
            last = re.lastIndex;
            if (m.index === re.lastIndex) re.lastIndex++;
        }
        if (last < text.length) container.appendChild(document.createTextNode(text.slice(last)));
    };

    // ----- markdown (@bot replies only) -------------------------------------
    // The model answers in Markdown. Render the subset it uses -- headings,
    // bullet/numbered lists (nested by indent), fenced code, **bold**, *italic*,
    // `code`, [label](url) -- as DOM nodes, never innerHTML, so model output
    // stays inert. Plain runs still go through renderText (links, @mentions).
    // Teammates' messages are not parsed: "**" typed by a person stays literal.
    // Underscore emphasis is skipped on purpose: it would mangle identifiers
    // such as run_photometry.
    const MD_INLINE_SRC = "(`+)([^`]|[^`][\\s\\S]*?[^`])\\1(?!`)" +      // 1-2 `code`
        "|\\*\\*(?=\\S)([\\s\\S]*?\\S)\\*\\*" +                             // 3 **bold**
        "|\\[([^\\]\\n]+)\\]\\(([^)\\s]+)\\)" +                              // 4-5 [label](url)
        "|(^|[^\\w*])\\*(?=[^\\s*])([^*\\n]*?[^\\s*])\\*(?![\\w*])";         // 6-7 *italic*
    const MD_SAFE_HREF = /^(https?:\/\/|\/(?!\/))/i;

    const renderInline = (container, text) => {
        const re = new RegExp(MD_INLINE_SRC, "g");
        let last = 0, m;
        while ((m = re.exec(text)) !== null) {
            // *italic* consumes the character before it; that char is plain text.
            const start = m.index + (m[6] !== undefined ? m[6].length : 0);
            if (start > last) renderText(container, text.slice(last, start));
            if (m[1]) {
                const code = document.createElement("code");
                code.textContent = m[2].trim() || m[2];
                container.appendChild(code);
            } else if (m[3] !== undefined) {
                const strong = document.createElement("strong");
                renderInline(strong, m[3]);
                container.appendChild(strong);
            } else if (m[4] !== undefined) {
                // Only http(s) and same-site paths become links; anything else
                // (javascript:, data:, //host) renders as its label alone.
                if (MD_SAFE_HREF.test(m[5])) container.appendChild(makeLink(m[5], m[4], m[5]));
                else renderText(container, m[4]);
            } else {
                const em = document.createElement("em");
                renderInline(em, m[7]);
                container.appendChild(em);
            }
            last = re.lastIndex;
        }
        if (last < text.length) renderText(container, text.slice(last));
    };

    const MD_FENCE = /^\s*```/;
    const MD_HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;
    const MD_RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
    const MD_ITEM = /^(\s*)([*+-]|\d{1,3}[.)])\s+(.*)$/;

    const appendCodeBlock = (container, lines) => {
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = lines.join("\n");
        pre.appendChild(code);
        container.appendChild(pre);
    };

    // lists: the open lists, outermost first, as { el, indent, li }. Returns the
    // list stack after adding `item` (a MD_ITEM match) as a new <li>.
    const appendListItem = (container, lists, item) => {
        const indent = item[1].replace(/\t/g, "    ").length;
        const tag = /\d/.test(item[2]) ? "ol" : "ul";
        const open = lists.filter((l) => l.indent <= indent);
        let top = open[open.length - 1];
        if (top && top.indent === indent && top.el.tagName.toLowerCase() !== tag) {
            open.pop();  // "1." after "*" at the same depth starts a new list
            top = open[open.length - 1];
        }
        if (!top || top.indent < indent) {
            const list = document.createElement(tag);
            (top && top.li ? top.li : container).appendChild(list);
            top = { el: list, indent, li: null };
            open.push(top);
        }
        const li = document.createElement("li");
        renderInline(li, item[3]);
        top.el.appendChild(li);
        top.li = li;
        return open;
    };

    const renderMarkdown = (container, text) => {
        const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
        let para = null;    // open <p>, continued by the next plain line
        let lists = [];

        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (MD_FENCE.test(line)) {
                const body = [];
                while (++i < lines.length && !MD_FENCE.test(lines[i])) body.push(lines[i]);
                para = null; lists = [];
                appendCodeBlock(container, body);
                continue;
            }
            if (!line.trim()) { para = null; continue; }  // a blank line ends a paragraph, not a list
            const heading = line.match(MD_HEADING);
            if (heading || MD_RULE.test(line)) {
                para = null; lists = [];
                const el = document.createElement(heading ? "p" : "hr");
                if (heading) { el.className = "md-heading"; renderInline(el, heading[1]); }
                container.appendChild(el);
                continue;
            }
            const item = line.match(MD_ITEM);
            if (item) {
                para = null;
                lists = appendListItem(container, lists, item);
                continue;
            }
            // An indented line under a list item continues that item.
            const top = lists[lists.length - 1];
            if (top && top.li && /^\s/.test(line)) {
                top.li.appendChild(document.createElement("br"));
                renderInline(top.li, line.trim());
                continue;
            }
            lists = [];
            if (para) {
                para.appendChild(document.createElement("br"));
            } else {
                para = document.createElement("p");
                container.appendChild(para);
            }
            renderInline(para, line.trim());
        }
    };

    // ----- unread / minimize ----------------------------------------------
    const clearUnread = () => { unread = 0; unreadBadge.textContent = "0"; unreadBadge.classList.add("hidden"); };
    const bumpUnread = () => {
        if (!isMinimized) return;
        unread += 1;
        unreadBadge.textContent = unread > 99 ? "99+" : String(unread);
        unreadBadge.classList.remove("hidden");
    };
    const setMinimized = (value) => {
        isMinimized = value;
        chatWindow.classList.toggle("minimized", value);
        minimizeChat.textContent = value ? "+" : "–";
        minimizeChat.setAttribute("aria-label", value ? "Expand chat" : "Minimize chat");
        if (value) {
            // Drop any drag-resized inline height so the window can collapse to
            // its header (inline styles outrank the .minimized CSS rule); stash
            // it to restore on expand.
            savedHeight = chatWindow.style.height;
            chatWindow.style.height = "";
        } else {
            if (savedHeight) { chatWindow.style.height = savedHeight; savedHeight = ""; }
            clearUnread(); stopTitleFlash(); chatMessages.scrollTop = chatMessages.scrollHeight;
        }
        // The pop-out window is always expanded — never let it write the shared
        // minimize preference and clobber the floating widget's state.
        if (!isPopout) { try { localStorage.setItem("muscat-chat-min", value ? "1" : "0"); } catch (e) {} }
    };
    chatHeader.addEventListener("click", (e) => {
        if (isPopout) return;  // the pop-out window is always expanded
        if (e.target.closest("#chat-online")) return;  // let the online list be inspected
        setMinimized(!isMinimized);
    });

    // ----- resize (drag the top-left handle; the window is anchored bottom-right) ---
    const applySize = (w, h) => { chatWindow.style.width = w + "px"; chatWindow.style.height = h + "px"; };
    const saveSize = () => {
        try {
            const rect = chatWindow.getBoundingClientRect();
            localStorage.setItem("muscat-chat-size", JSON.stringify({ w: rect.width, h: rect.height }));
        } catch (e) {}
    };
    const restoreSize = () => {
        if (isMobileViewport()) return;  // mobile layout pins left/right; let CSS own the size
        try {
            const raw = localStorage.getItem("muscat-chat-size");
            const parsed = raw ? JSON.parse(raw) : null;
            if (!(parsed && Number.isFinite(parsed.w) && Number.isFinite(parsed.h))) return;
            chatWindow.style.width = parsed.w + "px";
            // While minimized the window must stay collapsed; hold the height
            // aside so expanding restores it instead of inflating the header.
            if (isMinimized) savedHeight = parsed.h + "px";
            else chatWindow.style.height = parsed.h + "px";
        } catch (e) {}
    };
    if (resizeHandle && !isPopout) {
        let dragging = false, startX = 0, startY = 0, startW = 0, startH = 0;
        const onMove = (e) => {
            if (!dragging) return;
            // Dragging up/left grows the box; CSS min/max-width/height clamp the result.
            applySize(startW + (startX - e.clientX), startH + (startY - e.clientY));
        };
        const onUp = () => {
            if (!dragging) return;
            dragging = false;
            document.body.classList.remove("chat-resizing");
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            saveSize();
        };
        resizeHandle.addEventListener("mousedown", (e) => {
            if (isMobileViewport()) return;
            e.preventDefault();
            dragging = true;
            const rect = chatWindow.getBoundingClientRect();
            startX = e.clientX; startY = e.clientY; startW = rect.width; startH = rect.height;
            document.body.classList.add("chat-resizing");
            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", onUp);
        });
        restoreSize();
        // Only act on a mobile/desktop breakpoint crossing: mobile drops the
        // custom size so the fixed-corner CSS layout takes over, and coming
        // back re-applies it. CSS max-width/max-height already keep the
        // custom size in bounds as the viewport changes, so no per-resize
        // recompute is needed on the desktop side.
        window.addEventListener("resize", () => {
            if (isMobileViewport()) {
                if (chatWindow.style.width) { chatWindow.style.width = ""; chatWindow.style.height = ""; }
            } else if (!chatWindow.style.width) {
                restoreSize();
            }
        });
    }

    // ----- pop out into a standalone window --------------------------------
    if (popoutChat) {
        if (isPopout) {
            popoutChat.remove();  // already in the pop-out window
        } else {
            popoutChat.addEventListener("click", (e) => {
                e.stopPropagation();
                window.open("/chat/popout", "muscat-chat",
                    "width=380,height=560,resizable=yes,scrollbars=yes,noopener");
            });
        }
    }

    // ----- title flash on mention -----------------------------------------
    const stopTitleFlash = () => { if (flashTimer) { clearInterval(flashTimer); flashTimer = null; } document.title = origTitle; };
    const startTitleFlash = () => {
        if (flashTimer || (document.hasFocus() && !isMinimized)) return;
        let on = false;
        flashTimer = setInterval(() => { document.title = on ? origTitle : "💬 New mention"; on = !on; }, 1000);
    };
    window.addEventListener("focus", stopTitleFlash);

    // ----- message rendering ----------------------------------------------
    const scrollToBottom = () => { chatMessages.scrollTop = chatMessages.scrollHeight; };

    const buildReactions = (el, data) => {
        let bar = el.querySelector(".reactions");
        if (bar) bar.remove();
        bar = document.createElement("div");
        bar.className = "reactions";
        (data.reactions || []).forEach((r) => {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "reaction-chip";
            if (currentUser && (r.users || []).some((u) => u.toLowerCase() === currentUser.toLowerCase())) {
                chip.classList.add("mine");
            }
            chip.title = (r.users || []).join(", ");
            chip.textContent = r.emoji + " " + r.count;
            chip.addEventListener("click", (e) => { e.stopPropagation(); socket.emit("toggle_reaction", { id: data.id, emoji: r.emoji }); });
            bar.appendChild(chip);
        });
        el.appendChild(bar);
    };

    // ----- per-message kebab menu (react/edit/delete) ----------------------
    // A single static button per message, opened on click — no hover-reveal,
    // so the row never reflows as the cursor moves around the chat.
    let openMenu = null;
    let openMenuOutsideHandler = null;
    const closeMessageMenu = () => {
        if (openMenu) {
            openMenu.classList.add("hidden");
            const kebab = openMenu.parentElement && openMenu.parentElement.querySelector(".msg-kebab");
            if (kebab) kebab.classList.remove("open");
            openMenu = null;
        }
        if (openMenuOutsideHandler) {
            document.removeEventListener("click", openMenuOutsideHandler);
            openMenuOutsideHandler = null;
        }
    };
    const openMessageMenu = (kebabBtn, menu) => {
        menu.classList.remove("hidden");
        kebabBtn.classList.add("open");
        openMenu = menu;
        openMenuOutsideHandler = (ev) => {
            if (!menu.contains(ev.target) && ev.target !== kebabBtn) closeMessageMenu();
        };
        setTimeout(() => document.addEventListener("click", openMenuOutsideHandler), 0);
    };
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMessageMenu(); });

    const openEmojiPicker = (anchor, msgId) => {
        const existing = document.querySelector(".emoji-picker");
        if (existing) existing.remove();
        const picker = document.createElement("div");
        picker.className = "emoji-picker";
        PRESET_EMOJI.forEach((emoji) => {
            const b = document.createElement("button");
            b.type = "button";
            b.textContent = emoji;
            b.addEventListener("click", (e) => {
                e.stopPropagation();
                socket.emit("toggle_reaction", { id: msgId, emoji });
                picker.remove();
            });
            picker.appendChild(b);
        });
        anchor.appendChild(picker);
        setTimeout(() => {
            const close = (ev) => { if (!picker.contains(ev.target)) { picker.remove(); document.removeEventListener("click", close); } };
            document.addEventListener("click", close);
        }, 0);
    };

    // The element holding a message's editable text: the whole bubble for a
    // regular message, the span after "✳ who" for a /me line.
    const textElement = (el) => el.querySelector(".message-text");

    const beginEdit = (entry) => {
        if (!entry) return;
        const { el } = entry;
        const textEl = textElement(el);
        if (!textEl) return;
        const ta = document.createElement("textarea");
        ta.className = "edit-input";
        ta.value = entry.data.text;
        ta.rows = 2;
        textEl.replaceWith(ta);
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
        // Enter/Escape remove the textarea, and removing a focused element
        // fires blur in Chrome, which would run finish a second time.
        let finished = false;
        const finish = (commit) => {
            if (finished) return;
            finished = true;
            const newText = expandHere(ta.value).trim();
            // Read entry.data now, not at edit start: an edit/reaction event may
            // have refreshed it while the textarea was open.
            const current = entry.data.text;
            const restored = document.createElement(textEl.tagName);
            restored.className = textEl.className;
            renderText(restored, current);
            ta.replaceWith(restored);
            if (commit && newText && newText !== current) {
                socket.emit("edit_message", { id: entry.data.id, text: newText });
            }
        };
        ta.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); finish(true); }
            else if (e.key === "Escape") { e.preventDefault(); finish(false); }
        });
        ta.addEventListener("blur", () => finish(false));
    };

    // Per-message kebab (react/edit/delete). Edit/delete are gated to your own
    // messages via CSS + the .me class; the server enforces authorship too.
    const buildMessageActions = (data) => {
        const actions = document.createElement("div");
        actions.className = "message-actions";

        const kebab = document.createElement("button");
        kebab.type = "button"; kebab.className = "act msg-kebab";
        kebab.title = "More actions"; kebab.setAttribute("aria-label", "Message actions");
        kebab.textContent = "⋮";

        const menu = document.createElement("div");
        menu.className = "message-menu hidden";

        const menuItem = (label, cls, onClick) => {
            const b = document.createElement("button");
            b.type = "button"; b.className = cls; b.textContent = label;
            b.addEventListener("click", (e) => { e.stopPropagation(); closeMessageMenu(); onClick(); });
            return b;
        };
        menu.append(
            menuItem("☺ React", "react", () => openEmojiPicker(actions, data.id)),
            menuItem("✎ Edit", "edit", () => beginEdit(els.get(data.id))),
            menuItem("🗑 Delete", "delete", () => {
                const doDelete = () => socket.emit("delete_message", { id: data.id });
                if (window.showConfirmModal) {
                    window.showConfirmModal("Delete message", "Delete this message? This cannot be undone.",
                        { confirmLabel: "Delete", confirmClass: "btn primary" }).then((ok) => { if (ok) doDelete(); });
                } else {
                    doDelete();
                }
            }),
        );

        kebab.addEventListener("click", (e) => {
            e.stopPropagation();
            const wasOpen = openMenu === menu;
            closeMessageMenu();
            if (!wasOpen) openMessageMenu(kebab, menu);
        });

        actions.append(kebab, menu);
        return actions;
    };

    // Insert a message by timestamp rather than always at the end, so @bot
    // messages restored from this browser interleave with shared history that
    // arrives after them. Live messages are the newest, so they still append.
    const placeMessage = (el, ts) => {
        if (!Number.isFinite(ts)) { chatMessages.appendChild(el); return; }
        el.dataset.ts = String(ts);
        let prev = chatMessages.lastElementChild;
        while (prev && Number(prev.dataset.ts) > ts) prev = prev.previousElementSibling;
        chatMessages.insertBefore(el, prev ? prev.nextSibling : chatMessages.firstChild);
    };

    let activeRoom = "chat:public";
    const roomPresence = new Map();
    const roomSelect = document.createElement("select");
    roomSelect.id = "chat-room";
    roomSelect.setAttribute("aria-label", "Chat room");
    roomSelect.title = "Group rooms are visible only to their members, including admins";
    chatHeader.after(roomSelect);
    const filterRoom = () => {
        Array.from(chatMessages.children).forEach((el) => {
            el.hidden = !!el.dataset.room && el.dataset.room !== activeRoom;
        });
    };
    const updateRoom = () => {
        activeRoom = roomSelect.value;
        knownUsers = [];
        filterRoom();
        typingUsers.forEach(clearTimeout); typingUsers.clear(); renderTyping();
        const presence = roomPresence.get(activeRoom);
        onlineEl.textContent = presence ? presence.count + " online" : "";
        onlineEl.title = presence ? "Online: " + presence.users.join(", ") : "";
        const requestedRoom = activeRoom;
        fetch("/chat/users?room=" + encodeURIComponent(requestedRoom))
            .then(r => r.ok ? r.json() : null)
            .then(d => { if (d && roomSelect.value === requestedRoom) knownUsers = d.users || []; })
            .catch(() => {});
        try { localStorage.setItem("muscat-chat-room:" + getCurrentUser(), activeRoom); } catch (e) {}
    };
    roomSelect.addEventListener("change", updateRoom);
    socket.on("chat_rooms", (d) => {
        const rooms = d.rooms || [];
        let preferred = activeRoom;
        try { preferred = localStorage.getItem("muscat-chat-room:" + getCurrentUser()) || preferred; } catch (e) {}
        roomSelect.replaceChildren();
        rooms.forEach(room => {
            const option = document.createElement("option");
            option.value = room;
            option.textContent = room === "chat:public" ? "Team — public" : room.slice(6) + " — group + team";
            roomSelect.appendChild(option);
        });
        roomSelect.value = rooms.includes(preferred) ? preferred : (rooms[0] || "");
        // Reconnect may follow an access change: remove unauthorized cached rows.
        els.forEach((entry, id) => {
            if (!entry.data.visible_to && !rooms.includes(entry.data.room || "chat:public")) {
                entry.el.remove(); els.delete(id);
            }
        });
        updateRoom();
    });
    const renderMessage = (data) => {
        const previous = new Set(chatMessages.children);
        renderRoomMessage(data);
        Array.from(chatMessages.children).forEach(el => {
            if (!previous.has(el) && !data.private && !data.visible_to && !data.notice && data.kind !== "help") {
                el.dataset.room = data.room || "chat:public";
            }
        });
        filterRoom();
    };

    const renderRoomMessage = (data) => {
        if (chatEmpty && chatEmpty.parentNode) chatEmpty.remove();

        // System messages get a compact, centered treatment.
        if (data.kind === "system") {
            const sys = document.createElement("div");
            sys.className = "message system";
            if (data.notice) sys.classList.add("notice");
            const body = document.createElement("div");
            body.className = "message-body";
            renderText(body, data.text);
            const time = document.createElement("span");
            time.className = "time";
            time.textContent = formatTime(data.ts);
            body.appendChild(time);
            sys.appendChild(body);
            placeMessage(sys, data.ts);
            if (data.id != null) els.set(data.id, { el: sys, data });
            scrollToBottom();
            return;
        }

        // /me actions: a centered, attributed status line ("✳ Alice is …"),
        // editable/deletable by its author like any message.
        if (data.kind === "action") {
            const act = document.createElement("div");
            act.className = "message action " + (isMine(data) ? "me" : "them");
            if (currentUser && (data.mentions || []).some((n) => n.toLowerCase() === currentUser.toLowerCase())) {
                act.classList.add("mention-me");
            }
            const row = document.createElement("div");
            row.className = "message-row";
            const body = document.createElement("div");
            body.className = "message-body";
            const who = document.createElement("span");
            who.className = "user";
            who.textContent = isMine(data) ? "You" : (data.user || "Anonymous");
            const textEl = document.createElement("span");
            textEl.className = "message-text";
            renderText(textEl, data.text);
            const edited = document.createElement("span");
            edited.className = "edited";
            edited.textContent = " (edited)";
            if (!data.edited) edited.style.display = "none";
            const time = document.createElement("span");
            time.className = "time";
            time.textContent = formatTime(data.ts);
            body.append("✳ ", who, " ", textEl, edited, time);
            row.appendChild(body);
            if (data.id != null) row.appendChild(buildMessageActions(data));
            act.appendChild(row);
            placeMessage(act, data.ts);
            if (data.id != null) {
                els.set(data.id, { el: act, data });
                buildReactions(act, data);
            }
            scrollToBottom();
            return;
        }

        // @help: a local, private command reference rendered from a left-aligned
        // card (never sent to the server — see maybeRunLocalCommand).
        if (data.kind === "help") {
            const card = document.createElement("div");
            card.className = "message system help";
            const body = document.createElement("div");
            body.className = "message-body";
            renderText(body, data.text);
            card.appendChild(body);
            placeMessage(card, data.ts);
            scrollToBottom();
            return;
        }

        // Private @bot messages carry no server id; they are tracked (and
        // de-duplicated against the restored log) by their bot entry id.
        const botId = isBotMessage(data) ? botEntryId(data) : null;
        if (botId && botEls.has(botId)) return;

        const el = document.createElement("div");
        el.className = "message " + (isMine(data) ? "me" : "them");
        if (data.ephemeral) el.classList.add("ephemeral");
        if (data.kind === "agent") el.classList.add("agent");
        if (data.private) el.classList.add("private");
        if (data.mentions && currentUser &&
            data.mentions.some((n) => n.toLowerCase() === currentUser.toLowerCase())) {
            el.classList.add("mention-me");
        }
        if (data.id != null) el.dataset.id = data.id;

        const meta = document.createElement("div");
        meta.className = "message-meta";
        const user = document.createElement("span");
        user.className = "user";
        user.textContent = isMine(data) ? "You" : (data.user || "Anonymous");
        const time = document.createElement("span");
        time.className = "time";
        time.textContent = formatTime(data.ts);
        meta.append(user, time);
        if (data.ephemeral && data.kind !== "agent") {
            const tag = document.createElement("span");
            tag.className = "tag"; tag.textContent = "test";
            meta.appendChild(tag);
        }
        if (data.kind === "agent") {
            // Name-agnostic badge (the author span already shows the @name).
            const tag = document.createElement("span");
            tag.className = "tag agent-tag"; tag.textContent = "AI";
            meta.appendChild(tag);
        }
        if (data.private) {
            // Private @bot exchange: visible only to this user (server routes it
            // to their room and never persists it; this browser keeps a copy,
            // see "private @bot log" below).
            const tag = document.createElement("span");
            tag.className = "tag private-tag"; tag.textContent = "only you";
            meta.appendChild(tag);
        }
        const editedTag = document.createElement("span");
        editedTag.className = "edited";
        editedTag.textContent = "(edited)";
        if (!data.edited) editedTag.style.display = "none";
        meta.appendChild(editedTag);

        const row = document.createElement("div");
        row.className = "message-row";
        const body = document.createElement("div");
        body.className = "message-body message-text";
        if (data.kind === "agent") {
            body.classList.add("md");
            renderMarkdown(body, data.text);
        } else {
            renderText(body, data.text);
        }
        row.appendChild(body);

        if (!data.ephemeral && data.id != null) row.appendChild(buildMessageActions(data));
        if (botId) row.appendChild(buildBotDelete(botId));
        el.append(meta, row);
        placeMessage(el, data.ts);
        if (data.id != null) els.set(data.id, { el, data });
        if (botId) botEls.set(botId, { el, data });
        if (!data.ephemeral && data.id != null) buildReactions(el, data);
        scrollToBottom();
    };

    // Re-apply own/other styling once identity resolves (history may render first).
    const refreshOwnership = () => {
        [...els.values(), ...botEls.values()].forEach((entry) => {
            const { el, data } = entry;
            if (data.kind === "system") return;
            el.classList.toggle("me", isMine(data));
            el.classList.toggle("them", !isMine(data));
            const userSpan = el.querySelector(".message-meta .user, .message-body > .user");
            if (userSpan) userSpan.textContent = isMine(data) ? "You" : (data.user || "Anonymous");
        });
    };

    // ----- private @bot log (this browser only) ---------------------------
    // The server never stores the private @bot exchange (see chat.py), so the
    // asker's browser keeps it: one localStorage list per user, restored on load
    // and deletable message by message. Anonymous sessions keep nothing, since
    // their server-side @bot context is per-connection anyway. Every tab of the
    // user receives each message, so entries are keyed by server ts + kind.
    const botStoreKey = () => {
        return currentUser ? BOT_STORE_PREFIX + currentUser.toLowerCase() : null;
    };

    const loadBotLog = () => {
        const key = botStoreKey();
        const log = key && window.MuscatStorage ? window.MuscatStorage.getJSON(key, []) : [];
        return Array.isArray(log)
            ? log.filter((m) => m && typeof m.text === "string" && Number.isFinite(m.ts))
            : [];
    };

    const saveBotLog = (log) => {
        const key = botStoreKey();
        if (key && window.MuscatStorage) window.MuscatStorage.setJSON(key, log.slice(-BOT_STORE_MAX));
    };

    const rememberBotMessage = (data) => {
        if (!isBotMessage(data) || !botStoreKey()) return;
        const log = loadBotLog();
        const id = botEntryId(data);
        if (log.some((m) => botEntryId(m) === id)) return;
        saveBotLog([...log, {
            user: data.user, text: data.text, kind: data.kind, ts: data.ts, private: true,
        }]);
    };

    const forgetBotMessage = (id) => {
        const entry = botEls.get(id);
        if (entry) { entry.el.remove(); botEls.delete(id); }
        saveBotLog(loadBotLog().filter((m) => botEntryId(m) !== id));
    };

    const restoreBotLog = () => {
        loadBotLog().forEach((m) => { if (!botEls.has(botEntryId(m))) renderMessage(m); });
    };

    const buildBotDelete = (id) => {
        const actions = document.createElement("div");
        actions.className = "message-actions";
        const del = document.createElement("button");
        del.type = "button";
        del.className = "act bot-delete";
        del.textContent = "×";
        del.title = "Delete this message (only from your view)";
        del.setAttribute("aria-label", "Delete this message");
        del.addEventListener("click", () => forgetBotMessage(id));
        actions.appendChild(del);
        return actions;
    };

    // A delete in another tab of the same user removes the message here too.
    window.addEventListener("storage", (e) => {
        if (!e.key || e.key !== botStoreKey()) return;
        const kept = new Set(loadBotLog().map(botEntryId));
        [...botEls.keys()].forEach((id) => {
            if (!kept.has(id)) { botEls.get(id).el.remove(); botEls.delete(id); }
        });
    });

    // ----- special commands ------------------------------------------------
    // Reference shown by @help. Built at call time so the @name example reflects
    // the resolved user. Kept client-side (not the server) so it stays a
    // private, zero-traffic, always-available lookup.
    const helpText = () => {
        const example = getCurrentUser() !== "Anonymous" ? getCurrentUser() : "alice";
        return [
            "Chat commands",
            "  @here — insert a link to the page you're on",
            "  @name — notify a teammate (e.g. @" + example + ")",
            "  @everyone — ping everyone currently online (also @all, @channel)",
            "  @bot … — ask the codebase assistant, privately (only you see it)",
            "  @bot /reset — start a fresh @bot conversation",
            "  /me … — post a status line (e.g. /me observing TOI-1234 on muscat3)",
            "  /shrug … — add a ¯\\_(ツ)_/¯ to your message",
            "  @test … — send a message that isn't saved",
            "  /clear — hide messages from your view (local only)",
            "  @help — show this list",
            "Tip: TOI-1234 and TIC 12345678 auto-link to the target page.",
        ].join("\n");
    };

    // Handle purely client-side commands (never sent to the server). Returns
    // true if the input was consumed.
    const maybeRunLocalCommand = (raw) => {
        if (/^@help\b/i.test(raw)) {
            renderMessage({ kind: "help", text: helpText(), ts: Date.now() / 1000, private: true });
            return true;
        }
        if (/^\/clear\b/i.test(raw)) {
            clearedBefore = Date.now() / 1000;
            els.clear();
            chatMessages.innerHTML = "";
            renderMessage({
                kind: "system", ts: Date.now() / 1000,
                text: "Cleared your local view — reload the page to see the full history again.",
            });
            return true;
        }
        return false;
    };

    // Text substitutions applied to an outgoing message (still sent normally).
    const applyTextCommands = (text) => {
        const shrug = text.match(/^\/shrug\b\s*/i);
        if (shrug) return (text.slice(shrug[0].length) + " ¯\\_(ツ)_/¯").trim();
        return text;
    };

    // ----- sending ---------------------------------------------------------
    const send = () => {
        const raw = messageInput.value.trim();
        if (!raw) return;
        if (maybeRunLocalCommand(raw)) {
            messageInput.value = "";
            stopTyping();
            hideAutocomplete();
            messageInput.focus();
            return;
        }
        const text = applyTextCommands(expandHere(messageInput.value).trim());
        if (!text) return;
        socket.emit("message", { user: getCurrentUser(), text, room: activeRoom });
        messageInput.value = "";
        stopTyping();
        hideAutocomplete();
        messageInput.focus();
    };
    sendButton.addEventListener("click", send);

    messageInput.addEventListener("keydown", (e) => {
        if (!autocompleteEl.classList.contains("hidden")) {
            if (e.key === "ArrowDown") { e.preventDefault(); moveAc(1); return; }
            if (e.key === "ArrowUp") { e.preventDefault(); moveAc(-1); return; }
            if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); acceptAc(); return; }
            if (e.key === "Escape") { e.preventDefault(); hideAutocomplete(); return; }
        }
        if (e.key === "Enter") { e.preventDefault(); send(); }
    });

    messageInput.addEventListener("input", () => {
        // Live-expand "@here " so the sender previews the shared URL.
        const v = messageInput.value;
        if (/@here(?=\s)/i.test(v)) {
            const expanded = v.replace(/@here(?=\s)/gi, window.location.href);
            messageInput.value = expanded;
            messageInput.setSelectionRange(expanded.length, expanded.length);
        }
        sendTyping();
        updateAutocomplete();
    });

    // ----- @mention autocomplete ------------------------------------------
    const mentionToken = () => {
        const upto = messageInput.value.slice(0, messageInput.selectionStart);
        const m = upto.match(/@([A-Za-z0-9._-]*)$/);
        return m ? m[1] : null;
    };
    const hideAutocomplete = () => { autocompleteEl.classList.add("hidden"); autocompleteEl.innerHTML = ""; acItems = []; acIndex = -1; };
    const renderAutocomplete = () => {
        autocompleteEl.innerHTML = "";
        acItems.forEach((name, i) => {
            const item = document.createElement("div");
            item.className = "ac-item" + (i === acIndex ? " active" : "");
            item.textContent = name;
            item.addEventListener("mousedown", (e) => { e.preventDefault(); acIndex = i; acceptAc(); });
            autocompleteEl.appendChild(item);
        });
        autocompleteEl.classList.toggle("hidden", acItems.length === 0);
    };
    const updateAutocomplete = () => {
        const token = mentionToken();
        if (token === null) { hideAutocomplete(); return; }
        const low = token.toLowerCase();
        acItems = knownUsers.filter((u) => u.toLowerCase().startsWith(low)).slice(0, 6);
        acIndex = acItems.length ? 0 : -1;
        renderAutocomplete();
    };
    const moveAc = (delta) => {
        if (!acItems.length) return;
        acIndex = (acIndex + delta + acItems.length) % acItems.length;
        renderAutocomplete();
    };
    const acceptAc = () => {
        if (acIndex < 0 || acIndex >= acItems.length) { hideAutocomplete(); return; }
        const name = acItems[acIndex];
        const start = messageInput.selectionStart;
        const before = messageInput.value.slice(0, start).replace(/@([A-Za-z0-9._-]*)$/, "@" + name + " ");
        const after = messageInput.value.slice(start);
        messageInput.value = before + after;
        const caret = before.length;
        messageInput.setSelectionRange(caret, caret);
        hideAutocomplete();
    };

    // ----- typing indicator ------------------------------------------------
    const sendTyping = () => {
        if (!typingSent) { socket.emit("typing", { typing: true, room: activeRoom }); typingSent = true; }
        clearTimeout(typingStopTimer);
        typingStopTimer = setTimeout(stopTyping, 2500);
    };
    function stopTyping() {
        clearTimeout(typingStopTimer);
        if (typingSent) { socket.emit("typing", { typing: false, room: activeRoom }); typingSent = false; }
    }
    const renderTyping = () => {
        const names = Array.from(typingUsers.keys());
        if (!names.length) { typingEl.textContent = ""; return; }
        typingEl.textContent = names.length === 1
            ? names[0] + " is typing…"
            : names.slice(0, 2).join(", ") + (names.length > 2 ? " and others" : "") + " are typing…";
    };

    // ----- socket events ---------------------------------------------------
    // Apply a server-side edit to an already-rendered message.
    const applyEdit = (entry, data) => {
        entry.data = { ...entry.data, text: data.text, edited: true, mentions: data.mentions || entry.data.mentions };
        const textEl = textElement(entry.el);  // absent while its edit box is open
        if (textEl) { textEl.textContent = ""; renderText(textEl, data.text); }
        const edited = entry.el.querySelector(".edited");
        if (edited) edited.style.display = "";
        if (currentUser && (entry.data.kind === "user" || entry.data.kind === "action")) {
            entry.el.classList.toggle("mention-me",
                (entry.data.mentions || []).some((n) => n.toLowerCase() === currentUser.toLowerCase()));
        }
    };

    // The server sends history on every (re)connect -- after a server restart
    // or network drop too -- so reconcile it by id instead of appending: keep
    // what is shown, catch up on edits/reactions/deletes made while offline,
    // and render only messages that are new. A local /clear hides everything
    // up to that moment, so history from before it stays hidden.
    let clearedBefore = 0;
    const syncHistory = (messages, window) => {
        const incoming = messages.filter((m) => m && typeof m.text === "string" && !(m.ts <= clearedBefore));
        const ids = new Set(incoming.map((m) => m.id));
        if (window && Number.isFinite(window.since) && Number.isFinite(window.until)) {
            els.forEach((entry, id) => {
                const ts = entry.data.ts;
                // Preserve messages outside the queried snapshot, including
                // newer live arrivals and rows omitted by the history limit.
                const inWindow = ts >= window.since && ts <= window.until &&
                    !(ts === window.since && window.first_id != null && id < window.first_id);
                if (!ids.has(id) && inWindow) {
                    entry.el.remove();
                    els.delete(id);
                }
            });
        }
        incoming.forEach((m) => {
            const entry = els.get(m.id);
            if (!entry) { renderMessage(m); return; }
            if (m.text !== entry.data.text) applyEdit(entry, m);
            if (entry.data.kind === "user" || entry.data.kind === "action") {
                entry.data = { ...entry.data, reactions: m.reactions || [] };
                buildReactions(entry.el, entry.data);
            }
        });
        scrollToBottom();
    };

    socket.on("history", (d) => syncHistory(d && Array.isArray(d.messages) ? d.messages : [], d && d.window));
    socket.on("message", (data) => {
        if (!data || typeof data.text !== "string") return;
        rememberBotMessage(data);
        renderMessage(data);
        bumpUnread();
    });
    socket.on("message_edited", (data) => {
        const entry = els.get(data.id);
        if (entry) applyEdit(entry, data);
    });
    socket.on("message_deleted", (data) => {
        const entry = els.get(data.id);
        if (entry) { entry.el.remove(); els.delete(data.id); }
    });
    socket.on("reaction_updated", (data) => {
        const entry = els.get(data.id);
        if (!entry) return;
        entry.data = { ...entry.data, reactions: data.reactions };
        buildReactions(entry.el, entry.data);
    });
    socket.on("mention", () => { bumpUnread(); startTitleFlash(); });
    // Server-side refusals (save failed, not your message, shared chat not
    // available, ...) would otherwise just make the message vanish.
    const showNotice = (text) => {
        renderMessage({ kind: "system", notice: true, ts: Date.now() / 1000, text: "⚠️ " + text });
    };
    socket.on("chat_error", (d) => { if (d && d.error) showNotice(d.error); });

    socket.on("typing", (d) => {
        if (!d || !d.user || (d.room && d.room !== activeRoom)) return;
        if (currentUser && d.user.toLowerCase() === currentUser.toLowerCase()) return;
        if (d.typing) {
            clearTimeout(typingUsers.get(d.user));
            typingUsers.set(d.user, setTimeout(() => { typingUsers.delete(d.user); renderTyping(); }, 4000));
        } else {
            clearTimeout(typingUsers.get(d.user));
            typingUsers.delete(d.user);
        }
        renderTyping();
    });

    const setConnected = (connected) => {
        statusDot.classList.toggle("online", connected);
        statusDot.title = connected ? "Connected" : "Disconnected";
        if (!connected) onlineEl.textContent = "offline";
    };
    socket.on("connect", () => setConnected(true));
    socket.on("disconnect", () => setConnected(false));
    // A connection the server refuses (account not provisioned, auth required)
    // never fires connect/disconnect; say why instead of a silent blank status.
    let lastConnectError = "";
    socket.on("connect_error", (err) => {
        setConnected(false);
        const reason = (err && err.message) || "connection failed";
        onlineEl.textContent = "unavailable";
        statusDot.title = "Chat unavailable: " + reason;
        // Transport errors (e.g. a server restart) are retried automatically
        // and only shown in the status; a server refusal is final
        // (socket.active is false), so announce it once in the chat.
        if (!socket.active && reason !== lastConnectError) {
            lastConnectError = reason;
            showNotice("Chat unavailable: " + reason);
        }
    });
    socket.on("connect", () => { lastConnectError = ""; });
    socket.on("presence", (d) => {
        roomPresence.set(d.room || "chat:public", d);
        if ((d.room || "chat:public") !== activeRoom) return;
        const users = (d && Array.isArray(d.users)) ? d.users : [];
        const n = (d && typeof d.count === "number") ? d.count : users.length;
        onlineEl.textContent = n + " online";
        onlineEl.title = users.length ? "Online: " + users.join(", ") : "";
    });

    // Restore prior minimize state (UI only); the pop-out window always starts expanded.
    let storedMin = "0";
    if (!isPopout) {
        try { storedMin = localStorage.getItem("muscat-chat-min") || "0"; } catch (e) {}
    }
    setMinimized(storedMin === "1");

    // The page may already know the user (data-current-user); otherwise the
    // log is restored once /whoami resolves (onUserResolved).
    restoreBotLog();
});
