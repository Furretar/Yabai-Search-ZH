// Microbenchmark for browser dictionary gram collection. Run with:
//   node --expose-gc scripts/tools/bench_grams.mjs
//
// This compares the old global string Sets with the import-time numeric
// bigram + sorted per-bank collector used by custom_dicts.js.

const HAN_RANGES = [
    [0x3400, 0x4dbf],
    [0x4e00, 0x9fff],
    [0x20000, 0x20010],
];

function isHan(code) {
    return HAN_RANGES.some(([start, end]) => code >= start && code <= end);
}

let seed = 0x12345678;
function random() {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 0x100000000;
}
function randomHan() {
    if (random() < 0.02) {
        return String.fromCodePoint(0x20000 + Math.floor(random() * 17));
    }
    return String.fromCodePoint(0x4e00 + Math.floor(random() * 3000));
}
function makeText(min, max) {
    const length = min + Math.floor(random() * (max - min + 1));
    let text = "";
    for (let index = 0; index < length; index += 1) text += randomHan();
    return text;
}

const ROW_COUNT = Number(process.env.ROWS) || 30000;
const BANK_SIZE = 2000;
const rows = Array.from({ length: ROW_COUNT }, () => ({
    term: makeText(2, 6),
    reading: makeText(2, 6),
    gloss: makeText(20, 60),
}));

function addHanGrams(text, bigrams, trigrams) {
    let run = [];
    const flush = () => {
        for (let index = 0; index < run.length; index += 1) {
            if (index + 1 < run.length) {
                bigrams.add(run[index] + run[index + 1]);
            }
            if (index + 2 < run.length) {
                trigrams.add(run[index] + run[index + 1] + run[index + 2]);
            }
        }
    };
    for (const char of text) {
        if (isHan(char.codePointAt(0))) run.push(char);
        else {
            flush();
            run = [];
        }
    }
    flush();
}

const BIGRAM_BASE = 0x200000;
function packBigram(a, b) {
    return a * BIGRAM_BASE + b;
}
function mergeSortedNumbers(left, right) {
    const merged = [];
    let i = 0;
    let j = 0;
    while (i < left.length && j < right.length) {
        const a = left[i];
        const b = right[j];
        if (a < b) merged.push(a), (i += 1);
        else if (b < a) merged.push(b), (j += 1);
        else merged.push(a), (i += 1), (j += 1);
    }
    while (i < left.length) merged.push(left[i++]);
    while (j < right.length) merged.push(right[j++]);
    return merged;
}
function mergeSortedStrings(left, right) {
    const merged = [];
    let i = 0;
    let j = 0;
    while (i < left.length && j < right.length) {
        const a = left[i];
        const b = right[j];
        if (a < b) merged.push(a), (i += 1);
        else if (b < a) merged.push(b), (j += 1);
        else merged.push(a), (i += 1), (j += 1);
    }
    while (i < left.length) merged.push(left[i++]);
    while (j < right.length) merged.push(right[j++]);
    return merged;
}
function createGramCollector() {
    const bigrams = [];
    const trigrams = [];
    let run = [];
    const flush = () => {
        for (let index = 0; index < run.length; index += 1) {
            if (index + 1 < run.length) {
                bigrams.push(packBigram(run[index], run[index + 1]));
            }
            if (index + 2 < run.length) {
                trigrams.push(
                    String.fromCodePoint(run[index]) +
                        String.fromCodePoint(run[index + 1]) +
                        String.fromCodePoint(run[index + 2])
                );
            }
        }
    };
    return {
        addText(text) {
            run = [];
            for (const char of text) {
                const code = char.codePointAt(0);
                if (isHan(code)) run.push(code);
                else {
                    flush();
                    run = [];
                }
            }
            flush();
        },
        finish() {
            bigrams.sort((a, b) => a - b);
            let write = 0;
            for (let read = 0; read < bigrams.length; read += 1) {
                if (read === 0 || bigrams[read] !== bigrams[read - 1]) {
                    bigrams[write++] = bigrams[read];
                }
            }
            bigrams.length = write;
            trigrams.sort();
            write = 0;
            for (let read = 0; read < trigrams.length; read += 1) {
                if (read === 0 || trigrams[read] !== trigrams[read - 1]) {
                    trigrams[write++] = trigrams[read];
                }
            }
            trigrams.length = write;
            return { bigrams, trigrams };
        },
    };
}

function heapMb() {
    global.gc?.();
    return (process.memoryUsage().heapUsed / 1024 / 1024).toFixed(1);
}

