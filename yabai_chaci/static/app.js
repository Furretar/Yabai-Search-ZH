// Front-end script for Yabai-Chaci. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const clearButton = document.getElementById("clear-button");
const results = document.getElementById("results");
const resultTiming = document.getElementById("result-timing");
const pagination = document.getElementById("pagination");
const paginationTop = document.getElementById("pagination-top");
const parsed = document.getElementById("parsed");
const hint = document.getElementById("hint");
const scriptToggle = document.getElementById("script-toggle");
const toneToggle = document.getElementById("tone-toggle");
const phoneticToggle = document.getElementById("phonetic-toggle");
// Quick display controls in the Search options panel.
const quickSquares = [...document.querySelectorAll(".quick-script .quick-square")];
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
const DICT_ORDER_KEY = "yabai-chaci-dict-order";
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
};

// The most recent results, kept so the toggles can re-render without re-fetching.
let currentEntries = [];
// Server-reported durations: the initial batch and the whole query. Both are
// shown after the result count when the first batch was rendered on its own.
let lastSearchMs = null;
let firstSearchMs = null;
let currentFuzzy = [];
let currentQuery = "";
// The parsed words from the last multi-word input, so a display toggle (e.g.
// Zhuyin) can re-render them without re-annotating.
let currentTokens = [];

// --- Copyable debug log ------------------------------------------------
// A small in-memory log for the "Copy debug log" footer button. It collects
// search milestones and every console.error (including the stack), so a bug
// report can show the current state and the traceback that led to it.
const DEBUG_EVENT_LIMIT = 200;
const debugEvents = [];
let currentDebugSearch = null;
// Counts for the "*" pager's shortcut to the imported-dictionary results.
let lastWildcardCustom = null;

function debugErrorText(error) {
    if (!error) return "";
    if (error.stack) return String(error.stack);
    if (error.message) return `${error.name || "Error"}: ${error.message}`;
    return String(error);
}

// Record one event. While a search is active it is also attached to that
// search, which is what "traceback from the current search" reports.
function recordDebug(level, message, error = null) {
    const entry = {
        at: new Date().toISOString(),
        level,
        message: String(message),
    };
    if (error) entry.stack = debugErrorText(error);
    if (currentDebugSearch) {
        entry.query = currentDebugSearch.query;
        entry.offset = currentDebugSearch.offset;
        currentDebugSearch.events.push(entry);
        if (currentDebugSearch.events.length > DEBUG_EVENT_LIMIT) {
            currentDebugSearch.events.shift();
        }
    }
    debugEvents.push(entry);
    if (debugEvents.length > DEBUG_EVENT_LIMIT) debugEvents.shift();
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

function beginSearchDebug(query, offset, seq) {
    currentDebugSearch = {
        query: String(query),
        offset,
        seq,
        startedAt: new Date().toISOString(),
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

function debugSnapshot() {
    const currentState = {
        script: state.script,
        toneColoring: state.toneColoring,
        phonetic: state.phonetic,
        taiwan: state.taiwan,
        customDicts: state.customDicts,
        selectedDictionaries: [...state.dicts].sort(),
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
        currentState,
        customDictionaries: readCustomDictionaryDebugInfo(),
        customDictionaryStats: window.customDictsStats || null,
        currentSearch,
        recentEvents: debugEvents.slice(-50),
    };
    return "Yabai-Chaci debug log\n" + JSON.stringify(info, null, 2) + "\n";
}

// Fallback for browsers where the async clipboard API is unavailable.
function copyTextFallback(text) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    let copied = false;
    try {
        copied = document.execCommand("copy");
    } catch (error) {
        recordDebug("error", "Clipboard fallback failed", error);
    }
    area.remove();
    return copied;
}

async function copyDebugLog() {
    const button = document.getElementById("copy-log-button");
    const text = debugSnapshot();
    let copied = false;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            copied = true;
        }
    } catch (error) {
        recordDebug("error", "Clipboard write failed", error);
    }
    if (!copied) copied = copyTextFallback(text);
    if (!button) return;
    button.textContent = copied ? "Copied" : "Copy failed";
    button.classList.toggle("copied", copied);
    window.setTimeout(() => {
        button.textContent = "Copy debug log";
        button.classList.remove("copied");
    }, 1500);
}

