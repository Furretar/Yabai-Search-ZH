# Yabai-Search-ZH
牙白查词 / Yabai Search

A Chinese dictionary web app with Chinese-English, Chinese-Chinese, and Taiwanese Mandarin support. It is built on many open license dictionaries, such as CC-CEDICT and Wiktionary, with Taiwan
readings from the MOE 國語辭典簡編本.

## How does it work

### Language Codes
You can choose a language code from the drop-down menu on the left of the search bar. After selecting a language, results with definitions in the selected language will be shown first. Matching results in other languages will be shown in a section called "Other Dictionaries" below the main results. Searching with Taiwanese enabled will match to Taiwanese readings instead of of Mainland readings.

### Search functions

#### Search ordering
Searches have 6 levels of priority. Results within the same level will be sorted by frequency. By default searches will attempt to match headwords or readings. Definitions will be searched if there are no matching headwords or reading. Definitions can be searched by using quotes "". The top results will be exact matches for your search query. The 2nd level will be results that start with your search query. The 3rd level will be results that contain your query somewhere inside. The 4th level will be matches with the only listed reading in another language (i.e. Shanghainese, Wu, Xiang). The 5th level is all words from Wiktionary with a reading but no definition, and the 6th level is all words with no reading and no definition.

#### Pinyin
When searching with pinyin, you can specify tones with their corresponding numbers or leave it blank (shi2shi4 vs shishi). All syllable combinations will be matched, for example, dangan will match both dan'gan (单干) and dang'an (档案). You can also specify tones for specific characters, i.e. shi4shi will return any results for shi4 with any tone shi after it. There is a section called Fuzzy Matches below the main results with entries ignoring the pitch specifications.

#### Bopomofo 
Bopomofo is converted to tonal pinyin first, so it is fully supported.

#### Wildcard
You can use * in searches as a wildcard, it mixes with tone pinyin queries too. For example, *字 will return all words ending with 字.

### Dictionaries and Sources

#### Taiwan (MOE) readings
Taiwanese Mandarin is mostly the same as Mainland Mandarin, with a handful of different pronunciations for words and characters, and slightly different word usage. 

The Taiwanese Ministry of Education publishes dictionaries of Taiwanese Mandarin. 2 of these are used as sources for Yabai Search, the concise version 國語辭典簡編本, and the comprehensive version 重編國語辭典修訂本.

When the Taiwanese pronunciation toggle is turned on, matched readings from the Taiwanese dictionaries are shown instead. If there are no matches in Taiwanese Dictionaries but CEDICT lists a Taiwanese Pronunciation, the CEDICT Taiwanese pronunciation is used.

There are many characters in Taiwanese Mandarin that are always pronounced a certain way. There is not a detailed list of these, so all single character words with only 1 listed reading have their readings mapped onto all characters that only have 1 listed reading in the Chinese-English dictionaries. This method has quite high accuracy, but the rest of the false positives and false negatives will need to be manually fixed over time. 

#### English
CEDICT has separate entries for proper nouns and normal words. For example, the word/name 阮. Since search entries are based on headwords, these are collapsed into 1 entry.

## Tech Stack
Backend is FastAPI + SQLite (FTS5),
the frontend is a Jinja2 template plus plain JavaScript.

## Performance

Searches are read-only and every database is rebuilt by an importer, so the
expensive work happens once at import time instead of on every query.

### Import-time precomputation

- **Wiktionary readings** - each reading stores its spaced form
  (`pinyin_spaced`), numeric form (`pinyin_numeric`), simplified headword
  (`simplified`), and whether CEDICT already covers it (`is_cedict`). The
  deduplicated Mandarin readings CEDICT does not cover live in
  `readings_extra`, so pinyin searches never touch the full readings table.
- **CEDICT alternate pronunciations** - the `pr. [...]` notes are extracted
  into `alt_prons(entry_id, text)`; pinyin searches match that small table
  instead of scanning every definition.
- **Frequency ranks** - `entries.freq_rank` is filled from SUBTLEX-CH at import,
  replacing a correlated subquery in every `ORDER BY`.
- **Definition trigram indexes** - `senses_fts` (MOE) and `wiktionary_fts`
  (Wiktionary) use FTS5's trigram tokenizer, which lets `LIKE '%...%'` use an
  index for queries of three or more characters.
- **Han grams** - `definition_grams.sqlite` stores every Han unigram and bigram
  seen in a definition. If a gram of the query is missing, the substring cannot
  occur, so the definition scan is skipped.
- **Gram inverted index** - `gram_words.sqlite` maps each Han unigram/bigram to
  the words whose headword, gloss or sense contains it. A 1-2 character CJK
  query becomes one lookup plus a LIKE check on those candidates, instead of
  scanning the tables.
- **English words** - `english_words.sqlite` stores every whole word found in a
  definition; the "search the English word" hint is shown only for those words.

### Query-time work

- Plain pinyin lookups are ranked and limited in SQL; only the page's candidates
  reach Python. Tonal lookups fetch prefix matches first and only scan the
  "contains" rows when the page is not full yet.
- Connections are pooled read-only connections (`mode=ro`, mmap, page cache) and
  are reused for the life of the process. A restart is required after
  re-importing a database.
- Stable caches (CEDICT headwords and syllables, the MOE maps, the all-headwords
  list, the IDS scan, the OpenCC converters) are built at startup.
- Pure helpers (`to_simplified`, `to_traditional`, `to_toneless`, `lookup`, ...)
  are memoized. The segmenter (jieba) and the gram list are loaded at startup.
