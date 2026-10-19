// Shared custom-dictionary search logic. The browser worker passes a
// sqlite-wasm DB object here; the Node regression test passes a node:sqlite
// adapter with the same selectObjects() method.

export const ROW_LIMIT = 50;
export const HAN_RE = /\p{Script=Han}/u;

export function normalize(value) {
    return String(value || "")
        .normalize("NFKC")
        .toLowerCase()
        .replace(/ü/g, "u")
        .normalize("NFD")
        .replace(/\p{M}/gu, "")
        .replace(/[^\p{Script=Han}a-z0-9\s]/gu, "")
        .trim();
}

// Every unique Han unigram and bigram from the headword forms.
export function rowNgrams(row) {
    const grams = new Set();
    const forms = new Set([
        row.term,
        row.simplified || row.term,
        row.traditional || row.term,
    ]);
    for (const form of forms) {
        if (!form || !HAN_RE.test(form)) continue;
        const chars = [...form];
        for (let i = 0; i < chars.length; i += 1) {
            if (!HAN_RE.test(chars[i])) continue;
            grams.add(chars[i]);
            if (i + 1 < chars.length && HAN_RE.test(chars[i + 1])) {
                grams.add(chars[i] + chars[i + 1]);
            }
        }
    }
    return [...grams].sort();
}

// Statement-based helper kept for the Node regression adapter.
export function addRowNgrams(row, entryId, statement) {
    for (const gram of rowNgrams(row)) {
        statement.bind([gram, entryId]);
        statement.step();
        statement.reset();
    }
}

