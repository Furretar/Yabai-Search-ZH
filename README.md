牙白查词 / Yabai Search

A Chinese dictionary web app with Chinese-English, Chinese-Chinese, and Taiwanese Mandarin support.

The interface design was inspired by [jisho.org](https://jisho.org/).

## Search tips

| Search for | Try | Finds |
|---|---|---|
| Chinese | `国家` or `國家` | 国家, 国家级, 发展中国家… |
| English | `house` | 房子, 屋… |
| Pinyin | `side` | 四德... |
| English phrase | `"side"` | entries containing the English word "side" |
| Pinyin, any tone | `dangan` | 档案, 单干, 胆敢 |
| Syllable break | `dang'an` | 档案 only |
| Pinyin with tones | `shi4shi2` | 事实 |
| Pinyin, some tones | `shi4shi` | any tone on the second syllable |
| Wildcard | `*字` | words ending in 字 |
| Wildcard with tones | `*xinu4*` | 息怒, 喜怒哀乐 |
| Zhuyin (Bopomofo) | `ㄉㄢㄍㄢˋ` | 单干 |

<h2 id="sources">Sources &amp; Licenses</h2>

### English

- <span id="cedict"></span>**[CC-CEDICT](https://www.mdbg.net/chinese/dictionary?page=cedict)** 125,271 entries · CC BY-SA 4.0 · Updated 2026-10-23
    - An open-source Chinese-English dictionary project.

- <span id="wiktionary"></span>**[Wiktionary](https://en.wiktionary.org/)** 272,848 entries · CC BY-SA 4.0 / GFDL · Updated 2026-10-18
    - Chinese entries from the Wiktionary. English definitions, usage notes and etymology, from [kaikki.org](https://kaikki.org/dictionary/Chinese/).

### Mainland

- <span id="zhwiktionary"></span>**[中文维基词典](https://zh.wiktionary.org/)** 55,538 entries · CC BY-SA 4.0 · Updated 2026-10-18
    - Chinese-language definitions from the community-built Chinese Wiktionary.

### Taiwan

- <span id="moe"></span>**[《國語辭典簡編本》](https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/dict_concised_download.html)** 45,178 entries · CC BY-ND 3.0 TW · Updated 2026-09-29
    - Taiwan's Ministry of Education concise Mandarin dictionary - Taiwanese pronunciations and monolingual definitions written in Chinese.

- <span id="moe_revised"></span>**[《重編國語辭典修訂本》](https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/dict_reviseddict_download.html)** 163,909 entries · CC BY-ND 3.0 TW · Updated 2026-09-29
    - Taiwan's Ministry of Education comprehensive Mandarin dictionary - fuller definitions and additional Taiwanese readings.

### Japanese

- <span id="jpwiktionary"></span>**[ウィクショナリー](https://ja.wiktionary.org/)** 16,342 entries · CC BY-SA 4.0 · Updated 2026-10-18
    - Chinese headwords with Japanese-language definitions from Japanese Wiktionary.

### Frequency

- <span id="subtlex"></span>**[SUBTLEX-CH](https://www.ugent.be/pp/experimentele-psychologie/en/research/documents/subtlexch)** 99,121 words · CC BY 4.0 · Updated 2026-10-16
    - Chinese word frequencies from film and TV subtitles (Cai & Brysbaert, 2010), used to rank results.

### Vocabulary

- <span id="hsk"></span>**[HSK Vocabulary](https://github.com/drkameleon/complete-hsk-vocabulary)** 11,470 words · MIT · Updated 2026-10-19
    - HSK 2.0, HSK 3.0, and 2026 syllabus vocabulary levels, shown next to the frequency rank.

### Software

- <span id="opencc"></span>**[OpenCC](https://github.com/BYVoid/OpenCC)** · Apache-2.0. Simplified and Traditional Chinese conversion (used to match and display both scripts).
- <span id="jieba"></span>**[jieba](https://github.com/fxsjy/jieba)** · MIT. Chinese word segmentation (used to split text into words and syllables for search and furigana).

## Contact

You can email me at wyattpetrock2@gmail.com or woahzers@proton.me. Or add furretar2 on discord.

## How does it work

### Language Codes
You can choose a language code from the drop-down menu on the left of the search bar. After selecting a language, results with definitions in the selected language will be shown first. Matching results in other languages will be shown in a section called "Other Dictionaries" below the main results. Searching with Taiwanese enabled will match to Taiwanese readings instead of of Mainland readings.

### Search functions

#### Search ordering
Searches have 6 levels of priority. Results within the same level will be sorted by frequency. By default searches will attempt to match headwords or readings. Definitions will be searched if there are no matching headwords or reading; when headword or reading matches exist, only those results are shown and definition-only matches are hidden. The "Fuzzy match" section is reserved for tone-insensitive results. Definitions can be searched by using quotes "". The top results will be exact matches for your search query. The 2nd level will be results that start with your search query. The 3rd level will be results that contain your query somewhere inside. The 4th level will be matches with the only listed reading in another language (i.e. Shanghainese, Wu, Xiang). The 5th level is all words from Wiktionary with a reading but no definition, and the 6th level is all words with no reading and no definition.

#### Pinyin
When searching with pinyin, you can specify tones with their corresponding numbers or leave it blank (shi2shi4 vs shishi). All syllable combinations will be matched, for example, dangan will match both dan'gan (单干) and dang'an (档案). You can also specify tones for specific characters, i.e. shi4shi will return any results for shi4 with any tone shi after it. Tone-insensitive entries are shown in a "Fuzzy match" section below the main results, both when the tonal search finds nothing and alongside tonal results.

#### Bopomofo 
Bopomofo is converted to tonal pinyin first, so it is fully supported.

#### Wildcard
You can use * in searches as a wildcard, it mixes with tone pinyin queries too. For example, *字 will return all words ending with 字. Use ? for exactly one character: 国家? matches 国家级 but not 国家级旅游度假区. In pinyin, ? matches exactly one syllable: guojia? matches 国家级 but not 国家.

### Dictionaries and Sources

#### Mainland (Chinese Wiktionary)
Chinese Wiktionary ([zh.wiktionary.org](https://zh.wiktionary.org/)) provides
Chinese-language definitions for Mandarin headwords. Its data is licensed
CC BY-SA 4.0.

#### Japanese
Japanese Wiktionary (`ja.wiktionary.org`) gives Chinese headwords Japanese-language
definitions under `=={{L|zh}}==`. They are imported as a separate dictionary
source (CC BY-SA 4.0).

#### Taiwan (MOE) readings
Taiwanese Mandarin is mostly the same as Mainland Mandarin, with a handful of different pronunciations for words and characters, and slightly different word usage. 

The Taiwanese Ministry of Education publishes dictionaries of Taiwanese Mandarin. 2 of these are used as sources for Yabai Search, the concise version 國語辭典簡編本, and the comprehensive version 重編國語辭典修訂本.

When the Taiwanese pronunciation toggle is turned on, matched readings from the Taiwanese dictionaries are shown instead. If there are no matches in Taiwanese Dictionaries but CEDICT lists a Taiwanese Pronunciation, the CEDICT Taiwanese pronunciation is used.

There are many characters in Taiwanese Mandarin that are always pronounced a certain way. There is not a detailed list of these, so all single character words with only 1 listed reading have their readings mapped onto all characters that only have 1 listed reading in the Chinese-English dictionaries. This method has quite high accuracy, but the rest of the false positives and false negatives will need to be manually fixed over time. 

#### English
CEDICT has separate entries for proper nouns and normal words. For example, the word/name 阮. Since search entries are based on headwords, these are collapsed into 1 entry.

#### Tech Stack
Backend is FastAPI + SQLite (FTS5),
the frontend is a Jinja2 template plus plain JavaScript.

#### Performance

- Definition trigram indexes - `senses_fts` (MOE) and `wiktionary_fts`
  (Wiktionary) use FTS5's trigram tokenizer, which lets `LIKE '%...%'` use an
  index for queries of three or more characters.
- Han grams - `definition_grams.sqlite` stores every Han unigram and bigram
  seen in a definition. If a gram of the query is missing, the substring cannot
  occur, so the definition scan is skipped.
- Gram inverted index - `gram_words.sqlite` maps each Han unigram/bigram to
  the words whose headword, gloss or sense contains it. A 1-2 character CJK
  query becomes one lookup plus a LIKE check on those candidates, instead of
  scanning the tables.
- English words - `english_words.sqlite` stores every whole word found in a
  definition; the "search the English word" hint is shown only for those words.
- The first batch is rendered as soon as the fast CEDICT tier returns, provided it
has at least one result. The complete list replaces it when the slower source
tier arrives. The result count keeps both times, for example
`36 RESULTS (32 ms, 1788 ms)`.

#### Variant forms

CEDICT has entries whose entire definition is a cross-reference, e.g. `血 血 [xiě] /see 血[xuè]/`.
The UI lists them under **Other forms:** and combines all duplicates.
