// Front-end script for yabai-cidian. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const results = document.getElementById("results");

// Build a ruby annotation: the characters with their pinyin shown above them,
// like furigana. `pinyin` is space-separated by syllable (e.g. "nǐ hǎo").
function ruby(text, pinyin) {
  const chars = [...text]; // [...] splits into characters (code points)
  const syllables = pinyin.split(/\s+/).filter(Boolean);
  const node = document.createElement("ruby");

  if (chars.length === syllables.length) {
    // One syllable per character (the usual case): annotate each character.
    chars.forEach((char, i) => {
      const rt = document.createElement("rt");
      rt.textContent = syllables[i];
      node.append(char, rt);
    });
  } else {
    // Counts differ: show the whole reading above the whole word.
    node.append(text);
    const rt = document.createElement("rt");
    rt.textContent = pinyin;
    node.append(rt);
  }
  return node;
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
    hanzi.append(ruby(entry.simplified, entry.pinyin));

    // The definition (normalized form is friendlier to read).
    const defs = document.createElement("div");
    defs.className = "defs";
    defs.textContent = entry.english_normalized;

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

// Re-render when the user navigates with the browser's back/forward buttons.
window.addEventListener("popstate", restoreFromUrl);

// On first load, restore a search from the URL (e.g. after a reload).
restoreFromUrl();
