// Front-end script for Yabai-Chaci. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const results = document.getElementById("results");
const parsed = document.getElementById("parsed");
const hint = document.getElementById("hint");
const scriptToggle = document.getElementById("script-toggle");
const toneToggle = document.getElementById("tone-toggle");
const phoneticToggle = document.getElementById("phonetic-toggle");
const taiwanToggle = document.getElementById("taiwan-toggle");
// TEMPORARY: one checkbox per dictionary, for the per-source definitions view.
const sourceToggles = [...document.querySelectorAll(".source-toggle")];

// Search-language dropdown (left of the search box): EN / ZH / TW.
const langSelect = document.querySelector(".lang-select");
const langButton = langSelect.querySelector(".lang-button");
const langMenu = langSelect.querySelector(".lang-menu");

// Display options. script: "simplified" | "traditional"; toneColoring: on/off;
// phonetic: "pinyin" | "zhuyin"; taiwan: use the Taiwan reading when listed;
// defSources: which dictionaries' definitions to show (empty = none);
// lang: the selected search language (EN / ZH / TW).
const state = {
    script: "simplified",
    toneColoring: false,
    phonetic: "pinyin",
    taiwan: false,
    defSources: new Set(),
    lang: "EN",
};

// The most recent results, kept so the toggles can re-render without re-fetching.
let currentEntries = [];
let currentFuzzy = [];
let currentQuery = "";

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
// contiguous — used by the double-click handler below.
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
        fragment.append(furigana(word, reading));
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
    const others = []; // { reading, label } — label is "" for a plain note
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

// A muted detail row under a result (other forms / alternate pronunciations).
function detailRow(label, node) {
    const row = document.createElement("div");
    row.className = "other-forms";
    row.append(label, node);
    return row;
}

// Clear the results area (used by the home view; shows no "No results." message).
function clearResults() {
    currentEntries = [];
    results.replaceChildren();
    parsed.replaceChildren();
    hint.hidden = true;
}

