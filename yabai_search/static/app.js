// Front-end script for Yabai Search. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Native tooltip for the frequency rank.
const FREQ_TOOLTIP =
    "Frequency under 20,000 is common. This number is not reliable past 100,000.";

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const clearButton = document.getElementById("clear-button");
const results = document.getElementById("results");
const footerSeparator = document.querySelector(".footer-separator");
// MOE glyph masking (duplicated from glyphs.js so it is always available).
const MOE_GLYPH_RE_APP = /&([A-Za-z0-9][A-Za-z0-9._-]*\.(?:gif|png));/g;
const _glyphNamesApp = new Map();
const _glyphCharsApp = new Map();
function maskGlyphs(text) {
    return text.replace(MOE_GLYPH_RE_APP, (_whole, name) => {
        let char = _glyphCharsApp.get(name);
        if (char === undefined) {
            char = String.fromCodePoint(0xe000 + _glyphCharsApp.size);
            _glyphCharsApp.set(name, char);
            _glyphNamesApp.set(char, name);
        }
        return char;
    });
}

const pagination = document.getElementById("pagination");
const paginationTop = document.getElementById("pagination-top");
const parsed = document.getElementById("parsed");
const hint = document.getElementById("hint");
// Data fingerprint + server start time (from index.html). Sent as ?dv= so the
// server may allow /api/search responses to be cached; without it they stay
// uncached. Empty on pages that do not render this meta tag.
const DATA_VERSION = document.querySelector('meta[name="data-version"]')?.content || "";
const scriptToggle = document.getElementById("script-toggle");
const toneToggle = document.getElementById("tone-toggle");
const phoneticToggle = document.getElementById("phonetic-toggle");
// Quick display controls in the Search options panel.
const quickSquares = [...document.querySelectorAll(".quick-script .quick-square")];
const themeSquares = [...document.querySelectorAll("[data-theme]")];
const menuToggle = document.querySelector(".menu-toggle");
const menuDropdown = document.querySelector(".menu-dropdown");
const quickTone = document.getElementById("quick-tone");
const quickPinyin = document.querySelector(".quick-pinyin");
const quickZhuyin = document.querySelector(".quick-zhuyin");
const quickChina = document.querySelector('[data-region="china"]');
const quickTaiwanSquare = document.querySelector('[data-region="taiwan"]');
// Descriptions to the right of the controls.
const scriptDescription = document.getElementById("script-description");
const toneDescription = document.getElementById("tone-description");
const phoneticDescription = document.getElementById("phonetic-description");
const dialectDescription = document.getElementById("dialect-description");
const taiwanToggle = document.getElementById("taiwan-toggle");
const customDictsToggle = document.getElementById("custom-dicts-toggle");
const customDictsButton = document.getElementById("custom-dicts-button");
// Dictionaries panel: one switch per dictionary.
const dictToggles = [...document.querySelectorAll(".dict-toggle")];
// Anchor -> language group, from the Dictionaries panel markup.
const anchorLang = new Map(dictToggles.map((toggle) => [toggle.dataset.anchor, groupFor(toggle)]));
// The whole "Dictionaries" disclosure, shown only when "Custom dictionaries" is on.
const dictPanel = document.querySelector(".options.dictionaries");

// Search-language dropdown (left of the search box): EN / ZH / TW.
const langSelect = document.querySelector(".lang-select");
const langButton = langSelect.querySelector(".lang-button");
const langMenu = langSelect.querySelector(".lang-menu");
// Default dictionaries per language (from default_dictionaries.py).
const langDefaults = JSON.parse(langSelect.dataset.defaults || "{}");

// Compact mode shows one definition per language, chosen by the user's
// dictionary order. Detailed mode shows every selected dictionary.
const DICT_ORDER_KEY = "yabai-search-dict-order";
let dictOrder = {};

function loadDictOrder() {
    try {
        const saved = JSON.parse(localStorage.getItem(DICT_ORDER_KEY) || "{}");
        dictOrder = saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
    } catch (error) {
        dictOrder = {};
    }
}

function saveDictOrder() {
    try {
        localStorage.setItem(DICT_ORDER_KEY, JSON.stringify(dictOrder));
    } catch (error) {
        // localStorage unavailable - ordering is session-only.
    }
}

// Display options. script: "simplified" | "traditional"; toneColoring: on/off;
// phonetic: "pinyin" | "zhuyin"; taiwan: use the Taiwan reading when listed;
// customDicts: show the Dictionaries picker; dicts: the selected dictionaries;
// compact: one definition per language, by dictionary order; lang: the selected
// search language (EN / ZH / TW / JP).
const state = {
    script: "simplified",
    toneColoring: true,
    phonetic: "pinyin",
    taiwan: false,
    customDicts: false,
    dicts: new Set(), // selected dictionary anchors
    compact: false,
    lang: "EN",
    autoClipboard: false,
};

// True when this page was opened with display settings in the URL. Developer
// links may do this; the values are shown for this page but never overwrite the
// visitor's own localStorage preferences. Normal URLs never get these params.
let settingsFromUrl = false;

// /word/<word> renders the normal search page but asks the API for the exact
// entries of that word instead of a prefix/contains search.
let exactMode = window.__exactSearch === true;

// The most recent results, kept so the toggles can re-render without re-fetching.
let currentEntries = [];
// Both display layouts when the server merged the page (simplified and
// traditional). Null when the browser still has to merge, e.g. custom
// dictionaries are involved or the server merge is off.
let currentLayouts = null;
// Server-reported durations: the initial batch and the whole query. Both are
// shown after the result count when the first batch was rendered on its own.
let lastSearchMs = null;
let firstSearchMs = null;
// Request lag (client send to server receive), the server-side time for the
// first tier, and the server-side time for the complete query. These are
// separate measurements, not cumulative milestones.
let lagMs = null;
let firstDbMs = null;
let allDbMs = null;
// The two paint milestones, measured from the start of the search. `first` is
// the fast CEDICT tier, `all` is the complete response.
let firstPaintMs = null;
let allPaintMs = null;
let currentFuzzy = [];
let currentQuery = "";
// The parsed words from the last multi-word input, so a display toggle (e.g.
// Zhuyin) can re-render them without re-annotating.
let currentTokens = [];

// --- Copyable debug log ------------------------------------------------
// A small in-memory log for the "Download debug log" menu item. It collects
// search milestones and every console.error (including the stack), so a bug
// report can show the current state and the traceback that led to it.
const DEBUG_EVENT_LIMIT = 200;
const debugEvents = [];
let currentDebugSearch = null;
// Time of the last keystroke, used to report the debounce gap in the log.
let lastInputAt = 0;
// Counts for the "*" pager's shortcut to the imported-dictionary results.
let lastWildcardCustom = null;

function debugErrorText(error) {
    if (!error) return "";
    if (error.stack) return String(error.stack);
    if (error.message) return `${error.name || "Error"}: ${error.message}`;
    return String(error);
}

// Keep debug events small: a pasted paragraph would otherwise be copied into
// every event. The full query is still used for the actual search.
function debugQuery(text) {
    const value = String(text);
    return value.length > 80 ? `${value.slice(0, 80)}… (${value.length} chars)` : value;
}

// Record one event. While a search is active it is also attached to that
// search, which is what "traceback from the current search" reports.
function recordDebug(level, message, error = null, details = null) {
    const entry = {
        at: new Date().toISOString(),
        level,
        message: String(message),
    };
    if (error) entry.stack = debugErrorText(error);
    if (details && typeof details === "object") Object.assign(entry, details);
    if (currentDebugSearch) {
        entry.query = currentDebugSearch.query;
        entry.seq = currentDebugSearch.seq;
        entry.offset = currentDebugSearch.offset;
        if (typeof currentDebugSearch.startedAtPerf === "number") {
            entry.sinceSearchStartMs =
                Math.round((performance.now() - currentDebugSearch.startedAtPerf) * 10) / 10;
        }
        currentDebugSearch.events.push(entry);
        if (currentDebugSearch.events.length > DEBUG_EVENT_LIMIT) {
            currentDebugSearch.events.shift();
        }
    }
    debugEvents.push(entry);
    if (debugEvents.length > DEBUG_EVENT_LIMIT) debugEvents.shift();
}

// Record a client-side phase duration (debug only).
function recordPhase(name, start, details = null) {
    const ms = Math.round((performance.now() - start) * 10) / 10;
    recordDebug("phase", name, null, { ms, ...(details || {}) });
}

// Log the server's debug headers for one response (debug only).
function recordServerDebug(label, response) {
    const timing = response.headers.get("server-timing");
    const counters = response.headers.get("x-yabai-debug");
    if (timing) recordDebug("network", `${label} Server-Timing`, null, { serverTiming: timing });
    if (counters) recordDebug("network", `${label} server counters`, null, { serverCounters: counters });
    // Cloudflare's edge status explains stale Server-Timing values on a HIT and
    // whether the request reached an NRT or a US edge.
    const cf = response.headers.get("cf-cache-status");
    const ray = response.headers.get("cf-ray");
    if (cf || ray) {
        recordDebug("network", `${label} edge`, null, { cfCacheStatus: cf, cfRay: ray });
    }
}

// Log TTFB, download and transfer size from the browser's resource timing.
function recordNetworkTiming(label, url) {
    try {
        const absolute = new URL(url, location.href).href;
        const entries = performance.getEntriesByName(absolute);
        const entry = entries[entries.length - 1];
        if (!entry) return;
        const serverTiming = Array.isArray(entry.serverTiming)
            ? entry.serverTiming
                .map((item) => `${item.name}${item.duration == null ? "" : `=${item.duration}`}`)
                .join(", ")
            : "";
        recordDebug("network", label, null, {
            ttfbMs: Math.round(entry.responseStart - entry.startTime),
            downloadMs: Math.round(entry.responseEnd - entry.responseStart),
            durationMs: Math.round(entry.duration),
            transferSize: entry.transferSize,
            encodedBodySize: entry.encodedBodySize,
            cacheHit: entry.transferSize === 0,
            resourceServerTiming: serverTiming,
            nextHopProtocol: entry.nextHopProtocol,
            domainLookupMs:
                entry.domainLookupStart && entry.domainLookupEnd
                    ? Math.round(entry.domainLookupEnd - entry.domainLookupStart)
                    : null,
            connectMs:
                entry.connectStart && entry.connectEnd
                    ? Math.round(entry.connectEnd - entry.connectStart)
                    : null,
            tlsMs: entry.secureConnectionStart
                ? Math.round(entry.connectEnd - entry.secureConnectionStart)
                : 0,
            requestStartMs: entry.requestStart
                ? Math.round(entry.requestStart - entry.startTime)
                : null,
            blockedBeforeSendMs: entry.requestStart
                ? Math.round(entry.requestStart - entry.startTime)
                : null,
        });
    } catch (error) {
        // Resource timing is best-effort debug data.
    }
}

// Capture all existing console.error calls (the custom dictionary code logs
// failures that way) without changing what the browser console shows.
const nativeConsoleError = console.error.bind(console);
console.error = (...args) => {
    const error = args.find((arg) => arg instanceof Error) || null;
    const message = args
        .map((arg) =>
            arg instanceof Error
                ? `${arg.name || "Error"}: ${arg.message || String(arg)}`
                : String(arg)
        )
        .join(" ");
    recordDebug("error", message, error);
    nativeConsoleError(...args);
};

// Uncaught errors and rejected promises also get a stack in the log.
window.addEventListener("error", (event) => {
    recordDebug("error", event.message || "Window error", event.error || null);
});
window.addEventListener("unhandledrejection", (event) => {
    recordDebug("error", "Unhandled promise rejection", event.reason || null);
});

// Long tasks show when the main thread was blocked (debug only).
if ("PerformanceObserver" in window) {
    try {
        const supported = PerformanceObserver.supportedEntryTypes || [];
        if (supported.includes("longtask")) {
            const longTaskObserver = new PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                    recordDebug("longtask", "Long task", null, {
                        longTaskMs: Math.round(entry.duration),
                    });
                }
            });
            longTaskObserver.observe({ type: "longtask", buffered: true });
        }
    } catch (error) {
        // Long-task timing is optional debug data.
    }
}

function beginSearchDebug(query, offset, seq) {
    const now = performance.now();
    currentDebugSearch = {
        query: debugQuery(query),
        offset,
        seq,
        startedAt: new Date().toISOString(),
        startedAtPerf: now,
        queryLength: String(query).length,
        sinceLastInputMs: lastInputAt ? Math.round(now - lastInputAt) : null,
        previousSeq: Math.max(0, seq - 1),
        previousSearchMs: lastSearchMs,
        official: null,
        custom: null,
        events: [],
    };
    recordDebug("search", "Search started");
}

// Do not call this customDictionaryDebugInfo: a top-level function declaration
// becomes a window property and would overwrite (and then recurse into) the
// function that custom_dicts.js installs under that name.
function readCustomDictionaryDebugInfo() {
    try {
        return window.customDictionaryDebugInfo
            ? window.customDictionaryDebugInfo()
            : null;
    } catch (error) {
        return { error: debugErrorText(error) };
    }
}

function debugSnapshot(serverSearchLog = null) {
    const currentState = {
        script: state.script,
        toneColoring: state.toneColoring,
        phonetic: state.phonetic,
        taiwan: state.taiwan,
        customDicts: state.customDicts,
        selectedDictionaries: selectedAnchorsParam().split(",").filter(Boolean),
        compact: state.compact,
        lang: state.lang,
        currentQuery,
        renderedResults: currentEntries.length,
        fuzzyResults: currentFuzzy.length,
        lastSearchMs,
        firstSearchMs,
        activeSearchSeq: searchSeq,
    };
    const currentSearch = currentDebugSearch
        ? {
            query: currentDebugSearch.query,
            offset: currentDebugSearch.offset,
            seq: currentDebugSearch.seq,
            queryLength: currentDebugSearch.queryLength,
            sinceLastInputMs: currentDebugSearch.sinceLastInputMs,
            previousSeq: currentDebugSearch.previousSeq,
            previousSearchMs: currentDebugSearch.previousSearchMs,
            startedAt: currentDebugSearch.startedAt,
            official: currentDebugSearch.official,
            custom: currentDebugSearch.custom,
            events: currentDebugSearch.events,
        }
        : null;
    const info = {
        generatedAt: new Date().toISOString(),
        url: location.href,
        userAgent: navigator.userAgent,
        environment: {
            hardwareConcurrency: navigator.hardwareConcurrency ?? null,
            deviceMemory: navigator.deviceMemory ?? null,
        },
        currentState,
        customDictionaries: readCustomDictionaryDebugInfo(),
        customDictionaryStats: window.customDictsStats || null,
        currentSearch,
        recentEvents: debugEvents.slice(-50),
        serverSearchLog: serverSearchLog,
    };
    return "Yabai Search debug log\n" + JSON.stringify(info, null, 2) + "\n";
}

// Save the debug snapshot as a .txt file so it can be attached to a report or
// opened in an editor. The menu item shows brief "Downloaded" feedback.
async function downloadDebugLog() {
    let serverSearchLog = null;
    try {
        const response = await fetch("/api/search-log");
        if (response.ok) serverSearchLog = await response.json();
    } catch (error) {
        // The server log is best-effort debug data.
    }
    const text = debugSnapshot(serverSearchLog);
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `yabai-debug-${stamp}.txt`;
    link.style.display = "none";
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    const button = document.getElementById("download-log-button");
    const label = button ? button.querySelector(".menu-item-label") : null;
    if (!label) return;
    label.textContent = "Downloaded";
    window.setTimeout(() => {
        label.textContent = "Debug Log";
    }, 1500);
}

const downloadLogButton = document.getElementById("download-log-button");
if (downloadLogButton) downloadLogButton.addEventListener("click", downloadDebugLog);

// Auto Clipboard: search the clipboard text as soon as it changes. Browsers
// have no reliable "clipboard changed" event, so this uses every trigger there
// is: a fast timer that runs off the main thread (so background tabs are not
// throttled), the clipboardchange event where it exists, and any sign of life
// from the page (focus, visibility, pointer, key, touch). Each one just asks
// "did the clipboard change?", and duplicates are dropped.
//
// Reading the clipboard needs a secure context and permission. Chromium also
// needs the page focused, so unfocused reads only work where the browser
// allows them (Firefox with dom.events.testing.asyncClipboard). Turning the
// switch on reads the clipboard once from the tap itself: that asks for
// permission and records the current text, so old text is not searched.
const autoClipboardToggle = document.getElementById("auto-clipboard-toggle");
const AUTO_CLIPBOARD_MAX_LENGTH = 200;
const CLIPBOARD_TICK_MS = 200; // timer period
const CLIPBOARD_EVENT_GAP_MS = 80; // minimum gap between event-driven reads
const CLIPBOARD_SLOW_READ_MS = 1500; // a read this slow was waiting on a prompt
let lastClipboardText = "";
let clipboardReadBusy = false;
let clipboardLastRead = 0;
let clipboardSuppressUntil = 0;
let clipboardNeedsPrompt = false;
let clipboardWorker = null;
let clipboardWorkerUrl = "";
let clipboardTimer = 0;
let clipboardLastDone = 0;

// Phones and tablets: Android Chrome shows a "Paste" bubble for every read, so
// polling or reading on every tap would spam it (and tapping it for an
// unchanged clipboard looked like it did nothing). On touch devices the watch
// only reads when the page comes back to the foreground, at most once per
// cooldown, and a read the user approved with the bubble is always searched.
// Text copied on this page is taken from the selection with no prompt at all.
const clipboardIsTouch =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(pointer: coarse)").matches;
const CLIPBOARD_TOUCH_COOLDOWN_MS = 3000;

async function readClipboardText() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
        throw new Error("Clipboard reading is not available");
    }
    return ((await navigator.clipboard.readText()) || "").trim();
}

// Call just before the page puts text on the clipboard for its own copy
// buttons, so a copied definition is not searched. Selecting text on the page
// and copying it is not suppressed: that is searched like any other copy.
function ignoreNextClipboardChange() {
    clipboardSuppressUntil = performance.now() + 1500;
}

// Search clipboard text if it is worth searching.
function searchClipboardValue(value) {
    if (!value || value.length > AUTO_CLIPBOARD_MAX_LENGTH) return;
    if (value === input.value.trim()) return;
    performSearch(value);
}

async function checkClipboard(fromEvent = false) {
    if (!state.autoClipboard || clipboardReadBusy) return;
    const now = performance.now();
    if (fromEvent && now - clipboardLastRead < CLIPBOARD_EVENT_GAP_MS) return;
    // Touch: the paste bubble closing refocuses the page, which would trigger
    // another read and another bubble. Wait out a cooldown after each read.
    if (clipboardIsTouch && now - clipboardLastDone < CLIPBOARD_TOUCH_COOLDOWN_MS) return;
    clipboardLastRead = now;
    clipboardReadBusy = true;
    try {
        const value = await readClipboardText();
        // A read that waited on a paste prompt means the browser needs a tap
        // for every read (Firefox without the testing pref). Stop reading on
        // every timer tick and pointer move, so the prompt is not spammed;
        // focus, visibility and copy still check. A later fast read clears
        // this again, so one slow read cannot switch the timer off for good.
        clipboardNeedsPrompt = performance.now() - now > CLIPBOARD_SLOW_READ_MS;
        // Desktop skips text it has already seen. On touch the user just
        // approved this read with the paste bubble, so an unchanged clipboard
        // is still searched (unless it is already what is in the box).
        if (!value || (!clipboardIsTouch && value === lastClipboardText)) return;
        lastClipboardText = value;
        if (performance.now() < clipboardSuppressUntil) return;
        searchClipboardValue(value);
    } catch (error) {
        // Not focused, permission denied or clipboard unavailable: try again
        // on the next trigger.
    } finally {
        clipboardReadBusy = false;
        clipboardLastDone = performance.now();
    }
}

// Triggers that fire constantly (timer, pointer move) are skipped once the
// browser has shown it needs a tap per read.
function pollClipboard() {
    if (clipboardNeedsPrompt) return;
    checkClipboard(true);
}

function startClipboardWatch() {
    stopClipboardWatch();
    clipboardNeedsPrompt = false;
    // Touch devices have no timer: it would raise the paste bubble nonstop.
    if (clipboardIsTouch) return;
    // A worker's timers are not throttled like a background tab's are.
    try {
        const code = `setInterval(() => postMessage(0), ${CLIPBOARD_TICK_MS});`;
        clipboardWorkerUrl = URL.createObjectURL(
            new Blob([code], { type: "text/javascript" })
        );
        clipboardWorker = new Worker(clipboardWorkerUrl);
        clipboardWorker.onmessage = pollClipboard;
        clipboardWorker.onerror = () => {
            // Workers blocked: fall back to a plain timer.
            stopClipboardWatch();
            clipboardTimer = window.setInterval(pollClipboard, CLIPBOARD_TICK_MS);
        };
    } catch (error) {
        clipboardTimer = window.setInterval(pollClipboard, CLIPBOARD_TICK_MS);
    }
}

function stopClipboardWatch() {
    if (clipboardWorker) clipboardWorker.terminate();
    if (clipboardWorkerUrl) URL.revokeObjectURL(clipboardWorkerUrl);
    if (clipboardTimer) window.clearInterval(clipboardTimer);
    clipboardWorker = null;
    clipboardWorkerUrl = "";
    clipboardTimer = 0;
}

// Run the timer only while the switch is on. loadPrefs() calls this too, once
// the saved setting is known.
function syncClipboardWatch() {
    if (state.autoClipboard) startClipboardWatch();
    else stopClipboardWatch();
}

if (autoClipboardToggle) {
    autoClipboardToggle.checked = state.autoClipboard;
    syncClipboardWatch();
    autoClipboardToggle.addEventListener("change", async () => {
        if (autoClipboardToggle.checked) {
            try {
                lastClipboardText = await readClipboardText();
                // Turning the switch on searches the clipboard right away,
                // on every device, so the first paste does not need a change.
                searchClipboardValue(lastClipboardText);
                clipboardLastDone = performance.now();
            } catch (error) {
                autoClipboardToggle.checked = false;
                state.autoClipboard = false;
                savePrefs();
                syncClipboardWatch();
                const label = autoClipboardToggle
                    .closest(".menu-item")
                    .querySelector(".menu-item-label");
                if (label) {
                    label.textContent = "Clipboard blocked";
                    window.setTimeout(() => {
                        label.textContent = "Auto Clipboard";
                    }, 2500);
                }
                return;
            }
        }
        state.autoClipboard = autoClipboardToggle.checked;
        savePrefs();
        syncClipboardWatch();
    });

    // Always check on these, even after a prompt-per-read browser is detected.
    const checkNow = () => checkClipboard(false);
    window.addEventListener("focus", checkNow);
    window.addEventListener("pageshow", checkNow);
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") checkNow();
    });
    if (clipboardIsTouch) {
        // Copying on this page: read the selection directly, no paste bubble.
        for (const type of ["copy", "cut"]) {
            document.addEventListener(
                type,
                () => {
                    if (!state.autoClipboard) return;
                    const text = (window.getSelection()?.toString() || "").trim();
                    if (!text) return;
                    lastClipboardText = text;
                    searchClipboardValue(text);
                },
                true
            );
        }
    } else {
        document.addEventListener("focusin", checkNow);
        // Any sign of life from the page. Passive and rate-limited by the gap.
        for (const type of [
            "pointerdown", "pointermove", "mouseover", "keydown", "keyup",
            "touchstart", "wheel", "scroll", "resize", "online",
        ]) {
            window.addEventListener(type, pollClipboard, { passive: true, capture: true });
        }
        // Copying on this page: the clipboard updates just after the event, so
        // check a few times right away instead of waiting for the next tick.
        for (const type of ["copy", "cut"]) {
            document.addEventListener(
                type,
                () => {
                    for (const delay of [0, 40, 120, 300]) {
                        window.setTimeout(checkNow, delay);
                    }
                },
                true
            );
        }
    }
    // Where the browser has a real clipboard-change event, use it.
    try {
        if (navigator.clipboard && navigator.clipboard.addEventListener) {
            navigator.clipboard.addEventListener("clipboardchange", checkNow);
        }
    } catch (error) {
        // Not supported.
    }
}

// Tone-marked forms for each base vowel (index 0 = tone 1 ... 3 = tone 4).
const TONE_MARKS = {
    a: "āáǎà",
    e: "ēéěè",
    i: "īíǐì",
    o: "ōóǒò",
    u: "ūúǔù",
    ü: "ǖǘǚǜ",
    A: "ĀÁǍÀ",
    E: "ĒÉĚÈ",
    I: "ĪÍǏÌ",
    O: "ŌÓǑÒ",
    U: "ŪÚǓÙ",
    Ü: "ǕǗǙǛ",
};

// Reverse map: a tone-marked character -> its tone number (1-4).
const TONE_BY_MARK = {};
for (const marks of Object.values(TONE_MARKS)) {
    [...marks].forEach((char, i) => {
        TONE_BY_MARK[char] = i + 1;
    });
}

// Tone of a marked syllable (5 = neutral / no mark).
function toneOf(syllable) {
    for (const char of syllable) {
        if (TONE_BY_MARK[char]) return TONE_BY_MARK[char];
    }
    return 5;
}

// Add the tone mark to one numeric syllable, e.g. "guo2" -> "guó".
function markSyllable(syllable) {
    const m = syllable.match(/^([A-Za-zü:ê]+)([1-5])?$/);
    if (!m) return syllable;
    const body = m[1].replace(/u:/g, "ü").replace(/v/g, "ü").replace(/V/g, "Ü");
    const tone = m[2];
    if (!tone || tone === "5") return body;

    // Hanyu Pinyin placing rule: a/e win, else the "o" in "ou", else the last vowel.
    const lower = body.toLowerCase();
    let idx = -1;
    if (lower.includes("a")) idx = lower.indexOf("a");
    else if (lower.includes("e")) idx = lower.indexOf("e");
    else if (lower.includes("ou")) idx = lower.indexOf("o");
    else for (let i = 0; i < lower.length; i++) if ("iouü".includes(lower[i])) idx = i;
    if (idx === -1) return body;

    const marks = TONE_MARKS[body[idx]];
    if (!marks) return body;
    return body.slice(0, idx) + marks[Number(tone) - 1] + body.slice(idx + 1);
}

// Convert a numeric reading to tone-marked pinyin, e.g. "guo2 ji4" -> "guó jì".
// Also splits syllables that are run together ("xia4hai2" -> "xià hái").
function numericToMarked(pinyin) {
    const syllables = pinyin.match(/[A-Za-zü:]+[0-9]?/g) || [];
    return syllables.map(markSyllable).join(" ");
}

// --- Zhuyin (Bopomofo) -----------------------------------------------------
// Pinyin syllables are converted to zhuyin compositionally: an initial + a
// final, after rewriting the y/w spellings and the j/q/x + u (= ü) spelling.

const ZHUYIN_INITIALS = {
    b: "ㄅ", p: "ㄆ", m: "ㄇ", f: "ㄈ",
    d: "ㄉ", t: "ㄊ", n: "ㄋ", l: "ㄌ",
    g: "ㄍ", k: "ㄎ", h: "ㄏ",
    j: "ㄐ", q: "ㄑ", x: "ㄒ",
    zh: "ㄓ", ch: "ㄔ", sh: "ㄕ", r: "ㄖ",
    z: "ㄗ", c: "ㄘ", s: "ㄙ",
};

const ZHUYIN_FINALS = {
    a: "ㄚ", o: "ㄛ", e: "ㄜ", "ê": "ㄝ", er: "ㄦ",
    ai: "ㄞ", ei: "ㄟ", ao: "ㄠ", ou: "ㄡ",
    an: "ㄢ", en: "ㄣ", ang: "ㄤ", eng: "ㄥ",
    i: "ㄧ", ia: "ㄧㄚ", ie: "ㄧㄝ", iao: "ㄧㄠ", iu: "ㄧㄡ",
    ian: "ㄧㄢ", in: "ㄧㄣ", iang: "ㄧㄤ", ing: "ㄧㄥ", iong: "ㄩㄥ",
    u: "ㄨ", ua: "ㄨㄚ", uo: "ㄨㄛ", uai: "ㄨㄞ", ui: "ㄨㄟ",
    uan: "ㄨㄢ", un: "ㄨㄣ", uang: "ㄨㄤ", ong: "ㄨㄥ", ueng: "ㄨㄥ",
    "ü": "ㄩ", "üe": "ㄩㄝ", "üan": "ㄩㄢ", "ün": "ㄩㄣ",
    // "jue/que/xue" are written with u rather than ü in pinyin.
    ue: "ㄩㄝ",
};

