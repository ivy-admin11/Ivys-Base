"""Proactive agents: standardized run() signature, fake-pick removal, and
text-first delivery — every job must put its content in the message body on
every run, and must never push a PDF attachment unasked. Every test mocks
messaging/LLM/PDF calls — none of these send a real iMessage or call a
real external API.
"""

import inspect

import pytest

from ivy_core import text_delivery
from proactive_agents import Familia_meal_planner, happy_hour_scout, sports_bettor

AGENT_MODULES = [sports_bettor, happy_hour_scout, Familia_meal_planner]


@pytest.mark.parametrize("module", AGENT_MODULES, ids=[m.__name__ for m in AGENT_MODULES])
def test_run_has_standardized_keyword_only_signature(module):
    sig = inspect.signature(module.run)
    for name in ("force", "send", "requester", "request_id"):
        assert name in sig.parameters, f"{module.__name__}.run missing param '{name}'"
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY


def test_sports_bettor_has_no_fake_pick_injection():
    source = inspect.getsource(sports_bettor)
    assert "@Sharp1" not in source
    assert "HR Derby" not in source
    assert "TEST INJECTION" not in source


def test_sports_bettor_no_picks_does_not_send_when_send_false(monkeypatch):
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: [])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [])
    sent = []
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: sent.append(a) or True)

    result = sports_bettor.run(force=True, send=False)

    from ivy_core.pipeline_status import PipelineStatus

    assert result["status"] == PipelineStatus.NO_QUALIFYING_PICKS.value
    assert result["picks"] == 0
    assert result["sent"] is False
    assert sent == []


def test_sports_bettor_texts_the_picks_and_never_pushes_a_pdf(monkeypatch, tmp_path):
    """The regression that started all this: a PDF-only send that came back
    'submitted_unverified' was treated as success, so nothing was ever texted
    and Henry got silence. Picks must now always arrive as message text."""
    fake_pdf = tmp_path / "fake_picks.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real", "matchup": "A vs B"}])
    monkeypatch.setattr(
        sports_bettor, "merge_picks",
        lambda picks: [{
            "is_consensus": False, "consensus_count": 1,
            "enrichment": {"confidence": "high", "take": "Sharp side."},
            "sport": "MLB", "matchup": "A vs B", "side": "A", "odds": "-110",
            "handicappers": ["realSharp"],
        }],
    )
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "_report_signature", lambda merged: "sig-1")
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    saved = []
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: saved.append((sig, msg)))
    monkeypatch.setattr(sports_bettor, "format_picks_pdf", lambda merged: str(fake_pdf))

    sent = []
    monkeypatch.setattr(
        text_delivery, "send_imessage",
        lambda phone, body: sent.append((phone, body)) or True,
    )
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    result = sports_bettor.run(force=True, send=True)

    assert sent, "the picks were never texted"
    joined = "\n".join(b for _, b in sent)
    assert "A vs B" in joined and "-110" in joined, "the text didn't carry the actual pick"
    assert result["sent"] is True
    assert result["attached"] is False, "a PDF must not be pushed unasked"
    # The fingerprint is only stamped once the text actually went out.
    assert saved and saved[0][1] != saved[0][0], "last-report body must be the message, not the hash"


def test_sports_bettor_does_not_stamp_fingerprint_when_text_fails(monkeypatch, tmp_path):
    """A failed send must leave the slate resendable on the next run."""
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real"}])
    monkeypatch.setattr(
        sports_bettor, "merge_picks",
        lambda picks: [{
            "is_consensus": True, "consensus_count": 2,
            "enrichment": {"confidence": "high"},
            "sport": "NFL", "matchup": "C vs D", "side": "C", "odds": "+100",
        }],
    )
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    monkeypatch.setattr(sports_bettor, "format_picks_pdf", lambda merged: None)
    saved = []
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: saved.append(sig))
    monkeypatch.setattr(text_delivery, "send_imessage", lambda phone, body: False)
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    result = sports_bettor.run(force=True, send=True)

    assert saved == [], "a failed delivery must not suppress the next run"
    assert result["sent"] is False


def test_picks_digest_is_concise_and_keeps_every_pick_in_detail():
    picks = [
        {"sport": "MLB", "matchup": f"T{i} vs U{i}", "side": "ML", "odds": "-110",
         "is_consensus": i < 2, "consensus_count": 3 if i < 2 else 1,
         "handicappers": ["a", "b", "c"][: 3 if i < 2 else 1],
         "enrichment": {"confidence": "high"}}
        for i in range(9)
    ]
    body, detail = sports_bettor.format_picks_digest(picks)

    assert len(body) <= 1200, "the first bubble must stay skimmable"
    assert body.count("\n1. ") + body.startswith("1. ") >= 1
    assert "reply MORE" in body, "held-back picks must be discoverable"
    assert len(detail["items"]) == 9, "MORE/WHY must be able to reach every pick"
    assert detail["shown"] == sports_bettor.DIGEST_TOP_N
    assert all(item["detail"] for item in detail["items"]), "every pick needs a WHY answer"


