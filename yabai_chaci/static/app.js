// Front-end script for Yabai-Chaci. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const clearButton = document.getElementById("clear-button");
const results = document.getElementById("results");
const pagination = document.getElementById("pagination");
const paginationTop = document.getElementById("pagination-top");
const parsed = document.getElementById("parsed");
const hint = document.getElementById("hint");
const scriptToggle = document.getElementById("script-toggle");
const toneToggle = document.getElementById("tone-toggle");
const phoneticToggle = document.getElementById("phonetic-toggle");
const taiwanToggle = document.getElementById("taiwan-toggle");
const customDictsToggle = document.getElementById("custom-dicts-toggle");
// Dictionaries panel: group switches (English/Mainland/Taiwan) and per-dict.
const dictGroupToggles = [...document.querySelectorAll(".dict-group")];
const dictToggles = [...document.querySelectorAll(".dict-toggle")];
// The whole "Dictionaries" disclosure, shown only when "Custom dictionaries" is on.
const dictPanel = document.querySelector(".options.dictionaries");

// Search-language dropdown (left of the search box): EN / ZH / TW.
const langSelect = document.querySelector(".lang-select");
const langButton = langSelect.querySelector(".lang-button");
const langMenu = langSelect.querySelector(".lang-menu");
// Default dictionaries per language (from default_dictionaries.py).
const langDefaults = JSON.parse(langSelect.dataset.defaults || "{}");

// Display options. script: "simplified" | "traditional"; toneColoring: on/off;
// phonetic: "pinyin" | "zhuyin"; taiwan: use the Taiwan reading when listed;
// customDicts: show the Dictionaries picker; dicts: the selected dictionaries;
// lang: the selected search language (EN / ZH / TW).
const state = {
    script: "simplified",
    toneColoring: true,
    phonetic: "pinyin",
    taiwan: false,
    customDicts: false,
    dicts: new Set(), // selected dictionary anchors
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
    const m = syllable.match(/^([A-Za-zü:]+)([1-5])?$/);
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

// Convert one marked pinyin syllable to zhuyin, e.g. "hǎo" -> "ㄏㄠˇ".
function toZhuyin(syllable) {
    let base = plainSyllable(syllable);
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
        return ZHUYIN_INITIALS[initial] + ZHUYIN_TONES[toneOf(syllable) - 1];
    }
    // After j/q/x, a written "u" is actually ü.
    if (["j", "q", "x"].includes(initial) && final.startsWith("u")) {
        final = "ü" + final.slice(1);
    }
    const body = (ZHUYIN_INITIALS[initial] || "") + (ZHUYIN_FINALS[final] || final);
    return body + ZHUYIN_TONES[toneOf(syllable) - 1];
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
    const syllables = pinyin
        .split(/\s+/)
        .filter((syllable) => syllable && !PUNCT_TOKEN_RE.test(syllable));
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
        let index = 0;
        // Readings first, then characters, so a DOM range over one kind stays
        // contiguous (the double-click handler relies on that).
        chars.forEach((char, i) => {
            if (PUNCT_CHAR_RE.test(char)) return;
            const tone = colored ? toneOf(syllables[index]) : 0;
            node.append(gridCell("reading", readings[index], String(i + 1), tone));
            index += 1;
        });
        index = 0;
        chars.forEach((char, i) => {
            const punctuation = PUNCT_CHAR_RE.test(char);
            const tone = !punctuation && colored ? toneOf(syllables[index]) : 0;
            node.append(gridCell("char", char, String(i + 1), tone));
            if (!punctuation) index += 1;
        });
    } else {
        // Counts differ: put the whole reading above the whole word (still colored).
        const reading = document.createElement("span");
        reading.className = "reading";
        reading.style.gridRow = "1";
        reading.style.gridColumn = "1 / -1";
        reading.append(readingContent(readings, syllables, colored));

        const char = document.createElement("span");
        char.className = "char";
        char.style.gridRow = "2";
        char.style.gridColumn = "1 / -1";
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

        node.append(reading, char);
    }
    return node;
}

