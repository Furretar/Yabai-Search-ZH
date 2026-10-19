// Regression test for the browser custom-dictionary search SQL. Run with:
//   node scripts/test_custom_search.mjs
//
// The oracle is an exhaustive in-memory LIKE search using the same comparator
// as the worker. The ngram path must return the same top 50 rows.

import { DatabaseSync } from "node:sqlite";
import {
    HAN_RE,
    addRowNgrams,
    compareMatches,
    normalize,
    searchChunkedEntries,
    searchEntries,
} from "../yabai_chaci/static/custom_dict_search.mjs";

const db = new DatabaseSync(":memory:");
db.exec(`
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    source_key INTEGER,
    dict_id TEXT NOT NULL,
    term TEXT NOT NULL,
    simplified TEXT NOT NULL,
    traditional TEXT NOT NULL,
    reading TEXT NOT NULL,
    reading_toneless TEXT NOT NULL,
    definition TEXT NOT NULL
);
CREATE INDEX entries_dict_simp ON entries(dict_id, simplified);
CREATE INDEX entries_dict_trad ON entries(dict_id, traditional);
CREATE INDEX entries_dict_pinyin ON entries(dict_id, reading_toneless);
CREATE INDEX entries_dict_term ON entries(dict_id, term);
CREATE UNIQUE INDEX entries_source ON entries(dict_id, source_key);
CREATE TABLE ngrams (
    gram TEXT NOT NULL,
    entry_id INTEGER NOT NULL,
    PRIMARY KEY (gram, entry_id)
) WITHOUT ROWID;
`);

const samples = [
    ["a", "只", "只", "只", "zhi", "only"],
    ["a", "只有", "只有", "只有", "zhi you", "only have"],
    ["a", "隻身", "只身", "隻身", "zhi shen", "alone"],
    ["a", "知", "知", "知", "zhi", "to know"],
    ["a", "一石二鳥", "一石二鸟", "一石二鳥", "yi shi er niao", "two birds one stone"],
    ["a", "A型", "A型", "A型", "A xing", "type A"],
    ["a", "3D", "3D", "3D", "3D", "three dimensional"],
    ["a", "𠀀", "𠀀", "𠀀", "he", "rare supplement"],
    ["b", "隻", "只", "隻", "zhi", "measure word"],
    ["b", "只有你", "只有你", "只有你", "zhi you ni", "only you"],
    ["b", "guo", "guo", "guo", "guo", "a plain latin headword"],
    ["b", "過", "过", "過", "guo", "to pass"],
    ["b", "only", "only", "only", "only", "english only"],
    ["b", "hello", "hello", "hello", "hello", "a greeting"],
];

