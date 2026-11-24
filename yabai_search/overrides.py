"""Manual per-word overrides - edit here rather than in the code.

Hand-picked corrections for specific headwords, kept declarative so they are easy
to review, add to, or remove. Keys are matched against both the traditional and
simplified form of a word.
"""

# Force a Taiwan (MOE) reading, ignoring whatever the MOE dictionary lists. The
# value is the tone-marked pinyin shown while the Taiwan toggle is on.
TAIWAN_READINGS: dict[str, str] = {
    # Taiwan reads 血 as xiě, not xuè.
    "血": "xiě",
    # MOE lists both kòng dǎng and kōng dǎng; kōng dǎng is the Taiwan reading.
    "空檔": "kōng dǎng",
    # 期 on its own: MOE lists jī, qí and qī; Taiwan reads it qí (as in nearly every MOE word with 期).
    "期": "qí",
}

# Taiwan readings of single characters, used only to derive the Taiwan reading of a word MOE does not list
# (gen_taiwan_readings.derive_taiwan); a word MOE lists keeps MOE's own reading, and TAIWAN_READINGS above wins
# over both. Each entry is a character with exactly one reading in CC-CEDICT and exactly one in MOE, and the two
# differ: character -> (CC-CEDICT reading, Taiwan reading). The Taiwan reading replaces the syllable only when the
# syllable is that CC-CEDICT reading (tone aside). Generated once from both dictionaries (November 2026) and kept
# here by hand; differences only in capitals (Xiǎn / xiǎn) are not listed, nor are 唇 (MOE zhēn) and 蝎 (MOE hé),
# whose MOE reading is the sense another character has (脣, 蠍).
TAIWAN_CHAR_READINGS: dict[str, tuple[str, str]] = {
    # Checked by hand (November 2026): these have two readings in CC-CEDICT or MOE, so the rule above does not
    # list them, but the reading on the right is the one most MOE words use for the character (92-100% of them;
    # 筑 has no MOE word, its MOE entry is zhú). Only the CC-CEDICT reading on the left is replaced: 識 shí -> shì,
    # while 識 zhì (標識) stays.
    "緝": ("qī", "qì"),
    "期": ("qī", "qí"),
    "微": ("wēi", "wéi"),
    "質": ("zhì", "zhí"),
    "識": ("shí", "shì"),
    "擁": ("yōng", "yǒng"),
    "穴": ("xué", "xuè"),
    "剖": ("pōu", "pǒu"),
    "諷": ("fěng", "fèng"),
    "檔": ("dàng", "dǎng"),
    "危": ("wēi", "wéi"),
    "斂": ("liǎn", "liàn"),
    "筑": ("zhù", "zhú"),
    # Generated:
    "丮": ("jǐ", "jí"),
    "丳": ("chǎn", "chàn"),
    "乣": ("jiǔ", "diū"),
    "亳": ("bó", "bò"),
    "企": ("qǐ", "qì"),
    "俵": ("biào", "biǎo"),
    "倏": ("shū", "shù"),
    "倬": ("zhuō", "zhuó"),
    "偽": ("wěi", "wèi"),
    "僿": ("sài", "shì"),
    "儜": ("níng", "néng"),
    "儵": ("shū", "shù"),
    "冇": ("mǎo", "mǒu"),
    "冼": ("Xiǎn", "shěng"),
    "凊": ("qìng", "jìng"),
    "凔": ("cāng", "chuàng"),
    "剟": ("duō", "duó"),
    "剽": ("piāo", "piào"),
    "勑": ("chì", "lài"),
    "勣": ("jì", "jī"),
    "匊": ("jū", "jú"),
    "厓": ("yá", "yái"),
    "厜": ("zuī", "chuí"),
    "叔": ("shū", "shú"),
    "吔": ("yē", "ye"),
    "咄": ("duō", "duò"),
    "哱": ("bō", "pò"),
    "哳": ("zhā", "zhá"),
    "唅": ("hán", "hān"),
    "啅": ("zhào", "zhuó"),
    "喎": ("wāi", "kuāi"),
    "喱": ("lí", "lǐ"),
    "嗍": ("suō", "shuò"),
    "嗲": ("diǎ", "diē"),
    "嘌": ("piào", "piāo"),
    "噁": ("è", "ě"),
    "噸": ("dūn", "dùn"),
    "嚗": ("bó", "bō"),
    "囀": ("zhuàn", "zhuǎn"),
    "囔": ("nāng", "nang"),
    "囷": ("qūn", "jūn"),
    "垃": ("lā", "lè"),
    "垺": ("póu", "fú"),
    "埒": ("liè", "lè"),
    "埡": ("yā", "yà"),
    "堄": ("nì", "ní"),
    "堐": ("yá", "ái"),
    "墼": ("jī", "jí"),
    "壑": ("hè", "huò"),
    "夕": ("xī", "xì"),
    "奰": ("bì", "bèi"),
    "妮": ("nī", "ní"),
    "娭": ("āi", "xī"),
    "娸": ("qí", "qī"),
    "婐": ("wǒ", "ě"),
    "嬤": ("mó", "mā"),
    "嬬": ("xū", "rú"),
    "寀": ("cài", "cǎi"),
    "寂": ("jì", "jí"),
    "屘": ("mǎn", "mān"),
    "屩": ("juē", "jué"),
    "岈": ("yá", "xiā"),
    "岝": ("Zuò", "zé"),
    "崖": ("yá", "yái"),
    "崤": ("Xiáo", "yáo"),
    "崱": ("zé", "zè"),
    "巍": ("wēi", "wéi"),
    "幀": ("zhēn", "zhèng"),
    "幵": ("qiān", "jiān"),
    "廞": ("xīn", "qīn"),
    "彉": ("guō", "kuò"),
    "息": ("xī", "xí"),
    "惋": ("wǎn", "wàn"),
    "惜": ("xī", "xí"),
    "愓": ("dàng", "shāng"),
    "慴": ("shè", "zhé"),
    "懅": ("qú", "jù"),
    "懫": ("zhí", "zhì"),
    "戕": ("qiāng", "qiáng"),
    "拎": ("līn", "līng"),
    "拙": ("zhuō", "zhuó"),
    "挐": ("ná", "rú"),
    "掬": ("jū", "jú"),
    "揃": ("jiān", "jiǎn"),
    "搕": ("kè", "kē"),
    "搷": ("chēn", "tián"),
    "摑": ("guāi", "guó"),
    "撏": ("xián", "xún"),
    "撖": ("Hàn", "gǎn"),
    "播": ("bō", "bò"),
    "擉": ("chuò", "chù"),
    "擊": ("jī", "jí"),
    "擗": ("pǐ", "pì"),
    "擩": ("rǔ", "rù"),
    "擲": ("zhì", "zhí"),
    "攉": ("huō", "huò"),
    "斄": ("Tái", "lí"),
    "斸": ("zhǔ", "zhú"),
    "昫": ("xù", "xǔ"),
    "曙": ("shǔ", "shù"),
    "朾": ("tíng", "tīng"),
    "柁": ("tuó", "duò"),
    "框": ("kuàng", "kuāng"),
    "桉": ("ān", "àn"),
    "椰": ("yē", "yé"),
    "楗": ("jiàn", "juàn"),
    "槭": ("qì", "cù"),
    "樗": ("chū", "shū"),
    "樠": ("mén", "mán"),
    "樸": ("pǔ", "pú"),
    "櫛": ("zhì", "jié"),
    "欑": ("zàn", "cuán"),
    "欙": ("léi", "lěi"),
    "殀": ("yāo", "yǎo"),
    "殜": ("dié", "yè"),
    "殹": ("yì", "yī"),
    "氉": ("sào", "zào"),
    "氬": ("yà", "yǎ"),
    "氰": ("qíng", "qīng"),
    "汏": ("dà", "dài"),
    "汐": ("xī", "xì"),
    "沷": ("fā", "fú"),
    "泙": ("píng", "pēng"),
    "泜": ("zhī", "chí"),
    "泭": ("fū", "fú"),
    "洟": ("tì", "yí"),
    "浹": ("jiā", "jiá"),
    "淆": ("xiáo", "yáo"),
    "淑": ("shū", "shú"),
    "淜": ("péng", "pēng"),
    "渢": ("fēng", "féng"),
    "潀": ("cóng", "zhōng"),
    "潝": ("xī", "xì"),
    "濞": ("bì", "pì"),
    "烕": ("miè", "xuè"),
    "焞": ("tūn", "chún"),
    "焯": ("chāo", "zhuó"),
    "煦": ("xù", "xǔ"),
    "熄": ("xī", "xí"),
    "爊": ("āo", "áo"),
    "牁": ("kē", "gē"),
    "犆": ("tè", "zhí"),
    "犮": ("bá", "bó"),
    "猞": ("shē", "shè"),
    "獏": ("mò", "mú"),
    "琀": ("hán", "hàn"),
    "琊": ("yá", "yé"),
    "琍": ("lí", "lì"),
    "瓿": ("bù", "pǒu"),
    "甋": ("dì", "dí"),
    "畷": ("chuò", "zhuó"),
    "疒": ("nè", "chuáng"),
    "疧": ("qí", "zhī"),
    "痒": ("yǎng", "yáng"),
    "瘙": ("sào", "sāo"),
    "瘜": ("xī", "xí"),
    "皀": ("bī", "xiāng"),
    "皽": ("zhāo", "zhǎn"),
    "眶": ("kuàng", "kuāng"),
    "睃": ("suō", "jùn"),
    "睥": ("pì", "bì"),
    "矻": ("kū", "kù"),
    "矽": ("xī", "xì"),
    "砠": ("qū", "jū"),
    "碡": ("zhóu", "dú"),
    "磈": ("wěi", "kuǐ"),
    "礌": ("léi", "lèi"),
    "礧": ("lèi", "lěi"),
    "禜": ("yǒng", "yòng"),
    "穸": ("xī", "xì"),
    "突": ("tū", "tú"),
    "窣": ("sū", "sù"),
    "築": ("zhù", "zhú"),
    "簬": ("lù", "dù"),
    "糅": ("róu", "rǒu"),
    "糌": ("zān", "zán"),
    "紊": ("wěn", "wèn"),
    "紑": ("fóu", "fōu"),
    "絎": ("háng", "hèng"),
    "絣": ("bǐng", "bēng"),
    "綅": ("qīn", "xiān"),
    "緆": ("xī", "xì"),
    "縛": ("fù", "fú"),
    "縰": ("xǐ", "shǐ"),
    "績": ("jì", "jī"),
    "羰": ("tāng", "tàn"),
    "羵": ("fěn", "fén"),
    "翕": ("xī", "xì"),
    "翫": ("wán", "wàn"),
    "聒": ("guō", "guā"),
    "肸": ("xī", "xì"),
    "胂": ("shèn", "shēn"),
    "胺": ("àn", "ān"),
    "腊": ("xī", "xí"),
    "舡": ("gāng", "xiāng"),
    "舺": ("xiá", "jiǎ"),
    "艟": ("chōng", "tóng"),
    "芏": ("dù", "tǔ"),
    "芣": ("fú", "fóu"),
    "莙": ("jūn", "jùn"),
    "菂": ("dì", "dī"),
    "菹": ("zū", "jū"),
    "菽": ("shū", "shú"),
    "萴": ("cè", "zé"),
    "葅": ("zū", "jū"),
    "葖": ("tū", "tú"),
    "蓿": ("xù", "sù"),
    "蕹": ("wèng", "yōng"),
    "薇": ("wēi", "wéi"),
    "藩": ("fān", "fán"),
    "蚴": ("yòu", "yǒu"),
    "蝐": ("mào", "mèi"),
    "蝡": ("rú", "ruǎn"),
    "蝸": ("wō", "guā"),
    "蟯": ("náo", "ráo"),
    "蠁": ("xiǎng", "xiàng"),
    "蠼": ("qú", "jué"),
    "裰": ("duō", "duó"),
    "褐": ("hè", "hé"),
    "襆": ("fú", "pú"),
    "襝": ("liǎn", "liàn"),
    "覘": ("chān", "zhān"),
    "訌": ("hòng", "hóng"),
    "諍": ("zhèng", "zhēng"),
    "諕": ("háo", "xià"),
    "諝": ("xū", "xǔ"),
    "謅": ("zhōu", "zōu"),
    "謔": ("xuè", "nüè"),
    "謚": ("shì", "yì"),
    "謦": ("qì", "qìng"),
    "讘": ("niè", "zhé"),
    "貯": ("zhù", "zhǔ"),
    "跌": ("diē", "dié"),
    "跡": ("jì", "jī"),
    "踔": ("chuō", "zhuó"),
    "踘": ("jū", "jú"),
    "踤": ("cù", "zú"),
    "踮": ("diǎn", "diàn"),
    "踱": ("duó", "duò"),
    "蹈": ("dǎo", "dào"),
    "蹜": ("suō", "sù"),
    "蹟": ("jì", "jī"),
    "蹼": ("pǔ", "pú"),
    "躠": ("xiè", "sà"),
    "轇": ("jiāo", "jiū"),
    "遢": ("tā", "tà"),
    "鄩": ("Xún", "xín"),
    "鄫": ("Zēng", "céng"),
    "釙": ("pō", "pò"),
    "鉭": ("tǎn", "dàn"),
    "銨": ("ǎn", "ān"),
    "銻": ("tī", "tì"),
    "錫": ("xī", "xí"),
    "鍤": ("chā", "chá"),
    "鏚": ("qī", "qì"),
    "鐧": ("jiǎn", "jiàn"),
    "閌": ("kāng", "kàng"),
    "隄": ("dī", "tí"),
    "隼": ("sǔn", "zhǔn"),
    "雱": ("pāng", "páng"),
    "霢": ("mài", "mò"),
    "鞙": ("xuàn", "juān"),
    "鞫": ("jū", "jú"),
    "顣": ("qī", "cù"),
    "颿": ("fān", "fán"),
    "餫": ("hún", "yùn"),
    "驟": ("zhòu", "zòu"),
    "髂": ("qià", "kà"),
    "髮": ("fà", "fǎ"),
    "髲": ("pì", "bì"),
    "鬫": ("kàn", "hǎn"),
    "鬵": ("qián", "xín"),
    "魆": ("xū", "xù"),
    "魟": ("hóng", "hōng"),
    "魬": ("fǎn", "bàn"),
    "魭": ("yuán", "wǎn"),
    "鮟": ("ān", "àn"),
    "鯡": ("fēi", "fèi"),
    "鰂": ("zéi", "zé"),
    "鰫": ("yóng", "róng"),
    "鳦": ("yǐ", "yì"),
    "鵀": ("rèn", "rén"),
    "鶤": ("kūn", "yùn"),
    "鸒": ("yú", "yù"),
    "麌": ("wú", "yǔ"),
    "黕": ("zhěn", "dǎn"),
    "黮": ("tán", "tǎn"),
    "齞": ("niàn", "yǎn"),
}

