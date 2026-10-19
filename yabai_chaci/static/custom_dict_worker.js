// SQLite-WASM custom dictionary search index. Runs in a worker so OPFS sync
// access handles are available. The UI falls back to IndexedDB scanning when
// this worker or OPFS is unavailable.
import sqlite3InitModule from "/static/vendor/sqlite-wasm/sqlite3.mjs";

const SCHEMA = `
DROP TRIGGER IF EXISTS entries_ai;
DROP TRIGGER IF EXISTS entries_ad;
DROP TABLE IF EXISTS entries_trigram;
DROP TABLE IF EXISTS entries_def_trigram;
DROP TABLE IF EXISTS entries_fts;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS entries (
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
CREATE INDEX IF NOT EXISTS entries_simp ON entries(simplified);
CREATE INDEX IF NOT EXISTS entries_trad ON entries(traditional);
CREATE INDEX IF NOT EXISTS entries_pinyin ON entries(reading_toneless);
CREATE INDEX IF NOT EXISTS entries_term ON entries(term);
`;

let db = null;
let sqlite3 = null;

function post(message) {
    self.postMessage(message);
}

function normalize(value) {
    return String(value || "")
        .normalize("NFKC")
        .toLowerCase()
        .replace(/ü/g, "u")
        .normalize("NFD")
        .replace(/\p{M}/gu, "")
        .replace(/[^\p{Script=Han}a-z0-9\s]/gu, "")
        .trim();
}

function initDb() {
    db.exec("PRAGMA temp_store = MEMORY");
    db.exec(SCHEMA);
    // Older index files were created before source_key existed. Add the
    // column in place so a reload can resume rather than recreating the file.
    const columns = db.selectObjects("PRAGMA table_info(entries)");
    if (!columns.some((column) => column.name === "source_key")) {
        db.exec("ALTER TABLE entries ADD COLUMN source_key INTEGER");
    }
    db.exec(
        "CREATE UNIQUE INDEX IF NOT EXISTS entries_source " +
            "ON entries(dict_id, source_key)"
    );
}

function openDb() {
    if (db) return db;
    const pool = sqlite3.installOpfsSAHPoolVfs
        ? sqlite3.installOpfsSAHPoolVfs({ name: "yabai-custom-search" })
        : null;
    if (!pool) throw new Error("OPFS SAH pool is not available");
    return pool;
}

async function begin() {
    const pool = await openDb();
    db = new pool.OpfsSAHPoolDb("/yabai-custom-search.sqlite3");
    initDb();
}

function rowsFor(sql, params) {
    return db.selectObjects(sql, params || []);
}

function addRows(rows) {
    if (!rows.length) return;
    const statement = db.prepare(
        "INSERT OR IGNORE INTO entries (source_key, dict_id, term, simplified, traditional, reading, reading_toneless, definition) VALUES (?,?,?,?,?,?,?,?)"
    );
    try {
        db.exec("BEGIN");
        for (const row of rows) {
            statement.bind([
                row.source_key ?? null,
                row.dict_id,
                row.term,
                row.simplified || row.term,
                row.traditional || row.term,
                row.reading || "",
                normalize(row.reading_toneless || row.reading || ""),
                row.definition || "",
            ]);
            statement.step();
            statement.reset();
        }
        db.exec("COMMIT");
    } catch (error) {
        try {
            db.exec("ROLLBACK");
        } catch (rollbackError) {
            // Ignore a rollback on an already-closed transaction.
        }
        throw error;
    } finally {
        statement.finalize();
    }
}

function rowIdLimit() {
    return 50;
}

function addMatches(out, rows, rank, dictOrder) {
    for (const row of rows) {
        const existing = out.get(row.id);
        if (!existing || rank < existing.rank) {
            out.set(row.id, {
                ...row,
                rank,
                dictOrder: dictOrder.get(row.dict_id) ?? 999,
            });
        }
    }
}

function search(message) {
    const raw = normalize(message.query);
    if (!raw) return [];
    const dictIds = (message.dictIds || []).filter(Boolean);
    if (!dictIds.length) return [];
    const placeholders = dictIds.map(() => "?").join(",");
    const dictOrder = new Map(dictIds.map((id, index) => [id, index]));
    const han = /\p{Script=Han}/u.test(raw);
    const columns = [
        "id",
        "dict_id",
        "term",
        "simplified",
        "traditional",
        "reading",
        "reading_toneless",
        "definition",
    ].join(",");
    const matches = new Map();
    const limit = rowIdLimit();
    const run = (sql, params, rank) => {
        const rows = rowsFor(sql, params);
        addMatches(matches, rows, rank, dictOrder);
    };
    const upper = raw + "\uffff";

    // Tier 1: exact match.
    if (han) {
        run(
            `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND (simplified = ? OR traditional = ? OR term = ?) LIMIT ?`,
            [...dictIds, raw, raw, raw, limit],
            1
        );
    } else {
        run(
            `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND (reading_toneless = ? OR term = ?) LIMIT ?`,
            [...dictIds, raw, raw, limit],
            1
        );
    }

    // Tier 2: indexed prefix ranges.
    if (han) {
        for (const field of ["simplified", "traditional", "term"]) {
            run(
                `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND ${field} >= ? AND ${field} < ? LIMIT ?`,
                [...dictIds, raw, upper, limit],
                2
            );
        }
    } else {
        for (const field of ["reading_toneless", "term"]) {
            run(
                `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND ${field} >= ? AND ${field} < ? LIMIT ?`,
                [...dictIds, raw, upper, limit],
                2
            );
        }
    }

    // Tier 3: headword substring. There is no trigram FTS index in the
    // browser build (it was too slow to construct on phones), so use LIKE.
    if (han) {
        for (const field of ["term", "simplified", "traditional"]) {
            run(
                `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND ${field} LIKE ? LIMIT ?`,
                [...dictIds, `%${raw}%`, limit],
                3
            );
        }
    } else {
        // Pinyin/romanized queries can match anywhere in the reading, as the
        // old IndexedDB scan did.
        run(
            `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND (reading_toneless LIKE ? OR term LIKE ?) LIMIT ?`,
            [...dictIds, `%${raw}%`, `%${raw}%`, limit],
            3
        );
    }

    // Tier 4: definition match.
    run(
        `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND definition LIKE ? LIMIT ?`,
        [...dictIds, `%${raw}%`, limit],
        4
    );

    const result = [...matches.values()]
        .sort((a, b) => {
            if (a.rank !== b.rank) return a.rank - b.rank;
            if (a.dictOrder !== b.dictOrder) return a.dictOrder - b.dictOrder;
            const length = String(a.term).length - String(b.term).length;
            if (length) return length;
            return String(a.term).localeCompare(String(b.term));
        })
        .slice(0, limit);
    return result.map((row) => ({
        dictId: row.dict_id,
        term: row.term,
        simplified: row.simplified,
        traditional: row.traditional,
        reading: row.reading,
        definition: row.definition,
        rank: row.rank,
    }));
}