// Leading y/w are written as the underlying finals in zhuyin.
const PINYIN_Y = {
    yi: "i", ya: "ia", ye: "ie", yao: "iao", you: "iu", yan: "ian",
    yin: "in", yang: "iang", ying: "ing", yong: "iong",
    yu: "ü", yue: "üe", yuan: "üan", yun: "ün",
};
const PINYIN_W = {
    wu: "u", wa: "ua", wo: "uo", wai: "uai", wei: "ui", wan: "uan",
    wen: "un", wang: "uang", weng: "ueng",
};

// Zhuyin tone marks by tone number (1 = no mark, 5 = neutral).
const ZHUYIN_TONES = ["", "ˊ", "ˇ", "ˋ", "˙"];

// Reverse of the tone-mark map: a marked character -> its plain base.
const MARK_TO_BASE = {};
for (const [base, marks] of Object.entries(TONE_MARKS)) {
    [...marks].forEach((mark) => {
        MARK_TO_BASE[mark] = base;
    });
}

// A marked syllable with tones removed and lowercased, e.g. "Zhōng" -> "zhong".
function plainSyllable(syllable) {
    return [...syllable].map((c) => MARK_TO_BASE[c] || c).join("").toLowerCase();
}

// Strip tone marks and spaces from a pinyin string, e.g. "yá bái" -> "yabai".
function toToneless(text) {
    return [...text.toLowerCase()]
        .map((c) => MARK_TO_BASE[c] || c)
        .join("")
        .replace(/[^a-zü]/g, "");
}

// Tone-aware reading identity: "zhī" and "zhi1" both become "zhi1". Used to put
// an "Other forms" note under the reading it actually belongs to.
function readingKey(text) {
    return pinyinSyllables(text)
        .map((syllable) => `${plainSyllable(syllable)}${toneOf(syllable)}`)
        .join(" ");
}

// Every standard Hanyu Pinyin syllable can be validated from the zhuyin
// tables above, without carrying a separate 400-entry list. Some imported
// dictionaries write readings without spaces ("guàishēngguàiqì") or with
// separators ("huà//yā", "yí・ge"); the dynamic-programming splitter below
// finds the fewest valid syllables in each run.
const PINYIN_INITIALS = [
    "zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "j", "q", "x", "r", "z", "c", "s",
];
const PINYIN_FINALS = new Set([...Object.keys(ZHUYIN_FINALS), "ue", "ê"]);
const PINYIN_ZERO = new Set([
    "a", "o", "e", "ai", "ei", "ao", "ou", "an", "en", "ang", "eng",
    "er", "yo", "ê",
]);
const PINYIN_EXTRA = new Set(["r", "m", "n", "ng", "hm", "hng", "ê"]);
const PINYIN_ANNOTATION_RE = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;

function pinyinBase(syllable) {
    return plainSyllable(String(syllable || ""))
        .replace(/u:/g, "ü")
        .replace(/v/g, "ü")
        .replace(/[ńňǹ]/g, "n")
        .replace(/ḿ/g, "m")
        .replace(/[0-9]/g, "");
}

// True for a standard pinyin syllable (including erhua "r" and the syllabic
// nasals/interjections), with or without tone marks.
function isPinyinSyllable(syllable) {
    const base = pinyinBase(syllable);
    if (!base) return false;
    if (PINYIN_EXTRA.has(base)) return true;
    if (base in PINYIN_Y || base in PINYIN_W) return true;
    if (PINYIN_ZERO.has(base)) return true;
    for (const initial of PINYIN_INITIALS) {
        if (
            base.startsWith(initial) &&
            PINYIN_FINALS.has(base.slice(initial.length))
        ) {
            return true;
        }
    }
    return false;
}

// Split a run-together pinyin chunk into the fewest valid syllables. Returns
// null when the chunk is not wholly pinyin (an annotation or romanization).
function splitPinyinChunk(chunk) {
    const length = chunk.length;
    const best = new Array(length + 1).fill(null);
    best[0] = [];
    for (let start = 0; start < length; start += 1) {
        if (!best[start]) continue;
        const maxEnd = Math.min(start + 7, length);
        for (let end = start + 1; end <= maxEnd; end += 1) {
            const piece = chunk.slice(start, end);
            if (!isPinyinSyllable(piece)) continue;
            const candidate = best[start].concat(piece);
            if (!best[end] || candidate.length < best[end].length) {
                best[end] = candidate;
            }
        }
    }
    return best[length];
}

function normalizePinyinScan(text) {
    return [...String(text || "")]
        .map((char) => {
            const code = char.codePointAt(0);
            if (code >= 0xff01 && code <= 0xff5e) {
                return String.fromCodePoint(code - 0xfee0);
            }
            if (code === 0x3000) return " ";
            return char;
        })
        .join("");
}

// The split part that carries a chunk's tone. For erhua "dair4" the split is
// ["dai", "r"], and the tone belongs on "dai", not on the suffix "r".
function tonePartIndex(parts) {
    return parts.length > 1 && parts[parts.length - 1] === "r"
        ? parts.length - 2
        : parts.length - 1;
}

// Turn any dictionary reading into display tokens. Pinyin runs are split into
// syllables; separators are dropped, while CJK/kana annotations are kept so
// nothing from the source is lost. Run this before converting to zhuyin.
function pinyinSyllables(text) {
    const source = normalizePinyinScan(text);
    const tokens = [];
    let cursor = 0;
    const pushAnnotation = (value) => {
        const cleaned = value
            .replace(/^[\s\p{P}\p{S}]+|[\s\p{P}\p{S}]+$/gu, "")
            .replace(/\s+/g, " ")
            .trim();
        if (cleaned && PINYIN_ANNOTATION_RE.test(cleaned)) {
            tokens.push(cleaned);
        }
    };
    // Superscript tone digits (im¹, dair⁴) are part of the token. Several
    // dialect romanizations use them, and dropping them would lose the tone.
    for (const match of source.matchAll(/[\p{Script=Latin}:]+[0-9¹²³⁴⁵]?/gu)) {
        pushAnnotation(source.slice(cursor, match.index));
        cursor = match.index + match[0].length;
        const raw = match[0];
        const superTone = raw.match(/^(.*?)([¹²³⁴⁵])$/);
        if (superTone) {
            const parts = splitPinyinChunk(superTone[1]);
            if (!parts) {
                tokens.push(raw);
                continue;
            }
            const at = tonePartIndex(parts);
            parts.forEach((part, index) => {
                tokens.push(index === at ? part + superTone[2] : part);
            });
            continue;
        }
        const toneMatch = raw.match(/^(.*?)([1-5])$/);
        const letters = (toneMatch ? toneMatch[1] : raw).replace(/u:/g, "ü");
        const tone = toneMatch ? toneMatch[2] : "";
        const parts = splitPinyinChunk(letters);
        if (!parts) {
            tokens.push(raw);
            continue;
        }
        const at = tonePartIndex(parts);
        parts.forEach((part, index) => {
            if (tone && index === at) {
                tokens.push(markSyllable(part + tone));
            } else {
                tokens.push(part);
            }
        });
    }
    pushAnnotation(source.slice(cursor));
    return tokens.filter(Boolean);
}

// Convert one marked pinyin syllable to zhuyin, e.g. "hǎo" -> "ㄏㄠˇ".
function toZhuyin(syllable) {
    const marked = markSyllable(syllable);
    if (!isPinyinSyllable(marked)) return syllable;
    let base = plainSyllable(marked);
    if (base === "r") base = "er"; // the erhua suffix

    if (PINYIN_Y[base]) base = PINYIN_Y[base];
    else if (PINYIN_W[base]) base = PINYIN_W[base];

    let initial = "";
    for (const init of ["zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
        "g", "k", "h", "j", "q", "x", "r", "z", "c", "s"]) {
        if (base.startsWith(init)) {
            initial = init;
            break;
        }
    }
    let final = base.slice(initial.length);

    if (final === "i" && ["zh", "ch", "sh", "r", "z", "c", "s"].includes(initial)) {
        // zhi/chi/shi/ri/zi/ci/si: the "i" is not written in zhuyin.
        return ZHUYIN_INITIALS[initial] + ZHUYIN_TONES[toneOf(marked) - 1];
    }
    // After j/q/x, a written "u" is actually ü.
    if (["j", "q", "x"].includes(initial) && final.startsWith("u")) {
        final = "ü" + final.slice(1);
    }
    const body = (ZHUYIN_INITIALS[initial] || "") + (ZHUYIN_FINALS[final] || final);
    return body + ZHUYIN_TONES[toneOf(marked) - 1];
}

// Classify each reading token. A token that is valid Mandarin pinyin becomes
// bopomofo; everything else stays Latin. Lone m/n/ng and uppercase single
// letters stay Latin because ㄇ˙/ㄋ˙ or ㄚ˙ would be misleading.
//
// A Latin word spelled out letter by letter ("w o r d jiě") is the exception
// to per-token checking: its "o" and "r" are valid pinyin on their own. So a
// run of neutral-tone single letters and non-pinyin tokens that contains at
// least one non-pinyin token is kept Latin as a whole.
function phoneticTokens(syllables, { allowTones = true } = {}) {
    const tokens = syllables.map((syllable) => {
        const raw = String(syllable || "");
        if (!allowTones) {
            return { text: raw, kind: "latin" };
        }
        if (/^[A-ZÜ]$/.test(raw)) {
            return { text: raw, kind: "latin" };
        }
        const base = plainSyllable(raw);
        if (["m", "n", "ng", "hm", "hng"].includes(base)) {
            return { text: raw, kind: "latin" };
        }
        if (isPinyinSyllable(raw)) {
            return { text: toZhuyin(raw), kind: "pinyin" };
        }
        return { text: raw, kind: "latin" };
    });
    if (!allowTones) return tokens;

    const raws = syllables.map((syllable) => String(syllable || ""));
    // Could be one letter of a spelled-out word: already Latin, or an
    // unmarked single letter. A toned ā/ò is a real interjection, so it
    // never joins a run.
    const joinsRun = (i) =>
        tokens[i].kind === "latin" ||
        (/^\p{L}$/u.test(raws[i]) && toneOf(raws[i]) === 5);

    let i = 0;
    while (i < tokens.length) {
        if (!joinsRun(i)) {
            i += 1;
            continue;
        }
        let end = i;
        let hasNonPinyin = false;
        while (end < tokens.length && joinsRun(end)) {
            if (!isPinyinSyllable(raws[end])) hasNonPinyin = true;
            end += 1;
        }
        if (hasNonPinyin) {
            for (let j = i; j < end; j += 1) {
                tokens[j] = { text: raws[j], kind: "latin" };
            }
        }
        i = end;
    }
    return tokens;
}

// Build the reading text for a group of syllables: one tone-colored span per
// syllable when coloring is on, otherwise a single plain text node. Latin
// tokens get no tone class.
function readingContent(
    readings,
    syllables,
    colored = state.toneColoring,
    kinds = null,
) {
    if (!colored) {
        return document.createTextNode(readings.join("\u00a0"));
    }
    const fragment = document.createDocumentFragment();
    readings.forEach((reading, i) => {
        if (i > 0) fragment.append("\u00a0");
        const span = document.createElement("span");
        const kind = kinds ? kinds[i] : "pinyin";
        span.className = kind === "latin" ? "latin" : `tone${toneOf(syllables[i])}`;
        span.textContent = reading;
        fragment.append(span);
    });
    return fragment;
}

// Characters with tone colouring but no reading above them. Used in notes where
// the section heading already gives the reading.
function toneColoredText(text, pinyin) {
    const raw = String(text || "");
    const chars = [...raw];
    const syllables = pinyinSyllables(pinyin || "");
    const spoken = chars.filter((ch) => !PUNCT_CHAR_RE.test(ch));
    if (
        !state.toneColoring ||
        !syllables.length ||
        syllables.length !== spoken.length
    ) {
        return document.createTextNode(raw);
    }
    const fragment = document.createDocumentFragment();
    let index = 0;
    for (const ch of chars) {
        if (PUNCT_CHAR_RE.test(ch)) {
            fragment.append(ch);
            continue;
        }
        const span = document.createElement("span");
        span.className = `tone${toneOf(syllables[index])}`;
        span.textContent = ch;
        fragment.append(span);
        index += 1;
    }
    return fragment;
}

// One punctuation/symbol character: these never take a reading.
const PUNCT_CHAR_RE = /^[\p{P}\p{S}]$/u;

// Pinyin fields from source dictionaries can contain several alternate readings
// joined together. Keep the first syllable for each spoken character so the
// reading lines up with the headword instead of spilling over it.
function readingSyllablesForWord(pinyin, word) {
    let syllables = pinyinSyllables(pinyin);
    const spoken = [...String(word || "")].filter(
        (char) => !PUNCT_CHAR_RE.test(char)
    ).length;
    if (spoken && syllables.length > spoken) {
        syllables = syllables.slice(0, spoken);
    }
    return syllables;
}
// A punctuation token in a reading, e.g. the "," in "xīn , Xiāng": not a syllable.
const PUNCT_TOKEN_RE = /^[\p{P}\p{S}]+$/u;

// Numeral syllables, used to align a leading digit run's tones with its reading.
const NUMERAL_BASES = new Set([
    "yi", "er", "san", "si", "wu", "liu", "qi", "ba", "jiu",
    "shi", "bai", "qian", "wan", "ling", "liang", "yao",
]);

// The syllable without tone marks or case ("Wàn" -> "wan").
function syllableBase(syllable) {
    return syllable
        .toLowerCase()
        .normalize("NFD")
        .replace(/\p{M}/gu, "")
        .replace(/ü/g, "u");
}

// True when a syllable reads a number. "shi" is ambiguous: 十 (shí, tone 2) is a
// number, but 式/是 (shì, tone 4) is not.
function isNumeralSyllable(syllable) {
    const base = syllableBase(syllable);
    if (!NUMERAL_BASES.has(base)) return false;
    if (base === "shi") return toneOf(syllable) === 2;
    return true;
}

// Tone per character for a leading digit run, e.g. 10000 -> "yī wàn": "1" takes
// yī's tone and "0000" takes wàn's. Returns null when it cannot map safely.
function numberTones(chars, syllables) {
    if (!/^\d/.test(chars.join(""))) return null;

    const tones = new Array(chars.length).fill(0);
    let charIndex = 0;
    let syllableIndex = 0;
    while (charIndex < chars.length && syllableIndex < syllables.length) {
        const digits = (chars.slice(charIndex).join("").match(/^\d+/) || [""])[0];
        if (digits) {
            // Consume the numeral syllables that read this number.
            let end = syllableIndex;
            while (end < syllables.length && isNumeralSyllable(syllables[end])) {
                end += 1;
            }
            const numberSyllables = syllables.slice(syllableIndex, end);
            if (!numberSyllables.length) break;
            // Colour digits only when the reading lists them in order (1812 =
            // yī bā yī èr). A 0 is only coloured when it is read "líng", not
            // skipped as part of a place unit (2000 = èr qiān).
            const digitsListed =
                digits.length === numberSyllables.length &&
                digits.split("").every(
                    (digit, i) => digit !== "0" || syllableBase(numberSyllables[i]) === "ling"
                );
            if (digitsListed) {
                digits.split("").forEach((_, i) => {
                    tones[charIndex + i] = toneOf(numberSyllables[i]);
                });
            } else if (digits === "10" && numberSyllables.length === 1) {
                // 10 read as shí: both digits take that one tone.
                tones[charIndex] = tones[charIndex + 1] = toneOf(numberSyllables[0]);
            }
            charIndex += digits.length;
            syllableIndex = end;
        } else {
            // A single non-digit character (×, 米) takes the next syllable's tone.
            tones[charIndex] = toneOf(syllables[syllableIndex]);
            charIndex += 1;
            syllableIndex += 1;
        }
    }
    return tones.some((tone) => tone) ? tones : null;
}

// Tone per character for a Latin prefix plus trailing Chinese characters, e.g.
// ATM机 read "ēi tì ái mu jī". Only the trailing Chinese is coloured.
function trailingTones(chars, syllables) {
    let start = chars.length;
    while (start > 0 && CJK_RE.test(chars[start - 1])) start -= 1;
    const count = chars.length - start;
    if (!count || count > syllables.length) return null;
    if (!/[A-Za-z]/.test(chars.slice(0, start).join(""))) return null;
    const tones = new Array(chars.length).fill(0);
    for (let i = 0; i < count; i += 1) {
        tones[start + i] = toneOf(syllables[syllables.length - count + i]);
    }
    return tones;
}

function furigana(text, pinyin, allowTones = true, raw = false) {
    const chars = [...text];
    const rawReading = String(pinyin || "").trim();
    // A Dungan reading is a whole phrase with tone numerals
    // ("jiguanzɨhuar (I-I-I-I)"); splitting it would scatter the numerals, so
    // it is shown as one reading line.
    const syllables = raw
        ? rawReading
            ? [rawReading]
            : []
        : readingSyllablesForWord(pinyin, text).filter(
            (syllable) => syllable && !PUNCT_TOKEN_RE.test(syllable)
        );
    // The readings in the chosen phonetic system. Conversion is per token:
    // valid Mandarin syllables become bopomofo, while Latin tokens (w, d,
    // uppercase letters, a Latin word spelled out as pinyin) stay as written.
    const tokens =
        state.phonetic === "zhuyin"
            ? phoneticTokens(syllables, { allowTones })
            : null;
    const readings = tokens ? tokens.map((token) => token.text) : syllables;
    const kinds = tokens ? tokens.map((token) => token.kind) : null;
    const node = document.createElement("span");
    node.className = raw ? "furigana raw-reading" : "furigana";
    // Some readings (Wu romanizations) are shown, but not tone-coloured: the
    // colours are Hanyu Pinyin tones, which do not apply to them.
    const colored = state.toneColoring && allowTones;

    // Some headwords keep a literal number in their reading: 21三体综合症
    // has the pinyin "21 sān tǐ zōng hé zhèng". In that case the leading
    // digits are not read, so they get empty reading cells.
    const leadingDigits = (text.match(/^\d+/) || [""])[0];
    const pinyinDigits = (String(pinyin || "").trim().match(/^\d+/) || [""])[0];
    const literalPrefix = Boolean(leadingDigits && pinyinDigits === leadingDigits);

    // Map each character to its own reading when the counts line up. A literal
    // number prefix has no reading; the characters after it start the mapping.
    // Numeric headwords whose reading does not spell the digits one-for-one
    // (1500 = yī qiān wǔ bǎi) use the single-cell fallback below.
    let readingsByChar = null;
    let syllablesByChar = null;
    let kindsByChar = null;
    if (readings.length) {
        const spoken = chars.filter((ch) => !PUNCT_CHAR_RE.test(ch));
        const tailStart = literalPrefix ? leadingDigits.length : 0;
        const tailSpoken = chars
            .slice(tailStart)
            .filter((ch) => !PUNCT_CHAR_RE.test(ch));
        const canAlign = literalPrefix
            ? tailSpoken.length === syllables.length
            : !leadingDigits && spoken.length === syllables.length
                ? true
                : spoken.length === syllables.length && Boolean(numberTones(chars, syllables));
        if (canAlign) {
            readingsByChar = new Array(chars.length).fill(null);
            syllablesByChar = new Array(chars.length).fill(null);
            kindsByChar = new Array(chars.length).fill(null);
            let index = 0;
            chars.forEach((ch, i) => {
                if (i < tailStart || PUNCT_CHAR_RE.test(ch)) return;
                readingsByChar[i] = readings[index];
                syllablesByChar[i] = syllables[index];
                kindsByChar[i] = kinds ? kinds[index] : null;
                index += 1;
            });
        }
    }

    // The reading row. When each character has a reading, one cell per
    // character keeps the reading centered above that character. The empty
    // cells for a number prefix or punctuation reserve the right amount of
    // space. In the fallback the whole reading shares one centered cell.
    if (readings.length) {
        const reading = document.createElement("span");
        reading.className = "reading";
        if (readingsByChar) {
            chars.forEach((_ch, i) => {
                const cell = document.createElement("span");
                cell.className = "reading-cell";
                if (readingsByChar[i]) {
                    cell.append(
                        readingContent(
                            [readingsByChar[i]],
                            [syllablesByChar[i]],
                            colored,
                            [kindsByChar[i]]
                        )
                    );
                }
                reading.append(cell);
            });
        } else {
            const cell = document.createElement("span");
            cell.className = "reading-cell";
            cell.append(readingContent(readings, syllables, colored, kinds));
            reading.append(cell);
        }
        node.append(reading);
    }

    // The character row. It is one line of inline-block cells, so native
    // selection stays contiguous; the layout pass sets each cell to the wider
    // of its character and reading, which spaces the characters and keeps the
    // reading aligned.
    const char = document.createElement("span");
    char.className = "char";
    if (readingsByChar) {
        chars.forEach((ch, i) => {
            const cell = document.createElement("span");
            cell.className = "char-cell";
            const glyph = document.createElement("span");
            glyph.textContent = ch;
            if (colored && syllablesByChar[i]) {
                glyph.className = `tone${toneOf(syllablesByChar[i])}`;
            } else if (!syllablesByChar[i]) {
                // Numbers, punctuation and other unread characters are not
                // toned; show them in the neutral tone-5 grey.
                glyph.className = "plain";
            }
            cell.append(glyph);
            char.append(cell);
        });
    } else {
        const cell = document.createElement("span");
        cell.className = "char-cell";
        let tones = null;
        if (colored) {
            // Numbers often have more characters than syllables (10000 = yī
            // wàn). A segmented row underlines each piece in its own colour.
            tones = numberTones(chars, syllables) || trailingTones(chars, syllables);
        }
        if (tones) {
            node.classList.add("segmented");
            chars.forEach((ch, i) => {
                const glyph = document.createElement("span");
                glyph.textContent = ch;
                glyph.className = tones[i] ? `tone${tones[i]}` : "plain";
                cell.append(glyph);
            });
        } else {
            chars.forEach((ch) => {
                const glyph = document.createElement("span");
                glyph.textContent = ch;
                // Punctuation, digits and other unread pieces are neutral grey.
                if (!CJK_RE.test(ch)) glyph.className = "plain";
                cell.append(glyph);
            });
        }
        char.append(cell);
    }
    node.append(char);
    return node;
}

// Even out each reading/character pair: both cells get the width of the wider
// one. The reading sits centered above its character and the character line
// spreads naturally, while staying in normal inline flow for selection.
//
// Measuring every cell one node at a time forces a layout per getBoundingClientRect,
// which becomes quadratic on long pastes (thousands of cells). Collect the new
// nodes in a microtask and do all clears first, then all reads, then all
// writes: the browser lays the page out once instead of once per cell.
const pendingFurigana = new Set();
let furiganaFlushQueued = false;

function layoutFurigana(node) {
    if (!node || node.dataset.furiganaLayout === "done") return;
    pendingFurigana.add(node);
    if (furiganaFlushQueued) return;
    furiganaFlushQueued = true;
    queueMicrotask(flushFuriganaLayout);
}

function flushFuriganaLayout() {
    const start = performance.now();
    furiganaFlushQueued = false;
    const nodes = [...pendingFurigana].filter(
        (node) => node.isConnected && node.dataset.furiganaLayout !== "done"
    );
    pendingFurigana.clear();
    if (!nodes.length) return;
    const entries = [];
    // Phase 1: clear every width (writes only).
    for (const node of nodes) {
        const readingCells = [...node.querySelectorAll(".reading-cell")];
        const charCells = [...node.querySelectorAll(".char-cell")];
        if (!readingCells.length || !charCells.length) {
            node.dataset.furiganaLayout = "done";
            continue;
        }
        for (const cell of [...readingCells, ...charCells]) {
            cell.style.width = "";
            cell.style.minWidth = "";
        }
        entries.push({ node, readingCells, charCells });
    }
    // Phase 2: read every width (one layout, then cached geometry).
    for (const entry of entries) {
        const pairs = Math.min(entry.readingCells.length, entry.charCells.length);
        const widths = new Array(pairs);
        for (let i = 0; i < pairs; i += 1) {
            widths[i] = Math.max(
                entry.readingCells[i].getBoundingClientRect().width,
                entry.charCells[i].getBoundingClientRect().width
            );
        }
        entry.widths = widths;
    }
    // Phase 3: apply all widths (writes only).
    for (const { node, readingCells, charCells, widths } of entries) {
        for (let i = 0; i < widths.length; i += 1) {
            const rounded = `${Math.ceil(widths[i] * 100) / 100}px`;
            readingCells[i].style.width = rounded;
            charCells[i].style.width = rounded;
        }
        node.dataset.furiganaLayout = "done";
    }
    // Only instrument large batches. Adding a debug event to the normal result
    // render shifted the browser test's selection timing.
    if (nodes.length > 100) {
        recordPhase("furigana layout (batched)", start, { nodes: nodes.length });
    }
}

// New result rows, parsed words, and definition links are inserted all over
// the page, so watch for new furigana nodes rather than calling the layout
// pass from every render path.
const furiganaLayoutObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
        for (const added of mutation.addedNodes) {
            if (added.nodeType !== Node.ELEMENT_NODE) continue;
            if (added.matches?.(".furigana")) layoutFurigana(added);
            added.querySelectorAll?.(".furigana").forEach(layoutFurigana);
        }
    }
});
furiganaLayoutObserver.observe(document.body, { childList: true, subtree: true });

// CEDICT cross-references look like "國際企業管理|国际企业管理[guo2 ji4 ...]"
// or "个[ge4]". Group 1 = traditional (or the only form), 2 = simplified,
// 3 = numeric pinyin.
const CROSS_REF = /([^\s|\[\]:/]+)(?:\|([^\s|\[\]:/]+))?\[([^\]]+)\]/g;
// MOE (and other non-CEDICT) definitions write cross-references as "→word".
const ARROW_CROSS_REF = /→\s*(\p{Script=Han}+)/gu;
// A run of Chinese characters inside a definition, e.g. "alternative form of 日".
const CJK_RUN_RE = /\p{Script=Han}+/gu;

// Append plain definition text, turning Chinese runs into search links. The
// longest run stays one link, so a word is not split into separate characters.
// `linkChinese` is off for Chinese-language sources (MOE), whose prose is not a
// list of terms to look up.
function appendLinkedText(fragment, text, linkChinese) {
    if (!linkChinese) {
        fragment.append(text);
        return;
    }
    let last = 0;
    let match;
    CJK_RUN_RE.lastIndex = 0;
    while ((match = CJK_RUN_RE.exec(text)) !== null) {
        if (match.index > last) fragment.append(text.slice(last, match.index));
        fragment.append(searchLink(match[0]));
        last = match.index + match[0].length;
    }
    if (last < text.length) fragment.append(text.slice(last));
}

// A clickable link that searches `text`. `node` is the label (e.g. furigana);
// without it the text itself is the (underlined) label.
function searchLink(text, node) {
    const link = document.createElement("a");
    link.className = node ? "def-link" : "def-link def-text";
    link.href = `?q=${encodeURIComponent(text)}`;
    if (node) link.append(node);
    else link.textContent = text;
    link.addEventListener("click", (event) => {
        event.preventDefault();
        performSearch(text);
    });
    return link;
}

// Append text with CC-CEDICT style cross-references ("word[pin1 yin1]") as
// clickable furigana links. Annotated glosses use the same bracket form.
function appendCrossRefs(fragment, text, taiwanRefs, linkChinese) {
    let last = 0;
    let match;
    CROSS_REF.lastIndex = 0;
    while ((match = CROSS_REF.exec(text)) !== null) {
        if (match.index > last) {
            appendLinkedText(fragment, text.slice(last, match.index), linkChinese);
        }
        const word =
            state.script === "traditional"
                ? (match[1] || match[2])
                : (match[2] || match[1]);
        const reading =
            state.taiwan && taiwanRefs && taiwanRefs[word]
                ? taiwanRefs[word]
                : numericToMarked(match[3]);
        // Cross-references ("see also 洪秀全[…]") render as clickable, underlined
        // links that search the referenced word.
        fragment.append(searchLink(word, furigana(word, reading)));
        last = match.index + match[0].length;
    }
    if (last < text.length) {
        appendLinkedText(fragment, text.slice(last), linkChinese);
    }
}