# Force which reading a word shows when the dictionary offers several. The value
# is the tone-marked pinyin that the parsed input should display. Keys are
# matched against both the traditional and simplified form.
PREFERRED_READINGS: dict[str, str] = {
    # 了 on its own is the particle "le", not the verb liǎo, even though liǎo
    # has more senses.
    "了": "le",
    # 没 on its own is usually the negating méi, not the verb mò ("to sink"),
    # even though mò has more senses. Use méi when parsing single characters.
    "没": "méi",
    # 只 is mostly zhǐ "only; merely", not the classifier zhī (隻), which has its
    # own traditional spelling. Readings are compared with their tone.
    "只": "zhǐ",
}

# Context labels for definitions. CC-CEDICT and Wiktionary open some glosses with a parenthesis: a label such as
# "(literary) by chance" or "(Cantonese) mutual aid", or a part of the definition such as "(located) in; at" or
# "(before an adjective) more". Only the labels listed here are moved behind the definition, as a small muted note;
# every other parenthesis stays where it is. Add a label here to move it. Lower case, without the brackets. A
# parenthesis moves only when each of its comma-separated parts (or parts joined by " or ") is listed: "(Cantonese,
# slang)" and "(literary or dialectal)" move, "(literary, located)" does not.
NOTE_LABELS: set[str] = {
    # where a gloss came from: the Chinese Wikipedia article's English title (import_wiktionary.py)
    "wikipedia",
    # register and style
    "literary", "colloquial", "slang", "internet slang", "fandom slang", "archaic", "obsolete",
    "obsolete on its own in standard chinese", "dated", "historical", "historical dictionaries only", "old",
    "figurative", "figuratively", "literally", "literal", "formal", "informal", "humorous", "jocular",
    "euphemistic", "euphemism", "derogatory", "often derogatory", "offensive", "ethnic slur", "vulgar", "honorific",
    "polite", "humble", "courteous", "sarcastic", "endearing", "childish", "rare", "uncommon", "poetic", "classical",
    "neologism", "officialese", "by extension", "specifically", "idiomatic", "simile", "metonymic",
    "coll.", "lit.", "fig.", "derog.", "arch.",
    # usage and grammar
    "bound form", "attributive", "often attributive", "transitive", "intransitive", "countable", "uncountable",
    "onomatopoeia", "onom.", "ideophonic", "loanword", "idiom", "saying", "proverb", "in compounds",
    "or in compounds", "chiefly in the negative", "collective",
    # regions and dialects
    "mandarin", "dialectal mandarin", "beijing mandarin", "taiwanese mandarin", "northeastern mandarin",
    "southwestern mandarin", "jianghuai mandarin", "nanjing mandarin", "harbin mandarin", "wuhan mandarin",
    "xi'an mandarin", "yangzhou mandarin", "liuzhou mandarin", "sichuanese", "beijing dialect", "sichuan dialect",
    "cantonese", "hong kong cantonese", "dialectal cantonese", "chiefly cantonese",
    "hokkien", "taiwanese hokkien", "zhangzhou hokkien", "xiamen hokkien", "quanzhou hokkien", "singapore hokkien",
    "philippine hokkien", "penang hokkien", "mainland china hokkien", "xiamen", "xiamen and zhangzhou hokkien",
    "zhangzhou and taiwanese hokkien", "xiamen and quanzhou hokkien", "quanzhou and xiamen hokkien",
    "xiamen and taiwanese hokkien",
    "wu", "northern wu", "dialectal wu", "shanghainese", "suzhounese", "wenzhounese", "ningbonese", "hangzhounese",
    "jinhua wu", "xiang", "hengyang xiang", "changsha xiang", "loudi xiang", "gan", "pingxiang gan", "lichuan gan",
    "dialectal gan", "hakka", "taiwanese hakka", "dialectal hakka", "yudu hakka", "min", "eastern min",
    "southern min", "northern min", "puxian min", "leizhou min", "zhongshan min", "hainanese", "teochew", "jin",
    "huizhou", "dungan", "pinghua", "southern pinghua", "nanning pinghua", "dialectal", "dialect", "regional",
    "literary or dialectal", "literary or cantonese", "literary or hokkien",
    "taiwan", "chiefly taiwan", "tw", "mainland china", "chiefly mainland china", "hong kong", "chiefly hong kong",
    "hk", "macau", "singapore", "malaysia",
    # fields
    "medicine", "anatomy", "pathology", "pharmacology", "traditional chinese medicine", "tcm", "neuroanatomy",
    "physiology", "cytology", "immunology", "bacteriology", "biochemistry", "chemistry", "organic chemistry",
    "inorganic chemistry", "physics", "mathematics", "math.", "geometry", "statistics", "logic", "mechanics",
    "astronomy", "chinese astronomy", "meteorology", "geology", "geography", "mineralogy", "botany", "zoology",
    "biology", "genetics", "agriculture", "psychology", "linguistics", "grammar", "phonetics", "philosophy",
    "computing", "computer", "internet", "programming", "electronics", "electricity", "law", "finance", "banking",
    "accounting", "economics", "business", "politics", "military", "education", "history", "architecture",
    "engineering", "metallurgy", "mining", "textiles", "telecommunications", "optics", "automotive", "aviation",
    "nautical", "rail transport", "railway", "photography", "printing", "cooking", "sports", "soccer", "baseball",
    "basketball", "golf", "chess", "xiangqi", "mahjong", "card games", "video games", "acg", "martial arts", "music",
    "opera", "theater", "cinema", "calligraphy", "poetry", "literature", "buddhism", "taoism", "daoism",
    "christianity", "catholicism", "protestantism", "islam", "religion", "chinese mythology", "greek mythology",
    "mythology",
}

