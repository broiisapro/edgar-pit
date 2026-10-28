"""8-K filing text: discovery, cleaning, segmentation and ingest (decision A-31).

An 8-K's documents are listed by the filing's ``-index-headers.html`` page, so
``discover`` turns that page into document references, ``clean`` turns one HTML
document into plain text, ``segment`` splits that text on the filing's own item
headings, and ``ingest`` combines the three into ``raw.filings_text`` rows whose
``acceptance_ts`` and ``ts_imputed`` come from ``raw.filings``.
"""
