// Front-end script for yabai-cidian. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const results = document.getElementById("results");
const parsed = document.getElementById("parsed");
const scriptToggle = document.getElementById("script-toggle");
const toneToggle = document.getElementById("tone-toggle");
const phoneticToggle = document.getElementById("phonetic-toggle");
const taiwanToggle = document.getElementById("taiwan-toggle");

// Display options. script: "simplified" | "traditional"; toneColoring: on/off;
// phonetic: "pinyin" | "zhuyin"; taiwan: use the Taiwan reading when listed.
const state = { script: "simplified", toneColoring: false, phonetic: "pinyin", taiwan: false };

// The most recent results, kept so the toggles can re-render without re-fetching.
let currentEntries = [];

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
function renderDefinition(text) {
  const fragment = document.createDocumentFragment();
  let last = 0;
  let match;
  CROSS_REF.lastIndex = 0;
  while ((match = CROSS_REF.exec(text)) !== null) {
    if (match.index > last) fragment.append(text.slice(last, match.index));
    const word = match[2] || match[1]; // prefer the simplified form
    fragment.append(furigana(word, numericToMarked(match[3])));
    last = match.index + match[0].length;
  }
  if (last < text.length) fragment.append(text.slice(last));
  return fragment;
}

// Pull pronunciation notes out of a definition: "Taiwan pr. [...]" and
// "also/old pr. [...]". Returns the cleaned definition text plus the raw
// numeric readings found in each category.
function extractPronunciations(text) {
  const taiwan = new Set();
  const others = new Set();
  const classifiers = new Set();
  const cleaned = text
    .replace(/[;\s]*Taiwan pr\.\s*\[([^\]]+)\]/g, (_, p) => (taiwan.add(p), ""))
    .replace(/[;\s]*(?:also|old)?\s*pr\.\s*\[([^\]]+)\]/g, (_, p) => (others.add(p), ""))
    .replace(/[;\s]*pronounced\s+\[([^\]]+)\]/g, (_, p) => (others.add(p), ""))
    .replace(/[;\s]*CL:\s*([^\s;,/]+)/g, (_, p) => (classifiers.add(p), ""))
    // Remaining bare numeric-pinyin brackets, e.g. "... sb; Taiwan pr. [pang2],
    // [bang1], [bang4]". Only when preceded by a separator, so cross-references
    // like 字[pin] are left untouched.
    .replace(/(?:^|[\s,;]+)\[([A-Za-zü:]+[0-9](?:\s+[A-Za-zü:]+[0-9])*)\]/g, (_, p) => (others.add(p), ""));
  return {
    text: cleaned
      .replace(/\s*;\s*;/g, ";")
      .replace(/\(\s*\)/g, "") // drop parentheses the removal left empty
      .replace(/\s+;/g, ";") // tidy a space left before a separator
      .replace(/[;\s]+$/g, "")
      .replace(/\s{2,}/g, " ")
      .trim(),
    taiwan: [...taiwan],
    others: [...others],
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

// Render a classifier token (e.g. "把[ba3]" or "個|个[ge4]") as furigana.
function classifierNode(token) {
  const m = token.match(/^([^\s|\[\]]+)(?:\|([^\s|\[\]]+))?\[([^\]]+)\]$/);
  if (!m) return document.createTextNode(token);
  return furigana(m[2] || m[1], numericToMarked(m[3]));
}

// Clear the results area (used by the home view; shows no "No results." message).
function clearResults() {
  currentEntries = [];
  results.replaceChildren();
  parsed.replaceChildren();
}

