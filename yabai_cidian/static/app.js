// Front-end script for yabai-cidian. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const results = document.getElementById("results");
const scriptToggle = document.getElementById("script-toggle");
const toneToggle = document.getElementById("tone-toggle");

// Display options. script: "simplified" | "traditional"; toneColoring: on/off.
const state = { script: "simplified", toneColoring: false };

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

// Convert a whole numeric reading, e.g. "guo2 ji4 qi3" -> "guó jì qǐ".
function numericToMarked(pinyin) {
  return pinyin.split(/\s+/).map(markSyllable).join(" ");
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
function furigana(text, pinyin) {
  const chars = [...text];
  const syllables = pinyin.split(/\s+/).filter(Boolean);
  const node = document.createElement("span");
  node.className = "furigana";

  if (chars.length === syllables.length) {
    const colored = state.toneColoring;
    chars.forEach((_, i) => {
      node.append(gridCell("reading", syllables[i], String(i + 1), colored ? toneOf(syllables[i]) : 0));
    });
    chars.forEach((char, i) => {
      node.append(gridCell("char", char, String(i + 1), colored ? toneOf(syllables[i]) : 0));
    });
  } else {
    // Counts differ: put the whole reading above the whole word.
    node.append(gridCell("reading", pinyin, "1 / -1", 0));
    node.append(gridCell("char", text, "1 / -1", 0));
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

    const row = document.createElement("div");
    row.className = "entry";

    const hanzi = document.createElement("span");
    hanzi.className = "hanzi";
    hanzi.append(furigana(primary, entry.pinyin));

    const defs = document.createElement("div");
    defs.className = "defs";
    defs.append(renderDefinition(entry.english_normalized));

    row.append(hanzi, defs);

    if (other !== primary) {
      const otherForms = document.createElement("div");
      otherForms.className = "other-forms";
      otherForms.textContent = `Other forms: ${other} 【${entry.pinyin}】`;
      row.append(otherForms);
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
  search(query);
}

// Restore the view (results or home) from whatever is in the URL now.
function restoreFromUrl() {
  const query = queryFromUrl();
  if (query) {
    input.value = query;
    showTagline(false);
    search(query);
  } else {
    input.value = "";
    render([]);
    showTagline(true);
  }
}

// Load the display preferences from localStorage (if it's available).
function loadPrefs() {
  try {
    const script = localStorage.getItem("yabai-cidian-script");
    if (script === "traditional" || script === "simplified") state.script = script;
    state.toneColoring = localStorage.getItem("yabai-cidian-tone") === "true";
  } catch (e) {
    // localStorage unavailable — keep the defaults.
  }
  scriptToggle.checked = state.script === "traditional";
  toneToggle.checked = state.toneColoring;
}

// Persist the display preferences.
function savePrefs() {
  try {
    localStorage.setItem("yabai-cidian-script", state.script);
    localStorage.setItem("yabai-cidian-tone", String(state.toneColoring));
  } catch (e) {
    // localStorage unavailable — nothing to save.
  }
}

// Re-render the current results when a display toggle changes.
scriptToggle.addEventListener("change", () => {
  state.script = scriptToggle.checked ? "traditional" : "simplified";
  savePrefs();
  render(currentEntries);
});
toneToggle.addEventListener("change", () => {
  state.toneColoring = toneToggle.checked;
  savePrefs();
  render(currentEntries);
});

// Run a search when the form is submitted (Enter or the button).
form.addEventListener("submit", (event) => {
  event.preventDefault(); // don't reload the page
  const query = input.value.trim();
  if (query) performSearch(query);
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
