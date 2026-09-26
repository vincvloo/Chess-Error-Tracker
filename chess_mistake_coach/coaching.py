"""Plain-language coaching: how to rate a move, and what a mistake means.

The ratings use the same centipawn thresholds as the rest of the app
(analysis.INACCURACY / MISTAKE / BLUNDER), so "mistake" means the same thing in
live play, practice and the dashboard.
"""

from __future__ import annotations

from .analysis import BLUNDER, INACCURACY, MISTAKE

# One sentence per category analysis.classify() can return: what went wrong,
# and the habit that prevents it.
CATEGORY_EXPLANATIONS = {
    "allowed forced mate":
        "This let your opponent force checkmate. Before you move, check every check and "
        "capture they could reply with.",
    "missed forced mate":
        "There was a forced checkmate here. When the enemy king looks exposed, list every "
        "check first.",
    "moved a piece onto an attacked square":
        "The piece you moved could simply be captured. Before you move, ask what attacks "
        "the square you're moving to.",
    "left a piece undefended":
        "A piece was left with nothing protecting it, so it could be taken for free. After "
        "your move, check that each piece is defended.",
    "hung a pawn":
        "A pawn was left to be taken for nothing. Check what your opponent can capture "
        "after your move.",
    "underdefended piece, lost the exchange":
        "Your piece was defended, but the piece attacking it was worth less, so the trade "
        "lost material. Count attackers and defenders, and their values.",
    "allowed a strong check or fork":
        "This gave your opponent a strong check or a fork. Look at their forcing moves "
        "(checks, captures, threats) before you commit.",
    "missed a capture winning material":
        "A capture here would have won material. Scan the captures before anything else.",
    "missed a favourable capture":
        "A capture was available that came out ahead. Look at the captures first.",
    "missed a forcing check":
        "A check here was strong. Look at checks first: they limit your opponent's replies.",
    "missed a tactic setting up material gain":
        "A stronger move set up a material win a move later. Ask what your move threatens.",
    "positional or planning error":
        "No single tactic decided this: the engine simply preferred a different plan or "
        "placement.",
}

RATING_LABELS = {
    "best": "Best move",
    "good": "Good move",
    "inaccuracy": "Inaccuracy",
    "mistake": "Mistake",
    "blunder": "Blunder",
}


def explain(category: str | None) -> str:
    return CATEGORY_EXPLANATIONS.get(category or "", "")


def rate_move(cp_loss: int, played_best: bool) -> str:
    """best / good / inaccuracy / mistake / blunder from how many centipawns
    the move gave up compared with the engine's best."""
    if played_best:
        return "best"
    if cp_loss >= BLUNDER:
        return "blunder"
    if cp_loss >= MISTAKE:
        return "mistake"
    if cp_loss >= INACCURACY:
        return "inaccuracy"
    return "good"