const copyLogButton = document.getElementById("copy-log-button");
if (copyLogButton) copyLogButton.addEventListener("click", copyDebugLog);

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
    for (const match of source.matchAll(/[\p{Script=Latin}:]+[0-9]?/gu)) {
        pushAnnotation(source.slice(cursor, match.index));
        cursor = match.index + match[0].length;
        const raw = match[0];
        const toneMatch = raw.match(/^(.*?)([1-5])$/);
        const letters = (toneMatch ? toneMatch[1] : raw).replace(/u:/g, "ü");
        const tone = toneMatch ? toneMatch[2] : "";
        const parts = splitPinyinChunk(letters);
        if (!parts) {
            tokens.push(raw);
            continue;
        }
        parts.forEach((part, index) => {
            if (tone && index === parts.length - 1) {
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

// One cell of the furigana grid: kind is "reading" (top row) or "char" (bottom
// row); `column` is the CSS grid-column value. Tone coloring, when on, adds a
// tone class so the cell can be colored.
function gridCell(kind, content, column, tone) {
    const cell = document.createElement("span");
    cell.className = tone ? `${kind} tone${tone}` : kind;
    cell.textContent = content;
    cell.style.gridRow = kind === "reading" ? "1" : "2";
    cell.style.gridColumn = column;
    return cell;
}

// Build furigana: a two-row grid, one column per character, so each reading sits
// above its character and the word is as wide as its reading. Readings come
// first in the DOM (then all characters) so a range over a single kind is
// contiguous - used by the double-click handler below.
// Build the reading text for a group of syllables: one tone-colored span per
// syllable when coloring is on, otherwise a single plain text node.
function readingContent(readings, syllables, colored = state.toneColoring) {
    if (!colored) {
        return document.createTextNode(readings.join("\u00a0"));
    }
    const fragment = document.createDocumentFragment();
    readings.forEach((reading, i) => {
        if (i > 0) fragment.append("\u00a0");
        const span = document.createElement("span");
        span.className = `tone${toneOf(syllables[i])}`;
        span.textContent = reading;
        fragment.append(span);
    });
    return fragment;
}

// One punctuation/symbol character: these never take a reading.
const PUNCT_CHAR_RE = /^[\p{P}\p{S}]$/u;
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

function furigana(text, pinyin, allowTones = true) {
    const chars = [...text];
    const syllables = pinyinSyllables(pinyin).filter(
        (syllable) => syllable && !PUNCT_TOKEN_RE.test(syllable)
    );
    // The readings in the chosen phonetic system.
    const readings = syllables.map((s) => (state.phonetic === "zhuyin" ? toZhuyin(s) : s));
    const node = document.createElement("span");
    node.className = "furigana";
    // Some readings (Wu romanizations) are shown, but not tone-coloured: the
    // colours are Hanyu Pinyin tones, which do not apply to them.
    const colored = state.toneColoring && allowTones;

    // Match readings to characters, ignoring punctuation. Without this a phrase
    // like 除了他，谁也没来 (one comma, so one more character than syllable) would
    // fall back to a single uncolored character row.
    const spoken = chars.filter((char) => !PUNCT_CHAR_RE.test(char));
    // Numeric headwords never use the per-character layout: a digit and a
    // syllable do not line up (1500 = yī qiān wǔ bǎi). The fallback below keeps
    // most digits white and colours the characters after the number.
    const leadingDigits = (text.match(/^\d+/) || [""])[0];
    if (!leadingDigits && spoken.length === syllables.length) {
        // One wrapper per character keeps its reading directly above it. The
        // wrappers are flex items, so a long headword can wrap between
        // characters on a narrow screen.
        const readingCells = new Array(chars.length);
        let index = 0;
        chars.forEach((char, i) => {
            if (PUNCT_CHAR_RE.test(char)) return;
            const tone = colored ? toneOf(syllables[index]) : 0;
            readingCells[i] = gridCell(
                "reading",
                readings[index],
                String(i + 1),
                tone
            );
            index += 1;
        });
        index = 0;
        chars.forEach((char, i) => {
            const column = document.createElement("span");
            column.className = "furi-col";
            if (readingCells[i]) column.append(readingCells[i]);
            const punctuation = PUNCT_CHAR_RE.test(char);
            const tone = !punctuation && colored ? toneOf(syllables[index]) : 0;
            column.append(gridCell("char", char, String(i + 1), tone));
            if (!punctuation) index += 1;
            node.append(column);
        });
    } else {
        // Counts differ: put the whole reading above the whole word (still colored).
        const column = document.createElement("span");
        column.className = "furi-col";

        const reading = document.createElement("span");
        reading.className = "reading";
        reading.append(readingContent(readings, syllables, colored));

        const char = document.createElement("span");
        char.className = "char";
        // Numbers often have more characters than syllables (10000 = yī wàn),
        // so give each digit the tone of the syllable that reads it.
        let tones = colored ? numberTones(chars, syllables) : null;
        if (!tones && colored) tones = trailingTones(chars, syllables);
        if (tones) {
            node.classList.add("segmented");
            chars.forEach((ch, i) => {
                const cell = document.createElement("span");
                cell.textContent = ch;
                // Uncoloured pieces (digits) use the default colour, so their
                // underline is white/black rather than the link accent.
                cell.className = tones[i] ? `tone${tones[i]}` : "plain";
                char.append(cell);
            });
        } else {
            char.textContent = text;
        }

        column.append(reading, char);
        node.append(column);
    }
    return node;
}

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
function copyReading(pinyin) {
    if (!pinyin || state.phonetic !== "zhuyin") return pinyin || "";
    return pinyinSyllables(pinyin)
        .map((syllable) => toZhuyin(syllable))
        .join(" ");
}

function copyDefinitionButton(reading, word, definition) {
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
    button.append(svg);

    const text = [reading, word, definition].filter(Boolean).join("\n");
    button.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
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
        setTimeout(() => {
            button.classList.remove("copied");
            button.title = "Copy definition";
        }, 1200);
    });
    return button;
}

function definitionBody(source, taiwanRefs, copy) {
    const body = document.createDocumentFragment();
    // Only English-language definitions get automatic Chinese links; a Chinese
    // dictionary's own prose (MOE) is not a list of headwords to look up.
    const linkChinese = source.lang === "english";

    // Senses, grouped by reading. CEDICT polyphonic words get a header per
    // reading; other sources have a single (unlabelled) group.
    const groups =
        source.groups && source.groups.length
            ? source.groups
            : [{ pinyin: "", senses: source.defs || [] }];
    // Only label the readings when a word actually has more than one.
    const showReadings = groups.length > 1;
    for (const group of groups) {
        if (showReadings && group.pinyin) {
            const head = document.createElement("div");
            head.className = "def-reading";
            const syllables = pinyinSyllables(group.pinyin);
            const readings = syllables.map((s) => (state.phonetic === "zhuyin" ? toZhuyin(s) : s));
            head.append(readingContent(readings, syllables, state.toneColoring));
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
                        copyReading(group.pinyin || copy.reading),
                        copy.word,
                        text
                    )
                );
            }
            body.append(line);
        });
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
            if (i > 0) line.append(document.createTextNode("; "));
            // MOE stores a prose note instead of a single classifier word.
            if (classifier.note) {
                line.append(document.createTextNode(classifier.note));
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

// A labeled block showing one dictionary's definitions for a result. CC-CEDICT
// and Wiktionary names open that word in the source; other names link to their
// section on the About page.
function definitionsBlock(source, taiwanRefs, word, reading) {
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
    } else if (source.anchor.startsWith("custom-")) {
        // Browser-imported dictionaries have no About page entry.
        name.removeAttribute("href");
    } else {
        name.href = `/about#${encodeURIComponent(source.anchor)}`;
    }
    name.textContent = source.label;
    block.append(name, definitionBody(source, taiwanRefs, { reading, word }));
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

// The initial dictionaries for a result. Detailed mode uses every selected
// dictionary; compact mode keeps only the highest-ordered dictionary in each
// language that actually has a definition for the entry.
function initialDictSelection(entry) {
    const selected = new Set(state.dicts);
    if (!state.compact) return selected;
    const byLang = new Map();
    for (const source of entry.definitions || []) {
        if (!selected.has(source.anchor)) continue;
        const lang = source.lang || anchorLang.get(source.anchor) || "";
        if (!byLang.has(lang)) byLang.set(lang, []);
        byLang.get(lang).push(source.anchor);
    }
    const result = new Set();
    for (const [lang, anchors] of byLang) {
        const order = Array.isArray(dictOrder[lang]) ? dictOrder[lang] : [];
        anchors.sort((a, b) => {
            const ia = order.indexOf(a);
            const ib = order.indexOf(b);
            return (
                (ia < 0 ? Number.MAX_SAFE_INTEGER : ia) -
                (ib < 0 ? Number.MAX_SAFE_INTEGER : ib)
            );
        });
        result.add(anchors[0]);
    }
    return result;
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
    if (enabled.has(anchor)) enabled.delete(anchor);
    else enabled.add(anchor);
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
        const readings = syllables.map((s) => (state.phonetic === "zhuyin" ? toZhuyin(s) : s));
        row.append(
            readingContent(readings, syllables, state.toneColoring && !entry.pinyin_no_tone),
        );
        node.append(row);
    }
    const imageRow = document.createElement("span");
    imageRow.className = "ids-images";
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
    // A 5+ character headword no longer fits the steady 10rem headword column;
    // stack its definitions underneath instead of pushing them to the right.
    row.classList.toggle("long-headword", [...primary].length >= 5);
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
        hanzi.append(furigana(primary, reading, !entry.pinyin_no_tone));
    }
    head.append(hanzi);
    // A dialect reading (Xiang, Wu, Shanghainese) is tagged next to the word.
    if (entry.pinyin_label) {
        const label = document.createElement("span");
        label.className = "dialect-label";
        label.textContent = entry.pinyin_label;
        head.append(label);
    }

    // SUBTLEX-CH frequency: a chip-like box under the headword, above the chips.
    if (entry.freq) {
        const freq = document.createElement("div");
        freq.className = "freq";
        const chip = document.createElement("span");
        chip.className = "source";
        chip.textContent = `Frequency: #${entry.freq.rank.toLocaleString()}`;
        freq.append(chip);
        head.append(freq);
    }

    const enabled = visibleDicts(entry);
    // The selected language's dictionaries always come first in the entry.
    // Within a language, the order comes from the draggable dictionary list.
    const langGroup = selectedLangGroup();
    const langOrder = new Map();
    for (const source of entry.definitions || []) {
        if (!langOrder.has(source.lang)) langOrder.set(source.lang, langOrder.size);
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
    // stable order so clicking one does not make the row reshuffle.
    if (definitions.length && state.customDicts) {
        const box = document.createElement("div");
        box.className = "sources";
        for (const source of definitions) {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = enabled.has(source.anchor) ? "source on" : "source";
            chip.dataset.anchor = source.anchor;
            chip.textContent = source.label;
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
                notes.push(detailRow("Taiwan pronunciation: ", furigana(primary, numericToMarked(note))));
            }
        }
        for (const note of prons.others) {
            const label = note.label ? `Also pronounced (${note.label}): ` : "Also pronounced: ";
            notes.push(detailRow(label, furigana(primary, numericToMarked(note.reading))));
        }
        if (entry.forms && entry.forms.length) {
            const fragment = document.createDocumentFragment();
            entry.forms.forEach((form, i) => {
                if (i) fragment.append("; ");
                // Shown as-is, without the traditional/simplified toggle.
                const word = form.traditional;
                if (!word) {
                    if (form.pinyin) fragment.append(form.pinyin);
                    return;
                }
                // Every form is a search link too, with furigana when known.
                fragment.append(
                    form.pinyin
                        ? searchLink(word, furigana(word, form.pinyin))
                        : searchLink(word)
                );
            });
            notes.push(detailRow("Other forms: ", fragment));
        }
    }

    let notesPlaced = false;
    for (const source of definitions) {
        if (!enabled.has(source.anchor)) continue;
        defsBox.append(definitionsBlock(source, entry.taiwan_refs, primary, reading));
        if (source.anchor === "cedict" && notes.length) {
            defsBox.append(...notes);
            notesPlaced = true;
        }
    }
    if (notes.length && !notesPlaced) defsBox.append(...notes);

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

    row.append(head, defsBox);
}

