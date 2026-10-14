// Front-end script for yabai-cidian. Runs in the browser.
// It calls the /api/search endpoint and renders the results into the page.

// Grab the elements we need from index.html (by their id attributes).
const form = document.getElementById("search-form");
const input = document.getElementById("q");
const results = document.getElementById("results");

// Build the result list from the array of entries returned by the API.
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

    // Show both scripts when they differ, e.g. "汉语 / 漢語".
    const hanzi = document.createElement("span");
    hanzi.className = "hanzi";
    hanzi.textContent =
      entry.simplified === entry.traditional
        ? entry.simplified
        : `${entry.simplified} / ${entry.traditional}`;

    // The pinyin reading, in brackets.
    const pinyin = document.createElement("span");
    pinyin.className = "pinyin";
    pinyin.textContent = `[${entry.pinyin}]`;

    // The definition (normalized form is friendlier to read).
    const defs = document.createElement("div");
    defs.className = "defs";
    defs.textContent = entry.english_normalized;

    row.append(hanzi, pinyin, defs);
    results.append(row);
  }
}

// Query the API and hand the results to render().
async function search(query) {
  const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
  const data = await response.json();
  render(data.results);
}

// Run a search when the form is submitted (Enter or the button).
form.addEventListener("submit", (event) => {
  event.preventDefault(); // don't reload the page
  const query = input.value.trim();
  if (query) search(query);
});
