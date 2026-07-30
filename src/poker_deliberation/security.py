"""Best-effort redaction for artifacts and user-visible reports."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel

from poker_deliberation.schemas import CaseInput, SecurityEvent

_SENSITIVE_KEY = re.compile(
    r"(?:api[.\s_-]*key|authorization|bearer|cookie|password|passwd|secret|"
    r"session[.\s_-]*token|access[.\s_-]*token|client[.\s_-]*secret)",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:authorization|cookie)\s*[:=]\s*[^\r\n]+", re.IGNORECASE),
    re.compile(
        r"(?<![A-Za-z0-9])(?:[A-Za-z][A-Za-z0-9]*_)*"
        r"(?:API_KEY|SECRET(?:_ACCESS_KEY)?|ACCESS_TOKEN|TOKEN|PASSWORD)"
        r"\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:api[.\s_-]*key|password|secret|token|access[.\s_-]*token|"
        r"client[.\s_-]*secret)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
)
_SECURITY_IGNORABLES = re.compile(
    "[\u00ad\u0300-\u036f\u034f\u061c\u115f-\u1160\u17b4-\u17b5"
    "\u180b-\u180f\u1ab0-\u1aff\u1dc0-\u1dff\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u20d0-\u20ff\u3164"
    "\ufe00-\ufe0f\ufe20-\ufe2f\ufeff\uffa0"
    "\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    "\U000e0000-\U000e0fff]"
)
_SECURITY_IGNORABLE_CATEGORIES = frozenset({"Cf", "Mn", "Me"})
_SECURITY_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)
_SECRET_CONFUSABLE_TRANSLATION = str.maketrans(
    {
        "\u0430": "a",  # Cyrillic a
        "\u0435": "e",  # Cyrillic ie
        "\u0456": "i",  # Cyrillic i
        "\u043a": "k",  # Cyrillic ka
        "\u043e": "o",  # Cyrillic o
        "\u0440": "p",  # Cyrillic er
        "\u0441": "c",  # Cyrillic es
        "\u0442": "t",  # Cyrillic te
        "\u0445": "x",  # Cyrillic ha
        "\u0443": "y",  # Cyrillic u
        "\u0455": "s",  # Cyrillic dze
        "\u03b1": "a",  # Greek alpha
        "\u03b5": "e",  # Greek epsilon
        "\u03b9": "i",  # Greek iota
        "\u03ba": "k",  # Greek kappa
        "\u03bf": "o",  # Greek omicron
        "\u03c1": "p",  # Greek rho
        "\u03c4": "t",  # Greek tau
        "\u03c7": "x",  # Greek chi
        "\u03c5": "y",  # Greek upsilon
    }
)
_MIXED_SCRIPT_SECRET_ASSIGNMENT = re.compile(
    r"(?<![\w.-])"
    r"(?=[^\s:=]{2,64}\s*[:=])"
    r"(?=[^\s:=]*[A-Za-z])"
    r"(?=[^\s:=]*[\u0370-\u052f])"
    r"[^\s:=]{2,64}\s*[:=]\s*[^\s,;]{8,}",
)

_REAL_TIME_ASSISTANCE = re.compile(
    r"(?:(?:ただいま|いま|今|現在)(?:は)?.{0,24}"
    r"(?:オンライン(?:ポーカー|卓)?|ポーカー|ハンド|ゲーム|卓).{0,20}"
    r"(?:中|プレイ中|参加中|参加して(?:い)?(?:る|ます)|"
    r"打って(?:い)?(?:る|ます)|着席して(?:い)?(?:る|ます))|"
    r"今(?:まさに)?プレイ中|リアルタイム(?:で|の).{0,20}(?:指示|助言|教えて)|"
    r"(?:i(?:['\u2019]m| am)\s+)?playing\s+(?:poker\s+)?(?:right\s+)?now\b|"
    r"(?:i(?:['\u2019]m| am)\s+)?at\s+(?:a|the)\s+poker\s+table\s+(?:right\s+)?now\b|"
    r"(?:i(?:['\u2019]m| am)\s+)?in\s+(?:an?\s+)?(?:online\s+)?"
    r"(?:poker\s+|cash\s+)?game\s+"
    r"(?:right\s+)?now\b|"
    r"(?:i(?:['\u2019]m| am)\s+)?seated\s+at\s+(?:a|the)\s+"
    r"(?:poker|tournament)\s+table\s+(?:right\s+)?now\b|"
    r"今(?:キャッシュゲーム|トーナメント)(?:を)?(?:打って|プレイして)います|"
    r"(?:currently\s+playing|in\s+(?:a|the)\s+live\s+hand).{0,40}"
    r"(?:call|fold|check|bet|raise|shove|all[- ]?in|what\s+should\s+i\s+do)|"
    r"(?:give|provide|need|want|use).{0,20}real[- ]?time.{0,20}(?:advice|assistance)|"
    r"real[- ]?time.{0,20}(?:advice|assistance).{0,20}while\s+i\s+play)",
    re.IGNORECASE,
)
_LIVE_PLAY_CONTEXT = re.compile(
    r"(?:(?:オンライン\s*(?:ポーカー|卓|MTT|SNG|PKO|キャッシュ(?:ゲーム)?|"
    r"スピン\s*(?:&|アンド)\s*ゴー)|"
    r"(?:ポーカー|トーナメント|MTT|SNG|PKO|キャッシュ(?:ゲーム)?|"
    r"シット[- ]?アンド[- ]?ゴー|スピン\s*(?:&|アンド)\s*ゴー)(?:卓)?)"
    r"(?:に|で|を)?(?:参加して(?:おり|い)ます|出場して(?:おり|い)ます|"
    r"プレイして(?:おり|い)ます|打って(?:おり|い)ます|"
    r"着席して(?:おり|い)ます|参加中|出場中|プレイ中)|"
    r"\bi(?:['\u2019]m| am)\s+(?:currently\s+)?(?:"
    r"playing\s+(?:in\s+)?(?:an?\s+)?(?:online\s+)?"
    r"(?:(?:poker|cash)\s+(?:game|tournament|hand)|"
    r"poker|game|tournament|mtt|sng|pko|sit[- ]and[- ]go|"
    r"spin\s*(?:&|and)\s*go)|"
    r"in\s+(?:an?\s+)?(?:online\s+)?"
    r"(?:(?:poker|cash)\s+(?:game|tournament|hand)|"
    r"game|tournament|mtt|sng|pko|sit[- ]and[- ]go|"
    r"spin\s*(?:&|and)\s*go)|"
    r"at\s+(?:an?|the)?\s*(?:online\s+)?(?:poker|tournament|cash)\s+table|"
    r"seated\s+at\s+(?:an?|the)?\s*(?:online\s+)?"
    r"(?:poker|tournament|cash)\s+table|"
    r"multi[- ]tabling(?:\s+online)?|"
    r"on\s+(?:pokerstars|ggpoker|acr|wsop|888poker|partypoker|"
    r"ignition|bovada)(?:\s+(?:right\s+)?now)?|"
    r"playing\s+(?:zoom|rush|blitz|snap|fast[- ]?forward))|"
    r"オンラインで(?:\d+|[一二三四五六七八九十]+)面打ちして(?:おり|い)ます)",
    re.IGNORECASE,
)
_PARTIAL_LIVE_CONTEXT = re.compile(
    r"\bi(?:['\u2019]m| am)\s+(?:currently\s+)?(?:"
    r"playing\b|in\s+(?:an?\s+)?(?:online\b)?|"
    r"at\s+(?:an?|the)?\s*(?:online\b)?|on\b|seated\b)|"
    r"\bi(?:['\u2019]m| am)\s*$",
    re.IGNORECASE,
)
_DECISION_REQUEST = re.compile(
    r"(?:\b(?:call|fold|check|bet|raise|shove|all[- ]?in)\b|"
    r"\bshould\s+i\b(?!\s+have\b)|what\s+should\s+i\s+do|"
    r"コール|フォールド|チェック|ベット|レイズ|オールイン|"
    r"アクション|どちら|どうすべき|教えて|すべき)",
    re.IGNORECASE,
)
_ARCHIVED_QUOTATION = re.compile(
    r"(?:(?:^|[.!?\r\n\u3002\uff01\uff1f])"
    r"(?![^.!?\r\n\u3002\uff01\uff1f]{0,512}"
    r"(?:\b(?:current|currently|live|livestream|broadcasting|right\s+now|"
    r"today(?:'s)?)\b|(?:現在|ライブ)))"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,512}?"
    r"(?:\b(?:retrospective|archived|historical|recorded|replay|recording|"
    r"hand[- ]?history|"
    r"yesterday(?:'s)?|completed|finished|last\s+(?:week|month)|"
    r"\d+\s+days?\s+ago)\b|(?:事後|過去|履歴|昨日|完了|終了))"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,512}?"
    r"(?:(?:archived|historical|recorded)\b[^:\r\n]{0,96}"
    r"(?::|\breads?\s+)|"
    r"(?:(?:the\s+)?(?:video|replay|recording|hand[- ]?history)\s+)?"
    r"(?:subtitle|caption|quote|note)\s+(?:says?|reads?)\s+|"
    r"(?:the\s+)?(?:video|replay|recording)\s+(?:says?|reads?)\s+|"
    r"(?:アーカイブ|過去|履歴)(?:の)?(?:引用|記録|メモ)[^\uFF1A\r\n]{0,48}"
    r"[\uFF1A:])\s*"
    r"(?:[\"“][^\"”\r\n]*[\"”]|'[^'\r\n]*'|[「『][^」』\r\n]*[」』]))",
    re.IGNORECASE,
)
_QUOTED_SEGMENT = re.compile(r"(?:[\"“][^\"”\r\n]*[\"”]|'[^'\r\n]*'|[「『][^」』\r\n]*[」』])")
_ARCHIVED_ATTRIBUTION = re.compile(
    r"(?:(?:archived|historical|recorded)\b[^:\r\n]{0,96}(?::|\breads?\s+)|"
    r"(?:(?:the\s+)?(?:video|replay|recording|hand[- ]?history)\s+)?"
    r"(?:subtitle|caption|transcript|quote|note)\s+"
    r"(?:says?|reads?|states?|records?)\s+|"
    r"(?:the\s+)?(?:video|replay|recording)\s+(?:says?|reads?|states?|records?)\s+|"
    r"(?:字幕|文字起こし|記録|引用|メモ)(?:には|には、|は|に)?"
    r"[^「『\"“'\r\n]{0,48})$",
    re.IGNORECASE,
)
_ARCHIVED_CONTEXT = re.compile(
    r"\b(?:retrospective|archived|historical|recorded|replay|recording|"
    r"hand[- ]?history|yesterday(?:'s)?|completed|finished|ended|"
    r"last\s+(?:week|month)|\d+\s+days?\s+ago)\b|"
    r"(?:事後|過去|履歴|昨日|完了|終了|リプレイ|録画)",
    re.IGNORECASE,
)
_LIVE_ATTRIBUTION = re.compile(
    r"\b(?:current(?:ly)?|live|ongoing|right\s+now|today(?:'s)?|"
    r"currently\s+broadcasting)\b"
    r"[^.!?\r\n\"'“”]{0,64}"
    r"\b(?:video|feed|stream|livestream|broadcast|broadcasting|subtitle|caption|"
    r"transcript|recording)\b|"
    r"(?:現在|ライブ)(?:の)?(?:動画|配信|放送|字幕|文字起こし|録画)",
    re.IGNORECASE,
)
_INERT_LANGUAGE_EXAMPLE = re.compile(
    r"(?:(?:^|[.!?\r\n\u3002\uff01\uff1f])\s*(?:"
    r"(?!(?:i|we|my|our|this|current|today(?:'s)?)\b)"
    r"[^.!?\r\n\u3002\uff01\uff1f]{1,180}\b(?:present[- ]tense\s+)?"
    r"(?:grammar|language)\s+example\b|"
    r"(?:the\s+)?word\s+(?:call|fold|check|bet|raise|shove|all[- ]?in)\s+"
    r"(?:names?|means?|denotes?|refers?\s+to)\s+(?:an?\s+)?poker\s+action\b|"
    r"(?:the\s+)?(?:phrase|sentence|expression)\s+"
    r"[\"'][^\"'\r\n]{1,256}[\"']\s+is\s+"
    r"(?:a\s+)?(?:term|terminology|language\s+example)\b|"
    r"[「『][^」』\r\n]{1,128}[」』](?:は|が)"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,48}(?:文法例|用語)(?:です|である)?"
    r")[^.!?\r\n\u3002\uff01\uff1f]{0,96}(?=$|[.!?\r\n\u3002\uff01\uff1f]))",
    re.IGNORECASE,
)
_HISTORICAL_STATUS_CLAUSE = re.compile(
    r"(?:(?:^|[.!?\r\n\u3002\uff01\uff1f])"
    r"(?![^.!?\r\n\u3002\uff01\uff1f]{0,512}"
    r"(?:\b(?:current(?!\s+(?:theory|grammar|language|terminology))|"
    r"currently|live|livestream|broadcasting|right\s+now|today(?:'s)?)\b|"
    r"(?:現在(?!形)|ライブ)))"
    r"(?![^.!?\r\n\u3002\uff01\uff1f]{0,512}"
    r"(?:(?:(?:\b(?:ended|completed|finished)\b|(?:終了|完了)(?:した|しました)?)"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,256}|"
    r"(?:\b(?:but|while|whereas|meanwhile)\b)"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,256}"
    r")"
    r"\b(?:mtt|sng|event|game|match|tourney|tournament|table|stream)\b"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,96}"
    r"\b(?:in\s+full\s+swing|in\s+progress|underway|ongoing|live|"
    r"still\s+running|not\s+(?:done|over|finished|ended))\b|"
    r"(?:(?:終了|完了)(?:しました|した)|ですが|が[、,]?|一方|今日)"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,256}"
    r"(?:MTT|SNG|大会|トーナメント|トナメ|イベント)"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,48}"
    r"(?:進行中|継続中|真っ最中|終わっていません|終了していません)))"
    r"(?=[^.!?\r\n\u3002\uff01\uff1f]{0,512}"
    r"(?:\b(?:replay|recording|archive|historical|retrospective|yesterday)\b|"
    r"(?:リプレイ|録画|記録|過去|昨日|事後)))"
    r"(?=[^.!?\r\n\u3002\uff01\uff1f]{0,512}"
    r"(?:(?<!not\s)(?<!n['\u2019]t\s)\b(?:ended|completed|finished)\b|"
    r"(?:終了|完了)(?:した|済み|しています|している|しました)))"
    r"[^.!?\r\n\u3002\uff01\uff1f]{0,512})",
    re.IGNORECASE,
)
_RETROSPECTIVE_LIVE_SUFFIX = re.compile(
    r"\s+(?:(?:(?:video\s+)?(?:replay|recording|review|simulation|trainer|viewer)"
    r"|archive(?:\s+review)?|hand[- ]?history(?:\s+viewer)?)\b"
    r"(?=[^.!?\r\n]*\b(?:yesterday(?:'s)?|completed|finished|historical|"
    r"archived|recorded|last\s+(?:week|month|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|(?:one|two|three|four|five|six|seven|\d+)\s+"
    r"days?\s+ago|session\s+(?:has\s+)?ended|saved\s+last)\b)|"
    r"from\s+(?:yesterday|last\s+(?:week|month|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|(?:one|two|three|four|five|six|seven|\d+)\s+"
    r"days?\s+ago)\b|of\s+(?:an?|the)\s+(?:completed|finished)\b)",
    re.IGNORECASE,
)
_RETROSPECTIVE_REVERSAL = re.compile(
    r"\b(?:but|however|yet|although|nevertheless)\b|"
    r"(?:ですが|しかし|ただし|けれども|にもかかわらず)",
    re.IGNORECASE,
)
_LIVE_CONTRADICTION = re.compile(
    r"\b(?:actual(?:ly)?\s+live|live\s+(?:game|tournament|table|hand)|right\s+now|"
    r"i(?:['\u2019]m| am)\s+(?:still\s+)?playing(?:\s+(?:it|this\s+"
    r"(?:game|tournament|hand)))?\s+now|"
    r"i(?:['\u2019]m| am)\s+still\s+playing\s+(?:this\s+)?"
    r"(?:game|tournament|hand)|"
    r"this\s+is\s+my\s+current\s+(?:game|tournament|hand)|"
    r"(?:this|the)\s+(?:game|tournament|hand)\s+is\s+"
    r"(?:(?:currently\s+)?(?:in\s+progress|underway)|happening\s+now))\b",
    re.IGNORECASE,
)
_ACTIVE_LIVE_STATUS = re.compile(
    r"(?:\b(?:(?:this|the|our|today(?:'s)?)\s+"
    r"(?:mtt|sng|event|game|tourney|tournament|hand|session|play|stream)"
    r"\s+(?:(?:is|remains)\s+(?:currently\s+)?(?:in\s+progress|underway|ongoing|"
    r"in\s+full\s+swing|live|active|running)|"
    r"(?:is\s+not|isn't|isn\u2019t)\s+done(?:\s+yet)?|"
    r"has(?:n't|n\u2019t| not)\s+(?:ended|finished|completed|"
    r"concluded)(?:\s+yet)?)|"
    r"(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+still\s+"
    r"(?:(?:playing|competing|participating)(?:\s+(?:in\s+)?(?:it|this\s+"
    r"(?:mtt|sng|event|game|tournament|hand)))?|in\s+(?:it|this\s+"
    r"(?:mtt|sng|event|game|tournament|hand))|on\s+the\s+bubble)|"
    r"cards\s+are\s+(?:still\s+)?being\s+dealt|play\s+is\s+(?:still\s+)?ongoing)\b|"
    r"(?:この|その)?(?:大会|トーナメント|トナメ|MTT|SNG|イベント|ハンド|ゲーム|プレイ)"
    r".{0,16}(?:現在進行中|進行中|継続中|真っ最中|"
    r"まだ終わっていません|終わっていません|終了していません|"
    r"まだ参加中|まだ出場中)|"
    r"(?:私|自分)?(?:は)?(?:まだ|現在).{0,12}(?:参加|出場|プレイ)して(?:い|おり)ます)",
    re.IGNORECASE,
)
_EXPLICIT_ACTIVE_LIVE_STATUS = re.compile(
    r"(?:\b(?:"
    r"(?:(?:(?:this|the|my|our|that|current|today(?:'s)?)\s+(?:online\s+)?"
    r"(?:mtt|sng|pko|contest|event|game|tourney|tournament|table|hand|"
    r"session|play|stream))|"
    r"(?:mtt|sng|pko))\s+"
    r"(?:"
    r"(?:(?:is|remains)\s+(?:still\s+|currently\s+)?"
    r"(?:in\s+progress|in\s+full\s+swing|underway|ongoing|active|live|"
    r"running|not\s+over))|"
    r"(?:is(?:n't|n\u2019t|\s+not)\s+"
    r"(?:done|over|ended|finished|completed|concluded)(?:\s+yet)?)|"
    r"(?:has(?:n't|n\u2019t|\s+not)\s+(?:ended|finished|completed|concluded)"
    r"(?:\s+yet)?)|"
    r"(?:continues?|keeps\s+going)(?:\s+(?:today|now))?"
    r")|"
    r"(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+still\s+"
    r"(?:(?:playing|competing|participating)(?:\s+(?:in\s+)?(?:it|this\s+"
    r"(?:mtt|sng|contest|event|game|tournament|hand)))?|in\s+(?:it|this\s+"
    r"(?:mtt|sng|contest|event|game|tournament|hand))|seated|on\s+the\s+bubble)|"
    r"i\s+still\s+have\s+chips(?:\s+in\s+(?:it|the\s+"
    r"(?:mtt|sng|contest|event|game|tournament)))?|"
    r"i\s+have(?:n't|n\u2019t|\s+not)\s+busted(?:\s+yet)?|"
    r"(?:the\s+)?action\s+is\s+(?:(?:still\s+)?on|back\s+on)\s+me|"
    r"my\s+turn\s+has\s+come|"
    r"(?:my\s+)?(?:decision|action)\s+(?:clock|timer)\s+is\s+(?:still\s+)?running|"
    r"(?:before|until)\s+(?:my\s+|the\s+)?"
    r"(?:decision|action)?\s*(?:clock|timer|time\s*bank)\s+expires|"
    r"cards\s+are\s+(?:still\s+)?being\s+dealt|play\s+is\s+(?:still\s+)?ongoing"
    r")\b|"
    r"(?:\u3053\u306e|\u305d\u306e)?"
    r"(?:\u5927\u4f1a|\u30c8\u30fc\u30ca\u30e1\u30f3\u30c8|\u30c8\u30ca\u30e1|"
    r"\u30a4\u30d9\u30f3\u30c8|\u30b2\u30fc\u30e0|\u30cf\u30f3\u30c9|"
    r"\u30bb\u30c3\u30b7\u30e7\u30f3)"
    r"(?:\u306f)?\s*(?:\u307e\u3060|\u73fe\u5728)"
    r".{0,12}(?:\u7d9a\u3044\u3066|\u9032\u884c\u4e2d|\u771f\u3063\u6700\u4e2d|"
    r"\u7d42\u308f\u3063\u3066\u3044\u306a\u3044|"
    r"\u7d42\u4e86\u3057\u3066\u3044\u306a\u3044)|"
    r"\u30a2\u30af\u30b7\u30e7\u30f3(?:\u306f|\u304c)?"
    r"(?:\u307e\u3060|\u73fe\u5728)?.{0,8}"
    r"(?:\u79c1|\u81ea\u5206)(?:\u306b|\u306e\u756a)|"
    r"(?:\u30bf\u30a4\u30e0\u30d0\u30f3\u30af|\u6301\u3061\u6642\u9593|"
    r"\u6b8b\u308a\u6642\u9593).{0,12}(?:\u5207\u308c\u308b|\u5c3d\u304d\u308b)"
    r")",
    re.IGNORECASE,
)

_ACTIVE_GAME_SUBJECT = re.compile(
    r"\b(?:(?:my|our|this|the|that|current|today(?:'s)?)\s+"
    r"(?:mtt|sng|pko|competition|contest|event|game|match|tourney|tournament|table|hand|"
    r"session|play|one|field|stream)|(?:mtt|sng|pko))\b",
    re.IGNORECASE,
)
_BARE_ACTIVE_GAME_SUBJECT = re.compile(
    r"^\s*(?:competition|contest|event|game|match|tourney|tournament|table|hand|"
    r"session|play|stream)\b",
    re.IGNORECASE,
)
_ACTIVE_GAME_PREDICATE = re.compile(
    r"\b(?:"
    r"(?:is|remains)\s+(?:still\s+|currently\s+)?"
    r"(?:in\s+progress|in\s+full\s+swing|underway|ongoing|active|live|"
    r"going|running|playing|"
    r"not\s+over|still\s+on|on|unfinished)|"
    r"has\s+yet\s+to\s+(?:end|finish|conclude)|"
    r"(?:is(?:n't|n\u2019t|\s+not)|has(?:n't|n\u2019t|\s+not))\s+"
    r"(?:done|over|ended|finished|completed|concluded|wrapped\s+up)(?:\s+yet)?|"
    r"(?:has|have)\s+not\s+wrapped\s+up|"
    r"(?:continues?|keeps\s+going)(?:\s+(?:today|now))?"
    r")\b",
    re.IGNORECASE,
)
_ACTIVE_PLAYER_STATUS = re.compile(
    r"\b(?:"
    r"i(?:['\u2019]m| am)\s+mid[- ]hand"
    r"(?:\s+(?:right\s+now|at\s+the\s+moment))?|"
    r"(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+"
    r"in\s+the\s+middle\s+of\s+(?:a|the)\s+hand|"
    r"(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+currently\s+"
    r"heads[- ]up(?:\s+in\s+(?:the|this)\s+(?:event|game|tourney|tournament))?|"
    r"i(?:['\u2019]m| am)\s+facing\s+(?:a\s+)?bet\s+"
    r"(?:right\s+now|at\s+this\s+moment)|"
    r"(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+still\s+(?:in|alive)|"
    r"(?:i|we)\s+still\s+have\s+chips|"
    r"(?:i|we)\s+remain(?:s)?\s+in\s+(?:the\s+)?"
    r"(?:contest|event|game|match|mtt|sng|tourney|tournament)|"
    r"(?:i|we|i(?:['\u2019]ve|\s+have)|we(?:['\u2019]ve|\s+have))\s+"
    r"(?:have\s+)?not\s+been\s+(?:eliminated|knocked\s+out)(?:\s+yet)?|"
    r"i\s+survived\s+and\s+am\s+still\s+(?:playing|competing|participating)|"
    r"i\s+(?:survived\s+and\s+am|am|remain)\s+(?:still\s+)?alive"
    r"(?:\s+in\s+(?:the|this)\s+"
    r"(?:competition|contest|event|game|match|mtt|sng|tourney|tournament))?|"
    r"it\s+is\s+my\s+turn(?:\s+in\s+(?:this|the)\s+(?:mtt|sng|event|game|tournament))?|"
    r"my\s+seat\s+is\s+(?:still\s+)?active|"
    r"(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+still\s+"
    r"(?:at\s+(?:the\s+)?table|in\s+(?:the\s+)?"
    r"(?:contest|event|game|match|mtt|sng|tourney|tournament)|"
    r"playing|competing|participating)|"
    r"(?:the\s+)?action\s+has\s+reached\s+me|"
    r"(?:the\s+)?action\s+is\s+back\s+on\s+me|"
    r"my\s+turn\s+has\s+come|"
    r"(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+not\s+out\s+of\s+"
    r"(?:the\s+)?(?:mtt|sng|event|tourney|tournament)(?:\s+yet)?|"
    r"(?:my\s+)?(?:time\s*bank|clock|decision\s+(?:clock|timer)|"
    r"action\s+(?:clock|timer))"
    r"\s+(?:is\s+)?(?:still\s+)?(?:counting\s+down|running|ticking)|"
    r"(?:my\s+)?time\s*bank\s+has\s+\d+\s+seconds?\s+(?:left|remaining)|"
    r"i\s+have\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|forty|fifty|sixty)"
    r"\s+seconds?\s+left(?:\s+to\s+(?:act|decide))?|"
    r"(?:there\s+(?:are|is)\s+)?(?:\d+|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|"
    r"forty|fifty|sixty)\s+seconds?\s+left\s+to\s+(?:act|decide)|"
    r"(?:the\s+)?(?:decision\s+|action\s+)?(?:clock|timer)\s+says\s+"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|twenty|thirty|forty|fifty|sixty)\s+seconds?"
    r")\b",
    re.IGNORECASE,
)
_ACTIVE_JAPANESE_EVENT_SUBJECT = re.compile(
    r"(?:\u5927\u4f1a|\u30c8\u30fc\u30ca\u30e1\u30f3\u30c8|\u30c8\u30ca\u30e1|"
    r"MTT|SNG|"
    r"\u30a4\u30d9\u30f3\u30c8|\u30b2\u30fc\u30e0|\u30cf\u30f3\u30c9)",
    re.IGNORECASE,
)
_ACTIVE_JAPANESE_EVENT_PREDICATE = re.compile(
    r"(?:\u958b\u50ac\u4e2d|\u7d9a\u884c\u4e2d|\u9032\u884c\u4e2d|"
    r"\u307e\u3060\u3084\u3063\u3066(?:\u3044)?(?:\u307e\u3059|\u3044\u308b)|"
    r"\u4eca\u3082\u7d9a\u3044\u3066|\u307e\u3060\u7d42\u308f\u3063\u3066\u3044\u306a|"
    r"\u7d42\u308f\u3063\u3066\u3044\u306a|\u7d42\u4e86\u3057\u3066\u3044\u306a|"
    r"\u771f\u3063\u6700\u4e2d|\u672a\u7d42\u4e86)",
    re.IGNORECASE,
)
_ACTIVE_JAPANESE_PLAYER_STATUS = re.compile(
    r"(?:(?:\u79c1|\u81ea\u5206)\u306f\u307e\u3060\u751f\u304d\u6b8b\u3063\u3066|"
    r"(?:\u79c1|\u81ea\u5206)\u306f\u307e\u3060\u52dd\u3061\u6b8b\u3063\u3066|"
    r"(?:\u79c1|\u81ea\u5206)?(?:\u306f)?\u307e\u3060"
    r"(?:\u5927\u4f1a|\u30c8\u30fc\u30ca\u30e1\u30f3\u30c8|\u30c8\u30ca\u30e1|MTT)"
    r"(?:\u306b)?\u6b8b\u3063\u3066\u3044\u307e\u3059|"
    r"(?:\u4eca\u306f)?(?:\u79c1|\u81ea\u5206)\u306e\u624b\u756a|"
    r"\u30a2\u30af\u30b7\u30e7\u30f3(?:\u304c|\u306f)?"
    r"\u56de\u3063\u3066\u304d\u307e\u3057\u305f|"
    r"\u624b\u756a(?:\u304c|\u306f)?\u56de\u3063\u3066\u304d\u307e\u3057\u305f|"
    r"\u6b8b\u308a\s*\d+\s*\u79d2(?:\u3067\u3059)?|"
    r"\u4eca(?:\u306f)?(?:\u30c8\u30fc\u30ca\u30e1\u30f3\u30c8|\u30c8\u30ca\u30e1|MTT)"
    r"\u4e2d(?:\u3067\u3059)?|"
    r"(?:\u30c8\u30fc\u30ca\u30e1\u30f3\u30c8|\u30c8\u30ca\u30e1|MTT)"
    r"\u3067\u30d7\u30ec\u30a4\u3057\u3066(?:\u308b|\u3044\u307e\u3059)|"
    r"\u307e\u3060[^.!?\r\n\u3002\uff01\uff1f]{0,24}"
    r"(?:\u5927\u4f1a|\u30c8\u30fc\u30ca\u30e1\u30f3\u30c8|\u30c8\u30ca\u30e1|MTT)"
    r"(?:\u304b\u3089)?\u8131\u843d\u3057\u3066\u3044\u307e\u305b\u3093|"
    r"\u307e\u3060\u98db\u3093\u3067\u3044\u307e\u305b\u3093|"
    r"\u4eca[^.!?\r\n\u3002\uff01\uff1f]{0,16}"
    r"(?:\u5927\u4f1a|\u30c8\u30fc\u30ca\u30e1\u30f3\u30c8|\u30c8\u30ca\u30e1|MTT)"
    r"\u306b\u51fa\u3066\u3044\u307e\u3059|"
    r"\u3042\u3068\s*\d+\s*\u79d2"
    r"(?:\u3067\u3059|[^.!?\r\n\u3002\uff01\uff1f]{0,64}"
    r"(?:\u6c7a\u3081|\u30a2\u30af\u30b7\u30e7\u30f3))|"
    r"\u6b8b\u308a\u6642\u9593(?:\u306f|\u304c)?\s*\d+\s*\u79d2(?:\u3067\u3059)?|"
    r"\u307e\u3060(?:\u30d7\u30ec\u30a4\u4e2d|"
    r"(?:\u3053\u306e|\u305d\u306e)?\u5353\u306b\u3044(?:\u307e\u3059|\u308b))|"
    r"\u30cf\u30f3\u30c9\u306e\u9014\u4e2d(?:\u3067\u3059)?|"
    r"(?:\u79c1|\u81ea\u5206)\u306e\u756a\u304c\u6765(?:\u307e\u3057\u305f|\u305f)|"
    r"\u4eca(?:\u306f)?\u30d8\u30c3\u30ba\u30a2\u30c3\u30d7(?:\u3067\u3059)?)",
    re.IGNORECASE,
)
_ACTIVE_JAPANESE_PLAYER_SUBJECT = re.compile(
    r"(?:\u79c1|\u81ea\u5206)\u306f\u307e\u3060",
)
_ACTIVE_JAPANESE_PLAYER_PREDICATE = re.compile(
    r"(?:(?:\u306b)?\u3044(?:\u308b|\u307e\u3059)|\u6b8b\u3063\u3066|\u53c2\u52a0\u4e2d)",
)
_ACTIVE_JAPANESE_TIMER_SUBJECT = re.compile(
    r"(?:\u30bf\u30a4\u30e0\u30d0\u30f3\u30af|\u6301\u3061\u6642\u9593|"
    r"\u6b8b\u308a\u6642\u9593)",
)
_ACTIVE_JAPANESE_TIMER_PREDICATE = re.compile(
    r"(?:\u6e1b\u3063\u3066|\u6e1b\u5c11|\u30ab\u30a6\u30f3\u30c8\u30c0\u30a6\u30f3|"
    r"\u6b8b\u308a\s*\d+\s*\u79d2)",
)
_SENTENCE_BOUNDARY = re.compile(r"[.!?\r\n\u3002\uff01\uff1f]+")

_NEGATED_REAL_TIME_CONTEXT = re.compile(
    r"(?:(?:ただいま|いま|今|現在)(?:は)?.{0,24}"
    r"(?:オンライン(?:ポーカー|卓)?|ポーカー|ハンド|ゲーム|卓).{0,20}"
    r"(?:中ではありません|中ではない|プレイしていません|参加していません|"
    r"打っていません)|"
    r"\b(?:i(?:['\u2019]m| am)\s+)?not\s+(?:currently\s+)?"
    r"(?:playing(?:\s+poker)?|at\s+(?:a|the)\s+poker\s+table|"
    r"in\s+(?:an?\s+)?(?:online\s+)?(?:poker\s+|cash\s+)?game|"
    r"seated\s+at\s+(?:a|the)\s+(?:poker|tournament)\s+table)\s+"
    r"(?:right\s+)?now\b|"
    r"今(?:まさに)?プレイ中"
    r"(?:ではありません|じゃありません|ではない|じゃない|ではなくて?|じゃなくて?)"
    r"(?!\s*(?:です)?(?:[か\uFF1F?]|わけでは?ない)|\s*は?ない))",
    re.IGNORECASE,
)

_BLOCKED_RULE_GUIDANCE = {
    "scope-field-unspecified": (
        '`analysis_scope="unspecified"`は安全側で拒否しました。'
        '事後検討であることを明示して`analysis_scope="retrospective"`を指定してください。'
    ),
    "scope-field-real-time": (
        '`analysis_scope="real_time"`は対応範囲外です。プレイ終了後の記録を使い、'
        '`analysis_scope="retrospective"`として依頼してください。'
    ),
    "scope-real-time": (
        "入力から現在進行中のプレイ支援を検出しました。プレイ終了後の事後検討として"
        "依頼し直してください。"
    ),
    "private-cards": "相手の非公開カードの取得・推測支援には対応しません。",
    "collusion": "共謀やホールカード共有を伴う依頼には対応しません。",
    "automated-play": "自動プレイや意思決定ボットの運用支援には対応しません。",
    "detection-evasion": "検出・アンチチート回避の支援には対応しません。",
}

_BLOCKING_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "private_cards",
        "private-cards",
        re.compile(
            r"(?:相手の(?:非公開|ホール)カード.{0,20}(?:教えて|取得|見る)|"
            r"(?:reveal|get|show).{0,20}opponent(?:'s)?\s+(?:private|hole)\s+cards)",
            re.IGNORECASE,
        ),
    ),
    (
        "collusion",
        "collusion",
        re.compile(
            r"(?:共謀して|カードを共有して勝|collude\s+with\s+me|share\s+hole\s+cards)",
            re.IGNORECASE,
        ),
    ),
    (
        "automated_play",
        "automated-play",
        re.compile(
            r"(?:自動(?:で)?プレイするボット|ボット.{0,20}自動プレイ|"
            r"(?:build|run).{0,20}(?:poker\s+)?bot.{0,20}(?:auto|play))",
            re.IGNORECASE,
        ),
    ),
    (
        "detection_evasion",
        "detection-evasion",
        re.compile(
            r"(?:検出を回避|BANを回避|アンチチート.{0,10}回避|"
            r"(?:evade|bypass).{0,20}(?:detection|anti[- ]?cheat))",
            re.IGNORECASE,
        ),
    ),
)
_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prompt-injection-ignore",
        re.compile(
            r"(?:(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior)?\s*"
            r"(?:rules|instructions)|"
            r"以前の(?:指示|ルール)を無視)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt-injection-override",
        re.compile(
            r"(?:override|replace|bypass)\s+(?:the\s+)?"
            r"(?:system|developer)\s+(?:prompt|instructions)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt-injection-tool",
        re.compile(
            r"(?:execute\s+(?:a\s+)?shell|run\s+this\s+command|シェルを実行|コマンドを実行)",
            re.IGNORECASE,
        ),
    ),
)


def _security_probe(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    without_categories = normalized
    if any(
        unicodedata.category(character) in _SECURITY_IGNORABLE_CATEGORIES
        for character in normalized
    ):
        without_categories = "".join(
            character
            for character in normalized
            if unicodedata.category(character) not in _SECURITY_IGNORABLE_CATEGORIES
        )
    without_ignorables = _SECURITY_IGNORABLES.sub("", without_categories)
    return without_ignorables.translate(_SECURITY_DASH_TRANSLATION).casefold()


def _secret_probe(value: str) -> str:
    """Build a conservative ASCII skeleton only for secret-shape matching."""

    normalized = (
        unicodedata.normalize("NFKC", value)
        .translate(_SECURITY_DASH_TRANSLATION)
        .casefold()
        .translate(_SECRET_CONFUSABLE_TRANSLATION)
    )
    skeleton: list[str] = []
    for character in normalized:
        codepoint = ord(character)
        if character in "\r\n":
            skeleton.append(character)
        elif character.isspace():
            skeleton.append(" ")
        elif (
            0x30 <= codepoint <= 0x39
            or 0x41 <= codepoint <= 0x5A
            or 0x61 <= codepoint <= 0x7A
            or character in "_-=:"
        ):
            skeleton.append(character)
    return "".join(skeleton)


def _contains_secret_shape(value: str) -> bool:
    if _MIXED_SCRIPT_SECRET_ASSIGNMENT.search(unicodedata.normalize("NFKC", value)):
        return True
    probes = (_security_probe(value), _secret_probe(value))
    if any(pattern.search(probe) is not None for probe in probes for pattern in _SECRET_PATTERNS):
        return True
    sensitive_assignment_suffixes = (
        "apikey",
        "authorization",
        "cookie",
        "password",
        "passwd",
        "secret",
        "secretaccesskey",
        "sessiontoken",
        "accesstoken",
        "clientsecret",
        "token",
    )
    assignment_probe = re.sub(
        r"(?<=[A-Za-z0-9_=:\-])[\r\n]+(?=[A-Za-z0-9_=:\-])",
        "",
        probes[1],
    )
    max_suffix_length = max(map(len, sensitive_assignment_suffixes))
    for line in assignment_probe.splitlines():
        last_nonspace = len(line.rstrip()) - 1
        identifier_tail = ""
        identifier_is_sensitive = False
        for index, character in enumerate(line):
            if character.isascii() and character.isalnum():
                identifier_tail = (identifier_tail + character)[-max_suffix_length:]
                identifier_is_sensitive = any(
                    identifier_tail.endswith(suffix) for suffix in sensitive_assignment_suffixes
                )
            if character in ":=" and index < last_nonspace and identifier_is_sensitive:
                return True
    return False


_SENSITIVE_ASSIGNMENT_NAMES = (
    "apikey",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "secretaccesskey",
    "sessiontoken",
    "accesstoken",
    "clientsecret",
    "token",
)


def contains_sensitive_data_across_fragments(values: list[str]) -> bool:
    """Detect one secret assignment split across nonadjacent durable fields."""

    open_identifier_prefixes: set[str] = set()
    max_name_length = max(map(len, _SENSITIVE_ASSIGNMENT_NAMES))
    for value in values:
        if _contains_secret_shape(value):
            return True
        head = value[:512]
        if any(_contains_secret_shape(prefix + head) for prefix in open_identifier_prefixes):
            return True
        probe = _secret_probe(value)
        trailing = re.search(r"[a-z0-9_\-:= \r\n]+$", probe)
        if trailing is None:
            continue
        canonical_tail = re.sub(r"[^a-z0-9]", "", trailing.group(0))[-max_name_length:]
        for start in range(len(canonical_tail)):
            suffix = canonical_tail[start:]
            if len(suffix) >= 2 and any(
                name.startswith(suffix) for name in _SENSITIVE_ASSIGNMENT_NAMES
            ):
                open_identifier_prefixes.add(suffix)
    return False


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    if _contains_secret_shape(redacted):
        return "[REDACTED]"
    return redacted


def _collision_free_key(candidate: str, occupied: dict[str, Any]) -> str:
    if candidate not in occupied:
        return candidate
    ordinal = 2
    while True:
        alternative = f"{candidate} [collision {ordinal}]"
        if alternative not in occupied:
            return alternative
        ordinal += 1


def redact_sensitive(value: Any, *, enabled: bool = True) -> Any:
    """Return a JSON-compatible copy with common secret shapes removed."""

    if not enabled:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        return value
    if isinstance(value, BaseModel):
        return redact_sensitive(value.model_dump(mode="json"), enabled=True)
    if isinstance(value, dict):
        redacted_mapping: dict[str, Any] = {}
        for key, item in value.items():
            raw_key = str(key)
            safe_key = _collision_free_key(_redact_text(raw_key), redacted_mapping)
            redacted_mapping[safe_key] = (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(_security_probe(raw_key))
                or _SENSITIVE_KEY.search(_secret_probe(raw_key))
                else redact_sensitive(item, enabled=True)
            )
        return redacted_mapping
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item, enabled=True) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _isolate_prompt_injection_text(value: str) -> str:
    if any(pattern.search(_security_probe(value)) for _, pattern in _INJECTION_RULES):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"[PROMPT_INJECTION_REMOVED:{digest}]"
    return value


def isolate_prompt_injection(value: Any) -> Any:
    """Replace injection-bearing strings before any provider receives them."""

    if isinstance(value, BaseModel):
        return isolate_prompt_injection(value.model_dump(mode="json"))
    if isinstance(value, dict):
        isolated: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = _isolate_prompt_injection_text(str(key))
            while safe_key in isolated:
                safe_key += "_"
            isolated[safe_key] = isolate_prompt_injection(item)
        return isolated
    if isinstance(value, (list, tuple)):
        return [isolate_prompt_injection(item) for item in value]
    if isinstance(value, str):
        return _isolate_prompt_injection_text(value)
    return value


def _walk_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            strings.append(str(key))
            strings.extend(_walk_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            strings.extend(_walk_strings(item))
    elif isinstance(value, str):
        strings.append(value)
    return strings


def _has_unqualified_partial_live_context(cleaned: str) -> bool:
    retrospective_live_spans = tuple(
        match.span()
        for match in _LIVE_PLAY_CONTEXT.finditer(cleaned)
        if _RETROSPECTIVE_LIVE_SUFFIX.match(cleaned, match.end()) is not None
    )
    span_index = 0
    for partial in _PARTIAL_LIVE_CONTEXT.finditer(cleaned):
        while (
            span_index < len(retrospective_live_spans)
            and retrospective_live_spans[span_index][1] < partial.end()
        ):
            span_index += 1
        if span_index == len(retrospective_live_spans):
            return True
        live_start, live_end = retrospective_live_spans[span_index]
        if live_start > partial.start() or live_end < partial.end():
            return True
    return False


def _strip_archived_quotations(cleaned: str) -> str:
    slices: list[str] = []
    cursor = 0
    for quoted in _QUOTED_SEGMENT.finditer(cleaned):
        prefix = cleaned[max(0, quoted.start() - 1024) : quoted.start()]
        attribution = _ARCHIVED_ATTRIBUTION.search(prefix)
        if attribution is None:
            continue
        context = prefix[max(0, attribution.start() - 768) :]
        attribution_context = prefix[max(0, attribution.start() - 128) :]
        if (
            _ARCHIVED_CONTEXT.search(context) is None
            or _LIVE_ATTRIBUTION.search(attribution_context) is not None
        ):
            continue
        slices.append(cleaned[cursor : quoted.start()])
        cursor = quoted.end()
    if not slices:
        return cleaned
    slices.append(cleaned[cursor:])
    return "".join(slices)


def _has_retrospective_reversal(cleaned: str) -> bool:
    for live_match in _LIVE_PLAY_CONTEXT.finditer(cleaned):
        suffix = _RETROSPECTIVE_LIVE_SUFFIX.match(cleaned, live_match.end())
        if suffix is None:
            continue
        if _RETROSPECTIVE_REVERSAL.search(
            cleaned,
            suffix.end(),
            min(len(cleaned), suffix.end() + 512),
        ):
            return True
    return False


def _has_active_live_status(cleaned: str) -> bool:
    if (
        _ACTIVE_PLAYER_STATUS.search(cleaned) is not None
        or _ACTIVE_JAPANESE_PLAYER_STATUS.search(cleaned) is not None
    ):
        return True
    for clause in _SENTENCE_BOUNDARY.split(cleaned):
        if (
            (
                (
                    _ACTIVE_GAME_SUBJECT.search(clause) is not None
                    or _BARE_ACTIVE_GAME_SUBJECT.search(clause) is not None
                )
                and _ACTIVE_GAME_PREDICATE.search(clause) is not None
            )
            or (
                _ACTIVE_JAPANESE_EVENT_SUBJECT.search(clause) is not None
                and _ACTIVE_JAPANESE_EVENT_PREDICATE.search(clause) is not None
            )
            or (
                _ACTIVE_JAPANESE_PLAYER_SUBJECT.search(clause) is not None
                and _ACTIVE_JAPANESE_EVENT_SUBJECT.search(clause) is not None
                and _ACTIVE_JAPANESE_PLAYER_PREDICATE.search(clause) is not None
            )
            or (
                _ACTIVE_JAPANESE_TIMER_SUBJECT.search(clause) is not None
                and _ACTIVE_JAPANESE_TIMER_PREDICATE.search(clause) is not None
            )
        ):
            return True
    return False


def real_time_assistance_signals(value: Any) -> tuple[bool, bool, bool]:
    """Return live-context, decision-request, and explicit-assistance signals."""

    cleaned_texts: list[str] = []
    partial_live_context_present = False
    for text in _walk_strings(value):
        cleaned = _security_probe(text)
        cleaned = _strip_archived_quotations(cleaned)
        cleaned = _ARCHIVED_QUOTATION.sub("", cleaned)
        cleaned = _INERT_LANGUAGE_EXAMPLE.sub("", cleaned)
        cleaned = _HISTORICAL_STATUS_CLAUSE.sub("", cleaned)
        cleaned = _NEGATED_REAL_TIME_CONTEXT.sub("", cleaned)
        cleaned_texts.append(cleaned)
        partial_live_context_present = (
            partial_live_context_present or _has_unqualified_partial_live_context(cleaned)
        )
    combined = "\n".join(cleaned_texts)
    decision_request_present = _DECISION_REQUEST.search(combined) is not None
    explicit_assistance_present = _REAL_TIME_ASSISTANCE.search(combined) is not None
    live_context_present = partial_live_context_present or any(
        _RETROSPECTIVE_LIVE_SUFFIX.match(combined, match.end()) is None
        for match in _LIVE_PLAY_CONTEXT.finditer(combined)
    )
    if (
        _LIVE_CONTRADICTION.search(combined) is not None
        or _ACTIVE_LIVE_STATUS.search(combined) is not None
        or _EXPLICIT_ACTIVE_LIVE_STATUS.search(combined) is not None
        or _has_active_live_status(combined)
    ):
        live_context_present = True
    return (
        live_context_present,
        decision_request_present,
        explicit_assistance_present,
    )


def _contains_real_time_assistance(value: Any) -> bool:
    live_context, decision_request, explicit_assistance = real_time_assistance_signals(value)
    return explicit_assistance or (live_context and decision_request)


def contains_real_time_assistance(value: Any) -> bool:
    """Return whether strong live context and decision language coexist in one envelope."""

    return _contains_real_time_assistance(value)


def blocked_security_guidance(events: list[SecurityEvent]) -> list[str]:
    """Return deterministic user guidance for each distinct blocked security rule."""

    guidance: list[str] = []
    seen: set[str] = set()
    for event in events:
        if not event.blocked or event.rule_id in seen:
            continue
        seen.add(event.rule_id)
        guidance.append(
            _BLOCKED_RULE_GUIDANCE.get(
                event.rule_id,
                f"安全規則`{event.rule_id}`により事後検討を開始できませんでした。",
            )
        )
    return guidance


def screen_case(case: CaseInput) -> list[SecurityEvent]:
    """Detect prohibited live assistance and record inert injection attempts."""

    dumped = case.model_dump(mode="json")
    serialized = json.dumps(dumped, ensure_ascii=False, sort_keys=True, allow_nan=False)
    security_probe = _security_probe(serialized)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    events: list[SecurityEvent] = []
    seen: set[tuple[str, str]] = set()

    def add(category: str, rule_id: str, *, blocked: bool) -> None:
        key = (category, rule_id)
        if key in seen:
            return
        seen.add(key)
        events.append(
            SecurityEvent(
                category=category,
                rule_id=rule_id,
                action="refused" if blocked else "recorded",
                blocked=blocked,
                input_sha256=digest,
            )
        )

    if case.analysis_scope == "real_time":
        add("real_time_assistance", "scope-field-real-time", blocked=True)
    if case.analysis_scope == "unspecified":
        add("real_time_assistance", "scope-field-unspecified", blocked=True)
    if _contains_real_time_assistance(dumped):
        add("real_time_assistance", "scope-real-time", blocked=True)
    for category, rule_id, pattern in _BLOCKING_RULES:
        if pattern.search(security_probe):
            add(category, rule_id, blocked=True)
    for rule_id, pattern in _INJECTION_RULES:
        if pattern.search(security_probe):
            add("prompt_injection", rule_id, blocked=False)
    return events
