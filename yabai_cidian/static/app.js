// Front-end script for yabai-cidian. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const results = document.getElementById("results");

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

// Build furigana: the pinyin as a line above the characters, each a single
// line of text (not native <ruby>, not per-character cells). That way the
// browser treats each line as one selectable unit and a double-click grabs the
// whole line immediately.
function furigana(text, pinyin) {
  const node = document.createElement("span");
  node.className = "furigana";

  // The reading is one line. Non-breaking spaces keep it a single selectable
  // unit (a normal space would let a double-click grab just one syllable).
  const reading = document.createElement("span");
  reading.className = "reading";
  reading.textContent = pinyin.split(/\s+/).filter(Boolean).join("\u00a0");

  // The characters are a separate line, so selecting the word skips the reading.
  const chars = document.createElement("span");
  chars.className = "char";
  chars.textContent = text;

  node.append(reading, chars);
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
  results.replaceChildren(); // clear previous results

  if (!entries.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No results.";
    results.append(p);
    return;
  }

  for (const entry of entries) {
    const row = document.createElement("div");
    row.className = "entry";

    // The word (always simplified) with pinyin above each character.
    const hanzi = document.createElement("span");
    hanzi.className = "hanzi";
    hanzi.append(furigana(entry.simplified, entry.pinyin));

    // The definition, with cross-referenced words shown as furigana.
    const defs = document.createElement("div");
    defs.className = "defs";
    defs.append(renderDefinition(entry.english_normalized));

    row.append(hanzi, defs);

    // The traditional form, shown when it differs from the simplified one.
    if (entry.traditional !== entry.simplified) {
      const otherForms = document.createElement("div");
      otherForms.className = "other-forms";
      otherForms.textContent = `Other forms: ${entry.traditional} 【${entry.pinyin}】`;
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
    results.replaceChildren();
    showTagline(true);
  }
}

// Run a search when the form is submitted (Enter or the button).
form.addEventListener("submit", (event) => {
  event.preventDefault(); // don't reload the page
  const query = input.value.trim();
  if (query) performSearch(query);
});

// Double-clicking a character selects the whole word (all the characters),
// and double-clicking a reading selects the whole reading line — instead of the
// single piece the browser would normally pick.
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

// On first load, restore a search from the URL (e.g. after a reload).
restoreFromUrl();
