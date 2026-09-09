import chess
import chess.engine

from chess_tracker.analysis import classify, game_phase, resolve_colour, score_cp


def test_score_cp_from_pov_of_the_side_it_favours():
    info = {"score": chess.engine.PovScore(chess.engine.Cp(120), chess.WHITE)}
    assert score_cp(info, chess.WHITE) == 120


def test_score_cp_flips_sign_for_the_other_side():
    info = {"score": chess.engine.PovScore(chess.engine.Cp(120), chess.WHITE)}
    assert score_cp(info, chess.BLACK) == -120


def test_score_cp_maps_mate_to_the_mate_score_ceiling():
    info = {"score": chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE)}
    assert score_cp(info, chess.WHITE) == 9997
    assert score_cp(info, chess.BLACK) == -9997


def test_game_phase_first_ten_moves_are_always_opening():
    board = chess.Board()
    assert game_phase(board, 10) == "opening"


def test_game_phase_many_heavy_pieces_is_middlegame():
    board = chess.Board()  # starting position: full complement of heavy pieces
    assert game_phase(board, 11) == "middlegame"


def test_game_phase_few_heavy_pieces_is_endgame():
    board = chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1")  # kings and a pawn only
    assert game_phase(board, 30) == "endgame"


def test_resolve_colour_matches_white_case_insensitively():
    game_json = {"white": {"username": "Alice"}, "black": {"username": "bob"}}
    colour, mine, theirs = resolve_colour(game_json, "alice")
    assert colour == chess.WHITE
    assert mine["username"] == "Alice"
    assert theirs["username"] == "bob"


def test_resolve_colour_matches_black():
    game_json = {"white": {"username": "alice"}, "black": {"username": "bob"}}
    colour, mine, theirs = resolve_colour(game_json, "bob")
    assert colour == chess.BLACK
    assert mine["username"] == "bob"


def test_resolve_colour_returns_none_when_user_not_in_game():
    game_json = {"white": {"username": "alice"}, "black": {"username": "bob"}}
    assert resolve_colour(game_json, "carol") is None


def _classify(fen, played_uci, best_uci, colour, reply_uci, cp_before, cp_after):
    board = chess.Board(fen)
    played = chess.Move.from_uci(played_uci)
    best = chess.Move.from_uci(best_uci)
    reply = chess.Move.from_uci(reply_uci) if reply_uci else None
    return classify(board, played, best, colour, reply, cp_before, cp_after)


def test_classify_allowed_forced_mate():
    result = _classify("4k3/8/8/8/8/8/8/4K3 w - - 0 1", "e1d1", "e1d1",
                        chess.WHITE, None, cp_before=0, cp_after=-9500)
    assert result == "allowed forced mate"


def test_classify_missed_forced_mate():
    result = _classify("4k3/8/8/8/8/8/8/4K3 w - - 0 1", "e1d1", "e1d1",
                        chess.WHITE, None, cp_before=9500, cp_after=0)
    assert result == "missed forced mate"


def test_classify_hung_a_pawn():
    # White pushes e2-e4 into a black knight's attack; the pawn is undefended.
    result = _classify("4k3/8/5n2/8/8/8/4P3/4K3 w - - 0 1", "e2e4", "e2e4",
                        chess.WHITE, "f6e4", cp_before=0, cp_after=0)
    assert result == "hung a pawn"


def test_classify_left_a_piece_undefended():
    # The knight on d2 was already undefended before White's unrelated move.
    result = _classify("4k3/8/8/8/1b6/8/3N4/4K3 w - - 0 1", "e1f1", "e1f1",
                        chess.WHITE, "b4d2", cp_before=0, cp_after=0)
    assert result == "left a piece undefended"


def test_classify_moved_a_piece_onto_an_attacked_square():
    # The bishop itself walks onto the square the knight captures on.
    result = _classify("4k3/8/8/8/8/2n5/8/5BK1 w - - 0 1", "f1e2", "f1e2",
                        chess.WHITE, "c3e2", cp_before=0, cp_after=0)
    assert result == "moved a piece onto an attacked square"


def test_classify_underdefended_piece_lost_the_exchange():
    # Queen is defended (pawn on c3 can recapture), but a mere pawn took it.
    result = _classify("4k3/8/8/2p5/3Q4/2P5/8/4K3 w - - 0 1", "e1f1", "e1f1",
                        chess.WHITE, "c5d4", cp_before=0, cp_after=0)
    assert result == "underdefended piece, lost the exchange"


def test_classify_allowed_a_strong_check_or_fork():
    result = _classify("4k3/8/5b2/8/8/8/7P/4K3 w - - 0 1", "h2h3", "h2h3",
                        chess.WHITE, "f6c3", cp_before=0, cp_after=0)
    assert result == "allowed a strong check or fork"


def test_classify_missed_a_capture_winning_material():
    # Best was Qxe5 (wins a knight); White played an unrelated king move instead.
    result = _classify("4k3/8/8/4n3/8/8/4Q3/4K3 w - - 0 1", "e1f1", "e2e5",
                        chess.WHITE, None, cp_before=0, cp_after=0)
    assert result == "missed a capture winning material"


def test_classify_falls_back_to_positional_or_planning_error():
    result = _classify("4k3/8/8/8/8/8/8/4K3 w - - 0 1", "e1d1", "e1f1",
                        chess.WHITE, None, cp_before=0, cp_after=0)
    assert result == "positional or planning error"