// Build a definition: "word[pinyin]" cross-references (CC-CEDICT and annotated
// glosses) plus "→word" links (MOE/Wiktionary).
function renderDefinition(text, taiwanRefs, linkChinese) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    let match;
    ARROW_CROSS_REF.lastIndex = 0;
    while ((match = ARROW_CROSS_REF.exec(text)) !== null) {
        appendCrossRefs(fragment, text.slice(last, match.index), taiwanRefs, linkChinese);
        fragment.append("→");
        fragment.append(searchLink(match[1]));
        last = match.index + match[0].length;
    }
    appendCrossRefs(fragment, text.slice(last), taiwanRefs, linkChinese);
    return fragment;
}

// Pull pronunciation notes out of a definition: "Taiwan pr. [...]" and
// "also/old/colloquial pr. [...]". Returns the cleaned definition text plus the
// raw numeric readings found in each category. Non-Taiwan notes keep their
// register label (e.g. "colloquial") so it can be shown next to the reading
// instead of being left behind as stray text in the definition.
function extractPronunciations(text) {
    const taiwan = new Set();
    const others = []; // { reading, label } - label is "" for a plain note
    const classifiers = new Set();
    // A register word can sit right in front of "pr." ("colloquial pr.",
    // "also pr.", "Beijing pr."). Capture it so it is removed together with the
    // reading; otherwise words like "colloquial" would linger in the definition.
    const PR_LABELS = "Taiwan|Taiwanese|colloquial|coll\\.|archaic|literary|formal|"
        + "informal|dialectal|dialect|old|ancient|also|Beijing|commonly|Cantonese|"
        + "surname|variant";
    const cleaned = text
        .replace(
            new RegExp(`[;\\s]*(?:(${PR_LABELS})\\s+)?pr\\.\\s*\\[([^\\]]+)\\]`, "g"),
            (_, label, p) => {
                const key = label ? label.replace(/\.$/, "") : "";
                // "Taiwan pr." keeps its own row; everything else is an alternate
                // reading, tagged with its label ("also" reads as a plain note).
                if (key === "Taiwan") taiwan.add(p);
                else others.push({ reading: p, label: key === "also" ? "" : key });
                return "";
            }
        )
        .replace(/[;\s]*pronounced\s+\[([^\]]+)\]/g, (_, p) => (others.push({ reading: p, label: "" }), ""))
        // Classifiers are a comma-separated list of "word[pinyin]" tokens, e.g.
        // "CL:個|个[ge4],位[wei4]". Match whole tokens so a trailing ")" (as in
        // "(CL:首[shou3])") is not swallowed, and capture every token.
        .replace(
            /[;\s]*CL:\s*([^\s\[;,/]+\[[^\]]+\](?:\s*,\s*[^\s\[;,/]+\[[^\]]+\])*)/g,
            (_, list) => (list.split(/\s*,\s*/).forEach((token) => classifiers.add(token)), "")
        )
        // Remaining bare numeric-pinyin brackets, e.g. "... sb; Taiwan pr. [pang2],
        // [bang1], [bang4]". Only when preceded by a separator, so cross-references
        // like 字[pin] are left untouched.
        .replace(/(?:^|[\s,;]+)\[([A-Za-zü:]+[0-9](?:\s+[A-Za-zü:]+[0-9])*)\]/g, (_, p) => (others.push({ reading: p, label: "" }), ""));
    return {
        text: cleaned
            .replace(/\s*;\s*;/g, ";")
            .replace(/\(\s*\)/g, "") // drop parentheses the removal left empty
            .replace(/\s+;/g, ";") // tidy a space left before a separator
            .replace(/[;\s]+$/g, "")
            .replace(/\s{2,}/g, " ")
            .trim(),
        taiwan: [...taiwan],
        others,
        classifiers: [...classifiers],
    };
}

// A muted detail row under a result (alternate pronunciations), styled exactly
// like the Classifier line: bold label + value, same size/colour/indent.
function detailRow(label, node) {
    const row = document.createElement("div");
    row.className = "def-source-cls";
    const name = document.createElement("span");
    name.className = "def-source-cls-label";
    name.textContent = label.replace(/:\s*$/, ":");
    row.append(name, document.createTextNode(" "), node);
    return row;
}

// Clear the results area (used by the home view; shows no "No results." message).
function clearResults() {
    currentEntries = [];
    currentTokens = [];
    results.replaceChildren();
    parsed.replaceChildren();
    pagination.replaceChildren();
    pagination.hidden = true;
    paginationTop.replaceChildren();
    paginationTop.hidden = true;
    hint.hidden = true;
}

// Show the pinyin hint when the query matched Chinese readings exactly - which
// is also when English-definition matches are suppressed. Rather than telling the
// user to add quotes, we offer a link that runs that quoted (English) search.
function updateHint(query, pinyinMatch, englishMatch) {
    // Shown only when the query matched pinyin exactly AND the English search
    // would find something; hidden for tone-numbered, wildcard, or
    // already-quoted queries (they'd end up double-quoted).
    if (pinyinMatch && englishMatch && !/[0-9*"]/.test(query)) {
        const quoted = `"${query.trim()}"`;
        hint.replaceChildren("Matching Chinese pinyin, search the English word ");
        const link = document.createElement("a");
        // A real href (so it can be opened/copied) plus JS to search in place.
        link.href = `?q=${encodeURIComponent(quoted)}`;
        link.textContent = quoted; // only the quoted word itself is the link
        link.addEventListener("click", (event) => {
            event.preventDefault();
            performSearch(quoted);
        });
        hint.append(link);
        hint.hidden = false;
    } else {
        hint.hidden = true;
    }
}

// The body of one dictionary's definitions: numbered senses and the classifier
// line (no heading). Shared by result blocks and match boxes.
// The copied reading follows the Pinyin/Bopomofo display toggle.
function copyReading(pinyin, word = "", allowTones = true) {
    if (!pinyin || state.phonetic !== "zhuyin") return pinyin || "";
    const syllables = readingSyllablesForWord(pinyin, word);
    if (!syllables.length) return pinyin;
    return phoneticTokens(syllables, { allowTones })
        .map((token) => token.text)
        .join(" ");
}

// Convert pinyin inside a definition to bopomofo for the copy button. Only
// bracketed readings and a leading transliteration of the headword are safe to
// rewrite; ordinary English words that happen to look like pinyin are left
// alone.
function pinyinToZhuyinForCopy(text, reading) {
    if (state.phonetic !== "zhuyin") return text;
    let out = String(text || "").replace(/\[([^\]]+)\]/g, (whole, inner) => {
        const syllables = pinyinSyllables(inner);
        if (!syllables.length) return whole;
        return `[${phoneticTokens(syllables).map((token) => token.text).join(" ")}]`;
    });

    if (!reading) return out;
    const syllables = pinyinSyllables(reading);
    if (!syllables.length) return out;
    const first = out.match(/^([A-Za-züÜ:]+)\b/);
    if (
        first &&
        toToneless(first[1]) &&
        toToneless(first[1]) === toToneless(syllables.join(""))
    ) {
        out = out.replace(
            first[0],
            phoneticTokens(syllables).map((token) => token.text).join(" "),
        );
    }
    return out;
}

function copyDefinitionButton(reading, word, definition, pinyin = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "def-copy";
    button.title = "Copy definition";
    button.setAttribute("aria-label", "Copy definition");

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", "14");
    svg.setAttribute("height", "14");
    svg.setAttribute("aria-hidden", "true");
    const back = document.createElementNS("http://www.w3.org/2000/svg", "path");
    back.setAttribute("d", "M5.5 2.5h7a1 1 0 0 1 1 1v7");
    back.setAttribute("fill", "none");
    back.setAttribute("stroke", "currentColor");
    back.setAttribute("stroke-width", "1.3");
    back.setAttribute("stroke-linecap", "round");
    const front = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    front.setAttribute("x", "2.5");
    front.setAttribute("y", "4.5");
    front.setAttribute("width", "7");
    front.setAttribute("height", "9");
    front.setAttribute("rx", "1");
    front.setAttribute("fill", "none");
    front.setAttribute("stroke", "currentColor");
    front.setAttribute("stroke-width", "1.3");
    svg.append(back, front);
    svg.classList.add("icon-copy");

    // The same button switches to a checkmark after a successful copy.
    const check = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    check.setAttribute("viewBox", "0 0 16 16");
    check.setAttribute("width", "14");
    check.setAttribute("height", "14");
    check.setAttribute("aria-hidden", "true");
    check.classList.add("icon-check");
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "path");
    tick.setAttribute("d", "M3 8.5 6.3 11.8 13 4.8");
    tick.setAttribute("fill", "none");
    tick.setAttribute("stroke", "currentColor");
    tick.setAttribute("stroke-width", "1.6");
    tick.setAttribute("stroke-linecap", "round");
    tick.setAttribute("stroke-linejoin", "round");
    check.append(tick);
    button.append(svg, check);

    const text = [
        reading,
        word,
        pinyinToZhuyinForCopy(definition, pinyin),
    ]
        .filter(Boolean)
        .join("\n");
    button.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        ignoreNextClipboardChange();
        try {
            await navigator.clipboard.writeText(text);
        } catch (error) {
            // Fallback for browsers/contexts without the async clipboard API.
            const area = document.createElement("textarea");
            area.value = text;
            area.style.position = "fixed";
            area.style.opacity = "0";
            document.body.append(area);
            area.select();
            try {
                document.execCommand("copy");
            } catch (fallbackError) {
                // Nothing else we can do.
            }
            area.remove();
        }
        button.classList.add("copied");
        button.title = "Copied";
        button.setAttribute("aria-label", "Copied");
        setTimeout(() => {
            button.classList.remove("copied");
            button.title = "Copy definition";
            button.setAttribute("aria-label", "Copy definition");
        }, 1200);
    });
    return button;
}

function definitionBody(source, taiwanRefs, copy) {
    const body = document.createDocumentFragment();
    // Only English-language definitions get automatic Chinese links; a Chinese
    // dictionary's own prose (MOE) is not a list of headwords to look up.
    const linkChinese = source.lang === "english";

    // CC-CEDICT variant forms ("Other forms") belong under the reading they are
    // pronounced with, not at the bottom of the whole entry.
    const forms =
        source.anchor === "cedict" && copy && copy.forms ? copy.forms : [];
    const formKey = (form) => readingKey(form.pinyin || "");

    // Senses, grouped by reading. CEDICT polyphonic words get a header per
    // reading; other sources have a single (unlabelled) group.
    const groups =
        source.groups && source.groups.length
            ? source.groups
            : [{ pinyin: "", senses: source.defs || [] }];
    // Only label the readings when a word actually has more than one.
    const showReadings = groups.length > 1;
    for (const group of groups) {
        if (group.variantTitle) {
            // A merged headword: show this traditional spelling's reading and
            // character inside the dictionary block.
            body.append(variantTitleNode(group.variantTitle));
        } else if (showReadings && group.pinyin) {
            const head = document.createElement("div");
            head.className = "def-reading";
            const syllables = pinyinSyllables(group.pinyin);
            // A dialect group stays as written; a Mandarin group converts
            // token by token, so a Latin token in it is not garbled.
            const tokens =
                state.phonetic === "zhuyin"
                    ? phoneticTokens(syllables, {
                          allowTones: !copy.pinyin_no_tone,
                      })
                    : null;
            const readings = tokens ? tokens.map((token) => token.text) : syllables;
            head.append(
                readingContent(
                    readings,
                    syllables,
                    state.toneColoring,
                    tokens ? tokens.map((token) => token.kind) : null,
                ),
            );
            // The spelling these senses belong to: zhī (秖).
            if (group.spellings && group.spellings.length) {
                const spellings = document.createElement("span");
                spellings.className = "def-reading-spellings";
                spellings.append(" (");
                group.spellings.forEach((word, i) => {
                    if (i) spellings.append(", ");
                    spellings.append(toneColoredText(word, group.pinyin));
                });
                spellings.append(")");
                head.append(spellings);
            }
            body.append(head);
        }
        group.senses.forEach((text, i) => {
            const line = document.createElement("div");
            line.className = "def-source-line";
            const num = document.createElement("span");
            num.className = "def-source-num";
            num.textContent = `${i + 1}.`;
            line.append(num, renderDefinition(text, taiwanRefs, linkChinese));
            if (copy && text) {
                // Prefer the reading attached to this sense group (polyphonic
                // words), falling back to the entry's main reading.
                line.append(
                    copyDefinitionButton(
                        copyReading(
                            group.pinyin || copy.reading,
                            copy.word,
                            !copy.pinyin_no_tone,
                        ),
                        copy.word,
                        text,
                        group.pinyin || copy.reading
                    )
                );
            }
            body.append(line);
        });
        // Forms pronounced with this reading: the `zhǐ` group of 只 shows
        // "Other forms: 祇; 衹" underneath its own senses.
        if (forms.length) {
            const key = readingKey(group.pinyin || "");
            const matching = forms.filter((form) => formKey(form) === key);
            if (matching.length) body.append(...formsRows(matching, group.pinyin));
        }
    }

    // A form whose reading is not one of the entry's groups (rare) stays at the
    // bottom of the block rather than disappearing.
    if (forms.length) {
        const groupKeys = new Set(
            groups.map((group) => readingKey(group.pinyin || ""))
        );
        const unmatched = forms.filter((form) => !groupKeys.has(formKey(form)));
        if (unmatched.length) body.append(...formsRows(unmatched));
    }

    // Standardized classifiers, shown in the chosen script (Traditional toggle).
    if (source.classifiers && source.classifiers.length) {
        const useTraditional = state.script === "traditional";
        const line = document.createElement("div");
        line.className = "def-source-cls";
        const label = document.createElement("span");
        label.className = "def-source-cls-label";
        label.textContent = "Classifier:";
        line.append(label, document.createTextNode(" "));
        source.classifiers.forEach((classifier, i) => {
            if (i > 0) line.append(document.createTextNode(", "));
            // MOE stores a prose note instead of a single classifier word.
            // Any Chinese word in that note can be searched, so link it.
            if (classifier.note) {
                const noteFragment = document.createDocumentFragment();
                appendLinkedText(noteFragment, classifier.note, true);
                line.append(noteFragment);
                return;
            }
            const form =
                (useTraditional ? classifier.traditional : classifier.simplified) || "";
            if (!form) return;
            // Read the classifier like any other word: a link, with furigana
            // and tone colouring when the display toggles are on.
            const reading =
                (useTraditional
                    ? classifier.pinyin_traditional
                    : classifier.pinyin_simplified) ||
                (useTraditional
                    ? classifier.pinyin_simplified
                    : classifier.pinyin_traditional) ||
                "";
            line.append(reading ? searchLink(form, furigana(form, reading)) : searchLink(form));
        });
        body.append(line);
    }
    return body;
}

// Search URL for the CC-CEDICT editor; the word is appended URL-encoded.
const CC_CEDICT_SEARCH =
    "https://cc-cedict.org/editor/editor.php?handler=QueryDictionary&querydictionary_search=";

// Page URL for an English Wiktionary entry.
const WIKTIONARY_PAGE = "https://en.wiktionary.org/wiki/";

// Page URL for a Chinese Wiktionary entry.
const ZH_WIKTIONARY_PAGE = "https://zh.wiktionary.org/wiki/";

// A labeled block showing one dictionary's definitions for a result. CC-CEDICT
// and Wiktionary names open that word in the source; other names link to their
// section on the About page.
function definitionsBlock(source, taiwanRefs, word, reading, forms = []) {
    const block = document.createElement("div");
    block.className = "def-source";
    // Imported dictionaries preserve their source line breaks; official
    // definitions keep the previous whitespace behavior.
    if (String(source.anchor || "").startsWith("custom-")) {
        block.classList.add("custom-source");
    }
    const name = document.createElement("a");
    name.className = "def-source-name";
    if (source.anchor === "cedict") {
        name.href = CC_CEDICT_SEARCH + encodeURIComponent(word);
        name.target = "_blank";
        name.rel = "noopener";
    } else if (source.anchor === "wiktionary") {
        name.href = WIKTIONARY_PAGE + encodeURIComponent(word);
        name.target = "_blank";
        name.rel = "noopener";
    } else if (source.anchor === "jpwiktionary") {
        name.href =
            "https://ja.wiktionary.org/wiki/" +
            encodeURIComponent(source.headword || word);
        name.target = "_blank";
        name.rel = "noopener";
    } else if (source.anchor === "zhwiktionary") {
        // Open the exact entry in Chinese Wiktionary, using the headword the
        // source stored (traditional or simplified as that page spells it).
        name.href =
            ZH_WIKTIONARY_PAGE + encodeURIComponent(source.headword || word);
        name.target = "_blank";
        name.rel = "noopener";
    } else if (source.anchor.startsWith("custom-")) {
        // Browser-imported dictionaries have no About page entry.
        name.removeAttribute("href");
    } else {
        name.href = `/about#${encodeURIComponent(source.anchor)}`;
    }
    name.textContent = source.label;
    block.append(name, definitionBody(source, taiwanRefs, { reading, word, forms }));
    return block;
}

// Each result keeps its own set of shown dictionaries (per-entry toggle), which
// starts from the global selection. Keyed by headword.
const entryDictState = new Map();

function entryKey(entry) {
    return `${entry.traditional}|${entry.simplified}`;
}

function entryDicts(entry) {
    const key = entryKey(entry);
    let enabled = entryDictState.get(key);
    if (!enabled) {
        enabled = initialDictSelection(entry);
        entryDictState.set(key, enabled);
    }
    return enabled;
}

// A definition-search row the server put under "Other Dictionaries": the
// search text matched a definition only in dictionaries outside the selection
// (Taiwan selected, "word" matched CC-CEDICT's English). `match_anchors` lists
// the dictionaries whose definitions matched.
function isOtherDefinitionMatch(entry) {
    return (
        !state.customDicts &&
        entry.category === 2 &&
        (entry.match_anchors || []).length > 0
    );
}

// Languages tried, in order, when a result has nothing in the search language.
const FALLBACK_LANG_ORDER = ["english", "taiwan", "mainland", "japanese"];

// The highest-priority anchor of `anchors`, by the language's dictionary order.
function topAnchor(anchors, lang) {
    return [...anchors].sort(
        (a, b) => dictRank(a, lang) - dictRank(b, lang)
    )[0];
}

// The initial dictionaries for a result. Detailed mode uses every selected
// dictionary; compact mode keeps only the highest-ordered dictionary in each
// language that actually has a definition for the entry.
//
// With custom dictionaries off (always compact), a result with no definition in
// the search language falls back to the next dictionaries instead of showing
// none: the ones that matched a definition search, otherwise the best
// dictionary of the first other language that has one.
function initialDictSelection(entry) {
    let selected = new Set(state.dicts);
    let fallback = false;
    if (!state.customDicts) {
        // In default mode the search language decides: every English source
        // (CC-CEDICT, Wiktionary) is on, even though only CC-CEDICT is a
        // default anchor.
        const lang = selectedLangGroup();
        selected = new Set();
        for (const source of entry.definitions || []) {
            const sourceLang =
                source.lang || anchorLang.get(source.anchor) || "";
            if (sourceLang === lang) selected.add(source.anchor);
        }
        if ((entry.forms || []).length && lang === "english") {
            selected.add("cedict");
        }
        const defined = (entry.definitions || []).map((source) => source.anchor);
        if (isOtherDefinitionMatch(entry)) {
            // Show the definitions that matched the search, not unrelated
            // ones from the search language.
            const matched = new Set(entry.match_anchors);
            const shown = defined.filter((anchor) => matched.has(anchor));
            if (shown.length) selected = new Set(shown);
        } else if (!selected.size && defined.length) {
            selected = new Set(defined);
            fallback = true;
        }
    }
    if (!state.compact) return selected;
    const byLang = new Map();
    for (const source of entry.definitions || []) {
        if (!selected.has(source.anchor)) continue;
        const lang = source.lang || anchorLang.get(source.anchor) || "";
        if (!byLang.has(lang)) byLang.set(lang, []);
        byLang.get(lang).push(source.anchor);
    }
    // The two MOE dictionaries are displayed as one merged block, so compact
    // mode keeps both when both are selected even though it normally keeps only
    // one dictionary per language.
    const withMoePair = (set) => {
        if (set.has("moe") && selected.has("moe_revised")) set.add("moe_revised");
        if (set.has("moe_revised") && selected.has("moe")) set.add("moe");
        return set;
    };
    if (fallback) {
        // One dictionary only: the first other language that has a definition.
        const lang =
            FALLBACK_LANG_ORDER.find((name) => byLang.has(name)) ||
            byLang.keys().next().value;
        return withMoePair(
            new Set(lang === undefined ? [] : [topAnchor(byLang.get(lang), lang)]),
        );
    }
    const result = new Set();
    for (const [lang, anchors] of byLang) {
        result.add(topAnchor(anchors, lang));
    }
    return withMoePair(result);
}

// Language code -> the definition language it selects (used for ordering).
const LANG_GROUP = { EN: "english", ZH: "mainland", TW: "taiwan", JP: "japanese" };

function selectedLangGroup() {
    return LANG_GROUP[state.lang] || "english";
}

// Position of a dictionary inside its language column. Compact mode uses this
// to pick one dictionary; detailed mode uses it to order the definition blocks.
function dictRank(anchor, lang) {
    const order = Array.isArray(dictOrder[lang]) ? dictOrder[lang] : [];
    const index = order.indexOf(anchor);
    return index < 0 ? Number.MAX_SAFE_INTEGER : index;
}

// Which dictionaries to render for a result. Compact/detailed mode and the
// per-entry chip toggles decide this.
function visibleDicts(entry) {
    return entryDicts(entry);
}

// Toggle one dictionary's definitions for a single result row (not globally).
function toggleEntryDict(row, entry, anchor) {
    const enabled = entryDicts(entry);
    // The two MOE Taiwan sources share one chip.
    const anchors =
        anchor === "moe" || anchor === "moe_revised"
            ? ["moe", "moe_revised"]
            : [anchor];
    const turnOn = !enabled.has(anchor);
    for (const name of anchors) {
        if (turnOn) enabled.add(name);
        else enabled.delete(name);
    }
    // Re-render in place. The chip order stays fixed, so only the on/off state
    // and the shown definitions change.
    fillEntryRow(row, entry);
}

// True when any text shown for this entry uses an IDS (⿰) description.
function hasIdsChars(entry) {
    if ((entry.simplified || "").includes("⿰") || (entry.traditional || "").includes("⿰")) {
        return true;
    }
    for (const source of entry.definitions || []) {
        for (const text of source.defs || []) {
            if (text.includes("⿰")) return true;
        }
        for (const group of source.groups || []) {
            for (const text of group.senses || []) {
                if (text.includes("⿰")) return true;
            }
        }
    }
    for (const form of entry.forms || []) {
        if ((form.traditional || "").includes("⿰")) return true;
    }
    return false;
}

// An IDS headword shown as its Commons image(s), since no font renders it as a
// Unicode character. The reading still sits above it.
function idsHeadword(text, reading, images, entry) {
    const node = document.createElement("span");
    node.className = "ids-headword";
    const syllables = pinyinSyllables(reading || "");
    if (syllables.length) {
        const row = document.createElement("span");
        row.className = "reading";
        const tokens =
            state.phonetic === "zhuyin"
                ? phoneticTokens(syllables, {
                      allowTones: !entry.pinyin_no_tone,
                  })
                : null;
        const readings = tokens ? tokens.map((token) => token.text) : syllables;
        row.append(
            readingContent(
                readings,
                syllables,
                state.toneColoring && !entry.pinyin_no_tone,
                tokens ? tokens.map((token) => token.kind) : null,
            ),
        );
        node.append(row);
    }
    const imageRow = document.createElement("span");
    imageRow.className = "ids-images";
    // Some headwords mix a normal character with an IDS part (搞 + ⿰车匡);
    // the Commons image only covers the IDS part, so keep the readable prefix.
    const idsStart = text.search(/[\u2ff0-\u2fff]/);
    const prefix = idsStart > 0 ? text.slice(0, idsStart) : "";
    if (prefix) {
        const prefixNode = document.createElement("span");
        prefixNode.className = "ids-prefix";
        prefixNode.textContent = prefix;
        imageRow.append(prefixNode);
    }
    images.forEach((url, i) => {
        const glyph = document.createElement("span");
        glyph.className = "ids-glyph";
        if (!entry.pinyin_no_tone && state.toneColoring && syllables.length) {
            const syllable = syllables[Math.min(i, syllables.length - 1)];
            glyph.classList.add(`tone${toneOf(syllable)}`);
        }
        glyph.style.setProperty("--ids-image", `url("${url}")`);
        glyph.setAttribute("role", "img");
        glyph.setAttribute("aria-label", text);
        imageRow.append(glyph);
    });
    node.append(imageRow);
    return node;
}

// HSK 3.0 groups levels 7, 8 and 9 into a single "7-9" band.
function formatHskLevel(level) {
    return level >= 7 ? "7-9" : String(level);
}

// The note label for a CEDICT variant. Plain variants stay "Other forms",
// while qualified ones (e.g. an "old variant of 柿") get their own label so
// they are not confused with ordinary forms.
function formNoteLabel(qualifier) {
    const text = (qualifier || "").trim();
    if (!text) return "Other forms";
    return `${text.charAt(0).toUpperCase()}${text.slice(1)} variant`;
}

// "Other forms" rows for the forms that belong to one reading. They are grouped
// by the CEDICT qualifier so "Old variant: 柹" is not lumped in with plain
// "Other forms". Returned as an array so the caller can place them under the
// matching reading heading.
function formsRows(forms, sectionReading = null) {
    const rows = [];
    const groups = new Map();
    for (const form of forms) {
        const label = formNoteLabel(form.qualifier);
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push(form);
    }
    for (const [label, items] of groups) {
        const fragment = document.createDocumentFragment();
        let hasFurigana = false;
        items.forEach((form, i) => {
            if (i) fragment.append(", ");
            // Shown as-is, without the traditional/simplified toggle.
            const word = form.traditional;
            if (!word) {
                if (form.pinyin) fragment.append(form.pinyin);
                return;
            }
            // The reading heading already gives the reading, so repeat it above
            // the form only when this form is pronounced differently.
            const differs =
                sectionReading !== null &&
                form.pinyin &&
                readingKey(form.pinyin) !== readingKey(sectionReading);
            if (differs) {
                hasFurigana = true;
                fragment.append(searchLink(word, furigana(word, form.pinyin)));
            } else {
                fragment.append(searchLink(word, toneColoredText(word, form.pinyin)));
            }
        });
        const row = detailRow(`${label}: `, fragment);
        // Furigana in the note also needs room above it, or the reading can
        // overlap the last definition line.
        if (hasFurigana) row.classList.add("has-furigana");
        rows.push(row);
    }
    return rows;
}

// Heading above one traditional spelling's definitions in a merged row: its
// reading and character (e.g. hòu / 後 above the 後 senses).
// Heading for one traditional spelling inside a dictionary block: its reading
// and character (e.g. gān / 乾 above that spelling's senses).
function variantTitleNode(variant) {
    const title = document.createElement("div");
    title.className = "variant-title";
    title.append(
        furigana(maskGlyphs(variant.traditional || ""), variant.reading || "", true),
    );
    return title;
}

// Merge several blocks of one dictionary into one, combining groups with the
// same reading and dropping repeated senses. Used for CC-CEDICT, whose senses
// are stored under the simplified headword, so its traditional variants share
// rows and cannot be separated reliably.
function mergeSharedGroups(blocks, headword = "") {
    const groups = [];
    const classifiers = [];
    for (const block of blocks) {
        const blockGroups =
            block.groups && block.groups.length
                ? block.groups
                : [{ pinyin: "", senses: block.defs || [] }];
        // The spelling these senses are written on. The row's own headword is
        // never labelled; any other spelling is shown after its reading, e.g.
        // "zhī (秖)" above grain and "zhī (隻)" above the classifier sense.
        const spelling = block.traditional || "";
        const label = spelling && spelling !== headword ? spelling : "";
        for (const group of blockGroups) {
            const reading = group.pinyin || "";
            const senses = group.senses || [];
            // Spellings that say exactly the same thing under the same reading
            // (制 and 製 sharing a pool) share one group.
            const same = groups.find(
                (g) =>
                    g.pinyin === reading &&
                    g.senses.length === senses.length &&
                    g.senses.every((sense, i) => sense === senses[i]),
            );
            if (same) {
                if (label && !same.spellings.includes(label)) same.spellings.push(label);
                continue;
            }
            groups.push({
                pinyin: reading,
                senses: [...senses],
                spellings: label ? [label] : [],
            });
        }
        if (block.classifiers && block.classifiers.length) {
            for (const classifier of block.classifiers) {
                const key = JSON.stringify(classifier);
                if (!classifiers.some((item) => JSON.stringify(item) === key)) {
                    classifiers.push(classifier);
                }
            }
        }
    }
    // Same reading next to each other (zhǐ, zhī, zhī (秖)...) keeps the headings tidy.
    const order = [];
    for (const group of groups) if (!order.includes(group.pinyin)) order.push(group.pinyin);
    groups.sort((x, y) => order.indexOf(x.pinyin) - order.indexOf(y.pinyin));
    return { ...blocks[0], groups, defs: [], classifiers };
}

