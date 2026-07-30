#!/usr/bin/env python3
"""Live advisor: should I call for a check here, and which one?

Everything below is learned from 134h of professional play (see
[[cr-skill-checks]]). It is ADVISORY — it answers "is this inside the range
of what good DMs do", never "this is the right answer", because the corpus
shows the choice is genuinely underdetermined.

  advise "<player intent>" [--scene SOCIAL|EXPLORATION|COMBAT] [--situation "..."]
"""
import argparse
import json
import re
import sys

SKILLS = ["acrobatics", "animal handling", "arcana", "athletics", "deception",
          "history", "insight", "intimidation", "investigation", "medicine",
          "nature", "perception", "performance", "persuasion", "religion",
          "sleight of hand", "stealth", "survival"]

# Deterministic intent-cue -> skill table, written from the SRD's own skill
# definitions. A fired cue is a stronger signal than any scene prior, but it is
# still only a suggestion: see DEFENSIBLE below for why the choice among
# neighbouring skills is a judgment call rather than a fact.
RULES = [
    (r"\b(hide|hiding|sneak|sneaking|quietly|unseen|stay out of sight|shadows)\b", "stealth"),
    (r"\b(search|searching|look for|examine|inspect|study|comb through|rummage|"
     r"look around the|check the (room|body|desk|drawer)|clues?|traps?)\b", "investigation"),
    (r"\b(listen|hear|notice|spot|keep (an )?eye|watch for|look out|scan|"
     r"see anything|anyone (around|there)|on watch)\b", "perception"),
    (r"\b(convince|persuade|talk (him|her|them) into|plead|ask (him|her|them) to|"
     r"beg|negotiate|reason with|smooth (this|it) over)\b", "persuasion"),
    (r"\b(i(?:'m going to| will|'ll)? (?:lie|bluff|deceive)|i lie|"
     r"pretend (?:to be|that i)|pass (?:myself|ourselves) off|fake it|"
     r"make up a story|cover story|bluff (?:him|her|them))\b", "deception"),
    (r"\b(threaten|intimidate|scare|frighten|menace|lean on (him|her|them))\b", "intimidation"),
    (r"\b(read (?:him|her|them|whether|if)|(?:are|is) (?:they|he|she) lying|"
     r"do i believe|sense (?:motive|whether)|gauge|telling the truth|"
     r"get a read|can i tell if|seem (?:honest|sincere|genuine))\b", "insight"),
    (r"\b(climb|climbing|jump|leap|swim|lift|shove|push|pull|force (the|open)|"
     r"break (down|through)|hold on|grapple)\b", "athletics"),
    (r"\b(tumble|flip|balance|acrobat|land on my feet|squeeze through|dodge past)\b",
     "acrobatics"),
    (r"\b(magic|magical|arcane|spell|enchant|glyph|rune|sigil|identify|"
     r"detect magic|what kind of magic)\b", "arcana"),
    (r"\b(heal|wound|injur|poison|disease|stabilize|first aid|how did (he|she|they) die|"
     r"cause of death|medical)\b", "medicine"),
    (r"\b(track|tracking|footprints|trail|forage|navigate|weather|survive|campsite)\b",
     "survival"),
    (r"\b(plant|animal|beast|creature|terrain|natural|forest|herb|is it poisonous)\b",
     "nature"),
    (r"\b(god|deity|temple|shrine|holy|divine|prayer|cleric|religious|symbol of)\b",
     "religion"),
    (r"\b(history|historical|ancient|records|lore|who built|how old|legend|"
     r"heard of|remember (any|anything) about)\b", "history"),
    (r"\b(pickpocket|palm|slip (it|them)|pick (the )?lock|sleight|conceal on my person|"
     r"lift (his|her|their) purse)\b", "sleight of hand"),
    (r"\b(sing|play (a|the) (song|lute|instrument)|dance|perform|entertain|"
     r"tell a (joke|story) to the crowd)\b", "performance"),
    (r"\b(calm|soothe|ride|mount|befriend (the|a) (dog|horse|beast|animal)|"
     r"train (the|a) animal)\b", "animal handling"),
]

