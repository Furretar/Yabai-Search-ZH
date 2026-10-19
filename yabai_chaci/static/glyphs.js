// MOE glyph pictures: some characters in the 重編國語辭典修訂本 could not be
// written in Unicode, so the data keeps them as a picture reference like
// "&8ff0._104_0.gif;". This module swaps each reference for a
// <span class="moe-glyph"> that shows the picture, tinted with the surrounding
// text colour (the CSS mask trick, same as the IDS headword glyphs in style.css).
//
// Two shapes are handled:
//   - raw references in ordinary text (definitions, the word page), and
//   - headwords run through maskGlyphs() first: furigana() splits a headword
//     into one cell per character, which would shred "&8ff0._104_0.gif;" into
//     pieces no pattern could match. maskGlyphs replaces each reference with a
//     single private-use placeholder character so layout counts it as one
//     character, and renderGlyphs() below swaps either shape back to a picture.
//
// Loaded by index.html (before app.js, which calls maskGlyphs during rendering
// and renderGlyphs at the end of each result row) and by word.html (which calls
// renderGlyphs once on load; the word page has no app.js).

// "&<name>.gif;" / "&<name>.png;" - name is MOE's hex code, e.g. "8ff0._104_0.gif".
const MOE_GLYPH_RE = /&([A-Za-z0-9][A-Za-z0-9._-]*\.(?:gif|png));/g;

// Private-use-area characters stand in for references while text is laid out.
// The BMP private-use area (U+E000..U+F8FF) holds thousands of characters;
// this app needs one per distinct glyph (a few dozen).
const glyphNames = new Map(); // placeholder character -> reference name
const glyphChars = new Map(); // reference name -> placeholder character

// A picture glyph node for one reference. `name` comes from the regex above,
// which already forbids slashes, so it cannot escape /moe-glyphs/.
function moeGlyph(name) {
    const span = document.createElement("span");
    span.className = "moe-glyph";
    // The mask image, coloured by background-color: currentColor in the CSS.
    span.style.setProperty("--moe-glyph", `url("/moe-glyphs/${name}")`);
    // The true character is not in the data (that is the point of the picture),
    // so the reference itself is the only label to give assistive technology.
    span.setAttribute("role", "img");
    span.setAttribute("aria-label", name.replace(/\.(?:gif|png)$/, ""));
    return span;
}

// One placeholder character per distinct reference name, assigned on first use.
function placeholderFor(name) {
    let char = glyphChars.get(name);
    if (char === undefined) {
        char = String.fromCodePoint(0xe000 + glyphChars.size);
        glyphChars.set(name, char);
        glyphNames.set(char, name);
    }
    return char;
}

// Replace every glyph reference in `text` with its placeholder character.
// Call this before handing text to per-character layout (furigana); swap the
// placeholders back with renderGlyphs() once the nodes exist.
function maskGlyphs(text) {
    return text.replace(MOE_GLYPH_RE, (_whole, name) => placeholderFor(name));
}

// Replace glyph references and placeholders inside `root`'s text nodes with
// picture spans. Safe to call more than once: the spans hold no text nodes of
// their own, so later passes find nothing new. Each text node is split around
// the matches and the pieces keep their original parent, so no layout or
// tone-colouring class around the node is disturbed.
function renderGlyphs(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const targets = [];
    let node;
    while ((node = walker.nextNode())) {
        const data = node.data;
        MOE_GLYPH_RE.lastIndex = 0;
        if (
            MOE_GLYPH_RE.test(data) ||
            [...data].some((ch) => glyphNames.has(ch))
        ) {
            targets.push(node);
        }
    }
    for (const text of targets) {
        // Mask first, then split on placeholders - one pass handles both shapes.
        const masked = maskGlyphs(text.data);
        const fragment = document.createDocumentFragment();
        let buffer = "";
        for (const ch of masked) {
            const name = glyphNames.get(ch);
            if (name === undefined) {
                buffer += ch;
                continue;
            }
            if (buffer) {
                fragment.append(buffer);
                buffer = "";
            }
            fragment.append(moeGlyph(name));
        }
        if (buffer) fragment.append(buffer);
        text.replaceWith(fragment);
    }
}

// The word page renders once on load, so swap its references right away.
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
        const main = document.querySelector(".word-main");
        if (main) renderGlyphs(main);
    });
} else {
    const main = document.querySelector(".word-main");
    if (main) renderGlyphs(main);
}
