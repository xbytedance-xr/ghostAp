"""Slock Council orchestration.

Runs a structured same-question council flow:
1. multiple agents answer independently,
2. agents anonymously review/rank those answers,
3. a chairman agent synthesizes the final response.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from .models import (
    AgentIdentity,
    CouncilAggregate,
    CouncilResponse,
    CouncilReview,
    CouncilRun,
    CouncilStatus,
)

logger = logging.getLogger(__name__)

_LABEL_PREFIX = "Response"
_CHAIRMAN_ROLES: tuple[str, ...] = ("chair", "architect", "planner", "reviewer")


class CouncilManager:
    """Coordinates the Slock Council protocol for one engine instance."""

    def __init__(self, *, engine) -> None:
        self._engine = engine

    def run(
        self,
        question: str,
        *,
        participants: list[AgentIdentity],
        chairman: Optional[AgentIdentity] = None,
        on_stage: Optional[Callable[[CouncilRun], None]] = None,
        timeout: float = 300.0,
    ) -> CouncilRun:
        """Run the full council flow and return the completed run snapshot."""
        run = CouncilRun(
            channel_id=getattr(self._engine, "chat_id", ""),
            question=question,
            participant_ids=[agent.agent_id for agent in participants],
            chairman_agent_id=(chairman.agent_id if chairman else ""),
        )
        if len(participants) < 2:
            run.status = CouncilStatus.FAILED
            run.error = "Council requires at least two participants."
            run.completed_at = time.time()
            self._emit(on_stage, run)
            return run

        chairman = chairman or self._select_chairman(participants)
        run.chairman_agent_id = chairman.agent_id

        try:
            run.status = CouncilStatus.STAGE1_RUNNING
            self._emit(on_stage, run)
            run.responses = self._collect_independent_responses(question, participants, timeout)
            run.label_to_agent = {response.label: response.agent_id for response in run.responses}
            run.status = CouncilStatus.STAGE1_DONE
            self._emit(on_stage, run)

            run.status = CouncilStatus.STAGE2_RUNNING
            self._emit(on_stage, run)
            run.reviews = self._collect_peer_reviews(question, participants, run.responses, timeout)
            run.aggregate_rankings = calculate_aggregate_rankings(run.reviews, run.responses)
            run.status = CouncilStatus.STAGE2_DONE
            self._record_skill_feedback(question, run)
            self._emit(on_stage, run)

            run.status = CouncilStatus.STAGE3_RUNNING
            self._emit(on_stage, run)
            run.final_response = self._synthesize_final(question, chairman, run, timeout)
            run.status = CouncilStatus.COMPLETED
            run.completed_at = time.time()
            self._emit(on_stage, run)
            return run
        except Exception as exc:
            logger.error("Slock council run failed: %s", exc, exc_info=True)
            run.status = CouncilStatus.FAILED
            run.error = str(exc)
            run.completed_at = time.time()
            self._emit(on_stage, run)
            return run

    def _collect_independent_responses(
        self,
        question: str,
        participants: list[AgentIdentity],
        timeout: float,
    ) -> list[CouncilResponse]:
        labels = [_label_for_index(idx) for idx in range(len(participants))]
        responses: list[CouncilResponse] = []
        with ThreadPoolExecutor(max_workers=len(participants), thread_name_prefix="slock_council_stage1") as pool:
            futures = {
                pool.submit(self._ask_independent, agent, question, timeout): (label, agent)
                for label, agent in zip(labels, participants)
            }
            for future in as_completed(futures, timeout=timeout):
                label, agent = futures[future]
                try:
                    content = future.result() or ""
                    responses.append(
                        CouncilResponse(
                            label=label,
                            agent_id=agent.agent_id,
                            agent_name=agent.name,
                            content=content,
                        )
                    )
                except Exception as exc:
                    responses.append(
                        CouncilResponse(
                            label=label,
                            agent_id=agent.agent_id,
                            agent_name=agent.name,
                            error=str(exc),
                        )
                    )
        responses.sort(key=lambda item: labels.index(item.label))
        return responses

    def _collect_peer_reviews(
        self,
        question: str,
        reviewers: list[AgentIdentity],
        responses: list[CouncilResponse],
        timeout: float,
    ) -> list[CouncilReview]:
        valid_labels = [response.label for response in responses]
        reviews: list[CouncilReview] = []
        with ThreadPoolExecutor(max_workers=len(reviewers), thread_name_prefix="slock_council_stage2") as pool:
            futures = {
                pool.submit(self._ask_reviewer, reviewer, question, responses, timeout): reviewer
                for reviewer in reviewers
            }
            for future in as_completed(futures, timeout=timeout):
                reviewer = futures[future]
                try:
                    content = future.result() or ""
                except Exception as exc:
                    content = f"Review failed: {exc}"
                parsed = parse_ranking_from_text(content, valid_labels)
                reviews.append(
                    CouncilReview(
                        reviewer_agent_id=reviewer.agent_id,
                        reviewer_name=reviewer.name,
                        content=content,
                        parsed_ranking=parsed,
                    )
                )
        reviews.sort(key=lambda item: [r.agent_id for r in reviewers].index(item.reviewer_agent_id))
        return reviews

    def _ask_independent(self, agent: AgentIdentity, question: str, timeout: float) -> str:
        prompt = (
            "You are one member of a Slock council. Answer independently from your own role.\n"
            "Do not mention other agents. Be specific, evidence-oriented, and include tradeoffs.\n\n"
            f"# Council Question\n{question}"
        )
        full_prompt = self._engine.build_agent_prompt(agent, prompt)
        return self._engine.run_agent_session(agent, full_prompt, timeout=timeout) or ""

    def _ask_reviewer(
        self,
        reviewer: AgentIdentity,
        question: str,
        responses: list[CouncilResponse],
        timeout: float,
    ) -> str:
        response_text = "\n\n".join(
            f"{response.label}:\n{response.content or response.error}"
            for response in responses
        )
        prompt = (
            "You are reviewing anonymized Slock council answers. Do not infer or favor identities.\n"
            "Evaluate accuracy, usefulness, risks, and insight. Finish with an exact FINAL RANKING section.\n\n"
            f"# Original Question\n{question}\n\n"
            f"# Anonymous Answers\n{response_text}\n\n"
            "Required final format:\n"
            "FINAL RANKING:\n"
            "1. Response A\n"
            "2. Response B\n"
            "Only use labels that appear above."
        )
        full_prompt = self._engine.build_agent_prompt(reviewer, prompt)
        return self._engine.run_agent_session(reviewer, full_prompt, timeout=timeout) or ""

    def _synthesize_final(
        self,
        question: str,
        chairman: AgentIdentity,
        run: CouncilRun,
        timeout: float,
    ) -> str:
        responses = "\n\n".join(
            f"{response.label} ({response.agent_name}):\n{response.content or response.error}"
            for response in run.responses
        )
        reviews = "\n\n".join(
            f"{review.reviewer_name} ranking: {', '.join(review.parsed_ranking) or 'unparsed'}\n{review.content}"
            for review in run.reviews
        )
        aggregate = "\n".join(
            f"- {item.label} ({item.agent_name}): avg rank {item.average_rank:.2f}, score {item.quality_score:.1f}"
            for item in run.aggregate_rankings
        )
        prompt = (
            "You are the Slock council chair. Synthesize the independent answers and anonymous peer reviews.\n"
            "Return one actionable final answer. Include consensus, disagreements, and the recommended path.\n\n"
            f"# Original Question\n{question}\n\n"
            f"# Independent Answers\n{responses}\n\n"
            f"# Peer Reviews\n{reviews}\n\n"
            f"# Aggregate Ranking\n{aggregate}"
        )
        full_prompt = self._engine.build_agent_prompt(chairman, prompt)
        return self._engine.run_agent_session(chairman, full_prompt, timeout=timeout) or ""

    def _select_chairman(self, participants: list[AgentIdentity]) -> AgentIdentity:
        for role in _CHAIRMAN_ROLES:
            for agent in participants:
                if agent.role == role:
                    return agent
        return participants[0]

    def _record_skill_feedback(self, question: str, run: CouncilRun) -> None:
        if not run.aggregate_rankings:
            return
        try:
            skill_tags = self._engine.router.extract_skill_keywords(question)
        except Exception:
            skill_tags = ["council"]

        for item in run.aggregate_rankings:
            try:
                profiles = self._engine.memory.record_skill_feedback(
                    item.agent_id,
                    skill_tags,
                    quality_score=item.quality_score,
                )
                self._engine.router.set_skill_profiles(item.agent_id, profiles)
            except Exception as exc:
                logger.debug("Council skill feedback failed for %s: %s", item.agent_id, str(exc))

    @staticmethod
    def _emit(on_stage: Optional[Callable[[CouncilRun], None]], run: CouncilRun) -> None:
        if on_stage is None:
            return
        try:
            on_stage(run)
        except Exception as exc:
            logger.debug("Council stage callback failed: %s", str(exc))


def parse_ranking_from_text(text: str, valid_labels: list[str]) -> list[str]:
    """Parse a final ranking and return a complete, de-duplicated label order."""
    ranking_section = text
    if "FINAL RANKING:" in text:
        ranking_section = text.split("FINAL RANKING:", 1)[1]

    valid = set(valid_labels)
    matches = re.findall(r"\d+\.\s*(Response [A-Z])", ranking_section)
    if not matches:
        matches = re.findall(r"Response [A-Z]", ranking_section)

    parsed: list[str] = []
    for label in matches:
        if label in valid and label not in parsed:
            parsed.append(label)

    # Missing labels are appended in original order so aggregate scoring remains total.
    parsed.extend(label for label in valid_labels if label not in parsed)
    return parsed


def calculate_aggregate_rankings(
    reviews: list[CouncilReview],
    responses: list[CouncilResponse],
) -> list[CouncilAggregate]:
    """Aggregate anonymous peer rankings into per-agent quality scores."""
    label_to_response = {response.label: response for response in responses}
    positions: dict[str, list[int]] = defaultdict(list)

    for review in reviews:
        for pos, label in enumerate(review.parsed_ranking, start=1):
            if label in label_to_response:
                positions[label].append(pos)

    total = max(len(responses), 1)
    results: list[CouncilAggregate] = []
    for response in responses:
        ranks = positions.get(response.label) or [total]
        avg_rank = sum(ranks) / len(ranks)
        quality_score = 100.0 if total == 1 else 100.0 - ((avg_rank - 1.0) * (70.0 / (total - 1)))
        quality_score = max(30.0, min(100.0, quality_score))
        results.append(
            CouncilAggregate(
                label=response.label,
                agent_id=response.agent_id,
                agent_name=response.agent_name,
                average_rank=round(avg_rank, 2),
                rankings_count=len(ranks),
                quality_score=round(quality_score, 1),
            )
        )

    results.sort(key=lambda item: (item.average_rank, -item.rankings_count, item.label))
    return results


def _label_for_index(index: int) -> str:
    return f"{_LABEL_PREFIX} {chr(65 + index)}"