// A result is shown only when one of the selected dictionaries has a definition
// for it; a word found only in unselected dictionaries lives in the "Matches in
// other dictionaries" section instead.
function hasSelectedDefs(entry) {
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
        definitions: (entry.definitions || []).map((source) => ({
            ...source,
            groups: (source.groups || []).map((group) => ({
                pinyin: group.pinyin,
                senses: [...(group.senses || [])],
            })),
            defs: [...(source.defs || [])],
            classifiers: (source.classifiers || []).map((c) => ({ ...c })),
        })),
    };
}

// Main-list row count, used when the Fuzzy section adds more rows.
let mainResultCount = 0;

// Add one entry's definitions, source chips and forms to another entry.
function mergeEntry(target, addition) {
    for (const source of addition.definitions || []) {
        const block = target.definitions.find((s) => s.anchor === source.anchor);
        if (!block) {
            target.definitions.push(source);
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
    const byWord = new Map();
    for (const entry of entries) {
        const key = displayWord(entry);
        if (!byWord.has(key)) byWord.set(key, cloneEntry(entry));
        else mergeEntry(byWord.get(key), entry);
    }
    return [...byWord.values()];
}

// Build the results list from the array of entries returned by the API.
function render(entries, query) {
    currentEntries = entries;
    currentQuery = query;
    const merged = mergeEntries(entries);
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
    count.textContent = `${merged.length} RESULT${merged.length === 1 ? "" : "S"}`;
    results.append(count);
    updateTiming();

    for (const entry of withDefs) results.append(entryRow(entry));
    for (const entry of withoutReadingWithDefs) results.append(entryRow(entry));
    for (const entry of idsMain) results.append(entryRow(entry));
    for (const entry of dialectMain) results.append(entryRow(entry));
    if (otherEntries.length) {
        const heading = document.createElement("p");
        heading.className = "other-heading";
        heading.textContent = "Other Dictionaries";
        results.append(heading);
        for (const entry of otherEntries) results.append(entryRow(entry));
    }
    for (const entry of headwordsOnly) results.append(entryRow(entry));
    for (const entry of withoutReadingOnly) results.append(entryRow(entry));
}

// Move the query timings to the line above the footer.
function updateTiming() {
    if (!resultTiming) return;
    if (lastSearchMs === null) {
        resultTiming.textContent = "";
        return;
    }
    const parts = [];
    if (firstSearchMs !== null) parts.push(`First Result: ${firstSearchMs} ms`);
    parts.push(`All Results: ${lastSearchMs} ms`);
    resultTiming.textContent = parts.join(", ");
}

// Append the tone-insensitive "Fuzzy match" section under a separator.
function renderFuzzy(entries) {
    currentFuzzy = entries || [];
    if (!currentFuzzy.length) return;
    // The count covers both the main list and these fuzzy rows.
    const total = mainResultCount + currentFuzzy.length;
    const count = results.querySelector(".results-count");
    if (count) count.textContent = `${total} RESULT${total === 1 ? "" : "S"}`;
    const heading = document.createElement("p");
    heading.className = "fuzzy-heading";
    heading.textContent = "Fuzzy match";
    results.append(heading);
    for (const entry of currentFuzzy) results.append(entryRow(entry));
}

// Re-draw the current results (and any fuzzy section) after a display toggle.
function redraw() {
    // Re-render the parsed words too, so a phonetic/script/tone change applies to
    // them (they are otherwise only drawn once, in annotate()).
    if (currentTokens.length) renderParsed(currentTokens);
    if (!currentEntries.length && !currentFuzzy.length) return;
    render(currentEntries, currentQuery);
    renderFuzzy(currentFuzzy);
}

// Query the API and hand the results to render().
// A monotonically increasing id per search, so a late response from an old
// query is ignored once the user has typed a new one.
let searchSeq = 0;

async function search(query, offset = 0, scrollMode = false, recordTiming = true) {
    const url = `/api/search?q=${encodeURIComponent(query)}&taiwan=${state.taiwan ? 1 : 0}&offset=${offset}`;
    const startedAt = performance.now();
    const seq = ++searchSeq;
    const allWildcard = query.trim() === "*";
    lastWildcardCustom = null;
    beginSearchDebug(query, offset, seq);
    // The footer (About link) is pinned to the bottom; hide it while results
    // are pending so it cannot flash/jump as the results area refills.
    document.body.classList.add("searching");

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
    const firstResponse = await fetch(url + "&part=primary", { cache: "no-store" });
    const first = await firstResponse.json();
    if (seq !== searchSeq) return 0;
    if (currentDebugSearch) {
        currentDebugSearch.official = {
            total: first.total,
            offset: first.offset,
            pageSize: first.page_size,
            results: Array.isArray(first.results) ? first.results.length : 0,
            ms: first.ms,
        };
    }
    recordDebug("response", "Primary search response received");
    entryDictState.clear(); // a fresh search starts from the global selection
    const firstMs = typeof first.ms === "number" ? first.ms : null;
    const showFirst = first.total == null && offset === 0 && first.results.length >= 1;
    if (showFirst) {
        currentEntries = first.results;
        currentQuery = query;
        if (recordTiming) {
            lastSearchMs = firstMs;
            firstSearchMs = null;
        }
        render(first.results, query);
        renderFuzzy([]);
        updateHint(query, first.pinyin_match, first.english_match);
    }

    // Keep the page number in the URL (without clobbering the query), so a
    // reload or a shared link returns to the same page.
    const size = first.page_size || RESULTS_PAGE_SIZE;
    const page = Math.floor(offset / size) + 1;
    const current = new URL(location.href);
    if (page > 1) current.searchParams.set("page", String(page));
    else current.searchParams.delete("page");
    current.searchParams.delete("offset"); // older links used a raw offset
    history.replaceState({ ...(history.state || {}), page }, "", current);

    // A "*" page comes complete in one response (its `total` is set).
    if (first.total == null) {
        // Phase 2: the complete final list, slow source tiers included. Start
        // it without waiting for the local dictionary search.
        const fullResponse = await fetch(url, { cache: "no-store" });
        const full = await fullResponse.json();
        if (seq !== searchSeq) return 0;
        recordDebug(
            "response",
            `Complete search response: ${full.results.length} results, total ${full.total}`
        );

        // Show official results first; custom matches follow as a third tier.
        currentEntries = full.results;
        currentQuery = query;
        if (recordTiming) {
            firstSearchMs = showFirst ? firstMs : null;
            lastSearchMs = Math.round(performance.now() - startedAt);
        }
        render(full.results, query);
        renderFuzzy(full.fuzzy);
        renderPagination(query, full);
        updateHint(query, full.pinyin_match, full.english_match);
        document.body.classList.remove("searching");
        applyPagerScroll(scrollMode);

        const customResult = await customMatchesPromise;
        if (seq !== searchSeq) return 0;
        const matches = Array.isArray(customResult)
            ? customResult
            : customResult.matches || [];
        const allHeadwordMatches = [...matches];
        const fastKeys = new Set(matches.map(customMatchKey));
        if (matches.length) {
            await applyCustomMatches(full, matches);
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
        }
        if (!Array.isArray(customResult) && customResult.slow) {
            const slow = await customResult.slow;
            if (seq !== searchSeq) return 0;
            const extra = slow.filter((match) => !fastKeys.has(customMatchKey(match)));
            if (extra.length) {
                allHeadwordMatches.push(...extra);
                await applyCustomMatches(full, extra);
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
        return full.results.length;
    }

    // Wildcard results are complete already. Show the official rows, then merge
    // the local dictionary tier when it is ready.
    currentEntries = first.results;
    currentQuery = query;
    if (recordTiming) {
        firstSearchMs = null;
        lastSearchMs = Math.round(performance.now() - startedAt);
    }
    render(first.results, query);
    renderFuzzy(first.fuzzy);
    renderPagination(query, first);
    updateHint(query, first.pinyin_match, first.english_match);
    document.body.classList.remove("searching");
    applyPagerScroll(scrollMode);

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
                await applyCustomMatches(first, customPage.matches);
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
        return first.results.length;
    }

    const customResult = await customMatchesPromise;
    if (seq !== searchSeq) return 0;
    const matches = Array.isArray(customResult)
        ? customResult
        : customResult.matches || [];
    const fastKeys = new Set(matches.map(customMatchKey));
    if (matches.length) {
        await applyCustomMatches(first, matches);
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
            await applyCustomMatches(first, extra);
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
    }
    return first.results.length;
}

// Merge already-fetched browser dictionary matches into an API response.
function customMatchKey(match) {
    return `${match.dictId}|${match.term}|${match.reading}`;
}

async function applyCustomMatches(data, matches) {
    if (!window.mergeCustomMatches) return;
    try {
        await window.mergeCustomMatches(data, matches);
    } catch (error) {
        console.error("Custom dictionary merge failed:", error);
    }
}

function repaintMergedRows(data, query, recordTiming, startedAt, scrollMode) {
    entryDictState.clear();
    currentEntries = data.results;
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
            query.includes("*") ||
            /[1-5]/.test(query) ||
            /\s/.test(query.trim()))
    ) {
        return { added: false, matches: [] };
    }
    const remaining = Math.max(0, RESULTS_PAGE_SIZE - headwordMatches.length);
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
    await applyCustomMatches(temp, matches);
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

    return customJump ? [prev, label, customJump, next] : [prev, label, next];
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
    parsed.replaceChildren();
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
        parsed.append(node);
        if (dialect) {
            const label = document.createElement("span");
            label.className = "dialect-label";
            label.textContent = dialect;
            parsed.append(label);
        }
    }
}