export function compareMatches(a, b) {
    if (a.rank !== b.rank) return a.rank - b.rank;
    // Length before dictionary order keeps one large dictionary from filling
    // every substring slot before the others are considered.
    const length = String(a.term).length - String(b.term).length;
    if (length) return length;
    if (a.dictOrder !== b.dictOrder) return a.dictOrder - b.dictOrder;
    return String(a.term).localeCompare(String(b.term));
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

export function searchEntries(db, message) {
    const raw = normalize(message.query);
    if (!raw) return { rows: [], timings: [], candidates: 0 };
    const dictIds = (message.dictIds || []).filter(Boolean);
    if (!dictIds.length) return { rows: [], timings: [], candidates: 0 };
    const placeholders = dictIds.map(() => "?").join(",");
    const dictOrder = new Map(dictIds.map((id, index) => [id, index]));
    const han = HAN_RE.test(raw);
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
    const qualifiedColumns = columns
        .split(",")
        .map((column) => `e.${column}`)
        .join(",");
    const matches = new Map();
    const limit = ROW_LIMIT;
    const timings = [];
    const run = (sql, params, rank) => {
        const started = performance.now();
        const rows = db.selectObjects(sql, params || []);
        timings.push({
            tier: rank,
            ms: Math.round((performance.now() - started) * 10) / 10,
            rows: rows.length,
        });
        addMatches(matches, rows, rank, dictOrder);
    };
    const upper = raw + "\u{10FFFF}";

    // Tier 1: exact match. Query each field separately so SQLite can use the
    // composite (dict_id, field) indexes; an OR across fields does not.
    if (han) {
        for (const field of ["simplified", "traditional", "term"]) {
            run(
                `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND ${field} = ? LIMIT ?`,
                [...dictIds, raw, limit],
                1
            );
        }
    } else {
        for (const field of ["reading_toneless", "term"]) {
            run(
                `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND ${field} = ? LIMIT ?`,
                [...dictIds, raw, limit],
                1
            );
        }
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

    // Tier 3: headword substring. Ngrams are used when the backfill is ready;
    // otherwise fall back to the slower LIKE scan.
    if (matches.size < limit) {
        if (han && message.ngramsReady) {
            const chars = [...raw];
            if (chars.length === 1) {
                const perDictCandidates = Math.max(limit * 4, 200);
                for (const dictId of dictIds) {
                    run(
                        `SELECT ${qualifiedColumns}
                         FROM entries e
                         JOIN ngrams n ON n.entry_id = e.id
                         WHERE n.gram = ? AND e.dict_id = ?
                         ORDER BY length(e.term), e.id
                         LIMIT ?`,
                        [raw, dictId, perDictCandidates],
                        3
                    );
                }
            } else {
                const gramSet = new Set();
                for (let i = 0; i + 1 < chars.length; i += 1) {
                    if (HAN_RE.test(chars[i]) && HAN_RE.test(chars[i + 1])) {
                        gramSet.add(chars[i] + chars[i + 1]);
                    }
                }
                const grams = [...gramSet];
                if (grams.length) {
                    const gramPlaceholders = grams.map(() => "?").join(",");
                    run(
                        `SELECT ${columns} FROM entries WHERE id IN (
                            SELECT entry_id FROM ngrams
                            WHERE gram IN (${gramPlaceholders})
                            GROUP BY entry_id
                            HAVING COUNT(DISTINCT gram) = ?
                         ) AND dict_id IN (${placeholders})
                           AND (term LIKE ? OR simplified LIKE ? OR traditional LIKE ?)
                         LIMIT ?`,
                        [
                            ...grams,
                            grams.length,
                            ...dictIds,
                            `%${raw}%`,
                            `%${raw}%`,
                            `%${raw}%`,
                            limit,
                        ],
                        3
                    );
                } else {
                    run(
                        `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND (term LIKE ? OR simplified LIKE ? OR traditional LIKE ?) LIMIT ?`,
                        [...dictIds, `%${raw}%`, `%${raw}%`, `%${raw}%`, limit],
                        3
                    );
                }
            }
        } else if (han) {
            run(
                `SELECT ${columns} FROM entries WHERE dict_id IN (${placeholders}) AND (term LIKE ? OR simplified LIKE ? OR traditional LIKE ?) LIMIT ?`,
                [...dictIds, `%${raw}%`, `%${raw}%`, `%${raw}%`, limit],
                3
            );
        }
    }

    const result = [...matches.values()]
        .sort(compareMatches)
        .slice(0, limit);
    return {
        rows: result.map((row) => ({
            dictId: row.dict_id,
            term: row.term,
            simplified: row.simplified,
            traditional: row.traditional,
            reading: row.reading,
            definition: row.definition,
            rank: row.rank,
        })),
        timings,
        candidates: matches.size,
    };
}

// Definition and pinyin substring matches are a second, abortable request.
// The scan runs in id-range chunks and yields between them.
export async function searchChunkedEntries(db, message, mode, isAborted) {
    const raw = normalize(message.query);
    if (!raw) return { rows: [], timings: [] };
    const dictIds = (message.dictIds || []).filter(Boolean);
    if (!dictIds.length) return { rows: [], timings: [] };
    const wanted = Math.max(
        0,
        Math.min(ROW_LIMIT, Number(message.limit) || ROW_LIMIT)
    );
    if (!wanted) return { rows: [], timings: [] };
    const isPinyin = mode === "pinyin";
    const rank = isPinyin ? 3 : 4;
    const condition = isPinyin
        ? "(reading_toneless LIKE ? OR term LIKE ?)"
        : "definition LIKE ?";
    const conditionParams = isPinyin
        ? [`%${raw}%`, `%${raw}%`]
        : [`%${raw}%`];
    const placeholders = dictIds.map(() => "?").join(",");
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
    const skip = new Set(message.skipKeys || []);
    const bounds = db.selectObjects(
        `SELECT MIN(id) AS minId, MAX(id) AS maxId
         FROM entries WHERE dict_id IN (${placeholders})`,
        dictIds
    )[0];
    if (!bounds || bounds.minId === null || bounds.minId === undefined) {
        return { rows: [], timings: [] };
    }
    const minId = Number(bounds.minId);
    const maxId = Number(bounds.maxId);
    const rows = [];
    const startedAt = performance.now();
    let cursor = minId - 1;
    const chunkSize = 25000;
    while (cursor < maxId && rows.length < wanted) {
        if (isAborted()) return null;
        const end = Math.min(cursor + chunkSize, maxId);
        const chunk = db.selectObjects(
            `SELECT ${columns} FROM entries
             WHERE id > ? AND id <= ? AND dict_id IN (${placeholders})
               AND ${condition}
             LIMIT ?`,
            [cursor, end, ...dictIds, ...conditionParams, wanted - rows.length]
        );
        for (const row of chunk) {
            const key = `${row.dict_id}|${row.term}|${row.reading}`;
            if (skip.has(key)) continue;
            rows.push(row);
        }
        cursor = end;
        await new Promise((resolve) => setTimeout(resolve, 0));
    }
    if (isAborted()) return null;
    return {
        rows: rows.slice(0, wanted).map((row) => ({
            dictId: row.dict_id,
            term: row.term,
            simplified: row.simplified,
            traditional: row.traditional,
            reading: row.reading,
            definition: row.definition,
            rank,
        })),
        timings: [
            {
                tier: rank,
                ms: Math.round((performance.now() - startedAt) * 10) / 10,
                rows: rows.length,
            },
        ],
    };
}