- Every search records each SQL statement's duration and row count in
  `data/search.log`, which keeps the last 10 complete searches. It is on by
  default; set `YABAI_SQL_LOG=0` to disable it.

### Progressive results

The first batch is rendered as soon as the fast CEDICT tier returns, provided it
has at least one result. The complete list replaces it when the slower source
tier arrives. The result count keeps both times, for example
`36 RESULTS (32 ms, 1788 ms)`.

### Data files and rebuild order

Generated files under `data/` (all gitignored): `cedict.sqlite`, `moe.sqlite`,
`wiktionary.sqlite`, `subtlex.sqlite`, `english_words.sqlite`, and
`definition_grams.sqlite`, and `gram_words.sqlite`. Rebuild in this order:

```sh
uv run python -m scripts.import_subtlex     # first: CEDICT copies its ranks
uv run python -m scripts.import_cedict
uv run python -m scripts.import_moe
uv run python -m scripts.import_wiktionary  # needs cedict.sqlite
uv run python -m scripts.import_english_words
uv run python -m scripts.import_definition_grams
uv run python -m scripts.import_gram_words
```

The Wiktionary importer fails loudly if CEDICT is missing, because its
precomputed coverage flags and syllable split depend on it.

## How to run
nix develop -c uv run uvicorn yabai_chaci.main:app --reload --host 0.0.0.0 --port 8000


Serve at <http://127.0.0.1:8000>.


db columnsa re `simplified`, `traditional`, `pinyin` (tone-marked),
`english`, `english_normalized`, `pinyin_taiwan`, `taiwan_refs` when Taiwan readings were attached.


## Data model

Each dictionary lives in its own SQLite database under `data/` and is read the
same way, so a source is interchangeable:

- a **headword table** (at least a `word` column) - used for the source chips;
- **`senses(word, position, text)`** - the definition split into numbered items;
- **`classifiers(word, traditional, simplified, codes, note)`** - measure words;
- **`forms(word, traditional, simplified, pinyin)`** - variant forms (below);
- **`meta(key, value)`** - `updated`, shown on the About page.

The databases are `cedict.sqlite` (CC-CEDICT, table `entries` + FTS5
`entries_fts`), `moe.sqlite` (MOE 國語辭典簡編本, table `moe`), and
`wiktionary.sqlite` (kaikki.org / Wiktextract). They’re registered in
`_SOURCE_QUERIES` (`yabai_chaci/db.py`).

### Variant forms (`forms`)

CEDICT has entries whose entire definition is a cross-reference, e.g.
`血 血 [xiě] /see 血[xuè]/` or `輭 软 [ruǎn] /variant of 軟|软[ruǎn]/`. These are
*not* senses: the importer recognises `see X` / `variant of X` (`see_target`) and
stores the entry in the `forms` table (keyed by the **traditional** forms, which
stay distinct even when simplified collide), keeping the variant's own reading.
The UI lists them as **Other forms:** (`<form> [<reading>]`) instead of numbered
definitions, so a `xuè` result shows `Other forms: 血 [xiě]` rather than
`1. see 血[xuè]`, and `软` shows once with `Other forms: 輭 [ruǎn]`.

## Helper concepts

- **pinyin / toneless** - `to_toneless` strips tone marks, tone digits and
  spaces to a search key: `hàn yǔ` or `han4 yu3` → `hanyu`.
- **marked ⇄ numeric** - `numeric_to_marked` (`han4 yu3` → `hàn yǔ`) and its
  inverse `to_numeric`.
- **wildcards** - `_like_pattern` turns the user’s `*` into SQL `%`, escaping
  literal `%`, `_` and `\`.
- **CJK check** - `_has_cjk` (does a string contain a hanzi?).
- **simplified conversion** - `_simplify_map` learns traditional→simplified from
  CEDICT’s single-character entries; latin queries are converted too.
- **furigana** - the small pinyin/zhuyin annotation shown above each character
  (per character when counts line up, otherwise as one line above the word).

## Worked examples

| query | branch | result / notes |
|-------|--------|----------------|
| `"side"` | quoted | English whole-word phrase |
| `ㄉㄢㄍㄢˋ` | bopomofo → tonal | 单干 |
| `shi4` | tonal | whole readings first (世, 事…), then starts-with, then contains |
| `shi2shi` | tonal | any `shi` with 2nd tone first: 实事, 实时… |
| `ban4gongshi4` | tonal | 办公室 |
| `dan'gan` | toneless | 单干, 胆敢 (not 档案) |
| `dang'an` | toneless | 档案 |
| `dangan` | toneless | 单干, 档案, 胆敢 (no boundary given) |
| `*xinu*` | toneless + wildcard | 息怒, 喜怒哀乐, 喜怒无常… |
| `*xi3nu*` | tonal + wildcard | 喜怒哀乐, 喜怒无常 (3rd tone required) |
| `*xi2nu*` | tonal + wildcard | (none) |
| `su4zhi2` | tonal | none on Mainland → Fuzzy: 素质 |
| `su4zhi2` + Taiwan | tonal | 素质 (Taiwan `sù zhí` = `su4zhi2`) |
| `lese` + Taiwan | toneless | 垃圾 (Taiwan `lè sè`) |
| `yabai` | toneless | 牙白 |
| `word` | English | 词, 单词… (the latin slang headword “word” is ignored) |
| `a` | toneless | 啊, 呵, 吖… (tone-insensitive reading) |
| `国家` | Chinese word | 国家, 国家级, 国家队… |