// --- scroll position across reloads and back/forward ---------------------
// Results arrive from fetch(), so the browser's own scroll restoration happens
// before they are in the DOM and lands at the top. Save each URL's position
// ourselves and restore it once that URL's results are rendered.
if ("scrollRestoration" in history) history.scrollRestoration = "manual";
const SCROLL_KEY = "yabai-chaci-scroll";
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
async function annotate(text) {
    const response = await fetch("/api/annotate", {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
    });
    const data = await response.json();
    clearResults();
    // Show the parsed words for any multi-word input, even when the words only
    // exist outside CEDICT (e.g. 一償宿願 from MOE) and so have no entries.
    if (data.tokens.length > 1) renderParsed(data.tokens);
    // Search what was typed first: a direct query (including a wildcard like
    // 一償*) must be searched as-is. Only when the whole input finds nothing (a
    // pasted sentence) fall back to the first parsed word.
    const found = await search(text);
    if (!found && data.tokens.length && data.tokens[0].text !== text) {
        await search(data.tokens[0].text);
    }
    // Restore the saved position only now that the results are in the DOM.
    restoreScroll();
}

// Put the current query in the browser tab title (like Jisho), or the site name
// when there is no query.
function setTitle(query) {
    document.title = query ? `${query} - Yabai Search ZH` : "Yabai Search ZH";
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

// Results per page (matches db.ALL_PAGE_SIZE = 50) for turning ?page=N into an
// offset before the first request.
const RESULTS_PAGE_SIZE = 50;

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
                "yabai-chaci-panel-" +
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
    setTitle(query);
    showTagline(false);
    history.pushState({ q: query }, "", `?q=${encodeURIComponent(query)}`);
    annotate(query);
}

