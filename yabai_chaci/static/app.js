// Front-end script for Yabai-Chaci. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const clearButton = document.getElementById("clear-button");
const results = document.getElementById("results");
const parsed = document.getElementById("parsed");
const hint = document.getElementById("hint");
const scriptToggle = document.getElementById("script-toggle");
const toneToggle = document.getElementById("tone-toggle");
const phoneticToggle = document.getElementById("phonetic-toggle");
const taiwanToggle = document.getElementById("taiwan-toggle");
// Dictionaries panel: group switches (English/Mainland/Taiwan) and per-dict.
const dictGroupToggles = [...document.querySelectorAll(".dict-group")];
const dictToggles = [...document.querySelectorAll(".dict-toggle")];

// Search-language dropdown (left of the search box): EN / ZH / TW.
const langSelect = document.querySelector(".lang-select");
const langButton = langSelect.querySelector(".lang-button");
const langMenu = langSelect.querySelector(".lang-menu");
// Default dictionaries per language (from default_dictionaries.py).
const langDefaults = JSON.parse(langSelect.dataset.defaults || "{}");

// Display options. script: "simplified" | "traditional"; toneColoring: on/off;
// phonetic: "pinyin" | "zhuyin"; taiwan: use the Taiwan reading when listed;
// dicts: the selected dictionaries (bulk of the Dictionaries panel);
// lang: the selected search language (EN / ZH / TW).
const state = {
    script: "simplified",
    toneColoring: false,
    phonetic: "pinyin",
    taiwan: false,
    dicts: new Set(), // selected dictionary anchors
    lang: "EN",
};

// The most recent results, kept so the toggles can re-render without re-fetching.
let currentEntries = [];
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
function readingContent(readings, syllables) {
    if (!state.toneColoring) {
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

function furigana(text, pinyin) {
    const chars = [...text];
    const syllables = pinyin.split(/\s+/).filter(Boolean);
    // The readings in the chosen phonetic system.
    const readings = syllables.map((s) => (state.phonetic === "zhuyin" ? toZhuyin(s) : s));
    const node = document.createElement("span");
    node.className = "furigana";

    if (chars.length === syllables.length) {
        const colored = state.toneColoring;
        chars.forEach((_, i) => {
            node.append(gridCell("reading", readings[i], String(i + 1), colored ? toneOf(syllables[i]) : 0));
        });
        chars.forEach((char, i) => {
            node.append(gridCell("char", char, String(i + 1), colored ? toneOf(syllables[i]) : 0));
        });
    } else {
        // Counts differ: put the whole reading above the whole word (still colored).
        const reading = document.createElement("span");
        reading.className = "reading";
        reading.style.gridRow = "1";
        reading.style.gridColumn = "1 / -1";
        reading.append(readingContent(readings, syllables));

        const char = document.createElement("span");
        char.className = "char";
        char.style.gridRow = "2";
        char.style.gridColumn = "1 / -1";
        char.textContent = text;

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

// Render a plain (non-CEDICT) definition, turning "→word" cross-references into
// links that search the referenced word.
function renderCrossRefs(text) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    let match;
    ARROW_CROSS_REF.lastIndex = 0;
    while ((match = ARROW_CROSS_REF.exec(text)) !== null) {
        if (match.index > last) fragment.append(text.slice(last, match.index));
        fragment.append("→");
        fragment.append(searchLink(match[1]));
        last = match.index + match[0].length;
    }
    if (last < text.length) fragment.append(text.slice(last));
    return fragment;
}

// Build a definition: plain text, with cross-references rendered as furigana.
// `taiwanRefs` maps referenced words to their Taiwan reading (used when the
// Taiwan toggle is on).
function renderDefinition(text, taiwanRefs) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    let match;
    CROSS_REF.lastIndex = 0;
    while ((match = CROSS_REF.exec(text)) !== null) {
        if (match.index > last) fragment.append(text.slice(last, match.index));
        const word = match[2] || match[1]; // prefer the simplified form
        const reading =
            state.taiwan && taiwanRefs && taiwanRefs[word]
                ? taiwanRefs[word]
                : numericToMarked(match[3]);
        // Cross-references ("see also 洪秀全[…]") render as clickable, underlined
        // links that search the referenced word.
        fragment.append(searchLink(word, furigana(word, reading)));
        last = match.index + match[0].length;
    }
    if (last < text.length) fragment.append(text.slice(last));
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
            head.textContent = group.pinyin;
            body.append(head);
        }
        group.senses.forEach((text, i) => {
            const line = document.createElement("div");
            line.className = "def-source-line";
            const num = document.createElement("span");
            num.className = "def-source-num";
            num.textContent = `${i + 1}.`;
            line.append(
                num,
                source.anchor === "cedict"
                    ? renderDefinition(text, taiwanRefs)
                    : renderCrossRefs(text)
            );
            body.append(line);
        });
    }

    // Standardized classifiers, shown in the chosen script (Traditional toggle).
    if (source.classifiers && source.classifiers.length) {
        const useTraditional = state.script === "traditional";
        const forms = source.classifiers.map((c) =>
            c.note ? c.note : (useTraditional ? c.traditional : c.simplified) || ""
        );
        const line = document.createElement("div");
        line.className = "def-source-cls";
        const label = document.createElement("span");
        label.className = "def-source-cls-label";
        label.textContent = "Classifier:";
        line.append(label, document.createTextNode(" " + forms.join("; ")));
        body.append(line);
    }
    return body;
}

