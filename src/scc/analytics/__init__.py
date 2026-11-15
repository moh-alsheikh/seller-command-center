"""Deterministic business metrics (design rule 4: metrics come from code, not from the model).

Every function here:
  * takes an open DuckDB connection and an ``as_of`` date (design rule 6: fixed "today"),
  * reads only the ``sales`` / ``returns`` / ``products`` views,
  * returns plain pandas DataFrames or dicts with no randomness,
so the same question always gives the same numbers, and unit tests can check them exactly.

The tools in ``scc.tools`` wrap these functions for the agents. The model's job is to choose
which metric to ask for and to explain the result, not to do arithmetic.
"""
