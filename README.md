## Introduction

A Chinese dictionary web app with Chinese-English, Chinese-Chinese, and Taiwanese Mandarin support.

The interface design was inspired by [jisho.org](https://jisho.org/), since it has a wonderful and clean design.

---

## Contact

You can email me at `wyattpetrock2@gmail.com`.

Or add `furretar2` on Discord.

---

## Quick Tips

<span id="search-tips"></span>

| Search with | Example | Matches |
|---|---|---|
| Chinese | `国家` or `國家` | 国家, 国家级, 发展中国家… |
| English | `house` | 房子, 屋… |
| Pinyin | `side` | 四德… |
| English phrase | `"side"` | Entries containing the English word "side" |
| Pinyin, any tone | `dangan` | 档案, 单干, 胆敢 |
| Syllable break | `dang'an` | 档案 only |
| Pinyin with tones | `shi4shi2` | 事实 |
| Pinyin, some tones | `shi4shi` | Any tone on the second syllable |
| Single Character Wildcard | `国家?` or `guo2jia1?` | 国家级, 国家队 |
| Multiple Character Wildcard | `国家*` | 国家, 国家安全部… (All words starting with 国家) |
| Every entry | `*` | All headwords, by tier then frequency |
| Zhuyin (Bopomofo) | `ㄉㄢㄍㄢˋ` | 单干 |


---


## Search Documentation

### Step 1: Search Settings

#### Language Codes

The language of the search results is decided by the language code. You can choose a language code from the drop-down menu on the left of the search bar.

After selecting a language, results with definitions in the selected language will be shown first. Matching results in other languages will be shown in a section called "Other Dictionaries" below the main results.

Searching with `Taiwanese readings` enabled will match to Taiwanese readings instead of of Mainland readings.

#### Custom Dictionaries

`Custom Dictionaries` can be enabled in the `Search Options` drop-down. When enabled, you will be able to choose exactly which dictionaries you want to see for any language. 

The order of dictionaries within each language can be changed by dragging them up or down.

You can import [Yomitan](https://yomitan.wiki/) dictionaries by clicking the `+` button at the bottom of an expanded language category. They will be stored in your browser's local storage.

The language code will still decide which language appears at the top of each entry.


### Step 2: Queries

When making a search, two requests are sent. The first request is meant to populate results quickly by getting all tier 1 results first. The second request takes longer, but gets all matching entries. When searching with a wildcard (`?`, `*`), only 1 request is sent.

#### CJK Searches
1. Searches containing Chinese characters or valid pinyin will first query all selected dictionaries for headwords that start with the query string. This is a very fast search and usually only takes a few milliseconds. 
2. The second request will find all headwords in all dictionaries that contain the query string anywhere inside.

If a search contains a Chinese character, and is more than 40 characters long, search will be skipped and the text will be parsed instead. If the CJK-containing search has no results, it will fallback to parsing. 

CJK Searches have 3 categories and 6 tiers of priority. Results within the same category are sorted by tier, and tiers are sorted by frequency. Locally imported dictionaries will mess up the sorting because the server cannot see their results.

The 3 categories in order are:

1. Results with at least 1 definition in any dictionary in the currently selected language
    - When Custom Dictionaries is enabled, this includes all selected dictionaries

2. Results with matches only in dictionaries in other languages
    - When Custom Dictionaries is enabled, this includes all unselected dictionaries

3. Fuzzy matches when searching with tones and pinyin (`shi2shi4`)

The 6 tiers are:

1. Exact matches for your search query
2. Results that start with your search query
3. Results that contain your query somewhere inside
4. Matches with the only listed reading in another language (i.e. Shanghainese, Wu, Xiang)
5. All words from Wiktionary with a reading but no definition
6. All words with no reading and no definition

The separator line below each entry will glow a certain color when hovered according to which tier it is. I implemented this to help debug but I thought it looked nice so I kept it. The colors in order are red, yellow, green, blue, purple, pink. Wildcard searches with Chinese characters use the same colors (see Wildcard below); pinyin wildcard searches are all red.

#### English / Definition Searches

You can also search for English words and phrases in the search bar. A search will match dictionary definitions if it doesn't match any pinyin readings, or if the query was surrounded by quotes `""`.

Definition searches have 6 tiers:
1. Entries with Latin text inside a Chinese headword (e.g. `word姐` for `word`)
2. Entries with an exact matching definition (articles stripped, so `speak` still matches `to speak` etc)
3. Entries containing the query in their definitions, excluding notes
4. Entries containing the query in their definitions, only inside notes/parentheses
5. Entries containing the substring (e.g. `word` matches `sword`)
6. Entries containing the query in a reference (`variant of...`)

#### Pinyin

When searching with pinyin, you can specify tones with their corresponding numbers or leave it blank (`shi2shi4` vs `shishi`).

All syllable combinations will be matched. For example, `dangan` will match both dan'gan (单干) and dang'an (档案).

You can also specify tones for specific characters, i.e. `shi4shi` will return any results for shi4 with any tone shi after it.

Tone-insensitive entries are shown in a `Fuzzy match` section below the main results, both when the tonal search finds nothing and alongside tonal results.

#### Zhuyin / Bopomofo

Zhuyin is converted to tonal pinyin first, so it is fully supported. Enabling zhuyin will convert all pinyin readings to zhuyin.


#### Wildcard

A wildcard is a placeholder for normal text, `?` is a single character wildcard, and `*` is a multiple character wildcard.

Use `?` to match exactly 1 character: `国家?` matches `国家级`, and `?怒??` matches `喜怒哀乐`.

It works with pinyin too: `guojia?` matches `国家级`.

Use `*` to match any number of characters: `国家*` matches `国家`, `国家级`, `国家安全部`, etc.

With Chinese characters, wildcard results are ranked like any other search. Tier 1 (exact) is a word that fits the whole pattern: `*后` matches every word that ends in 后, and `?后` matches every 2-character word that ends in 后. Tier 2 is a word that starts with something that fits the pattern, so 售后服务 comes after the exact matches for both `?后` and `*后`. Tier 3 is a word that has a match later inside it. Each tier is sorted by frequency.

A bare `*` fits every headword, so tiers 1-3 do not separate anything: every ordinary result is tier 1. Tiers 4-6 still apply, so a dialect-only reading, a word with no definition, or a word with no reading is demoted below the rest. Results are sorted by tier first, then by frequency. Words missing from the frequency list come last.

### Step 3: Formatting Results

#### Simplified and Traditional

Some characters were combined when converting from Traditional to Simplified. For example, `干`, `幹`, and `乾` all simplified into `干`. These are all displayed as different entries in traditional mode, but are combined in simplified mode. However, the definitions are kept separate with headers showing the traditional version that they apply to. You can see this [here](https://yabaisearch.org/?q=干&script=simplified&tone=1&phonetic=pinyin&taiwan=0&custom=0&compact=1&lang=TW).

Taiwanese definitions will also stay in traditional Chinese even in simplified mode.

#### CC-CEDICT

CEDICT has separate entries for proper nouns and normal words. For example, the word/name 阮. The custom dictionary toggle is reliant on standardization of headwords into entries, so these are merged into 1 entry. Within the merged entry there are headers that show which traditional character applies to which meaning. This can be seen [here](https://yabaisearch.org/?q=%E5%8F%AA&script=simplified&tone=1&phonetic=pinyin&taiwan=0&custom=0&compact=1&lang=EN).

CEDICT has entries that are cross-references, e.g. `血 [xiě] /see 血[xuè]/`. The UI lists them under **Other forms:** and merges all duplicates.

<h2 id="sources">Sources &amp; Licenses</h2>

#### Taiwan (MOE) readings

Taiwanese Mandarin is mostly the same as Mainland Mandarin, with a handful of different pronunciations for words and characters, and slightly different word usage.

The Taiwanese Ministry of Education publishes dictionaries of Taiwanese Mandarin. 2 of these are used as sources for Yabai Search: the concise version `國語辭典簡編本`, and the comprehensive version `重編國語辭典修訂本`.

When the Taiwanese pronunciation toggle is turned on, matched readings from the Taiwanese dictionaries are shown instead. If there are no matches in Taiwanese Dictionaries but CEDICT lists a Taiwanese Pronunciation, the CEDICT Taiwanese pronunciation is used.

There are many characters in Taiwanese Mandarin that are always pronounced a certain way. There is not a detailed list of these, so all single character words with only 1 listed reading have their readings mapped onto all characters that only have 1 listed reading in the Chinese-English dictionaries.

This method is mostly accurate, but the Taiwanese readings displayed are not guaranteed to be correct. There is a list of characters that must have their readings overridden manually. Please report an entry if you notice incorrect information.

---

## Sources

### English

- <span id="cedict"></span>**[CC-CEDICT](https://www.mdbg.net/chinese/dictionary?page=cedict)**
  125,271 entries · CC BY-SA 4.0 · Updated 2026-10-23
    - An open-source Chinese-English dictionary project.

- <span id="wiktionary"></span>**[Wiktionary](https://en.wiktionary.org/)**
  272,848 entries · CC BY-SA 4.0 / GFDL · Updated 2026-10-18
    - Chinese entries from the Wiktionary. English definitions, usage notes and etymology, from [kaikki.org](https://kaikki.org/dictionary/Chinese/).


### Mainland

- <span id="zhwiktionary"></span>**[中文维基词典](https://zh.wiktionary.org/)**
  55,538 entries · CC BY-SA 4.0 · Updated 2026-10-18
    - Chinese-language definitions from the community-built Chinese Wiktionary.


### Taiwan

- <span id="moe"></span>**[《國語辭典簡編本》](https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/dict_concised_download.html)**
  45,178 entries · CC BY-ND 3.0 TW · Updated 2026-09-29
    - Taiwan's Ministry of Education concise Mandarin dictionary - Taiwanese pronunciations and monolingual definitions written in Chinese.

- <span id="moe_revised"></span>**[《重編國語辭典修訂本》](https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/dict_reviseddict_download.html)**
  163,909 entries · CC BY-ND 3.0 TW · Updated 2026-09-29
    - Taiwan's Ministry of Education comprehensive Mandarin dictionary - fuller definitions and additional Taiwanese readings.


### Japanese

- <span id="jpwiktionary"></span>**[ウィクショナリー](https://ja.wiktionary.org/)**
  16,342 entries · CC BY-SA 4.0 · Updated 2026-10-18
    - Chinese headwords with Japanese-language definitions from Japanese Wiktionary.


### Frequency

- <span id="fineweb2-frequency"></span>**[FineWeb2 Mandarin Frequency List](https://github.com/Furretar/FineWeb2-Mandarin-Frequency-List)**
   270,160 entries · ODC-BY corpus · Updated 2026-10-27
    - A frequency list we developed using 20GB of the FineWeb2 corpus.

- <span id="subtlex"></span>**[SUBTLEX-CH](https://www.ugent.be/pp/experimentele-psychologie/en/research/documents/subtlexch)**
  99,121 words · CC BY 4.0 · Updated 2026-10-16
    - Chinese word frequencies from film and TV subtitles (Cai & Brysbaert, 2010). Used to develop the FineWeb2 Mandarin Frequency List.


### Vocabulary

- <span id="hsk"></span>**[HSK Vocabulary](https://github.com/drkameleon/complete-hsk-vocabulary)**
  11,470 words · MIT · Updated 2026-10-19
    - HSK 3.0 and 2026 syllabus vocabulary levels, shown next to the frequency rank.


### Software

- <span id="opencc"></span>**[OpenCC](https://github.com/BYVoid/OpenCC)**
  Apache-2.0
    - Simplified and Traditional Chinese conversion.

- <span id="jieba"></span>**[jieba](https://github.com/fxsjy/jieba)**
  MIT
    - Chinese word parser.


---

## Hosting
Yabai Search is currently hosted locally on various devices, including a laptop, desktop, and android tablets. You can view the current host in the hamburger menu in the top-right. The host with lowest latency is chosen as the global host. Each user will probe each host and be redirected to the fastest host from their location, the local host. To avoid conflicts when making accounts, users will be redirected to the global host when signing up.

The local host will change if another host is at least 20% faster for longer than 30 seconds. The global host wil change if another host is at least 20% faster for longer than 5 minutes, with a cooldown of 30 minutes. The cooldowns will be ignored if the global host takes longer than 3 seconds to respond. The global host will be switched immediately if it doesn't respond for 10 seconds. The hosts must be synced before switching.

---

### Todo
- Add character search and stroke order in the details page, mainland and taiwan
- check for security issues
- add audio to details page
- add example sentences
- ai generate missing definitions for jp

---