# Words whose "Other forms" row should be hidden (e.g. when it just repeats a
# pronunciation already shown elsewhere).
HIDE_FORMS: set[str] = {
    "血",
}

# Commons images for IDS headwords that have no Unicode codepoint. Scraped from
# the Wiktionary entry pages (their headword is an image); the app shows these
# instead of the dashed-box "⿰" sequence. Longer headwords can be split into
# these components (e.g. ⿰男疾⿰男戶 -> ⿰男疾 + ⿰男戶).
IDS_IMAGES: dict[str, str] = {
    "⿰扌幸": "/static/ids_images/ids_0a1db781c220.png",
    "⿰男戶": "/static/ids_images/ids_1b5ecad2664e.png",
    "⿰男疾": "/static/ids_images/ids_712fdd95f307.png",
    "⿰禾𰢫": "/static/ids_images/ids_5867cda6a171.png",
    "⿰訁𪢱": "/static/ids_images/ids_c8f6c4dd873b.png",
    "⿱寶⿰寶寶": "/static/ids_images/ids_0d0f1a1c7626.png",
    "⿱𡩧⿺進⿰貝招": "/static/ids_images/ids_7567c4aaf284.png",
    # More Commons headword images, scraped the same way. Simplified and
    # traditional spellings share an image where Wiktionary has only one page.
    "⿰讠旦": "/static/ids_images/ids_64ee5d2784e5.png",
    "⿰贝专": "/static/ids_images/ids_2f40c7943657.png",
    "⿰车匡": "/static/ids_images/ids_7de0dd13289a.png",
    "搞⿰车匡": "/static/ids_images/ids_7b5e1ecec5eb.png",
    "⿰车卜": "/static/ids_images/ids_0c0c9fa7a236.png",
    "⿰阝单": "/static/ids_images/ids_5633968c62db.png",
    "⿰麦方": "/static/ids_images/ids_b82252ebbbfa.png",
    "⿱⺈⿻⿻⿵冂⿰一一⿺乚丨⿻⿱ココ弖": "/static/ids_images/ids_c28a58fafeb5.png",
    "⿱⿻丅⿱冖⿰丶丶令": "/static/ids_images/ids_13e77ab622b2.png",
    "⿱⿻丅⿱冖⿰丶丶双": "/static/ids_images/ids_43c1e9e28a3b.png",
    "⿱⿻丅⿱冖⿰丶丶巴": "/static/ids_images/ids_45264f56de1c.png",
    "⿳⿱⿴𦥑⿵冂〒冖⿰金⿷⿼己⿼コ丨⿷匚丨火": "/static/ids_images/ids_e1d880cfec03.png",
    "⿳宀珎⿺进⿰贝招": "/static/ids_images/ids_6cefca80597e.png",
    "⿳宀珎⿺進⿰貝招": "/static/ids_images/ids_6cefca80597e.png",
    "⿴⿻弓𠃊⿴囗⿻⿰丿丿丶": "/static/ids_images/ids_98d3da6883cf.png",
    "⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰长𮪉心": "/static/ids_images/ids_5c1ed04e886f.png",
    "⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰長𮪉心": "/static/ids_images/ids_5c1ed04e886f.png",
    "⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰长𮪉心⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰长𮪉心面": "/static/ids_images/ids_227b65c13fdf.png",
    "⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰長𮪉心⿺⻍⿳⿻山昍⿰月⿱⿲糸言糸⿰長𮪉心麵": "/static/ids_images/ids_227b65c13fdf.png",
    "⿺辶⿳⿻山昍⿰月⿱亦⿰𲈤长心": "/static/ids_images/ids_f61aa6cb1ef4.png",
    "⿻屮⿱⿻⿰コ匚一冂": "/static/ids_images/ids_5c278ecec0f7.png",
}