COMP = [(re.compile(pat, re.I), sk) for pat, sk in RULES]

# Scene-conditioned priors, measured (757 skill calls).
PRIORS = {
    "EXPLORATION": {"perception": .40, "investigation": .23, "arcana": .10,
                    "stealth": .10, "survival": .04, "athletics": .04,
                    "history": .03, "nature": .03},
    "SOCIAL": {"persuasion": .20, "insight": .13, "perception": .10,
               "deception": .09, "investigation": .09, "arcana": .07,
               "history": .06, "performance": .05, "intimidation": .04},
    "COMBAT": {"athletics": .33, "perception": .20, "stealth": .15,
               "acrobatics": .09, "investigation": .07, "arcana": .07},
}
# Mutually defensible sets, derived from intent-language cosine >= 0.60.
DEFENSIBLE = {
    "perception": {"perception", "investigation", "arcana", "stealth"},
    "investigation": {"investigation", "perception", "arcana"},
    "arcana": {"arcana", "investigation", "perception"},
    "stealth": {"stealth", "perception"},
}
# The three-part gate. A proposal draws a roll only ~6.9% of the time.
GATE = ["Is the outcome genuinely uncertain for THIS character?",
        "Would failure be interesting rather than merely blocking?",
        "Is the character actually being tested, not just acting?"]


ACTION_VERB = re.compile(r"\b(search|look|listen|climb|jump|sneak|hide|convince|"
                         r"persuade|lie|threaten|examine|investigate|track|pick|"
                         r"swim|push|pull|grab|force|balance|shove)\b", re.I)


def roll_likelihood(intent, scene):
    """Learned signals for WHETHER to roll (base rate 6.9% of proposals).
    Exploration scenes: 49% of roll-drawing proposals vs 24% of no-roll ones.
    A concrete action verb: 28.1% vs 11.5%."""
    p = 0.069
    if scene.upper() == "EXPLORATION":
        p *= 2.0
    if ACTION_VERB.search(intent):
        p *= 2.4
    return min(p, 0.6)


def advise(intent, scene="SOCIAL", situation=""):
    text = f"{situation} {intent}"
    cued = []
    for rx, sk in COMP:
        if rx.search(text):
            cued.append(sk)
    prior = PRIORS.get(scene.upper(), PRIORS["SOCIAL"])
    scored = {}
    for sk in set(list(prior) + cued):
        p = prior.get(sk, 0.02)
        if sk in cued:
            # A fired intent cue is 46% precise (measured) — better than any
            # scene prior, whose top entry is 40%. So a cue must DOMINATE the
            # prior, not merely be weighted by it.
            p = max(p * 6.0, 0.5)
        scored[sk] = p
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])[:4]
    top = ranked[0][0]
    lik = roll_likelihood(intent, scene)
    return {
        "should_i_roll": {
            "estimated_likelihood": round(lik, 3),
            "verdict": ("worth a check" if lik >= 0.20 else
                        "probably just narrate it"),
            "gate": GATE,
        },
        "roll_gate": GATE,
        "base_rate_reminder": "a player proposal draws a roll only ~7% of the "
                              "time at professional tables — default to letting it happen",
        "scene": scene.upper(),
        "cues_found": sorted(set(cued)) or None,
        "candidates": [{"skill": s, "weight": round(w, 3)} for s, w in ranked],
        "defensible_set": sorted(DEFENSIBLE.get(top, {top})),
        "note": ("choice among the defensible set is a judgment call, not a "
                 "fact — say the skill AND the reason so the table learns the boundary"),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="skilladvisor")
    ap.add_argument("intent")
    ap.add_argument("--scene", default="SOCIAL")
    ap.add_argument("--situation", default="")
    a = ap.parse_args()
    print(json.dumps(advise(a.intent, a.scene, a.situation), indent=1))