// A labeled block showing one dictionary's definitions for a result. The
// dictionary name links to its section on the About page.
function definitionsBlock(source, taiwanRefs) {
    const block = document.createElement("div");
    block.className = "def-source";
    const name = document.createElement("a");
    name.className = "def-source-name";
    name.href = `/about#${encodeURIComponent(source.anchor)}`;
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
    const reading = state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;

    const hanzi = document.createElement("span");
    hanzi.className = "hanzi";
    hanzi.append(furigana(primary, reading));
    row.append(hanzi);

    // SUBTLEX-CH frequency: a chip-like box under the headword, above the chips.
    if (entry.freq) {
        const freq = document.createElement("div");
        freq.className = "freq";
        const chip = document.createElement("span");
        chip.className = "source";
        chip.textContent = `Frequency: #${entry.freq.rank.toLocaleString()}`;
        freq.append(chip);
        row.append(freq);
    }

    const enabled = entryDicts(entry);
    const definitions = entry.definitions || [];

    // Source chips are toggles: the ones on come first (and are lit yellow).
    if (definitions.length) {
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
        row.append(box);
    }

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
                fragment.append(form.pinyin ? furigana(word, form.pinyin) : document.createTextNode(word));
            });
            notes.push(detailRow("Other forms: ", fragment));
        }
    }

    let notesPlaced = false;
    for (const source of definitions) {
        if (!enabled.has(source.anchor)) continue;
        row.append(definitionsBlock(source, entry.taiwan_refs));
        if (source.anchor === "cedict" && notes.length) {
            row.append(...notes);
            notesPlaced = true;
        }
    }
    if (notes.length && !notesPlaced) row.append(...notes);
}

// A result is shown only when one of the selected dictionaries has a definition
// for it; a word found only in unselected dictionaries lives in the "Matches in
// other dictionaries" section instead.
function hasSelectedDefs(entry) {
    if ((entry.definitions || []).some((s) => state.dicts.has(s.anchor))) return true;
    // A "see X" variant has no senses - only "other forms" (CC-CEDICT) - but it
    // should still show when CC-CEDICT is on.
    return (entry.forms || []).length > 0 && state.dicts.has("cedict");
}