# ---------------------------------------------------------------------------
# English normalization (moved here from normalize.py).
# CC-CEDICT uses a compact style with abbreviations ("sb." = somebody, etc.).
# This module expands a small, explicit set of them so definitions read
# naturally and are searchable by the full words. The raw text is kept untouched
# in the `english` column; the normalized form is stored separately in
# `english_normalized`, so nothing is lost.
# ---------------------------------------------------------------------------
import re as _re

# Ordered list of (pattern, replacement). Each pattern is matched as a whole
# word (word boundaries) and case-sensitively, so we never rewrite text by
# accident. Add new rules here, grouped and commented.
NORMALIZE_RULES: list[tuple[str, str]] = [
    # people / things (CEDICT writes "sb" with no trailing period)
    (r"\bsb\b", "somebody"),
    # references
    (r"\babbr\.", "abbreviation"),
    # grammar / register
    (r"\bonom\.", "onomatopoeia"),
    (r"\bsth\b", "something"),
    # "cf." (compare) reads more naturally as "see".
    (r"\bcf\.", "see"),
    # "(Tw)" is CEDICT's shorthand for a Taiwan-specific usage.
    (r"\(Tw\)", "(Taiwan)"),
    # "fig." = figurative.
    (r"\bfig\.", "figurative"),
    # "(lit.)" = literally / literal.
    (r"\(lit\.\)", "(literal)"),
    # "coll." = colloquial.
    (r"\bcoll\.", "colloquial"),

    
]

# Compile the rules once, at import time (regex compilation is not free).
_NORMALIZE_COMPILED = [
    (_re.compile(pattern), replacement) for pattern, replacement in NORMALIZE_RULES
]


def normalize_english(text: str) -> str:
    """Apply every normalization rule to `text` and return the expanded version."""
    for pattern, replacement in _NORMALIZE_COMPILED:
        text = pattern.sub(replacement, text)
    return text