// Merge one dictionary's blocks for several traditional spellings into one
// block. The dictionary header stays single, and the first sense group of each
// spelling carries its own reading/character header. CEDICT stores its senses
// under the simplified headword, so its variants share identical rows; those are
// left as one block instead of repeating every definition.
function mergeVariantBlocks(blocks) {
    const signature = (block) =>
        JSON.stringify(
            (block.groups && block.groups.length
                ? block.groups
                : [{ pinyin: "", senses: block.defs || [] }]
            ).map((group) => [group.pinyin || "", group.senses || []]),
        );
    const first = signature(blocks[0]);
    if (blocks.every((block) => signature(block) === first)) return blocks[0];

    const base = blocks[0];
    const groups = [];
    const classifiers = [];
    for (const block of blocks) {
        const variant = {
            traditional: block.traditional ?? "",
            reading: block.reading ?? "",
        };
        const blockGroups =
            block.groups && block.groups.length
                ? block.groups
                : [{ pinyin: "", senses: block.defs || [] }];
        blockGroups.forEach((group, index) => {
            groups.push({
                pinyin: group.pinyin || "",
                senses: [...(group.senses || [])],
                ...(index === 0 ? { variantTitle: variant } : {}),
            });
        });
        if (block.classifiers && block.classifiers.length) {
            classifiers.push(...block.classifiers);
        }
    }
    return { ...base, groups, defs: [], classifiers };
}

// Merge 國語辭典簡編本 and 重編國語辭典修訂本 into one block. The concise
// dictionary supplies the header and comes first; comprehensive senses are
// appended unless the exact same trimmed definition is already present.
function mergeMoeDictionaries(concise, revised) {
    const groups = [];
    const seen = new Set();
    const sameTitle = (a, b) =>
        JSON.stringify(a.variantTitle || null) === JSON.stringify(b.variantTitle || null);
    for (const block of [concise, revised]) {
        const blockGroups =
            block.groups && block.groups.length
                ? block.groups
                : [{ pinyin: "", senses: block.defs || [] }];
        for (const group of blockGroups) {
            const senses = [];
            for (const text of group.senses || []) {
                const trimmed = String(text || "").trim();
                if (!trimmed || seen.has(trimmed)) continue;
                seen.add(trimmed);
                senses.push(trimmed);
            }
            if (!senses.length) continue;
            const target = groups.find(
                (item) =>
                    (item.pinyin || "") === (group.pinyin || "") &&
                    sameTitle(item, group),
            );
            if (target) {
                target.senses.push(...senses);
            } else {
                groups.push({ ...group, senses });
            }
        }
    }
    const classifiers = [];
    for (const block of [concise, revised]) {
        for (const classifier of block.classifiers || []) {
            const key = JSON.stringify(classifier);
            if (!classifiers.some((item) => JSON.stringify(item) === key)) {
                classifiers.push(classifier);
            }
        }
    }
    return {
        ...concise,
        groups,
        defs: groups.flatMap((group) => group.senses),
        classifiers,
    };
}

// Male-voice pronunciation player shared by every speaker button.
let audioPlayer = null;
let activeAudioButton = null;

function speakerIcon() {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const cone = document.createElementNS(ns, "path");
    cone.setAttribute("d", "M4 9.5h4l5-4v13l-5-4H4z");
    cone.setAttribute("fill", "currentColor");
    const waves = [
        "M16 8.5a5 5 0 0 1 0 7",
        "M18.5 6a8 8 0 0 1 0 12",
    ].map((d) => {
        const path = document.createElementNS(ns, "path");
        path.setAttribute("d", d);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "currentColor");
        path.setAttribute("stroke-width", "1.8");
        path.setAttribute("stroke-linecap", "round");
        return path;
    });
    svg.append(cone, ...waves);
    return svg;
}

function playEntryAudio(button) {
    const src = button.dataset.audio;
    if (!src) return;
    if (!audioPlayer) {
        audioPlayer = new Audio();
        audioPlayer.addEventListener("ended", () => {
            if (activeAudioButton) activeAudioButton.classList.remove("playing");
            activeAudioButton = null;
        });
        audioPlayer.addEventListener("error", () => {
            if (activeAudioButton) activeAudioButton.hidden = true;
            activeAudioButton = null;
        });
    }
    if (activeAudioButton === button && !audioPlayer.paused) {
        audioPlayer.pause();
        audioPlayer.currentTime = 0;
        button.classList.remove("playing");
        activeAudioButton = null;
        return;
    }
    if (activeAudioButton) activeAudioButton.classList.remove("playing");
    activeAudioButton = button;
    audioPlayer.src = src;
    audioPlayer
        .play()
        .then(() => button.classList.add("playing"))
        .catch(() => {
            // No file for this reading: hide the icon.
            button.hidden = true;
            activeAudioButton = null;
        });
}

// Build one result row for an entry.
function entryRow(entry) {
    const row = document.createElement("div");
    row.className = "entry";
    fillEntryRow(row, entry);
    return row;
}

// (Re)fill a result row: the headword, the source chips (as toggles), and the
// definitions of each dictionary toggled on for this row.
function fillEntryRow(row, entry) {
    row.replaceChildren();
    const primary = state.script === "traditional" ? entry.traditional : entry.simplified;
    // Glyph references ("&8ff0._104_0.gif;") must count as one character for
    // layout, so mask them before furigana() splits the headword per char.
    // renderGlyphs() at the end swaps the placeholders back to pictures.
    const primaryMasked = maskGlyphs(primary);
    // A 5+ character headword no longer fits the steady 10rem headword column;
    // stack its definitions underneath instead of pushing them to the right.
    const longHeadword = [...primaryMasked].length >= 5;
    row.classList.toggle("long-headword", longHeadword);
    const reading = state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;

    // The headword, frequency and source chips form one block, so the frequency
    // stays directly under the headword even when the definitions are long.
    const head = document.createElement("div");
    head.className = "entry-head";

    const hanzi = document.createElement("span");
    hanzi.className = "hanzi";
    if (entry.ids_images && entry.ids_images.length) {
        hanzi.append(idsHeadword(primary, reading, entry.ids_images, entry));
    } else {
        hanzi.append(
            furigana(
                primaryMasked,
                reading,
                !entry.pinyin_no_tone,
                entry.pinyin_label === "Dungan"
            )
        );
    }
    // Double-clicking the reading line or the character line selects only that
    // line. A double-click elsewhere still selects the whole headword.
    hanzi.addEventListener("dblclick", (event) => {
        const group = hanzi.querySelector(".furigana");
        const selection = window.getSelection();
        if (!selection) return;
        clearFuriganaSelection();
        const reading = event.target.closest?.(".reading");
        const char = event.target.closest?.(".char");
        let target = group || hanzi;
        if (group && reading) target = reading;
        else if (group && char) target = char;
        else hanzi.classList.add("word-selected");
        const range = document.createRange();
        range.selectNodeContents(target);
        selection.removeAllRanges();
        selection.addRange(range);
        event.preventDefault();
    });
    head.append(hanzi);
    if (entry.audio) {
        const audio = document.createElement("button");
        audio.type = "button";
        audio.className = "audio-button";
        audio.dataset.audio = entry.audio;
        audio.setAttribute("aria-label", "Play pronunciation (male voice)");
        audio.title = "Play pronunciation";
        audio.append(speakerIcon());
        audio.addEventListener("click", () => playEntryAudio(audio));
        head.append(audio);
    }
    // A dialect reading (Xiang, Wu, Shanghainese) is tagged next to the word.
    if (entry.pinyin_label) {
        const label = document.createElement("span");
        label.className = "dialect-label";
        label.textContent = entry.pinyin_label;
        head.append(label);
    }
    // Frequency and HSK levels: chip-like boxes under the headword, above the
    // dictionary source chips.
    if (entry.freq || entry.hsk) {
        const meta = document.createElement("div");
        meta.className = "freq";
        if (entry.freq) {
            const chip = document.createElement("span");
            chip.className = "source";
            chip.textContent = `Frequency: #${entry.freq.rank.toLocaleString()}`;
            chip.title = FREQ_TOOLTIP;
            meta.append(chip);
        }
        if (entry.hsk) {
            // Show the latest available level: 2026 syllabus, then HSK 3.0.
            const level = entry.hsk.newest ?? entry.hsk.new;
            if (level) {
                const chip = document.createElement("span");
                chip.className = "source hsk";
                chip.textContent = `HSK ${formatHskLevel(level)}`;
                chip.title = entry.hsk.newest
                    ? `2026 syllabus: HSK ${formatHskLevel(entry.hsk.newest)}`
                    : `HSK 3.0: HSK ${formatHskLevel(entry.hsk.new)}`;
                meta.append(chip);
            }
        }
        head.append(meta);
    }

    const enabled = visibleDicts(entry);
    // The selected language's dictionaries always come first in the entry.
    // Within a language, the order comes from the draggable dictionary list.
    const langGroup = selectedLangGroup();
    const langOrder = new Map();
    for (const source of entry.definitions || []) {
        if (!langOrder.has(source.lang)) langOrder.set(source.lang, langOrder.size);
    }
    // Variant order: traditional spellings that share a simplified headword.
    const variantOrder = new Map();
    for (const source of entry.definitions || []) {
        const key = source.traditional ?? "";
        if (!variantOrder.has(key)) variantOrder.set(key, variantOrder.size);
    }
    const definitions = [...(entry.definitions || [])].sort((a, b) => {
        const aFirst = a.lang === langGroup ? 1 : 0;
        const bFirst = b.lang === langGroup ? 1 : 0;
        if (aFirst !== bFirst) return bFirst - aFirst;
        const aLang = langOrder.get(a.lang) ?? Number.MAX_SAFE_INTEGER;
        const bLang = langOrder.get(b.lang) ?? Number.MAX_SAFE_INTEGER;
        if (aLang !== bLang) return aLang - bLang;
        return dictRank(a.anchor, a.lang) - dictRank(b.anchor, b.lang);
    });

    // Source chips are toggles: they are lit yellow when on, but stay in a
    // stable order so clicking one does not make the row reshuffle. A small
    // header names the row, sitting below the frequency/HSK chips.
    if (definitions.length && (state.customDicts || exactMode)) {
        const label = document.createElement("div");
        label.className = "sources-label";
        label.textContent = "Dictionaries";
        head.append(label);

        const box = document.createElement("div");
        box.className = "sources";
        const chipAnchors = new Set();
        for (const source of definitions) {
            if (chipAnchors.has(source.anchor)) continue;
            // The revised MOE source shares the visible 國語辭典 chip whenever
            // this entry has both sources, whether the merged chip is on or off.
            if (
                source.anchor === "moe_revised" &&
                definitions.some((item) => item.anchor === "moe")
            ) {
                continue;
            }
            chipAnchors.add(source.anchor);
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = enabled.has(source.anchor) ? "source on" : "source";
            chip.dataset.anchor = source.anchor;
            chip.textContent =
                source.anchor === "moe" ? "重編國語辭典修訂本" : source.label;
            chip.addEventListener("click", () => toggleEntryDict(row, entry, source.anchor));
            box.append(chip);
        }
        head.append(box);
    }

    // Definitions and their notes go in their own block, beside the headword on
    // wide screens and below it on narrow ones.
    const defsBox = document.createElement("div");
    defsBox.className = "entry-defs";

    // "Taiwan pr." / "also pr." / variant-form notes (all CC-CEDICT data), shown
    // only while CC-CEDICT is on for this row, right after its block.
    const notes = [];
    if (enabled.has("cedict")) {
        const prons = extractPronunciations(entry.english_normalized);
        // With Taiwan readings already active, the separate Taiwan note is
        // redundant, so hide it.
            if (!state.taiwan) {
                for (const note of prons.taiwan) {
                    notes.push(detailRow("Taiwan pronunciation: ", furigana(primaryMasked, numericToMarked(note))));
                }
            }
            for (const note of prons.others) {
                // In Taiwan mode, a "colloquial" note that repeats the Taiwan
                // reading already shown as the headword is redundant (e.g. 胺:
                // Taiwan ān, "colloquial pr. [an1]"). Hide just that duplicate;
                // other registers keep their note.
                const register = (note.label || "").toLowerCase();
                const duplicateTaiwan =
                    state.taiwan &&
                    Boolean(entry.pinyin_taiwan) &&
                    (register === "colloquial" || register === "coll") &&
                    numericToMarked(note.reading) === reading;
                if (duplicateTaiwan) continue;
                const label = note.label ? `Also pronounced (${note.label}): ` : "Also pronounced: ";
                notes.push(detailRow(label, furigana(primaryMasked, numericToMarked(note.reading))));
            }
    }

    // One block per dictionary. When several traditional spellings share a
    // headword, their reading/character headers go inside that block, after the
    // dictionary name, so the dictionary header is not repeated.
    const byAnchor = new Map();
    for (const source of definitions) {
        if (!enabled.has(source.anchor)) continue;
        if (!byAnchor.has(source.anchor)) byAnchor.set(source.anchor, []);
        byAnchor.get(source.anchor).push(source);
    }
    // Collapse one dictionary's variant blocks first, then merge the two MOE
    // dictionaries into a single block with the concise definitions on top.
    const blockFor = (anchor, blocks) => {
        blocks.sort(
            (a, b) =>
                (variantOrder.get(a.traditional ?? "") ?? 0) -
                (variantOrder.get(b.traditional ?? "") ?? 0),
        );
        return blocks.length > 1
            ? anchor === "cedict"
                ? mergeSharedGroups(blocks, displayWord(entry))
                : mergeVariantBlocks(blocks)
            : blocks[0];
    };
    const mergedMoe =
        byAnchor.has("moe") && byAnchor.has("moe_revised")
            ? mergeMoeDictionaries(
                  blockFor("moe", byAnchor.get("moe")),
                  blockFor("moe_revised", byAnchor.get("moe_revised")),
              )
            : null;
    let notesPlaced = false;
    let mergedMoeDrawn = false;
    for (const [anchor, blocks] of byAnchor) {
        if (mergedMoe && (anchor === "moe" || anchor === "moe_revised")) {
            if (!mergedMoeDrawn) {
                defsBox.append(
                    definitionsBlock(mergedMoe, entry.taiwan_refs, primary, reading, entry.forms || []),
                );
                mergedMoeDrawn = true;
            }
            continue;
        }
        const source = blockFor(anchor, blocks);
        defsBox.append(definitionsBlock(source, entry.taiwan_refs, primary, reading, entry.forms || []));
        if (anchor === "cedict" && notes.length) {
            defsBox.append(...notes);
            notesPlaced = true;
        }
    }
    if (notes.length && !notesPlaced) defsBox.append(...notes);

    // The headword shows one dialect's reading above it. The other dialects are
    // notes below the definitions, e.g. "Southern Min: im¹ bo⁵鹰婆".
    if (entry.readings && entry.readings.length > 1) {
        for (const extra of entry.readings.slice(1)) {
            const word = document.createElement("span");
            word.className = "dialect-reading";
            // Show the reading above the headword like furigana. allowTones is
            // false: this is a dialect romanization, not Hanyu Pinyin.
            word.append(
                furigana(
                    primaryMasked,
                    extra.pinyin,
                    false,
                    (extra.label || "") === "Dungan"
                )
            );
            defsBox.append(detailRow(`${extra.label || "Reading"}:`, word));
        }
    }

    // Traditional spellings merged into this row from the same simplified form.
    const mergedForms = entry.mergedForms || [];
    if (mergedForms.length) {
        const line = document.createElement("div");
        line.className = "def-source-cls";
        const label = document.createElement("span");
        label.className = "def-source-cls-label";
        label.textContent = "Traditional forms:";
        line.append(label, document.createTextNode(" "));
        mergedForms.forEach((form, index) => {
            if (index) line.append(document.createTextNode(", "));
            const word = form.traditional ?? form;
            const pinyin = form.pinyin || "";
            // No furigana here: the forms are not under a reading heading.
            // Tone colour still shows how each one is pronounced. The forms
            // are other words, so each is a link that searches it.
            line.append(searchLink(word, toneColoredText(word, pinyin)));
        });
        defsBox.append(line);
    }

    // A result kept only because nothing else matched has no definitions, but it
    // should still show which dictionary lists it (with the usual source link).
    if (!definitions.length && entry.sources && entry.sources.length) {
        for (const source of entry.sources) {
            defsBox.append(
                definitionsBlock(
                    {
                        anchor: source.anchor,
                        label: source.label,
                        groups: [],
                        defs: [],
                        classifiers: [],
                    },
                    entry.taiwan_refs,
                    primary,
                    reading,
                ),
            );
        }
    }

    if (hasIdsChars(entry)) {
        const note = document.createElement("p");
        note.className = "ids-note";
        note.textContent =
            "This entry contains characters that cannot be rendered with Unicode.";
        defsBox.append(note);
    }

    if (definitions.length || (entry.sources && entry.sources.length)) {
        const actions = document.createElement("div");
        actions.className = "entry-actions";
        // The details page is already open, so it does not link to itself.
        if (!exactMode) {
            // Open the word in the same tab, using the normal search page so all
            // of the entry's definitions and toggles stay available.
            const details = document.createElement("a");
            details.className = "report-link details-link";
            details.href = `/word/${encodeURIComponent(
                displayWord(entry) || entry.simplified || entry.traditional
            )}`;
            details.textContent = "Details";
            actions.append(details);
        }
        const report = document.createElement("button");
        report.type = "button";
        report.className = "report-link";
        report.textContent = "Report entry";
        report.addEventListener("click", () => openReportDialog(entry));
        actions.append(report);
        defsBox.append(actions);
    }

    row.append(head, defsBox);
    // Swap any glyph references (raw in definitions, placeholders in the
    // headword) for the tinted MOE pictures. See static/glyphs.js.
    if (typeof window.renderGlyphs === "function") window.renderGlyphs(row);
}

// --- Bug report dialog --------------------------------------------------

// Snapshot the display settings with a report. The reports page turns these
// into URL parameters so clicking a reported word restores the same view.
function currentReportSettings() {
    return {
        script: state.script,
        toneColoring: state.toneColoring,
        phonetic: state.phonetic,
        taiwan: state.taiwan,
        customDicts: state.customDicts,
        compact: state.compact,
        lang: state.lang,
        selectedDictionaries: selectedAnchorsParam().split(",").filter(Boolean),
    };
}

function updateReportFields() {
    const kind = document.getElementById("report-kind")?.value;
    const suggestion = kind === "suggestion";
    const wordField = document.getElementById("report-word-field");
    const messageLabel = document.getElementById("report-message-label");
    const message = document.getElementById("report-message");
    const note = document.getElementById("report-note");
    // Only entry-specific "Incorrect information" reports need the word.
    if (wordField) wordField.hidden = kind !== "incorrect";
    if (messageLabel) messageLabel.hidden = suggestion;
    // Suggestions do not include the debug log.
    if (note) note.hidden = suggestion;
    if (message) {
        message.placeholder = suggestion ? "Details" : "Describe the issue";
        message.setAttribute(
            "aria-label",
            suggestion ? "Details" : "Describe the issue"
        );
    }
}

document
    .getElementById("report-kind")
    ?.addEventListener("change", updateReportFields);

function openReportDialog(entry) {
    const dialog = document.getElementById("report-dialog");
    if (!dialog) return;
    const kind = document.getElementById("report-kind");
    const word = document.getElementById("report-word");
    const message = document.getElementById("report-message");
    const status = document.getElementById("report-status");
    const submit = document.getElementById("report-submit");
    const cancel = document.getElementById("report-cancel");
    if (kind) kind.value = "incorrect";
    updateReportFields();
    if (word) word.value = displayWord(entry) || "";
    if (message) message.value = "";
    if (status) {
        status.hidden = true;
        status.textContent = "";
        status.classList.remove("error");
    }
    if (submit) {
        submit.hidden = false;
        submit.disabled = false;
        submit.textContent = "Send report";
    }
    if (cancel) cancel.textContent = "Cancel";
    if (!dialog.open) dialog.showModal();
    // Do not focus the message box here: on phones that pops the on-screen
    // keyboard immediately. The dialog's own first control gets focus instead.
}

const reportForm = document.getElementById("report-form");
if (reportForm) {
    reportForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const status = document.getElementById("report-status");
        const submit = document.getElementById("report-submit");
        const cancel = document.getElementById("report-cancel");
        const kind = document.getElementById("report-kind")?.value || "incorrect";
        const word = document.getElementById("report-word")?.value.trim() || "";
        const message =
            document.getElementById("report-message")?.value.trim() || "";
        const payload = {
            kind,
            word: kind === "incorrect" ? word : "",
            message,
            url: location.href,
            user_agent: navigator.userAgent,
            log: kind === "suggestion" ? "" : debugSnapshot(),
            settings: currentReportSettings(),
        };
        if (status) {
            status.hidden = false;
            status.classList.remove("error");
            status.textContent = "Sending report...";
        }
        if (submit) submit.disabled = true;
        try {
            const response = await fetch("/api/report", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!response.ok) {
                throw new Error(await response.text());
            }
            if (status) {
                status.classList.remove("error");
                status.textContent =
                    kind === "suggestion"
                        ? "Suggestions/Requests sent. Thank you."
                        : "Report sent. Thank you. The debug log was included.";
            }
            // Once the report is sent, the only action left is Close.
            if (submit) submit.hidden = true;
            if (cancel) {
                cancel.textContent = "Close";
                cancel.focus();
            }
        } catch (error) {
            console.error("Report send failed:", error);
            if (status) {
                status.classList.add("error");
                status.textContent =
                    "Could not send the report. Please try again.";
            }
            if (submit) {
                submit.hidden = false;
                submit.disabled = false;
                submit.textContent = "Send report";
            }
        }
    });
}

document.getElementById("report-cancel")?.addEventListener("click", () => {
    document.getElementById("report-dialog")?.close();
});

// Clear the custom double-click selection from every headword.
function clearFuriganaSelection() {
    document.querySelectorAll(".hanzi.word-selected").forEach((node) => {
        node.classList.remove("word-selected");
    });
}

// A primary click elsewhere clears the double-click selection. Right-click is
// left alone so its Copy item still sees the selected row.
document.addEventListener("pointerdown", (event) => {
    if (event.button === 0) clearFuriganaSelection();
});

// A result is shown only when one of the selected dictionaries has a definition
// for it; a word found only in unselected dictionaries lives in the "Matches in
// other dictionaries" section instead.
function hasSelectedDefs(entry) {
    // Shown with the dictionaries that matched, but the match is outside the
    // selection, so it is not a selected-dictionary result.
    if (isOtherDefinitionMatch(entry)) return false;
    const enabled = entryDicts(entry);
    if ((entry.definitions || []).some((s) => enabled.has(s.anchor))) return true;
    // A "see X" variant has no senses - only "other forms" (CC-CEDICT) - but it
    // should still show when CC-CEDICT is on.
    return (entry.forms || []).length > 0 && enabled.has("cedict");
}

function hasAnyDefs(entry) {
    return (entry.definitions || []).length > 0 || (entry.forms || []).length > 0;
}

// The headword a result row shows, in the current script.
function displayWord(entry) {
    return state.script === "traditional" ? entry.traditional : entry.simplified;
}

// A deep-enough copy of an entry, so merging does not mutate the cached API data
// (render() is re-run from that data whenever a display option changes).
function cloneEntry(entry) {
    return {
        ...entry,
        sources: [...(entry.sources || [])],
        forms: [...(entry.forms || [])],
        mergedForms: [...(entry.mergedForms || [])],
        definitions: (entry.definitions || []).map((source) => cloneDefinition(source, entry)),
    };
}

// The reading shown for an entry (the Taiwan reading when Taiwan is on).
function entryReading(entry) {
    return state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;
}

// Copy one dictionary block and tag it with the traditional headword and reading
// of the entry it came from. Merged rows use these tags to keep each traditional
// spelling's definitions in its own group.
function cloneDefinition(source, entry) {
    return {
        ...source,
        // A server-merged row already tags each block with the spelling it came
        // from (只, 隻, 秖). Keep that tag; a raw row's block has none, so it
        // takes the row's own spelling.
        traditional: source.traditional || entry.traditional,
        reading: source.reading || entryReading(entry),
        groups: (source.groups || []).map((group) => ({
            pinyin: group.pinyin,
            senses: [...(group.senses || [])],
        })),
        defs: [...(source.defs || [])],
        classifiers: (source.classifiers || []).map((c) => ({ ...c })),
    };
}

// Main-list row count, used when the Fuzzy section adds more rows.
let mainResultCount = 0;

// Add one entry's definitions, source chips and forms to another entry.
function mergeEntry(target, addition) {
    if (
        !addition.main &&
        addition.traditional &&
        addition.traditional !== target.traditional &&
        !target.mergedForms.some(
            (form) => (form.traditional ?? form) === addition.traditional
        ) &&
        !(target.forms || []).some(
            (form) => form.traditional === addition.traditional
        )
    ) {
        target.mergedForms.push({
            traditional: addition.traditional,
            pinyin: addition.pinyin || "",
        });
    }
    for (const source of addition.definitions || []) {
        // Blocks are matched by dictionary and traditional spelling, so two
        // spellings that share a simplified headword keep separate definitions.
        const block = target.definitions.find(
            (s) => s.anchor === source.anchor && s.traditional === addition.traditional,
        );
        if (!block) {
            target.definitions.push(cloneDefinition(source, addition));
            continue;
        }
        for (const group of source.groups || []) {
            const existing = block.groups.find((g) => g.pinyin === group.pinyin);
            if (!existing) {
                block.groups.push({ pinyin: group.pinyin, senses: [...group.senses] });
                continue;
            }
            for (const sense of group.senses || []) {
                if (!existing.senses.includes(sense)) existing.senses.push(sense);
            }
        }
        for (const def of source.defs || []) {
            if (!block.defs.includes(def)) block.defs.push(def);
        }
        for (const cls of source.classifiers || []) {
            const key = JSON.stringify(cls);
            if (!block.classifiers.some((c) => JSON.stringify(c) === key)) {
                block.classifiers.push(cls);
            }
        }
    }
    for (const chip of addition.sources || []) {
        if (!target.sources.some((s) => s.anchor === chip.anchor)) target.sources.push(chip);
    }
    for (const form of addition.forms || []) {
        // Never list a row's own spelling as one of its "Other forms" (the 隻 row
        // lists 只 as a form; merged into the 只 row that would be 只 under 只).
        if (form.traditional === target.traditional) continue;
        if (!target.forms.some((f) => f.traditional === form.traditional)) {
            target.forms.push(form);
        }
    }
    if (!target.freq && addition.freq) target.freq = addition.freq;
}

// Collapse results that show the exact same headword into one row (e.g. 了 and
// 瞭 in simplified view, where both display as 了). Their definitions, readings,
// chips and other forms are combined.
function mergeEntries(entries) {
    // Simplified mode collapses everything that shares a simplified headword.
    // Traditional mode only merges an identical traditional + simplified pair,
    // so 隻/只, 秖/只 and 只/只 stay separate while the two 只/只 readings
    // still combine.
    const groups = new Map();
    for (const entry of entries) {
        const key =
            state.script === "traditional"
                ? `${entry.simplified || ""}\u0000${entry.traditional || ""}`
                : entry.simplified || entry.traditional || displayWord(entry);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(entry);
    }
    // The merged row is built on one entry and shows that entry's reading. Use the
    // entry whose reading best represents the headword (`reading_score`, set by
    // the server: most real senses, plus manual preferred readings), not whichever
    // row happened to arrive first. Otherwise 只 shows zhī (the classifier) just
    // because its row sorted ahead of zhǐ "only". Ties keep the server's order.
    const rows = [...groups.values()].map((group) => {
        let base = group[0];
        for (const entry of group) {
            if ((entry.reading_score ?? 0) > (base.reading_score ?? 0)) base = entry;
        }
        const merged = cloneEntry(base);
        for (const entry of group) {
            if (entry !== base) mergeEntry(merged, entry);
        }
        return merged;
    });
    return state.script === "traditional" ? mergeIdenticalRows(rows) : rows;
}

