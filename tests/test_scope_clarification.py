"""Stage 1's scope question has to be answerable, or it loops forever.

The failure this guards against, observed in the studio UI: the operator is
asked "all institute modules, or only selected?", types an answer, and the same
keyword matcher that failed the first time fails again and re-asks the same
question. Nothing on screen changes, so the run looks broken rather than
blocked, and there is no phrasing that escapes — the box that collects the
answer is not the thing that decides.

Two ways out are tested here: saying "all of it" in words, and naming the
modules outright in the goal payload.
"""

import unittest

from agents.problem_definition_agent import (MODULE_DEFINITIONS,
                                             ProblemDefinitionAgent)
from scripts.run_pipeline import wrap_goal


def brief_for(question, modules=None):
    """Exactly the payload the studio sends — a hand-built goal misses fields
    and raises unrelated clarifying questions, which would test nothing."""
    payload = wrap_goal(question)
    if modules is not None:
        payload["goal"]["modules"] = {name: {"enabled": name in modules}
                                      for name in MODULE_DEFINITIONS}
    return ProblemDefinitionAgent().run(payload)


def enabled(brief):
    return sorted(brief["scope"]["enabled_modules"])


class ScopeAnswerInWords(unittest.TestCase):

    def test_a_request_naming_no_module_asks_the_scope_question(self):
        brief = brief_for("pal branch")
        self.assertEqual(brief["status"], "needs_clarification")
        self.assertTrue(any("modules should be in scope" in q
                            for q in brief["clarifying_questions"]))

    def test_answering_all_modules_is_taken_as_the_answer(self):
        brief = brief_for("all modules")
        self.assertNotEqual(brief["status"], "needs_clarification")
        self.assertEqual(enabled(brief), sorted(MODULE_DEFINITIONS))

    def test_the_operators_own_answer_is_accepted(self):
        # Typed verbatim into the studio: one dropped keystroke in "all", which
        # previously cost an unescapable re-ask.
        brief = brief_for("pal branch al module")
        self.assertNotEqual(brief["status"], "needs_clarification")
        self.assertEqual(enabled(brief), sorted(MODULE_DEFINITIONS))

    def test_other_whole_institute_phrasings(self):
        for text in ("everything", "whole institute", "every module",
                     "all of it", "the entire business"):
            with self.subTest(text=text):
                self.assertTrue(ProblemDefinitionAgent._scope_is_all(text))

    def test_scope_words_do_not_fire_on_ordinary_questions(self):
        for text in ("pal branch", "why are fees pending",
                     "which courses have dropout", "pal"):
            with self.subTest(text=text):
                self.assertFalse(ProblemDefinitionAgent._scope_is_all(text))

    def test_a_question_that_names_a_module_still_scopes_to_it(self):
        # The escape hatch must not widen scope for requests that were specific.
        brief = brief_for("why is pending fee so high")
        self.assertEqual(enabled(brief), ["fee_management"])


class ScopeAnswerAsModules(unittest.TestCase):

    def test_explicit_modules_answer_the_question_without_any_keyword(self):
        brief = brief_for("pal branch", modules=["fee_management", "courses"])
        self.assertNotEqual(brief["status"], "needs_clarification")
        self.assertEqual(enabled(brief), ["courses", "fee_management"])

    def test_explicit_modules_win_over_the_words(self):
        brief = brief_for("why is pending fee so high", modules=["courses"])
        self.assertEqual(enabled(brief), ["courses"])


if __name__ == "__main__":
    unittest.main()