def test_familia_meal_planner_texts_the_plan_and_never_pushes_a_pdf(monkeypatch, tmp_path):
    fake_pdf = tmp_path / "fake_meal.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(Familia_meal_planner._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(Familia_meal_planner, "check_48h_gate", lambda force=False: True)
    monkeypatch.setattr(
        Familia_meal_planner, "generate_family_meal_plan",
        lambda: {"status": "success", "recipe_count": 2, "recipes": [
            {"recipe_name": "Arepas de Pollo", "cuisine_origin": "Venezuelan",
             "prep_time_minutes": 15, "cooking_time_minutes": 20,
             "toddler_adaptations": ["shred the chicken"]},
            {"recipe_name": "Miso Butter Salmon", "cuisine_origin": "Asian fusion",
             "prep_time_minutes": 10, "cooking_time_minutes": 15},
        ]},
    )
    monkeypatch.setattr(Familia_meal_planner, "format_meal_plan_pdf", lambda data: str(fake_pdf))
    monkeypatch.setattr(Familia_meal_planner, "load_state", lambda: {"execution_history": []})
    monkeypatch.setattr(Familia_meal_planner, "save_state", lambda state: None)

    sent = []
    monkeypatch.setattr(
        text_delivery, "send_imessage",
        lambda phone, body: sent.append((phone, body)) or True,
    )
    monkeypatch.setattr(Familia_meal_planner, "send_imessage", lambda *a, **k: True)

    result = Familia_meal_planner.run(force=True, send=True)

    joined = "\n".join(b for _, b in sent)
    assert "Arepas de Pollo" in joined, "the plan was never texted"
    assert result["status"] == "success"


def test_familia_meal_planner_force_bypasses_48h_gate():
    assert Familia_meal_planner.check_48h_gate(force=True) is True


def test_happy_hour_scout_texts_the_specials_and_never_pushes_a_pdf(monkeypatch, tmp_path):
    fake_pdf = tmp_path / "fake_hh.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(happy_hour_scout._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(
        happy_hour_scout, "fetch_local_specials",
        lambda: {
            "venues": [{"name": "Hudson House", "region": "Frisco, TX"}],
            "specials": [{"venue": "Hudson House", "detail": "half-price oysters",
                          "days_hours": "Mon-Fri 3-6"}],
        },
    )
    monkeypatch.setattr(happy_hour_scout, "format_happy_hour_pdf", lambda data: str(fake_pdf))

    sent = []
    monkeypatch.setattr(
        text_delivery, "send_imessage",
        lambda phone, body: sent.append((phone, body)) or True,
    )
    monkeypatch.setattr(happy_hour_scout, "send_imessage", lambda *a, **k: True)

    result = happy_hour_scout.run(force=True, send=True)

    joined = "\n".join(b for _, b in sent)
    assert "Hudson House" in joined and "oysters" in joined, "the specials were never texted"
    assert result["status"] == "success"


def test_sports_bettor_speaks_up_when_picks_exist_but_none_qualify(monkeypatch, tmp_path):
    """The most recent real run: 7 picks swept, 0 cleared the threshold, and the
    job returned in total silence — indistinguishable from not running."""
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real"}])
    monkeypatch.setattr(
        sports_bettor, "merge_picks",
        lambda picks: [{
            "is_consensus": False, "consensus_count": 1,
            "enrichment": {"confidence": "low"},
            "sport": "MLB", "matchup": f"E{i} vs F{i}", "side": "ML", "odds": "-110",
        } for i in range(7)],
    )
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: None)

    sent = []
    monkeypatch.setattr(
        text_delivery, "send_imessage",
        lambda phone, body: sent.append(body) or True,
    )
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    result = sports_bettor.run(force=True, send=True)

    joined = "\n".join(sent)
    assert sent, "a swept-but-filtered board must still be reported"
    assert "E0 vs F0" in joined
    assert "cleared the bar" in joined, "the message must not read like a play"
    assert result["picks"] == 0


def test_sports_bettor_stays_quiet_on_an_unchanged_below_bar_board(monkeypatch, tmp_path):
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real"}])
    monkeypatch.setattr(
        sports_bettor, "merge_picks",
        lambda picks: [{
            "is_consensus": False, "consensus_count": 1,
            "enrichment": {"confidence": "low"},
            "sport": "MLB", "matchup": "G vs H", "side": "ML", "odds": "-110",
        }],
    )
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: None)

    picks_for_sig = [{
        "is_consensus": False, "consensus_count": 1,
        "sport": "MLB", "matchup": "G vs H", "side": "ML", "odds": "-110",
    }]
    monkeypatch.setattr(
        sports_bettor, "load_last_report",
        lambda: {"signature": sports_bettor._report_signature(picks_for_sig)},
    )

    sent = []
    monkeypatch.setattr(text_delivery, "send_imessage", lambda phone, body: sent.append(body) or True)
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    sports_bettor.run(force=False, send=True)

    assert sent == [], "an unchanged below-the-bar board must not nag"