// Build the results list from the array of entries returned by the API.
function render(entries, query) {
    currentEntries = entries;
    currentQuery = query;
    results.replaceChildren(); // clear previous results

    // Results with a definition (from a dictionary selected for them) come first;
    // the rest are shown as a bare headword, at the bottom.
    const withDefs = entries.filter(hasSelectedDefs);
    const headwordsOnly = entries.filter((entry) => !hasSelectedDefs(entry));

    const count = document.createElement("p");
    count.className = "results-count";
    count.textContent = `${entries.length} RESULT${entries.length === 1 ? "" : "S"}`;
    results.append(count);

    for (const entry of withDefs) results.append(entryRow(entry));
    for (const entry of headwordsOnly) results.append(entryRow(entry));
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
async function search(query) {
    const url = `/api/search?q=${encodeURIComponent(query)}&taiwan=${state.taiwan ? 1 : 0}`;
    const response = await fetch(url);
    const data = await response.json();
    entryDictState.clear(); // a fresh search starts from the global selection
    render(data.results, query);
    renderFuzzy(data.fuzzy);
    updateHint(query, data.pinyin_match, data.english_match);
}

// Render the segmented input as clickable words (clicking one searches it).
// Found words are annotated with furigana; whitespace is left plain.
// A punctuation-only token (e.g. ，。、！) matches this.
const PUNCT_RE = /^[\p{P}\p{S}]+$/u;

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
        if (!token.text.trim()) {
            node.classList.add("space"); // whitespace: no underline
            node.textContent = token.text;
        } else if (token.entries.length) {
            const entry = token.entries[0];
            const text = scriptText(token);
            const reading = state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;
            node.append(furigana(text, reading));
            node.classList.add("clickable");
            node.addEventListener("click", () => {
                setTitle(text);
                search(text);
            });
        } else {
            // Unmatched word or punctuation: render it in the furigana grid with an
            // empty reading, so it lines up with the matched words. It gets no
            // separating padding (unlike matched words).
            node.append(furigana(scriptText(token), ""));
            node.classList.add("unmatched");
            // Punctuation is not underlined.
            if (PUNCT_RE.test(token.text)) node.classList.add("punct");
        }
        parsed.append(node);
    }
}

// Segment the input. Multi-word text shows clickable words; a single word is
// looked up directly.
async function annotate(text) {
    const response = await fetch("/api/annotate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
    });
    const data = await response.json();
    const words = data.tokens.filter((token) => token.entries.length);
    clearResults();
    if (words.length > 1) renderParsed(data.tokens);
    // Search the first matched word, or fall back to the raw text (pinyin/English).
    search(words.length ? words[0].text : text);
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
    if (query) {
        input.value = query;
        setTitle(query);
        showTagline(false);
        annotate(query);
    } else {
        input.value = "";
        setTitle("");
        clearResults();
        showTagline(true);
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
        state.toneColoring = localStorage.getItem("yabai-chaci-tone") === "true";
        state.phonetic = localStorage.getItem("yabai-chaci-phonetic") === "zhuyin" ? "zhuyin" : "pinyin";
        state.taiwan = localStorage.getItem("yabai-chaci-taiwan") === "true";
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
    for (const input of dictToggles) input.checked = state.dicts.has(input.dataset.anchor);
    syncGroups();
    applyLang(state.lang);
}

// Persist the display preferences.
function savePrefs() {
    try {
        localStorage.setItem("yabai-chaci-script", state.script);
        localStorage.setItem("yabai-chaci-tone", String(state.toneColoring));
        localStorage.setItem("yabai-chaci-phonetic", state.phonetic);
        localStorage.setItem("yabai-chaci-taiwan", String(state.taiwan));
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
    // Re-run the search: Taiwan mode also matches Taiwan readings.
    const query = input.value.trim();
    if (query) search(query);
    else redraw();
});

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

// Double-click a character to select the whole word, or a reading to select the
// whole reading line.
results.addEventListener("dblclick", (event) => {
    const kind = event.target.classList.contains("char")
        ? "char"
        : event.target.classList.contains("reading")
            ? "reading"
            : null;
    if (!kind) return;

    const furigana = event.target.closest(".furigana");
    const parts = furigana ? furigana.querySelectorAll(`.${kind}`) : [];
    if (!parts.length) return;

    const range = document.createRange();
    range.setStartBefore(parts[0]);
    range.setEndAfter(parts[parts.length - 1]);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    event.preventDefault();
});

// Re-render when the user navigates with the browser's back/forward buttons.
window.addEventListener("popstate", restoreFromUrl);

// On first load: apply saved preferences, then restore a search from the URL.
loadPrefs();
restoreFromUrl();
