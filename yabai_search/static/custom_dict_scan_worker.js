// IndexedDB fallback scanner for browsers without OPFS/SQLite support. The
// exact/prefix tiers still come from the main thread's IDB indexes; this worker
// handles the deferred substring and definition tiers without blocking the UI.
const DB_NAME = "yabai-search-custom-dicts";
const DB_VERSION = 3;
const MAX_RESULTS = 50;

let dbPromise = null;
let rowsByDict = new Map();

function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
    return dbPromise;
}

function rowsForDict(dictId) {
    if (rowsByDict.has(dictId)) return Promise.resolve(rowsByDict.get(dictId));
    return openDb().then(
        (db) =>
            new Promise((resolve, reject) => {
                const request = db
                    .transaction("entries", "readonly")
                    .objectStore("entries")
                    .index("dictId")
                    .getAll(dictId);
                request.onsuccess = () => {
                    const rows = request.result || [];
                    rowsByDict.set(dictId, rows);
                    resolve(rows);
                };
                request.onerror = () => reject(request.error);
            })
    );
}

function normalize(value) {
    return String(value || "")
        .normalize("NFKC")
        .toLowerCase()
        .replace(/ü/g, "u")
        .normalize("NFD")
        .replace(/\p{M}/gu, "")
        .replace(/[^\p{Script=Han}a-z0-9]/gu, "");
}

function normalizeQuery(value) {
    return normalize(value);
}

function toneless(value) {
    return normalize(value).replace(/[^a-z0-9]/g, "");
}

async function scan(message) {
    const raw = normalizeQuery(message.query);
    if (!raw) return [];
    const dictIds = message.dictIds || [];
    if (!dictIds.length) return [];
    const han = /\p{Script=Han}/u.test(raw);
    const pinyin = toneless(message.query);
    const rows = [];
    for (const dictId of dictIds) {
        rows.push(...(await rowsForDict(dictId)));
    }
    const order = new Map(dictIds.map((id, index) => [id, index]));
    const matches = new Map();
    for (const row of rows) {
        const term = normalize(row.term);
        const simplified = normalize(row.simplified || row.term);
        const traditional = normalize(row.traditional || row.term);
        const reading = toneless(row.readingToneless || row.reading || "");
        const definition = normalize(row.gloss || "");
        let rank = 0;
        if (han) {
            if (term === raw || simplified === raw || traditional === raw) rank = 1;
            else if (
                term.startsWith(raw) ||
                simplified.startsWith(raw) ||
                traditional.startsWith(raw)
            ) rank = 2;
            else if (
                term.includes(raw) ||
                simplified.includes(raw) ||
                traditional.includes(raw)
            ) rank = 3;
            else if (definition.includes(raw)) rank = 4;
        } else {
            if (reading === pinyin) rank = 1;
            else if (reading.startsWith(pinyin)) rank = 2;
            else if (reading.includes(pinyin) || term.includes(raw)) rank = 3;
            else if (definition.includes(raw)) rank = 4;
        }
        if (!rank) continue;
        const key = `${row.dictId}|${row.term}|${row.reading}`;
        const existing = matches.get(key);
        if (!existing || rank < existing.rank) {
            matches.set(key, {
                dictId: row.dictId,
                term: row.term,
                simplified: row.simplified || row.term,
                traditional: row.traditional || row.term,
                reading: row.reading,
                glosses: row.gloss ? [row.gloss] : [],
                rank,
                dictOrder: order.get(row.dictId) ?? 999,
            });
        }
    }
    return [...matches.values()]
        .sort((a, b) => {
            if (a.rank !== b.rank) return a.rank - b.rank;
            if (a.dictOrder !== b.dictOrder) return a.dictOrder - b.dictOrder;
            return String(a.term).length - String(b.term).length;
        })
        .slice(0, MAX_RESULTS)
        .map(({ rank, dictOrder, ...match }) => match);
}

self.onmessage = async (event) => {
    const message = event.data || {};
    try {
        const matches = await scan(message);
        self.postMessage({ id: message.id, matches });
    } catch (error) {
        self.postMessage({
            id: message.id,
            error: String((error && error.message) || error),
        });
    }
};