def test_player_props_do_not_borrow_the_game_market_price():
    """Live run 2026-09-02: 'David Peterson Under 4.5 Strikeouts' was texted with
    '(Over 8 (-117) / Under 8 (-103))' — the game's run total, not the prop's
    price, and a contradicting number besides."""
    games = [{
        "away": "Milwaukee Brewers", "home": "Chicago Cubs", "sport": "MLB",
        "total": "Over 8 (-117) / Under 8 (-103)",
        "moneyline": "MIL +105 / CHC -125", "spread": "MIL +1.5",
        "commence": "2026-09-03T23:15:00Z",
    }]
    picks = [
        {"matchup": "Milwaukee Brewers @ Chicago Cubs",
         "side": "David Peterson Under 4.5 Strikeouts"},
        {"matchup": "Milwaukee Brewers @ Chicago Cubs", "side": "Under 8.5"},
    ]
    sports_bettor.attach_odds(picks, games)

    assert not picks[0].get("odds"), "a prop must not inherit the game total"
    assert picks[0]["sport"] == "MLB", "sport and start time still backfill"
    assert picks[0]["start"] == "2026-09-03T23:15:00Z"
    # "Under 8.5" takes the under half, not the whole market.
    assert picks[1]["odds"] == "Under 8 (-103)", "real game totals still fill, narrowed to the side"


def test_home_run_props_do_not_borrow_the_game_moneyline():
    """Sep 3 9am report: 'Coby Mayo HR (Baltimore Orioles +108 / Boston Red Sox
    -126)' — the game moneyline beside a home-run prop. 'HR' matched none of the
    long-form stat words, so the prop guard let it through."""
    games = [{
        "away": "Boston Red Sox", "home": "Baltimore Orioles", "sport": "MLB",
        "moneyline": "Baltimore Orioles +108 / Boston Red Sox -126",
        "total": "Over 9 (-110)", "spread": "BAL +1.5",
        "commence": "2026-09-03T23:15:00Z",
    }]
    picks = [
        {"matchup": "Boston Red Sox @ Baltimore Orioles", "side": "Coby Mayo HR"},
        {"matchup": "Boston Red Sox @ Baltimore Orioles", "side": "Baltimore Orioles ML"},
    ]
    sports_bettor.attach_odds(picks, games)

    assert not picks[0].get("odds"), "an HR prop must not inherit the game moneyline"
    # Narrowed to the side taken — the full two-sided market is the old bug.
    assert picks[1]["odds"] == "Baltimore Orioles +108"


def test_prop_guard_does_not_fire_on_team_names():
    """'KT Wiz' contains a K; 'Hanwha' contains hr. Whole words only."""
    assert not sports_bettor._is_player_prop("KT Wiz ML")
    assert not sports_bettor._is_player_prop("Hanwha Eagles ML")
    assert not sports_bettor._is_player_prop("Los Angeles Dodgers -1.5")
    assert sports_bettor._is_player_prop("Coby Mayo HR")
    assert sports_bettor._is_player_prop("Luka 30+ PTS")


class TestNCAAFCoverage:
    """College football has to be added in three places at once. Missing any
    one of them fails quietly: no games, or picks the sweep labels 'NFL'."""

    def test_ncaaf_is_in_the_odds_feed(self):
        assert sports_bettor.ODDS_SPORT_KEYS.get("NCAAF") == "americanfootball_ncaaf"

    def test_ncaaf_is_reachable_by_the_sweep(self):
        """The sweep is unrestricted now, so college football is covered by
        default. The hashtag hints still name it, because sharps post "#CFB"
        far more than they post the words and that biases the search."""
        assert "#NCAAF" in sports_bettor.SPORT_HINTS
        assert "#CFB" in sports_bettor.SPORT_HINTS
        prompt = sports_bettor._build_sweep_prompt(["h"], "", sports_bettor.SPORT_HINTS)
        assert "college football" in prompt.lower()

    def test_sweep_prompt_names_ncaaf_as_a_league_value(self):
        prompt = sports_bettor._build_sweep_prompt(["someHandle"], "", sports_bettor.SPORT_QUERY)
        assert "NCAAF" in prompt
        assert "never NFL" in prompt, "Grok mislabels college games as NFL without this"

    def test_ncaaf_picks_render_with_a_football_emoji(self):
        body, _ = sports_bettor.format_picks_digest([{
            "sport": "NCAAF", "matchup": "Ohio State Buckeyes @ Texas Longhorns",
            "side": "Texas +7", "odds": "-110",
            "is_consensus": True, "consensus_count": 2, "handicappers": ["a", "b"],
        }])
        assert "\U0001F3C8" in body
        assert "Texas +7" in body

    def test_ncaaf_odds_attach_by_team_name(self):
        games = [{
            "away": "Ohio State Buckeyes", "home": "Texas Longhorns", "sport": "NCAAF",
            "spread": "Texas +7 (-110)", "moneyline": "OSU -280 / TEX +230",
            "total": "Over 54.5", "commence": "2026-09-05T23:30:00Z",
        }]
        picks = [{"matchup": "Ohio State Buckeyes @ Texas Longhorns", "side": "Texas +7"}]
        sports_bettor.attach_odds(picks, games)
        assert picks[0]["sport"] == "NCAAF"
        assert picks[0]["odds"] == "Texas +7 (-110)"


