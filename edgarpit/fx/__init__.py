"""Point-in-time FX conversion for Phase 2 (decision A-32).

``edgarpit/fx/valet.py`` holds the Bank of Canada Valet client and the parser
that turns a Valet observations document into ``raw.fx_daily`` rows. A rate for
date ``d`` is treated as known at ``d`` 17:00 ``America/Toronto``; conversion
uses the latest observation on or before the period end, so a converted value is
never known before both its filing and its rate.
"""
