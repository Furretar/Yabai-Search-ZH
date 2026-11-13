"""Developer tools for looking inside a search; nothing here changes what a search returns.

perf.py         per-request timing trace (free unless YABAI_TRACE=1), used by db.py, services.py and the middleware
trace_query.py  command line: shows at which stage a word survives or drops out of a search
                (python -m yabai_search.debug.trace_query zhi 纸)
"""
