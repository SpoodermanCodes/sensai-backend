import hashlib
import json
from typing import Optional
from api.utils.db import get_new_db_connection, execute_db_operation
from api.config import feedback_patterns_table_name


def _hash_answer(answer: str) -> str:
    """Normalize and hash a wrong answer for deduplication."""
    normalized = " ".join(answer.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


async def get_wrong_answers_for_question(
    question_id: Optional[int],
    task_id: Optional[int],
    limit: int = 50,
) -> list[str]:
    """
    Pull the last `limit` wrong (non-max-score) user answers for a question or task.
    For quiz questions: reads chat_history where question_id matches and the
    immediately following assistant message has at least one criterion below max_score.
    For assignments: reads chat_history where task_id matches and evaluation_status
    is needs_resubmission or the key_area_scores show sub-max scores.
    """
    from api.config import chat_history_table_name

    if question_id:
        rows = await execute_db_operation(
            f"""
            SELECT u.content, a.content
            FROM {chat_history_table_name} u
            JOIN {chat_history_table_name} a
              ON a.user_id = u.user_id
             AND a.question_id = u.question_id
             AND a.role = 'assistant'
             AND a.id = (
                 SELECT MIN(id) FROM {chat_history_table_name}
                 WHERE user_id = u.user_id
                   AND question_id = u.question_id
                   AND role = 'assistant'
                   AND id > u.id
                   AND deleted_at IS NULL
             )
            WHERE u.question_id = ?
              AND u.role = 'user'
              AND u.deleted_at IS NULL
              AND a.deleted_at IS NULL
            ORDER BY u.id DESC
            LIMIT ?
            """,
            (question_id, limit),
            fetch_all=True,
        )
    elif task_id:
        rows = await execute_db_operation(
            f"""
            SELECT u.content, a.content
            FROM {chat_history_table_name} u
            JOIN {chat_history_table_name} a
              ON a.user_id = u.user_id
             AND a.task_id = u.task_id
             AND a.role = 'assistant'
             AND a.id = (
                 SELECT MIN(id) FROM {chat_history_table_name}
                 WHERE user_id = u.user_id
                   AND task_id = u.task_id
                   AND role = 'assistant'
                   AND id > u.id
                   AND deleted_at IS NULL
             )
            WHERE u.task_id = ?
              AND u.role = 'user'
              AND u.deleted_at IS NULL
              AND a.deleted_at IS NULL
            ORDER BY u.id DESC
            LIMIT ?
            """,
            (task_id, limit),
            fetch_all=True,
        )
    else:
        return []

    wrong_answers = []
    for user_content, ai_content in (rows or []):
        if not user_content or not ai_content:
            continue
        try:
            ai_data = json.loads(ai_content)
        except (json.JSONDecodeError, TypeError):
            continue

        # Quiz: check scorecard for any below-max criterion
        if "scorecard" in ai_data and ai_data["scorecard"]:
            scorecard = ai_data["scorecard"]
            has_wrong = any(
                isinstance(v, dict) and v.get("score", 0) < v.get("max_score", 1)
                for v in scorecard.values()
            )
            if has_wrong:
                wrong_answers.append(user_content)

        # Assignment: check key_area_scores or needs_resubmission
        elif ai_data.get("evaluation_status") == "needs_resubmission":
            wrong_answers.append(user_content)
        elif "key_area_scores" in ai_data and ai_data["key_area_scores"]:
            scores = ai_data["key_area_scores"]
            has_wrong = any(
                isinstance(v, dict) and v.get("score", 0) < v.get("max_score", 1)
                for v in scores.values()
            )
            if has_wrong:
                wrong_answers.append(user_content)

    return wrong_answers


async def upsert_feedback_pattern(
    pattern_summary: str,
    example_snippet: str,
    question_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> None:
    """Insert a new pattern or increment occurrence_count if the summary already exists."""
    pattern_hash = _hash_answer(pattern_summary)

    async with get_new_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            f"""
            INSERT INTO {feedback_patterns_table_name}
                (question_id, task_id, pattern_hash, pattern_summary, example_snippet, occurrence_count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(pattern_hash) DO UPDATE SET
                occurrence_count = occurrence_count + 1,
                example_snippet  = excluded.example_snippet,
                updated_at       = CURRENT_TIMESTAMP
            """,
            (question_id, task_id, pattern_hash, pattern_summary, example_snippet),
        )
        await conn.commit()


async def get_common_errors_for_question(
    question_id: Optional[int] = None,
    task_id: Optional[int] = None,
    min_occurrences: int = 2,
    limit: int = 3,
) -> str:
    """
    Return a formatted string of the top recurring error patterns for prompt injection.
    Returns empty string if no patterns exist yet (graceful degradation).
    """
    if question_id:
        rows = await execute_db_operation(
            f"""
            SELECT pattern_summary, occurrence_count
            FROM {feedback_patterns_table_name}
            WHERE question_id = ?
              AND occurrence_count >= ?
            ORDER BY occurrence_count DESC
            LIMIT ?
            """,
            (question_id, min_occurrences, limit),
            fetch_all=True,
        )
    elif task_id:
        rows = await execute_db_operation(
            f"""
            SELECT pattern_summary, occurrence_count
            FROM {feedback_patterns_table_name}
            WHERE task_id = ?
              AND occurrence_count >= ?
            ORDER BY occurrence_count DESC
            LIMIT ?
            """,
            (task_id, min_occurrences, limit),
            fetch_all=True,
        )
    else:
        return ""

    if not rows:
        return ""

    lines = [f"- {row[0]} (seen {row[1]} time{'s' if row[1] != 1 else ''})" for row in rows]
    return "\n".join(lines)
