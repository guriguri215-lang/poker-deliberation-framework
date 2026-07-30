from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest
from pydantic import ValidationError

from poker_deliberation.confirmed_review import (
    ConfirmedReviewError,
    admit_confirmed_review,
    authority_snapshot_sha256,
    candidate_sha256,
    confirmation_sha256,
    create_review_confirmation,
    prepare_review_intake,
)
from poker_deliberation.confirmed_review_models import (
    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
    MAX_CONFIRMED_REVIEW_SOURCE_BYTES,
    ConfirmedReviewDiagnosticCode,
    ReviewConfirmationAuthorityV1,
    ReviewIntakePreparationResultV1,
)
from poker_deliberation.security import _contains_secret_shape
from tests.confirmed_review_support import (
    SOURCE_BYTES,
    candidate_payload,
    ready_preparation,
)


def _prepare_source(source: bytes):
    return _prepare_source_with_payload(source, candidate_payload())


def _prepare_source_with_payload(source: bytes, payload: object):
    return prepare_review_intake(
        source,
        payload,
        source_id="source-unit-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"", ConfirmedReviewDiagnosticCode.SOURCE_SIZE),
        (b"\xef\xbb\xbfsource\n", ConfirmedReviewDiagnosticCode.SOURCE_BOM),
        (b"source\r\n", ConfirmedReviewDiagnosticCode.SOURCE_NEWLINE),
        (b"\xff", ConfirmedReviewDiagnosticCode.SOURCE_UTF8),
        ("e\u0301\n".encode(), ConfirmedReviewDiagnosticCode.SOURCE_NFC),
        (b"source\x00\n", ConfirmedReviewDiagnosticCode.SOURCE_CONTROL),
        (b"api_key=sk-abcdefgh\n", ConfirmedReviewDiagnosticCode.SOURCE_SECRET),
        (b"api key: ABCDEFGHIJKLMNOP123456\n", ConfirmedReviewDiagnosticCode.SOURCE_SECRET),
        (b"api  key: ABCDEFGHIJKLMNOP123456\n", ConfirmedReviewDiagnosticCode.SOURCE_SECRET),
        (b"api\tkey: ABCDEFGHIJKLMNOP123456\n", ConfirmedReviewDiagnosticCode.SOURCE_SECRET),
        (
            "api\u00a0key: ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (b"api.key: ABCDEFGHIJKLMNOP123456\n", ConfirmedReviewDiagnosticCode.SOURCE_SECRET),
        (b"api:_key=ABCDEFGHIJKLMNOP123456\n", ConfirmedReviewDiagnosticCode.SOURCE_SECRET),
        (b"api=_key=ABCDEFGHIJKLMNOP123456\n", ConfirmedReviewDiagnosticCode.SOURCE_SECRET),
        (
            "api\u2010key: ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "\uff21\uff30\uff29 \uff2b\uff25\uff39: ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            b"Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==\n",
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            b"Cookie: sessionid=ABCDEFGHIJKLMNOP123456\n",
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "api\ufe0f_key: ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            b"OPENAI_API_KEY=ABCDEFGHIJKLMNOP123456\n",
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            b"AWS_SECRET_ACCESS_KEY=ABCDEFGHIJKLMNOP123456\n",
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            b"GITHUB_TOKEN=ABCDEFGHIJKLMNOP123456\n",
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "api\u034f_key: ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "api\u180b_key=sk_test_never_store\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "api\u0600_key=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_CONTROL,
        ),
        (
            "api\ufff0_key=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "api\u0332_key: ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "\u0430pi_key=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "ap\u0456_key=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "api_k\u0435y=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "\u0455ecret=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "pa\u0455sword=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "\u0455\u0435\u0441\u0433\u0435\u0442=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            ("\u0440\u0430\u0455\u0455\u051d\u043e\u0433\u0501=ABCDEFGHIJKLMNOP123456\n").encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "\u0442\u043e\u043a\u0435\u043f=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            "aut\u04bborization=ABCDEFGHIJKLMNOP123456\n".encode(),
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            b"bearer=ABCDEFGHIJKLMNOP123456\n",
            ConfirmedReviewDiagnosticCode.SOURCE_SECRET,
        ),
        (
            b"I am mid-hand at the moment. What should I do?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"My time bank has 12 seconds remaining. Should I shove?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am facing a bet at this moment. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "今トナメ中です。コールかフォールドか教えて。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "トーナメントでプレイしてる。コールかフォールドか教えて。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am currently playing poker right now. What should I do?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "いまオンラインポーカー中です。次のアクションはcallとfoldのどちらですか?\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "いまオンライン卓に参加しています。次のアクションはcallとfoldのどちらですか?\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "ただいまオンラインポーカーを打っています。次のアクションを教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "現在オンライン卓に着席しています。次のアクションはcallとfoldのどちらですか?\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing online poker at the moment. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "オンラインポーカーに参加しております。コールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am in an online poker tournament. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am in an online poker tournament.\nShould I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "オンラインMTTに出場しています。\nコールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am in an online poker tournament. "
            + b"Stack and action details. " * 8
            + b"Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "オンラインMTTに出場しています。".encode()
            + "スタックとアクションの詳細です。".encode() * 10
            + "コールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am in an online MTT. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing an online MTT. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am in a sit-and-go. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am currently in an online poker tournament. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I'm playing in an online poker tournament. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing in a Spin & Go. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am at an online cash table. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I'm playing a PKO. Should I shove?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "オンラインキャッシュに参加しています。コールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "I am in an online \uff2d\uff34\uff34. Should I call or fold?\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "オンラインSNGに参加しています。コールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I'm multi-tabling online. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I'm on PokerStars right now. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I'm playing Zoom. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "オンラインで4面打ちしています。コールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am in an online poker tournament replay, but this is the actual "
            b"live tournament. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am in an online poker tournament hand-history viewer, but the hand "
            b"is actually live. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing an online MTT replay. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"The hand from yesterday is complete. "
            b"I am in an online poker tournament. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing an online MTT replay from yesterday, but I am playing it now. "
            b"Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing an online MTT replay from yesterday, but this tournament "
            b"is in progress. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing an online MTT replay from yesterday, but I am still playing "
            b"this tournament. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing an online MTT replay from yesterday, but this is my current "
            b"tournament. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I am playing an online MTT replay from yesterday, but the hand is happening "
            b"now. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        *[
            (
                (
                    "I am playing an online MTT replay from yesterday, but "
                    f"{active_status}. Should I call or fold?\n"
                ).encode(),
                ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
            )
            for active_status in (
                "the event is ongoing",
                "I am still competing in it",
                "this tournament has not ended yet",
                "I am still in this tournament",
                "the tournament hasn't ended yet",
                "cards are still being dealt",
                "we are still on the bubble",
                "play is ongoing",
                "this tournament remains active",
                "this contest is still underway",
                "the tournament continues today",
                "I still have chips in the event",
                "the tournament is not over",
                "the action is on me",
                "my decision timer is still running",
            )
        ],
        (
            b"I am playing an online MTT replay from yesterday. "
            + b"Historical stack details. " * 40
            + b"But the tournament is not over. Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "昨日のMTTリプレイを見ていますが、この大会は現在進行中です。"
            "コールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "昨日のMTTリプレイを見ています。この大会はまだ続いています。"
            "コールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"Yesterday's hand ended, but my current tournament is still running. "
            b"Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"I reviewed a completed hand yesterday, while my current MTT is ongoing. "
            b"Should I call or fold?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            "昨日のハンドは終了しましたが、現在のMTTは進行中です。"
            "コールかフォールドか教えてください。\n".encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            b"For retrospective review, the live video subtitle says "
            b'"the action is on me." Should I call or fold?\n',
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            (
                "\u6628\u65e5\u306e\u5b8c\u4e86\u30cf\u30f3\u30c9\u306e"
                "\u30ea\u30d7\u30ec\u30a4\u753b\u9762\u3092"
                "\u958b\u3044\u305f\u307e\u307e\u30d7\u30ec\u30a4\u4e2d\u3067\u3059\u3002"
                "\u30b3\u30fc\u30eb\u3059\u3079\u304d\uff1f\n"
            ).encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            (
                "\u6628\u65e5\u306e\u5b8c\u4e86\u30cf\u30f3\u30c9\u306e"
                "\u30ea\u30d7\u30ec\u30a4\u753b\u9762\u3092"
                "\u898b\u306a\u304c\u3089\u307e\u3060\u5353\u306b\u3044\u307e\u3059\u3002"
                "\u30b3\u30fc\u30eb\u3059\u3079\u304d\uff1f\n"
            ).encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            (
                "\u6628\u65e5\u306e\u5b8c\u4e86\u30cf\u30f3\u30c9\u306e"
                "\u30ea\u30d7\u30ec\u30a4\u753b\u9762\u306e\u6a2a\u3067"
                "\u30d7\u30ec\u30a4\u4e2d\u3067\u3059\u3002"
                "\u30b3\u30fc\u30eb\u3059\u3079\u304d\uff1f\n"
            ).encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
        (
            (
                "\u6628\u65e5\u306e\u5b8c\u4e86\u30cf\u30f3\u30c9\u306e"
                "\u30ea\u30d7\u30ec\u30a4\u753b\u9762\u3068\u306f\u5225\u306b"
                "\u5bfe\u6226\u4e2d\u3067\u3059\u3002"
                "\u30b3\u30fc\u30eb\u3059\u3079\u304d\uff1f\n"
            ).encode(),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
    ],
)
def test_source_contract_fails_closed_with_stable_codes(
    source: bytes,
    expected: ConfirmedReviewDiagnosticCode,
) -> None:
    result = _prepare_source(source)
    assert result.status == "blocked"
    assert result.candidate is None
    assert [item.code for item in result.diagnostics] == [expected]


@pytest.mark.parametrize(
    "source",
    [
        "player\u03b1=Alexander123\n",
        "\u0442\u0435\u0441\u0442Var=Example123\n",
    ],
)
def test_nonsecret_mixed_script_assignments_are_not_misclassified(source: str) -> None:
    result = _prepare_source(source.encode())
    assert result.status == "ready"
    assert result.candidate is not None
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "source",
    [
        (
            b"Currently, poker theory discusses blockers. "
            b"For the completed hand from yesterday, should I call or fold?\n"
        ),
        "現在のポーカー理論を使い、昨日終了したハンドでcallすべきだったか教えてください。\n".encode(),
        (
            b"For the hand I played yesterday in an online poker tournament, "
            b"should I have called or folded?\n"
        ),
        (b"I was in an online poker tournament yesterday. Should I have called or folded?\n"),
        (
            b"I am playing an online MTT replay from yesterday to study the completed hand. "
            b"Should I have called or folded?\n"
        ),
        (
            b"I am in an online poker tournament hand-history viewer, "
            b"reviewing yesterday's completed hand. Should I have called or folded?\n"
        ),
        (
            b"For retrospective review of yesterday's completed hand, archived quote: "
            b'"I am in an online poker tournament." The quote is historical. '
            b"Should I have called or folded?\n"
        ),
        (
            b"I am playing an online MTT video replay from yesterday to study the "
            b"completed hand. Should I have called or folded?\n"
        ),
        (
            b"I am in an online poker tournament archive reviewing yesterday's "
            b"completed hand. Should I have called or folded?\n"
        ),
        (
            b"For retrospective review, the archived quote reads "
            b'"I am in an online poker tournament." Should I have called or folded?\n'
        ),
        (
            b"For retrospective review, archived quote: "
            b"'I am in an online poker tournament.' Should I have called or folded?\n"
        ),
        (
            b"For retrospective review, the hand-history note says "
            b'"the action is on me." Should I have called or folded?\n'
        ),
        (
            b"I am playing an online MTT video replay from last week to study the "
            b"finished hand. Should I have called or folded?\n"
        ),
        (
            b"I am in an online poker tournament archive from two days ago; "
            b"the session ended. Should I have called or folded?\n"
        ),
        (
            b"I am playing an online MTT replay from yesterday, but I do not "
            b"understand the river. Should I call or fold in that spot?\n"
        ),
        (
            b"I am playing an online MTT replay from last week; however, the "
            b"river decision is unclear. Should I call or fold in that spot?\n"
        ),
        (
            b"I am playing an online MTT replay from yesterday, but only for study. "
            b"What should I do at the river node?\n"
        ),
        (
            b"I am playing an online MTT replay from last week, but the hand "
            b"completed last week. Should I have called or folded?\n"
        ),
        (
            b"I am playing an online MTT replay from yesterday; nevertheless, "
            b"the session ended. Should I have called or folded?\n"
        ),
        (
            b"I am playing an online MTT recording saved last Monday for review. "
            b"Should I have called or folded?\n"
        ),
        (
            b"A tournament is ongoing until only one player remains. "
            b"Should I call or fold in yesterday's completed hand?\n"
        ),
        (
            b"I am reviewing yesterday's completed hand. In the recording, the "
            b"tournament is still running at this point, but it ended yesterday. "
            b"Should I have called or folded?\n"
        ),
        (
            b"This replay is from yesterday and the hand was completed. "
            b'The subtitle states "The table is still running at this point. '
            b'Should I call?"\n'
        ),
        (
            b"This hand was completed yesterday. "
            b'The transcript records "The action is on me. Should I call?"\n'
        ),
        (
            "これは昨日終了したハンドの事後レビューです。"
            "字幕には「今は私の手番です。コールすべきですか\uff1f」とあります。\n"
        ).encode(),
        (
            b"Tournament is underway is a present-tense grammar example. "
            b"The word call names a poker action.\n"
        ),
        "「大会は進行中」は現在形の文法例です。「コール」はポーカー用語です。\n".encode(),
        (
            b"For retrospective review under current theory, the hand-history note says "
            b'"the action is on me." Should I have called or folded?\n'
        ),
        (b'The phrase "the event is not done yet" is terminology, and "call" is a poker term.\n'),
        (b'The sentence "The event is not done yet. Should I call?" is a terminology example.\n'),
        (
            b'The expression "the event is not done yet" is terminology, '
            b'and "call" is a poker term.\n'
        ),
        (
            b'The example sentence "The event is not done yet. Should I call?" '
            b"illustrates present tense.\n"
        ),
        (
            "\u6628\u65e5\u306e\u5b8c\u4e86\u30cf\u30f3\u30c9\u306e"
            "\u30ea\u30d7\u30ec\u30a4\u753b\u9762\u3067\u306f"
            "\u6b8b\u308a\u6642\u9593\u306f10\u79d2\u3067\u3059\u3002"
            "\u30b3\u30fc\u30eb\u3059\u3079\u304d\u3060\u3063\u305f\uff1f\n"
        ).encode(),
        (
            "\u6628\u65e5\u306e\u5b8c\u4e86\u30cf\u30f3\u30c9\u306e"
            "\u753b\u9762\u3067\u306f\u307e\u3060"
            "\u30d7\u30ec\u30a4\u4e2d\u3067\u3059\u3002"
            "\u30b3\u30fc\u30eb\u3059\u3079\u304d\u3060\u3063\u305f\uff1f\n"
        ).encode(),
    ],
)
def test_explicit_retrospective_source_is_not_misclassified_as_live(source: bytes) -> None:
    result = _prepare_source(source)
    assert result.status == "ready"
    assert result.candidate is not None
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "active_status",
    [
        "I remain in the contest",
        "The tournament has not wrapped up",
        "The match is underway",
        "We have not been eliminated yet",
        "My time bank is counting down",
        "This one is still running",
        "We're still at the table and the action has reached me",
        "Meanwhile, the field is still playing",
        "I survived and am still playing",
        "I remain alive in the tournament",
        "The competition is underway",
        "The tournament is unfinished",
        "The tourney is underway",
        "The event has yet to finish",
        "Tournament is underway",
        "Event is live",
        "Table is still running",
        "My clock is ticking",
        "I've not been eliminated yet",
        "It is my turn in this MTT",
        "My seat is still active",
        "I have ten seconds left to act",
        "Our tournament is underway",
        "We're still in the tourney",
        "The event is not done yet",
        "We remain in the MTT",
        "The action is back on me",
        "My turn has come",
        "Yesterday's hand ended, but the tournament is still running",
        "Yesterday's hand ended, but today's tournament is still running",
        "I reviewed a completed hand yesterday, while the MTT is ongoing",
        "The event is in full swing",
        "We're not out of the tournament yet",
        "I am still in",
        "We are still alive",
        "We still have chips",
        "I have not been knocked out yet",
        "We are in the middle of a hand",
        "We are in a hand at the moment",
        "We are halfway through a hand",
        "We are midway through a hand",
        "I am partway through a hand",
        "We are playing this hand at the moment",
        "The tournament is still on",
        "We are currently heads-up in the tournament",
        "We are heads-up now",
        "I am heads-up at the moment",
        "We are on the bubble",
        "We are down to heads-up",
        "I am tanking",
        "Cards are being dealt",
        "There are 10 seconds left to act",
        "The timer says 10 seconds",
        "The action clock shows 10 seconds",
        "The clock reads 10 seconds",
        "The timer displays 10 seconds",
        "Only 10 seconds remain to act",
        "Ten seconds remain to act",
        "I need to act within 10 seconds",
        "I have 10 seconds to act",
        "The action has come to me",
        "The action is with me",
        "My turn is now",
        "It is my action now",
        "It is my move",
        "\u30d7\u30ec\u30a4\u4e2d\u3067\u3059",
        "\u4eca\u30d7\u30ec\u30a4\u3057\u3066\u3044\u307e\u3059",
        "\u5bfe\u6226\u4e2d\u3067\u3059",
        "\u307e\u3060\u30cf\u30f3\u30c9\u4e2d\u3067\u3059",
        "\u30d7\u30ec\u30a4\u306e\u6700\u4e2d\u3067\u3059",
        "\u81ea\u5206\u304c\u30a2\u30af\u30b7\u30e7\u30f3\u3059\u308b\u756a\u3067\u3059",
        "\u3042\u3068\u5341\u79d2\u3067\u3059",
        "\u307e\u3060\u30b2\u30fc\u30e0\u5185\u3067\u3059",
        "\u5353\u306b\u3064\u3044\u3066\u3044\u307e\u3059",
        "10\u79d2\u3057\u304b\u306a\u3044",
        "まだプレイ中です",
        "まだ卓にいます",
        "ハンドの途中です",
        "自分の番が来ました",
        "残り時間は10秒です",
        "今ヘッズアップです",
        (
            "Yesterday's hand ended. The ongoing livestream transcript says "
            '"The action is back on me. Should I call?"'
        ),
        (
            "Yesterday's hand ended. The ongoing broadcast caption says "
            '"The action is back on me. Should I call?"'
        ),
        (
            "Yesterday's hand ended. The ongoing feed caption says "
            '"The action is back on me. Should I call?"'
        ),
        (
            'For retrospective review, the hand-history note says "the action is on me." '
            "The stream is live"
        ),
        "私はまだトーナメントにいます",
        "大会は終わっていません",
        "まだ大会から脱落していません",
        "今大会に出ています",
        "昨日のハンドは終了しましたが、今日のMTTは進行中です",
        "大会は真っ最中です",
        "トナメは進行中です",
        "まだ飛んでいません",
        "この大会はまだ開催中です",
        "大会は今も続いています",
        "MTTはまだ続行中",
        "私はまだこの大会に残っています",
        "私はまだ生き残っています",
        "大会はまだやっています",
        "私はまだ勝ち残っています",
        "今は私の手番です",
        "まだトーナメントに残っています",
        "アクションが回ってきました",
        "手番が回ってきました",
        "残り10秒です",
        "あと10秒で決めないといけません",
        "タイムバンクがカウントダウン中です",
    ],
)
def test_present_active_status_is_refused(active_status: str) -> None:
    result = _prepare_source(f"{active_status}. Should I call or fold?\n".encode())
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE


def test_long_active_clause_is_refused_without_a_distance_window() -> None:
    source = (
        "I am reviewing a replay from yesterday. This tournament, "
        + "according to the currently visible lobby details, " * 20
        + "is still running. Should I call or fold?\n"
    ).encode()
    result = _prepare_source(source)
    assert result.status == "blocked"
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE


def test_repeated_live_subject_scan_stays_linear_enough_for_bounded_input() -> None:
    source = ("tournament " * 6400).encode()
    started = perf_counter()
    result = _prepare_source(source)
    elapsed = perf_counter() - started
    assert result.status == "ready"
    assert elapsed < 2.0


def test_secret_assignment_scan_is_linear_enough_at_source_limit() -> None:
    source = ":" * MAX_CONFIRMED_REVIEW_SOURCE_BYTES
    started = perf_counter()
    contains_secret = _contains_secret_shape(source)
    elapsed = perf_counter() - started
    assert contains_secret is False
    assert elapsed < 2.0


@pytest.mark.parametrize(
    "source",
    [
        (
            "I am playing an online MTT replay called "
            + "X" * 240
            + " from yesterday to review a finished hand. "
            "Should I have called or folded?\n"
        ).encode(),
        (
            b"For retrospective review of yesterday's completed hand, "
            b'the video subtitle says "the action is on me." '
            b"Should I have called or folded?\n"
        ),
    ],
)
def test_long_replay_title_and_archived_subtitle_remain_retrospective(source: bytes) -> None:
    result = _prepare_source(source)
    assert result.status == "ready"
    assert result.candidate is not None