await (async () => {
    try {
        sqlite3 = await sqlite3InitModule({
            locateFile: () => "/static/vendor/sqlite-wasm/sqlite3.wasm",
        });
        await begin();
        post({ type: "ready" });
    } catch (error) {
        post({
            type: "error",
            error: String((error && error.message) || error),
            stack: error && error.stack,
        });
    }
})();

self.onmessage = async (event) => {
    const message = event.data || {};
    const id = message.id;
    try {
        if (!db) throw new Error("SQLite index is not ready");
        if (message.type === "init") {
            const row = rowsFor(
                "SELECT value FROM meta WHERE key = 'fingerprint'"
            )[0];
            // A partial build leaves this key behind so a reload can resume.
            const buildRow = rowsFor(
                "SELECT value FROM meta WHERE key = 'build_fingerprint'"
            )[0];
            post({
                type: "init",
                id,
                needsBuild: !row || row.value !== message.fingerprint,
                buildFingerprint: buildRow ? buildRow.value : "",
                count: rowsFor("SELECT COUNT(*) AS count FROM entries")[0]
                    .count,
            });
            return;
        }
        if (message.type === "beginBuild") {
            db.exec("BEGIN");
            db.exec("DELETE FROM entries");
            db.exec("COMMIT");
            const statement = db.prepare(
                "INSERT OR REPLACE INTO meta (key, value) " +
                    "VALUES ('build_fingerprint', ?)"
            );
            try {
                statement.bind([message.fingerprint || ""]);
                statement.step();
            } finally {
                statement.finalize();
            }
            post({ type: "ok", id });
            return;
        }
        if (message.type === "addRows") {
            addRows(message.rows || []);
            post({ type: "ok", id });
            return;
        }
        if (message.type === "endBuild") {
            const statement = db.prepare(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('fingerprint', ?)"
            );
            try {
                statement.bind([message.fingerprint]);
                statement.step();
            } finally {
                statement.finalize();
            }
            db.exec(
                "DELETE FROM meta WHERE key = 'build_fingerprint'"
            );
            post({
                type: "ok",
                id,
                count: rowsFor("SELECT COUNT(*) AS count FROM entries")[0]
                    .count,
            });
            return;
        }
        if (message.type === "wildcard") {
            const dictIds = (message.dictIds || []).filter(Boolean);
            if (!dictIds.length) {
                post({ type: "wildcard", id, total: 0, rows: [] });
                return;
            }
            const placeholders = dictIds.map(() => "?").join(",");
            const total = rowsFor(
                `SELECT COUNT(*) AS count FROM entries WHERE dict_id IN (${placeholders})`,
                dictIds
            )[0].count;
            const limit = Math.max(0, Number(message.limit) || 0);
            let rows = [];
            if (limit) {
                const offset = Math.max(0, Number(message.offset) || 0);
                rows = rowsFor(
                    `SELECT id, dict_id, term, simplified, traditional, reading, reading_toneless, definition
                     FROM entries WHERE dict_id IN (${placeholders})
                     ORDER BY term, traditional, reading, id LIMIT ? OFFSET ?`,
                    [...dictIds, limit, offset]
                ).map((row) => ({
                    dictId: row.dict_id,
                    term: row.term,
                    simplified: row.simplified,
                    traditional: row.traditional,
                    reading: row.reading,
                    definition: row.definition,
                }));
            }
            post({ type: "wildcard", id, total, rows });
            return;
        }
        if (message.type === "search") {
            post({
                type: "search",
                id,
                rows: search(message),
            });
            return;
        }
        if (message.type === "stats") {
            const count = rowsFor(
                "SELECT COUNT(*) AS count FROM entries"
            )[0].count;
            const pageCount = rowsFor("PRAGMA page_count")[0].page_count;
            const pageSize = rowsFor("PRAGMA page_size")[0].page_size;
            const meta = rowsFor(
                "SELECT value FROM meta WHERE key = 'fingerprint'"
            )[0];
            post({
                type: "stats",
                id,
                rows: count,
                bytes: (pageCount || 0) * (pageSize || 0),
                fingerprint: meta ? meta.value : "",
            });
            return;
        }
        throw new Error(`Unknown worker message: ${message.type}`);
    } catch (error) {
        post({
            type: "error",
            id,
            error: String((error && error.message) || error),
            stack: error && error.stack,
        });
    }
};