// Restore the view (results or home) from whatever is in the URL now.
function restoreFromUrl() {
    const query = queryFromUrl();
    const page = pageFromUrl();
    if (query) {
        input.value = query;
        setTitle(query);
        showTagline(false);
        // A paged "*" result: go straight to the requested page. Restore the
        // saved scroll once the page's results are in the DOM (browser scroll
        // anchoring can otherwise land near the bottom).
        if (page > 1) search(query, (page - 1) * RESULTS_PAGE_SIZE).finally(restoreScroll);
        else annotate(query);
    } else {
        input.value = "";
        setTitle("");
        clearResults();
        showTagline(true);
        restoreScroll();
    }
}

// Return to the home view: no query, no results, tagline shown, URL reset.
function goHome() {
    input.value = "";
    setTitle("");
    clearResults();
    showTagline(true);
    history.pushState({}, "", location.pathname);
}

// Load the display preferences from localStorage (if it's available).
function loadPrefs() {
    try {
        const script = localStorage.getItem("yabai-chaci-script");
        if (script === "traditional" || script === "simplified") state.script = script;
        // Tone coloring is on by default; only an explicit "false" disables it.
        state.toneColoring = localStorage.getItem("yabai-chaci-tone") !== "false";
        state.phonetic = localStorage.getItem("yabai-chaci-phonetic") === "zhuyin" ? "zhuyin" : "pinyin";
        state.taiwan = localStorage.getItem("yabai-chaci-taiwan") === "true";
        state.customDicts = localStorage.getItem("yabai-chaci-custom-dicts") === "true";
        state.compact = localStorage.getItem("yabai-chaci-compact") === "true";
        state.lang = localStorage.getItem("yabai-chaci-lang") || "EN";
        loadDictOrder();
        const stored = JSON.parse(localStorage.getItem("yabai-chaci-dicts") || "null");
        // On a fresh start use the language's defaults (EN -> cedict only).
        state.dicts =
            stored === null
                ? new Set(langDefaults[state.lang] || langDefaults.EN || ["cedict"])
                : new Set(stored);
    } catch (e) {
        // localStorage unavailable - keep the defaults.
    }
    scriptToggle.checked = state.script === "traditional";
    toneToggle.checked = state.toneColoring;
    phoneticToggle.checked = state.phonetic === "zhuyin";
    taiwanToggle.checked = state.taiwan;
    customDictsToggle.checked = state.customDicts;
    for (const input of dictToggles) input.checked = state.dicts.has(input.dataset.anchor);
    updateQuickToggles();
    syncGroups();
    syncDictPanel();
    applyLang(state.lang);
}

