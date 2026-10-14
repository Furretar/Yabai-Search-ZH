const form = document.getElementById("search-form");
const input = document.getElementById("q");
const results = document.getElementById("results");

function render(entries) {
  results.replaceChildren();
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

    const hanzi = document.createElement("span");
    hanzi.className = "hanzi";
    hanzi.textContent =
      entry.simplified === entry.traditional
        ? entry.simplified
        : `${entry.simplified} / ${entry.traditional}`;

    const pinyin = document.createElement("span");
    pinyin.className = "pinyin";
    pinyin.textContent = `[${entry.pinyin}]`;

    const defs = document.createElement("div");
    defs.className = "defs";
    defs.textContent = entry.english;

    row.append(hanzi, pinyin, defs);
    results.append(row);
  }
}

async function runSearch(query) {
  const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
  const data = await response.json();
  render(data.results);
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = input.value.trim();
  if (query) runSearch(query);
});
