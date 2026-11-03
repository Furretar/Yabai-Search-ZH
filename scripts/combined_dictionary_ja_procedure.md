# Procedure: Combined Japanese dictionary (top frequent words)

Goal: for each of the 50 most frequent Chinese words, merge the senses from
four sources (comprehensive MOE, CC-CEDICT, English Wiktionary, Chinese Wiktionary),
match them across sources, keep the most relevant ones, and write a single
Japanese definition list with synonyms. No example sentences. The Japanese text
must blend the sources so the origin of any single sense is not obvious.

## Sources
1. `data/moe_revised.sqlite` (教育部 重編國語辭典修訂本) - Chinese monolingual definitions.
   Read `senses(word, position, text)`; fall back to `moe.definition`.
2. `data/cedict.sqlite` (CC-CEDICT) - English glosses.
   Read `senses(word, reading, position, text)`.
3. `data/wiktionary.sqlite` (English Wiktionary) - English glosses.
   Read `wiktionary(word, gloss)`.
4. `data/zhwiktionary.sqlite` (Chinese Wiktionary) - Chinese definitions.
   Read `senses(word, position, text)`.
5. `data/extra/` - three Chinese-Japanese dictionaries (Yomitan format, `term_bank_*.json`):
   - 白水社中国語辞典 (`-ZH-JA- 白水社 中国語辞典.zip`)
   - 中日大辞典 第二版 (`-ZH-JA- 中日大辞典 第二版.zip`)
   - 小学館中日辞典 第3版 (`-ZH-JA- 小学館中日辞典 第3版-2025-05-03--pinyin-.zip`)
   Each entry is `[term, reading, tags, ..., [definition strings], ...]`. Extract plain text
   from the definition strings (structured content for 小学館) and use as Japanese reference.
   These are reference only: blend into the output so no source is identifiable.

## Steps per word
1. Fetch every sense/gloss from all four sources for the headword.
   Also try the simplified/traditional counterpart if the headword has both forms.
2. De-duplicate near-identical senses within a source (e.g. MOE often lists the
   same sense twice under different readings).
3. Match senses across sources by meaning, not by wording:
   - Group glosses that describe the same concept (same part of speech, same
     object/usage).
   - Keep a sense if at least two sources agree, OR if only one source has it
     but it is a distinct, well-attested meaning.
   - Drop senses that are clearly duplicates of another kept sense.
4. For each kept sense, write ONE Japanese definition that absorbs the nuance
   from all sources that contributed to it (do not quote any source verbatim;
   blend). Include part-of-speech hint when useful (助詞, 名詞, 動詞, etc.).
5. Append 3-6 Japanese synonyms / closely related words after the definition.
   Synonyms may be Japanese or the original Chinese term in parentheses.
6. Sort senses in a sensible order: most common / primary meaning first,
   then grammatical particles, then rare/archaic, then proper-noun uses.
7. Output format per entry:
   {
     "word": "...",
     "rank": <subtlex rank>,
     "per_million": <value>,
     "senses": [
       {"ja": "<Japanese definition>", "synonyms": ["...", ...]}
     ]
   }
   No example sentences anywhere.

## Quality rules
- Standard Japanese; Chinese-specific terms keep the original word with the
  Japanese meaning in parentheses, e.g. 甜点 (デザート).
- Do not leave any example sentence in the output.
- Blend sources; do not make it obvious which dictionary a sense came from.
- Keep each definition short enough to read on one screen.
- Every behaviour change needs a test; here the artefact is data, so at minimum
  validate JSON and that all 50 words are present.

## Frequency source
SUBTLEX-CH (data/subtlex.sqlite), ordered by `rank` ascending. Top 50 = rows
1..50 by rank.
PYEOF
echo done