// Persist the display preferences.
function savePrefs() {
    try {
        localStorage.setItem("yabai-chaci-script", state.script);
        localStorage.setItem("yabai-chaci-tone", String(state.toneColoring));
        localStorage.setItem("yabai-chaci-phonetic", state.phonetic);
        localStorage.setItem("yabai-chaci-taiwan", String(state.taiwan));
        localStorage.setItem("yabai-chaci-custom-dicts", String(state.customDicts));
        localStorage.setItem("yabai-chaci-compact", String(state.compact));
        localStorage.setItem("yabai-chaci-dicts", JSON.stringify([...state.dicts]));
        localStorage.setItem("yabai-chaci-lang", state.lang);
    } catch (e) {
        // localStorage unavailable - nothing to save.
    }
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
customDictsToggle.addEventListener("change", () => {
    state.customDicts = customDictsToggle.checked;
    savePrefs();
    syncDictPanel();
    // Re-render so the per-entry dictionary chips appear/disappear too.
    redraw();
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
        if (toggle.checked) state.dicts.add(toggle.dataset.anchor);
        else state.dicts.delete(toggle.dataset.anchor);
        syncGroups();
        entryDictState.clear();
        savePrefs();
        redraw();
    });
}

// Each language header has a disclosure arrow. It controls its dictionary list
// manually; selecting or clearing dictionaries never collapses the list. The
// open languages are remembered across reloads.
const DICT_OPEN_KEY = "yabai-chaci-dict-open";

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
            if (event.target.closest(".dict-disclosure, .dict-remove")) return;
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