// Build the results list from the array of entries returned by the API.
function render(entries) {
  currentEntries = entries;
  results.replaceChildren(); // clear previous results

  if (!entries.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No results.";
    results.append(p);
    return;
  }

  const useTraditional = state.script === "traditional";

  for (const entry of entries) {
    // Show the chosen script as the main word; keep the other for "other forms".
    const primary = useTraditional ? entry.traditional : entry.simplified;
    const other = useTraditional ? entry.simplified : entry.traditional;

    // Use the Taiwan reading when the toggle is on and the entry lists one.
    const reading = state.taiwan && entry.pinyin_taiwan ? entry.pinyin_taiwan : entry.pinyin;

    const row = document.createElement("div");
    row.className = "entry";

    const hanzi = document.createElement("span");
    hanzi.className = "hanzi";
    hanzi.append(furigana(primary, reading));
    row.append(hanzi);

    // Pull "Taiwan pr." / "also pr." notes out of the definition so they get
    // their own rows instead of sitting in the main listing.
    const prons = extractPronunciations(entry.english_normalized);
    const defs = document.createElement("div");
    defs.className = "defs";
    defs.append(renderDefinition(prons.text));
    row.append(defs);

    if (other !== primary) {
      row.append(detailRow("Other forms: ", document.createTextNode(other)));
    }
    for (const note of prons.taiwan) {
      row.append(detailRow("Taiwan pronunciation: ", furigana(primary, numericToMarked(note))));
    }
    for (const note of prons.others) {
      row.append(detailRow("Also pronounced: ", furigana(primary, numericToMarked(note))));
    }
    for (const token of prons.classifiers) {
      row.append(detailRow("Classifier: ", classifierNode(token)));
    }

    results.append(row);
  }
}

// Query the API and hand the results to render().
async function search(query) {
  const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
  const data = await response.json();
  render(data.results);
}

// Render the segmented input as clickable words (clicking one searches it).
function renderParsed(tokens) {
  parsed.replaceChildren();
  for (const token of tokens) {
    const node = document.createElement("span");
    node.className = "parsed-word";
    node.textContent = token.text;
    if (token.entries.length) {
      node.classList.add("clickable");
      node.addEventListener("click", () => search(token.text));
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
  if (words.length) search(words[0].text); // auto-show the first matched word
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
    const script = localStorage.getItem("yabai-cidian-script");
    if (script === "traditional" || script === "simplified") state.script = script;
    state.toneColoring = localStorage.getItem("yabai-cidian-tone") === "true";
    state.phonetic = localStorage.getItem("yabai-cidian-phonetic") === "zhuyin" ? "zhuyin" : "pinyin";
    state.taiwan = localStorage.getItem("yabai-cidian-taiwan") === "true";
  } catch (e) {
    // localStorage unavailable — keep the defaults.
  }
  scriptToggle.checked = state.script === "traditional";
  toneToggle.checked = state.toneColoring;
  phoneticToggle.checked = state.phonetic === "zhuyin";
  taiwanToggle.checked = state.taiwan;
}

// Persist the display preferences.
function savePrefs() {
  try {
    localStorage.setItem("yabai-cidian-script", state.script);
    localStorage.setItem("yabai-cidian-tone", String(state.toneColoring));
    localStorage.setItem("yabai-cidian-phonetic", state.phonetic);
    localStorage.setItem("yabai-cidian-taiwan", String(state.taiwan));
  } catch (e) {
    // localStorage unavailable — nothing to save.
  }
}

// Re-render the current results when a display toggle changes.
scriptToggle.addEventListener("change", () => {
  state.script = scriptToggle.checked ? "traditional" : "simplified";
  savePrefs();
  if (currentEntries.length) render(currentEntries);
});
toneToggle.addEventListener("change", () => {
  state.toneColoring = toneToggle.checked;
  savePrefs();
  if (currentEntries.length) render(currentEntries);
});
phoneticToggle.addEventListener("change", () => {
  state.phonetic = phoneticToggle.checked ? "zhuyin" : "pinyin";
  savePrefs();
  if (currentEntries.length) render(currentEntries);
});
taiwanToggle.addEventListener("change", () => {
  state.taiwan = taiwanToggle.checked;
  savePrefs();
  if (currentEntries.length) render(currentEntries);
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
restoreFromUrl();