// Traditional mode keeps each spelling in its own row, but two spellings that
// simplify alike and have the same reading and exactly the same definitions
// (隻 and 秖, which CEDICT stores under one shared 只 entry) say the same thing
// twice. Fold those into the first one and add the other spelling to its
// "Other forms" list. Rows without any definition are never folded.
function mergeIdenticalRows(rows) {
    const bySignature = new Map();
    const out = [];
    for (const row of rows) {
        // Compare only the dictionaries this row actually shows. 隻 and 秖
        // share CEDICT's senses, but MOE/Wiktionary may have separate entries;
        // folding them while those are visible would hide real definitions.
        const visible = visibleDicts(row);
        const definitions = (row.definitions || [])
            .filter((block) => visible.has(block.anchor))
            .map((block) => [
                block.anchor,
                (block.groups || []).map((group) => [
                    group.pinyin || "",
                    group.senses || [],
                ]),
                block.defs || [],
            ]);
        const hasSenses = definitions.some(
            ([, groups, defs]) => groups.length || defs.length
        );
        if (!hasSenses) {
            out.push(row);
            continue;
        }
        // The signature deliberately ignores the traditional spelling: 隻 and
        // 秖 differ only by headword, not by what the visible dictionaries say.
        const signature = JSON.stringify([
            row.simplified || "",
            entryReading(row) || "",
            definitions,
        ]);
        const existing = bySignature.get(signature);
        if (existing) {
            mergeEntry(existing, row);
            // mergeEntry would list this row under "Traditional forms"; these
            // rows have the same definitions, so put the spelling in the
            // "Other forms" list instead.
            const merged = existing.mergedForms || [];
            const at = merged.findIndex(
                (form) => (form.traditional ?? form) === row.traditional
            );
            if (at >= 0) merged.splice(at, 1);
            if (
                row.traditional &&
                !existing.forms.some(
                    (form) => form.traditional === row.traditional
                )
            ) {
                existing.forms.push({
                    traditional: row.traditional,
                    simplified: row.simplified || existing.simplified || "",
                    pinyin: row.pinyin || "",
                    qualifier: "",
                });
            }
        } else {
            bySignature.set(signature, row);
            out.push(row);
        }
    }
    return out;
}

// How closely a result matches the query. The README defines six tiers:
// 1 exact, 2 starts with, 3 contains, 4 dialect reading, 5 reading without a
// definition, 6 no reading and no definition.
function matchTier(entry, query) {
    const text = (query || "").trim();
    if (!text) return null;
    if (CJK_RE.test(text)) {
        const words = [entry.simplified, entry.traditional].filter(Boolean);
        // A Chinese "*" / "?" pattern is matched as a pattern, not as text.
        if (isWildcardQuery(text)) return wildcardTier(words, text);
        if (words.some((word) => word === text)) return 2;
        if (words.some((word) => word.startsWith(text))) return 3;
        if (words.some((word) => word.includes(text))) return 4;
        return null;
    }
    const key = toToneless(text);
    if (!key) return null;
    // Rank on the reading the row SHOWS: with the Taiwan toggle that is the
    // Taiwan reading (the server matched and ordered by it too), not CEDICT's.
    const shown = state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;
    const reading = toToneless(shown || "");
    if (reading === key) return 2;
    if (reading.startsWith(key)) return 3;
    if (reading.includes(key)) return 4;
    return null;
}

// True when `word` matches the WHOLE of `pattern`. "*" stands for any run of
// characters (including none) and "?" for exactly one character. A copy of
// _glob_match() in db.py; keep the two in step. It walks both strings once and
// only falls back to the latest "*", so a pattern full of stars stays fast.
// Array.from splits by code point, so a rare character outside the Basic
// Multilingual Plane still counts as ONE character for "?".
function globMatch(pattern, word) {
    const pat = Array.from(pattern);
    const chars = Array.from(word);
    let p = 0;
    let w = 0;
    let star = -1; // index of the last "*" seen in the pattern
    let mark = 0; // where in the word that "*" currently stops swallowing
    while (w < chars.length) {
        if (p < pat.length && pat[p] === "*") {
            star = p;
            mark = w;
            p++;
        } else if (p < pat.length && (pat[p] === "?" || pat[p] === chars[w])) {
            p++;
            w++;
        } else if (star >= 0) {
            // Let the last "*" swallow one more character and try again.
            mark++;
            p = star + 1;
            w = mark;
        } else {
            return false;
        }
    }
    while (p < pat.length && pat[p] === "*") p++;
    return p === pat.length;
}

// Match tier 1-3 of a Chinese wildcard pattern (copy of _wildcard_tier in
// db.py): 1 the whole word fits the pattern, 2 the pattern fits the start of
// the word, 3 it fits somewhere later. Null when it fits nowhere. The best tier
// over the simplified and traditional spelling wins.
function wildcardTier(words, query) {
    const pattern = query.trim().replace(/＊/g, "*").replace(/？/g, "?").toLowerCase();
    let best = null;
    for (const word of words) {
        const lower = word.toLowerCase();
        // "start" is the whole-word match of "pattern + any ending"; "contains"
        // is "any start + pattern + any ending".
        const tiers = [
            [1, pattern],
            [2, pattern + "*"],
            [3, "*" + pattern + "*"],
        ];
        for (const [tier, glob] of tiers) {
            if (globMatch(glob, lower)) {
                best = best === null ? tier : Math.min(best, tier);
                break;
            }
        }
    }
    return best;
}

// True when any dictionary gives this entry a definition.
function hasDefinitionText(entry) {
    return (entry.definitions || []).some(
        (definition) =>
            (definition.defs || []).length ||
            (definition.groups || []).some((group) => (group.senses || []).length),
    );
}

// True for an unquoted "*" / "?" pattern query (full-width forms too). Mirrors
// completeInOneResponse() and the wildcard branch of main.py's api_search.
function isWildcardQuery(query) {
    const text = (query || "").trim();
    const quoted = text.length >= 2 && text.startsWith('"') && text.endsWith('"');
    return /[*?＊？]/.test(text) && !quoted;
}

function isBareWildcardQuery(query) {
    // "*" (or "**", "?") with no literal characters: the pattern fits every
    // headword, so match tiers 1-3 have nothing to separate.
    const text = (query || "").trim();
    return isWildcardQuery(text) && !text.replace(/[*?＊？]/g, "").trim();
}

function entryTier(entry, query) {
    // The server already ranked this row; use its tier so the page cannot
    // disagree with the order it was sent in.
    if (Number.isInteger(entry.tier)) return entry.tier;
    // A dialect-only reading is tier 4 even if it happens to match the query.
    if (entry.pinyin_label || entry.pinyin_no_tone) return 4;
    // A bare "*" fits every headword, so every ordinary result is tier 1.
    // Tiers 5 and 6 still apply: a word with no definition is demoted below
    // the words that have one.
    if (isBareWildcardQuery(query)) {
        return hasDefinitionText(entry) ? 1 : entry.pinyin ? 5 : 6;
    }
    // A pinyin wildcard search lists a pattern's matches in the server's order:
    // every result is tier 1, so every separator is drawn red (and the match
    // tiers and the definition / reading demotions below do not reorder
    // anything). A Chinese pattern (`?后`) is tiered like a normal search:
    // matchTier() sorts exact, starts-with and contains matches.
    if (isWildcardQuery(query) && !CJK_RE.test(query)) return 1;
    // A word with no definition anywhere is tier 5 (it has a reading) or tier 6
    // (it has neither), however well it matches: it must not outrank a word that
    // actually has a definition. Otherwise thousands of definition-less exact
    // readings would sit above real results.
    if (!hasDefinitionText(entry)) return entry.pinyin ? 5 : 6;
    // Tiers 1-3 are how well the headword or reading matches the query.
    const match = matchTier(entry, query);
    if (match) return match;
    const hasDefinitions = (entry.definitions || []).some(
        (definition) =>
            (definition.defs || []).length ||
            (definition.groups || []).some(
                (group) => (group.senses || []).length
            ),
    );
    if (entry.pinyin) return hasDefinitions ? 3 : 5;
    return hasDefinitions ? 3 : 6;
}

function entryCategory(entry) {
    // The server already ranked this row; use its category.
    if (Number.isInteger(entry.category)) return entry.category;
    // Category is the primary sort key, as the README says ("Results within
    // the same category are sorted by tier"):
    //   1. a definition in a selected dictionary,
    //   2. a definition only in a dictionary that is not selected,
    //   3. no definition anywhere.
    // So a category 1 dialect row (tier 4) beats a category 2 exact match
    // (tier 1).
    const definitions = entry.definitions || [];
    const hasText = (definition) =>
        (definition.defs || []).length ||
        (definition.groups || []).some(
            (group) => (group.senses || []).length
        );
    if (
        definitions.some(
            (definition) => state.dicts.has(definition.anchor) && hasText(definition)
        )
    ) {
        return 1;
    }
    if (definitions.some(hasText)) return 2;
    return 3;
}

// Highest tier among the main results, used to keep the Fuzzy section's
// separators below the official ones.
let lastResultTier = 0;

function addEntrySeparator(tier) {
    const separator = document.createElement("div");
    separator.className = "entry-separator";
    separator.dataset.tier = String(Number.isInteger(tier) ? tier : 1);
    separator.setAttribute("aria-hidden", "true");
    results.append(separator);
}

// The bottom separator follows the highest tier above it, like every separator
// between rows. With no results it stays neutral.
function updateFooterSeparator(tier) {
    if (!footerSeparator) return;
    if (Number.isInteger(tier) && tier >= 0) {
        footerSeparator.dataset.tier = String(tier);
    } else {
        delete footerSeparator.dataset.tier;
    }
}

// Build the results list from the array of entries returned by the API.
function render(entries, query) {
    currentEntries = entries;
    currentQuery = query;
    // A server-merged page is already one row per headword, in category/tier
    // order, so the browser must not merge or sort it again.
    const serverOrdered =
        currentLayouts !== null &&
        (entries === currentLayouts.simplified ||
            entries === currentLayouts.traditional);
    const merged = serverOrdered ? entries : mergeEntries(entries);
    // IDS headwords (⿰) are set aside and listed near the end, before the
    // definition-less entries.
    const hasIdsWord = (entry) =>
        (entry.simplified || "").includes("⿰") || (entry.traditional || "").includes("⿰");
    const idsEntries = merged.filter(hasIdsWord);
    const rest = merged.filter((entry) => !hasIdsWord(entry));
    // A dialect-only reading (Wu, Xiang, Shanghainese) has no Mandarin pinyin;
    // list those after the Mandarin results, but before headwords that only
    // have a reading.
    const isDialect = (entry) => Boolean(entry.pinyin_label || entry.pinyin_no_tone);
    const dialectEntries = rest.filter(isDialect);
    const mandarin = rest.filter((entry) => !isDialect(entry));
    const withReading = mandarin.filter((entry) => entry.pinyin);
    const withoutReading = mandarin.filter((entry) => !entry.pinyin);
    results.replaceChildren(); // clear previous results

    // With custom dictionaries off, results with definitions in the selected
    // language come first; results that only exist in other languages go to a
    // separate "Other Dictionaries" section below them.
    const otherMode = !state.customDicts;
    const isOther = (entry) =>
        otherMode && !hasSelectedDefs(entry) && hasAnyDefs(entry);
    const isBare = (entry) =>
        otherMode ? !hasAnyDefs(entry) : !hasSelectedDefs(entry);
    const withDefs = withReading.filter(hasSelectedDefs);
    const otherReading = withReading.filter(isOther);
    const headwordsOnly = withReading.filter(isBare);
    // No reading at all: still put ones with a definition above bare headwords.
    const withoutReadingWithDefs = withoutReading.filter(hasSelectedDefs);
    const otherNoReading = withoutReading.filter(isOther);
    const withoutReadingOnly = withoutReading.filter(isBare);
    const idsMain = idsEntries.filter((entry) => !isOther(entry));
    const idsOther = idsEntries.filter(isOther);
    const dialectMain = dialectEntries.filter((entry) => !isOther(entry));
    const dialectOther = dialectEntries.filter(isOther);
    const otherEntries = [
        ...otherReading,
        ...otherNoReading,
        ...idsOther,
        ...dialectOther,
    ];

    const count = document.createElement("p");
    count.className = "results-count";
    mainResultCount = merged.length;
    // The exact-word page has no result count; it shows a "Details" label in the
    // same slot and style as the search page's "N RESULTS" line.
    count.textContent = exactMode
        ? "Details"
        : `${merged.length} RESULT${merged.length === 1 ? "" : "S"}`;
    results.append(count);
    window.__debugRender = { merged: merged.length, withDefs: withDefs.length, otherReading: otherReading.length, headwordsOnly: headwordsOnly.length, withoutReadingWithDefs: withoutReadingWithDefs.length, withoutReadingOnly: withoutReadingOnly.length, otherEntries: otherEntries.length, idsMain: idsMain.length, dialectMain: dialectMain.length, customDicts: state.customDicts, dicts: [...state.dicts], compact: state.compact };
    if (!exactMode) updateTiming();

    let hasEntry = false;
    let lastTier = 0;
    const appendEntry = (entry) => {
        const tier = entryTier(entry, query);
        // The separator below a row shows that row's tier. Using the highest
        // tier so far breaks at the category boundary, where the tier can go
        // back down (category 1 tier 3 -> category 2 tier 1).
        if (hasEntry) addEntrySeparator(lastTier);
        results.append(entryRow(entry));
        hasEntry = true;
        lastTier = tier;
    };

    try {
        if (serverOrdered) {
            // The server already ordered and merged the rows. Walk them in
            // order and put the heading before the first other-dictionary row.
            let headingShown = false;
            for (const entry of merged) {
                if (!headingShown && entryCategory(entry) === 2) {
                    const heading = document.createElement("p");
                    heading.className = "other-heading";
                    heading.textContent = "Other Dictionaries";
                    results.append(heading);
                    headingShown = true;
                }
                appendEntry(entry);
            }
        } else {
            // Category is the primary key, then the six-level search tier.
            // Within one (category, tier) the section order above is kept
            // (definitions first), because the sort is stable.
            const ordered = [
                ...withDefs,
                ...withoutReadingWithDefs,
                ...idsMain,
                ...dialectMain,
                ...otherEntries,
                ...headwordsOnly,
                ...withoutReadingOnly,
            ];
            ordered.sort((a, b) => {
                const category = entryCategory(a) - entryCategory(b);
                return category || entryTier(a, query) - entryTier(b, query);
            });

            const otherSet = new Set(otherEntries);
            let otherHeadingShown = false;
            for (const entry of ordered) {
                if (!otherHeadingShown && otherSet.has(entry)) {
                    const heading = document.createElement("p");
                    heading.className = "other-heading";
                    heading.textContent = "Other Dictionaries";
                    results.append(heading);
                    otherHeadingShown = true;
                }
                appendEntry(entry);
            }
        }
    } catch (err) {
        window.__renderError = String(err);
        const errEl = document.createElement("p");
        errEl.textContent = "RENDER ERROR: " + String(err);
        results.append(errEl);
    }
    lastResultTier = lastTier;
    updateFooterSeparator(lastTier);
}

// Show the first-paint time in parentheses after the result count, e.g.
// "4 RESULTS (4 ms)". Hovering or focusing it reveals lag, first-results DB
// time, all-results DB time, and first paint.
function updateTiming() {
    const count = results.querySelector(".results-count");
    let timing = count ? count.querySelector(".results-timing") : null;
    if (!count || exactMode || lastSearchMs === null) {
        if (timing) timing.remove();
        return;
    }
    const visible = firstPaintMs !== null ? firstPaintMs : allPaintMs;
    if (visible === null) {
        if (timing) timing.remove();
        return;
    }
    if (!timing) {
        timing = document.createElement("span");
        timing.className = "results-timing";
        timing.tabIndex = 0;
    }
    // Keep one literal space between the count text and the timing span.
    const previous = timing.previousSibling;
    if (!previous || previous.nodeType !== 3 || previous.nodeValue !== " ") {
        count.append(document.createTextNode(" "));
    }
    count.append(timing);

    // Separate measurements: network lag, server time for the first tier,
    // server time for the complete query, then the cumulative first paint.
    const lines = [];
    if (lagMs !== null) {
        lines.push(`Lag: ${lagMs} ms`);
    }
    if (firstDbMs !== null) {
        lines.push(`First results DB: ${firstDbMs} ms`);
    }
    if (allDbMs !== null) {
        lines.push(`All results DB: ${allDbMs} ms`);
    }
    if (firstPaintMs !== null) {
        lines.push(`First paint: ${firstPaintMs} ms`);
    }

    const value = document.createElement("span");
    value.className = "timing-value";
    value.textContent = `(${visible} ms)`;
    const tooltip = document.createElement("span");
    tooltip.className = "timing-tooltip";
    tooltip.textContent = lines.join("\n");
    timing.replaceChildren(value, tooltip);
}

// Append the tone-insensitive "Fuzzy match" section under a separator.
function renderFuzzy(entries) {
    currentFuzzy = entries || [];
    if (!currentFuzzy.length) return;
    // The count covers both the main list and these fuzzy rows.
    const total = mainResultCount + currentFuzzy.length;
    const count = results.querySelector(".results-count");
    if (count && !exactMode) {
        count.textContent = `${total} RESULT${total === 1 ? "" : "S"}`;
        // Setting textContent removes the timing span; put it back.
        updateTiming();
    }
    const heading = document.createElement("p");
    heading.className = "fuzzy-heading";
    heading.textContent = "Fuzzy match";
    results.append(heading);
    let fuzzyHasEntry = false;
    let fuzzyTier = lastResultTier;
    for (const entry of currentFuzzy) {
        if (fuzzyHasEntry) addEntrySeparator(fuzzyTier || 3);
        results.append(entryRow(entry));
        fuzzyHasEntry = true;
        fuzzyTier = Math.max(fuzzyTier, entryTier(entry, currentQuery));
    }
    updateFooterSeparator(fuzzyTier);
}

// Re-run the current search from page 1. Used when something the SERVER ranks
// by changes (search language, selected dictionaries); a plain redraw() cannot
// reorder rows across pages. The timing line from the last search-bar search is
// kept, like the Taiwan toggle does.
function rerunSearch() {
    if (currentQuery && currentQuery.trim() !== "*") {
        search(currentQuery, 0, false, false);
    }
}

// Re-draw the current results (and any fuzzy section) after a display toggle.
// Both layouts from one API response, or null when the browser has to merge.
function serverLayouts(data) {
    if (!data || (data.script !== "simplified" && data.script !== "traditional")) {
        return null;
    }
    if (data.alt_same) {
        // No group on the page splits, so both scripts show the same rows.
        return {
            simplified: data.results,
            traditional: data.results,
        };
    }
    if (!Array.isArray(data.alt_results) || !data.alt_results.length) return null;
    if (data.script === "traditional") {
        return {
            simplified: data.alt_results,
            traditional: data.results,
        };
    }
    return {
        simplified: data.results,
        traditional: data.alt_results,
    };
}

function redraw() {
    // Re-render the parsed words too, so a phonetic/script/tone change applies to
    // them (they are otherwise only drawn once, in annotate()).
    if (currentTokens.length) renderParsed(currentTokens);
    if (!currentEntries.length && !currentFuzzy.length) return;
    // A server-merged page already has a layout for each script; use it instead
    // of re-merging the other script's rows.
    const layout = currentLayouts && currentLayouts[state.script];
    render(layout || currentEntries, currentQuery);
    renderFuzzy(currentFuzzy);
}

// Prefetch cache for next/previous pages: Map<"query|offset|taiwan", { first, full }>.
const pageCache = new Map();
// Keep the 3-before + current + 3-after window, plus headroom for a second
// query or a jump. Each parsed page is well under 100 KB.
const PAGE_CACHE_MAX = 16;
// How many pages on each side of the current page to warm in the background.
const PREFETCH_RADIUS = 3;
// In-flight prefetches, so a hover does not start a second request for a page
// the post-render prefetch already asked for.
const pagePrefetches = new Map();

function pageCacheKey(query, offset) {
    // Taiwan readings change the rows, so they get their own cache entry. So do
    // the search language (it picks the preview dictionary) and the selected
    // dictionaries (they decide each row's category and therefore the order).
    return `${query}|${offset}|${state.taiwan ? 1 : 0}|${state.lang}|${selectedAnchorsParam()}`;
}

// Store one page in the bounded cache, evicting the oldest entry if needed.
function cachePage(key, value) {
    if (pageCache.size >= PAGE_CACHE_MAX && !pageCache.has(key)) {
        const oldest = pageCache.keys().next().value;
        pageCache.delete(oldest);
    }
    pageCache.set(key, value);
}

// True when the server answers the query completely in one response: the
// all-headwords listing (blank / only "*") and unquoted wildcard patterns.
// Mirrors the routing in main.py's api_search.
function completeInOneResponse(query) {
    if (!query.trim().replace(/^\*+|\*+$/g, "")) return true;
    const quoted = query.length >= 2 && query.startsWith('"') && query.endsWith('"');
    return /[*?＊？]/.test(query) && !quoted;
}

// The dictionaries whose definitions put a result in category 1, as a sorted,
// comma-separated list of server anchors. This mirrors hasSelectedDefs(): with
// custom dictionaries it is exactly the dictionaries the user ticked; otherwise
// it is every dictionary of the selected search language (EN also counts
// Wiktionary, not only CC-CEDICT). Browser-imported dictionaries have no server
// anchor and are left out; they are merged on the client.
function selectedAnchorsParam() {
    // The drag handle moves the row in the DOM, so the checked rows there are
    // the order the user sees. Do not sort: the server uses this order for the
    // fast preview and it is part of any shared/report URL.
    return [...document.querySelectorAll(".dict-toggle")]
        .filter((toggle) => toggle.checked && anchorLang.has(toggle.dataset.anchor))
        .map((toggle) => toggle.dataset.anchor)
        .join(",");
}

// Build an /api/search URL, including the cache version when the page has one.
function searchUrl(query, offset) {
    let url = `/api/search?q=${encodeURIComponent(query)}&taiwan=${state.taiwan ? 1 : 0}&offset=${offset}&lang=${encodeURIComponent(state.lang)}&script=${state.script}&merge=1`;
    const dictsParam = selectedAnchorsParam();
    if (dictsParam) url += `&dicts=${encodeURIComponent(dictsParam)}`;
    if (exactMode) url += "&exact=1";
    if (DATA_VERSION) url += `&dv=${encodeURIComponent(DATA_VERSION)}`;
    return url;
}

// Fetch and parse one search tier. `silent` keeps prefetch out of the debug log.
async function fetchSearchPart(
    label,
    requestUrl,
    { priority = "high", silent = false } = {},
) {
    const fetchStart = performance.now();
    const response = await fetch(requestUrl, {
        priority,
        headers: { "X-Client-Sent": String(Date.now()) },
    });
    if (!silent) {
        recordPhase(`${label} fetch`, fetchStart, { status: response.status });
        recordServerDebug(label, response);
    }
    const parseStart = performance.now();
    const data = await response.json();
    const lagHeader = response.headers.get("x-yabai-lag");
    if (lagHeader !== null) {
        const lag = Number(lagHeader);
        if (Number.isFinite(lag)) data.lag = lag;
    }
    if (!silent) {
        recordPhase(`${label} parse`, parseStart, {
            results: Array.isArray(data.results) ? data.results.length : 0,
            ...(label === "full" ? { total: data.total } : {}),
        });
        recordNetworkTiming(`${label} network`, requestUrl);
    }
    return data;
}

// Prefetch a page's API responses in the background and cache them.
// The shapes follow completeInOneResponse():
//   - complete query: { first: <complete response>, full: null }
//   - later page:     { first: null, full: <complete response> }
//   - first page:     { first: <fast tier>, full: <complete response> }
function prefetchPage(query, offset) {
    if (offset < 0) return Promise.resolve();
    // Respect data-saver mode: the click still searches, it just is not warmed.
    if (navigator.connection?.saveData) return Promise.resolve();
    const key = pageCacheKey(query, offset);
    if (pageCache.has(key)) return Promise.resolve();
    const pending = pagePrefetches.get(key);
    if (pending) return pending;
    const promise = (async () => {
        try {
            const url = searchUrl(query, offset);
            if (completeInOneResponse(query)) {
                const complete = await fetchSearchPart("full", url, {
                    priority: "low",
                    silent: true,
                });
                cachePage(key, { first: complete, full: null, url });
                return;
            }
            if (offset > 0) {
                // Later pages only need the complete list.
                const full = await fetchSearchPart("full", url, {
                    priority: "low",
                    silent: true,
                });
                cachePage(key, { first: null, full, url });
                return;
            }
            const [first, full] = await Promise.all([
                fetchSearchPart("primary", url + "&part=primary", {
                    priority: "low",
                    silent: true,
                }),
                fetchSearchPart("full", url, { priority: "low", silent: true }),
            ]);
            cachePage(key, { first, full, url });
        } catch (error) {
            // Prefetch is best-effort.
        }
    })();
    pagePrefetches.set(key, promise);
    promise.finally(() => pagePrefetches.delete(key));
    return promise;
}

// Warm up to PREFETCH_RADIUS pages before and after the current page, so
// Next/Previous stay instant for a few clicks. Each side stops at the first or
// last page. prefetchPage dedupes in-flight requests and honours save-data.
function prefetchAround(query, offset, pageSize, totalResults) {
    for (let step = 1; step <= PREFETCH_RADIUS; step += 1) {
        const next = offset + step * pageSize;
        if (next < totalResults) prefetchPage(query, next);
        const previous = offset - step * pageSize;
        if (previous >= 0) prefetchPage(query, previous);
    }
}

// Query the API and hand the results to render().
// A monotonically increasing id per search, so a late response from an old
// query is ignored once the user has typed a new one.
let searchSeq = 0;

// A reload of a search page starts with an empty result area (the HTML is
// served before the results are rendered). index.html adds the
// "initial-search-pending" class before the first paint and style.css keeps the
// result area hidden while it is present. Drop it once the first complete
// render is in the DOM, so the page fills in once instead of visibly flashing
// empty -> partial -> final. Later searches reveal it immediately.
let initialResultsRevealed = !document.documentElement.classList.contains(
    "initial-search-pending",
);

function revealInitialResults() {
    if (initialResultsRevealed) return;
    initialResultsRevealed = true;
    document.documentElement.classList.remove("initial-search-pending");
}

