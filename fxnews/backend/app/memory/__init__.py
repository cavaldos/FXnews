"""Long-term market memory (Graphiti knowledge graph).

What goes in: only observed facts -- the event (title, time, impact, actual / forecast / previous) and what the price of
each related symbol actually did afterwards (``bt_outcomes``). What never goes in: anything an LLM (or the rule engine)
predicted -- ``bt_predictions``, ``analyses``, ``signals``, ``news_items.rule_bias``. See ``facts.py``.

Facts are written deterministically (no LLM extraction, no LLM dedupe / invalidation), so the graph cannot pick up a
model's opinion on the way in either. Retrieval is hybrid BM25 + embedding search, also without an LLM.
"""
