"""
Longitudinal chess error tracker with a persistent store.

Pulls your Chess.com game history, runs Stockfish over every position where it
was your move, classifies each significant mistake, and keeps everything in a
local SQLite database so the picture gets sharper every time you run it.
"""

__version__ = "0.1.0"