async function search(query, offset = 0, scrollMode = false, recordTiming = true) {
    releaseClearScrollGuard();
    currentLayouts = null;
    const url = searchUrl(query, offset);
    const startedAt = performance.now();
    lagMs = null;
    firstDbMs = null;
    allDbMs = null;
    firstPaintMs = null;
    allPaintMs = null;
    const seq = ++searchSeq;
    const allWildcard = query.trim() === "*";
    lastWildcardCustom = null;
    beginSearchDebug(query, offset, seq);

    // Start the browser-side dictionary search immediately instead of waiting
    // for the API tiers. It can take seconds with large imported dictionaries,
    // so it must not delay the official results.
    const customMatchesPromise =
        !allWildcard && window.customDictionaryMatches
            ? window.customDictionaryMatches(query).catch((error) => {
                console.error("Custom dictionary search failed:", error);
                return [];
            })
            : Promise.resolve([]);

    // Phase 1: the fast CEDICT tier. Render it as soon as the server replies.
    // Wildcard / all-headword queries come complete in one response, so they
    // skip the second phase entirely.
    const twoPhase = !completeInOneResponse(query) && !exactMode;
    const primaryUrl = url + "&part=primary";
    const cacheKey = pageCacheKey(query, offset);
    // Use server-embedded primary results if available (avoids the fetch). The
    // embed is always mainland readings, so skip it when Taiwan is selected.
    const embeddedPrimary = (!state.taiwan && window.__initialPrimary && window.__initialPrimary.query === query && offset === 0 && window.__initialPrimaryLang === state.lang)
        ? window.__initialPrimary
        : null;
    if (embeddedPrimary) delete window.__initialPrimary;
    const cached = pageCache.get(cacheKey);
    const cachedFull = cached && cached.full ? cached.full : null;

    // Start the slow complete tier immediately. On the first page of a normal
    // search it runs alongside the fast tier (the server handles both
    // concurrently), instead of waiting for the fast tier to arrive first.
    let fullPromise = null;
    let fullSettled = false;
    if (twoPhase && !cachedFull) {
        fullPromise = fetchSearchPart("full", url);
        fullPromise.then(
            () => { fullSettled = true; },
            () => { fullSettled = true; },
        );
        // Handle the rejection even if this search is superseded before the
        // await below, so it never surfaces as an unhandled rejection. A failed
        // complete request also releases the result area right away.
        fullPromise.catch(() => revealInitialResults());
    }

    let first = null;
    if (embeddedPrimary) {
        first = embeddedPrimary;
    } else if (!twoPhase) {
        // One response already contains everything (wildcard / all-headwords).
        first = cached && cached.first ? cached.first : await fetchSearchPart("primary", url);
    } else if (offset === 0) {
        // First page: show the fast tier while the complete list is in flight.
        first = cached && cached.first ? cached.first : await fetchSearchPart("primary", primaryUrl);
    }
    // A later page only needs the complete list started above.
    if (seq !== searchSeq) return 0;
    if (currentDebugSearch && first) {
        currentDebugSearch.official = {
            total: first.total,
            offset: first.offset,
            pageSize: first.page_size,
            results: Array.isArray(first.results) ? first.results.length : 0,
            ms: first.ms,
        };
    }
    if (first) {
        recordDebug("response", "Primary search response received", null, {
            source: embeddedPrimary ? "embedded" : (cached && cached.first ? "pageCache" : "network"),
            serverMs: first.ms,
        });
    }
    entryDictState.clear(); // a fresh search starts from the global selection
    // Client wall-clock for both numbers. The server's first.ms is stale when
    // the response came from the page cache or the embedded primary.
    const firstMs = first ? Math.round(performance.now() - startedAt) : null;
    if (first) {
        if (typeof first.lag === "number") lagMs = first.lag;
        if (typeof first.ms === "number") firstDbMs = first.ms;
    }
    const showFirst =
        twoPhase &&
        first &&
        first.total == null &&
        offset === 0 &&
        first.results.length >= 1 &&
        !fullSettled &&
        !cachedFull;
    if (showFirst) {
        currentEntries = first.results;
        currentLayouts = serverLayouts(first);
        currentQuery = query;
        if (recordTiming) {
            lastSearchMs = firstMs;
            firstSearchMs = null;
        }
        const primaryRenderStart = performance.now();
        render(first.results, query);
        renderFuzzy([]);
        updateHint(query, first.pinyin_match, first.english_match);
        if (recordTiming) {
            // Cumulative from the start of the search, not just the render.
            firstPaintMs = Math.round(performance.now() - startedAt);
            updateTiming();
        }
        recordPhase("primary render", primaryRenderStart, {
            rows: first.results.length,
        });
    }

    // Keep the page number in the URL (without clobbering the query), so a
    // reload or a shared link returns to the same page.
    const size = (first && first.page_size) || RESULTS_PAGE_SIZE;
    const page = Math.floor(offset / size) + 1;
    const current = new URL(location.href);
    if (page > 1) current.searchParams.set("page", String(page));
    else current.searchParams.delete("page");
    current.searchParams.delete("offset"); // older links used a raw offset
    history.replaceState({ ...(history.state || {}), page }, "", current);

    if (twoPhase) {
        // Phase 2: the complete final list, slow source tiers included. On the
        // first page it was started before the fast tier, so this await usually
        // has little left to wait for.
        const full = cachedFull || (await fullPromise);
        if (seq !== searchSeq) return 0;
        if (!first && currentDebugSearch) {
            currentDebugSearch.official = {
                total: full.total,
                offset: full.offset,
                pageSize: full.page_size,
                results: Array.isArray(full.results) ? full.results.length : 0,
                ms: full.ms,
            };
        }
        recordDebug(
            "response",
            `Complete search response: ${full.results.length} results, total ${full.total}`
        );
        if (typeof full.lag === "number" && lagMs === null) lagMs = full.lag;
        if (typeof full.ms === "number") allDbMs = full.ms;
        if (firstDbMs === null) firstDbMs = allDbMs;

        // Show official results first; custom matches follow as a third tier.
        currentEntries = full.results;
        currentLayouts = serverLayouts(full);
        currentQuery = query;
        if (recordTiming) {
            firstSearchMs = first ? firstMs : null;
            lastSearchMs = Math.round(performance.now() - startedAt);
        }
        const fullRenderStart = performance.now();
        render(full.results, query);
        renderFuzzy(full.fuzzy);
        renderPagination(query, full);
        updateHint(query, full.pinyin_match, full.english_match);
        applyPagerScroll(scrollMode);
        revealInitialResults();
        if (recordTiming) {
            // Cumulative from the start of the search, not just the render.
            allPaintMs = Math.round(performance.now() - startedAt);
            // If the full response won the race and the fast tier was never
            // shown separately, its rows were painted as part of this render.
            if (firstPaintMs === null) firstPaintMs = allPaintMs;
            updateTiming();
        }
        recordPhase("full render", fullRenderStart, { rows: full.results.length });

        const customResult = await customMatchesPromise;
        if (seq !== searchSeq) return 0;
        const matches = Array.isArray(customResult)
            ? customResult
            : customResult.matches || [];
        const allHeadwordMatches = [...matches];
        const fastKeys = new Set(matches.map(customMatchKey));
        if (matches.length) {
            const customMergeStart = performance.now();
            await applyCustomMatches(full, matches, RESULTS_PAGE_SIZE);
            if (seq !== searchSeq) return 0;
            entryDictState.clear();
            currentEntries = full.results;
            currentQuery = query;
            if (recordTiming) {
                lastSearchMs = Math.round(performance.now() - startedAt);
            }
            render(full.results, query);
            renderFuzzy(full.fuzzy);
            renderPagination(query, full);
            updateHint(query, full.pinyin_match, full.english_match);
            applyPagerScroll(scrollMode);
            recordPhase("custom merge", customMergeStart, {
                rows: full.results.length,
                matches: matches.length,
            });
        }
        if (!Array.isArray(customResult) && customResult.slow) {
            const slow = await customResult.slow;
            if (seq !== searchSeq) return 0;
            const extra = slow.filter((match) => !fastKeys.has(customMatchKey(match)));
            if (extra.length) {
                const slowMergeStart = performance.now();
                allHeadwordMatches.push(...extra);
                await applyCustomMatches(full, extra, RESULTS_PAGE_SIZE);
                if (seq !== searchSeq) return 0;
                entryDictState.clear();
                currentEntries = full.results;
                currentQuery = query;
                if (recordTiming) {
                    lastSearchMs = Math.round(performance.now() - startedAt);
                }
                render(full.results, query);
                renderFuzzy(full.fuzzy);
                renderPagination(query, full);
                updateHint(query, full.pinyin_match, full.english_match);
                applyPagerScroll(scrollMode);
                recordPhase("slow custom merge", slowMergeStart, {
                    rows: full.results.length,
                    matches: extra.length,
                });
            }
        }

        // Add chunked pinyin and definition matches below the headword rows,
        // without moving the existing rows.
        const pinyin = await appendCustomChunked(
            full,
            query,
            allHeadwordMatches,
            seq,
            "pinyin"
        );
        if (seq !== searchSeq) return 0;
        if (pinyin.matches.length) {
            allHeadwordMatches.push(...pinyin.matches);
        }
        if (pinyin.added) {
            repaintMergedRows(
                full,
                query,
                recordTiming,
                startedAt,
                scrollMode
            );
        }

        const definitions = await appendCustomChunked(
            full,
            query,
            allHeadwordMatches,
            seq,
            "defs"
        );
        if (seq !== searchSeq) return 0;
        if (definitions.added) {
            repaintMergedRows(
                full,
                query,
                recordTiming,
                startedAt,
                scrollMode
            );
        }
        // Cache this page and prefetch adjacent pages for instant navigation.
        try {
            if (!cached) cachePage(cacheKey, { first, full, url });
            const pageSize = full.page_size || RESULTS_PAGE_SIZE;
            const totalResults = full.total || 0;
            prefetchAround(query, offset, pageSize, totalResults);
        } catch (cacheError) {
            // Prefetch caching is best-effort; never break the search.
        }

        return full.results.length;
    }

    // Wildcard results are complete already. Show the official rows, then merge
    // the local dictionary tier when it is ready.
    currentEntries = first.results;
    currentLayouts = serverLayouts(first);
    currentQuery = query;
    if (typeof first.lag === "number") lagMs = first.lag;
    if (typeof first.ms === "number") {
        firstDbMs = first.ms;
        allDbMs = first.ms;
    }
    if (recordTiming) {
        lastSearchMs = Math.round(performance.now() - startedAt);
        firstSearchMs = lastSearchMs;
    }
    render(first.results, query);
    renderFuzzy(first.fuzzy);
    renderPagination(query, first);
    updateHint(query, first.pinyin_match, first.english_match);
    applyPagerScroll(scrollMode);
    revealInitialResults();
    if (recordTiming) {
        // Cumulative from the start of the search, not just the render.
        allPaintMs = Math.round(performance.now() - startedAt);
        if (firstPaintMs === null) firstPaintMs = allPaintMs;
        updateTiming();
    }

    if (allWildcard && !window.customDictionaryWildcardPage) {
        recordDebug(
            "error",
            "Custom dictionary wildcard paging is unavailable (script did not load)."
        );
    }
    if (allWildcard && window.customDictionaryWildcardPage) {
        const officialTotal = first.total;
        const officialCount = first.results.length;
        const customOffset = Math.max(0, offset - officialTotal);
        const customLimit =
            offset >= officialTotal
                ? size
                : Math.max(0, size - officialCount);
        let customPage = null;
        try {
            customPage = await window.customDictionaryWildcardPage(
                customOffset,
                customLimit
            );
        } catch (error) {
            console.error("Custom wildcard paging failed:", error);
        }
        if (seq !== searchSeq) return 0;
        if (customPage && customPage.total) {
            const newTotal = officialTotal + customPage.total;
            lastWildcardCustom = {
                query,
                officialTotal,
                customTotal: customPage.total,
            };
            if (currentDebugSearch) {
                currentDebugSearch.custom = {
                    total: customPage.total,
                    offset: customOffset,
                    limit: customLimit,
                    matches: customPage.matches.length,
                };
            }
            recordDebug(
                "response",
                `Custom wildcard page: ${customPage.matches.length} rows, total ${customPage.total}`
            );
            first.total = newTotal;
            if (customPage.matches.length) {
                first.total = null;
                await applyCustomMatches(first, customPage.matches, RESULTS_PAGE_SIZE);
                first.total = newTotal;
                entryDictState.clear();
                currentEntries = first.results;
                currentQuery = query;
                render(first.results, query);
                renderFuzzy(first.fuzzy);
                updateHint(query, first.pinyin_match, first.english_match);
            }
            renderPagination(query, first);
            if (recordTiming) {
                lastSearchMs = Math.round(performance.now() - startedAt);
            }
            applyPagerScroll(scrollMode);
        } else {
            const source = window.customDictsStats?.lastSource || "unknown";
            recordDebug(
                "info",
                `Custom wildcard page returned no entries (source: ${source})`
            );
        }
        // Cache this page and prefetch adjacent pages.
        try {
            if (!cached) cachePage(cacheKey, { first, full: null, url });
            const pageSize = first.page_size || RESULTS_PAGE_SIZE;
            const totalResults = first.total || 0;
            prefetchAround(query, offset, pageSize, totalResults);
        } catch (cacheError) {}

        return first.results.length;
    }

    const customResult = await customMatchesPromise;
    if (seq !== searchSeq) return 0;
    const matches = Array.isArray(customResult)
        ? customResult
        : customResult.matches || [];
    const fastKeys = new Set(matches.map(customMatchKey));
    if (matches.length) {
        await applyCustomMatches(first, matches, RESULTS_PAGE_SIZE);
        if (seq !== searchSeq) return 0;
        entryDictState.clear();
        currentEntries = first.results;
        currentQuery = query;
        if (recordTiming) {
            lastSearchMs = Math.round(performance.now() - startedAt);
        }
        render(first.results, query);
        renderFuzzy(first.fuzzy);
        renderPagination(query, first);
        updateHint(query, first.pinyin_match, first.english_match);
        applyPagerScroll(scrollMode);
    }
    if (!Array.isArray(customResult) && customResult.slow) {
        const slow = await customResult.slow;
        if (seq !== searchSeq) return 0;
        const extra = slow.filter((match) => !fastKeys.has(customMatchKey(match)));
        if (extra.length) {
            await applyCustomMatches(first, extra, RESULTS_PAGE_SIZE);
            if (seq !== searchSeq) return 0;
            entryDictState.clear();
            currentEntries = first.results;
            currentLayouts = null;
            currentQuery = query;
            if (recordTiming) {
                lastSearchMs = Math.round(performance.now() - startedAt);
            }
            render(first.results, query);
            renderFuzzy(first.fuzzy);
            renderPagination(query, first);
            updateHint(query, first.pinyin_match, first.english_match);
            applyPagerScroll(scrollMode);
        }
    }
    // Cache this page and prefetch a few pages either way.
    try {
        if (!cached) cachePage(cacheKey, { first, full: null, url });
        const pageSize = first.page_size || RESULTS_PAGE_SIZE;
        const totalResults = first.total || 0;
        prefetchAround(query, offset, pageSize, totalResults);
    } catch (cacheError) {}

    return first.results.length;
}

// Merge already-fetched browser dictionary matches into an API response.
function customMatchKey(match) {
    return `${match.dictId}|${match.term}|${match.reading}`;
}

async function applyCustomMatches(data, matches, maxResults = RESULTS_PAGE_SIZE) {
    if (!window.mergeCustomMatches) return;
    // The server layouts do not include browser-only rows, so fall back to the
    // client merge after a custom dictionary edits the page.
    currentLayouts = null;
    try {
        await window.mergeCustomMatches(data, matches, maxResults);
    } catch (error) {
        console.error("Custom dictionary merge failed:", error);
    }
}

function repaintMergedRows(data, query, recordTiming, startedAt, scrollMode) {
    entryDictState.clear();
    currentEntries = data.results;
    currentLayouts = null;
    currentQuery = query;
    if (recordTiming) {
        lastSearchMs = Math.round(performance.now() - startedAt);
    }
    render(data.results, query);
    renderFuzzy(data.fuzzy);
    renderPagination(query, data);
    updateHint(query, data.pinyin_match, data.english_match);
    applyPagerScroll(scrollMode);
}

// Fill the remaining result slots with chunked custom matches. Headword rows
// stay where they are; pinyin and definition rows are appended below them.
async function appendCustomChunked(
    data,
    query,
    headwordMatches,
    seq,
    mode
) {
    const fetcher =
        mode === "pinyin"
            ? window.customDictionaryPinyinMatches
            : window.customDictionaryDefinitionMatches;
    if (!fetcher) return { added: false, matches: [] };
    // The SQLite pinyin path does not understand tones, wildcards or spaces;
    // those queries go through the IndexedDB path instead.
    if (
        mode === "pinyin" &&
        (/\p{Script=Han}/u.test(query) ||
            query.includes("*") || query.includes("＊") ||
            query.includes("?") || query.includes("？") ||
            /[1-5]/.test(query) ||
            /\s/.test(query.trim()))
    ) {
        return { added: false, matches: [] };
    }
    // Fuzzy rows are already part of this page, so leave room for them.
    const fuzzyRows = Array.isArray(data.fuzzy) ? data.fuzzy.length : 0;
    const remaining = Math.max(
        0,
        RESULTS_PAGE_SIZE - data.results.length - fuzzyRows
    );
    if (!remaining) return { added: false, matches: [] };
    const skipKeys = headwordMatches.map(customMatchKey);
    const matches = await fetcher(query, skipKeys, remaining);
    if (seq !== searchSeq || !matches.length) {
        return { added: false, matches: [] };
    }
    const temp = {
        results: [],
        total: null,
        page_size: RESULTS_PAGE_SIZE,
        offset: 0,
    };
    await applyCustomMatches(temp, matches, remaining);
    if (seq !== searchSeq || !temp.results.length) {
        return { added: false, matches };
    }
    const existing = new Set(
        data.results.map((entry) => `${entry.simplified}|${entry.traditional}`)
    );
    let added = false;
    for (const entry of temp.results) {
        const key = `${entry.simplified}|${entry.traditional}`;
        if (existing.has(key)) continue;
        existing.add(key);
        data.results.push(entry);
        added = true;
    }
    return { added, matches };
}

// Show prev/next paging above and below a "*" (all-headwords) search. When there
// is more than one page, the top pager replaces the "N RESULTS" count.
function renderPagination(query, data) {
    const total = data.total;
    const size = data.page_size || RESULTS_PAGE_SIZE;
    const pages = total == null ? 0 : Math.max(1, Math.ceil(total / size));
    const paging = total != null && pages > 1;

    const count = results.querySelector(".results-count");
    // Keep the result count (and its timing) visible on paged `*` results;
    // the pager is shown above/below it rather than replacing it.
    if (count) count.hidden = false;

    for (const nav of [paginationTop, pagination]) {
        nav.replaceChildren();
        nav.hidden = !paging;
    }
    if (!paging) return;

    const page = Math.floor((data.offset || 0) / size) + 1;
    const info = `Page ${page} of ${pages} (${total.toLocaleString()} results)`;
    for (const nav of [paginationTop, pagination]) {
        // The top pager leaves the page where it is; the bottom pager brings
        // the top pager into view (not the very top of the page).
        nav.append(...pagerButtons(query, data, page, pages, info, nav === paginationTop));
    }
}

// After a page change, position the view according to which pager was used.
function applyPagerScroll(mode) {
    if (mode === "top-pager") {
        if (paginationTop.hidden) return;
        const top = paginationTop.getBoundingClientRect().top + window.scrollY;
        window.scrollTo({ top, behavior: "auto" });
    } else if (mode === true) {
        window.scrollTo({ top: 0, behavior: "auto" });
    }
}

// The Previous / info / Next row shared by the top and bottom pagers.
function pagerButtons(query, data, page, pages, info, fromTop) {
    const size = data.page_size || RESULTS_PAGE_SIZE;
    const scrollMode = fromTop ? false : "top-pager";

    const first = document.createElement("button");
    first.type = "button";
    first.textContent = "First";
    first.disabled = page <= 1;
    first.addEventListener("click", () => search(query, 0, scrollMode));

    const prev = document.createElement("button");
    prev.type = "button";
    prev.textContent = "Previous";
    prev.disabled = page <= 1;
    prev.addEventListener("click", () => search(query, (data.offset || 0) - size, scrollMode));

    const label = document.createElement("span");
    label.className = "page-info";
    label.textContent = info;

    // Imported-dictionary headwords come after the official ones in a "*"
    // listing. With a large official list they would be thousands of pages
    // away, so give the pager a one-click shortcut to the first custom page.
    let customJump = null;
    if (lastWildcardCustom && lastWildcardCustom.query === query) {
        customJump = document.createElement("button");
        customJump.type = "button";
        customJump.textContent = `Imported (${lastWildcardCustom.customTotal.toLocaleString()})`;
        customJump.title = "Jump to headwords from imported dictionaries.";
        customJump.addEventListener("click", () =>
            search(query, lastWildcardCustom.officialTotal, scrollMode)
        );
    }

    const next = document.createElement("button");
    next.type = "button";
    next.textContent = "Next";
    next.disabled = page >= pages;
    next.addEventListener("click", () => search(query, (data.offset || 0) + size, scrollMode));

    const last = document.createElement("button");
    last.type = "button";
    last.textContent = "Last";
    last.disabled = page >= pages;
    last.addEventListener("click", () => search(query, (pages - 1) * size, scrollMode));

    // Warm the destination page when the pointer, focus or touch signals intent.
    // This makes Next feel instant without prefetching for visitors who never
    // page; the in-flight map dedupes it against the post-render prefetch.
    const prefetchOnIntent = (button, targetOffset) => {
        if (targetOffset < 0) return;
        const warm = () => {
            if (!button.disabled) prefetchPage(query, targetOffset);
        };
        button.addEventListener("mouseenter", warm, { once: true });
        button.addEventListener("focus", warm, { once: true });
        button.addEventListener("touchstart", warm, { once: true, passive: true });
    };
    prefetchOnIntent(first, 0);
    prefetchOnIntent(prev, (data.offset || 0) - size);
    prefetchOnIntent(next, (data.offset || 0) + size);
    prefetchOnIntent(last, (pages - 1) * size);

    return customJump
        ? [label, first, prev, customJump, next, last]
        : [label, first, prev, next, last];
}

// Render the segmented input as clickable words (clicking one searches it).
// Found words are annotated with furigana; whitespace is left plain.
// True when the text contains a Chinese character. Latin (pinyin, digits) and
// punctuation stay out of the furigana grid.
const CJK_RE = /\p{Script=Han}/u;

// The token's text in the chosen script, from the backend's whole-sentence
// OpenCC conversion (so it is context-aware, e.g. 一只 -> 一隻); falls back to
// the original text if the conversion is missing.
function scriptText(token) {
    return (state.script === "traditional" ? token.traditional : token.simplified) || token.text;
}

function renderParsed(tokens) {
    currentTokens = tokens;
    const buildStart = performance.now();
    const fragment = document.createDocumentFragment();
    for (const token of tokens) {
        const node = document.createElement("span");
        node.className = "parsed-word";
        let dialect = "";
        if (!token.text.trim()) {
            node.classList.add("space"); // whitespace: no underline
            node.textContent = token.text;
        } else if (token.entries.length) {
            const entry = token.entries[0];
            dialect = entry.pinyin_label || "";
            const text = scriptText(token);
            const reading = state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;
            if (entry.ids_images && entry.ids_images.length) {
                node.append(idsHeadword(text, reading, entry.ids_images, entry));
            } else {
                node.append(furigana(text, reading, !entry.pinyin_no_tone));
            }
            node.classList.add("clickable");
            node.addEventListener("click", () => {
                setTitle(text);
                search(text);
            });
        } else {
            const text = scriptText(token);
            if (CJK_RE.test(text)) {
                // Unmatched Chinese: keep it in the furigana grid with an empty
                // reading, so it lines up with the matched words. It gets no
                // separating padding (unlike matched words).
                node.append(furigana(text, ""));
                node.classList.add("unmatched");
            } else {
                // Latin (pinyin, digits) and punctuation: plain text, with no
                // empty reading row above every character.
                node.textContent = text;
                node.classList.add("plain");
            }
        }
        fragment.append(node);
        if (dialect) {
            const label = document.createElement("span");
            label.className = "dialect-label";
            label.textContent = dialect;
            fragment.append(label);
        }
    }
    recordPhase("parsed build (detached)", buildStart, { tokens: tokens.length });
    const attachStart = performance.now();
    parsed.replaceChildren(fragment);
    recordPhase("parsed attach", attachStart);
    // Debug only: force layout so the furigana layout cost shows up here
    // rather than hiding inside the next frame.
    const layoutStart = performance.now();
    void parsed.offsetHeight;
    recordPhase("parsed layout", layoutStart, { tokens: tokens.length });
}

// --- scroll position across reloads and back/forward ---------------------
// Results arrive from fetch(), so the browser's own scroll restoration happens
// before they are in the DOM and lands at the top. Save each URL's position
// ourselves and restore it once that URL's results are rendered.
if ("scrollRestoration" in history) history.scrollRestoration = "manual";
const SCROLL_KEY = "yabai-search-scroll";
const SCROLL_LIMIT = 30;

// Newest last. Keeping entries for other URLs is what makes Back restore the
// previous page rather than the page that was visited most recently.
function readScrollEntries() {
    try {
        const saved = JSON.parse(sessionStorage.getItem(SCROLL_KEY) || "[]");
        if (Array.isArray(saved)) return saved.filter((entry) => entry && entry.url);
        if (saved && saved.url) return [saved]; // old single-entry format
    } catch (error) {
        // Storage disabled or corrupt: start fresh.
    }
    return [];
}

function savedScrollY(url) {
    const entries = readScrollEntries();
    for (let i = entries.length - 1; i >= 0; i -= 1) {
        if (entries[i].url === url) return entries[i].y;
    }
    return null;
}

function saveScroll() {
    try {
        const entries = readScrollEntries().filter((entry) => entry.url !== location.href);
        entries.push({ url: location.href, y: window.scrollY });
        while (entries.length > SCROLL_LIMIT) entries.shift();
        sessionStorage.setItem(SCROLL_KEY, JSON.stringify(entries));
    } catch (error) {
        // Storage disabled (private mode): just skip saving.
    }
}

function restoreScroll() {
    const y = savedScrollY(location.href);
    // The results (or the home view) are in the DOM now; release the space that
    // prepareInitialScroll reserved.
    if (initialScrollPending) {
        document.body.style.minHeight = "";
        initialScrollPending = false;
    }
    // Only restore for the same URL, so a new search starts at the top.
    if (y !== null) window.scrollTo(0, y);
}

// True while a refresh has reserved page space to restore the scroll position
// before the asynchronous results arrive.
let initialScrollPending = false;

// On a return to a page the results are fetched a moment later, so the page is
// initially too short to scroll down. Reserve the missing height and jump to the
// saved position now (before the first paint), so the page does not visibly
// scroll once the results appear.
function prepareInitialScroll() {
    const y = savedScrollY(location.href);
    if (y === null || !y) return;
    document.body.style.minHeight = `${y + window.innerHeight}px`;
    initialScrollPending = true;
    window.scrollTo(0, y);
}

let scrollSaveTimer = 0;
window.addEventListener(
    "scroll",
    () => {
        clearTimeout(scrollSaveTimer);
        scrollSaveTimer = setTimeout(saveScroll, 150);
    },
    { passive: true },
);
// pagehide fires for normal navigation and for back/forward cache entries.
window.addEventListener("pagehide", saveScroll);
window.addEventListener("beforeunload", saveScroll);

// Segment the input. Multi-word text shows clickable words; a single word is
// looked up directly.
// Pasted text longer than any dictionary headword cannot match one, so skip the
// two-tier dictionary search and parse it directly.
const ANNOTATE_DIRECT_LEN = 40;
const annotateCache = new Map();
let annotateController = null;

async function fetchAnnotate(text) {
    const hit = annotateCache.get(text);
    if (hit) {
        recordDebug("annotate", "Annotate client cache hit", null, { tokens: hit.length });
        return { tokens: hit };
    }
    // A newer parse supersedes the old one; stop waiting on it.
    if (annotateController) annotateController.abort();
    const controller = new AbortController();
    annotateController = controller;
    const fetchStart = performance.now();
    try {
        const response = await fetch("/api/annotate", {
            method: "POST",
            cache: "no-store",
            priority: "high",
            signal: controller.signal,
            headers: {
                "Content-Type": "application/json",
                "X-Client-Sent": String(Date.now()),
            },
            body: JSON.stringify({ text }),
        });
        recordPhase("annotate fetch", fetchStart, {
            status: response.status,
            chars: text.length,
        });
        recordServerDebug("annotate", response);
        const inflight = response.headers.get("x-yabai-inflight");
        if (inflight) {
            recordDebug("network", "annotate server in-flight", null, { inflight });
        }
        const parseStart = performance.now();
        const data = await response.json();
        recordPhase("annotate parse", parseStart, {
            tokens: Array.isArray(data.tokens) ? data.tokens.length : 0,
        });
        recordNetworkTiming("annotate network", "/api/annotate");
        if (Array.isArray(data.tokens)) {
            annotateCache.set(text, data.tokens);
            if (annotateCache.size > 20) {
                annotateCache.delete(annotateCache.keys().next().value);
            }
        }
        return data;
    } catch (error) {
        if (error && error.name === "AbortError") {
            recordDebug("annotate", "Annotate request aborted (superseded)");
        } else {
            recordDebug("error", "Annotate failed", error);
        }
        return { tokens: [], aborted: true };
    }
}

async function annotate(text) {
    releaseClearScrollGuard();
    clearResults();
    const longText = text.length > ANNOTATE_DIRECT_LEN && CJK_RE.test(text);
    const hasSpace = text.includes(" ");

    if (longText) {
        recordDebug("search", "Skipped dictionary search: long CJK text", null, {
            queryLength: text.length,
        });
        const data = await fetchAnnotate(text);
        if (data.aborted) return;
        if (data.tokens && data.tokens.length > 1) {
            const renderStart = performance.now();
            renderParsed(data.tokens);
            recordPhase("renderParsed total", renderStart, { tokens: data.tokens.length });
            if (data.tokens[0].text !== text) await search(data.tokens[0].text);
        }
        restoreScroll();
        return;
    }

    if (!hasSpace) {
        const found = await search(text);
        if (!found && CJK_RE.test(text)) {
            // Fall back to annotation in case the word is a multi-token phrase
            // without spaces (e.g. "我是中国人").
            const data = await fetchAnnotate(text);
            if (data.aborted) return;
            if (data.tokens && data.tokens.length > 1) {
                renderParsed(data.tokens);
                if (data.tokens[0].text !== text) {
                    await search(data.tokens[0].text);
                }
            }
        }
        restoreScroll();
        return;
    }

    const annotatePromise = fetchAnnotate(text);
    const searchPromise = search(text);
    const data = await annotatePromise;
    if (data.aborted) {
        searchPromise.catch(() => {});
        return;
    }
    if (data.tokens && data.tokens.length > 1) renderParsed(data.tokens);
    const found = await searchPromise;
    if (!found && data.tokens && data.tokens.length && data.tokens[0].text !== text) {
        await search(data.tokens[0].text);
    }
    restoreScroll();
}

