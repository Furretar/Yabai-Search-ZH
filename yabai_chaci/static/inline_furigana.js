// Move pinyin that is already present in definition text into furigana above
// the Chinese characters before it. Everything here is local: no server calls.
(() => {
    "use strict";

    const results = document.getElementById("results");
    if (!results) return;

    const HAN_RE = /\p{Script=Han}/u;
    // A pinyin run can include spaces, apostrophes, slashes and the middle dot
    // used by the imported dictionaries (huà//yā, yí・ge, luòtuo'ān).
    const PINYIN_RUN_RE =
        /[\p{Script=Latin}0-9:]+(?:[\s'’/／・\-–]+[\p{Script=Latin}0-9:]+)*/gu;
    // The Han run and any symbol/space between it and the pinyin that follows.
    const HAN_BEFORE_RE =
        /([\p{Script=Han}]+)([^\p{Script=Han}\p{Script=Latin}\p{Script=Hiragana}\p{Script=Katakana}]*)$/u;

    function normalizeScan(text) {
        return [...text]
            .map((char) => {
                const code = char.codePointAt(0);
                if (code >= 0xff01 && code <= 0xff5e) {
                    return String.fromCodePoint(code - 0xfee0);
                }
                if (code === 0x3000) return " ";
                return char;
            })
            .join("");
    }

    function pinyinTokens(run) {
        if (typeof pinyinSyllables !== "function") return null;
        const tokens = pinyinSyllables(run);
        if (!tokens.length) return null;
        if (!tokens.every((token) => isPinyinSyllable(token))) return null;
        const hasTone =
            /[1-5]/.test(run) || tokens.some((token) => toneOf(token) !== 5);
        return hasTone ? tokens : null;
    }

    function makeRuby(char, syllable) {
        const phonetic = window.state?.phonetic === "zhuyin" ? "zhuyin" : "pinyin";
        const reading =
            phonetic === "zhuyin" && typeof toZhuyin === "function"
                ? toZhuyin(syllable)
                : syllable;

        const ruby = document.createElement("ruby");
        ruby.className = "inline-furigana";
        const base = document.createElement("rb");
        base.textContent = char;
        if (window.state?.toneColoring && typeof toneOf === "function") {
            base.className = `tone${toneOf(syllable)}`;
        }
        ruby.append(base);

        const rt = document.createElement("rt");
        if (window.state?.toneColoring && typeof toneOf === "function") {
            const colored = document.createElement("span");
            colored.className = `tone${toneOf(syllable)}`;
            colored.textContent = reading;
            rt.append(colored);
        } else {
            rt.textContent = reading;
        }
        ruby.append(rt);
        return ruby;
    }

    function annotateTextNode(node) {
        const original = node.data;
        const scan = normalizeScan(original);
        if (!HAN_RE.test(scan)) return;

        let cursor = 0;
        let fragment = null;

        for (const match of scan.matchAll(PINYIN_RUN_RE)) {
            const run = match[0];
            const tokens = pinyinTokens(run);
            if (!tokens) continue;

            const before = scan.slice(0, match.index);
            const han = before.match(HAN_BEFORE_RE);
            if (!han) continue;

            const hanChars = [...han[1]];
            // A partial reading such as "国际机场 chǎng" belongs to the last
            // character. More syllables than characters is too ambiguous.
            if (tokens.length > hanChars.length) continue;

            const hanStart = match.index - han[0].length;
            const keep = hanChars.length - tokens.length;
            if (!fragment) fragment = document.createDocumentFragment();
            fragment.append(original.slice(cursor, hanStart));
            if (keep) fragment.append(hanChars.slice(0, keep).join(""));
            for (let i = 0; i < tokens.length; i += 1) {
                fragment.append(makeRuby(hanChars[keep + i], tokens[i]));
            }
            cursor = match.index + run.length;
        }

        if (!fragment) return;
        fragment.append(original.slice(cursor));
        node.replaceWith(fragment);
    }

    function annotateLine(line) {
        if (line.dataset.inlineFurigana === "done") return;
        line.dataset.inlineFurigana = "done";

        const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                if (!node.data || !HAN_RE.test(node.data)) {
                    return NodeFilter.FILTER_REJECT;
                }
                if (node.parentElement?.closest(".inline-furigana, .furigana")) {
                    return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
            },
        });

        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        for (const node of nodes) annotateTextNode(node);
    }

    function scan() {
        const started = performance.now();
        let lines = 0;
        for (const line of results.querySelectorAll(".def-source-line")) {
            if (line.dataset.inlineFurigana === "done") continue;
            lines += 1;
            annotateLine(line);
        }
        const elapsed = performance.now() - started;
        stats.runs += 1;
        stats.totalMs += elapsed;
        stats.lastMs = elapsed;
        stats.lastLines = lines;
        if (lines) {
            stats.lastUsefulMs = elapsed;
            stats.lastUsefulLines = lines;
        }
    }

    let scheduled = false;
    function schedule() {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(() => {
            scheduled = false;
            scan();
        });
    }

    const stats = {
        runs: 0,
        totalMs: 0,
        lastMs: 0,
        lastLines: 0,
        lastUsefulMs: 0,
        lastUsefulLines: 0,
    };
    window.inlineFuriganaStats = stats;

    new MutationObserver(schedule).observe(results, {
        childList: true,
        subtree: true,
        characterData: true,
    });
    schedule();
})();