// Selecting a search language switches on its default dictionaries and off all
// the others (see yabai_chaci/default_dictionaries.py).
function applyDictDefaults(value) {
    if (!(value in langDefaults)) return;
    state.dicts = new Set(langDefaults[value]);
    for (const toggle of dictToggles) {
        toggle.checked = state.dicts.has(toggle.dataset.anchor);
    }
    syncGroups();
    entryDictState.clear();
}

langButton.addEventListener("click", () => setLangOpen(langMenu.hidden));
langMenu.addEventListener("click", (event) => {
    const item = event.target.closest("[role='option']");
    if (!item) return;
    applyLang(item.dataset.value);
    // In custom-dictionary mode the user controls the dictionary selection;
    // changing the search language must not toggle any dictionaries.
    if (!state.customDicts) applyDictDefaults(item.dataset.value);
    savePrefs();
    setLangOpen(false);
    redraw(); // the selected language or default dictionaries may have changed
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

// On phones the two disclosures behave like an accordion.
const narrowOptionsQuery = window.matchMedia("(max-width: 600px)");

function applyOptionPanelState(panel, open, save = true) {
    panel.classList.toggle("open", open);
    const toggle = panel.querySelector(".options-toggle");
    if (toggle) {
        toggle.setAttribute("aria-expanded", String(open));
        if (save) {
            try {
                const key =
                    "yabai-chaci-panel-" +
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
    if (open && narrowOptionsQuery.matches) {
        const others = [
            ...document.querySelectorAll(".options.open"),
        ].filter((other) => other !== panel);
        const searchPanel = panel.closest(".search-panel");
        if (searchPanel && others.length) {
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
    }
    applyOptionPanelState(panel, open, save);
}

for (const panel of document.querySelectorAll(".options")) {
    const toggle = panel.querySelector(".options-toggle");
    const key = "yabai-chaci-panel-" + (toggle.getAttribute("aria-controls") || "");
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

// The clear (✕) button empties the box, returns to the home view, and re-focuses
// the input - which reopens the keyboard on mobile.
clearButton.addEventListener("click", () => {
    goHome();
    input.focus();
});

// Clicking the title returns to the home view.
document.getElementById("home-link").addEventListener("click", (event) => {
    event.preventDefault(); // handle it in JS instead of reloading the page
    goHome();
});

// Copy text for a selection inside one furigana group, Jisho-style: the whole
// reading, a space, then the word (e.g. "diàn nǎo 电脑"). Selecting just the
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

    const selected = (selector) =>
        [...group.querySelectorAll(selector)]
            .filter((node) => range.intersectsNode(node))
            .map((node) => node.textContent);
    const readings = selected(".reading");
    const chars = selected(".char");
    if (!readings.length && !chars.length) return null;
    // The grid cells copy with newlines between them; join them into one line.
    if (readings.length && chars.length) {
        return `${readings.join(" ")} ${chars.join("")}`;
    }
    return readings.length ? readings.join(" ") : chars.join("");
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
const PULL_KEY = "yabai-chaci-pulled";
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
const dictModeInfo = document.querySelector(".dict-mode-info");
if (dictModeInfo) {
    const setInfoOpen = (open) => {
        dictModeInfo.classList.toggle("open", open);
        dictModeInfo.setAttribute("aria-expanded", String(open));
    };
    dictModeInfo.addEventListener("click", (event) => {
        event.stopPropagation();
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
    });
    container.addEventListener("drop", (event) => {
        if (!draggedDictLabel || draggedDictLabel.parentElement !== container) return;
        event.preventDefault();
        const target = event.target.closest("label");
        if (target && target !== draggedDictLabel) {
            const rect = target.getBoundingClientRect();
            const before = event.clientY < rect.top + rect.height / 2;
            container.insertBefore(
                draggedDictLabel,
                before ? target : target.nextSibling
            );
        } else if (!target) {
            container.append(draggedDictLabel);
        }
        const lang = container.closest(".dict-column")?.dataset.lang;
        if (lang) {
            dictOrder[lang] = [
                ...container.querySelectorAll(".dict-toggle"),
            ].map((toggle) => toggle.dataset.anchor);
            saveDictOrder();
        }
        entryDictState.clear();
        redraw();
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

// On first load: apply saved preferences, then restore a search from the URL.
loadPrefs();
setupDictionaryOrdering();
// Reserve space so restoring the scroll position does not visibly jump once the
// results render.
prepareInitialScroll();
// Re-open the panel if it was expanded when the page was last left.
try {
    if (sessionStorage.getItem(PULL_KEY) === "true") setPulled(true);
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
    const sub = heading && heading.querySelector(".site-subtitle");
    if (!heading || !link || !sub) return;
    // Start from the CSS size, so growing the window can restore it.
    heading.style.fontSize = "";
    const style = getComputedStyle(heading);
    const gap = parseFloat(style.columnGap) || parseFloat(style.gap) || 0;
    const available = heading.clientWidth;
    const textWidth = link.scrollWidth + sub.scrollWidth;
    if (textWidth + gap <= available) return;
    const size = parseFloat(style.fontSize);
    const scale = (available - gap - 1) / textWidth;
    heading.style.fontSize = `${(size * scale).toFixed(2)}px`;
}
fitTitle();
window.addEventListener("resize", fitTitle);