// CEDICT cross-references look like "國際企業管理|国际企业管理[guo2 ji4 ...]"
// or "个[ge4]". Group 1 = traditional (or the only form), 2 = simplified,
// 3 = numeric pinyin.
const CROSS_REF = /([^\s|\[\]:/]+)(?:\|([^\s|\[\]:/]+))?\[([^\]]+)\]/g;
// MOE (and other non-CEDICT) definitions write cross-references as "→word".
const ARROW_CROSS_REF = /→\s*([\u3400-\u9fff]+)/g;
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
function definitionBody(source, taiwanRefs) {
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
            const syllables = group.pinyin.split(/\s+/).filter(Boolean);
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
function definitionsBlock(source, taiwanRefs, word) {
    const block = document.createElement("div");
    block.className = "def-source";
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
    } else {
        name.href = `/about#${encodeURIComponent(source.anchor)}`;
    }
    name.textContent = source.label;
    block.append(name, definitionBody(source, taiwanRefs));
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
        enabled = new Set(state.dicts);
        entryDictState.set(key, enabled);
    }
    return enabled;
}

// Source priority when "Custom dictionaries" is off: English first (CC-CEDICT
// before Wiktionary), then Mainland, then Taiwan. Each headword shows only the
// highest-priority source that has a definition for it.
const LANG_PRIORITY = { english: 0, mainland: 1, taiwan: 2 };
const SOURCE_PRIORITY = ["cedict", "wiktionary", "moe"];

function prioritySource(entry) {
    let best = null;
    let bestRank = Infinity;
    for (const source of entry.definitions || []) {
        const rank =
            (LANG_PRIORITY[source.lang] ?? 9) * 100 +
            (SOURCE_PRIORITY.indexOf(source.anchor) + 1 || 99);
        if (rank < bestRank) {
            bestRank = rank;
            best = source;
        }
    }
    return best;
}

// Which dictionaries to render for a result. With custom dictionaries on this is
// the per-entry selection; with it off it is just the single top-priority source.
function visibleDicts(entry) {
    if (state.customDicts) return entryDicts(entry);
    const source = prioritySource(entry);
    return source ? new Set([source.anchor]) : new Set();
}

// Remember where the chips are, so they can be animated after a re-order.
function chipPositions(row) {
    const positions = new Map();
    for (const chip of row.querySelectorAll(".source")) {
        positions.set(chip.dataset.anchor, chip.getBoundingClientRect().left);
    }
    return positions;
}

function animateChips(row, before) {
    for (const chip of row.querySelectorAll(".source")) {
        const old = before.get(chip.dataset.anchor);
        if (old === undefined) continue;
        const dx = old - chip.getBoundingClientRect().left;
        if (!dx) continue;
        chip.style.transition = "none";
        chip.style.transform = `translateX(${dx}px)`;
        requestAnimationFrame(() => {
            chip.style.transition = "";
            chip.style.transform = "";
        });
    }
}