const insertRow = db.prepare(
    `INSERT INTO entries
     (id, source_key, dict_id, term, simplified, traditional, reading,
      reading_toneless, definition)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
);
const insertGram = db.prepare(
    "INSERT OR IGNORE INTO ngrams (gram, entry_id) VALUES (?, ?)"
);

let nextId = 1;
for (const [dictId, term, simplified, traditional, reading, definition] of samples) {
    const id = nextId++;
    const normalizedReading = normalize(reading);
    insertRow.run(
        id,
        id,
        dictId,
        term,
        simplified,
        traditional,
        reading,
        normalizedReading,
        definition
    );
    let pending = null;
    const statement = {
        bind: (params) => {
            pending = params;
        },
        step: () => {
            insertGram.run(...pending);
        },
        reset: () => {
            pending = null;
        },
    };
    addRowNgrams(
        {
            dict_id: dictId,
            term,
            simplified,
            traditional,
        },
        id,
        statement
    );
}

const adapter = {
    selectObjects: (sql, params = []) => db.prepare(sql).all(...params),
};

function allRows(dictIds) {
    const placeholders = dictIds.map(() => "?").join(",");
    return adapter.selectObjects(
        `SELECT * FROM entries WHERE dict_id IN (${placeholders}) ORDER BY id`,
        dictIds
    );
}

function oracleHeadwords(query, dictIds) {
    const raw = normalize(query);
    const han = HAN_RE.test(raw);
    const order = new Map(dictIds.map((id, index) => [id, index]));
    const matches = [];
    for (const row of allRows(dictIds)) {
        const term = String(row.term || "");
        const simplified = String(row.simplified || term);
        const traditional = String(row.traditional || term);
        const reading = String(row.reading_toneless || "").toLowerCase();
        let rank = 0;
        if (han) {
            if (simplified === raw || traditional === raw || term === raw) {
                rank = 1;
            } else if (
                simplified.startsWith(raw) ||
                traditional.startsWith(raw) ||
                term.startsWith(raw)
            ) {
                rank = 2;
            } else if (
                // SQLite LIKE is case-insensitive for ASCII.
                term.toLowerCase().includes(raw) ||
                simplified.toLowerCase().includes(raw) ||
                traditional.toLowerCase().includes(raw)
            ) {
                rank = 3;
            }
        } else {
            // Non-Han substring matches come from the chunked pinyin scan,
            // not from searchEntries().
            if (reading === raw || term === raw) rank = 1;
            else if (reading.startsWith(raw) || term.startsWith(raw)) rank = 2;
        }
        if (!rank) continue;
        matches.push({
            row,
            rank,
            term,
            dictOrder: order.get(row.dict_id) ?? 999,
        });
    }
    matches.sort(compareMatches);
    return matches.slice(0, 50).map((match) => ({
        dictId: match.row.dict_id,
        term: match.row.term,
        reading: match.row.reading,
        rank: match.rank,
    }));
}

function oracleChunked(query, dictIds, mode, skipKeys = []) {
    const raw = normalize(query);
    const skip = new Set(skipKeys);
    const matches = [];
    for (const row of allRows(dictIds)) {
        const key = `${row.dict_id}|${row.term}|${row.reading}`;
        if (skip.has(key)) continue;
        const reading = String(row.reading_toneless || "").toLowerCase();
        const term = String(row.term || "").toLowerCase();
        const definition = String(row.definition || "").toLowerCase();
        const hit =
            mode === "pinyin"
                ? reading.includes(raw) || term.includes(raw)
                : definition.includes(raw);
        if (hit) {
            matches.push({
                dictId: row.dict_id,
                term: row.term,
                reading: row.reading,
                rank: mode === "pinyin" ? 3 : 4,
            });
        }
    }
    return matches.slice(0, 50);
}

function keyOf(match) {
    return `${match.dictId}|${match.term}|${match.reading}`;
}

function assertSame(label, actual, expected) {
    const a = actual.map(keyOf);
    const e = expected.map(keyOf);
    if (JSON.stringify(a) !== JSON.stringify(e)) {
        console.error(`FAIL ${label}`);
        console.error("  actual:  ", a);
        console.error("  expected:", e);
        process.exitCode = 1;
    } else {
        console.log(`ok ${label}`);
    }
}

const dictIds = ["a", "b"];
const queries = [
    "只",
    "隻",
    "只有",
    "只身",
    "一石二鳥",
    "𠀀",
    "A型",
    "3D",
    "zhi",
    "guo",
    "only",
];
for (const query of queries) {
    const found = searchEntries(adapter, {
        query,
        dictIds,
        ngramsReady: true,
    });
    assertSame(
        `headwords ${query}`,
        found.rows,
        oracleHeadwords(query, dictIds)
    );
    const likeFound = searchEntries(adapter, {
        query,
        dictIds,
        ngramsReady: false,
    });
    assertSame(
        `headwords-like ${query}`,
        likeFound.rows,
        oracleHeadwords(query, dictIds)
    );
}

for (const [mode, queries] of [
    ["defs", ["only", "stone", "greeting"]],
    ["pinyin", ["zhi", "guo", "hello"]],
]) {
    for (const query of queries) {
        const found = await searchChunkedEntries(
            adapter,
            { query, dictIds, limit: 50 },
            mode,
            () => false
        );
        assertSame(
            `${mode} ${query}`,
            found.rows,
            oracleChunked(query, dictIds, mode)
        );
    }
}

if (process.exitCode) {
    console.error("custom search regression test failed");
} else {
    console.log("custom search regression test passed");
}
