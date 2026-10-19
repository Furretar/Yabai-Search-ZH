// SQLite-WASM custom dictionary search index. Runs in a worker so OPFS sync
// access handles are available. The UI falls back to IndexedDB scanning when
// this worker or OPFS is unavailable.
import sqlite3InitModule from "/static/vendor/sqlite-wasm/sqlite3.mjs";

// Carry the worker's cache-busting version onto the shared search module too.
const SCRIPT_VERSION = new URL(import.meta.url).searchParams.get("v") || "";
const {
    addRowNgrams,
    normalize,
    rowNgrams,
    searchChunkedEntries,
    searchEntries,
} = await import(
    `./custom_dict_search.mjs${
        SCRIPT_VERSION ? `?v=${encodeURIComponent(SCRIPT_VERSION)}` : ""
    }`
);

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
DROP INDEX IF EXISTS entries_simp;
DROP INDEX IF EXISTS entries_trad;
DROP INDEX IF EXISTS entries_pinyin;
DROP INDEX IF EXISTS entries_term;
/* Unigram/bigram inverted index for short Hanzi queries. This is a plain
   rowid table during the load; its lookup index is created at the end so the
   bulk load appends instead of doing random B-tree inserts. */
CREATE TABLE IF NOT EXISTS ngrams (
    id INTEGER PRIMARY KEY,
    gram TEXT NOT NULL,
    dict_id TEXT NOT NULL,
    entry_id INTEGER NOT NULL
);
`;

const SEARCH_INDEXES = [
    "CREATE INDEX IF NOT EXISTS entries_dict_simp ON entries(dict_id, simplified)",
    "CREATE INDEX IF NOT EXISTS entries_dict_trad ON entries(dict_id, traditional)",
    "CREATE INDEX IF NOT EXISTS entries_dict_pinyin ON entries(dict_id, reading_toneless)",
    "CREATE INDEX IF NOT EXISTS entries_dict_term ON entries(dict_id, term)",
    "CREATE UNIQUE INDEX IF NOT EXISTS entries_source ON entries(dict_id, source_key)",
];
const NGRAM_INDEX =
    "CREATE INDEX IF NOT EXISTS ngrams_idx ON ngrams(gram, dict_id, entry_id)";

function ensureSearchIndexes() {
    for (const sql of SEARCH_INDEXES) db.exec(sql);
}

function dropSearchIndexes() {
    db.exec("DROP INDEX IF EXISTS entries_dict_simp");
    db.exec("DROP INDEX IF EXISTS entries_dict_trad");
    db.exec("DROP INDEX IF EXISTS entries_dict_pinyin");
    db.exec("DROP INDEX IF EXISTS entries_dict_term");
    db.exec("DROP INDEX IF EXISTS entries_source");
}

function ensureNgramIndex() {
    db.exec(NGRAM_INDEX);
}

function dropNgramIndex() {
    db.exec("DROP INDEX IF EXISTS ngrams_idx");
}

let db = null;
let sqlite3 = null;
let definitionSeq = 0;
let ngramsReady = false;

function post(message) {
    self.postMessage(message);
}

function initDb() {
    db.exec("PRAGMA temp_store = MEMORY");
    // This index is a rebuildable cache, so durability during the bulk build
    // is not worth the extra write cost on a phone.
    db.exec("PRAGMA synchronous = OFF");
    try {
        db.exec("PRAGMA journal_mode = MEMORY");
    } catch (error) {
        // Some OPFS builds do not allow changing the journal mode.
    }
    db.exec("PRAGMA cache_size = -32768");
    db.exec(SCHEMA);
    // Older index files were created before source_key existed. Add the
    // column in place so a reload can resume rather than recreating the file.
    const columns = db.selectObjects("PRAGMA table_info(entries)");
    if (!columns.some((column) => column.name === "source_key")) {
        db.exec("ALTER TABLE entries ADD COLUMN source_key INTEGER");
    }
    // Migrate the old WITHOUT ROWID ngrams table to the append-friendly rowid
    // table. The ngram backfill rebuilds it from entries.
    const ngramColumns = db.selectObjects("PRAGMA table_info(ngrams)");
    if (
        ngramColumns.length &&
        !ngramColumns.some((column) => column.name === "id")
    ) {
        db.exec("DROP TABLE ngrams");
        db.exec(
            "CREATE TABLE ngrams (id INTEGER PRIMARY KEY, gram TEXT NOT NULL, " +
                "dict_id TEXT NOT NULL, entry_id INTEGER NOT NULL)"
        );
        setMeta("ngrams_ready", "0");
        setMeta("ngram_cursor", "0");
        setMeta("ngram_version", "");
    }
    ngramsReady = metaValue("ngrams_ready") === "1";
    if (ngramsReady) {
        ensureSearchIndexes();
        ensureNgramIndex();
    }
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

function setMeta(key, value) {
    const statement = db.prepare(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)"
    );
    try {
        statement.bind([key, String(value)]);
        statement.step();
    } finally {
        statement.finalize();
    }
}

function metaValue(key) {
    const row = rowsFor("SELECT value FROM meta WHERE key = ?", [key])[0];
    return row ? row.value : null;
}

function addRows(rows) {
    if (!rows.length) return;
    const withKeys = [];
    const withoutKeys = [];
    for (const row of rows) {
        if (row.source_key === null || row.source_key === undefined) {
            withoutKeys.push(row);
        } else {
            withKeys.push(row);
        }
    }
    try {
        db.exec("BEGIN");
        if (withKeys.length) {
            const ids = withKeys.map((row) => row.source_key);
            const existingRows = db.selectObjects(
                `SELECT id FROM entries
                 WHERE id IN (SELECT value FROM json_each(?))`,
                [JSON.stringify(ids)]
            );
            const existing = new Set(
                existingRows.map((row) => Number(row.id))
            );
            const entryValues = withKeys.map((row) => [
                row.source_key,
                row.source_key,
                row.dict_id,
                row.term,
                row.simplified || row.term,
                row.traditional || row.term,
                row.reading || "",
                normalize(row.reading_toneless || row.reading || ""),
                row.definition || "",
            ]);
            db.exec({
                sql: `INSERT OR IGNORE INTO entries
                        (id, source_key, dict_id, term, simplified,
                         traditional, reading, reading_toneless, definition)
                      SELECT json_extract(value, '$[0]'),
                             json_extract(value, '$[1]'),
                             json_extract(value, '$[2]'),
                             json_extract(value, '$[3]'),
                             json_extract(value, '$[4]'),
                             json_extract(value, '$[5]'),
                             json_extract(value, '$[6]'),
                             json_extract(value, '$[7]'),
                             json_extract(value, '$[8]')
                      FROM json_each(?)`,
                bind: [JSON.stringify(entryValues)],
            });
            const pairs = [];
            for (const row of withKeys) {
                const entryId = row.source_key;
                if (existing.has(Number(entryId))) continue;
                for (const gram of rowNgrams(row)) {
                    pairs.push([gram, row.dict_id, entryId]);
                }
            }
            if (pairs.length) {
                db.exec({
                    sql: `INSERT INTO ngrams (gram, dict_id, entry_id)
                          SELECT json_extract(value, '$[0]'),
                                 json_extract(value, '$[1]'),
                                 json_extract(value, '$[2]')
                          FROM json_each(?)`,
                    bind: [JSON.stringify(pairs)],
                });
            }
        }
        if (withoutKeys.length) {
            // No stable key: use the rowid returned by SQLite and the slower
            // per-row path. Normal imports always have a source key.
            const statement = db.prepare(
                "INSERT OR IGNORE INTO entries (id, source_key, dict_id, term, simplified, traditional, reading, reading_toneless, definition) VALUES (NULL,NULL,?,?,?,?,?,?,?)"
            );
            const gramStatement = db.prepare(
                "INSERT INTO ngrams (gram, dict_id, entry_id) VALUES (?,?,?)"
            );
            try {
                for (const row of withoutKeys) {
                    statement.bind([
                        row.dict_id,
                        row.term,
                        row.simplified || row.term,
                        row.traditional || row.term,
                        row.reading || "",
                        normalize(row.reading_toneless || row.reading || ""),
                        row.definition || "",
                    ]);
                    statement.step();
                    if (db.changes() === 1) {
                        const entryId = Number(
                            db.selectValue("SELECT last_insert_rowid()")
                        );
                        addRowNgrams(row, entryId, gramStatement);
                    }
                    statement.reset();
                }
            } finally {
                statement.finalize();
                gramStatement.finalize();
            }
        }
        db.exec("COMMIT");
    } catch (error) {
        try {
            db.exec("ROLLBACK");
        } catch (rollbackError) {
            // Ignore a rollback on an already-closed transaction.
        }
        throw error;
    }
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
            const fullFingerprint = metaValue("fingerprint");
            const entriesFingerprint = metaValue("entries_fingerprint");
            // A partial build leaves this key behind so a reload can resume.
            const buildFingerprint = metaValue("build_fingerprint") || "";
            const count = rowsFor("SELECT COUNT(*) AS count FROM entries")[0]
                .count;
            const expected = Math.max(
                0,
                Number(message.expectedEntries) || 0
            );
            // Older indexes predate entries_fingerprint. If their row count
            // matches the imported term counts, the entries are reusable.
            const countMatches =
                !entriesFingerprint &&
                expected > 0 &&
                count === expected;
            const entriesReusable =
                entriesFingerprint === message.entriesFingerprint ||
                countMatches;
            const needsBuild = !entriesReusable;
            const needsMigration =
                !needsBuild &&
                (fullFingerprint !== message.fingerprint || !ngramsReady);
            post({
                type: "init",
                id,
                needsBuild,
                needsMigration,
                buildFingerprint,
                entriesFingerprint:
                    entriesFingerprint ||
                    (countMatches ? message.entriesFingerprint : ""),
                ngramsReady,
                count,
            });
            return;
        }
        if (message.type === "beginBuild") {
            // Build the secondary indexes after the bulk load. Maintaining
            // them per inserted row is much slower on a phone.
            dropSearchIndexes();
            dropNgramIndex();
            db.exec("BEGIN");
            db.exec("DELETE FROM ngrams");
            db.exec("DELETE FROM entries");
            db.exec("COMMIT");
            setMeta("build_fingerprint", message.fingerprint || "");
            setMeta("ngrams_ready", "0");
            setMeta("ngram_cursor", "0");
            ngramsReady = false;
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
            setMeta("entries_fingerprint", message.entriesFingerprint || "");
            setMeta("ngram_version", message.version || "");
            const maxRow = rowsFor(
                "SELECT MAX(id) AS maxId FROM entries"
            )[0];
            setMeta("ngram_cursor", String(maxRow?.maxId || 0));
            ensureSearchIndexes();
            ensureNgramIndex();
            db.exec("DELETE FROM meta WHERE key = 'build_fingerprint'");
            setMeta("ngrams_ready", "1");
            ngramsReady = true;
            post({
                type: "ok",
                id,
                count: rowsFor("SELECT COUNT(*) AS count FROM entries")[0]
                    .count,
            });
            return;
        }
        if (message.type === "migrateStart") {
            // Make the existing entries searchable while ngrams backfill.
            ensureSearchIndexes();
            dropNgramIndex();
            const version = String(message.version || "");
            const storedVersion = metaValue("ngram_version");
            let cursor = Number(metaValue("ngram_cursor")) || 0;
            if (
                storedVersion !== version ||
                metaValue("ngrams_ready") === "1"
            ) {
                // A new index version or a completed old ngram set starts over.
                db.exec("DELETE FROM ngrams");
                cursor = 0;
                setMeta("ngram_cursor", "0");
            }
            setMeta("ngram_version", version);
            setMeta("ngrams_ready", "0");
            setMeta(
                "entries_fingerprint",
                message.entriesFingerprint || ""
            );
            ngramsReady = false;
            post({ type: "migrateStart", id, cursor });
            return;
        }
        if (message.type === "migrateChunk") {
            const batchSize = Math.max(
                1000,
                Math.min(20000, Number(message.batchSize) || 5000)
            );
            const cursor = Number(metaValue("ngram_cursor")) || 0;
            const rows = rowsFor(
                `SELECT id, dict_id, term, simplified, traditional
                 FROM entries WHERE id > ? ORDER BY id LIMIT ?`,
                [cursor, batchSize]
            );
            if (!rows.length) {
                setMeta("fingerprint", message.fingerprint);
                setMeta(
                    "entries_fingerprint",
                    message.entriesFingerprint || ""
                );
                setMeta("ngram_version", String(message.version || ""));
                ensureNgramIndex();
                setMeta("ngrams_ready", "1");
                ngramsReady = true;
                post({
                    type: "migrateChunk",
                    id,
                    done: true,
                    cursor,
                    count: rowsFor("SELECT COUNT(*) AS count FROM entries")[0]
                        .count,
                });
                return;
            }
            const statement = db.prepare(
                "INSERT OR IGNORE INTO ngrams (gram, dict_id, entry_id) VALUES (?,?,?)"
            );
            try {
                db.exec("BEGIN");
                for (const row of rows) {
                    addRowNgrams(row, row.id, statement);
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
            const lastId = rows[rows.length - 1].id;
            setMeta("ngram_cursor", String(lastId));
            post({
                type: "migrateChunk",
                id,
                done: false,
                cursor: Number(lastId),
                rows: rows.length,
            });
            return;
        }
        if (message.type === "wildcard") {
            definitionSeq += 1;
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
            // A new headword search supersedes any definition scan.
            definitionSeq += 1;
            const found = searchEntries(db, {
                ...message,
                ngramsReady,
            });
            post({
                type: "search",
                id,
                rows: found.rows,
                timings: found.timings,
                candidates: found.candidates,
            });
            return;
        }
        if (message.type === "searchDefs") {
            const seq = ++definitionSeq;
            const found = await searchChunkedEntries(
                db,
                message,
                "defs",
                () => seq !== definitionSeq
            );
            post({
                type: "searchDefs",
                id,
                rows: found ? found.rows : [],
                timings: found ? found.timings : [],
                aborted: !found,
            });
            return;
        }
        if (message.type === "searchPinyin") {
            const seq = ++definitionSeq;
            const found = await searchChunkedEntries(
                db,
                message,
                "pinyin",
                () => seq !== definitionSeq
            );
            post({
                type: "searchPinyin",
                id,
                rows: found ? found.rows : [],
                timings: found ? found.timings : [],
                aborted: !found,
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
