import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from draft_assistant.policies.consensus import AnalystRanks, ConsensusModel
from draft_assistant.research import load_waldman_redraft_csv
from draft_assistant.board import load_default_board
from draft_assistant.policies.source_board import SourceBoardPolicy


class ConsensusModelTests(unittest.TestCase):
    def test_rookie_wr_gives_harmon_the_largest_weight(self):
        result = ConsensusModel().score("WR", AnalystRanks(20, 18, 8, 60, True))
        self.assertEqual(dict(result.weights), {"jj": .20, "waldman": .35, "harmon": .45})
        self.assertGreater(result.score, .72)

    def test_tight_three_way_agreement_is_gold(self):
        result = ConsensusModel().score("WR", AnalystRanks(10, 11, 9, 60, False))
        self.assertEqual(result.label, "GOLD")

    def test_tight_late_round_three_way_agreement_is_still_gold(self):
        result = ConsensusModel().score("WR", AnalystRanks(48, 47, 49, 60, False))
        self.assertEqual(result.label, "GOLD")
        self.assertLess(result.score, .30)

    def test_a_single_positive_specialist_is_not_gold(self):
        result = ConsensusModel().score("WR", AnalystRanks(38, 40, 6, 60, True))
        self.assertNotEqual(result.label, "GOLD")

    def test_waldman_redraft_import_preserves_rank_projection_and_adp_edge(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "waldman.csv"
            path.write_text(
                "Rank,Player,Pos,Points,ADP,Rank vs ADP,Upside\n"
                "3,Matthew Stafford,QB3,317.55,111,+108,5.0\n",
                encoding="utf-8",
            )
            stafford = load_waldman_redraft_csv(path)["matthewstafford"]
            self.assertEqual((stafford.overall_rank, stafford.position_rank), (3, 3))
            self.assertEqual(stafford.projected_points, 317.55)
            self.assertEqual(stafford.rank_vs_adp, 108.0)

    def test_policy_uses_real_three_source_alignment_for_gold(self):
        board = load_default_board()
        records = load_waldman_redraft_csv(Path(r"C:\Users\kenik\Downloads\rankings-preseason-2026-all.csv"))
        player = board.by_normalized_name["pukanacua"]
        consensus = SourceBoardPolicy(waldman_redraft=records)._consensus(
            board, player, board.harmon_wr_adjustment(player.name)
        )
        self.assertIsNotNone(consensus)
        self.assertEqual(consensus.label, "GOLD")

    def test_reconciled_wr_order_does_not_double_count_harmon(self):
        board = load_default_board()
        records = load_waldman_redraft_csv(Path(r"C:\Users\kenik\Downloads\rankings-preseason-2026-all.csv"))
        policy = SourceBoardPolicy(waldman_redraft=records)
        from draft_assistant.models import DraftState

        result = policy.recommend(
            board,
            DraftState(
                league_id="league-1",
                username="kenikh",
                draft_status="drafting",
                round_number=3,
                pick_label="3.05",
                current_pick_number=29,
                our_pick_number=29,
                roster_positions=("QB", "RB"),
                available_players=frozenset(
                    {"Tee Higgins", "Nico Collins", "George Pickens", "Chris Olave"}
                ),
            ),
            limit=4,
        )

        names = [candidate.player.name for candidate in result.candidates]
        self.assertLess(names.index("Nico Collins"), names.index("Tee Higgins"))
        self.assertLess(names.index("George Pickens"), names.index("Tee Higgins"))
        self.assertLess(names.index("Chris Olave"), names.index("Tee Higgins"))


if __name__ == "__main__":
    unittest.main()
