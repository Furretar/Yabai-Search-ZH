# Yabai-Chaci 牙白查词 / Yabai Search

A Chinese dictionary web app with Chinese-English, Chinese-Chinese, and Taiwanese Mandarin support. It is built on many open license dictionaries, such as CC-CEDICT and Wiktionary, with Taiwan
readings from the MOE 國語辭典簡編本.

## How does it work

### Search functions

#### English phrases
If you search with the English alphabet, the engine will first try to read it as phonetic pinyin. If the search doesn't match any pinyin readings, it will search for definitions containing the string. You can force an English phrase search with quotes "". Exact matches are shown first, then definitions containing the word, then definitions containing the string anywhere inside.

#### Pinyin
When searching with pinyin, you can specify tones with their corresponding numbers or leave it blank (shi2shi4 vs shishi). Searching pinyin will get exact matches at the top, then results starting with the query. All syllable combinations will be matched, for example, dangan will match both dan'gan (单干) and dang'an (档案).

You can also specify tones for certain characters, i.e. shi4shi will return any results for shi4 with any tone shi after it. Below that will be results starting with the query, then results containing the query anywhere inside. If there are no results for the specific tone combination, it will fallback to results ignoring your tone numbers. The ignored toneless results will be displayed under a section call "Fuzzy Results."

#### Wildcard
You can use * in searches as a wildcard, it mixes with tone pinyin queries too. For example, *字 will return all words ending with 字.

#### Bopomofo 
Bopomofo is converted to tonal pinyin first, so it is fully supported.

### Dictionaries and Sources

#### Taiwan (MOE) readings
Taiwanese Mandarin is mostly the same as Mainland Mandarin, with a handful of different pronunciations for words and characters, and slightly different word usage. 

- words in `_TAIWAN_EXCEPTIONS` get **none** (currently `啊`);
- a **monophonic** word (exactly one CEDICT reading) may take its whole-word
  MOE reading, even when it differs entirely (垃圾 → `lè sè`);
- a **polyphonic** word keeps its own reading - the MOE reading is keyed by word,
  not by sense, so using it wholesale would collapse 啊’s five readings into one
  or turn 呵[ā] into hē;
- otherwise, per-character derivation, and only for characters that are
  themselves monophonic in CEDICT and unambiguous in MOE.

Readings are attached to each entry (`pinyin_taiwan`) and to the words
referenced inside definitions (`taiwan_refs`), which render as furigana too.
## Tech Stack
Backend is FastAPI + SQLite (FTS5),
the frontend is a Jinja2 template plus plain JavaScript.

## How to run
uv sync
uv run python -m scripts.import_cedict     # builds data/cedict.sqlite (add --force to re-download)
uv run python -m scripts.import_moe        # builds data/moe.sqlite (Taiwan readings)
uv run uvicorn yabai_chaci.main:app --reload
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