// Put the current query in the browser tab title (like Jisho), or the site name
// when there is no query.
function setTitle(query) {
    document.title = query ? `${query} - Yabai Search` : "Yabai Search";
    // Only search pages reserve a screen for the below-the-fold timings/About.
    document.body.classList.toggle("has-query", Boolean(query));
}

// Show or hide the home-page tagline ("search functions" description).
function showTagline(show) {
    document.querySelectorAll(".tagline").forEach((el) => {
        el.hidden = !show;
    });
}

// The query currently in the URL (?q=...), or "" for the home view.
function queryFromUrl() {
    return new URLSearchParams(location.search).get("q") || "";
}

// Results per page (matches db.ALL_PAGE_SIZE = 25) for turning ?page=N into an
// offset before the first request.
const RESULTS_PAGE_SIZE = 25;

// The results page from ?page=..., or 1 when it is absent/invalid.
function pageFromUrl() {
    const value = Number(new URLSearchParams(location.search).get("page") || 1);
    return Number.isFinite(value) && value > 1 ? Math.floor(value) : 1;
}

// Close the optional search controls so the results get the space back.
function collapseOptionPanels() {
    for (const panel of document.querySelectorAll(".options.open")) {
        panel.classList.remove("open");
        const toggle = panel.querySelector(".options-toggle");
        if (!toggle) continue;
        toggle.setAttribute("aria-expanded", "false");
        if (toggle.getAttribute("aria-controls") === "search-options") {
            document
                .querySelector(".mobile-search-options-toggle")
                ?.setAttribute("aria-expanded", "false");
        }
        try {
            const key =
                "yabai-search-panel-" +
                (toggle.getAttribute("aria-controls") || "");
            localStorage.setItem(key, "false");
        } catch (error) {
            // localStorage unavailable - nothing to save.
        }
    }
}

// Run a search and put the query in the URL, so a reload keeps the results.
function performSearch(query) {
    // A "#anchor" query jumps to that section of the About page (e.g. #cedict).
    if (query.startsWith("#") && query.length > 1) {
        location.assign(`/about${query}`);
        return;
    }
    collapseOptionPanels();
    input.value = query;
    updateClearButton();
    setTitle(query);
    showTagline(false);
    // A search started from /word/<word> is a normal search at the home URL.
    if (exactMode) document.body.classList.remove("exact-mode");
    exactMode = false;
    history.pushState({ q: query }, "", `/?q=${encodeURIComponent(query)}`);
    annotate(query);
    // On a phone the soft keyboard stays up after the form is submitted; blur the
    // box so it minimizes once the search has started. Desktop keeps the focus
    // so the query can be refined without clicking back in.
    if (window.matchMedia("(pointer: coarse)").matches) input.blur();
}

// Restore the view (results or home) from whatever is in the URL now.
function restoreFromUrl() {
    const urlQuery = queryFromUrl();
    const wordQuery = window.__wordQuery || "";
    // A real ?q= wins over the /word/<word> page's embedded word.
    if (urlQuery && urlQuery !== wordQuery) exactMode = false;
    const query = urlQuery || wordQuery;
    const page = pageFromUrl();
    if (query) {
        // The exact-word page performs the lookup but leaves the search box
        // empty; only a real ?q= search fills it.
        input.value = exactMode && !urlQuery ? "" : query;
        setTitle(query);
        showTagline(false);
        // A paged "*" result: go straight to the requested page. Restore the
        // saved scroll once the page's results are in the DOM (browser scroll
        // anchoring can otherwise land near the bottom).
        if (page > 1) search(query, (page - 1) * RESULTS_PAGE_SIZE).finally(restoreScroll);
        else if (exactMode) search(query).finally(restoreScroll);
        else annotate(query);
    } else {
        input.value = "";
        setTitle("");
        clearResults();
        showTagline(true);
        revealInitialResults();
        restoreScroll();
    }
    updateClearButton();
}

// Release the temporary page height used to keep the scroll position after
// clearing the search in the pop-out panel.
function releaseClearScrollGuard() {
    if (document.body.dataset.clearScrollGuard !== "true") return;
    document.body.style.minHeight = "";
    delete document.body.dataset.clearScrollGuard;
}

// Show the clear (x) button only while the search box has text.
function updateClearButton() {
    clearButton.hidden = input.value.length === 0;
}

// Return to the home view: no query, no results, tagline shown, URL reset.
// In the pop-out panel, keep the current scroll position: clearing the results
// would otherwise make the page shorter and jump the user back to the top.
function goHome(keepScroll = false) {
    const scrollY = keepScroll ? window.scrollY : 0;
    const height = keepScroll ? document.documentElement.scrollHeight : 0;
    input.value = "";
    updateClearButton();
    setTitle("");
    clearResults();
    showTagline(true);
    revealInitialResults();
    exactMode = false;
    document.body.classList.remove("exact-mode");
    history.pushState({}, "", "/");
    if (keepScroll) {
        document.body.style.minHeight = `${height}px`;
        document.body.dataset.clearScrollGuard = "true";
        window.scrollTo(0, scrollY);
    } else {
        releaseClearScrollGuard();
    }
}

// Load the display preferences from localStorage (if it's available).
function loadPrefs() {
    try {
        const script = localStorage.getItem("yabai-search-script");
        if (script === "traditional" || script === "simplified") state.script = script;
        // Tone coloring is on by default; only an explicit "false" disables it.
        state.toneColoring = localStorage.getItem("yabai-search-tone") !== "false";
        state.phonetic = localStorage.getItem("yabai-search-phonetic") === "zhuyin" ? "zhuyin" : "pinyin";
        state.taiwan = localStorage.getItem("yabai-search-taiwan") === "true";
        state.customDicts = localStorage.getItem("yabai-search-custom-dicts") === "true";
        state.compact = localStorage.getItem("yabai-search-compact") === "true";
        // Auto Clipboard is remembered across page loads. The watcher only
        // reads when the switch is on, so turning it off stops all access.
        state.autoClipboard = localStorage.getItem("yabai-search-auto-clipboard") === "true";
        // The Compact/Detailed switch is inside the custom dictionary panel,
        // so without custom dictionaries the app always uses compact mode.
        if (!state.customDicts) state.compact = true;
        state.lang = localStorage.getItem("yabai-search-lang") || "EN";
        loadDictOrder();
        const stored = JSON.parse(localStorage.getItem("yabai-search-dicts") || "null");
        // Without custom dictionaries, ignore saved choices and use the
        // language's defaults (EN -> every English dictionary). A custom order
        // belongs to
        // the custom setup, so drop it too.
        state.dicts = expandMoeSelection(
            stored === null || !state.customDicts
                ? defaultDictsForLanguage(state.lang)
                : new Set(stored),
        );
        if (!state.customDicts) {
            dictOrder = {};
            saveDictOrder();
        }
    } catch (e) {
        // localStorage unavailable - keep the defaults.
    }
    scriptToggle.checked = state.script === "traditional";
    toneToggle.checked = state.toneColoring;
    phoneticToggle.checked = state.phonetic === "zhuyin";
    taiwanToggle.checked = state.taiwan;
    customDictsToggle.checked = state.customDicts;
    if (autoClipboardToggle) autoClipboardToggle.checked = state.autoClipboard;
    syncClipboardWatch();
    for (const input of dictToggles) input.checked = state.dicts.has(input.dataset.anchor);
    updateQuickToggles();
    syncGroups();
    syncDictPanel();
    applyLang(state.lang);
}

// Persist the display preferences. Settings supplied by a developer URL are
// shown for this page but never written back, so they cannot replace the
// visitor's saved preferences.
function savePrefs() {
    if (settingsFromUrl) return;
    try {
        localStorage.setItem("yabai-search-script", state.script);
        localStorage.setItem("yabai-search-tone", String(state.toneColoring));
        localStorage.setItem("yabai-search-phonetic", state.phonetic);
        localStorage.setItem("yabai-search-taiwan", String(state.taiwan));
        localStorage.setItem("yabai-search-custom-dicts", String(state.customDicts));
        localStorage.setItem("yabai-search-compact", String(state.compact));
        localStorage.setItem("yabai-search-auto-clipboard", String(state.autoClipboard));
        localStorage.setItem("yabai-search-dicts", JSON.stringify([...state.dicts]));
        localStorage.setItem("yabai-search-lang", state.lang);
    } catch (e) {
        // localStorage unavailable - nothing to save.
    }
}

// Apply the display settings carried by a report link (see the Reports page).
// The URL parameters are optional; a normal search link only has q/page.
function applySettingsFromUrl() {
    const params = new URLSearchParams(location.search);
    const boolParam = (name) => {
        const value = params.get(name);
        return value === "1" || value === "true" || value === "on";
    };
    let changed = false;
    if (params.has("script")) {
        state.script = params.get("script") === "traditional" ? "traditional" : "simplified";
        changed = true;
    }
    if (params.has("tone")) {
        state.toneColoring = boolParam("tone");
        changed = true;
    }
    if (params.has("phonetic")) {
        state.phonetic = params.get("phonetic") === "zhuyin" ? "zhuyin" : "pinyin";
        changed = true;
    }
    if (params.has("taiwan")) {
        state.taiwan = boolParam("taiwan");
        changed = true;
    }
    if (params.has("custom")) {
        state.customDicts = boolParam("custom");
        changed = true;
    }
    if (params.has("compact")) {
        state.compact = boolParam("compact");
        changed = true;
    }
    if (params.has("lang")) {
        const lang = params.get("lang");
        if (lang) {
            state.lang = lang;
            changed = true;
        }
    }
    if (params.has("dicts")) {
        const anchors = (params.get("dicts") || "")
            .split(",")
            .map((anchor) => anchor.trim())
            .filter((anchor) => anchor && anchorLang.has(anchor));
        state.dicts = new Set(anchors);
        // A shared/report URL carries the drag order. Apply it to this page
        // only; settingsFromUrl keeps it out of localStorage.
        if (state.customDicts) {
            dictOrder = {};
            for (const anchor of anchors) {
                const lang = anchorLang.get(anchor);
                if (!lang) continue;
                if (!dictOrder[lang]) dictOrder[lang] = [];
                dictOrder[lang].push(anchor);
            }
        }
        changed = true;
    }
    // Report links can carry dictionary choices, but they are ignored unless
    // custom dictionaries are on. A URL-provided setting must not rewrite the
    // visitor's saved dictionary order either.
    settingsFromUrl = changed;
    if (!state.customDicts) {
        state.dicts = defaultDictsForLanguage(state.lang);
        state.compact = true;
        dictOrder = {};
        if (!settingsFromUrl) saveDictOrder();
        entryDictState.clear();
    }
    if (!changed) return;

    // Bring the visible controls in line with the restored state.
    scriptToggle.checked = state.script === "traditional";
    toneToggle.checked = state.toneColoring;
    phoneticToggle.checked = state.phonetic === "zhuyin";
    taiwanToggle.checked = state.taiwan;
    customDictsToggle.checked = state.customDicts;
    if (autoClipboardToggle) autoClipboardToggle.checked = state.autoClipboard;
    for (const input of dictToggles) input.checked = state.dicts.has(input.dataset.anchor);
    updateQuickToggles();
    syncGroups();
    syncDictPanel();
    applyLang(state.lang);
    updateDictMode();
}

// The quick controls mirror the display switches. Only the tone square's
// sample reacts to tone coloring (gold when on, grayed out when off); the other
// squares always keep their own palette.
function updateQuickToggles() {
    if (!quickSquares.length) return;
    const traditional = state.script === "traditional";
    for (const square of quickSquares) {
        const active =
            square.dataset.script === (traditional ? "traditional" : "simplified");
        square.setAttribute("aria-pressed", String(active));
    }
    // The tone sample follows the script toggle (simplified/traditional) and
    // turns gray when tone coloring is off.
    if (quickTone) {
        const phrase = traditional ? ["花", "紅", "柳", "綠"] : ["花", "红", "柳", "绿"];
        const chars = quickTone.querySelectorAll(".tone-phrase span");
        chars.forEach((node, i) => {
            if (phrase[i]) node.textContent = phrase[i];
        });
        quickTone.setAttribute("aria-pressed", String(state.toneColoring));
    }
    if (quickPinyin) quickPinyin.setAttribute("aria-pressed", String(state.phonetic === "pinyin"));
    if (quickZhuyin) quickZhuyin.setAttribute("aria-pressed", String(state.phonetic === "zhuyin"));
    if (quickChina) quickChina.setAttribute("aria-pressed", String(!state.taiwan));
    if (quickTaiwanSquare) {
        quickTaiwanSquare.setAttribute("aria-pressed", String(state.taiwan));
    }
    // The active side of each description is gold; the tone row also changes
    // its wording and colour between Off (white) and On (gold).
    if (scriptDescription) {
        for (const word of scriptDescription.querySelectorAll("[data-script]")) {
            const active = word.dataset.script === (traditional ? "traditional" : "simplified");
            word.classList.toggle("active", active);
        }
    }
    if (toneDescription) {
        toneDescription.textContent = state.toneColoring ? "Tone Color On" : "Tone Color Off";
        toneDescription.classList.toggle("on", state.toneColoring);
    }
    if (phoneticDescription) {
        for (const word of phoneticDescription.querySelectorAll("[data-phonetic]")) {
            word.classList.toggle("active", word.dataset.phonetic === state.phonetic);
        }
    }
    if (dialectDescription) {
        for (const word of dialectDescription.querySelectorAll("[data-region]")) {
            const active = word.dataset.region === (state.taiwan ? "taiwan" : "china");
            word.classList.toggle("active", active);
        }
    }
}

// Re-render the current results when a display toggle changes.
scriptToggle.addEventListener("change", () => {
    state.script = scriptToggle.checked ? "traditional" : "simplified";
    savePrefs();
    redraw();
    updateQuickToggles();
});
toneToggle.addEventListener("change", () => {
    state.toneColoring = toneToggle.checked;
    savePrefs();
    redraw();
    updateQuickToggles();
});
phoneticToggle.addEventListener("change", () => {
    state.phonetic = phoneticToggle.checked ? "zhuyin" : "pinyin";
    savePrefs();
    redraw();
    updateQuickToggles();
});

// The quick controls flip the matching checkbox and reuse its change handler.
// Theme: light / dark. Persisted in localStorage.
function applyTheme(theme) {
    const light = theme === "light";
    document.documentElement.classList.toggle("theme-light", light);
    for (const btn of themeSquares) {
        btn.setAttribute("aria-pressed", String(btn.dataset.theme === theme));
    }
    try { localStorage.setItem("yabai-theme", theme); } catch (error) {}
}
let savedTheme = "dark";
try { savedTheme = localStorage.getItem("yabai-theme") || "dark"; } catch (error) {}
applyTheme(savedTheme);

for (const square of themeSquares) {
    square.addEventListener("click", () => {
        applyTheme(square.dataset.theme);
    });
}

// Hamburger menu: toggle the dropdown.
if (menuToggle && menuDropdown) {
    menuToggle.addEventListener("click", (event) => {
        event.stopPropagation();
        const open = !menuDropdown.classList.contains("open");
        menuDropdown.classList.toggle("open", open);
        menuToggle.setAttribute("aria-expanded", String(open));
    });
    // Close when clicking outside.
    document.addEventListener("click", (event) => {
        if (menuDropdown.classList.contains("open") && !menuToggle.contains(event.target) && !menuDropdown.contains(event.target)) {
            menuDropdown.classList.remove("open");
            menuToggle.setAttribute("aria-expanded", "false");
        }
    });
}

for (const square of quickSquares) {
    square.addEventListener("click", () => {
        const script = square.dataset.script;
        if (state.script === script) return;
        scriptToggle.checked = script === "traditional";
        scriptToggle.dispatchEvent(new Event("change"));
    });
}
// Tone coloring: one square, toggled on click.
quickTone.addEventListener("click", () => {
    toneToggle.checked = !toneToggle.checked;
    toneToggle.dispatchEvent(new Event("change"));
});
// Phonetic system: one square per option.
quickPinyin.addEventListener("click", () => {
    if (state.phonetic === "pinyin") return;
    phoneticToggle.checked = false;
    phoneticToggle.dispatchEvent(new Event("change"));
});
quickZhuyin.addEventListener("click", () => {
    if (state.phonetic === "zhuyin") return;
    phoneticToggle.checked = true;
    phoneticToggle.dispatchEvent(new Event("change"));
});
// Region: reuse the Taiwan switch's change handler, so the readings update
// immediately and Taiwan-only reading matches are refreshed.
quickChina.addEventListener("click", () => {
    if (!state.taiwan) return;
    taiwanToggle.checked = false;
    taiwanToggle.dispatchEvent(new Event("change"));
});
quickTaiwanSquare.addEventListener("click", () => {
    if (state.taiwan) return;
    taiwanToggle.checked = true;
    taiwanToggle.dispatchEvent(new Event("change"));
});
// The description text is clickable too. Clicking a script, phonetic, or region
// word picks that side; clicking the tone label flips tone coloring.
if (scriptDescription) {
    for (const word of scriptDescription.querySelectorAll("[data-script]")) {
        word.addEventListener("click", () => {
            const script = word.dataset.script;
            if (state.script === script) return;
            scriptToggle.checked = script === "traditional";
            scriptToggle.dispatchEvent(new Event("change"));
        });
    }
}
if (toneDescription) {
    toneDescription.addEventListener("click", () => {
        toneToggle.checked = !toneToggle.checked;
        toneToggle.dispatchEvent(new Event("change"));
    });
}
if (phoneticDescription) {
    for (const word of phoneticDescription.querySelectorAll("[data-phonetic]")) {
        word.addEventListener("click", () => {
            const phonetic = word.dataset.phonetic;
            if (state.phonetic === phonetic) return;
            phoneticToggle.checked = phonetic === "zhuyin";
            phoneticToggle.dispatchEvent(new Event("change"));
        });
    }
}
if (dialectDescription) {
    for (const word of dialectDescription.querySelectorAll("[data-region]")) {
        word.addEventListener("click", () => {
            const taiwan = word.dataset.region === "taiwan";
            if (state.taiwan === taiwan) return;
            taiwanToggle.checked = taiwan;
            taiwanToggle.dispatchEvent(new Event("change"));
        });
    }
}
taiwanToggle.addEventListener("change", () => {
    state.taiwan = taiwanToggle.checked;
    savePrefs();
    updateQuickToggles();
    // Re-render the readings, then re-run the search so Taiwan readings move
    // entries between the main list and Fuzzy match. Keep the timing line from
    // the last search-bar search.
    redraw();
    if (currentQuery && currentQuery.trim() !== "*") {
        search(currentQuery, 0, false, false);
    }
});
// Load custom_dicts.js on demand, so pages that never use it skip the 163 KB.
// Cached so repeated toggles don't load it twice.
let customDictsLoadPromise = null;
function loadCustomDicts() {
    if (window.customDictionaryMatches || !window.__customDictsSrc) {
        return Promise.resolve();
    }
    if (customDictsLoadPromise) return customDictsLoadPromise;
    customDictsLoadPromise = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = window.__customDictsSrc;
        script.dataset.late = "1";
        script.onload = () => resolve();
        script.onerror = () => {
            customDictsLoadPromise = null;
            reject(new Error("Failed to load custom_dicts.js"));
        };
        document.head.appendChild(script);
    });
    return customDictsLoadPromise;
}

customDictsToggle.addEventListener("change", () => {
    state.customDicts = customDictsToggle.checked;
    // Turning custom dictionaries off returns the dictionary settings to the
    // language defaults and forces compact mode, which shows one higher-priority
    // source per language.
    if (!state.customDicts) {
        state.compact = true;
        updateDictMode();
        resetDictSettings();
    } else {
        loadCustomDicts().catch((error) => console.error(error));
        entryDictState.clear();
    }
    savePrefs();
    syncDictPanel();
    // Re-render so the per-entry dictionary chips appear/disappear too.
    redraw();
    rerunSearch(); // the effective selected dictionaries just changed
});

// The button flips the hidden switch and reuses its change handler.
if (customDictsButton) {
    customDictsButton.addEventListener("click", () => {
        customDictsToggle.checked = !customDictsToggle.checked;
        customDictsToggle.dispatchEvent(new Event("change"));
    });
}

// Show the Dictionaries disclosure only when "Custom dictionaries" is on.
function syncDictPanel() {
    dictPanel.hidden = !state.customDicts;
    if (customDictsButton) {
        customDictsButton.setAttribute("aria-pressed", String(state.customDicts));
    }
}

// Each dictionary belongs to its language column.
function groupFor(toggle) {
    return toggle.closest(".dict-column").dataset.lang;
}

function syncGroups() {
    // The language switches were replaced by the Compact/Detailed control.
    updateDictMode();
}

for (const toggle of dictToggles) {
    toggle.addEventListener("change", () => {
        const anchor = toggle.dataset.anchor;
        // 國語辭典簡編本 and 重編國語辭典修訂本 share one visible checkbox.
        const anchors =
            anchor === "moe" || anchor === "moe_revised"
                ? ["moe", "moe_revised"]
                : [anchor];
        for (const name of anchors) {
            if (toggle.checked) state.dicts.add(name);
            else state.dicts.delete(name);
        }
        for (const input of dictToggles) {
            if (anchors.includes(input.dataset.anchor)) {
                input.checked = toggle.checked;
            }
        }
        syncGroups();
        entryDictState.clear();
        savePrefs();
        redraw();
        rerunSearch();
    });
}

// Each language header has a disclosure arrow. It controls its dictionary list
// manually; selecting or clearing dictionaries never collapses the list. The
// open languages are remembered across reloads.
const DICT_OPEN_KEY = "yabai-search-dict-open";

function readOpenDictLangs() {
    try {
        const saved = JSON.parse(localStorage.getItem(DICT_OPEN_KEY) || "[]");
        return new Set(Array.isArray(saved) ? saved : []);
    } catch (error) {
        return new Set();
    }
}

function saveOpenDictLangs() {
    try {
        const open = [...document.querySelectorAll(".dict-column.open")].map(
            (column) => column.dataset.lang
        );
        localStorage.setItem(DICT_OPEN_KEY, JSON.stringify(open));
    } catch (error) {
        // localStorage unavailable - nothing to save.
    }
}

// On phones, only one language column may be expanded. Opening any language
// collapses the others; desktop still allows several open columns.
const narrowDictsQuery = window.matchMedia("(max-width: 600px)");

function closeOtherDictColumns(except) {
    if (!narrowDictsQuery.matches) return;
    for (const column of document.querySelectorAll(".dict-column.open")) {
        if (column === except) continue;
        column.classList.remove("open");
        const disclosure = column.querySelector(".dict-disclosure");
        if (disclosure) disclosure.setAttribute("aria-expanded", "false");
    }
    saveOpenDictLangs();
}

function enforceSingleOpenDictColumn() {
    if (!narrowDictsQuery.matches) return;
    closeOtherDictColumns(document.querySelector(".dict-column.open"));
}

window.closeOtherDictColumns = closeOtherDictColumns;
window.enforceSingleOpenDictColumn = enforceSingleOpenDictColumn;

const openDictLangs = readOpenDictLangs();
for (const column of document.querySelectorAll(".dict-column")) {
    const button = column.querySelector(".dict-disclosure");
    if (!button) continue;
    if (openDictLangs.has(column.dataset.lang)) {
        column.classList.add("open");
        button.setAttribute("aria-expanded", "true");
    }
    button.addEventListener("click", () => {
        const open = !column.classList.contains("open");
        column.classList.toggle("open", open);
        button.setAttribute("aria-expanded", String(open));
        if (open) closeOtherDictColumns(column);
        saveOpenDictLangs();
    });
    // Clicking the language name/header also opens its dictionary dropdown.
    const head = column.querySelector(".dict-column-head");
    if (head) {
        head.addEventListener("click", (event) => {
            if (event.target.closest(".dict-disclosure, .dict-remove, .dict-add-dict")) return;
            button.click();
        });
    }
}

enforceSingleOpenDictColumn();
narrowDictsQuery.addEventListener("change", (event) => {
    if (event.matches) enforceSingleOpenDictColumn();
});

// --- search-language dropdown (EN / ZH / TW) ------------------------------
// Set the chosen language: update the button label and the selected option.
// (Selection is stored but doesn't change search behaviour yet.)
function applyLang(value) {
    state.lang = value;
    langButton.textContent = value;
    for (const item of langMenu.querySelectorAll("[role='option']")) {
        item.setAttribute("aria-selected", String(item.dataset.value === value));
    }
}

function setLangOpen(open) {
    langMenu.hidden = !open;
    langButton.setAttribute("aria-expanded", String(open));
}

// The dictionaries a search language uses by default.
function defaultDictsForLanguage(lang) {
    return new Set(langDefaults[lang] || langDefaults.EN || ["cedict"]);
}

// The two MOE dictionaries share one visible checkbox. Selecting either one
// selects both, so the client can merge their definitions.
function expandMoeSelection(dicts) {
    if (dicts.has("moe")) dicts.add("moe_revised");
    if (dicts.has("moe_revised")) dicts.add("moe");
    return dicts;
}

// Selecting a search language switches on its default dictionaries and off all
// the others (see yabai_search/default_dictionaries.py).
function applyDictDefaults(value) {
    if (!(value in langDefaults)) return;
    // Start from a clean slate: every dictionary in the new language category,
    // and compact mode (one definition per language).
    state.dicts = expandMoeSelection(defaultDictsForLanguage(value));
    for (const toggle of dictToggles) {
        toggle.checked = state.dicts.has(toggle.dataset.anchor);
    }
    state.compact = true;
    updateDictMode();
    syncGroups();
    entryDictState.clear();
}

// Reset every dictionary setting: selection, per-entry toggles and the custom
// drag order. The search language itself stays as the user chose it.
function resetDictSettings() {
    state.dicts = expandMoeSelection(defaultDictsForLanguage(state.lang));
    for (const toggle of dictToggles) {
        toggle.checked = state.dicts.has(toggle.dataset.anchor);
    }
    entryDictState.clear();
    dictOrder = {};
    saveDictOrder();
    // Put the rows back in the server's language order; otherwise the next
    // search would still send the old drag order.
    for (const container of document.querySelectorAll(".dict-items")) {
        applyDictOrder(container);
    }
    syncGroups();
}

langButton.addEventListener("click", () => setLangOpen(langMenu.hidden));
langMenu.addEventListener("click", (event) => {
    const item = event.target.closest("[role='option']");
    if (!item) return;
    applyLang(item.dataset.value);
    // Every language switch resets the dictionary selection to that language's
    // defaults (all dictionaries in the category) and turns compact mode on.
    applyDictDefaults(item.dataset.value);
    savePrefs();
    setLangOpen(false);
    redraw(); // the selected language or default dictionaries may have changed
    rerunSearch(); // the server picks the preview dictionary and the categories
});
// Close the menu when clicking elsewhere, or pressing Escape.
document.addEventListener("click", (event) => {
    if (!langSelect.contains(event.target)) setLangOpen(false);
});
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setLangOpen(false);
});

// Collapsible panels ("Search options", "Dictionaries"). Each `.options` element
// has an `.options-toggle` button; the `.open` class drives the CSS height
// animation. The open/closed choice is remembered per panel.
const mobileSearchOptionsToggle = document.querySelector(
    ".mobile-search-options-toggle"
);

function isSearchOptionsPanel(panel) {
    return Boolean(panel.querySelector("#search-options"));
}

function syncMobileSearchOptionsToggle(panel) {
    if (!mobileSearchOptionsToggle || !isSearchOptionsPanel(panel)) return;
    mobileSearchOptionsToggle.setAttribute(
        "aria-expanded",
        String(panel.classList.contains("open"))
    );
}

function applyOptionPanelState(panel, open, save = true) {
    panel.classList.toggle("open", open);
    const toggle = panel.querySelector(".options-toggle");
    if (toggle) {
        toggle.setAttribute("aria-expanded", String(open));
        if (save) {
            try {
                const key =
                    "yabai-search-panel-" +
                    (toggle.getAttribute("aria-controls") || "");
                localStorage.setItem(key, String(open));
            } catch (e) {
                // localStorage unavailable - nothing to save.
            }
        }
    }
    syncMobileSearchOptionsToggle(panel);
}