def test_source_size_limit_is_exact() -> None:
    accepted = _prepare_source(b"x" * MAX_CONFIRMED_REVIEW_SOURCE_BYTES)
    rejected = _prepare_source(b"x" * (MAX_CONFIRMED_REVIEW_SOURCE_BYTES + 1))
    assert accepted.status == "ready"
    assert rejected.diagnostics[0].code is ConfirmedReviewDiagnosticCode.SOURCE_SIZE


@pytest.mark.parametrize(
    ("source", "claim"),
    [
        (b"I am in an online MTT.\n", "Should I call or fold?"),
        (b"I am currently playing", "poker. Should I call or fold?"),
        (b"I am in an online", "MTT. Should I call or fold?"),
        (
            b"I am playing an online MTT replay from yesterday.",
            "This is actually a live tournament. Should I call or fold?",
        ),
        (
            b"My current online MTT table is still running and the action is on me.",
            "Should I call or fold?",
        ),
        (
            b"This tournament is still",
            "running. Should I call or fold?",
        ),
        (
            b"This tournament is still run",
            "ning. Should I call or fold?",
        ),
        (
            "この大会は現在進行".encode(),
            "中です。コールかフォールドか教えてください。",
        ),
        (
            b"I am reviewing a completed hand from yesterday.",
            "Action is on me before the decision timer expires. Should I call or fold?",
        ),
        (
            "オンラインMTTに出場しています。\n".encode(),
            "コールかフォールドか教えてください。",
        ),
    ],
)
def test_live_context_and_decision_cannot_be_split_across_artifacts(
    source: bytes,
    claim: str,
) -> None:
    payload = candidate_payload()
    payload["claims"][0]["text"] = claim
    result = prepare_review_intake(
        source,
        payload,
        source_id="source-split-live-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE


@pytest.mark.parametrize("field", ["description", "candidates", "selected"])
def test_resolved_ambiguity_free_text_is_in_the_live_security_envelope(field: str) -> None:
    payload = candidate_payload()
    ambiguity = {
        "ambiguity_id": "ambiguity-live-1",
        "field_path": "hand.actions.2.amount",
        "description": "Resolved amount.",
        "status": "resolved",
        "candidates": ["5"],
        "selected": "5",
    }
    live_text = "I am currently playing poker. Should I call or fold?"
    ambiguity[field] = [live_text] if field == "candidates" else live_text
    payload["ambiguities"] = [ambiguity]
    result = _prepare_source_with_payload(SOURCE_BYTES, payload)
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE


def test_secret_shape_cannot_be_split_into_resolved_ambiguity_text() -> None:
    payload = candidate_payload()
    payload["ambiguities"] = [
        {
            "ambiguity_id": "ambiguity-secret-1",
            "field_path": "hand.actions.2.amount",
            "description": "_key=ABCDEFGHIJKLMNOP123456",
            "status": "resolved",
            "candidates": ["5"],
            "selected": "5",
        }
    ]
    result = _prepare_source_with_payload(b"api", payload)
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY


@pytest.mark.parametrize("field", ["candidates", "selected"])
def test_secret_source_tail_is_checked_against_each_later_ambiguity_field(field: str) -> None:
    payload = candidate_payload()
    ambiguity = {
        "ambiguity_id": "ambiguity-secret-nonadjacent-1",
        "field_path": "hand.actions.2.amount",
        "description": "Resolved amount.",
        "status": "resolved",
        "candidates": ["5"],
        "selected": "5",
    }
    ambiguity[field] = (
        ["_key=ABCDEFGHIJKLMNOP123456"] if field == "candidates" else "_key=ABCDEFGHIJKLMNOP123456"
    )
    payload["ambiguities"] = [ambiguity]

    result = _prepare_source_with_payload(b"api", payload)

    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY


def test_secret_source_tail_is_checked_against_unrestricted_hand_identifiers() -> None:
    payload = candidate_payload()
    payload["hand"]["players"][0]["player_id"] = "_key=ABCDEFGHIJKLMNOP123456"
    payload["hand"]["hero_player_id"] = "_key=ABCDEFGHIJKLMNOP123456"
    for action in payload["hand"]["actions"]:
        if action["actor"] == "hero":
            action["actor"] = "_key=ABCDEFGHIJKLMNOP123456"

    result = _prepare_source_with_payload(b"api", payload)

    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY


@pytest.mark.parametrize("field", ["candidates", "selected"])
def test_live_source_tail_is_checked_against_each_later_ambiguity_field(field: str) -> None:
    payload = candidate_payload()
    ambiguity = {
        "ambiguity_id": "ambiguity-live-nonadjacent-1",
        "field_path": "hand.actions.2.amount",
        "description": "Resolved amount.",
        "status": "resolved",
        "candidates": ["5"],
        "selected": "5",
    }
    ambiguity[field] = (
        ["ing poker. Should I call or fold?"]
        if field == "candidates"
        else "ing poker. Should I call or fold?"
    )
    payload["ambiguities"] = [ambiguity]

    result = _prepare_source_with_payload(b"I am currently play", payload)

    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE


def test_hand_observation_free_text_is_in_the_live_security_envelope() -> None:
    payload = candidate_payload()
    payload["hand"]["opponent_observations"] = ["I am mid-hand at the moment. Should I call?"]
    result = _prepare_source_with_payload(SOURCE_BYTES, payload)
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE


@pytest.mark.parametrize(
    ("source", "claim"),
    [
        (b"api", "_key=ABCDEFGHIJKLMNOP123456"),
        (b"api ", "key=ABCDEFGHIJKLMNOP123456"),
        (b"api:\n", "_key=ABCDEFGHIJKLMNOP123456"),
        (b"api=\n", "_key=ABCDEFGHIJKLMNOP123456"),
    ],
)
def test_secret_shape_cannot_be_split_across_artifacts(
    source: bytes,
    claim: str,
) -> None:
    payload = candidate_payload()
    payload["claims"][0]["text"] = claim
    result = prepare_review_intake(
        source,
        payload,
        source_id="source-split-secret-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY


@pytest.mark.parametrize("prefix", ["api", "api:\n", "api=\n"])
def test_secret_shape_cannot_be_split_across_adjacent_claims(prefix: str) -> None:
    payload = candidate_payload()
    payload["claims"] = [
        {
            "claim_id": "claim-secret-prefix",
            "text": prefix,
            "label": "USER_CLAIM",
            "confidence": "C",
        },
        {
            "claim_id": "claim-secret-suffix",
            "text": "_key=ABCDEFGHIJKLMNOP123456",
            "label": "USER_CLAIM",
            "confidence": "C",
        },
    ]
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-split-claim-secret-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY


@pytest.mark.parametrize(
    "source",
    [
        b'The live video subtitle says "the action is on me." Should I call or fold?\n',
        (b'The video caption says "the action is on me." It is live. Should I call or fold?\n'),
        (
            b"Yesterday's hand ended. Today's livestream transcript says "
            b'"The action is back on me. Should I call?"\n'
        ),
        (
            b"For retrospective review of yesterday's completed hand, "
            b'the live table caption says "The action is on me. Should I call?"\n'
        ),
        (
            b"For retrospective review of yesterday's completed hand, "
            b'the currently broadcasting caption says "The action is on me. Should I call?"\n'
        ),
        (
            b"Yesterday's hand ended. The real-time feed transcript says "
            b'"The action is on me. Should I call?"\n'
        ),
        (
            b"Yesterday's hand ended. The active livestream caption says "
            b'"The action is on me. Should I call?"\n'
        ),
        (
            b"Yesterday's hand ended. The in-progress feed caption says "
            b'"The action is on me. Should I call?"\n'
        ),
        (
            b"Yesterday's hand ended. The streaming feed caption says "
            b'"The action is on me. Should I call?"\n'
        ),
        (
            b"Yesterday's hand ended. The underway livestream transcript says "
            b'"The action is on me. Should I call?"\n'
        ),
    ],
)
def test_live_quotation_is_not_treated_as_archived(source: bytes) -> None:
    result = _prepare_source(source)
    assert result.status == "blocked"
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE


def test_source_rights_matrix_is_closed() -> None:
    result = prepare_review_intake(
        SOURCE_BYTES,
        candidate_payload(),
        source_id="source-rights-1",
        source_kind="user_supplied",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    assert result.status == "blocked"
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.SOURCE_RIGHTS


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value["hand"].update({"hero_cards": []}),
            ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING,
        ),
        (
            lambda value: value["hand"].update({"actions": []}),
            ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING,
        ),
        (
            lambda value: value["ambiguities"].append(
                {
                    "ambiguity_id": "ambiguity-1",
                    "field_path": "hand.actions.2.amount",
                    "description": "raise amount is unclear",
                    "status": "unresolved",
                }
            ),
            ConfirmedReviewDiagnosticCode.CANDIDATE_AMBIGUITY,
        ),
        (
            lambda value: value["hand"].update({"game_type": "PLO"}),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
    ],
)
def test_candidate_completeness_and_scope_are_fail_closed(mutation, expected) -> None:
    payload = deepcopy(candidate_payload())
    mutation(payload)
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-candidate-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.diagnostics[0].code is expected


def test_candidate_and_confirmation_hashes_are_self_replayable() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    assert prepared.source is not None
    assert prepared.candidate.candidate_sha256 == candidate_sha256(prepared.candidate.projection)
    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-unit-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    now = datetime(2026, 7, 29, 1, tzinfo=UTC)
    confirmation = create_review_confirmation(
        prepared.candidate,
        run_id="run-unit-confirmation-1",
        confirmation_id="confirmation-unit-1",
        idempotency_key="idempotency-unit-1",
        authority=authority,
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=prepared.candidate.candidate_sha256,
        confirmed_at=now,
    )
    assert confirmation.confirmation_sha256 == confirmation_sha256(confirmation)


def test_model_copy_cannot_bypass_candidate_confirmation_or_authority_contracts() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    assert prepared.source is not None
    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-unit-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    now = datetime.now(UTC)
    confirmation = create_review_confirmation(
        prepared.candidate,
        run_id="run-unit-model-copy-1",
        confirmation_id="confirmation-unit-model-copy-1",
        idempotency_key="idempotency-unit-model-copy-1",
        authority=authority,
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=prepared.candidate.candidate_sha256,
        confirmed_at=now,
    )

    unconfirmed = confirmation.model_copy(update={"confirmed": False})
    unconfirmed = unconfirmed.model_copy(
        update={"confirmation_sha256": confirmation_sha256(unconfirmed)}
    )
    with pytest.raises(ConfirmedReviewError) as invalid_confirmation:
        admit_confirmed_review(SOURCE_BYTES, prepared.candidate, unconfirmed)
    assert invalid_confirmation.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING

    invalid_authority = authority.model_copy(update={"authority_kind": "verified_application"})
    forged_authority = confirmation.model_copy(
        update={
            "authority": invalid_authority,
            "authority_snapshot_sha256": authority_snapshot_sha256(invalid_authority),
        }
    )
    forged_authority = forged_authority.model_copy(
        update={"confirmation_sha256": confirmation_sha256(forged_authority)}
    )
    with pytest.raises(ConfirmedReviewError) as invalid_authority_error:
        admit_confirmed_review(SOURCE_BYTES, prepared.candidate, forged_authority)
    assert (
        invalid_authority_error.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_AUTHORITY
    )

    candidate_input = prepared.candidate.projection.candidate_input
    oversized_claim = candidate_input.claims[0].model_copy(
        update={"text": "x" * (MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES + 1)}
    )
    oversized_input = candidate_input.model_copy(update={"claims": (oversized_claim,)})
    oversized_projection = prepared.candidate.projection.model_copy(
        update={"candidate_input": oversized_input}
    )
    oversized_candidate = prepared.candidate.model_copy(
        update={
            "projection": oversized_projection,
            "candidate_sha256": candidate_sha256(oversized_projection),
        }
    )
    oversized_confirmation = confirmation.model_copy(
        update={"candidate_sha256": oversized_candidate.candidate_sha256}
    )
    oversized_confirmation = oversized_confirmation.model_copy(
        update={"confirmation_sha256": confirmation_sha256(oversized_confirmation)}
    )
    with pytest.raises(ConfirmedReviewError) as invalid_candidate:
        admit_confirmed_review(
            SOURCE_BYTES,
            oversized_candidate,
            oversized_confirmation,
        )
    assert invalid_candidate.value.code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA


def test_preparation_rejects_a_mismatched_duplicate_source_projection() -> None:
    prepared = ready_preparation()
    assert prepared.source is not None
    payload = prepared.model_dump(mode="json")
    payload["source"]["content_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        ReviewIntakePreparationResultV1.model_validate(payload, strict=True)


def test_confirmation_requires_out_of_band_exact_hashes() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-unit-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    with pytest.raises(ConfirmedReviewError) as captured:
        create_review_confirmation(
            prepared.candidate,
            run_id="run-unit-confirmation-2",
            confirmation_id="confirmation-unit-2",
            idempotency_key="idempotency-unit-2",
            authority=authority,
            expected_source_sha256="0" * 64,
            expected_candidate_sha256=prepared.candidate.candidate_sha256,
        )
    assert captured.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING


def test_source_mutation_and_expiry_are_rejected_before_run_admission() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    assert prepared.source is not None
    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-unit-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    now = datetime.now(UTC)
    confirmation = create_review_confirmation(
        prepared.candidate,
        run_id="run-unit-admission-1",
        confirmation_id="confirmation-unit-3",
        idempotency_key="idempotency-unit-3",
        authority=authority,
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=prepared.candidate.candidate_sha256,
        confirmed_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    with pytest.raises(ConfirmedReviewError) as mutated:
        admit_confirmed_review(
            SOURCE_BYTES + b"mutation\n",
            prepared.candidate,
            confirmation,
        )
    assert mutated.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING
    expired_confirmation = create_review_confirmation(
        prepared.candidate,
        run_id="run-unit-admission-expired",
        confirmation_id="confirmation-unit-expired",
        idempotency_key="idempotency-unit-expired",
        authority=authority,
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=prepared.candidate.candidate_sha256,
        confirmed_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
    )
    with pytest.raises(ConfirmedReviewError) as expired:
        admit_confirmed_review(
            SOURCE_BYTES,
            prepared.candidate,
            expired_confirmation,
        )
    assert expired.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_EXPIRED


def test_legacy_range_shape_cannot_enter_candidate_contract() -> None:
    payload = candidate_payload()
    payload["hand"]["known_ranges"] = [
        {
            "player_id": "villain",
            "notation": "AKs",
            "assumed": True,
        }
    ]
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-legacy-range-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA


@pytest.mark.parametrize(
    "secret",
    [
        "api_key=sk-abcdefgh",
        "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        "Cookie: sessionid=ABCDEFGHIJKLMNOP123456",
        "api\ufe0f_key: ABCDEFGHIJKLMNOP123456",
        "OPENAI_API_KEY=ABCDEFGHIJKLMNOP123456",
        "AWS_SECRET_ACCESS_KEY=ABCDEFGHIJKLMNOP123456",
        "GITHUB_TOKEN=ABCDEFGHIJKLMNOP123456",
        "api\u034f_key: ABCDEFGHIJKLMNOP123456",
        "api\u180b_key=sk_test_never_store",
        "api\u0600_key=ABCDEFGHIJKLMNOP123456",
        "api\ufff0_key=ABCDEFGHIJKLMNOP123456",
        "api\u0332_key: ABCDEFGHIJKLMNOP123456",
        "api\x00_key=sk_test_never_store",
        "api\x01_key=sk_test_never_store",
        "api\x7f_key=sk_test_never_store",
        "api\u0903_key=sk_test_never_store",
        "api\ue000_key=sk_test_never_store",
        "api\ufffc_key=sk_test_never_store",
        "api/_key=sk_test_never_store",
    ],
)
def test_candidate_secret_is_not_written_to_preparation_artifact(secret: str) -> None:
    payload = candidate_payload()
    payload["claims"][0]["text"] = secret
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-candidate-secret-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY


def test_candidate_artifact_size_limit_fails_closed() -> None:
    payload = candidate_payload()
    payload["hand"]["opponent_observations"] = ["x" * MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES]
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-candidate-size-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA
    assert result.diagnostics[0].field_path == "candidate.size_bytes"
