// Browser-side Yomitan dictionary imports.
//
// Imported languages and dictionary rows live in IndexedDB (with a persistent
// storage request), so they survive reloads. The term index is kept in memory
// while the page is open for fast headword/reading searches.

(() => {
    "use strict";

    const LOADED_LATE = Boolean(document.currentScript?.dataset?.late);

    const SCRIPT_VERSION = (() => {
        try {
            const src = document.currentScript?.src;
            return src ? new URL(src).searchParams.get("v") || "" : "";
        } catch (error) {
            return "";
        }
    })();
    const DB_NAME = "yabai-search-custom-dicts";
    const DB_VERSION = 6;
    const OPEN_KEY = "yabai-search-dict-open";
    const HAS_DICTS_KEY = "yabai-search-custom-dicts-has";
    const MAX_SEARCH_RESULTS = 50;

    let dbPromise = null;
    let readyResolve = null;
    const ready = new Promise((resolve) => {
        readyResolve = resolve;
    });

    let languages = [];
    let dictionaries = [];
    let termIndex = null;
    let termIndexByDict = null;
    let termIndexPromise = null;
    let termIndexGeneration = 0;
    let sqliteSearchEnabled = true;
    try {
        sqliteSearchEnabled =
            localStorage.getItem("yabai-search-sqlite-search") !== "false";
    } catch (error) {
        // localStorage unavailable - keep the default.
    }
    let sqliteWorker = null;
    let sqliteWorkerReady = null;
    let sqliteWorkerReadyReject = null;
    let sqliteReady = false;
    let sqliteWorkerFailed = false;
    let sqliteIndexPromise = null;
    let sqlitePreparing = false;
    let sqliteRebuildTimer = null;
    let sqliteRequestId = 0;
    const sqlitePending = new Map();
    let scanWorker = null;
    let scanRequestId = 0;
    const scanPending = new Map();
    // One SQLite search may run while one newer query waits. Superseded
    // queries resolve empty instead of queueing up behind a slow scan.
    const searchQueue = { active: null, pending: null };
    let migrationSeq = 0;
    let importTarget = null;
    let importing = false;
    let importCancelled = false;
    let activeImportId = null;
    const cancelledImports = new Set();

    class ImportCancelled extends Error {}
    const CUSTOM_INDEX_VERSION = 9;
    const BUILD_STATE_KEY = "yabai-search-custom-index-build";

    // Where a partially built SQLite index stopped. IndexedDB keys are stable
    // across reloads, so the build can continue from the last inserted row.
    function loadBuildState() {
        try {
            const saved = JSON.parse(
                localStorage.getItem(BUILD_STATE_KEY) || "null"
            );
            return saved && typeof saved === "object" ? saved : null;
        } catch (error) {
            return null;
        }
    }

    function saveBuildState(state) {
        try {
            localStorage.setItem(BUILD_STATE_KEY, JSON.stringify(state));
        } catch (error) {
            // localStorage unavailable - progress is session-only.
        }
    }

    function clearBuildState() {
        try {
            localStorage.removeItem(BUILD_STATE_KEY);
        } catch (error) {
            // localStorage unavailable - nothing to clear.
        }
    }

    let buildWakeLock = null;

    // Android browsers throttle background tabs. Keep the screen awake while
    // building; failure is harmless and the build still resumes later.
    async function acquireBuildWakeLock() {
        try {
            if (navigator.wakeLock && !buildWakeLock) {
                buildWakeLock = await navigator.wakeLock.request("screen");
            }
        } catch (error) {
            buildWakeLock = null;
        }
    }

    function releaseBuildWakeLock() {
        try {
            buildWakeLock?.release();
        } catch (error) {
            // Already released.
        }
        buildWakeLock = null;
    }
    const customStats = {
        lastSearchMs: 0,
        lastIndexMs: 0,
        lastScanMs: 0,
        lastMatches: 0,
        lastWildcardTotal: 0,
        lastWildcardMatches: 0,
        lastTiers: [],
        lastDefTiers: [],
        lastPinyinTiers: [],
        lastCandidates: 0,
        entryCount: 0,
        lastDefMs: 0,
        lastPinyinMs: 0,
        buildReadMs: 0,
        buildInsertMs: 0,
        buildBatches: 0,
        migrating: false,
        ngramsReady: false,
        lastImport: null,
        lastSource: "",
        sqliteReady: false,
    };
    window.customDictsStats = customStats;

    // Snapshot for the "Download debug log" item. It describes the imported
    // dictionaries and which of them are currently selected, without copying
    // any dictionary content.
    window.customDictionaryDebugInfo = () => {
        const state = window.state;
        return {
            ready: Boolean(termIndex),
            termIndexRows: termIndex ? termIndex.length : 0,
            sqliteSearchEnabled,
            sqliteReady,
            sqliteWorkerFailed,
            sqlitePreparing,
            sqliteIndexPending: Boolean(sqliteIndexPromise),
            ngramsReady: customStats.ngramsReady,
            migrating: customStats.migrating,
            scanWorkerActive: Boolean(scanWorker),
            buildState: loadBuildState(),
            expectedEntryCount: buildableDictionaries().reduce(
                (sum, dictionary) =>
                    sum + (Number(dictionary.termCount) || 0),
                0
            ),
            selectedAnchors: state ? [...state.dicts].sort() : [],
            activeIds: activeCustomDictIds(),
            dictionaries: dictionaries.map((dictionary) => ({
                id: dictionary.id,
                title: dictionary.title,
                anchor: dictionary.anchor,
                languageId: dictionary.languageId,
                termCount: dictionary.termCount,
                indexed: Boolean(dictionary.indexed),
            })),
        };
    };

    // The app asks for imported matches before rendering a search response.
    // The split API lets the UI start this work alongside the server search
    // and merge the matches into whichever official tier has arrived.
    window.customDictionaryResults = async (data, query) => {
        await ready;
        const matches = await searchImported(query);
        await mergeImported(data, matches);
    };
    window.customDictionaryMatches = async (query) => {
        const startedAt = performance.now();
        await ready;
        const dictIds = activeCustomDictIds();
        if (!dictIds.length) return [];
        if (
            sqliteSearchEnabled &&
            !sqliteReady &&
            !sqliteWorkerFailed &&
            (sqlitePreparing || backfillPromise)
        ) {
            // Loading the old full index while the SQLite index is being built
            // is what made large dictionaries crash or take seconds. Show the
            // official tier and let the fast index finish instead.
            customStats.lastSource = "pending-index";
            return [];
        }
        // The old path already handles wildcard and numeric-tone queries.
        if (
            query.includes("*") ||
            /[1-5]/.test(query) ||
            /\s/.test(query.trim())
        ) {
            const result = await searchImported(query);
            customStats.lastSource = "indexeddb";
            return result;
        }
        if (sqliteSearchEnabled && sqliteReady && !sqliteWorkerFailed) {
            try {
                const response = await queueSqliteSearch({
                    type: "search",
                    query,
                    dictIds,
                });
                const result = matchesFromSqliteRows(response.rows || []);
                customStats.lastSearchMs = performance.now() - startedAt;
                customStats.lastMatches = result.length;
                customStats.lastTiers = response.timings || [];
                customStats.lastCandidates = response.candidates || 0;
                customStats.lastSource = "sqlite";
                return result;
            } catch (error) {
                console.error("SQLite custom search failed:", error);
                sqliteReady = false;
                customStats.sqliteReady = false;
            }
        }
        const matchKey = (match) =>
            `${match.dictId}|${match.term}|${match.reading}`;
        const fast = await indexedDbFastMatches(query, dictIds);
        const fastKeys = new Set(fast.map(matchKey));
        const slow = scanWorkerMatches(query, dictIds)
            .then((matches) =>
                matches.filter((match) => !fastKeys.has(matchKey(match)))
            )
            .catch((error) => {
                console.error("IndexedDB scan worker failed:", error);
                return searchImported(query).then((matches) =>
                    matches.filter((match) => !fastKeys.has(matchKey(match)))
                );
            });
        customStats.lastSource = "indexeddb-tiered";
        return { matches: fast, slow };
    };
    // Definition matches run as a separate, abortable worker request so the
    // headword results can render first. A newer search cancels the scan.
    window.customDictionaryDefinitionMatches = async (
        query,
        skipKeys = [],
        limit = MAX_SEARCH_RESULTS
    ) => {
        await ready;
        const dictIds = activeCustomDictIds();
        if (!dictIds.length || !sqliteReady || sqliteWorkerFailed) return [];
        try {
            const startedAt = performance.now();
            const response = await sqliteRequest(
                {
                    type: "searchDefs",
                    query,
                    dictIds,
                    skipKeys: [...skipKeys],
                    limit,
                },
                0
            );
            if (response.aborted) return [];
            customStats.lastDefMs = performance.now() - startedAt;
            customStats.lastDefTiers = response.timings || [];
            return matchesFromSqliteRows(response.rows || []);
        } catch (error) {
            console.error("SQLite definition search failed:", error);
            return [];
        }
    };
    // Pinyin/Latin substring matches use the same chunked, abortable scan as
    // definitions so they cannot block the headword results.
    window.customDictionaryPinyinMatches = async (
        query,
        skipKeys = [],
        limit = MAX_SEARCH_RESULTS
    ) => {
        await ready;
        const dictIds = activeCustomDictIds();
        if (!dictIds.length || !sqliteReady || sqliteWorkerFailed) return [];
        try {
            const startedAt = performance.now();
            const response = await sqliteRequest(
                {
                    type: "searchPinyin",
                    query,
                    dictIds,
                    skipKeys: [...skipKeys],
                    limit,
                },
                0
            );
            if (response.aborted) return [];
            customStats.lastPinyinMs = performance.now() - startedAt;
            customStats.lastPinyinTiers = response.timings || [];
            return matchesFromSqliteRows(response.rows || []);
        } catch (error) {
            console.error("SQLite pinyin search failed:", error);
            return [];
        }
    };
    window.mergeCustomMatches = async (data, matches, maxResults) => {
        await ready;
        if (matches && matches.length) {
            await mergeImported(data, matches, maxResults);
        }
    };

    // --- IndexedDB ---------------------------------------------------------

    function openDb() {
        if (dbPromise) return dbPromise;
        dbPromise = new Promise((resolve, reject) => {
            const request = indexedDB.open(DB_NAME, DB_VERSION);
            request.onupgradeneeded = () => {
                const db = request.result;
                if (!db.objectStoreNames.contains("languages")) {
                    db.createObjectStore("languages", { keyPath: "id" });
                }
                if (!db.objectStoreNames.contains("dictionaries")) {
                    db.createObjectStore("dictionaries", { keyPath: "id" });
                }
                if (!db.objectStoreNames.contains("imports")) {
                    db.createObjectStore("imports", { keyPath: "id" });
                }
                if (!db.objectStoreNames.contains("import-files")) {
                    db.createObjectStore("import-files", { keyPath: "id" });
                }
                let entries;
                if (!db.objectStoreNames.contains("entries")) {
                    entries = db.createObjectStore("entries", {
                        keyPath: "key",
                        autoIncrement: true,
                    });
                } else {
                    entries = request.transaction.objectStore("entries");
                }
                for (const [name, keyPath] of [
                    ["dictId", "dictId"],
                    ["term", "term"],
                    ["simplified", "simplified"],
                    ["traditional", "traditional"],
                    ["readingToneless", "readingToneless"],
                ]) {
                    if (!entries.indexNames.contains(name)) {
                        entries.createIndex(name, keyPath, { unique: false });
                    }
                }
                if (!db.objectStoreNames.contains("grams")) {
                    // dictId is the key prefix, so a separate index is not
                    // needed for lookups or deletion.
                    db.createObjectStore("grams", {
                        keyPath: ["dictId", "size", "gram"],
                    });
                } else {
                    const store =
                        request.transaction.objectStore("grams");
                    if (store.indexNames.contains("dictId")) {
                        store.deleteIndex("dictId");
                    }
                }
                if (!db.objectStoreNames.contains("gramBuckets")) {
                    // One record per (dict, size, first character), holding a
                    // sorted gram array. New imports use this instead of one
                    // record per gram.
                    db.createObjectStore("gramBuckets", {
                        keyPath: ["dictId", "size", "bucket"],
                    });
                }
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
        return dbPromise;
    }

    async function dbAll(storeName) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const request = db
                .transaction(storeName, "readonly")
                .objectStore(storeName)
                .getAll();
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function dbGet(storeName, key) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const request = db
                .transaction(storeName, "readonly")
                .objectStore(storeName)
                .get(key);
            request.onsuccess = () => resolve(request.result || null);
            request.onerror = () => reject(request.error);
        });
    }

    async function dbAllByIndex(storeName, indexName, key) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const request = db
                .transaction(storeName, "readonly")
                .objectStore(storeName)
                .index(indexName)
                .getAll(key);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    // Read one dictionary's entries in batches. Loading a whole large import
    // with getAll can freeze or kill a phone, and it delays the first progress
    // message until every row is in memory. The dictId index gives us the
    // first and last primary key, so the rest can be paged from IndexedDB.
    async function dictKeyBounds(dictId) {
        const db = await openDb();
        // Use one transaction per cursor: an IndexedDB transaction can close
        // as soon as it has no pending request, so it cannot be reused after
        // awaiting the first edge.
        const edge = (direction) =>
            new Promise((resolve, reject) => {
                const index = db
                    .transaction("entries", "readonly")
                    .objectStore("entries")
                    .index("dictId");
                const request = index.openCursor(
                    IDBKeyRange.only(dictId),
                    direction
                );
                request.onerror = () => reject(request.error);
                request.onsuccess = () => {
                    const cursor = request.result;
                    resolve(cursor ? cursor.primaryKey : null);
                };
            });
        const first = await edge("next");
        if (first === null) return null;
        const last = await edge("prev");
        return { first, last };
    }

    async function forEachEntryBatch(
        dictId,
        batchSize,
        callback,
        startAfter = null
    ) {
        const bounds = await dictKeyBounds(dictId);
        if (!bounds) return;
        // Resume inside this dictionary's key range. A key outside the range
        // means the saved position belongs to another dictionary.
        let after = null;
        if (startAfter !== null && startAfter !== undefined) {
            if (startAfter >= bounds.last) return; // already at the end
            if (startAfter >= bounds.first) after = startAfter;
        }
        while (true) {
            const range =
                after === null
                    ? IDBKeyRange.bound(bounds.first, bounds.last)
                    : IDBKeyRange.bound(after, bounds.last, true, false);
            const db = await openDb();
            const readStarted = performance.now();
            const rows = await new Promise((resolve, reject) => {
                const request = db
                    .transaction("entries", "readonly")
                    .objectStore("entries")
                    .getAll(range, batchSize);
                request.onerror = () => reject(request.error);
                request.onsuccess = () => resolve(request.result || []);
            });
            const readMs = performance.now() - readStarted;
            const lastKey = rows.length ? rows[rows.length - 1].key : null;
            const matching = rows.filter((row) => row.dictId === dictId);
            if (matching.length) {
                await callback(matching, lastKey, readMs);
            }
            if (rows.length < batchSize) return;
            if (lastKey === null || lastKey === after) return;
            after = lastKey;
        }
    }

    function invalidateTermIndex() {
        termIndexGeneration += 1;
        termIndex = null;
        termIndexByDict = null;
        termIndexPromise = null;
    }

    function setupSqliteWorker() {
        if (sqliteWorker || sqliteWorkerFailed) return;
        if (typeof Worker !== "function") {
            sqliteWorkerFailed = true;
            return;
        }
        const suffix = SCRIPT_VERSION
            ? `?v=${encodeURIComponent(SCRIPT_VERSION)}`
            : "";
        sqliteWorker = new Worker(
            `/static/custom_dict_worker.js${suffix}`,
            { type: "module" }
        );
        sqliteWorkerReady = new Promise((resolve, reject) => {
            sqliteWorkerReadyReject = reject;
            sqliteWorker.addEventListener("message", (event) => {
                const message = event.data || {};
                if (message.type === "ready") {
                    resolve(true);
                    return;
                }
                if (message.type === "error" && !message.id) {
                    sqliteWorkerFailed = true;
                    reject(new Error(message.error || "SQLite worker error"));
                    return;
                }
                const pending = sqlitePending.get(message.id);
                if (!pending) return;
                sqlitePending.delete(message.id);
                if (message.type === "error") {
                    pending.reject(new Error(message.error || "SQLite error"));
                } else {
                    pending.resolve(message);
                }
            });
            sqliteWorker.addEventListener("error", (event) => {
                sqliteWorkerFailed = true;
                sqliteReady = false;
                const error = new Error(event.message || "SQLite worker failed");
                reject(error);
                for (const pending of sqlitePending.values()) {
                    pending.reject(error);
                }
                sqlitePending.clear();
            });
        });
        sqliteWorkerReady.catch(() => {});
    }

    async function sqliteRequest(message, timeoutMs = 30000) {
        setupSqliteWorker();
        if (sqliteWorkerFailed || !sqliteWorker) {
            throw new Error("SQLite search is unavailable");
        }
        // A worker that never sends its "ready" message (for example when OPFS
        // is unavailable) must not hang the progress message forever. Creating
        // composite indexes on an existing index can take a while on a phone.
        await new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                sqliteWorkerFailed = true;
                if (sqliteWorker) {
                    sqliteWorker.terminate();
                    sqliteWorker = null;
                }
                setBackfillStatus("");
                reject(new Error("SQLite worker startup timed out"));
            }, 120000);
            sqliteWorkerReady.then(
                () => {
                    clearTimeout(timer);
                    resolve();
                },
                (error) => {
                    clearTimeout(timer);
                    reject(error);
                }
            );
        });
        return new Promise((resolve, reject) => {
            const id = ++sqliteRequestId;
            // timeoutMs = 0 means no timeout. Index builds can legitimately
            // take minutes on a phone and must not be aborted halfway.
            let timer = null;
            if (timeoutMs > 0) {
                timer = setTimeout(() => {
                    sqlitePending.delete(id);
                    reject(new Error("SQLite request timed out"));
                }, timeoutMs);
            }
            sqlitePending.set(id, {
                resolve: (value) => {
                    clearTimeout(timer);
                    resolve(value);
                },
                reject: (error) => {
                    clearTimeout(timer);
                    reject(error);
                },
            });
            sqliteWorker.postMessage({ ...message, id });
        });
    }

    function startSqliteSearchJob(job) {
        searchQueue.active = job;
        sqliteRequest(job.message)
            .then(job.resolve, job.reject)
            .finally(() => {
                searchQueue.active = null;
                const next = searchQueue.pending;
                searchQueue.pending = null;
                if (next) startSqliteSearchJob(next);
            });
    }

    function queueSqliteSearch(message) {
        return new Promise((resolve, reject) => {
            const job = { message, resolve, reject };
            if (searchQueue.active) {
                if (searchQueue.pending) {
                    searchQueue.pending.resolve({
                        rows: [],
                        timings: [],
                        candidates: 0,
                        superseded: true,
                    });
                }
                searchQueue.pending = job;
                return;
            }
            startSqliteSearchJob(job);
        });
    }

    function buildableDictionaries() {
        return dictionaries.filter(
            (dictionary) => !dictionary.importing && !dictionary.incomplete
        );
    }

    function customDictionaryEntriesFingerprint() {
        return JSON.stringify(
            buildableDictionaries().map((dictionary) => [
                dictionary.id,
                dictionary.termCount,
                dictionary.createdAt,
                Boolean(dictionary.converted),
            ])
        );
    }

    function customDictionaryFingerprint() {
        return JSON.stringify([
            CUSTOM_INDEX_VERSION,
            ...JSON.parse(customDictionaryEntriesFingerprint()),
        ]);
    }

    async function buildSqliteIndex(
        fingerprint,
        entriesFingerprint,
        resumeState = null
    ) {
        // Each completed dictionary records how many entry rows it stored at
        // import time, so the progress text can show a real total: 40k/360k.
        const sourceDictionaries = buildableDictionaries();
        const totalRows = sourceDictionaries.reduce(
            (sum, dictionary) => sum + (Number(dictionary.termCount) || 0),
            0
        );
        let startDictIndex = 0;
        let startAfter = null;
        let added = 0;
        let resuming = false;
        if (resumeState && resumeState.fingerprint === fingerprint) {
            resuming = true;
            startDictIndex = Math.max(0, Number(resumeState.dictIndex) || 0);
            startAfter =
                resumeState.afterKey === undefined
                    ? null
                    : resumeState.afterKey;
            added = Number(resumeState.added) || 0;
        }
        customStats.buildReadMs = 0;
        customStats.buildInsertMs = 0;
        customStats.buildBatches = 0;
        acquireBuildWakeLock();
        setBackfillStatus(
            `Building custom search index: ` +
                `${formatCount(added)}/${formatCount(totalRows)}` +
                (resuming ? " (resuming)" : "")
        );
        try {
            if (!resuming) {
                // Clear the old index only when this is a fresh build.
                await sqliteRequest(
                    {
                        type: "beginBuild",
                        fingerprint,
                        entriesFingerprint,
                        version: CUSTOM_INDEX_VERSION,
                    },
                    0
                );
                startDictIndex = 0;
                startAfter = null;
                added = 0;
            }
            saveBuildState({
                fingerprint,
                dictIndex: startDictIndex,
                afterKey: startAfter,
                added,
            });
            for (
                let dictIndex = startDictIndex;
                dictIndex < sourceDictionaries.length;
                dictIndex += 1
            ) {
                const dictionary = sourceDictionaries[dictIndex];
                const after = dictIndex === startDictIndex ? startAfter : null;
                // Stream this dictionary in small batches so progress appears
                // early and a huge import does not have to fit in memory.
                await forEachEntryBatch(
                    dictionary.id,
                    5000,
                    async (chunk, lastKey, readMs) => {
                        const insertStarted = performance.now();
                        await sqliteRequest(
                            {
                                type: "addRows",
                                rows: chunk.map((row) => ({
                                    // IndexedDB's single autoIncrement store
                                    // makes this key unique across dictionaries.
                                    // It also makes resumed inserts idempotent.
                                    source_key: row.key,
                                    dict_id: row.dictId,
                                    term: row.term,
                                    simplified: row.simplified || row.term,
                                    traditional: row.traditional || row.term,
                                    reading: row.reading || "",
                                    reading_toneless:
                                        row.readingToneless || row.reading || "",
                                    definition: row.gloss || "",
                                })),
                            },
                            0
                        );
                        customStats.buildReadMs += readMs || 0;
                        customStats.buildInsertMs +=
                            performance.now() - insertStarted;
                        customStats.buildBatches += 1;
                        added += chunk.length;
                        saveBuildState({
                            fingerprint,
                            dictIndex,
                            afterKey: lastKey,
                            added,
                        });
                        const total = Math.max(totalRows, added);
                        setBackfillStatus(
                            `Building custom search index: ` +
                                `${formatCount(added)}/${formatCount(total)}`
                        );
                        await new Promise((resolve) => setTimeout(resolve, 0));
                    },
                    after
                );
                // The whole dictionary is committed; the next one starts fresh.
                saveBuildState({
                    fingerprint,
                    dictIndex: dictIndex + 1,
                    afterKey: null,
                    added,
                });
            }
            setBackfillStatus(
                `Building custom search index: ` +
                    `${formatCount(added)}/${formatCount(Math.max(totalRows, added))} ` +
                    `(finalizing)`
            );
            const ended = await sqliteRequest(
                {
                    type: "endBuild",
                    fingerprint,
                    entriesFingerprint,
                    version: CUSTOM_INDEX_VERSION,
                },
                0
            );
            customStats.entryCount = ended.count || 0;
            if (customStats.entryCount !== totalRows) {
                console.error(
                    "Custom index row count mismatch: " +
                        `${customStats.entryCount} stored, ` +
                        `${totalRows} expected`
                );
            }
            clearBuildState();
            sqliteReady = true;
        } finally {
            // Never leave "Building..." on screen if the worker throws.
            setBackfillStatus("");
            releaseBuildWakeLock();
        }
    }

    // Backfill ngrams from the existing entries table. This is resumable and
    // does not re-read IndexedDB. Searches use the LIKE path until it finishes.
    async function migrateSqliteIndex(
        fingerprint,
        entriesFingerprint,
        expectedCount
    ) {
        const token = ++migrationSeq;
        customStats.migrating = true;
        acquireBuildWakeLock();
        const total = Number(expectedCount) || customStats.entryCount || 0;
        setBackfillStatus(
            `Upgrading custom search index: ` +
                `0/${formatCount(total)}`
        );
        try {
            const started = await sqliteRequest(
                {
                    type: "migrateStart",
                    fingerprint,
                    entriesFingerprint,
                    version: CUSTOM_INDEX_VERSION,
                },
                0
            );
            if (token !== migrationSeq) return;
            let cursor = Number(started.cursor) || 0;
            setBackfillStatus(
                `Upgrading custom search index: ` +
                    `${formatCount(cursor)}/${formatCount(total)}`
            );
            while (true) {
                if (token !== migrationSeq) return;
                const chunk = await sqliteRequest(
                    {
                        type: "migrateChunk",
                        fingerprint,
                        entriesFingerprint,
                        version: CUSTOM_INDEX_VERSION,
                        batchSize: 5000,
                    },
                    0
                );
                if (token !== migrationSeq) return;
                if (chunk.done) {
                    customStats.entryCount =
                        chunk.count || customStats.entryCount;
                    break;
                }
                cursor = Number(chunk.cursor) || cursor;
                setBackfillStatus(
                    `Upgrading custom search index: ` +
                        `${formatCount(cursor)}/${formatCount(total)}`
                );
                await new Promise((resolve) => setTimeout(resolve, 0));
            }
            customStats.ngramsReady = true;
        } catch (error) {
            // Keep the LIKE path working if the migration cannot finish.
            console.error("Custom index migration failed:", error);
        } finally {
            if (token === migrationSeq) {
                customStats.migrating = false;
                setBackfillStatus("");
                releaseBuildWakeLock();
            }
        }
    }

    function matchesFromIndexedDbRows(rows) {
        const groups = new Map();
        for (const row of rows) {
            const key = `${row.dictId}|${String(row.term).toLowerCase()}|${String(
                row.reading
            ).toLowerCase()}`;
            let group = groups.get(key);
            if (!group) {
                group = {
                    dictId: row.dictId,
                    term: row.term,
                    simplified: row.simplified || row.term,
                    traditional: row.traditional || row.term,
                    reading: row.reading,
                    glosses: [],
                };
                groups.set(key, group);
            }
            if (row.gloss && !group.glosses.includes(row.gloss)) {
                group.glosses.push(row.gloss);
            }
        }
        return [...groups.values()].slice(0, MAX_SEARCH_RESULTS);
    }

    async function indexedDbWildcardPage(offset, limit, dictIds) {
        const rows = [];
        for (const dictId of dictIds) {
            rows.push(...(await dbAllByIndex("entries", "dictId", dictId)));
        }
        rows.sort((a, b) => {
            const term = String(a.term).localeCompare(String(b.term));
            if (term) return term;
            const traditional = String(a.traditional || "").localeCompare(
                String(b.traditional || "")
            );
            if (traditional) return traditional;
            return String(a.reading || "").localeCompare(
                String(b.reading || "")
            );
        });
        const total = rows.length;
        const page = limit ? rows.slice(offset, offset + limit) : [];
        return { total, matches: matchesFromIndexedDbRows(page) };
    }

    // Wait for a pending SQLite build instead of returning no matches. The
    // official "*" results are already on screen while this runs, so the wait
    // only delays the imported-dictionary tier. The timeout keeps a broken
    // index from leaving the search promise pending forever.
    async function waitForSqliteSearch(timeoutMs = 120000) {
        const startedAt = performance.now();
        while (
            !sqliteReady &&
            !sqliteWorkerFailed &&
            (sqlitePreparing || sqliteIndexPromise || backfillPromise)
        ) {
            if (performance.now() - startedAt >= timeoutMs) break;
            await new Promise((resolve) => window.setTimeout(resolve, 250));
        }
        return sqliteReady;
    }

    window.customDictionaryWildcardPage = async (offset, limit) => {
        await ready;
        const dictIds = activeCustomDictIds();
        if (!dictIds.length) return { total: 0, matches: [] };
        if (
            sqliteSearchEnabled &&
            !sqliteReady &&
            !sqliteWorkerFailed &&
            (sqlitePreparing || sqliteIndexPromise || backfillPromise)
        ) {
            customStats.lastSource = "waiting-index";
            await waitForSqliteSearch();
        }
        if (
            !sqliteReady &&
            !sqliteWorkerFailed &&
            (sqlitePreparing || sqliteIndexPromise || backfillPromise)
        ) {
            customStats.lastSource = "pending-index";
            return { total: 0, matches: [] };
        }
        if (sqliteSearchEnabled && sqliteReady && !sqliteWorkerFailed) {
            try {
                const response = await sqliteRequest({
                    type: "wildcard",
                    dictIds,
                    offset,
                    limit,
                });
                customStats.lastSource = "sqlite-wildcard";
                const page = {
                    total: response.total || 0,
                    matches: matchesFromSqliteRows(response.rows || []),
                };
                customStats.lastWildcardTotal = page.total;
                customStats.lastWildcardMatches = page.matches.length;
                return page;
            } catch (error) {
                console.error("SQLite wildcard search failed:", error);
            }
        }
        customStats.lastSource = "indexeddb-wildcard";
        try {
            const page = await indexedDbWildcardPage(offset, limit, dictIds);
            customStats.lastWildcardTotal = page.total;
            customStats.lastWildcardMatches = page.matches.length;
            return page;
        } catch (error) {
            console.error("IndexedDB wildcard paging failed:", error);
            return { total: 0, matches: [] };
        }
    };

    async function initializeSqliteSearch() {
        if (!sqliteSearchEnabled || sqliteWorkerFailed) return false;
        if (sqliteIndexPromise) return sqliteIndexPromise;
        sqliteIndexPromise = (async () => {
            try {
                migrationSeq += 1; // cancel any older migration
                setBackfillStatus("Preparing custom search index...");
                const fingerprint = customDictionaryFingerprint();
                const entriesFingerprint =
                    customDictionaryEntriesFingerprint();
                const expectedEntries = dictionaries.reduce(
                    (sum, dictionary) =>
                        sum + (Number(dictionary.termCount) || 0),
                    0
                );
                const init = await sqliteRequest({
                    type: "init",
                    fingerprint,
                    entriesFingerprint,
                    expectedEntries,
                });
                customStats.entryCount = init.count || 0;
                customStats.ngramsReady = Boolean(init.ngramsReady);
                if (
                    !init.needsBuild &&
                    !init.needsMigration &&
                    init.count !== expectedEntries
                ) {
                    console.error(
                        "Custom index row count mismatch: " +
                            `${init.count} stored, ${expectedEntries} expected`
                    );
                }
                if (init.needsBuild) {
                    // Resume only when the saved position belongs to this exact
                    // fingerprint and the worker still holds that partial build.
                    const saved = loadBuildState();
                    const resume =
                        saved &&
                        saved.fingerprint === fingerprint &&
                        init.buildFingerprint === fingerprint
                            ? saved
                            : null;
                    await buildSqliteIndex(
                        fingerprint,
                        entriesFingerprint,
                        resume
                    );
                    customStats.ngramsReady = true;
                    sqliteReady = true;
                    customStats.sqliteReady = true;
                } else {
                    clearBuildState();
                    // Entries are reusable. Make them searchable immediately;
                    // the LIKE path is used until the ngram backfill finishes.
                    sqliteReady = true;
                    customStats.sqliteReady = true;
                    if (init.needsMigration) {
                        customStats.ngramsReady = false;
                        migrateSqliteIndex(
                            fingerprint,
                            entriesFingerprint,
                            init.count
                        ).catch((error) =>
                            console.error(
                                "Custom index migration failed:",
                                error
                            )
                        );
                    } else {
                        customStats.ngramsReady = Boolean(
                            init.ngramsReady
                        );
                        setBackfillStatus("");
                    }
                }
                return true;
            } catch (error) {
                sqliteWorkerFailed = true;
                sqliteReady = false;
                customStats.sqliteReady = false;
                setBackfillStatus("");
                console.error("SQLite custom search unavailable:", error);
                return false;
            } finally {
                sqliteIndexPromise = null;
            }
        })();
        return sqliteIndexPromise;
    }

    function invalidateSqliteSearch() {
        migrationSeq += 1;
        customStats.migrating = false;
        customStats.ngramsReady = false;
        sqliteReady = false;
        customStats.sqliteReady = false;
        setBackfillStatus("");
        releaseBuildWakeLock();
    }

    function scheduleSqliteRebuild(delay = 1200) {
        if (!sqliteSearchEnabled) return;
        sqlitePreparing = true;
        if (sqliteRebuildTimer) clearTimeout(sqliteRebuildTimer);
        sqliteRebuildTimer = setTimeout(() => {
            sqliteRebuildTimer = null;
            initializeSqliteSearch()
                .catch(() => {})
                .finally(() => {
                    sqlitePreparing = false;
                });
        }, delay);
    }

    function activeCustomDictIds() {
        const state = window.state;
        if (!state) return [];
        return dictionaries
            .filter(
                (dictionary) =>
                    !dictionary.importing &&
                    !dictionary.incomplete &&
                    state.dicts.has(dictionary.anchor)
            )
            .map((dictionary) => dictionary.id);
    }

    function normalizeFallbackQuery(text) {
        return String(text || "")
            .normalize("NFKC")
            .toLowerCase()
            .replace(/ü/g, "u")
            .normalize("NFD")
            .replace(/\p{M}/gu, "")
            .replace(/[^\p{Script=Han}a-z0-9\s]/gu, "")
            .trim();
    }

    function indexedDbIndexRows(indexName, range, dictIds, limit) {
        return openDb().then(
            (db) =>
                new Promise((resolve, reject) => {
                    const request = db
                        .transaction("entries", "readonly")
                        .objectStore("entries")
                        .index(indexName)
                        .openCursor(range);
                    const rows = [];
                    const seen = new Set();
                    request.onerror = () => reject(request.error);
                    request.onsuccess = () => {
                        const cursor = request.result;
                        if (!cursor || rows.length >= limit) {
                            resolve(rows);
                            return;
                        }
                        const row = cursor.value;
                        if (
                            dictIds.has(row.dictId) &&
                            !seen.has(row.key)
                        ) {
                            seen.add(row.key);
                            rows.push(row);
                        }
                        cursor.continue();
                    };
                })
        );
    }

    async function indexedDbFastMatches(query, dictIds) {
        const raw = normalizeFallbackQuery(query);
        if (!raw) return [];
        const han = /\p{Script=Han}/u.test(raw);
        const value = han
            ? raw
            : window.toToneless?.(query) || raw;
        const ids = new Set(dictIds);
        const fields = han
            ? ["simplified", "traditional", "term"]
            : ["readingToneless", "term"];
        const matches = new Map();
        const add = (rows, rank) => {
            for (const row of rows) {
                const key = row.key;
                const existing = matches.get(key);
                if (!existing || rank < existing.rank) {
                    matches.set(key, { row, rank });
                }
            }
        };
        try {
            for (const field of fields) {
                add(
                    await indexedDbIndexRows(
                        field,
                        IDBKeyRange.only(value),
                        ids,
                        MAX_SEARCH_RESULTS
                    ),
                    1
                );
                if (value) {
                    add(
                        await indexedDbIndexRows(
                            field,
                            IDBKeyRange.bound(value, value + "\uffff"),
                            ids,
                            MAX_SEARCH_RESULTS
                        ),
                        2
                    );
                }
            }
        } catch (error) {
            return [];
        }
        const groups = new Map();
        const ordered = [...matches.values()].sort(
            (a, b) => a.rank - b.rank
        );
        for (const { row } of ordered) {
            const key = `${row.dictId}|${String(row.term).toLowerCase()}|${String(
                row.reading
            ).toLowerCase()}`;
            let group = groups.get(key);
            if (!group) {
                group = {
                    dictId: row.dictId,
                    term: row.term,
                    simplified: row.simplified || row.term,
                    traditional: row.traditional || row.term,
                    reading: row.reading,
                    glosses: [],
                };
                groups.set(key, group);
            }
            if (row.gloss && !group.glosses.includes(row.gloss)) {
                group.glosses.push(row.gloss);
            }
        }
        return [...groups.values()].slice(0, MAX_SEARCH_RESULTS);
    }

    function resetScanWorker() {
        if (scanWorker) {
            scanWorker.terminate();
            scanWorker = null;
        }
        for (const pending of scanPending.values()) {
            pending.reject(new Error("IndexedDB scan worker reset"));
        }
        scanPending.clear();
    }

    function scanWorkerMatches(query, dictIds) {
        if (typeof Worker !== "function") {
            return Promise.reject(new Error("Worker unavailable"));
        }
        if (!scanWorker) {
            const suffix = SCRIPT_VERSION
                ? `?v=${encodeURIComponent(SCRIPT_VERSION)}`
                : "";
            scanWorker = new Worker(
                `/static/custom_dict_scan_worker.js${suffix}`
            );
            scanWorker.onmessage = (event) => {
                const message = event.data || {};
                const pending = scanPending.get(message.id);
                if (!pending) return;
                scanPending.delete(message.id);
                if (message.error) {
                    pending.reject(new Error(message.error));
                } else {
                    pending.resolve(message.matches || []);
                }
            };
            scanWorker.onerror = (event) => {
                const error = new Error(
                    event.message || "IndexedDB scan worker failed"
                );
                for (const pending of scanPending.values()) {
                    pending.reject(error);
                }
                scanPending.clear();
                scanWorker = null;
            };
        }
        return new Promise((resolve, reject) => {
            const id = ++scanRequestId;
            scanPending.set(id, { resolve, reject });
            scanWorker.postMessage({ id, query, dictIds });
        });
    }

    function matchesFromSqliteRows(rows) {
        const groups = new Map();
        for (const row of rows) {
            const key = `${row.dictId}|${String(row.term).toLowerCase()}|${String(
                row.reading
            ).toLowerCase()}`;
            let group = groups.get(key);
            if (!group) {
                group = {
                    dictId: row.dictId,
                    term: row.term,
                    simplified: row.simplified,
                    traditional: row.traditional,
                    reading: row.reading,
                    glosses: [],
                };
                groups.set(key, group);
            }
            if (row.definition && !group.glosses.includes(row.definition)) {
                group.glosses.push(row.definition);
            }
        }
        return [...groups.values()].slice(0, MAX_SEARCH_RESULTS);
    }

    async function dbPut(storeName, value) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readwrite");
            tx.objectStore(storeName).put(value);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    async function dbDelete(storeName, key) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readwrite");
            tx.objectStore(storeName).delete(key);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    async function addTermRows(rows, onTransaction = null) {
        const db = await openDb();
        const storeNames = onTransaction
            ? ["entries", "dictionaries", "imports"]
            : "entries";
        return new Promise((resolve, reject) => {
            let tx;
            try {
                tx = db.transaction(storeNames, "readwrite", {
                    durability: "relaxed",
                });
            } catch (error) {
                tx = db.transaction(storeNames, "readwrite");
            }
            const store = tx.objectStore("entries");
            for (const row of rows) store.add(row);
            if (onTransaction) onTransaction(tx);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    async function putTermRows(rows) {
        const db = await openDb();
        const chunkSize = 5000;
        for (let offset = 0; offset < rows.length; offset += chunkSize) {
            const chunk = rows.slice(offset, offset + chunkSize);
            await new Promise((resolve, reject) => {
                const tx = db.transaction("entries", "readwrite");
                const store = tx.objectStore("entries");
                for (const row of chunk) store.put(row);
                tx.oncomplete = () => resolve();
                tx.onerror = () => reject(tx.error);
            });
        }
    }

    function tonelessReading(text) {
        if (typeof window.toToneless === "function") {
            return window.toToneless(text || "");
        }
        return String(text || "")
            .toLowerCase()
            .replace(/[^a-zü]/g, "");
    }

    // Store the script variants with each imported row so searches do not have
    // to make a conversion request for every custom-only headword.
    async function convertRowsInPlace(rows, shouldStop = null) {
        const terms = [...new Set(rows.map((row) => row.term).filter(Boolean))];
        const converted = new Map();
        for (let offset = 0; offset < terms.length; offset += 200) {
            if (shouldStop && shouldStop()) {
                throw new ImportCancelled("Import cancelled");
            }
            const chunk = terms.slice(offset, offset + 200);
            const results = await convertScripts(chunk);
            chunk.forEach((term, index) => {
                converted.set(term, results[index] || {});
            });
        }
        for (const row of rows) {
            const values = converted.get(row.term) || {};
            row.simplified = values.simplified || row.term;
            row.traditional = values.traditional || row.term;
            row.readingToneless = tonelessReading(row.reading);
        }
    }

    function writeBucketChunk(db, records) {
        return new Promise((resolve, reject) => {
            let tx;
            try {
                tx = db.transaction("gramBuckets", "readwrite", {
                    durability: "relaxed",
                });
            } catch (error) {
                tx = db.transaction("gramBuckets", "readwrite");
            }
            const store = tx.objectStore("gramBuckets");
            for (const record of records) store.put(record);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    async function writeGramBuckets(
        dictId,
        size,
        values,
        total,
        onProgress = null,
        shouldStop = null
    ) {
        const db = await openDb();
        const records = [];
        let bucket = null;
        let bucketGrams = [];
        let processed = 0;
        const flushBucket = () => {
            if (!bucketGrams.length) return;
            bucketGrams.sort();
            records.push({ dictId, size, bucket, grams: bucketGrams });
            bucketGrams = [];
        };
        const flushRecords = async () => {
            if (!records.length) return;
            await writeBucketChunk(db, records.splice(0));
        };
        for (const gram of values) {
            if (shouldStop && shouldStop()) {
                throw new ImportCancelled("Import cancelled");
            }
            const first = [...gram][0];
            if (bucket === null) {
                bucket = first;
            } else if (first !== bucket) {
                flushBucket();
                bucket = first;
            }
            bucketGrams.push(gram);
            processed += 1;
            if (records.length >= 20) {
                await flushRecords();
            }
            if (onProgress && processed % 25000 === 0) {
                onProgress(processed, total);
            }
        }
        flushBucket();
        await flushRecords();
        if (onProgress) onProgress(processed, total);
    }

    async function addGramRows(
        dictId,
        size,
        grams,
        onProgress = null,
        shouldStop = null
    ) {
        const values = Array.isArray(grams) ? grams : [...grams];
        if (!values.length) {
            if (onProgress) onProgress(0, 0);
            return;
        }
        await writeGramBuckets(
            dictId,
            size,
            values,
            values.length,
            onProgress,
            shouldStop
        );
    }

    async function addNumericGramRows(
        dictId,
        size,
        grams,
        onProgress = null,
        shouldStop = null
    ) {
        if (!grams.length) {
            if (onProgress) onProgress(0, 0);
            return;
        }
        function* strings() {
            for (const value of grams) yield unpackBigram(value);
        }
        await writeGramBuckets(
            dictId,
            size,
            strings(),
            grams.length,
            onProgress,
            shouldStop
        );
    }

    async function deleteDictionaryData(dictId) {
        // Remove the metadata first so a reload cannot bring the dictionary
        // back while its potentially large entry/index stores are cleaned up.
        await dbDelete("dictionaries", dictId);
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(
                ["entries", "grams", "gramBuckets"],
                "readwrite"
            );
            const entries = tx.objectStore("entries");
            const cursor = entries
                .index("dictId")
                .openKeyCursor(IDBKeyRange.only(dictId));
            cursor.onsuccess = () => {
                const current = cursor.result;
                if (!current) return;
                entries.delete(current.primaryKey);
                current.continue();
            };
            // Both gram stores start with dictId in the key. An array upper
            // bound sorts after every string size, covering bi and tri.
            const range = IDBKeyRange.bound(
                [dictId],
                [dictId, []],
                false,
                true
            );
            tx.objectStore("grams").delete(range);
            tx.objectStore("gramBuckets").delete(range);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    }

    // --- ZIP reader --------------------------------------------------------

    function zipEntries(buffer) {
        const view = new DataView(buffer);
        const bytes = new Uint8Array(buffer);
        let eocd = -1;
        const stop = Math.max(0, buffer.byteLength - 22 - 65535);
        for (let i = buffer.byteLength - 22; i >= stop; i -= 1) {
            if (view.getUint32(i, true) === 0x06054b50) {
                eocd = i;
                break;
            }
        }
        if (eocd < 0) throw new Error("Not a ZIP file.");
        const count = view.getUint16(eocd + 10, true);
        let offset = view.getUint32(eocd + 16, true);
        const decoder = new TextDecoder();
        const files = new Map();
        for (let i = 0; i < count; i += 1) {
            if (view.getUint32(offset, true) !== 0x02014b50) break;
            const method = view.getUint16(offset + 10, true);
            const compressedSize = view.getUint32(offset + 20, true);
            const nameLength = view.getUint16(offset + 28, true);
            const extraLength = view.getUint16(offset + 30, true);
            const commentLength = view.getUint16(offset + 32, true);
            const localOffset = view.getUint32(offset + 42, true);
            const name = decoder.decode(
                bytes.subarray(offset + 46, offset + 46 + nameLength)
            );
            files.set(name, { method, compressedSize, localOffset });
            offset += 46 + nameLength + extraLength + commentLength;
        }

        return {
            names: [...files.keys()],
            async read(name) {
                const file = files.get(name);
                if (!file) return null;
                const local = file.localOffset;
                if (view.getUint32(local, true) !== 0x04034b50) {
                    throw new Error("Bad ZIP local header.");
                }
                const nameLength = view.getUint16(local + 26, true);
                const extraLength = view.getUint16(local + 28, true);
                const start = local + 30 + nameLength + extraLength;
                const data = bytes.subarray(start, start + file.compressedSize);
                if (file.method === 0) return data;
                if (file.method !== 8) {
                    throw new Error("Unsupported ZIP compression.");
                }
                if (typeof DecompressionStream !== "function") {
                    throw new Error("This browser cannot decompress ZIP files.");
                }
                const stream = new Blob([data])
                    .stream()
                    .pipeThrough(new DecompressionStream("deflate-raw"));
                return new Uint8Array(await new Response(stream).arrayBuffer());
            },
        };
    }

    // Yomitan glossaries can be strings or nested structured content. Extract
    // only the readable text, ignoring images, audio and link metadata.
    function glossaryText(value) {
        if (value == null) return "";
        if (typeof value === "string") return value;
        if (Array.isArray(value)) {
            return value.map(glossaryText).filter(Boolean).join("\n");
        }
        if (typeof value === "object") {
            if (value.type === "image" || value.tag === "img") return "";
            if (typeof value.content === "string") return value.content;
            if (Array.isArray(value.content)) {
                return value.content.map(glossaryText).filter(Boolean).join("");
            }
            if (typeof value.text === "string") return value.text;
        }
        return "";
    }

    function cleanGloss(text) {
        return text
            .replace(/\r/g, "")
            .replace(/[ \t]+/g, " ")
            .replace(/\n[ \t]+/g, "\n")
            .replace(/\n{3,}/g, "\n\n")
            .trim();
    }

    // Structured Yomitan glossaries repeat the headword and reading inline
    // before the meaning. Formats seen in the imported ZH-JA dictionaries:
    //   〔国际〕guójì                 (中日)
    //   ［形］国际⚶guójì形...        (小学館)
    //   国际 guójì                   (白水社)
    // Strip the POS/headword labels, rank symbols and repeated reading.
    function normalizeGlossReading(value) {
        return [...String(value || "").toLowerCase()]
            .map((char) =>
                char.normalize("NFD").replace(/\p{M}/gu, "")
            )
            .join("")
            .replace(/[^a-z]/g, "");
    }

    function stripLeadingReading(text, reading) {
        const target = normalizeGlossReading(reading);
        if (!target) return text;
        let letters = "";
        for (let index = 0; index < text.length; index += 1) {
            const char = text[index];
            // Parenthetical variants such as guójì (gōng) fǎ are part of the
            // reading, but their contents should not break the match.
            if (char === "(" || char === "（") {
                const close = char === "(" ? ")" : "）";
                const end = text.indexOf(close, index + 1);
                if (end !== -1) {
                    index = end;
                    continue;
                }
            }
            const normalized = normalizeGlossReading(char);
            if (!normalized) {
                if (!letters) continue;
                if (/[\s'’/／・\-–:：,，]/.test(char)) continue;
                return text;
            }
            letters += normalized;
            if (letters.length < target.length) {
                if (!target.startsWith(letters)) return text;
                continue;
            }
            if (letters !== target) return text;
            const next = text[index + 1] || "";
            // Do not strip a reading that is merely the start of a longer word.
            if (/[A-Za-z]/.test(next)) return text;
            return text.slice(index + 1).replace(/^[\s:：·・/／]+/, "");
        }
        return text;
    }

    const GLOSS_TOP_LEVEL_RE =
        /^(?:[（(]\s*(?:[0-9０-９]+|[一二三四五六七八九十]+)\s*[）)]|(?:[0-9０-９]+|[一二三四五六七八九十]+)[.．、]?(?=\s|[.．、]|$))/;

    function splitGlossSenses(text) {
        const lines = cleanGloss(text).split("\n");
        const senses = [];
        let current = [];
        for (const rawLine of lines) {
            let line = rawLine.trim();
            if (!line) continue;
            const isTopLevel = GLOSS_TOP_LEVEL_RE.test(line);
            if (isTopLevel && current.length) {
                senses.push(current.join("\n"));
                current = [];
            }
            if (isTopLevel) {
                line = line.replace(GLOSS_TOP_LEVEL_RE, "").trim();
            }
            if (line) current.push(line);
        }
        if (current.length) senses.push(current.join("\n"));
        return senses;
    }

    function stripGlossHeader(gloss, term, reading) {
        let text = cleanGloss(gloss);
        let bracketText = "";
        // Remove leading POS/headword brackets. Two passes cover entries such as
        // "［名］〔词〕..." without touching semantic labels later in the text.
        for (let pass = 0; pass < 2; pass += 1) {
            const bracket = text.match(/^[［【〔]([^］】〕]*)[］】〕]\s*/);
            if (!bracket) break;
            bracketText = bracket[1].trim();
            text = text.slice(bracket[0].length);
        }

        let hadHeader = Boolean(bracketText);
        if (term) {
            const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
            const termPattern = new RegExp(
                `^[〔［【]?${escaped}[〕］】]?`
            );
            if (termPattern.test(text)) {
                text = text.replace(termPattern, "");
                hadHeader = true;
            }
        }

        // Rank symbols (⚶/⚷) and separators sit between the headword and its
        // reading in 小学館 entries.
        text = text.replace(/^[\s:：·・⚶⚷★☆◆◇■□●○]+/, "");
        if (hadHeader && reading) {
            text = stripLeadingReading(text, reading);
        }
        // Rank symbols can also occur later in the definition.
        text = text.replace(/[⚶⚷]/g, "");

        // 小学館 repeats its POS marker inside the meaning (［形］国际⚶guójì形…).
        if (bracketText && text.startsWith(bracketText)) {
            text = text.slice(bracketText.length).replace(/^[\s:：·・]+/, "");
        }
        return cleanGloss(text);
    }

    // Han unigram ranges, including supplementary planes.
    function isHan(code) {
        return (
            (code >= 0x3400 && code <= 0x4dbf) ||
            (code >= 0x4e00 && code <= 0x9fff) ||
            (code >= 0xf900 && code <= 0xfaff) ||
            (code >= 0x20000 && code <= 0x323af)
        );
    }

    function addHanGrams(text, bigrams, trigrams) {
        let run = [];
        const flush = () => {
            for (let index = 0; index < run.length; index += 1) {
                if (index + 1 < run.length) {
                    bigrams.add(run[index] + run[index + 1]);
                }
                if (index + 2 < run.length) {
                    trigrams.add(
                        run[index] + run[index + 1] + run[index + 2]
                    );
                }
            }
        };
        for (const char of text) {
            if (isHan(char.codePointAt(0))) {
                run.push(char);
            } else {
                flush();
                run = [];
            }
        }
        flush();
    }

    function hanGramSets(text) {
        const bigrams = new Set();
        const trigrams = new Set();
        addHanGrams(text, bigrams, trigrams);
        return { bigrams, trigrams };
    }

    // Import-time gram collection. Bigrams pack into one exact 42-bit Number;
    // trigrams stay strings because three 21-bit code points exceed the 53-bit
    // integer range once supplementary-plane Han is included. Each bank is
    // sorted, deduplicated, and merged into the running sorted arrays.
    const BIGRAM_BASE = 0x200000; // 2^21

    function packBigram(a, b) {
        return a * BIGRAM_BASE + b;
    }

    function unpackBigram(value) {
        const low = value % BIGRAM_BASE;
        const high = (value - low) / BIGRAM_BASE;
        return (
            String.fromCodePoint(high) +
            String.fromCodePoint(low)
        );
    }

    function mergeSortedNumbers(left, right) {
        const merged = [];
        let i = 0;
        let j = 0;
        while (i < left.length && j < right.length) {
            const a = left[i];
            const b = right[j];
            if (a < b) {
                merged.push(a);
                i += 1;
            } else if (b < a) {
                merged.push(b);
                j += 1;
            } else {
                merged.push(a);
                i += 1;
                j += 1;
            }
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
            if (a < b) {
                merged.push(a);
                i += 1;
            } else if (b < a) {
                merged.push(b);
                j += 1;
            } else {
                merged.push(a);
                i += 1;
                j += 1;
            }
        }
        while (i < left.length) merged.push(left[i++]);
        while (j < right.length) merged.push(right[j++]);
        return merged;
    }

    // Size-tiered merging keeps gram merging O(N log banks) without holding
    // every per-bank array and every merge level at once.
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
                    result = result.length
                        ? mergeFn(result, tier)
                        : tier;
                }
                return result;
            },
        };
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
                for (const char of text || "") {
                    const code = char.codePointAt(0);
                    if (isHan(code)) {
                        run.push(code);
                    } else {
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

    function numericGramBytes(grams) {
        let bytes = 0;
        for (const value of grams) {
            const low = value % BIGRAM_BASE;
            const high = (value - low) / BIGRAM_BASE;
            bytes +=
                utf8Length(String.fromCodePoint(high)) +
                utf8Length(String.fromCodePoint(low)) +
                GRAM_BYTES_OVERHEAD;
        }
        return bytes;
    }

    function utf8Length(text) {
        let bytes = 0;
        for (let index = 0; index < text.length; index += 1) {
            const code = text.charCodeAt(index);
            if (code < 0x80) bytes += 1;
            else if (code < 0x800) bytes += 2;
            else if (code >= 0xd800 && code <= 0xdbff) {
                bytes += 4;
                index += 1;
            } else {
                bytes += 3;
            }
        }
        return bytes;
    }

    // Approximate record overhead for the index size estimate.
    const GRAM_BYTES_OVERHEAD = 24;

    function gramBytes(grams) {
        let bytes = 0;
        for (const gram of grams) {
            bytes += utf8Length(gram) + GRAM_BYTES_OVERHEAD;
        }
        return bytes;
    }

    // OpenCC conversion is fetched from the server and cached, so imported
    // headwords follow the script toggle like the official dictionaries.
    const convertCache = new Map();

    async function convertScript(text) {
        if (!text) return { simplified: "", traditional: "" };
        if (convertCache.has(text)) return convertCache.get(text);
        const promise = fetch(`/api/convert?text=${encodeURIComponent(text)}`, {
            cache: "no-store",
        })
            .then((response) => response.json())
            .then((data) => ({
                simplified: data.simplified || text,
                traditional: data.traditional || text,
            }))
            .catch(() => ({ simplified: text, traditional: text }));
        convertCache.set(text, promise);
        return promise;
    }

    // Convert several headwords in one request. The server caps each request
    // at 200 texts, so send 200 per request with a few requests in flight.
    const CONVERT_BATCH_SIZE = 200;
    const CONVERT_CONCURRENCY = 5;

    async function convertScripts(texts) {
        const unique = [...new Set(texts.filter(Boolean))];
        if (!unique.length) return [];
        const missing = unique.filter((text) => !convertCache.has(text));
        const chunks = [];
        for (let offset = 0; offset < missing.length; offset += CONVERT_BATCH_SIZE) {
            chunks.push(missing.slice(offset, offset + CONVERT_BATCH_SIZE));
        }
        let nextChunk = 0;
        const workers = Array.from(
            { length: Math.min(CONVERT_CONCURRENCY, chunks.length) },
            async () => {
                while (true) {
                    const index = nextChunk++;
                    if (index >= chunks.length) return;
                    const chunk = chunks[index];
                    let items = [];
                    try {
                        const response = await fetch("/api/convert-batch", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ texts: chunk }),
                            cache: "no-store",
                        });
                        if (!response.ok) {
                            throw new Error("convert-batch failed");
                        }
                        const data = await response.json();
                        items = Array.isArray(data)
                            ? data
                            : data.results || [];
                    } catch (error) {
                        items = [];
                    }
                    chunk.forEach((text, itemIndex) => {
                        const item = items[itemIndex] || {};
                        convertCache.set(
                            text,
                            Promise.resolve({
                                simplified: item.simplified || text,
                                traditional: item.traditional || text,
                            })
                        );
                    });
                }
            }
        );
        await Promise.all(workers);
        return Promise.all(unique.map((text) => convertCache.get(text)));
    }

    // Tone-mark to base + tone, for the tonal pinyin ranking.
    const TONE_BASE = {
        "ā": "a", "á": "a", "ǎ": "a", "à": "a",
        "ē": "e", "é": "e", "ě": "e", "è": "e",
        "ī": "i", "í": "i", "ǐ": "i", "ì": "i",
        "ō": "o", "ó": "o", "ǒ": "o", "ò": "o",
        "ū": "u", "ú": "u", "ǔ": "u", "ù": "u",
        "ǖ": "ü", "ǘ": "ü", "ǚ": "ü", "ǜ": "ü",
        "Ā": "A", "Á": "A", "Ǎ": "A", "À": "A",
        "Ē": "E", "É": "E", "Ě": "E", "È": "E",
        "Ī": "I", "Í": "I", "Ǐ": "I", "Ì": "I",
        "Ō": "O", "Ó": "O", "Ǒ": "O", "Ò": "O",
        "Ū": "U", "Ú": "U", "Ǔ": "U", "Ù": "U",
        "Ǖ": "Ü", "Ǘ": "Ü", "Ǚ": "Ü", "Ǜ": "Ü",
    };
    const TONE_NUMBER = {
        "ā": 1, "á": 2, "ǎ": 3, "à": 4,
        "ē": 1, "é": 2, "ě": 3, "è": 4,
        "ī": 1, "í": 2, "ǐ": 3, "ì": 4,
        "ō": 1, "ó": 2, "ǒ": 3, "ò": 4,
        "ū": 1, "ú": 2, "ǔ": 3, "ù": 4,
        "ǖ": 1, "ǘ": 2, "ǚ": 3, "ǜ": 4,
        "Ā": 1, "Á": 2, "Ǎ": 3, "À": 4,
        "Ē": 1, "É": 2, "Ě": 3, "È": 4,
        "Ī": 1, "Í": 2, "Ǐ": 3, "Ì": 4,
        "Ō": 1, "Ó": 2, "Ǒ": 3, "Ò": 4,
        "Ū": 1, "Ú": 2, "Ǔ": 3, "Ù": 4,
        "Ǖ": 1, "Ǘ": 2, "Ǚ": 3, "Ǜ": 4,
    };

    function markedToNumeric(pinyin) {
        return (pinyin || "")
            .trim()
            .split(/\s+/)
            .map((syllable) => {
                let base = "";
                let tone = 5;
                for (const char of syllable) {
                    if (TONE_BASE[char] !== undefined) {
                        base += TONE_BASE[char];
                        tone = TONE_NUMBER[char];
                    } else {
                        base += char;
                    }
                }
                return base + tone;
            })
            .join(" ");
    }

    function parseTones(text) {
        let letters = "";
        const tones = {};
        const boundaries = new Set();
        let hasTone = false;
        for (const char of text.toLowerCase()) {
            if (char >= "1" && char <= "5") {
                tones[letters.length] = char;
                boundaries.add(letters.length);
                hasTone = true;
            } else if (char === "'" || char === "’" || char === " ") {
                boundaries.add(letters.length);
            } else if ((char >= "a" && char <= "z") || char === "ü") {
                letters += char;
            } else {
                return null;
            }
        }
        return hasTone ? { letters, tones, boundaries } : null;
    }

    function readingRank(numeric, query) {
        const parts = numeric.trim().split(/\s+/).map((syllable) => {
            const match = syllable.match(/^(.*?)([1-5])?$/);
            return [match[1], match[2] || ""];
        });
        const constraints = new Set([
            ...Object.keys(query.tones).map(Number),
            ...query.boundaries,
        ]);
        let best = null;
        for (let start = 0; start < parts.length; start += 1) {
            let pos = 0;
            let index = start;
            let ok = true;
            while (pos < query.letters.length && index < parts.length) {
                const [word, tone] = parts[index];
                for (const offset of constraints) {
                    if (pos < offset && offset < pos + word.length) {
                        ok = false;
                        break;
                    }
                }
                if (!ok) break;
                pos += word.length;
                if (query.tones[pos] !== undefined && query.tones[pos] !== tone) {
                    ok = false;
                    break;
                }
                index += 1;
            }
            if (!ok || pos !== query.letters.length) continue;
            const consumed = parts.slice(start, index).map((part) => part[0]).join("");
            if (consumed !== query.letters) continue;
            if ([...constraints].some((offset) => offset > query.letters.length)) continue;
            const rank = start === 0 && index === parts.length ? 0 : start === 0 ? 1 : 2;
            if (best === null || rank < best) best = rank;
        }
        return best;
    }

    function formatBytes(bytes) {
        if (bytes < 1024) return `${Math.round(bytes)} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }

    // Compact counts for the narrow loading rows: 1,000 -> 1k, 42,990 -> 43k.
    function formatCount(value) {
        const number = Number(value) || 0;
        if (number < 1000) return String(Math.round(number));
        const unit = number >= 1000000 ? 1000000 : 1000;
        const suffix = unit === 1000000 ? "M" : "k";
        const scaled = number / unit;
        const text =
            scaled >= 100
                ? String(Math.round(scaled))
                : scaled.toFixed(1).replace(/\.0$/, "");
        return `${text}${suffix}`;
    }

    // Compare titles loosely so the same archive is not imported twice with
    // different casing or stray whitespace.
    function normalizeDictionaryTitle(title) {
        return String(title || "")
            .trim()
            .toLowerCase()
            .replace(/\s+/g, " ");
    }

    // --- Import ------------------------------------------------------------

    async function requestPersistence(statusNode) {
        if (statusNode) {
            statusNode.classList.remove("storage-granted", "storage-denied");
            statusNode.textContent = "Checking persistent storage...";
        }
        if (!window.isSecureContext) {
            if (statusNode) {
                statusNode.textContent =
                    "Persistent storage requires HTTPS; imports may be evicted.";
                statusNode.classList.add("storage-denied");
            }
            return;
        }
        if (!navigator.storage || !navigator.storage.persist) {
            if (statusNode) {
                statusNode.textContent =
                    "Persistent storage is not supported by this browser.";
                statusNode.classList.add("storage-denied");
            }
            return;
        }
        try {
            const granted = await navigator.storage.persist();
            if (statusNode) {
                statusNode.textContent = granted
                    ? "Persistent storage granted."
                    : "Persistent storage was not granted; the browser may evict imports.";
                statusNode.classList.add(
                    granted ? "storage-granted" : "storage-denied"
                );
            }
        } catch (error) {
            if (statusNode) {
                statusNode.textContent = "Could not check persistent storage.";
                statusNode.classList.add("storage-denied");
            }
        }
    }

    // Put a temporary row under the language as soon as a file starts
    // importing, so every queued file is visible with a loading label.
    function addLoadingDictionaryRow(file, languageId) {
        const column = document.querySelector(
            `.dict-column[data-lang="${CSS.escape(languageId)}"]`
        );
        if (!column) return null;
        const items = column.querySelector(".dict-items");
        if (!items) return null;
        column.classList.add("open");
        const disclosure = column.querySelector(".dict-disclosure");
        if (disclosure) disclosure.setAttribute("aria-expanded", "true");
        rememberOpenLanguage(languageId);

        const row = document.createElement("label");
        row.className = "dict-loading";
        const toggle = document.createElement("input");
        toggle.type = "checkbox";
        toggle.className = "dict-loading-toggle";
        toggle.disabled = true;
        const title = document.createElement("span");
        title.className = "dict-loading-title";
        title.textContent = file.name.replace(/\.zip$/i, "");
        const status = document.createElement("span");
        status.className = "dict-loading-status";
        status.textContent = "loading...";
        const text = document.createElement("span");
        text.className = "dict-loading-text";
        text.append(title, status);
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "dict-cancel";
        cancel.textContent = "×";
        cancel.title = "Cancel import";
        cancel.setAttribute("aria-label", "Cancel import");
        cancel.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            const dictId = row.dataset.dictId;
            if (dictId) {
                cancelImport(dictId, row);
            } else {
                row.dataset.cancelled = "true";
                importCancelled = true;
                row.remove();
                const importStatus =
                    document.querySelector(".import-status");
                if (importStatus) {
                    importStatus.textContent = "Cancelling import...";
                }
            }
        });
        row.append(toggle, text, cancel);
        items.append(row);

        // The panel is already revealed by the import plus, but keep the state
        // in sync if the row is created from another entry point.
        window.state.customDicts = true;
        const customToggle = document.getElementById("custom-dicts-toggle");
        if (customToggle) customToggle.checked = true;
        window.syncDictPanel?.();
        return row;
    }

    async function importZip(
        file,
        languageId,
        statusPrefix = "",
        placeholder = null,
        resumeJob = null
    ) {
        let dictionary = null;
        let job = resumeJob;
        const importStartedAt = performance.now();
        let parseMs = 0;
        let convertMs = 0;
        let entriesMs = 0;
        let bigramMs = 0;
        let trigramMs = 0;
        const status = document.querySelector(".import-status");
        const setStatus = (text) => {
            if (status) status.textContent = statusPrefix + text;
        };
        const setPlaceholderStatus = (text) => {
            if (!placeholder || !placeholder.isConnected) return;
            const node = placeholder.querySelector(".dict-loading-status");
            if (node) node.textContent = text;
        };
        const shouldStop = () =>
            importCancelled ||
            (placeholder && placeholder.dataset.cancelled === "true") ||
            (dictionary && cancelledImports.has(dictionary.id));
        try {
            setStatus(resumeJob ? "Resuming import..." : "Reading ZIP...");
            setPlaceholderStatus(
                resumeJob ? "resuming import..." : "reading ZIP..."
            );
            const buffer = await file.arrayBuffer();
            const zip = zipEntries(buffer);
            let dictId;
            let anchor;
            let title;
            let revision;
            let bankNames;
            let startBank = 0;
            let termCount = 0;
            let dataBytes = 0;
            const bigramMerger = createSortedMerger(mergeSortedNumbers);
            const trigramMerger = createSortedMerger(mergeSortedStrings);

            if (resumeJob) {
                dictId = resumeJob.dictId;
                anchor = resumeJob.anchor;
                title = resumeJob.title;
                revision = resumeJob.revision || "";
                bankNames = resumeJob.bankNames || [];
                startBank = Math.max(
                    0,
                    Math.min(
                        bankNames.length,
                        Number(resumeJob.nextBankIndex) || 0
                    )
                );
                termCount = Number(resumeJob.termCount) || 0;
                dataBytes = Number(resumeJob.dataBytes) || 0;
                dictionary = dictionaries.find(
                    (entry) => entry.id === dictId
                );
                if (!dictionary) {
                    dictionary = {
                        id: dictId,
                        languageId: resumeJob.languageId || languageId,
                        title,
                        revision,
                        anchor,
                        termCount,
                        dataBytes,
                        indexed: false,
                        bigramCount: 0,
                        trigramCount: 0,
                        bigramBytes: 0,
                        trigramBytes: 0,
                        totalBytes: 0,
                        converted: false,
                        importing: true,
                        incomplete: false,
                        importProgress: "",
                        createdAt: resumeJob.createdAt || Date.now(),
                    };
                    dictionaries.push(dictionary);
                } else {
                    dictionary.importing = true;
                    dictionary.incomplete = false;
                }
                job = resumeJob;
                // Rebuild the gram sets from rows written before the reload.
                const existingRows = await dbAllByIndex(
                    "entries",
                    "dictId",
                    dictId
                );
                const rowChunk = 25000;
                for (
                    let offset = 0;
                    offset < existingRows.length;
                    offset += rowChunk
                ) {
                    const collector = createGramCollector();
                    for (const row of existingRows.slice(
                        offset,
                        offset + rowChunk
                    )) {
                        collector.addText(row.term || "");
                        collector.addText(row.reading || "");
                        collector.addText(row.gloss || "");
                    }
                    const part = collector.finish();
                    bigramMerger.add(part.bigrams);
                    trigramMerger.add(part.trigrams);
                    await new Promise((resolve) => setTimeout(resolve, 0));
                }
            } else {
                const indexBytes = await zip.read("index.json");
                if (!indexBytes) {
                    throw new Error("The ZIP has no index.json.");
                }
                const index = JSON.parse(
                    new TextDecoder().decode(indexBytes)
                );
                title = (index.title || file.name).trim();
                revision = index.revision || "";
                if (placeholder) {
                    const placeholderTitle = placeholder.querySelector(
                        ".dict-loading-title"
                    );
                    if (placeholderTitle) {
                        placeholderTitle.textContent = title;
                    }
                }
                const normalizedTitle = normalizeDictionaryTitle(title);
                const duplicate = dictionaries.some(
                    (entry) =>
                        normalizeDictionaryTitle(entry.title) ===
                        normalizedTitle
                );
                if (duplicate) {
                    setStatus(`Skipped ${title}: already imported.`);
                    if (placeholder && placeholder.isConnected) {
                        placeholder.remove();
                    }
                    return "skipped";
                }
                bankNames = zip.names
                    .filter((name) => /^term_bank_\d+\.json$/.test(name))
                    .sort();
                if (!bankNames.length) {
                    throw new Error(
                        "The ZIP has no term_bank_*.json files."
                    );
                }
                dictId = `dict-${Date.now()}-${Math.random()
                    .toString(36)
                    .slice(2, 7)}`;
                anchor = `custom-${dictId}`;
                dictionary = {
                    id: dictId,
                    languageId,
                    title,
                    revision,
                    anchor,
                    termCount: 0,
                    dataBytes: 0,
                    indexed: false,
                    bigramCount: 0,
                    trigramCount: 0,
                    bigramBytes: 0,
                    trigramBytes: 0,
                    totalBytes: 0,
                    converted: false,
                    importing: true,
                    incomplete: false,
                    importProgress: "",
                    createdAt: Date.now(),
                };
                await dbPut("dictionaries", dictionary);
                dictionaries.push(dictionary);
                job = {
                    id: dictId,
                    dictId,
                    anchor,
                    title,
                    revision,
                    languageId,
                    bankNames,
                    nextBankIndex: 0,
                    termCount: 0,
                    dataBytes: 0,
                    createdAt: Date.now(),
                };
                await dbPut("import-files", { id: dictId, file });
                await dbPut("imports", job);
            }

            if (placeholder) placeholder.dataset.dictId = dictId;
            activeImportId = dictId;
            if (shouldStop()) {
                throw new ImportCancelled("Import cancelled");
            }

            for (let b = startBank; b < bankNames.length; b += 1) {
                if (shouldStop()) {
                    throw new ImportCancelled("Import cancelled");
                }
                setStatus(
                    `Bank ${b + 1}/${bankNames.length} · reading ${bankNames[b]}...`
                );
                setPlaceholderStatus(
                    `bank ${b + 1}/${bankNames.length} · reading`
                );
                const parseStarted = performance.now();
                const bytes = await zip.read(bankNames[b]);
                const rows = JSON.parse(new TextDecoder().decode(bytes));
                parseMs += performance.now() - parseStarted;
                const collector = createGramCollector();
                const batch = [];
                for (const row of rows) {
                    if (!Array.isArray(row) || !row[0]) continue;
                    const term = String(row[0]).trim();
                    const reading = String(row[1] || "").trim();
                    let gloss = stripGlossHeader(
                        glossaryText(row[5]),
                        term,
                        reading
                    );
                    if (!gloss) gloss = cleanGloss(glossaryText(row[5]));
                    if (!term || !gloss) continue;
                    collector.addText(term);
                    collector.addText(reading);
                    collector.addText(gloss);
                    dataBytes +=
                        utf8Length(term) +
                        utf8Length(reading) +
                        utf8Length(gloss) +
                        32;
                    batch.push({ dictId, term, reading, gloss });
                }
                const bankGrams = collector.finish();
                bigramMerger.add(bankGrams.bigrams);
                trigramMerger.add(bankGrams.trigrams);
                if (shouldStop()) {
                    throw new ImportCancelled("Import cancelled");
                }
                setStatus(
                    `Bank ${b + 1}/${bankNames.length} · ` +
                        `converting ${formatCount(batch.length)} rows...`
                );
                setPlaceholderStatus(
                    `bank ${b + 1}/${bankNames.length} · converting`
                );
                const convertStarted = performance.now();
                await convertRowsInPlace(batch, shouldStop);
                convertMs += performance.now() - convertStarted;
                const nextTermCount = termCount + batch.length;
                dictionary.termCount = nextTermCount;
                dictionary.dataBytes = dataBytes;
                dictionary.importProgress = `${b + 1}/${bankNames.length}`;
                job.nextBankIndex = b + 1;
                job.termCount = nextTermCount;
                job.dataBytes = dataBytes;
                setStatus(
                    `Bank ${b + 1}/${bankNames.length} · saving rows...`
                );
                setPlaceholderStatus(
                    `bank ${b + 1}/${bankNames.length} · saving rows`
                );
                const entriesStarted = performance.now();
                await addTermRows(batch, (transaction) => {
                    transaction
                        .objectStore("dictionaries")
                        .put(dictionary);
                    transaction.objectStore("imports").put(job);
                });
                entriesMs += performance.now() - entriesStarted;
                termCount = nextTermCount;
                const elapsed = (
                    (performance.now() - importStartedAt) /
                    1000
                ).toFixed(1);
                const counts =
                    `${formatCount(termCount)} entries · ` +
                    `${formatCount(bigramMerger.total)} bigrams · ` +
                    `${formatCount(trigramMerger.total)} trigrams · ` +
                    `${elapsed}s`;
                setStatus(
                    `Bank ${b + 1}/${bankNames.length} done · ${counts}`
                );
                setPlaceholderStatus(
                    `bank ${b + 1}/${bankNames.length} · ${counts}`
                );
                await new Promise((resolve) => setTimeout(resolve, 0));
            }

            const bigrams = bigramMerger.finish();
            const trigrams = trigramMerger.finish();
            const bigramTotal = formatCount(bigrams.length);
            const trigramTotal = formatCount(trigrams.length);
            const writeElapsed = () =>
                ((performance.now() - importStartedAt) / 1000).toFixed(1);
            setStatus(
                `Writing bigrams 0/${bigramTotal} · ` +
                    `${formatCount(termCount)} entries · ${writeElapsed()}s`
            );
            setPlaceholderStatus(`bigrams 0/${bigramTotal}`);
            const bigramStarted = performance.now();
            await addNumericGramRows(
                dictId,
                "bi",
                bigrams,
                (done, total) => {
                    setStatus(
                        `Writing bigrams ${formatCount(done)}/${formatCount(total)} · ` +
                            `${formatCount(termCount)} entries · ${writeElapsed()}s`
                    );
                    setPlaceholderStatus(
                        `bigrams ${formatCount(done)}/${formatCount(total)}`
                    );
                },
                shouldStop
            );
            bigramMs += performance.now() - bigramStarted;
            setStatus(
                `Writing trigrams 0/${trigramTotal} · ` +
                    `${formatCount(termCount)} entries · ${writeElapsed()}s`
            );
            setPlaceholderStatus(`trigrams 0/${trigramTotal}`);
            const trigramStarted = performance.now();
            await addGramRows(
                dictId,
                "tri",
                trigrams,
                (done, total) => {
                    setStatus(
                        `Writing trigrams ${formatCount(done)}/${formatCount(total)} · ` +
                            `${formatCount(termCount)} entries · ${writeElapsed()}s`
                    );
                    setPlaceholderStatus(
                        `trigrams ${formatCount(done)}/${formatCount(total)}`
                    );
                },
                shouldStop
            );
            trigramMs += performance.now() - trigramStarted;
            if (shouldStop()) {
                throw new ImportCancelled("Import cancelled");
            }
            setStatus("Saving dictionary...");
            setPlaceholderStatus("saving...");
            const bigramBytes = numericGramBytes(bigrams);
            const trigramBytes = gramBytes(trigrams);
            dictionary.termCount = termCount;
            dictionary.dataBytes = dataBytes;
            dictionary.indexed = true;
            dictionary.bigramCount = bigrams.length;
            dictionary.trigramCount = trigrams.length;
            dictionary.bigramBytes = bigramBytes;
            dictionary.trigramBytes = trigramBytes;
            dictionary.totalBytes =
                dataBytes + bigramBytes + trigramBytes;
            dictionary.converted = true;
            dictionary.importing = false;
            dictionary.incomplete = false;
            dictionary.importProgress = "";
            await dbPut("dictionaries", dictionary);
            await dbDelete("imports", dictId);
            await dbDelete("import-files", dictId);
            invalidateTermIndex();
            invalidateSqliteSearch();
            resetScanWorker();
            scheduleSqliteRebuild();
            if (shouldStop()) {
                await cleanupImport(dictId, dictionary, placeholder);
                return "cancelled";
            }
            addDictionaryToLanguage(dictionary, false, placeholder);
            window.state.customDicts = true;
            const customToggle = document.getElementById(
                "custom-dicts-toggle"
            );
            if (customToggle) customToggle.checked = true;
            window.entryDictState.clear();
            window.syncGroups();
            window.syncDictPanel();
            window.savePrefs();
            window.redraw();
            setStatus(
                `Imported ${title} (${termCount.toLocaleString()} entries).`
            );
            customStats.lastImport = {
                id: dictId,
                title,
                termCount,
                bigrams: bigrams.length,
                trigrams: trigrams.length,
                parseMs: Math.round(parseMs),
                convertMs: Math.round(convertMs),
                entriesMs: Math.round(entriesMs),
                bigramMs: Math.round(bigramMs),
                trigramMs: Math.round(trigramMs),
                totalMs: Math.round(performance.now() - importStartedAt),
            };
            return "imported";
        } catch (error) {
            const wasCancelled =
                error instanceof ImportCancelled || shouldStop();
            if (wasCancelled && dictionary) {
                await cleanupImport(
                    dictionary.id,
                    dictionary,
                    placeholder
                );
                setStatus("Import cancelled.");
                return "cancelled";
            }
            console.error(error);
            setStatus(`Import failed: ${error.message || error}`);
            if (dictionary && dictionary.importing) {
                dictionary.importing = false;
                dictionary.incomplete = true;
                dictionary.importProgress = "";
                try {
                    await dbPut("dictionaries", dictionary);
                    await dbDelete("imports", dictionary.id);
                    await dbDelete("import-files", dictionary.id);
                    addDictionaryToLanguage(
                        dictionary,
                        false,
                        placeholder
                    );
                } catch (saveError) {
                    console.error(saveError);
                }
            }
            if (placeholder && placeholder.isConnected) {
                const placeholderStatus = placeholder.querySelector(
                    ".dict-loading-status"
                );
                if (placeholderStatus) {
                    placeholderStatus.textContent = "failed";
                }
                placeholder.classList.add("dict-loading-error");
            }
            return "failed";
        } finally {
            activeImportId = null;
            importCancelled = false;
        }
    }

    async function cleanupImport(dictId, dictionary = null, placeholder = null) {
        const target =
            dictionary ||
            dictionaries.find((entry) => entry.id === dictId);
        if (target) forgetDictionary(target);
        if (placeholder && placeholder.isConnected) placeholder.remove();
        await deleteDictionaryData(dictId);
        await dbDelete("imports", dictId);
        await dbDelete("import-files", dictId);
        cancelledImports.delete(dictId);
        importCancelled = false;
        activeImportId = null;
        refreshAfterDictionaryChange();
    }

    function cancelImport(dictId, placeholder = null) {
        importCancelled = true;
        if (dictId) cancelledImports.add(dictId);
        const dictionary = dictId
            ? dictionaries.find((entry) => entry.id === dictId)
            : null;
        // Close the row immediately. The background import checks the cancel
        // flag at its next checkpoint and deletes the partial data.
        if (placeholder && placeholder.isConnected) placeholder.remove();
        if (dictionary) forgetDictionary(dictionary);
        const status = document.querySelector(".import-status");
        if (status) status.textContent = "Cancelling import...";
        const active = importing && activeImportId === dictId;
        if (!active) {
            cleanupImport(dictId, dictionary, placeholder).catch((error) =>
                console.error(error)
            );
        }
    }

    async function resumeImports(jobs, filesById) {
        importing = true;
        importCancelled = false;
        try {
            for (const job of jobs) {
                if (importCancelled) break;
                const file = filesById.get(job.id);
                if (!file) continue;
                const placeholder = addLoadingDictionaryRow(
                    { name: `${job.title}.zip` },
                    job.languageId
                );
                if (placeholder) {
                    placeholder.dataset.dictId = job.dictId;
                }
                await importZip(
                    file,
                    job.languageId,
                    "",
                    placeholder,
                    job
                );
            }
        } finally {
            importing = false;
            importCancelled = false;
        }
    }

    async function importFiles(files, languageId) {
        if (importing || !files.length) return;
        importing = true;
        importCancelled = false;
        cancelledImports.clear();
        let imported = 0;
        let skipped = 0;
        let failed = 0;
        let cancelled = 0;
        try {
            const placeholders = files.map((file) =>
                addLoadingDictionaryRow(file, languageId)
            );
            for (let index = 0; index < files.length; index += 1) {
                const prefix =
                    files.length > 1
                        ? `[${index + 1}/${files.length}] ${files[index].name}: `
                        : "";
                const result = await importZip(
                    files[index],
                    languageId,
                    prefix,
                    placeholders[index]
                );
                if (result === "imported") imported += 1;
                else if (result === "skipped") skipped += 1;
                else if (result === "cancelled") {
                    cancelled += 1;
                    for (
                        let rest = index + 1;
                        rest < placeholders.length;
                        rest += 1
                    ) {
                        placeholders[rest]?.remove();
                    }
                    break;
                } else failed += 1;
            }
            const status = document.querySelector(".import-status");
            if (status) {
                const parts = [`${imported} imported`];
                if (skipped) parts.push(`${skipped} skipped`);
                if (failed) parts.push(`${failed} failed`);
                if (cancelled) parts.push(`${cancelled} cancelled`);
                status.textContent = `Finished: ${parts.join(", ")}.`;
            }
        } finally {
            importing = false;
            importCancelled = false;
            activeImportId = null;
            cancelledImports.clear();
        }
    }

    // --- Custom panel ------------------------------------------------------

    function rememberOpenLanguage(lang) {
        try {
            const saved = JSON.parse(localStorage.getItem(OPEN_KEY) || "[]");
            const set = new Set(Array.isArray(saved) ? saved : []);
            set.add(lang);
            localStorage.setItem(OPEN_KEY, JSON.stringify([...set]));
        } catch (error) {
            // Storage unavailable - the column still opens this time.
        }
    }

    function addColumnPlus(column) {
        if (column.querySelector(".dict-add-dict")) return;
        const lang = column.dataset.lang;
        const label =
            column.querySelector(".dict-name")?.textContent?.trim() || lang;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "dict-add-dict";
        button.textContent = "+";
        button.title = `Import a dictionary for ${label}`;
        button.setAttribute("aria-label", `Import a dictionary for ${label}`);
        button.addEventListener("click", () => openImportModal(lang, label));
        column.append(button);
    }

    function addLanguageColumn(language) {
        const columns = document.querySelector(".dict-columns");
        if (!columns) return null;
        const column = document.createElement("div");
        column.className = "dict-column";
        column.dataset.lang = language.id;

        const head = document.createElement("div");
        head.className = "dict-column-head";

        const name = document.createElement("span");
        name.className = "dict-name";
        name.textContent = language.name;

        const disclosure = document.createElement("button");
        disclosure.type = "button";
        disclosure.className = "dict-disclosure";
        disclosure.setAttribute("aria-expanded", "false");
        disclosure.setAttribute("aria-label", `Show ${language.name} dictionaries`);
        const caret = document.createElement("span");
        caret.className = "dict-caret";
        disclosure.append(caret);

        const removeLanguage = document.createElement("button");
        removeLanguage.type = "button";
        removeLanguage.className = "dict-remove dict-remove-lang";
        removeLanguage.textContent = "×";
        removeLanguage.title = `Delete ${language.name}`;
        removeLanguage.setAttribute("aria-label", `Delete ${language.name}`);
        removeLanguage.addEventListener("click", () => {
            deleteLanguage(language, column);
        });

        head.append(name, disclosure, removeLanguage);
        const items = document.createElement("div");
        items.className = "dict-items";
        column.append(head, items);
        const addCell = columns.querySelector(".dict-add-lang-cell");
        if (addCell) columns.insertBefore(column, addCell);
        else columns.append(column);

        disclosure.addEventListener("click", () => {
            const open = !column.classList.contains("open");
            column.classList.toggle("open", open);
            disclosure.setAttribute("aria-expanded", String(open));
            if (open) {
                rememberOpenLanguage(language.id);
                window.closeOtherDictColumns?.(column);
            }
        });
        head.addEventListener("click", (event) => {
            if (event.target.closest(".dict-disclosure, .dict-remove")) return;
            disclosure.click();
        });
        column.classList.add("open");
        disclosure.setAttribute("aria-expanded", "true");
        rememberOpenLanguage(language.id);
        window.closeOtherDictColumns?.(column);
        addColumnPlus(column);
        window.syncGroups();
        return column;
    }

    function addDictionaryToLanguage(dictionary, select, placeholder = null) {
        const column = document.querySelector(
            `.dict-column[data-lang="${CSS.escape(dictionary.languageId)}"]`
        );
        if (!column) return;
        if (column.querySelector(`[data-anchor="${CSS.escape(dictionary.anchor)}"]`)) {
            return;
        }
        const items = column.querySelector(".dict-items");
        const label = document.createElement("label");
        const toggle = document.createElement("input");
        toggle.type = "checkbox";
        toggle.className = "dict-toggle";
        toggle.dataset.anchor = dictionary.anchor;
        const removeDictionary = document.createElement("button");
        removeDictionary.type = "button";
        removeDictionary.className = "dict-remove dict-remove-dict";
        removeDictionary.textContent = "×";
        if (dictionary.importing) {
            removeDictionary.title = `Cancel import of ${dictionary.title}`;
            removeDictionary.setAttribute(
                "aria-label",
                `Cancel import of ${dictionary.title}`
            );
            removeDictionary.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                cancelImport(dictionary.id, placeholder);
            });
        } else {
            removeDictionary.title = `Delete ${dictionary.title}`;
            removeDictionary.setAttribute(
                "aria-label",
                `Delete ${dictionary.title}`
            );
            removeDictionary.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                deleteDictionary(dictionary);
            });
        }
        const name = document.createElement("span");
        name.className = "dict-name-label";
        name.textContent = dictionary.title;
        if (dictionary.importing || dictionary.incomplete) {
            const badge = document.createElement("span");
            badge.className = "dict-badge";
            badge.textContent = dictionary.importing
                ? "importing"
                : "incomplete";
            name.append(" ", badge);
        }
        label.append(toggle, name, removeDictionary);
        if (placeholder && placeholder.parentElement === items) {
            items.insertBefore(label, placeholder);
            placeholder.remove();
        } else {
            items.append(label);
        }

        window.dictToggles.push(toggle);
        window.anchorLang.set(dictionary.anchor, dictionary.languageId);
        toggle.checked = select || window.state.dicts.has(dictionary.anchor);
        if (toggle.checked) window.state.dicts.add(dictionary.anchor);
        if (dictionary.importing || dictionary.incomplete) {
            toggle.checked = false;
            toggle.disabled = true;
            window.state.dicts.delete(dictionary.anchor);
        }
        toggle.addEventListener("change", () => {
            if (toggle.checked) window.state.dicts.add(dictionary.anchor);
            else window.state.dicts.delete(dictionary.anchor);
            invalidateTermIndex();
            window.syncGroups();
            window.entryDictState.clear();
            window.savePrefs();
            window.redraw();
        });

        if (select) {
            window.state.customDicts = true;
            const customToggle = document.getElementById("custom-dicts-toggle");
            if (customToggle) customToggle.checked = true;
            window.entryDictState.clear();
            window.syncGroups();
            window.syncDictPanel();
            window.savePrefs();
            window.redraw();
        }
        const disclosure = column.querySelector(".dict-disclosure");
        column.classList.add("open");
        if (disclosure) disclosure.setAttribute("aria-expanded", "true");
        rememberOpenLanguage(dictionary.languageId);
        window.closeOtherDictColumns?.(column);
        window.syncGroups();
        window.setupDictionaryOrdering?.();
    }

    // In-page dialogs instead of window.confirm/prompt, which browsers can
    // suppress when dialogs are blocked.
    function askConfirm(message) {
        const dialog = document.getElementById("confirm-dialog");
        const text = document.getElementById("confirm-message");
        if (!dialog || !text) return Promise.resolve(false);
        text.textContent = message;
        dialog.returnValue = "";
        if (dialog.open) dialog.close("cancel");
        dialog.showModal();
        return new Promise((resolve) => {
            dialog.addEventListener(
                "close",
                () => resolve(dialog.returnValue === "delete"),
                { once: true }
            );
        });
    }

    function promptLanguageName() {
        const dialog = document.getElementById("add-language-dialog");
        const form = document.getElementById("add-language-form");
        const input = document.getElementById("add-language-name");
        const error = document.getElementById("add-language-error");
        const cancel = document.getElementById("add-language-cancel");
        if (!dialog || !form || !input) return Promise.resolve(null);
        input.value = "";
        if (error) error.hidden = true;
        if (dialog.open) dialog.close();
        return new Promise((resolve) => {
            let result = null;
            const onSubmit = (event) => {
                event.preventDefault();
                const name = input.value.trim();
                if (!name) {
                    if (error) error.hidden = false;
                    input.focus();
                    return;
                }
                result = name;
                dialog.close();
            };
            const onCancel = () => {
                result = null;
                dialog.close();
            };
            const onClose = () => {
                form.removeEventListener("submit", onSubmit);
                cancel.removeEventListener("click", onCancel);
                dialog.removeEventListener("close", onClose);
                resolve(result);
            };
            form.addEventListener("submit", onSubmit);
            cancel.addEventListener("click", onCancel);
            dialog.addEventListener("close", onClose);
            dialog.showModal();
            input.focus();
        });
    }

    function forgetDictionary(dictionary) {
        dictionaries = dictionaries.filter((d) => d.id !== dictionary.id);
        window.anchorLang.delete(dictionary.anchor);
        window.state.dicts.delete(dictionary.anchor);
        const index = window.dictToggles.findIndex(
            (toggle) => toggle.dataset.anchor === dictionary.anchor
        );
        if (index >= 0) window.dictToggles.splice(index, 1);
        const label = document
            .querySelector(
                `.dict-toggle[data-anchor="${CSS.escape(dictionary.anchor)}"]`
            )
            ?.closest("label");
        if (label) label.remove();
    }

    function refreshAfterDictionaryChange() {
        invalidateTermIndex();
        invalidateSqliteSearch();
        resetScanWorker();
        scheduleSqliteRebuild(600);
        window.entryDictState.clear();
        window.syncGroups();
        window.savePrefs();
        window.refreshSearch();
    }

    async function deleteDictionary(dictionary) {
        if (!(await askConfirm(`Delete "${dictionary.title}"?`))) return;
        // Remove it from the interface and state immediately, then clean up
        // IndexedDB in the background.
        forgetDictionary(dictionary);
        refreshAfterDictionaryChange();
        deleteDictionaryData(dictionary.id).catch((error) =>
            console.error(error)
        );
    }

    async function deleteLanguage(language, column) {
        if (!(await askConfirm(`Delete "${language.name}" and its dictionaries?`))) {
            return;
        }
        const owned = dictionaries.filter(
            (dictionary) => dictionary.languageId === language.id
        );
        for (const dictionary of owned) forgetDictionary(dictionary);
        column.remove();
        const index = languages.findIndex((item) => item.id === language.id);
        if (index >= 0) languages.splice(index, 1);
        refreshAfterDictionaryChange();
        // IndexedDB cleanup happens after the column and toggles are gone.
        for (const dictionary of owned) {
            deleteDictionaryData(dictionary.id).catch((error) =>
                console.error(error)
            );
        }
        dbDelete("languages", language.id).catch((error) => console.error(error));
    }

    function addLanguagePlusCell() {
        const columns = document.querySelector(".dict-columns");
        if (!columns || columns.querySelector(".dict-add-lang-cell")) return;
        const cell = document.createElement("div");
        cell.className = "dict-add-lang-cell";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "dict-add-lang";
        button.textContent = "+";
        button.title = "Add a language";
        button.setAttribute("aria-label", "Add a language");
        button.addEventListener("click", async () => {
            await requestPersistence(null);
            const name = await promptLanguageName();
            if (!name) return;
            const language = {
                id: `custom-lang-${Date.now()}-${Math.random()
                    .toString(36)
                    .slice(2, 7)}`,
                name: name.trim(),
                createdAt: Date.now(),
            };
            await dbPut("languages", language);
            languages.push(language);
            addLanguageColumn(language);
        });
        cell.append(button);
        columns.append(cell);
    }

    // --- Import modal ------------------------------------------------------

    function ensureImportModal() {
        if (document.getElementById("custom-dict-modal")) return;
        const modal = document.createElement("div");
        modal.id = "custom-dict-modal";
        modal.className = "import-modal";
        modal.hidden = true;
        modal.innerHTML = `
            <div class="import-dialog" role="dialog" aria-modal="true">
                <h2>Import dictionary</h2>
                <p class="import-target"></p>
                <div class="import-drop" tabindex="0">
                    <p>Drop Yomitan <code>.zip</code> files here</p>
                    <p class="import-source"><a href="https://github.com/MarvNC/yomitan-dictionaries"
                        target="_blank" rel="noopener">Download Yomitan dictionaries</a></p>
                    <button type="button" class="import-choose">Choose files</button>
                    <input type="file" class="import-file" accept=".zip,application/zip" multiple hidden>
                </div>
                <p class="import-status"></p>
                <p class="import-storage-status"></p>
                <button type="button" class="import-close">Close</button>
            </div>
        `;
        document.body.append(modal);

        const fileInput = modal.querySelector(".import-file");
        const drop = modal.querySelector(".import-drop");
        modal.querySelector(".import-choose").addEventListener("click", () => {
            fileInput.click();
        });
        fileInput.addEventListener("change", async () => {
            const files = fileInput.files ? [...fileInput.files] : [];
            fileInput.value = "";
            if (files.length && importTarget) {
                await importFiles(files, importTarget.id);
            }
        });
        drop.addEventListener("dragover", (event) => {
            event.preventDefault();
            drop.classList.add("dragging");
        });
        drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
        drop.addEventListener("drop", async (event) => {
            event.preventDefault();
            drop.classList.remove("dragging");
            const files = event.dataTransfer?.files
                ? [...event.dataTransfer.files]
                : [];
            if (files.length && importTarget) {
                await importFiles(files, importTarget.id);
            }
        });
        modal.querySelector(".import-close").addEventListener("click", () => {
            modal.hidden = true;
        });
        modal.addEventListener("keydown", (event) => {
            if (event.key === "Escape") modal.hidden = true;
        });
    }

    function openImportModal(languageId, languageName) {
        ensureImportModal();
        importTarget = { id: languageId, name: languageName };
        const modal = document.getElementById("custom-dict-modal");
        modal.querySelector(".import-target").textContent = `Adding to ${languageName}`;
        modal.querySelector(".import-status").textContent = "";
        modal.querySelector(".import-storage-status").textContent = "";
        modal.hidden = false;
        // Ask for persistent storage as part of the plus-button click.
        requestPersistence(modal.querySelector(".import-storage-status"));
        modal.querySelector(".import-close").focus();
    }

    // --- Search index ------------------------------------------------------

    async function loadTermIndex() {
        if (termIndex) return termIndex;
        if (termIndexPromise) return termIndexPromise;
        const state = window.state;
        const activeIds = new Set(
            dictionaries
                .filter(
                    (dictionary) =>
                        !dictionary.importing &&
                        !dictionary.incomplete &&
                        state &&
                        state.dicts.has(dictionary.anchor)
                )
                .map((dictionary) => dictionary.id)
        );
        // Nothing selected: do not touch the entries store at all. This avoids
        // loading hundreds of thousands of imported entries (and crashing the
        // tab) when the user only searches the official dictionaries.
        if (!activeIds.size) {
            termIndex = [];
            termIndexByDict = new Map();
            return termIndex;
        }
        const generation = termIndexGeneration;
        termIndexPromise = (async () => {
            const built = [];
            const byDict = new Map();
            for (const dictId of activeIds) {
                const rows = await dbAllByIndex("entries", "dictId", dictId);
                for (const row of rows) {
                    const term = row.term || "";
                    const reading = row.reading || "";
                    const entry = {
                        dictId: row.dictId,
                        term,
                        simplified: row.simplified || term,
                        traditional: row.traditional || term,
                        reading,
                        gloss: row.gloss || "",
                        termLower: term.toLowerCase(),
                        readingLower: reading.toLowerCase(),
                        readingToneless:
                            row.readingToneless || tonelessReading(reading),
                        glossLower: (row.gloss || "").toLowerCase(),
                    };
                    built.push(entry);
                    const group = byDict.get(row.dictId);
                    if (group) group.push(entry);
                    else byDict.set(row.dictId, [entry]);
                    if (built.length % 25000 === 0) {
                        await new Promise((resolve) => setTimeout(resolve, 0));
                    }
                }
                await new Promise((resolve) => setTimeout(resolve, 0));
            }
            if (generation === termIndexGeneration) {
                termIndex = built;
                termIndexByDict = byDict;
            }
            return built;
        })();
        return termIndexPromise;
    }

    function binarySearch(sorted, value) {
        let low = 0;
        let high = sorted.length - 1;
        while (low <= high) {
            const middle = (low + high) >> 1;
            const current = sorted[middle];
            if (current === value) return middle;
            if (current < value) low = middle + 1;
            else high = middle - 1;
        }
        return -1;
    }

    async function getGramBucket(dictId, size, bucket) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const request = db
                .transaction("gramBuckets", "readonly")
                .objectStore("gramBuckets")
                .get([dictId, size, bucket]);
            request.onsuccess = () => resolve(request.result || null);
            request.onerror = () => reject(request.error);
        });
    }

    async function oldGramExists(dictId, size, gram) {
        const db = await openDb();
        return new Promise((resolve, reject) => {
            const request = db
                .transaction("grams", "readonly")
                .objectStore("grams")
                .get([dictId, size, gram]);
            request.onsuccess = () => resolve(Boolean(request.result));
            request.onerror = () => reject(request.error);
        });
    }

    async function gramExists(dictId, size, gram) {
        const bucket = [...gram][0];
        const record = await getGramBucket(dictId, size, bucket);
        if (record) {
            return binarySearch(record.grams, gram) >= 0;
        }
        return oldGramExists(dictId, size, gram);
    }

    // Exact negative filter: a missing bigram or trigram proves the substring
    // cannot occur in this dictionary, so its entries do not need scanning.
    async function dictionaryHasGrams(dictId, grams) {
        for (const [size, set] of [
            ["bi", grams.bigrams],
            ["tri", grams.trigrams],
        ]) {
            if (!set.size) continue;
            const buckets = new Map();
            for (const gram of set) {
                const bucket = [...gram][0];
                const list = buckets.get(bucket);
                if (list) list.push(gram);
                else buckets.set(bucket, [gram]);
            }
            for (const [bucket, list] of buckets) {
                const record = await getGramBucket(dictId, size, bucket);
                if (record) {
                    for (const gram of list) {
                        if (binarySearch(record.grams, gram) < 0) {
                            return false;
                        }
                    }
                } else {
                    for (const gram of list) {
                        if (!(await oldGramExists(dictId, size, gram))) {
                            return false;
                        }
                    }
                }
            }
        }
        return true;
    }

    async function searchImported(query) {
        const searchStartedAt = performance.now();
        const indexStartedAt = performance.now();
        const state = window.state;
        if (!state) return [];
        const dictById = new Map(dictionaries.map((d) => [d.id, d]));
        const activeDicts = new Set();
        for (const dictionary of dictionaries) {
            if (state.dicts.has(dictionary.anchor)) {
                activeDicts.add(dictionary.id);
            }
        }
        if (!activeDicts.size) return [];
        const raw = (query || "").trim();
        if (!raw) return [];
        const text = raw.replace(/^"|"$/g, "").trim();
        if (!text) return [];
        await loadTermIndex();
        const indexMs = performance.now() - indexStartedAt;
        if (!termIndex || !termIndex.length) return [];
        const scanStartedAt = performance.now();
        const queryGrams = hanGramSets(text);
        if (queryGrams.bigrams.size || queryGrams.trigrams.size) {
            for (const dictId of [...activeDicts]) {
                if (!dictById.get(dictId)?.indexed) continue;
                if (!(await dictionaryHasGrams(dictId, queryGrams))) {
                    activeDicts.delete(dictId);
                }
            }
            if (!activeDicts.size) return [];
        }
        const scanRows = [];
        for (const dictId of activeDicts) {
            const rows = termIndexByDict?.get(dictId);
            if (rows) scanRows.push(...rows);
        }
        const lower = text.toLowerCase();
        const tonelessFn =
            typeof window.toToneless === "function"
                ? window.toToneless
                : (value) => value.toLowerCase();
        const toneless = tonelessFn(text);
        const wildcard = text.includes("*");
        const parsedTones = wildcard ? null : parseTones(text);
        const escape = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const wildcardRe = wildcard
            ? new RegExp(
                  "^" + text.split("*").map(escape).join(".*") + "$",
                  "i"
              )
            : null;
        const tonelessParts = wildcard
            ? text.split("*").map((part) => tonelessFn(part))
            : [];
        const tonelessRe =
            wildcard && tonelessParts.some(Boolean)
                ? new RegExp(
                      "^" +
                          tonelessParts.map(escape).join(".*") +
                          "$",
                      "i"
                  )
                : null;
        const matches = [];
        const seen = new Set();
        const push = (row, rank) => {
            if (!activeDicts.has(row.dictId)) return;
            const key = `${row.dictId}|${row.termLower}|${row.readingLower}`;
            if (seen.has(key)) return;
            seen.add(key);
            matches.push({ row, rank });
        };

        for (const row of scanRows) {
            let rank = -1;
            if (wildcard) {
                const termHit = wildcardRe.test(row.term);
                const readingHit = wildcardRe.test(row.reading);
                const tonelessHit = tonelessRe && tonelessRe.test(row.readingToneless);
                const glossHit = wildcardRe.test(row.gloss);
                if (termHit) rank = 0;
                else if (readingHit || tonelessHit) rank = 3;
                else if (glossHit) rank = 7;
            } else if (row.termLower === lower) rank = 0;
            else if (row.termLower.startsWith(lower)) rank = 1;
            else if (row.termLower.includes(lower)) rank = 2;
            else {
                if (parsedTones) {
                    const toneRank = readingRank(
                        markedToNumeric(row.reading),
                        parsedTones
                    );
                    if (toneRank !== null) rank = 3 + toneRank;
                }
                if (rank < 0) {
                    if (row.readingLower === lower) rank = 3;
                    else if (row.readingLower.startsWith(lower)) rank = 4;
                    else if (row.readingLower.includes(lower)) rank = 5;
                    else if (toneless && row.readingToneless.includes(toneless)) {
                        rank = 6;
                    }
                }
            }
            if (rank >= 0) push(row, rank);
        }

        if (!wildcard && matches.length < MAX_SEARCH_RESULTS && text.length >= 2) {
            for (const row of scanRows) {
                if (!row.glossLower.includes(lower)) continue;
                push(row, 7);
                if (matches.length >= MAX_SEARCH_RESULTS * 4) break;
            }
        }

        matches.sort((a, b) => {
            if (a.rank !== b.rank) return a.rank - b.rank;
            if (a.row.term.length !== b.row.term.length) {
                return a.row.term.length - b.row.term.length;
            }
            return a.row.dictId.localeCompare(b.row.dictId);
        });

        const groups = new Map();
        for (const { row } of matches) {
            const key = `${row.dictId}|${row.termLower}|${row.readingLower}`;
            let group = groups.get(key);
            if (!group) {
                group = {
                    dictId: row.dictId,
                    term: row.term,
                    simplified: row.simplified || row.term,
                    traditional: row.traditional || row.term,
                    reading: row.reading,
                    glosses: [],
                };
                groups.set(key, group);
            }
            if (row.gloss && !group.glosses.includes(row.gloss)) {
                group.glosses.push(row.gloss);
            }
            if (groups.size >= MAX_SEARCH_RESULTS) break;
        }
        const result = [...groups.values()];
        customStats.lastSearchMs = performance.now() - searchStartedAt;
        customStats.lastIndexMs = indexMs;
        customStats.lastScanMs = performance.now() - scanStartedAt;
        customStats.lastMatches = result.length;
        return result;
    }

    function mergeDefinition(target, source) {
        target.groups = target.groups || [];
        target.defs = target.defs || [];
        const sourceGroups =
            source.groups && source.groups.length
                ? source.groups
                : [{ pinyin: "", senses: source.defs || [] }];
        for (const group of sourceGroups) {
            const reading = group.pinyin || "";
            let targetGroup = target.groups.find(
                (item) => (item.pinyin || "") === reading
            );
            if (!targetGroup) {
                targetGroup = { pinyin: reading, senses: [] };
                target.groups.push(targetGroup);
            }
            for (const sense of group.senses || []) {
                if (!targetGroup.senses.includes(sense)) {
                    targetGroup.senses.push(sense);
                }
            }
        }
        for (const sense of source.defs || []) {
            if (!target.defs.includes(sense)) target.defs.push(sense);
        }
        if (source.classifiers && source.classifiers.length) {
            target.classifiers = target.classifiers || [];
            for (const classifier of source.classifiers) {
                const key = `${classifier.traditional}|${classifier.simplified}|` +
                    `${classifier.codes}|${classifier.note}`;
                const duplicate = target.classifiers.some(
                    (item) =>
                        `${item.traditional}|${item.simplified}|` +
                            `${item.codes}|${item.note}` === key
                );
                if (!duplicate) target.classifiers.push(classifier);
            }
        }
    }

    function addOrMergeDefinition(entry, source) {
        entry.definitions = entry.definitions || [];
        const existing = entry.definitions.find(
            (item) => item.anchor === source.anchor
        );
        if (existing) {
            mergeDefinition(existing, source);
            return;
        }
        entry.definitions.push(source);
    }

    async function mergeImported(data, matches, maxResults = Infinity) {
        if (!matches.length) return;
        const entries = data.results;
        const dictById = new Map(dictionaries.map((d) => [d.id, d]));
        const byHeadword = new Map();
        for (const entry of entries) {
            if (entry.simplified) byHeadword.set(entry.simplified, entry);
            if (entry.traditional) byHeadword.set(entry.traditional, entry);
        }
        const customOnly = [];
        const customByKey = new Map();
        // Newer imports store both script forms. Only rows imported before that
        // change need the batched conversion fallback.
        const missingTerms = [
            ...new Set(
                matches
                    .filter(
                        (match) =>
                            !byHeadword.has(match.term) &&
                            (!match.simplified || !match.traditional)
                    )
                    .map((match) => match.term)
                    .filter(Boolean)
            ),
        ];
        const convertedByTerm = new Map();
        if (missingTerms.length) {
            const converted = await convertScripts(missingTerms);
            missingTerms.forEach((term, index) => {
                convertedByTerm.set(term, converted[index]);
            });
        }
        for (const match of matches) {
            const dictionary = dictById.get(match.dictId);
            if (!dictionary) continue;
            // Clean again at display time so dictionaries imported before the
            // newer header cleaner also get the improved definitions.
            const senses = [];
            const seenSenses = new Set();
            for (const rawGloss of match.glosses || []) {
                const cleaned = stripGlossHeader(
                    rawGloss,
                    match.term,
                    match.reading
                );
                for (const sense of splitGlossSenses(cleaned)) {
                    if (!sense || seenSenses.has(sense)) continue;
                    seenSenses.add(sense);
                    senses.push(sense);
                }
            }
            if (!senses.length) continue;
            const source = {
                anchor: dictionary.anchor,
                label: dictionary.title,
                lang: dictionary.languageId,
                headword: match.term,
                groups: [
                    {
                        pinyin: match.reading,
                        senses,
                    },
                ],
                defs: senses,
                classifiers: [],
            };
            let existing = byHeadword.get(match.term);
            let simplified = match.simplified || match.term;
            let traditional = match.traditional || match.term;
            if (!existing && (!match.simplified || !match.traditional)) {
                const converted = convertedByTerm.get(match.term) || {
                    simplified: match.term,
                    traditional: match.term,
                };
                simplified = converted.simplified || match.term;
                traditional = converted.traditional || match.term;
            }
            if (!existing) {
                existing =
                    byHeadword.get(simplified) || byHeadword.get(traditional);
            }
            if (existing) {
                addOrMergeDefinition(existing, source);
                existing.sources = existing.sources || [];
                if (!existing.sources.some((item) => item.anchor === source.anchor)) {
                    existing.sources.push({
                        label: source.label,
                        anchor: source.anchor,
                    });
                }
                continue;
            }
            const key = `${simplified}|${traditional}|${match.reading}`;
            let entry = customByKey.get(key);
            if (!entry) {
                entry = {
                    simplified,
                    traditional,
                    pinyin: match.reading,
                    pinyin_taiwan: match.reading,
                    pinyin_no_tone: false,
                    pinyin_label: "",
                    english: "",
                    english_normalized: "",
                    sources: [],
                    definitions: [],
                    main: null,
                    forms: [],
                    freq: null,
                    taiwan_refs: {},
                };
                customByKey.set(key, entry);
                customOnly.push(entry);
            }
            addOrMergeDefinition(entry, source);
            if (!entry.sources.some((item) => item.anchor === source.anchor)) {
                entry.sources.push({
                    label: source.label,
                    anchor: source.anchor,
                });
            }
        }
        // Imported rows are appended after the server rows, but never past the
        // page limit. Fuzzy rows are already part of the page, so leave room
        // for them too. Definitions merged into existing rows do not consume a
        // slot; only custom-only rows do.
        const fuzzyRows = Array.isArray(data.fuzzy) ? data.fuzzy.length : 0;
        const room = Math.max(0, maxResults - entries.length - fuzzyRows);
        for (const entry of customOnly.slice(0, room)) entries.push(entry);
    }

    // --- Storage report ----------------------------------------------------

    async function sqliteStorageStats() {
        if (sqliteWorkerFailed) return { status: "unavailable" };
        if (!sqliteReady) {
            return {
                status:
                    sqlitePreparing || sqliteIndexPromise
                        ? "building"
                        : "unavailable",
            };
        }
        try {
            const response = await sqliteRequest({ type: "stats" });
            return {
                status: "ready",
                rows: response.rows,
                bytes: response.bytes,
            };
        } catch (error) {
            return { status: "unavailable" };
        }
    }

    async function browserStorageStats() {
        try {
            if (navigator.storage && navigator.storage.estimate) {
                return await navigator.storage.estimate();
            }
        } catch (error) {
            // Storage estimate unavailable.
        }
        return null;
    }

    function preferenceStats(storage) {
        let keys = 0;
        let bytes = 0;
        try {
            for (let index = 0; index < storage.length; index += 1) {
                const key = storage.key(index);
                if (!key || !key.startsWith("yabai-search-")) continue;
                const value = storage.getItem(key) || "";
                keys += 1;
                bytes += (key.length + value.length) * 2;
            }
        } catch (error) {
            // Storage unavailable.
        }
        return { keys, bytes };
    }

    function appendStorageItem(list, titleText, metaText, rows) {
        const item = document.createElement("div");
        item.className = "storage-item";
        const title = document.createElement("div");
        title.className = "storage-title";
        title.textContent = titleText;
        const meta = document.createElement("div");
        meta.className = "storage-meta";
        meta.textContent = metaText;
        item.append(title, meta);
        for (const [label, value] of rows) {
            const row = document.createElement("div");
            row.className = "storage-row";
            if (label === "Total") row.classList.add("storage-total-row");
            const name = document.createElement("span");
            name.textContent = label;
            const amount = document.createElement("span");
            amount.textContent = value;
            row.append(name, amount);
            item.append(row);
        }
        list.append(item);
    }

    async function openStorageDialog() {
        const dialog = document.getElementById("storage-dialog");
        const list = document.getElementById("storage-list");
        const totalNode = document.getElementById("storage-total");
        if (!dialog || !list) return;
        dialog.showModal();
        list.textContent = "Calculating...";
        if (totalNode) totalNode.textContent = "";

        if (dictionaries.some((dict) => dict.dataBytes == null)) {
            try {
                await loadTermIndex();
            } catch (error) {
                console.error(error);
            }
        }

        list.replaceChildren();
        if (!dictionaries.length) {
            const empty = document.createElement("p");
            empty.className = "storage-empty";
            empty.textContent = "No imported dictionaries.";
            list.append(empty);
        }

        let grandTotal = 0;
        for (const dict of dictionaries) {
            let dataBytes = dict.dataBytes;
            if (dataBytes == null) {
                const rows = termIndexByDict?.get(dict.id) || [];
                dataBytes = rows.reduce(
                    (sum, row) =>
                        sum +
                        utf8Length(row.term) +
                        utf8Length(row.reading) +
                        utf8Length(row.gloss) +
                        32,
                    0
                );
            }
            const bigramBytes = dict.bigramBytes || 0;
            const trigramBytes = dict.trigramBytes || 0;
            const dictTotal = dataBytes + bigramBytes + trigramBytes;
            grandTotal += dictTotal;

            const language =
                languages.find((item) => item.id === dict.languageId)?.name ||
                dict.languageId;
            appendStorageItem(
                list,
                dict.title,
                `${language} · ${dict.termCount.toLocaleString()} entries`,
                [
                    ["Dictionary data", formatBytes(dataBytes)],
                    [
                        dict.indexed
                            ? `Bigram index (${dict.bigramCount.toLocaleString()} grams)`
                            : "Bigram index",
                        dict.indexed ? formatBytes(bigramBytes) : "not indexed",
                    ],
                    [
                        dict.indexed
                            ? `Trigram index (${dict.trigramCount.toLocaleString()} grams)`
                            : "Trigram index",
                        dict.indexed ? formatBytes(trigramBytes) : "not indexed",
                    ],
                    ["Total", formatBytes(dictTotal)],
                ]
            );
        }

        const sqlite = await sqliteStorageStats();
        if (sqlite.status === "ready") {
            grandTotal += sqlite.bytes || 0;
            appendStorageItem(list, "SQLite search index", "OPFS · persistent", [
                ["Rows", (sqlite.rows || 0).toLocaleString()],
                ["Approximate size", formatBytes(sqlite.bytes || 0)],
            ]);
        } else if (sqlite.status === "building") {
            appendStorageItem(list, "SQLite search index", "OPFS · preparing", [
                ["Status", "Building"],
            ]);
        } else {
            appendStorageItem(list, "SQLite search index", "OPFS · unavailable", [
                ["Status", "Not available; using IndexedDB fallback"],
            ]);
        }

        const estimate = await browserStorageStats();
        if (estimate) {
            const rows = [
                ["Used", formatBytes(estimate.usage || 0)],
                ["Quota", formatBytes(estimate.quota || 0)],
            ];
            const details = estimate.usageDetails;
            if (details) {
                for (const key of [
                    "indexedDB",
                    "caches",
                    "serviceWorkerRegistrations",
                    "fileSystem",
                ]) {
                    if (typeof details[key] === "number") {
                        rows.push([
                            key === "indexedDB"
                                ? "IndexedDB"
                                : key === "fileSystem"
                                  ? "OPFS / files"
                                  : key,
                            formatBytes(details[key]),
                        ]);
                    }
                }
            }
            appendStorageItem(
                list,
                "Browser storage",
                location.origin,
                rows
            );
        } else {
            appendStorageItem(list, "Browser storage", location.origin, [
                ["Status", "Storage estimate unavailable"],
            ]);
        }

        const preferences = preferenceStats(localStorage);
        const sessionPreferences = preferenceStats(sessionStorage);
        grandTotal += preferences.bytes + sessionPreferences.bytes;
        appendStorageItem(
            list,
            "Preferences",
            "localStorage + sessionStorage",
            [
                [
                    "localStorage keys / size",
                    `${preferences.keys.toLocaleString()} · ${formatBytes(
                        preferences.bytes
                    )}`,
                ],
                [
                    "sessionStorage keys / size",
                    `${sessionPreferences.keys.toLocaleString()} · ${formatBytes(
                        sessionPreferences.bytes
                    )}`,
                ],
            ]
        );

        if (totalNode) {
            totalNode.textContent = `Tracked local data: ${formatBytes(
                grandTotal
            )}`;
        }
    }

    // --- Conversion backfill ----------------------------------------------

    let backfillPromise = null;

    function setBackfillStatus(text) {
        let node = document.getElementById("dict-backfill-status");
        if (!node) {
            node = document.createElement("div");
            node.id = "dict-backfill-status";
            node.className = "dict-backfill-status";
            document.body.append(node);
        }
        node.hidden = !text;
        node.textContent = text || "";
    }

    async function backfillConversions() {
        if (backfillPromise) return backfillPromise;
        const pending = buildableDictionaries().filter(
            (dictionary) => !dictionary.converted
        );
        if (!pending.length) return;
        backfillPromise = (async () => {
            try {
                for (let index = 0; index < pending.length; index += 1) {
                    const dictionary = pending[index];
                    const rows = await dbAllByIndex(
                        "entries",
                        "dictId",
                        dictionary.id
                    );
                    if (!rows.length) {
                        dictionary.converted = true;
                        await dbPut("dictionaries", dictionary);
                        continue;
                    }
                    const terms = [
                        ...new Set(rows.map((row) => row.term).filter(Boolean)),
                    ];
                    const converted = new Map();
                    for (let offset = 0; offset < terms.length; offset += 200) {
                        const chunk = terms.slice(offset, offset + 200);
                        setBackfillStatus(
                            `Preparing ${dictionary.title}: ` +
                                `${Math.min(offset + chunk.length, terms.length)}` +
                                ` / ${terms.length} headwords`
                        );
                        const results = await convertScripts(chunk);
                        chunk.forEach((term, i) => {
                            converted.set(term, results[i] || {});
                        });
                        await new Promise((resolve) => setTimeout(resolve, 0));
                    }
                    const updated = rows.map((row) => {
                        const values = converted.get(row.term) || {};
                        return {
                            ...row,
                            simplified: values.simplified || row.term,
                            traditional: values.traditional || row.term,
                            readingToneless: tonelessReading(row.reading),
                        };
                    });
                    await putTermRows(updated);
                    dictionary.converted = true;
                    await dbPut("dictionaries", dictionary);
                }
                invalidateTermIndex();
                invalidateSqliteSearch();
                resetScanWorker();
            } finally {
                backfillPromise = null;
                setBackfillStatus("");
            }
        })();
        return backfillPromise;
    }

    // --- Init --------------------------------------------------------------

    async function init() {
        // Install the import/add-language controls before touching IndexedDB.
        // A version upgrade can be blocked by another open tab; the controls
        // must still be available while that resolves.
        for (const column of document.querySelectorAll(".dict-column")) {
            addColumnPlus(column);
        }
        addLanguagePlusCell();

        await openDb();
        languages = await dbAll("languages");
        dictionaries = await dbAll("dictionaries");
        const importJobs = await dbAll("imports");
        const jobsById = new Map(
            importJobs.map((job) => [job.id, job])
        );
        // Load ZIP blobs only for jobs that still need them. Pulling every
        // stored ZIP at startup delays the dictionary menu on large imports.
        const filesById = new Map();
        for (const job of importJobs) {
            const entry = await dbGet("import-files", job.id);
            if (entry && entry.file) {
                filesById.set(job.id, entry.file);
            }
        }
        const resumeJobs = [];
        // Resume imports whose ZIP was saved. Anything else is marked
        // incomplete so the user can delete or re-import it.
        for (const dictionary of dictionaries) {
            if (!dictionary.importing) continue;
            const job = jobsById.get(dictionary.id);
            const file = filesById.get(dictionary.id);
            if (job && file) {
                resumeJobs.push(job);
            } else {
                dictionary.importing = false;
                dictionary.incomplete = true;
                dictionary.importProgress = "";
                await dbPut("dictionaries", dictionary);
            }
        }
        // Remove jobs whose dictionary was deleted or already finished.
        for (const job of importJobs) {
            const dictionary = dictionaries.find(
                (entry) => entry.id === job.id
            );
            if (!dictionary || !dictionary.importing) {
                await dbDelete("imports", job.id);
                await dbDelete("import-files", job.id);
            }
        }
        const storageClose = document.getElementById("storage-close");
        if (storageClose) {
            storageClose.addEventListener("click", () => {
                document.getElementById("storage-dialog")?.close();
            });
        }
        const storageLink = document.getElementById("dict-storage-link");
        if (storageLink) {
            storageLink.addEventListener("click", openStorageDialog);
        }

        for (const language of languages) {
            addLanguageColumn(language);
        }
        for (const dictionary of dictionaries) {
            // Importing dictionaries are shown by the resume loading row.
            if (dictionary.importing) continue;
            addDictionaryToLanguage(dictionary, false);
        }
        // Load the imported term index lazily on the first custom search.
        // Loading every imported dictionary at startup is expensive on phones.
        window.setupDictionaryOrdering?.();
        window.enforceSingleOpenDictColumn?.();
        window.syncGroups();
        if (buildableDictionaries().length) {
            sqlitePreparing = true;
            setTimeout(async () => {
                try {
                    if (
                        buildableDictionaries().some(
                            (dictionary) => !dictionary.converted
                        )
                    ) {
                        await backfillConversions();
                    }
                    await initializeSqliteSearch();
                } catch (error) {
                    console.error(error);
                } finally {
                    sqlitePreparing = false;
                }
            }, 800);
        }
        if (resumeJobs.length) {
            resumeImports(resumeJobs, filesById).catch((error) =>
                console.error(error)
            );
        }
    }

    window.addEventListener("pagehide", () => {
        try {
            localStorage.setItem(HAS_DICTS_KEY, String(dictionaries.length > 0));
        } catch (error) {
            // Storage unavailable - nothing to remember.
        }
        if (sqliteWorker) {
            sqliteWorker.terminate();
            sqliteWorker = null;
        }
        resetScanWorker();
    });

    // Expose the current imported metadata for debugging in the console.
    window.customDictsInfo = () => ({
        languages,
        dictionaries,
        terms: termIndex ? termIndex.length : 0,
    });

    // init() must run after the DOM is ready. When the script is injected
    // late (dataset.late = "1") the document is already loaded, so start
    // immediately; otherwise wait for DOMContentLoaded.
    function start() {
        init()
            .catch((error) => console.error(error))
            .finally(() => readyResolve());
    }
    if (LOADED_LATE || document.readyState !== "loading") {
        start();
    } else {
        document.addEventListener("DOMContentLoaded", start, { once: true });
    }
})();