// Switch the two panels in one height transition. The inner disclosures snap
// to their final state, then the whole search panel grows or shrinks smoothly.
function switchOptionPanel(panel, others, searchPanel, save) {
    if (searchPanel._accordionCleanup) {
        searchPanel._accordionCleanup();
    }
    const startHeight = searchPanel.getBoundingClientRect().height;
    panel.classList.add("accordion-instant");
    for (const other of others) other.classList.add("accordion-instant");
    applyOptionPanelState(panel, true, save);
    for (const other of others) applyOptionPanelState(other, false, save);
    const endHeight = searchPanel.getBoundingClientRect().height;
    if (Math.abs(startHeight - endHeight) < 1) {
        panel.classList.remove("accordion-instant");
        for (const other of others) other.classList.remove("accordion-instant");
        return;
    }

    let timer = null;
    const cleanup = () => {
        searchPanel.removeEventListener("transitionend", onEnd);
        if (timer) clearTimeout(timer);
        searchPanel.style.transition = "";
        searchPanel.style.height = "";
        searchPanel.style.overflow = "";
        searchPanel.style.boxSizing = "";
        panel.classList.remove("accordion-instant");
        for (const other of others) other.classList.remove("accordion-instant");
        searchPanel._accordionCleanup = null;
    };
    const onEnd = (event) => {
        if (event.target !== searchPanel || event.propertyName !== "height") {
            return;
        }
        cleanup();
    };

    searchPanel.style.transition = "none";
    searchPanel.style.boxSizing = "border-box";
    searchPanel.style.height = `${startHeight}px`;
    searchPanel.style.overflow = "hidden";
    void searchPanel.offsetHeight; // commit the starting height
    searchPanel.style.transition = "height 200ms ease";
    searchPanel.style.height = `${endHeight}px`;
    searchPanel.addEventListener("transitionend", onEnd);
    timer = setTimeout(cleanup, 260);
    searchPanel._accordionCleanup = cleanup;
}

function setOptionPanelOpen(panel, open, save = true) {
    // The two disclosures behave like an accordion on every screen size:
    // opening one closes the other.
    const others = [...document.querySelectorAll(".options.open")].filter(
        (other) => other !== panel
    );
    const searchPanel = panel.closest(".search-panel");
    if (open && searchPanel && others.length) {
        if (save) {
            switchOptionPanel(panel, others, searchPanel, save);
        } else {
            // Restoring saved state on load: do not animate.
            applyOptionPanelState(panel, true, save);
            for (const other of others) {
                applyOptionPanelState(other, false, save);
            }
        }
        return;
    }
    applyOptionPanelState(panel, open, save);
}

for (const panel of document.querySelectorAll(".options")) {
    const toggle = panel.querySelector(".options-toggle");
    const key = "yabai-search-panel-" + (toggle.getAttribute("aria-controls") || "");
    toggle.addEventListener("click", () => {
        setOptionPanelOpen(panel, !panel.classList.contains("open"));
    });
    try {
        if (localStorage.getItem(key) === "true") {
            setOptionPanelOpen(panel, true, false);
        } else {
            syncMobileSearchOptionsToggle(panel);
        }
    } catch (e) {
        // localStorage unavailable - start closed.
    }
}

if (mobileSearchOptionsToggle) {
    mobileSearchOptionsToggle.addEventListener("click", () => {
        const panel = document
            .getElementById("search-options")
            ?.closest(".options");
        if (!panel) return;
        setOptionPanelOpen(panel, !panel.classList.contains("open"));
        if (panel.classList.contains("open")) {
            setLangOpen(false);
            // The gear is the only way to reveal the dictionaries section in
            // the compact stuck-to-top panel.
            const searchPanel = panel.closest(".search-panel");
            if (
                searchPanel &&
                searchPanel.classList.contains("pulled") &&
                !searchPanel.classList.contains("docked")
            ) {
                searchPanel.classList.add("show-dictionaries");
            }
        }
    });
    const panel = document.getElementById("search-options")?.closest(".options");
    if (panel) syncMobileSearchOptionsToggle(panel);
}

// Run a search when the form is submitted (Enter or the button).
form.addEventListener("submit", (event) => {
    event.preventDefault(); // don't reload the page
    const query = input.value.trim();
    if (query) performSearch(query);
});

// Keep the clear button in sync with the text in the search box.
input.addEventListener("input", () => {
    lastInputAt = performance.now();
    updateClearButton();
});

// The clear (✕) button empties the box, returns to the home view, and re-focuses
// the input - which reopens the keyboard on mobile.
clearButton.addEventListener("click", () => {
    // Keep the page position when the search panel is popped out; removing the
    // results would otherwise make the document shorter and scroll to the top.
    goHome(searchPanel.classList.contains("pulled"));
    input.focus();
});

// Clicking the title returns to the home view.
document.getElementById("home-link").addEventListener("click", (event) => {
    event.preventDefault(); // handle it in JS instead of reloading the page
    goHome();
});

// Copy text for a selection inside one furigana group, Jisho-style: the whole
// reading, a newline, then the word (e.g. "diàn nǎo\n电脑"). Selecting just the
// reading or just the word copies only that. Returns null when the selection is
// not inside a single furigana group, so normal copying is untouched.
function furiganaCopyText(range) {
    const start = range.startContainer.nodeType === 1
        ? range.startContainer
        : range.startContainer.parentElement;
    const end = range.endContainer.nodeType === 1
        ? range.endContainer
        : range.endContainer.parentElement;
    const group = start ? start.closest(".furigana") : null;
    if (!group || !end || end.closest(".furigana") !== group) return null;

    const selected = (rowSelector, cellSelector, separator) => {
        const row = group.querySelector(rowSelector);
        if (!row || !range.intersectsNode(row)) return "";
        return [...row.querySelectorAll(cellSelector)]
            .filter((node) => range.intersectsNode(node))
            .map((node) => node.textContent)
            .filter(Boolean)
            .join(separator);
    };
    const readings = selected(".reading", ".reading-cell", " ");
    const chars = selected(".char", ".char-cell", "");
    if (!readings && !chars) return null;
    // The reading and the headword are separate lines; copy only the line(s)
    // the selection actually touched.
    if (readings && chars) return `${readings}\n${chars}`;
    return readings || chars;
}

document.addEventListener("copy", (event) => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return;
    const text = furiganaCopyText(selection.getRangeAt(0));
    if (text === null) return;
    event.clipboardData.setData("text/plain", text);
    event.preventDefault();
});

// Slim pull-down handle for the search panel. It appears once the panel is
// scrolled out of view; clicking it pulls the whole panel (options and
// dictionaries included) down from the top, and clicking again closes it.
const searchPanel = document.querySelector(".search-panel");
const panelSlot = document.querySelector(".search-panel-slot");
const pullArrow = document.getElementById("pull-arrow");

// The panel's expanded state survives a refresh (sessionStorage is scoped to the
// tab, so a fresh tab still starts with the panel in the page).
const PULL_KEY = "yabai-search-pulled";
// Pending timer that finishes the close animation.
let closeTimer = null;
// Pending timer that releases the panel's reserved space after its row has
// finished growing back to the normal tall size.
let slotReleaseTimer = null;
// requestAnimationFrame id for making the pull tab follow the panel.
let pullArrowFrame = null;

function savePulled(open) {
    try {
        sessionStorage.setItem(PULL_KEY, String(open));
    } catch (error) {
        // Storage disabled (private mode): just skip saving.
    }
}

function positionPullArrow() {
    if (!searchPanel || !pullArrow) return;
    if (
        searchPanel.classList.contains("pulled") &&
        !searchPanel.classList.contains("closing")
    ) {
        placePulledPanel();
    }
    // Center the tab on the search panel itself (the CSS transform centers it
    // around this point).
    const rect = searchPanel.getBoundingClientRect();
    pullArrow.style.left = `${rect.left + rect.width / 2}px`;
    // The tracking loop below owns the tab's visibility and position.
}

// Keep the handle on the panel's bottom edge every frame, then stick it to the
// top center once the panel's bottom has scrolled above the viewport.
function startPullArrowTracking() {
    if (pullArrowFrame) return;

    function track() {
        if (!searchPanel) {
            pullArrowFrame = null;
            return;
        }
        const rect = searchPanel.getBoundingClientRect();
        const pulled = searchPanel.classList.contains("pulled");
        const docked = searchPanel.classList.contains("docked");
        // Hide the tab when the search bar is fully visible in its normal
        // place. Keep it available while the bar is only partly visible, so
        // scrolling back up still has the pull handle until it reaches the top.
        const barFullyVisible =
            !pulled && rect.top >= 0 && rect.bottom <= window.innerHeight;
        pullArrow.hidden = docked || barFullyVisible;
        // Lift by 1px so the tab's background covers the panel's bottom border.
        pullArrow.style.top = `${Math.max(0, rect.bottom - 1)}px`;
        pullArrowFrame = requestAnimationFrame(track);
    }

    pullArrowFrame = requestAnimationFrame(track);
}

function updatePullArrow() {
    if (!searchPanel || !pullArrow) return;
    positionPullArrow();
}

// Reserve the panel's normal space in the page before it becomes fixed, so the
// content below does not jump. The panel's bottom margin normally collapses
// with the next element's top margin; once the panel is fixed, only that next
// margin remains, so add just the difference.
function reservePanelSpace() {
    if (!panelSlot) return;
    const height = searchPanel.getBoundingClientRect().height;
    const marginBottom = parseFloat(getComputedStyle(searchPanel).marginBottom) || 0;
    const next = nextInFlowSibling(panelSlot);
    const marginTop = next
        ? parseFloat(getComputedStyle(next).marginTop) || 0
        : 0;
    panelSlot.style.height = `${height + Math.max(marginBottom - marginTop, 0)}px`;
}

// The next sibling that actually takes part in the page flow (the pull handle
// is fixed, so skip it).
function nextInFlowSibling(element) {
    let node = element.nextElementSibling;
    while (node) {
        const position = getComputedStyle(node).position;
        if (position !== "fixed" && position !== "absolute") return node;
        node = node.nextElementSibling;
    }
    return null;
}

// The fixed panel keeps the same left edge and width as the body column. It
// follows the page upward and sticks at the top only once its normal place has
// scrolled past the viewport edge.
function placePulledPanel() {
    if (!panelSlot) return;
    const rect = panelSlot.getBoundingClientRect();
    searchPanel.style.setProperty("--pulled-left", `${rect.left}px`);
    searchPanel.style.setProperty("--pulled-width", `${rect.width}px`);
    const top = Math.max(0, rect.top);
    searchPanel.style.top = `${top}px`;
    const wasDocked = searchPanel.classList.contains("docked");
    const docked = top > 0;
    searchPanel.classList.toggle("docked", docked);
    // While docked in its normal place, keep the reserved space in sync with
    // the panel's current height. When stuck at the top, leave it alone so
    // growing the panel never moves the page.
    if (docked) {
        // A new stuck session starts with the dictionaries section hidden.
        if (!wasDocked) searchPanel.classList.remove("show-dictionaries");
        reservePanelSpace();
    }
}

// Briefly disable the search-row transition so opening the stuck panel switches
// straight to its compact size instead of animating down from the normal size.
function suppressRowTransition() {
    searchPanel.classList.add("no-row-transition");
    // Flush the styles with transitions disabled.
    searchPanel.getBoundingClientRect();
    requestAnimationFrame(() => {
        searchPanel.classList.remove("no-row-transition");
    });
}

function setPulled(open) {
    if (!searchPanel) return;
    // The exact-word view keeps the search bar in the page, never popped out.
    if (open && exactMode) return;
    if (open) {
        // If a close animation is still running, cancel it and stay open.
        if (closeTimer) {
            clearTimeout(closeTimer);
            closeTimer = null;
        }
        if (slotReleaseTimer) {
            clearTimeout(slotReleaseTimer);
            slotReleaseTimer = null;
        }
        searchPanel.classList.remove("closing");
        if (!searchPanel.classList.contains("pulled")) {
            // Keep the panel's place in the flow, then fix it to the viewport.
            reservePanelSpace();
            placePulledPanel();
            suppressRowTransition();
            searchPanel.classList.add("pulled");
            pullArrow.setAttribute("aria-expanded", "true");
            savePulled(true);
        }
    } else if (searchPanel.classList.contains("pulled")) {
        // Slide the panel up first, then return it to the normal page flow.
        const closeDelay = searchPanel.classList.contains("docked") ? 150 : 210;
        searchPanel.classList.add("closing");
        pullArrow.setAttribute("aria-expanded", "false");
        savePulled(false);
        // Closing the pop-out tab also puts the search options and
        // dictionaries menus away.
        collapseOptionPanels();
        closeTimer = setTimeout(() => {
            closeTimer = null;
            searchPanel.classList.remove(
                "closing",
                "pulled",
                "docked",
                "show-dictionaries"
            );
            searchPanel.style.removeProperty("--pulled-left");
            searchPanel.style.removeProperty("--pulled-width");
            searchPanel.style.removeProperty("top");
            updatePullArrow();
            releaseClearScrollGuard();
            // Keep the reserved height until the search row has grown back to
            // the normal tall size. Clearing it earlier makes the page scroll.
            slotReleaseTimer = setTimeout(() => {
                slotReleaseTimer = null;
                if (!searchPanel.classList.contains("pulled") && panelSlot) {
                    panelSlot.style.height = "";
                }
            }, 240);
        }, closeDelay);
    }
    updatePullArrow();
}

// The opening animation slides the panel down; once it settles, move the pull
// handle to the panel's real bottom edge.
searchPanel.addEventListener("animationend", (event) => {
    if (event.target === searchPanel && event.animationName === "pull-panel-in") {
        positionPullArrow();
    }
});

pullArrow.addEventListener("click", () => {
    setPulled(!searchPanel.classList.contains("pulled"));
});
if ("ResizeObserver" in window) {
    new ResizeObserver(() => {
        if (searchPanel.classList.contains("pulled")) positionPullArrow();
    }).observe(searchPanel);
}
window.addEventListener("scroll", updatePullArrow, { passive: true });
window.addEventListener("resize", () => {
    if (searchPanel.classList.contains("pulled")) placePulledPanel();
    updatePullArrow();
});
startPullArrowTracking();
updatePullArrow();

// Re-render when the user navigates with the browser's back/forward buttons.
window.addEventListener("popstate", restoreFromUrl);

// Compact / Detailed display switch.
const dictModeButtons = [...document.querySelectorAll(".dict-mode-option")];

function updateDictMode() {
    for (const button of dictModeButtons) {
        const active = (button.dataset.mode === "compact") === state.compact;
        button.setAttribute("aria-pressed", String(active));
    }
}

for (const button of dictModeButtons) {
    button.addEventListener("click", () => {
        const compact = button.dataset.mode === "compact";
        if (state.compact === compact) return;
        state.compact = compact;
        savePrefs();
        updateDictMode();
        entryDictState.clear();
        redraw();
    });
}

// Tap/click support for the compact-mode info bubble. Desktop hover and
// keyboard focus are handled by CSS; touch devices need an explicit toggle.
for (const dictModeInfo of document.querySelectorAll(".dict-mode-info")) {
    const help = dictModeInfo.querySelector(".dict-mode-help");
    const inMenu = Boolean(dictModeInfo.closest(".menu-dropdown"));
    const setInfoOpen = (open) => {
        dictModeInfo.classList.toggle("open", open);
        dictModeInfo.setAttribute("aria-expanded", String(open));
        if (open) placeInfoHelp(dictModeInfo);
    };
    // Inside the menu the bubble is position: fixed (the menu clips anything
    // that overflows it). Put it under `target` (the icon or the switch),
    // right-aligned and kept on screen, so it never covers what it hangs from.
    const placeInfoHelp = (target) => {
        if (!help || !inMenu) return;
        // Measure it while shown; it may still be display: none here.
        help.style.display = "block";
        const rect = target.getBoundingClientRect();
        const margin = 8;
        const left = rect.right - help.offsetWidth + 4;
        const maxLeft = window.innerWidth - help.offsetWidth - margin;
        help.style.left = `${Math.max(margin, Math.min(left, maxLeft))}px`;
        help.style.top = `${rect.bottom + 6}px`;
        help.style.display = "";
    };
    dictModeInfo.addEventListener("mouseenter", () => placeInfoHelp(dictModeInfo));
    dictModeInfo.addEventListener("click", (event) => {
        event.stopPropagation();
        // The icon can sit inside a <label>; do not let the tap flip its switch.
        event.preventDefault();
        setInfoOpen(!dictModeInfo.classList.contains("open"));
    });
    dictModeInfo.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        setInfoOpen(!dictModeInfo.classList.contains("open"));
    });
    document.addEventListener("click", (event) => {
        if (!dictModeInfo.contains(event.target)) setInfoOpen(false);
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") setInfoOpen(false);
    });
}

// Dictionary order, used by compact mode for priority.
function orderForLanguage(lang, anchors) {
    const saved = Array.isArray(dictOrder[lang]) ? dictOrder[lang] : [];
    // Keep saved anchors that are not in the DOM yet. Browser-imported
    // dictionaries are loaded asynchronously, so their saved position must not
    // be dropped by the first (built-in-only) ordering pass on page load.
    const merged = [...saved];
    for (const anchor of anchors) {
        if (!merged.includes(anchor)) merged.push(anchor);
    }
    dictOrder[lang] = merged;
    saveDictOrder();
    // Visual order only for the dictionaries currently in the DOM.
    return merged.filter((anchor) => anchors.includes(anchor));
}

function applyDictOrder(container) {
    const lang = container.closest(".dict-column")?.dataset.lang;
    if (!lang) return;
    const labels = [...container.querySelectorAll("label:not(.dict-loading)")];
    const anchors = labels
        .map((label) => label.querySelector(".dict-toggle")?.dataset.anchor)
        .filter(Boolean);
    const order = orderForLanguage(lang, anchors);
    const byAnchor = new Map(
        labels.map((label) => [
            label.querySelector(".dict-toggle")?.dataset.anchor,
            label,
        ])
    );
    // Import placeholders sit after the real rows. Keep the sorted rows before
    // the first placeholder so an in-progress import is not jumped over.
    const placeholder = container.querySelector("label.dict-loading");
    for (const anchor of order) {
        const label = byAnchor.get(anchor);
        if (!label) continue;
        if (placeholder) container.insertBefore(label, placeholder);
        else container.append(label);
    }
}

let draggedDictLabel = null;

// HTML5 drag and drop works with a mouse, but not with a finger. Use pointer
// events for touch/pen so the same grip can reorder rows on mobile.
function attachHandlePointerDrag(handle, label, container) {
    let pointerId = null;
    let moved = false;

    function cleanup() {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", finish);
        document.removeEventListener("pointercancel", finish);
    }

    function move(event) {
        if (pointerId === null || event.pointerId !== pointerId) return;
        if (event.cancelable) event.preventDefault();
        moved = true;
        const others = [...container.querySelectorAll("label")].filter(
            (other) => other !== label
        );
        let target = null;
        for (const other of others) {
            const rect = other.getBoundingClientRect();
            if (event.clientY < rect.top + rect.height / 2) {
                target = other;
                break;
            }
        }
        if (target) container.insertBefore(label, target);
        else container.append(label);
    }

    function finish(event) {
        if (pointerId === null || event.pointerId !== pointerId) return;
        if (event.cancelable) event.preventDefault();
        cleanup();
        label.classList.remove("dict-dragging");
        try {
            handle.releasePointerCapture(pointerId);
        } catch (error) {
            // Synthetic or already-released pointer.
        }
        pointerId = null;
        if (!moved) return;
        const lang = container.closest(".dict-column")?.dataset.lang;
        if (lang) {
            dictOrder[lang] = [
                ...container.querySelectorAll(".dict-toggle"),
            ].map((toggle) => toggle.dataset.anchor);
            saveDictOrder();
        }
        entryDictState.clear();
        redraw();
        // The server chooses its preview dictionary from this order too.
        rerunSearch();
    }

    handle.addEventListener(
        "pointerdown",
        (event) => {
            if (event.pointerType === "mouse" || pointerId !== null) return;
            event.preventDefault();
            event.stopPropagation();
            pointerId = event.pointerId;
            moved = false;
            label.classList.add("dict-dragging");
            try {
                handle.setPointerCapture(pointerId);
            } catch (error) {
                // Pointer capture is optional; document-level moves still work
                // in browsers that do not support it for synthetic pointers.
            }
            document.addEventListener("pointermove", move, { passive: false });
            document.addEventListener("pointerup", finish);
            document.addEventListener("pointercancel", finish);
        },
        { passive: false }
    );
}
function setupDictionaryDrag(container) {
    for (const label of container.querySelectorAll("label:not(.dict-loading)")) {
        label.draggable = false;
        if (!label.querySelector(".dict-handle")) {
            const handle = document.createElement("span");
            handle.className = "dict-handle";
            handle.draggable = true;
            handle.setAttribute("aria-hidden", "true");
            handle.title = "Drag to reorder";
            // A click on the grip should not toggle the dictionary switch.
            handle.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
            });
            attachHandlePointerDrag(handle, label, container);
            label.prepend(handle);
        }
    }

    // The per-column import plus is only shown while the cursor is on or below
    // the last dictionary row, not anywhere in the language column.
    const column = container.closest(".dict-column");
    if (column && column.dataset.addHoverReady !== "true") {
        column.dataset.addHoverReady = "true";
        const updateAddButton = (event) => {
            const last = [...container.querySelectorAll("label:not(.dict-loading)")].pop();
            const show =
                column.classList.contains("open") &&
                (!last ||
                    event.clientY >= last.getBoundingClientRect().top - 1);
            column.classList.toggle("add-visible", Boolean(show));
        };
        column.addEventListener("mousemove", updateAddButton);
        column.addEventListener("mouseenter", updateAddButton);
        column.addEventListener("mouseleave", () => {
            column.classList.remove("add-visible");
        });
    }

    // Move the dragged row to where the pointer is. Running this on every
    // dragover (not just drop) makes the list follow the pointer and lets a
    // drop in the empty space below the last row still move the item.
    function moveDraggedTo(event) {
        if (!draggedDictLabel || draggedDictLabel.parentElement !== container) return;
        const others = [
            ...container.querySelectorAll("label:not(.dict-loading)"),
        ].filter((other) => other !== draggedDictLabel);
        let target = null;
        for (const other of others) {
            const rect = other.getBoundingClientRect();
            if (event.clientY < rect.top + rect.height / 2) {
                target = other;
                break;
            }
        }
        const placeholder = container.querySelector("label.dict-loading");
        if (target) container.insertBefore(draggedDictLabel, target);
        else if (placeholder) container.insertBefore(draggedDictLabel, placeholder);
        else container.append(draggedDictLabel);
    }

    if (container.dataset.dragReady === "true") return;
    container.dataset.dragReady = "true";
    container.addEventListener("dragstart", (event) => {
        const handle = event.target.closest(".dict-handle");
        if (!handle) return;
        const label = handle.closest("label");
        if (!label || label.parentElement !== container) return;
        draggedDictLabel = label;
        label.classList.add("dict-dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", "");
    });
    container.addEventListener("dragend", () => {
        if (draggedDictLabel) draggedDictLabel.classList.remove("dict-dragging");
        draggedDictLabel = null;
    });
    container.addEventListener("dragover", (event) => {
        if (!draggedDictLabel || draggedDictLabel.parentElement !== container) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        moveDraggedTo(event);
    });
    container.addEventListener("drop", (event) => {
        if (!draggedDictLabel || draggedDictLabel.parentElement !== container) return;
        event.preventDefault();
        moveDraggedTo(event);
        const lang = container.closest(".dict-column")?.dataset.lang;
        if (lang) {
            dictOrder[lang] = [
                ...container.querySelectorAll(".dict-toggle"),
            ].map((toggle) => toggle.dataset.anchor);
            saveDictOrder();
        }
        entryDictState.clear();
        redraw();
        // The server chooses its preview dictionary from this order too.
        rerunSearch();
    });
}

function setupDictionaryOrdering() {
    for (const container of document.querySelectorAll(".dict-items")) {
        applyDictOrder(container);
        setupDictionaryDrag(container);
    }
}

// Small integration surface for the browser-side custom dictionary importer.
window.state = state;
window.toToneless = toToneless;
window.anchorLang = anchorLang;
window.dictToggles = dictToggles;
window.entryDictState = entryDictState;
window.syncGroups = syncGroups;
window.savePrefs = savePrefs;
window.redraw = redraw;
window.groupFor = groupFor;
window.syncDictPanel = syncDictPanel;
window.setupDictionaryOrdering = setupDictionaryOrdering;
window.refreshSearch = () => {
    if (currentQuery && currentQuery.trim()) search(currentQuery);
    else redraw();
};

// On first load: apply saved preferences, then any report-link settings, then
// restore a search from the URL.
loadPrefs();
applySettingsFromUrl();
setupDictionaryOrdering();
// Reserve space so restoring the scroll position does not visibly jump once the
// results render.
prepareInitialScroll();
// Re-open the panel if it was expanded when the page was last left. The
// exact-word view never uses the popped-out panel.
try {
    if (!exactMode && sessionStorage.getItem(PULL_KEY) === "true") setPulled(true);
} catch (error) {
    // Storage disabled - start with the panel in the page.
}
// Focus the search box (what the old `autofocus` attribute did), but without
// scrolling: on a refresh that would jump a restored page back to the top.
input.focus({ preventScroll: true });
restoreFromUrl();

// Keep the two-part title on one line: use the design size (2.5rem) while it
// fits, otherwise scale the whole heading down to the available width.
function fitTitle() {
    const heading = document.querySelector("h1");
    const link = document.getElementById("home-link");
    if (!heading || !link) return;
    // Start from the CSS size, so growing the window can restore it.
    heading.style.fontSize = "";
    const style = getComputedStyle(heading);
    const available = heading.clientWidth;
    // The subtitle lives inside the link, so its width is already included.
    const textWidth = link.scrollWidth;
    if (textWidth <= available) return;
    const size = parseFloat(style.fontSize);
    const scale = (available - 1) / textWidth;
    heading.style.fontSize = `${(size * scale).toFixed(2)}px`;
}
fitTitle();
window.addEventListener("resize", fitTitle);

// Re-enable theme toggle animation after the initial render.
document.documentElement.classList.remove("no-anim");
document.documentElement.style.setProperty("--theme-transition", "180ms");

// --- Page load timing (debug) ---------------------------------------------
// Record how long the UI took to become interactive, so the "Download debug log"
// item can report it. Uses the Navigation Timing and Paint Timing APIs.
function recordPageLoadTiming() {
    try {
        const nav = performance.getEntriesByType("navigation")[0];
        if (nav) {
            recordDebug("page", "Page load timing", null, {
                dnsMs: Math.round(nav.domainLookupEnd - nav.domainLookupStart),
                tcpMs: Math.round(nav.connectEnd - nav.connectStart),
                ttfbMs: Math.round(nav.responseStart - nav.requestStart),
                downloadMs: Math.round(nav.responseEnd - nav.responseStart),
                domParseMs: Math.round(nav.domInteractive - nav.responseEnd),
                domCompleteMs: Math.round(nav.domComplete - nav.startTime),
                loadEventMs: Math.round(nav.loadEventEnd - nav.startTime),
            });
        }
        const paints = performance.getEntriesByType("paint");
        for (const paint of paints) {
            recordDebug("page", paint.name, null, {
                ms: Math.round(paint.startTime),
            });
        }
        // Resource timing for the key scripts.
        for (const name of ["app.js", "custom_dicts.js", "style.css", "variables.css"]) {
            const entries = performance.getEntriesByName(
                new URL(`/static/${name}`, location.href).href
            );
            const last = entries[entries.length - 1];
            if (last) {
                recordDebug("page", `Resource: ${name}`, null, {
                    ttfbMs: Math.round(last.responseStart - last.startTime),
                    downloadMs: Math.round(last.responseEnd - last.responseStart),
                    durationMs: Math.round(last.duration),
                    transferSize: last.transferSize,
                    encodedBodySize: last.encodedBodySize,
                });
            }
        }
    } catch (error) {
        // Timing APIs are best-effort debug data.
    }
}

// Run after the page is fully loaded.
if (document.readyState === "complete") {
    recordPageLoadTiming();
} else {
    window.addEventListener("load", recordPageLoadTiming, { once: true });
}
// --- End page load timing -------------------------------------------------