// Show the pinyin hint when the query matched Chinese readings exactly — which
// is also when English-definition matches are suppressed. Rather than telling the
// user to add quotes, we offer a link that runs that quoted (English) search.
function updateHint(query, pinyinMatch) {
    // Hidden for tone-numbered or wildcard queries: the reading is exact/patterned,
    // so an English-search link would be misleading.
    if (pinyinMatch && !/[0-9*]/.test(query)) {
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

// Small boxes listing the dictionaries that contain a headword. Each links to
// that source's section on the About page.
function sourceChips(sources) {
    const box = document.createElement("div");
    box.className = "sources";
    for (const source of sources) {
        const chip = document.createElement("a");
        chip.className = "source";
        chip.href = `/about#${encodeURIComponent(source.anchor)}`;
        chip.textContent = source.label;
        box.append(chip);
    }
    return box;
}

// A labeled block showing one dictionary's definitions for a result.
function definitionsBlock(source, taiwanRefs) {
    const block = document.createElement("div");
    block.className = "def-source";
    const header = document.createElement("a");
    header.className = "def-source-name";
    header.href = `/about#${encodeURIComponent(source.anchor)}`;
    header.textContent = source.label;
    block.append(header);

    // Numbered senses (1., 2., ...), consistent across every dictionary.
    source.defs.forEach((text, i) => {
        const line = document.createElement("div");
        line.className = "def-source-line";
        const num = document.createElement("span");
        num.className = "def-source-num";
        num.textContent = `${i + 1}.`;
        // Only CEDICT text carries [pinyin] cross-references; others are plain.
        line.append(
            num,
            source.anchor === "cedict"
                ? renderDefinition(text, taiwanRefs)
                : document.createTextNode(text)
        );
        block.append(line);
    });

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
        block.append(line);
    }
    return block;
}

// Build one result row for an entry.
function entryRow(entry) {
    // Show the chosen script as the main word.
    const primary = state.script === "traditional" ? entry.traditional : entry.simplified;
    // Use the Taiwan reading when the toggle is on and the entry lists one.
    const reading = state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;

    const row = document.createElement("div");
    row.className = "entry";

    const hanzi = document.createElement("span");
    hanzi.className = "hanzi";
    hanzi.append(furigana(primary, reading));
    row.append(hanzi);

    // Which dictionaries contain this headword, as small boxes between the word
    // and its definitions.
    if (entry.sources && entry.sources.length) {
        row.append(sourceChips(entry.sources));
    }

    // Pull "Taiwan pr." / "also pr." notes out of the definition so they get
    // their own rows instead of sitting in the main listing.
    const prons = extractPronunciations(entry.english_normalized);
    // Definitions: only the switched-on dictionaries show — nothing checked
    // means no definitions at all.
    if (entry.definitions) {
        for (const source of entry.definitions) {
            if (!state.defSources.has(source.anchor)) continue;
            // CC-CEDICT uses the cleaned text; its pronunciation/classifier notes
            // are shown as separate rows below.
            const defs = source.anchor === "cedict" ? [prons.text] : source.defs;
            row.append(
                definitionsBlock(
                    { anchor: source.anchor, label: source.label, defs },
                    entry.taiwan_refs
                )
            );
        }
    }

    for (const note of prons.taiwan) {
        row.append(detailRow("Taiwan pronunciation: ", furigana(primary, numericToMarked(note))));
    }
    for (const note of prons.others) {
        // Show the register label (e.g. "colloquial") alongside the reading.
        const label = note.label ? `Also pronounced (${note.label}): ` : "Also pronounced: ";
        row.append(detailRow(label, furigana(primary, numericToMarked(note.reading))));
    }
    return row;
}

// Build the results list from the array of entries returned by the API.
function render(entries, query) {
    currentEntries = entries;
    currentQuery = query;
    results.replaceChildren(); // clear previous results

    if (!entries.length) {
        // Shown even when a fuzzy section follows (so it sits above it).
        const p = document.createElement("p");
        p.className = "muted";
        p.textContent = `No results for ${query}.`;
        results.append(p);
        return;
    }

    for (const entry of entries) results.append(entryRow(entry));
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
    if (!currentEntries.length && !currentFuzzy.length) return;
    render(currentEntries, currentQuery);
    renderFuzzy(currentFuzzy);
}

// Query the API and hand the results to render().
async function search(query) {
    const url = `/api/search?q=${encodeURIComponent(query)}&taiwan=${state.taiwan ? 1 : 0}`;
    const response = await fetch(url);
    const data = await response.json();
    render(data.results, query);
    renderFuzzy(data.fuzzy);
    updateHint(query, data.pinyin_match);
}

// Render the segmented input as clickable words (clicking one searches it).
// Found words are annotated with furigana; whitespace is left plain.
function renderParsed(tokens) {
    parsed.replaceChildren();
    for (const token of tokens) {
        const node = document.createElement("span");
        node.className = "parsed-word";
        if (!token.text.trim()) {
            node.classList.add("space"); // whitespace: no underline
            node.textContent = token.text;
        } else if (token.entries.length) {
            const entry = token.entries[0];
            const reading = state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;
            node.append(furigana(token.text, reading));
            node.classList.add("clickable");
            node.addEventListener("click", () => search(token.text));
        } else {
            node.textContent = token.text;
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
    showTagline(false);
    history.pushState({ q: query }, "", `?q=${encodeURIComponent(query)}`);
    annotate(query);
}

// Restore the view (results or home) from whatever is in the URL now.
function restoreFromUrl() {
    const query = queryFromUrl();
    if (query) {
        input.value = query;
        showTagline(false);
        annotate(query);
    } else {
        input.value = "";
        clearResults();
        showTagline(true);
    }
}

// Return to the home view: no query, no results, tagline shown, URL reset.
function goHome() {
    input.value = "";
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
        const defSources = JSON.parse(localStorage.getItem("yabai-chaci-def-sources") || "[]");
        state.defSources = new Set(defSources);
        state.lang = localStorage.getItem("yabai-chaci-lang") || "EN";
    } catch (e) {
        // localStorage unavailable — keep the defaults.
    }
    scriptToggle.checked = state.script === "traditional";
    toneToggle.checked = state.toneColoring;
    phoneticToggle.checked = state.phonetic === "zhuyin";
    taiwanToggle.checked = state.taiwan;
    for (const input of sourceToggles) input.checked = state.defSources.has(input.dataset.anchor);
    applyLang(state.lang);
}

// Persist the display preferences.
function savePrefs() {
    try {
        localStorage.setItem("yabai-chaci-script", state.script);
        localStorage.setItem("yabai-chaci-tone", String(state.toneColoring));
        localStorage.setItem("yabai-chaci-phonetic", state.phonetic);
        localStorage.setItem("yabai-chaci-taiwan", String(state.taiwan));
        localStorage.setItem("yabai-chaci-def-sources", JSON.stringify([...state.defSources]));
        localStorage.setItem("yabai-chaci-lang", state.lang);
    } catch (e) {
        // localStorage unavailable — nothing to save.
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

// TEMPORARY: toggling a dictionary switches to the per-source definitions view.
for (const toggle of sourceToggles) {
    toggle.addEventListener("change", () => {
        if (toggle.checked) state.defSources.add(toggle.dataset.anchor);
        else state.defSources.delete(toggle.dataset.anchor);
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

langButton.addEventListener("click", () => setLangOpen(langMenu.hidden));
langMenu.addEventListener("click", (event) => {
    const item = event.target.closest("[role='option']");
    if (!item) return;
    applyLang(item.dataset.value);
    savePrefs();
    setLangOpen(false);
});
// Close the menu when clicking elsewhere, or pressing Escape.
document.addEventListener("click", (event) => {
    if (!langSelect.contains(event.target)) setLangOpen(false);
});
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setLangOpen(false);
});

// Toggle the "Search options" panel. The open/close animation is pure CSS (the
// .open class drives the grid row); here we only flip the class and ARIA state,
// and remember the choice so the panel reopens expanded next time.
const optionsPanel = document.querySelector(".options");
const optionsToggle = optionsPanel.querySelector(".options-toggle");

function setOptionsOpen(open) {
    optionsPanel.classList.toggle("open", open);
    optionsToggle.setAttribute("aria-expanded", String(open));
    try {
        localStorage.setItem("yabai-chaci-options", String(open));
    } catch (e) {
        // localStorage unavailable — nothing to save.
    }
}

// Reopen the panel on load if it was left open.
function restoreOptionsOpen() {
    try {
        setOptionsOpen(localStorage.getItem("yabai-chaci-options") === "true");
    } catch (e) {
        // localStorage unavailable — start closed.
    }
}

optionsToggle.addEventListener("click", () => {
    setOptionsOpen(!optionsPanel.classList.contains("open"));
});

// Run a search when the form is submitted (Enter or the button).
form.addEventListener("submit", (event) => {
    event.preventDefault(); // don't reload the page
    const query = input.value.trim();
    if (query) performSearch(query);
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
restoreOptionsOpen();
restoreFromUrl();