// Toggle one dictionary's definitions for a single result row (not globally).
function toggleEntryDict(row, entry, anchor) {
    const enabled = entryDicts(entry);
    if (enabled.has(anchor)) enabled.delete(anchor);
    else enabled.add(anchor);
    const before = chipPositions(row);
    fillEntryRow(row, entry);
    animateChips(row, before);
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
    const syllables = (reading || "").split(/\s+/).filter(Boolean);
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
    const definitions = entry.definitions || [];

    // Source chips are toggles: the ones on come first (and are lit yellow).
    // They only appear when "Custom dictionaries" is on.
    if (definitions.length && state.customDicts) {
        const box = document.createElement("div");
        box.className = "sources";
        const ordered = [...definitions].sort(
            (a, b) => (enabled.has(b.anchor) ? 1 : 0) - (enabled.has(a.anchor) ? 1 : 0)
        );
        for (const source of ordered) {
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
        for (const note of prons.taiwan) {
            notes.push(detailRow("Taiwan pronunciation: ", furigana(primary, numericToMarked(note))));
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
        defsBox.append(definitionsBlock(source, entry.taiwan_refs, primary));
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
    if (!state.customDicts) {
        // Priority mode: whatever source has a definition is shown.
        return (entry.definitions || []).length > 0 || (entry.forms || []).length > 0;
    }
    if ((entry.definitions || []).some((s) => state.dicts.has(s.anchor))) return true;
    // A "see X" variant has no senses - only "other forms" (CC-CEDICT) - but it
    // should still show when CC-CEDICT is on.
    return (entry.forms || []).length > 0 && state.dicts.has("cedict");
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

    const withDefs = withReading.filter(hasSelectedDefs);
    const headwordsOnly = withReading.filter((entry) => !hasSelectedDefs(entry));
    // No reading at all: still put ones with a definition above bare headwords.
    const withoutReadingWithDefs = withoutReading.filter(hasSelectedDefs);
    const withoutReadingOnly = withoutReading.filter(
        (entry) => !hasSelectedDefs(entry),
    );

    const count = document.createElement("p");
    count.className = "results-count";
    // "(first batch ms, total ms)" when the first batch was shown, else just
    // the total (or nothing on a fresh page).
    const timing =
        lastSearchMs === null
            ? ""
            : firstSearchMs === null
              ? ` (${lastSearchMs} ms)`
              : ` (${firstSearchMs} ms, ${lastSearchMs} ms)`;
    count.textContent = `${merged.length} RESULT${merged.length === 1 ? "" : "S"}${timing}`;
    results.append(count);

    for (const entry of withDefs) results.append(entryRow(entry));
    for (const entry of withoutReadingWithDefs) results.append(entryRow(entry));
    for (const entry of idsEntries) results.append(entryRow(entry));
    for (const entry of dialectEntries) results.append(entryRow(entry));
    for (const entry of headwordsOnly) results.append(entryRow(entry));
    for (const entry of withoutReadingOnly) results.append(entryRow(entry));
}

// Append the tone-insensitive "Fuzzy match" section under a separator.
function renderFuzzy(entries) {
    currentFuzzy = entries || [];
    if (!currentFuzzy.length) return;
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

async function search(query, offset = 0, scrollTop = false) {
    const url = `/api/search?q=${encodeURIComponent(query)}&taiwan=${state.taiwan ? 1 : 0}&offset=${offset}`;
    const startedAt = performance.now();
    const seq = ++searchSeq;
    // The footer (About link) is pinned to the bottom; hide it while results
    // are pending so it cannot flash/jump as the results area refills.
    document.body.classList.add("searching");
    // Phase 1: the fast CEDICT tier. It is rendered immediately as soon as it
    // has any results; otherwise we wait for the full list.
    const firstResponse = await fetch(url + "&part=primary", { cache: "no-store" });
    const first = await firstResponse.json();
    if (seq !== searchSeq) return 0;
    entryDictState.clear(); // a fresh search starts from the global selection
    const firstMs = typeof first.ms === "number" ? first.ms : null;
    const showFirst = first.total == null && first.results.length >= 1;
    if (showFirst) {
        currentEntries = first.results;
        currentQuery = query;
        lastSearchMs = firstMs;
        firstSearchMs = null;
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
    // Paging always starts at the top of the new page.
    if (scrollTop) window.scrollTo({ top: 0 });
    // A "*" page comes complete in one response (its `total` is set).
    if (first.total == null) {
        // Phase 2: the complete final list, slow source tiers included.
        const fullResponse = await fetch(url, { cache: "no-store" });
        const full = await fullResponse.json();
        if (seq !== searchSeq) return 0;
        currentEntries = full.results;
        currentQuery = query;
        // Keep the first batch's timing next to the total when it was shown.
        firstSearchMs = showFirst ? firstMs : null;
        lastSearchMs = Math.round(performance.now() - startedAt);
        render(full.results, query);
        renderFuzzy(full.fuzzy);
        renderPagination(query, full);
        updateHint(query, full.pinyin_match, full.english_match);
        document.body.classList.remove("searching");
        return full.results.length;
    }
    currentEntries = first.results;
    currentQuery = query;
    firstSearchMs = null;
    lastSearchMs = firstMs;
    render(first.results, query);
    renderFuzzy(first.fuzzy);
    renderPagination(query, first);
    updateHint(query, first.pinyin_match, first.english_match);
    document.body.classList.remove("searching");
    return first.results.length;
}

// Show prev/next paging above and below a "*" (all-headwords) search. When there
// is more than one page, the top pager replaces the "N RESULTS" count.
function renderPagination(query, data) {
    const total = data.total;
    const size = data.page_size || 100;
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
        nav.append(...pagerButtons(query, data, page, pages, info));
    }
}

// The Previous / info / Next row shared by the top and bottom pagers.
function pagerButtons(query, data, page, pages, info) {
    const size = data.page_size || 100;

    const prev = document.createElement("button");
    prev.type = "button";
    prev.textContent = "Previous";
    prev.disabled = page <= 1;
    prev.addEventListener("click", () => search(query, (data.offset || 0) - size, true));

    const label = document.createElement("span");
    label.className = "page-info";
    label.textContent = info;

    const next = document.createElement("button");
    next.type = "button";
    next.textContent = "Next";
    next.disabled = page >= pages;
    next.addEventListener("click", () => search(query, (data.offset || 0) + size, true));

    return [prev, label, next];
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

// --- scroll position across reloads ---------------------------------------
// Results arrive from fetch(), so the browser's own scroll restoration happens
// before they are in the DOM and lands at the top. Save the position ourselves
// and restore it once the results are rendered.
if ("scrollRestoration" in history) history.scrollRestoration = "manual";
const SCROLL_KEY = "yabai-chaci-scroll";

function saveScroll() {
    try {
        sessionStorage.setItem(
            SCROLL_KEY,
            JSON.stringify({ url: location.href, y: window.scrollY }),
        );
    } catch (error) {
        // Storage disabled (private mode): just skip saving.
    }
}

function restoreScroll() {
    let saved = null;
    try {
        saved = JSON.parse(sessionStorage.getItem(SCROLL_KEY) || "null");
    } catch (error) {
        return;
    }
    // The results (or the home view) are in the DOM now; release the space that
    // prepareInitialScroll reserved.
    if (initialScrollPending) {
        document.body.style.minHeight = "";
        initialScrollPending = false;
    }
    // Only restore for the same URL, so a new search starts at the top.
    if (saved && saved.url === location.href) window.scrollTo(0, saved.y);
}

// True while a refresh has reserved page space to restore the scroll position
// before the asynchronous results arrive.
let initialScrollPending = false;

// On a refresh the results are fetched a moment later, so the page is initially
// too short to scroll down. Reserve the missing height and jump to the saved
// position now (before the first paint), so the page does not visibly scroll
// once the results appear.
function prepareInitialScroll() {
    let saved = null;
    try {
        saved = JSON.parse(sessionStorage.getItem(SCROLL_KEY) || "null");
    } catch (error) {
        return;
    }
    if (!saved || saved.url !== location.href || !saved.y) return;
    document.body.style.minHeight = `${saved.y + window.innerHeight}px`;
    initialScrollPending = true;
    window.scrollTo(0, saved.y);
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
    document.title = query ? `${query} - Yabai-Chaci` : "Yabai-Chaci";
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

// Results per page (matches db.ALL_PAGE_SIZE) for turning ?page=N into an
// offset before the first request.
const RESULTS_PAGE_SIZE = 100;

// The results page from ?page=..., or 1 when it is absent/invalid.
function pageFromUrl() {
    const value = Number(new URLSearchParams(location.search).get("page") || 1);
    return Number.isFinite(value) && value > 1 ? Math.floor(value) : 1;
}

// Run a search and put the query in the URL, so a reload keeps the results.
function performSearch(query) {
    // A "#anchor" query jumps to that section of the About page (e.g. #cedict).
    if (query.startsWith("#") && query.length > 1) {
        location.assign(`/about${query}`);
        return;
    }
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
        state.lang = localStorage.getItem("yabai-chaci-lang") || "EN";
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
        localStorage.setItem("yabai-chaci-dicts", JSON.stringify([...state.dicts]));
        localStorage.setItem("yabai-chaci-lang", state.lang);
    } catch (e) {
        // localStorage unavailable - nothing to save.
    }
}

// Re-render the current results when a display toggle changes.
scriptToggle.addEventListener("change", () => {
    state.script = scriptToggle.checked ? "traditional" : "simplified";
    savePrefs();
    redraw();
});
toneToggle.addEventListener("change", () => {
    state.toneColoring = toneToggle.checked;
    savePrefs();
    redraw();
});
phoneticToggle.addEventListener("change", () => {
    state.phonetic = phoneticToggle.checked ? "zhuyin" : "pinyin";
    savePrefs();
    redraw();
});
taiwanToggle.addEventListener("change", () => {
    state.taiwan = taiwanToggle.checked;
    savePrefs();
    // Re-render immediately so the reading flips without clearing the page
    // (which is what made it flash). Then re-run the same search so Taiwan-only
    // reading matches update too; "*" listing is independent of Taiwan.
    redraw();
    if (currentQuery && currentQuery.trim() !== "*") search(currentQuery);
});
customDictsToggle.addEventListener("change", () => {
    state.customDicts = customDictsToggle.checked;
    savePrefs();
    syncDictPanel();
    // Re-render so the per-entry dictionary chips appear/disappear too.
    redraw();
});

// Show the Dictionaries disclosure only when "Custom dictionaries" is on.
function syncDictPanel() {
    dictPanel.hidden = !state.customDicts;
}

// Dictionaries panel: a group switch toggles its whole column, and each
// per-dictionary switch selects one dictionary.
function groupFor(toggle) {
    return toggle.closest(".dict-column").dataset.lang;
}

// Reflect the selected dictionaries on the group switches: a column switch is
// on as soon as *any* dictionary in that column is selected.
function syncGroups() {
    for (const group of dictGroupToggles) {
        const members = dictToggles.filter((t) => groupFor(t) === group.dataset.lang);
        const on = members.filter((t) => state.dicts.has(t.dataset.anchor)).length;
        group.disabled = members.length === 0; // e.g. Mainland (empty for now)
        group.checked = on > 0;
        group.indeterminate = false;
    }
}

for (const group of dictGroupToggles) {
    group.addEventListener("change", () => {
        for (const toggle of dictToggles) {
            if (groupFor(toggle) !== group.dataset.lang) continue;
            toggle.checked = group.checked;
            if (group.checked) state.dicts.add(toggle.dataset.anchor);
            else state.dicts.delete(toggle.dataset.anchor);
        }
        group.indeterminate = false;
        entryDictState.clear();
        savePrefs();
        redraw();
    });
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
    applyDictDefaults(item.dataset.value);
    savePrefs();
    setLangOpen(false);
    redraw(); // the default dictionaries changed, so re-render the results
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
for (const panel of document.querySelectorAll(".options")) {
    const toggle = panel.querySelector(".options-toggle");
    const key = "yabai-chaci-panel-" + (toggle.getAttribute("aria-controls") || "");
    toggle.addEventListener("click", () => {
        const open = !panel.classList.contains("open");
        panel.classList.toggle("open", open);
        toggle.setAttribute("aria-expanded", String(open));
        try {
            localStorage.setItem(key, String(open));
        } catch (e) {
            // localStorage unavailable - nothing to save.
        }
    });
    try {
        if (localStorage.getItem(key) === "true") {
            panel.classList.add("open");
            toggle.setAttribute("aria-expanded", "true");
        }
    } catch (e) {
        // localStorage unavailable - start closed.
    }
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
const pullArrow = document.getElementById("pull-arrow");

// The panel's expanded state survives a refresh (sessionStorage is scoped to the
// tab, so a fresh tab still starts with the panel in the page).
const PULL_KEY = "yabai-chaci-pulled";

function savePulled(open) {
    try {
        sessionStorage.setItem(PULL_KEY, String(open));
    } catch (error) {
        // Storage disabled (private mode): just skip saving.
    }
}

function positionPullArrow() {
    if (!searchPanel || !pullArrow) return;
    const rect = searchPanel.getBoundingClientRect();
    // Keep the handle's left edge on the search box's left edge, so it always
    // lines up with the bottom-left corner it attaches to when pulled.
    pullArrow.style.left = `${rect.left}px`;
    pullArrow.style.top = searchPanel.classList.contains("pulled")
        ? `${rect.bottom}px`
        : "0";
}

function updatePullArrow() {
    if (!searchPanel || !pullArrow) return;
    pullArrow.hidden =
        !searchPanel.classList.contains("pulled") &&
        searchPanel.getBoundingClientRect().bottom >= 0;
    positionPullArrow();
}

function setPulled(open) {
    if (!searchPanel) return;
    if (open && !searchPanel.classList.contains("pulled")) {
        // Sticky (see CSS): the panel keeps its place at the top of the page and
        // follows the viewport once it would scroll out of view.
        searchPanel.classList.add("pulled");
        pullArrow.setAttribute("aria-expanded", "true");
        pullArrow.hidden = false;
        savePulled(true);
        input.focus({ preventScroll: true });
    } else if (!open && searchPanel.classList.contains("pulled")) {
        searchPanel.classList.remove("pulled");
        pullArrow.setAttribute("aria-expanded", "false");
        savePulled(false);
    }
    updatePullArrow();
}

pullArrow.addEventListener("click", () => {
    setPulled(!searchPanel.classList.contains("pulled"));
});
if ("ResizeObserver" in window) {
    new ResizeObserver(() => {
        if (searchPanel.classList.contains("pulled")) positionPullArrow();
    }).observe(searchPanel);
}
window.addEventListener("scroll", updatePullArrow, { passive: true });
window.addEventListener("resize", updatePullArrow);
updatePullArrow();

// Re-render when the user navigates with the browser's back/forward buttons.
window.addEventListener("popstate", restoreFromUrl);

// On first load: apply saved preferences, then restore a search from the URL.
loadPrefs();
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
