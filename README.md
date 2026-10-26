#

A Chinese dictionary web app with Chinese-English, Chinese-Chinese, and Taiwanese Mandarin support.

The interface design was inspired by [jisho.org](https://jisho.org/), since it has a wonderful and clean design.

---

#### Quick Search Tips

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
| Zhuyin (Bopomofo) | `ㄉㄢㄍㄢˋ` | 单干 |


#### Search Logic

Searches have 6 levels of priority. Results within the same level will be sorted by frequency.

1. **Exact matches** for your search query.
2. Results that **start with** your search query.
3. Results that **contain** your query somewhere inside.
4. Matches with the **only listed reading in another language** (i.e. Shanghainese, Wu, Xiang).
5. All words from Wiktionary with a **reading but no definition**.
6. All words with **no reading and no definition**.

By default, searches will attempt to match headwords or readings. Definitions will be searched as a fallback if there are no matching headwords or readings for your search.

Definitions can be searched explicitly by using quotes `""`.

#### Pinyin

When searching with pinyin, you can specify tones with their corresponding numbers or leave it blank (`shi2shi4` vs `shishi`).

All syllable combinations will be matched. For example, `dangan` will match both dan'gan (单干) and dang'an (档案).

You can also specify tones for specific characters, i.e. `shi4shi` will return any results for shi4 with any tone shi after it.

Tone-insensitive entries are shown in a "Fuzzy match" section below the main results, both when the tonal search finds nothing and alongside tonal results.


#### Bopomofo

Bopomofo is converted to tonal pinyin first, so it is fully supported.


#### Wildcard

A wildcard is a placeholder for normal text, `?` is a single character wildcard, and `*` is a multiple character wildcard.

Use `?` to match exactly 1 character: `国家?` matches 国家级, and `?怒??` matches 喜怒哀乐.

It works with pinyin too: `guojia?` matches 国家级.

Use `*` to match any number of characters: `国家*` matches 国家, 国家级, 国家安全部, etc.

#### Language Codes

You can choose a language code from the drop-down menu on the left of the search bar.

After selecting a language, results with definitions in the selected language will be shown first. Matching results in other languages will be shown in a section called "Other Dictionaries" below the main results.

Searching with Taiwanese enabled will match to Taiwanese readings instead of of Mainland readings.

---

### Dictionaries and Sources

#### Taiwan (MOE) readings

Taiwanese Mandarin is mostly the same as Mainland Mandarin, with a handful of different pronunciations for words and characters, and slightly different word usage.

The Taiwanese Ministry of Education publishes dictionaries of Taiwanese Mandarin. 2 of these are used as sources for Yabai Search: the concise version 國語辭典簡編本, and the comprehensive version 重編國語辭典修訂本.

When the Taiwanese pronunciation toggle is turned on, matched readings from the Taiwanese dictionaries are shown instead. If there are no matches in Taiwanese Dictionaries but CEDICT lists a Taiwanese Pronunciation, the CEDICT Taiwanese pronunciation is used.

There are many characters in Taiwanese Mandarin that are always pronounced a certain way. There is not a detailed list of these, so all single character words with only 1 listed reading have their readings mapped onto all characters that only have 1 listed reading in the Chinese-English dictionaries.

This method is mostly accurate, but the Taiwanese readings displayed are not guaranteed to be accurate. There is a list of characters that must have their readings overridden manually. Please report an entry if you notice incorrect information.


#### Entries / CEDICT

CEDICT has separate entries for proper nouns and normal words. For example, the word/name 阮. The custom dictionary toggle is reliant on standardization of headwords into entries, these are collapsed into 1 entry.

CEDICT has entries whose entire definition is a cross-reference, e.g. `血 血 [xiě] /see 血[xuè]/`. The UI lists them under **Other forms:** and combines all duplicates.

<h2 id="sources">Sources &amp; Licenses</h2>

The application code is MIT-licensed (see [LICENSE](https://github.com/Furretar/Yabai-Search-ZH/blob/master/LICENSE)). The dictionary data listed below is not covered by that license and remains under the licenses shown for each source.


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

- <span id="subtlex"></span>**[SUBTLEX-CH](https://www.ugent.be/pp/experimentele-psychologie/en/research/documents/subtlexch)**
  99,121 words · CC BY 4.0 · Updated 2026-10-16
    - Chinese word frequencies from film and TV subtitles (Cai & Brysbaert, 2010), used to rank results.


### Vocabulary

- <span id="hsk"></span>**[HSK Vocabulary](https://github.com/drkameleon/complete-hsk-vocabulary)**
  11,470 words · MIT · Updated 2026-10-19
    - HSK 2.0, HSK 3.0, and 2026 syllabus vocabulary levels, shown next to the frequency rank.


### Software

- <span id="opencc"></span>**[OpenCC](https://github.com/BYVoid/OpenCC)**
  Apache-2.0
    - Simplified and Traditional Chinese conversion (used to match and display both scripts).

- <span id="jieba"></span>**[jieba](https://github.com/fxsjy/jieba)**
  MIT
    - Chinese word segmentation (used to split text into words and syllables for search and furigana).


---


## Contact

You can email me at `wyattpetrock2@gmail.com`.

Or add `furretar2` on Discord.
