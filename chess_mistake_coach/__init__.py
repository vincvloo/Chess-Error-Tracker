"""
Chess Mistake Coach: find the mistakes you keep making and train them away.

Pulls your Chess.com game history, runs Stockfish over every position where it
was your move, classifies each significant mistake, and keeps everything in a
local SQLite database. A local web app then coaches you through your own
mistakes with practice, puzzles and a bot that rates your moves as you play.
"""

__version__ = "0.1.0"