function mergeAll(arrays, mergeFn) {
    let level = arrays;
    while (level.length > 1) {
        const next = [];
        for (let i = 0; i < level.length; i += 2) {
            next.push(
                i + 1 < level.length
                    ? mergeFn(level[i], level[i + 1])
                    : level[i]
            );
        }
        level = next;
    }
    return level[0] || [];
}

function createSortedMerger(mergeFn) {
    const tiers = [];
    let total = 0;
    return {
        add(sorted) {
            total += sorted.length;
            let current = sorted;
            let level = 0;
            while (tiers[level]) {
                current = mergeFn(tiers[level], current);
                tiers[level] = null;
                level += 1;
            }
            tiers[level] = current;
        },
        get total() {
            return total;
        },
        finish() {
            let result = [];
            for (const tier of tiers) {
                if (!tier) continue;
                result = result.length ? mergeFn(result, tier) : tier;
            }
            return result;
        },
    };
}

function runNumericTiered() {
    const before = heapMb();
    const started = performance.now();
    const bigramMerger = createSortedMerger(mergeSortedNumbers);
    const trigramMerger = createSortedMerger(mergeSortedStrings);
    for (let offset = 0; offset < rows.length; offset += BANK_SIZE) {
        const collector = createGramCollector();
        for (const row of rows.slice(offset, offset + BANK_SIZE)) {
            collector.addText(row.term);
            collector.addText(row.reading);
            collector.addText(row.gloss);
        }
        const part = collector.finish();
        bigramMerger.add(part.bigrams);
        trigramMerger.add(part.trigrams);
    }
    const bigrams = bigramMerger.finish();
    const trigrams = trigramMerger.finish();
    return {
        ms: Math.round(performance.now() - started),
        heap: `${before} -> ${heapMb()} MB`,
        bigrams: bigrams.length,
        trigrams: trigrams.length,
    };
}

function runStringSets() {
    const before = heapMb();
    const started = performance.now();
    const bigrams = new Set();
    const trigrams = new Set();
    for (const row of rows) {
        addHanGrams(row.term, bigrams, trigrams);
        addHanGrams(row.reading, bigrams, trigrams);
        addHanGrams(row.gloss, bigrams, trigrams);
    }
    return {
        ms: Math.round(performance.now() - started),
        heap: `${before} -> ${heapMb()} MB`,
        bigrams: bigrams.size,
        trigrams: trigrams.size,
    };
}

function runNumericCollector() {
    const before = heapMb();
    const started = performance.now();
    const bigramParts = [];
    const trigramParts = [];
    for (let offset = 0; offset < rows.length; offset += BANK_SIZE) {
        const collector = createGramCollector();
        for (const row of rows.slice(offset, offset + BANK_SIZE)) {
            collector.addText(row.term);
            collector.addText(row.reading);
            collector.addText(row.gloss);
        }
        const part = collector.finish();
        bigramParts.push(part.bigrams);
        trigramParts.push(part.trigrams);
    }
    const bigrams = mergeAll(bigramParts, mergeSortedNumbers);
    const trigrams = mergeAll(trigramParts, mergeSortedStrings);
    return {
        ms: Math.round(performance.now() - started),
        heap: `${before} -> ${heapMb()} MB`,
        bigrams: bigrams.length,
        trigrams: trigrams.length,
    };
}

function runNumericSets() {
    const before = heapMb();
    const started = performance.now();
    const bigrams = new Set();
    const trigrams = new Set();
    const addText = (text) => {
        let run = [];
        const flush = () => {
            for (let index = 0; index < run.length; index += 1) {
                if (index + 1 < run.length) {
                    bigrams.add(packBigram(run[index], run[index + 1]));
                }
                if (index + 2 < run.length) {
                    trigrams.add(
                        String.fromCodePoint(run[index]) +
                            String.fromCodePoint(run[index + 1]) +
                            String.fromCodePoint(run[index + 2])
                    );
                }
            }
        };
        for (const char of text) {
            const code = char.codePointAt(0);
            if (isHan(code)) run.push(code);
            else {
                flush();
                run = [];
            }
        }
        flush();
    };
    for (const row of rows) {
        addText(row.term);
        addText(row.reading);
        addText(row.gloss);
    }
    const numericBigrams = [...bigrams].sort((a, b) => a - b);
    const stringTrigrams = [...trigrams].sort();
    return {
        ms: Math.round(performance.now() - started),
        heap: `${before} -> ${heapMb()} MB`,
        bigrams: numericBigrams.length,
        trigrams: stringTrigrams.length,
    };
}

console.log(`rows: ${ROW_COUNT}, bank: ${BANK_SIZE}`);
console.log("strings:", runStringSets());
console.log("numeric:", runNumericCollector());
console.log("numeric sets:", runNumericSets());
console.log("numeric tiered:", runNumericTiered());