class TestHandleVetting:
    """scripts/vet_x_handles.py — the repeatable version of the check that
    should have caught the dead 16-handle list. Never calls X in tests."""

    @staticmethod
    def _mod():
        import importlib.util, pathlib
        path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "vet_x_handles.py"
        spec = importlib.util.spec_from_file_location("vet_x_handles", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_silent_zero_handles_are_reported_not_dropped(self):
        vet = self._mod()
        results = vet.vet(["live", "dead"], sweep=lambda batch, slate: [
            {"handicapper": "live", "sport": "NCAAF", "matchup": "A @ B", "side": "B +7"}
        ])
        assert results["dead"] == [], "a handle returning nothing must still appear"
        assert len(results["live"]) == 1

    def test_handle_casing_from_grok_is_tolerated(self):
        vet = self._mod()
        results = vet.vet(["KyleHunterPicks"], sweep=lambda batch, slate: [
            {"handicapper": "@kylehunterpicks", "sport": "NCAAF", "matchup": "A @ B", "side": "B"}
        ])
        assert len(results["KyleHunterPicks"]) == 1

    def test_candidates_exclude_the_known_non_bettors(self):
        vet = self._mod()
        for bad in ("sharpfootball", "ToddFuhrman", "vegasbedwards", "stevejanus"):
            assert bad not in vet.CANDIDATES, f"{bad} does not post free bettable picks"

    def test_candidates_are_not_already_in_the_sweep(self):
        vet = self._mod()
        overlap = set(vet.CANDIDATES) & set(sports_bettor.TARGET_X_ACCOUNTS)
        assert not overlap, f"already swept: {overlap}"


class TestCurrentHandleAudit:
    """--current answers "does anyone I already follow post <sport>?" without
    adding anyone. A zero there means quiet, not unusable."""

    @staticmethod
    def _mod():
        import importlib.util, pathlib
        path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "vet_x_handles.py"
        spec = importlib.util.spec_from_file_location("vet_x_handles_audit", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_audit_wording_does_not_tell_you_to_drop_a_quiet_handle(self, capsys):
        vet = self._mod()
        vet.report({"quietOne": []}, auditing=True)
        out = capsys.readouterr().out
        assert "do not add" not in out
        assert "nothing bettable" in out

    def test_audit_summarises_the_leagues_actually_posted(self, capsys):
        vet = self._mod()
        vet.report({
            "ItsCappersPicks": [
                {"sport": "NCAAF", "matchup": "A @ B", "side": "B +7"},
                {"sport": "MLB", "matchup": "C @ D", "side": "D ML"},
            ],
        }, auditing=True)
        out = capsys.readouterr().out
        assert "MLB, NCAAF" in out

    def test_current_flag_targets_the_live_sweep_list(self):
        vet = self._mod()
        assert "ItsCappersPicks" in vet.TARGET_X_ACCOUNTS


class TestCardAnalysis:
    """The old Context column read enrichment["summary"], a key nothing writes,
    so it was blank in every PDF ever produced. The card analysis line carries
    the take and the market notes; grade and attribution sit on the card's meta
    line and are deliberately not repeated."""

    PICK = {
        "sport": "NCAAF", "matchup": "Ohio State Buckeyes @ Texas Longhorns",
        "side": "Texas +7", "odds": "+7 (-110)",
        "is_consensus": True, "consensus_count": 3,
        "handicappers": ["ItsCappersPicks", "billhpicks"],
        "enrichment": {
            "confidence": "high",
            "take": "Line opened +9.5 and got bet to +7.",
            "line_movement": "+9.5 -> +7",
            "injury": "OSU LT questionable",
            "sharp_public": "71% of money on TEX",
        },
    }

    def test_analysis_carries_the_take_and_the_market_notes(self):
        out = sports_bettor._pick_analysis(self.PICK)
        for fragment in ("Line opened +9.5", "Line: +9.5", "Inj: OSU LT questionable",
                         "71% of money"):
            assert fragment in out, fragment

    def test_analysis_never_reads_the_nonexistent_summary_key(self):
        pick = dict(self.PICK, enrichment={"summary": "ignored legacy field"})
        assert sports_bettor._pick_analysis(pick) == ""

    def test_placeholder_takes_are_dropped(self):
        pick = dict(self.PICK, enrichment={"take": "no data available"})
        assert sports_bettor._pick_analysis(pick) == ""

    def test_markup_characters_are_escaped(self):
        pick = dict(self.PICK, enrichment={"take": "Top-10 SP+ & <best> in the country"})
        out = sports_bettor._pick_analysis(pick)
        assert "&amp;" in out and "&lt;best&gt;" in out

    def test_every_card_gets_its_analysis_and_a_signal_note(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(sports_bettor, "build_dashboard",
                            lambda path, picks, **kw: captured.update(picks=picks, **kw) or path)
        sports_bettor.format_picks_pdf([dict(self.PICK)])
        assert "Line opened +9.5" in captured["picks"][0]["analysis"]
        assert "consensus play" in captured["signal_note"]


def test_team_totals_do_not_borrow_the_game_total():
    """Real board 2026-09-05 09:49: "Auburn Tigers TT Over 34.5" was printed
    with "(Over 58.5 (-115) / Under 58.5 (-105))" — the game total. A team
    total and a game total are different numbers for different bets."""
    games = [{
        "away": "Baylor Bears", "home": "Auburn Tigers", "sport": "NCAAF",
        "total": "Over 58.5 (-115) / Under 58.5 (-105)",
        "spread": "Auburn -7.5 (-105)", "moneyline": "AUB -280",
        "commence": "2026-09-05T19:30:00Z",
    }]
    picks = [
        {"matchup": "Baylor Bears @ Auburn Tigers", "side": "Auburn Tigers TT Over 34.5"},
        {"matchup": "Baylor Bears @ Auburn Tigers", "side": "Over 58.5"},
    ]
    sports_bettor.attach_odds(picks, games)
    assert not picks[0].get("odds"), "a team total must not inherit the game total"
    assert picks[1]["odds"] == "Over 58.5 (-115)", "real game totals still fill, narrowed to the side"


def test_prop_guard_leaves_ordinary_spreads_alone():
    assert not sports_bettor._is_player_prop("Auburn Tigers -7")
    assert not sports_bettor._is_player_prop("Oregon Ducks -24")
    assert sports_bettor._is_player_prop("Auburn Tigers TT Over 34.5")


class TestOddsNarrowing:
    """The Odds column printed the whole two-sided market. Real 09:49 board:
    "LSU Tigers 1H -6.5" was shown with "Clemson Tigers +10.5 (-118) / LSU
    Tigers -10.5 (-104)" — both sides, and a full-game line beside a 1H bet."""

    SPREAD = "Clemson Tigers +10.5 (-118) / LSU Tigers -10.5 (-104)"
    TOTAL = "Over 58.5 (-115) / Under 58.5 (-105)"
    ML = "Baltimore Orioles +108 / Boston Red Sox -126"

    def test_spread_takes_the_side_actually_bet(self):
        assert sports_bettor._price_for_side("Clemson Tigers +10.5", self.SPREAD) == "Clemson Tigers +10.5 (-118)"
        assert sports_bettor._price_for_side("LSU Tigers -10.5", self.SPREAD) == "LSU Tigers -10.5 (-104)"

    def test_totals_take_the_matching_half(self):
        assert sports_bettor._price_for_side("Over 58.5", self.TOTAL) == "Over 58.5 (-115)"
        assert sports_bettor._price_for_side("Under 58.5", self.TOTAL) == "Under 58.5 (-105)"

    def test_moneyline_takes_the_named_team(self):
        assert sports_bettor._price_for_side("Baltimore Orioles ML", self.ML) == "Baltimore Orioles +108"

    def test_period_bets_get_no_price(self):
        """A 1H line has no counterpart in a full-game feed."""
        for side in ("LSU Tigers 1H -6.5", "Oregon 2H -3", "Army first half Over 24"):
            assert sports_bettor._price_for_side(side, self.SPREAD) == "", side

    def test_unreadable_side_gets_no_price_rather_than_a_guess(self):
        assert sports_bettor._price_for_side("something odd", self.SPREAD) == ""

    def test_one_sided_market_passes_through(self):
        assert sports_bettor._price_for_side("Auburn -7.5", "Auburn -7.5 (-105)") == "Auburn -7.5 (-105)"

    def test_grok_supplied_two_sided_odds_are_narrowed_too(self):
        picks = [{"matchup": "Clemson Tigers @ LSU Tigers", "side": "Clemson Tigers +10.5",
                  "odds": self.SPREAD}]
        sports_bettor.attach_odds(picks, [])
        assert picks[0]["odds"] == "Clemson Tigers +10.5 (-118)"


class TestConflictingPicks:
    """The 09:49 board carried Liberty +6 and James Madison -6 — same game,
    opposite sides, same handle. Together they lose to the vig."""

    def test_opposing_spreads_are_both_dropped(self):
        board = [
            {"matchup": "Liberty Flames @ James Madison Dukes", "side": "Liberty Flames +6"},
            {"matchup": "Liberty Flames @ James Madison Dukes", "side": "James Madison -6"},
        ]
        kept, conflicts = sports_bettor.drop_conflicting_picks(board)
        assert kept == []
        assert len(conflicts) == 1 and len(conflicts[0]) == 2

    def test_over_and_under_on_one_game_are_dropped(self):
        board = [
            {"matchup": "Bryant @ Army", "side": "Over 50.5"},
            {"matchup": "Bryant @ Army", "side": "Under 50.5"},
        ]
        kept, _ = sports_bettor.drop_conflicting_picks(board)
        assert kept == []

    def test_a_spread_and_a_total_on_one_game_both_survive(self):
        board = [
            {"matchup": "Baylor Bears @ Auburn Tigers", "side": "Auburn Tigers -7"},
            {"matchup": "Baylor Bears @ Auburn Tigers", "side": "Auburn Tigers TT Over 34.5"},
        ]
        kept, conflicts = sports_bettor.drop_conflicting_picks(board)
        assert len(kept) == 2 and conflicts == []

    def test_a_period_bet_does_not_conflict_with_the_full_game(self):
        board = [
            {"matchup": "Clemson Tigers @ LSU Tigers", "side": "LSU Tigers 1H -6.5"},
            {"matchup": "Clemson Tigers @ LSU Tigers", "side": "Clemson Tigers +10.5"},
        ]
        kept, conflicts = sports_bettor.drop_conflicting_picks(board)
        assert len(kept) == 2 and conflicts == []

    def test_same_team_on_spread_and_moneyline_is_not_a_conflict(self):
        board = [
            {"matchup": "Baylor Bears @ Auburn Tigers", "side": "Auburn Tigers -7"},
            {"matchup": "Baylor Bears @ Auburn Tigers", "side": "Auburn Tigers ML"},
        ]
        kept, conflicts = sports_bettor.drop_conflicting_picks(board)
        assert len(kept) == 2 and conflicts == []

    def test_reversed_matchup_order_still_matches_the_same_game(self):
        board = [
            {"matchup": "Liberty Flames @ James Madison Dukes", "side": "Liberty Flames +6"},
            {"matchup": "James Madison Dukes vs Liberty Flames", "side": "James Madison Dukes -6"},
        ]
        kept, _ = sports_bettor.drop_conflicting_picks(board)
        assert kept == []


def test_below_threshold_board_names_a_single_source_as_the_cause(monkeypatch, tmp_path):
    """14 picks from one handle can never reach a 2-sharp consensus. Saying
    'nothing qualified' without saying why reads as a quiet slate."""
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["g"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@x"}])
    monkeypatch.setattr(sports_bettor, "merge_picks", lambda picks: [
        {"is_consensus": False, "consensus_count": 1, "handicappers": ["cappersforfree"],
         "sport": "NCAAF", "matchup": f"A{i} @ B{i}", "side": "B -3", "odds": "-110",
         "enrichment": {"confidence": "low"}} for i in range(5)
    ])
    monkeypatch.setattr(sports_bettor, "save_picks", lambda p, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda m, g: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda m, g: None)
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda s, m: None)

    sent = []
    monkeypatch.setattr(text_delivery, "send_imessage", lambda phone, body: sent.append(body) or True)
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    sports_bettor.run(force=True, send=True)

    joined = "\n".join(sent)
    assert "@cappersforfree" in joined
    assert "coverage gap" in joined


def test_a_game_market_copied_onto_a_prop_is_cleared_not_narrowed():
    """Real 09:49 board: "Auburn Tigers TT Over 34.5" arrived carrying the
    game total. Narrowing it to "Over 58.5" is still the wrong bet's price."""
    picks = [{"matchup": "Baylor Bears @ Auburn Tigers",
              "side": "Auburn Tigers TT Over 34.5",
              "odds": "Over 58.5 (-115) / Under 58.5 (-105)"}]
    sports_bettor.attach_odds(picks, [])
    assert picks[0]["odds"] == ""


def test_a_genuine_prop_price_from_grok_survives():
    picks = [{"matchup": "A @ B", "side": "Coby Mayo HR", "odds": "+450"}]
    sports_bettor.attach_odds(picks, [])
    assert picks[0]["odds"] == "+450"


class TestSignalNote:
    """The old summary read "12 pick(s) sourced from X handicappers — 0
    consensus play(s) with 2+ sharps agreeing": X is the platform but scanned
    as a missing number, and a printed zero explained nothing. Counts now live
    in the stat tiles, so the banner carries only the reason."""

    def test_single_source_is_named_as_the_cause(self):
        note = sports_bettor._signal_note([{}], [], {"cappersforfree"})
        assert "single source" in note
        assert "0 consensus" not in note

    def test_multiple_sources_with_no_agreement_say_that_instead(self):
        note = sports_bettor._signal_note([{}, {}], [], {"a", "b"})
        assert "no two landed on the same one" in note
        assert "single source" not in note

    def test_consensus_is_reported_with_real_plurals(self):
        assert "1 consensus play on the board" in sports_bettor._signal_note([{}], [{}], {"a", "b"})
        assert "2 consensus plays on the board" in sports_bettor._signal_note([{}], [{}, {}], {"a", "b"})

    def test_no_parenthesised_plurals_anywhere(self):
        for args in (([{}], [], {"a"}), ([{}], [{}], {"a", "b"}), ([{}], [], {"a", "b"})):
            assert "(s)" not in sports_bettor._signal_note(*args)

    def test_count_helper_pluralises(self):
        assert sports_bettor._count(1, "pick") == "1 pick"
        assert sports_bettor._count(0, "pick") == "0 picks"
        assert sports_bettor._count(2, "consensus play") == "2 consensus plays"


class TestDashboard:
    """The board Henry asked for: stat tiles, a signal banner, and cards
    attributed to the handicapper rather than to "X Sharp Picks"."""

    def test_attribution_names_the_handicapper_not_the_platform(self):
        from picks_dashboard import _attribution
        assert _attribution({"handicappers": ["cappersforfree"]}) == "@cappersforfree"
        assert _attribution({"handicappers": ["a", "b"]}) == "@a, @b"
        assert _attribution({"handicappers": ["a", "b", "c"]}) == "@a +2 more"
        assert _attribution({"handicappers": []}) == "unattributed"

    def test_market_badges_describe_the_bet(self):
        from picks_dashboard import market_badge
        assert market_badge("Auburn Tigers -7") == "SPREAD"
        assert market_badge("Over 50.5") == "TOTAL"
        assert market_badge("Auburn Tigers TT Over 34.5") == "TEAM TOTAL"
        assert market_badge("LSU Tigers 1H -6.5") == "1H SPREAD"
        assert market_badge("Indiana Hoosiers 1st Half -24.5") == "1H SPREAD"
        assert market_badge("Baltimore Orioles ML") == "MONEYLINE"
        assert market_badge("Coby Mayo HR") == "PROP"

    def test_odds_label_drops_the_repeated_team_name(self):
        from picks_dashboard import _odds_label
        assert _odds_label({"side": "Tennessee -50",
                            "odds": "Tennessee Volunteers -48.5 (-110)"}) == "-48.5 (-110)"
        assert _odds_label({"side": "Baltimore Orioles ML",
                            "odds": "Baltimore Orioles +108"}) == "+108"

    def test_odds_label_keeps_over_under_which_is_the_bet_itself(self):
        from picks_dashboard import _odds_label
        assert _odds_label({"side": "Over 50.5", "odds": "Over 50.5 (-110)"}) == "Over 50.5 (-110)"

    def test_a_missing_price_says_so(self):
        from picks_dashboard import _odds_label
        assert _odds_label({"side": "LSU Tigers 1H -6.5", "odds": ""}) == "No line"

    def test_it_builds_a_real_pdf(self, tmp_path):
        from picks_dashboard import build_dashboard
        out = tmp_path / "d.pdf"
        picks = [{"matchup": "A @ B", "side": "B -7", "handicappers": ["one"],
                  "consensus_count": 1, "sport": "NCAAF", "odds": "-110"} for _ in range(12)]
        build_dashboard(str(out), picks, signal_note="note", confidence_label="LOW")
        assert out.exists() and out.read_bytes().startswith(b"%PDF")
        assert out.stat().st_size > 1000

    def test_an_empty_board_still_renders(self, tmp_path):
        from picks_dashboard import build_dashboard
        out = tmp_path / "empty.pdf"
        build_dashboard(str(out), [])
        assert out.exists() and out.read_bytes().startswith(b"%PDF")


class TestSinglePageGuarantee:
    """A board is a card you glance at; page 2 of a glance is a contradiction.
    The builder measures each density rung and takes the densest that fits,
    then truncates with a note rather than ever flowing to a second page."""

    @staticmethod
    def _board(n, long_names=True):
        name = "Some Long University Team" if long_names else "A"
        return [{
            "matchup": f"{name} {i} @ Another Long Team {i}",
            "side": f"Another Long Team {i} -{i % 20}.5", "odds": "-110",
            "handicappers": ["cappersforfree"], "consensus_count": 1,
            "sport": "NCAAF", "start_label": "Sat Sep 5, 2:30 PM CT",
            "analysis": "Line opened +9.5 and got bet to +7.<br/>Line: +9.5 -&gt; +7",
        } for i in range(1, n + 1)]

    @pytest.mark.parametrize("n", [1, 6, 12, 18, 24, 32, 48, 80, 150])
    def test_never_more_than_one_page(self, tmp_path, n):
        from pypdf import PdfReader
        from picks_dashboard import build_dashboard
        out = tmp_path / f"b{n}.pdf"
        build_dashboard(str(out), self._board(n),
                        signal_note="No consensus plays detected.", confidence_label="LOW")
        assert len(PdfReader(str(out)).pages) == 1, f"{n} picks spilled to a second page"

    def test_a_small_board_keeps_the_roomy_layout(self):
        """Density must not collapse just because it can — 12 picks is the
        reference layout and should stay two columns."""
        from picks_dashboard import DENSITIES
        assert DENSITIES[0].cols == 2
        assert DENSITIES[0].show_analysis is True
        assert DENSITIES[-1].cols >= DENSITIES[0].cols, "the ladder must get denser, not looser"

    def test_densities_are_ordered_loosest_to_densest(self):
        from picks_dashboard import DENSITIES
        sizes = [d.pick.fontSize for d in DENSITIES]
        assert sizes == sorted(sizes, reverse=True)
        cols = [d.cols for d in DENSITIES]
        assert cols == sorted(cols)

    def test_an_unfittable_board_says_what_it_left_off(self, tmp_path):
        """Truncation has to be visible — a silently short board is the same
        class of bug as a silently missing report."""
        from pypdf import PdfReader
        from picks_dashboard import build_dashboard
        out = tmp_path / "huge.pdf"
        build_dashboard(str(out), self._board(400), signal_note="x", confidence_label="LOW")
        reader = PdfReader(str(out))
        assert len(reader.pages) == 1
        assert "not shown" in reader.pages[0].extract_text()


class TestAllSportsCoverage:
    """The sweep used to be bounded by a hand-maintained league list, which is
    how college football stayed invisible for a season. It is now unbounded,
    and the odds feed discovers what is in season instead of being told."""

    def test_the_prompt_does_not_restrict_to_a_league_list(self):
        prompt = sports_bettor._build_sweep_prompt(["h"], "", sports_bettor.SPORT_HINTS)
        assert "COVER EVERY SPORT AND LEAGUE" in prompt
        assert "NOT a filter" in prompt
        assert "Focus on these leagues" not in prompt

    def test_hints_bias_the_search_without_bounding_it(self):
        for tag in ("#NCAAF", "#WNBA", "#UFC", "#NHL", "#MLS"):
            assert tag in sports_bettor.SPORT_HINTS, tag

    def test_discovery_falls_back_to_the_seed_list_when_the_api_is_down(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sports_bettor, "SPORTS_CACHE_PATH", str(tmp_path / "c.json"))
        monkeypatch.setattr(sports_bettor.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        pairs = sports_bettor.discover_sport_keys(force=True)
        assert ("NCAAF", "americanfootball_ncaaf") in pairs
        assert len(pairs) == len(sports_bettor.ODDS_SPORT_KEYS)

    def test_discovery_keeps_in_season_head_to_head_sports_only(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sports_bettor, "SPORTS_CACHE_PATH", str(tmp_path / "c.json"))
        monkeypatch.setattr(sports_bettor, "ODDS_API_KEY", "test-key")
        catalogue = [
            {"key": "basketball_wnba", "title": "WNBA", "active": True, "has_outrights": False},
            {"key": "mma_mixed_martial_arts", "title": "MMA", "active": True, "has_outrights": False},
            {"key": "americanfootball_nfl_super_bowl_winner", "title": "Super Bowl Winner",
             "active": True, "has_outrights": True},
            {"key": "baseball_kbo", "title": "KBO", "active": False, "has_outrights": False},
        ]

        class R:
            def raise_for_status(self): pass
            def json(self): return catalogue

        monkeypatch.setattr(sports_bettor.requests, "get", lambda *a, **k: R())
        pairs = sports_bettor.discover_sport_keys(force=True)
        keys = [k for _, k in pairs]
        assert "basketball_wnba" in keys and "mma_mixed_martial_arts" in keys
        assert "americanfootball_nfl_super_bowl_winner" not in keys, "outrights have no h2h line"
        assert "baseball_kbo" not in keys, "out of season"

    def test_discovery_is_capped_and_cached(self, monkeypatch, tmp_path):
        cache = tmp_path / "c.json"
        monkeypatch.setattr(sports_bettor, "SPORTS_CACHE_PATH", str(cache))
        monkeypatch.setattr(sports_bettor, "ODDS_API_KEY", "test-key")
        monkeypatch.setattr(sports_bettor, "ODDS_MAX_LEAGUES", 2)
        catalogue = [{"key": f"sport_{i}", "title": f"S{i}", "active": True,
                      "has_outrights": False} for i in range(10)]

        calls = []

        class R:
            def raise_for_status(self): pass
            def json(self): return catalogue

        monkeypatch.setattr(sports_bettor.requests, "get",
                            lambda *a, **k: calls.append(1) or R())
        assert len(sports_bettor.discover_sport_keys(force=True)) == 2
        assert cache.exists()
        sports_bettor.discover_sport_keys()  # cached: no second call
        assert len(calls) == 1

    def test_api_keys_never_reach_a_log_line(self):
        leaky = ("HTTPSConnectionPool(host='api.the-odds-api.com') url: "
                 "/v4/sports?apiKey=99f379dfSECRETVALUE (Caused by ProxyError)")
        out = sports_bettor._redact(leaky)
        assert "SECRETVALUE" not in out
        assert "apiKey=***" in out

    def test_unknown_leagues_still_render(self):
        body, _ = sports_bettor.format_picks_digest([{
            "sport": "Cricket", "matchup": "India @ Australia", "side": "India ML",
            "odds": "-120", "is_consensus": False, "consensus_count": 1,
            "handicappers": ["someone"], "enrichment": {"confidence": "medium"},
        }])
        assert "India @ Australia" in body
